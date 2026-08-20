"""Saved cleaning templates in SQLite.

Follows `tasks/db.py` line for line, which follows `checks/db.py` and `chat_types/db.py`:
a short-lived connection per call (connections aren't safe to share across Streamlit's
per-session threads), and every read or write scoped to the owning `user_id`, so one
account cannot open another's templates.

One name per account. Saving a template whose name is already taken **updates that one**,
rather than hitting the unique index with an error the user can do nothing useful about —
re-saving after cleaning one more column is the normal way to use this, not an exception
to it. That is also what makes "Update template" a plain call to `save_template`.

What is stored is a recipe. `cleaner.template.to_json` is where that is enforced; nothing
here inspects the payload.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cleaner.exceptions import TemplateStorageError
from cleaner.template import CleaningTemplate, from_json, to_json

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

_CREATE_TEMPLATES_TABLE = """
CREATE TABLE IF NOT EXISTS cleaning_templates (
    template_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    template_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_NAME_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_cleaning_templates_user_name
ON cleaning_templates (user_id, name COLLATE NOCASE);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `auth.db.get_connection`.

    Raises:
        TemplateStorageError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise TemplateStorageError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise TemplateStorageError(
            "A database operation failed while accessing saved cleaning templates."
        ) from error
    finally:
        connection.close()


def init_cleaning_templates_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the templates table if it doesn't exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_TEMPLATES_TABLE)
        connection.execute(_CREATE_NAME_INDEX)


def list_templates(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """The templates owned by user_id, newest edit first, without their JSON.

    The JSON is left out because this feeds a picker: reading a hundred full recipes to draw
    a dropdown is work nobody asked for. `load_template` fetches the one that gets chosen.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT template_id, user_id, name, description, created_at, updated_at "
            "FROM cleaning_templates WHERE user_id = ? ORDER BY updated_at DESC;",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _get_owned_template(
    connection: sqlite3.Connection, template_id: int, user_id: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM cleaning_templates WHERE template_id = ? AND user_id = ?;",
        (template_id, user_id),
    ).fetchone()
    if row is None:
        raise TemplateStorageError(f"No cleaning template {template_id} found for this account.")
    return row


def load_template(
    template_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> CleaningTemplate:
    """Reads one saved template back.

    Raises:
        TemplateStorageError: if it doesn't belong to user_id, or its JSON can't be read.
    """
    with _get_connection(db_path) as connection:
        row = _get_owned_template(connection, template_id, user_id)
    return from_json(
        row["template_json"],
        template_id=row["template_id"],
        name=row["name"],
        description=row["description"] or "",
    )


def save_template(
    user_id: int, template: CleaningTemplate, db_path: Path | str = DEFAULT_DB_PATH
) -> CleaningTemplate:
    """Inserts or updates a template, and returns it carrying the `template_id` it now has.

    Raises:
        TemplateStorageError: if the name is blank, or on a database failure.
    """
    name = (template.name or "").strip()
    if not name:
        raise TemplateStorageError("Give this template a name before saving it.")

    # Serialised before the connection is opened, so a payload that refuses to build fails
    # without having held a write transaction open while it did.
    payload = to_json(template)
    description = (template.description or "").strip()

    with _get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT template_id FROM cleaning_templates WHERE user_id = ? AND name = ? COLLATE NOCASE;",
            (user_id, name),
        ).fetchone()
        target_id = existing["template_id"] if existing is not None else template.template_id

        if target_id is not None:
            _get_owned_template(connection, target_id, user_id)
            connection.execute(
                "UPDATE cleaning_templates SET name = ?, description = ?, template_json = ?, "
                "updated_at = datetime('now') WHERE template_id = ? AND user_id = ?;",
                (name, description, payload, target_id, user_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO cleaning_templates (user_id, name, description, template_json) "
                "VALUES (?, ?, ?, ?);",
                (user_id, name, description, payload),
            )
            target_id = cursor.lastrowid

    logger.info("Saved cleaning template '%s' (%s) for user %s.", name, target_id, user_id)
    template.template_id = target_id
    template.name = name
    return template


def delete_template(template_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Deletes a template owned by user_id.

    Raises:
        TemplateStorageError: if it doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_template(connection, template_id, user_id)
        connection.execute(
            "DELETE FROM cleaning_templates WHERE template_id = ? AND user_id = ?;",
            (template_id, user_id),
        )
    logger.info("Deleted cleaning template %s for user %s.", template_id, user_id)
