"""Saved transform pipelines in SQLite.

Follows `cleaner/db.py` line for line, which follows `tasks/db.py`: a short-lived
connection per call (connections aren't safe to share across Streamlit's per-session
threads), and every read or write scoped to the owning `user_id`, so one account cannot
open another's pipelines.

One name per account. Saving a pipeline whose name is already taken **updates that one**,
rather than hitting the unique index with an error the user can do nothing useful about —
re-saving after adding one more step is the normal way to use this, not an exception to
it. That is also what makes "Update pipeline" a plain call to `save_pipeline`.

What is stored is a recipe. `transform.template.to_json` is where that is enforced;
nothing here inspects the payload.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from transform.exceptions import PipelineStorageError
from transform.template import SavedPipeline, from_json, to_json
from utils.env import get_data_dir

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = get_data_dir() / "tikitarai.db"

_CREATE_PIPELINES_TABLE = """
CREATE TABLE IF NOT EXISTS transform_pipelines (
    pipeline_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    pipeline_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_NAME_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_transform_pipelines_user_name
ON transform_pipelines (user_id, name COLLATE NOCASE);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `auth.db.get_connection`.

    Raises:
        PipelineStorageError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise PipelineStorageError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise PipelineStorageError(
            "A database operation failed while accessing saved transform pipelines."
        ) from error
    finally:
        connection.close()


def init_transform_pipelines_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the pipelines table if it doesn't exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_PIPELINES_TABLE)
        connection.execute(_CREATE_NAME_INDEX)


def list_pipelines(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """The pipelines owned by user_id, newest edit first, without their JSON.

    The JSON is left out because this feeds a picker: reading a hundred full recipes to draw
    a dropdown is work nobody asked for. `load_pipeline` fetches the one that gets chosen.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT pipeline_id, user_id, name, description, created_at, updated_at "
            "FROM transform_pipelines WHERE user_id = ? ORDER BY updated_at DESC;",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _get_owned_pipeline(
    connection: sqlite3.Connection, pipeline_id: int, user_id: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM transform_pipelines WHERE pipeline_id = ? AND user_id = ?;",
        (pipeline_id, user_id),
    ).fetchone()
    if row is None:
        raise PipelineStorageError(f"No transform pipeline {pipeline_id} found for this account.")
    return row


def load_pipeline(
    pipeline_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> SavedPipeline:
    """Reads one saved pipeline back.

    Raises:
        PipelineStorageError: if it doesn't belong to user_id, or its JSON can't be read.
    """
    with _get_connection(db_path) as connection:
        row = _get_owned_pipeline(connection, pipeline_id, user_id)
    return from_json(
        row["pipeline_json"],
        pipeline_id=row["pipeline_id"],
        name=row["name"],
        description=row["description"] or "",
    )


def save_pipeline(
    user_id: int, pipeline: SavedPipeline, db_path: Path | str = DEFAULT_DB_PATH
) -> SavedPipeline:
    """Inserts or updates a pipeline, and returns it carrying the `pipeline_id` it now has.

    Raises:
        PipelineStorageError: if the name is blank, or on a database failure.
    """
    name = (pipeline.name or "").strip()
    if not name:
        raise PipelineStorageError("Give this pipeline a name before saving it.")

    # Serialised before the connection is opened, so a payload that refuses to build fails
    # without having held a write transaction open while it did.
    payload = to_json(pipeline)
    description = (pipeline.description or "").strip()

    with _get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT pipeline_id FROM transform_pipelines WHERE user_id = ? AND name = ? COLLATE NOCASE;",
            (user_id, name),
        ).fetchone()
        target_id = existing["pipeline_id"] if existing is not None else pipeline.pipeline_id

        if target_id is not None:
            _get_owned_pipeline(connection, target_id, user_id)
            connection.execute(
                "UPDATE transform_pipelines SET name = ?, description = ?, pipeline_json = ?, "
                "updated_at = datetime('now') WHERE pipeline_id = ? AND user_id = ?;",
                (name, description, payload, target_id, user_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO transform_pipelines (user_id, name, description, pipeline_json) "
                "VALUES (?, ?, ?, ?);",
                (user_id, name, description, payload),
            )
            target_id = cursor.lastrowid

    logger.info("Saved transform pipeline '%s' (%s) for user %s.", name, target_id, user_id)
    pipeline.pipeline_id = target_id
    pipeline.name = name
    return pipeline


def delete_pipeline(pipeline_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Deletes a pipeline owned by user_id.

    Raises:
        PipelineStorageError: if it doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_pipeline(connection, pipeline_id, user_id)
        connection.execute(
            "DELETE FROM transform_pipelines WHERE pipeline_id = ? AND user_id = ?;",
            (pipeline_id, user_id),
        )
    logger.info("Deleted transform pipeline %s for user %s.", pipeline_id, user_id)
