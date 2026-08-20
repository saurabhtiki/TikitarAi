"""Saved Tasks in SQLite (requirement 7.6).

Follows `checks/db.py` and `chat_types/db.py` exactly: a short-lived connection per call
(connections aren't safe to share across Streamlit's per-session threads), and every read or
write scoped to the owning `user_id`, so one account cannot open another's Tasks.

One name per account. Saving a Task whose name is already taken **updates that one**, rather
than hitting the unique index with an error the user can do nothing useful about — the same
call `checks.db.save_set` makes, and for the same reason: re-saving after an edit is the
normal way to use this, not an exception to it.

What is stored is a recipe. `tasks.model.to_json` is where that is enforced; nothing here
inspects the payload.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from tasks.exceptions import TaskStorageError
from tasks.model import Task, from_json, to_json

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

_CREATE_TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    task_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_NAME_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_user_name
ON tasks (user_id, name COLLATE NOCASE);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `auth.db.get_connection`.

    Raises:
        TaskStorageError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise TaskStorageError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise TaskStorageError("A database operation failed while accessing saved tasks.") from error
    finally:
        connection.close()


def init_tasks_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the tasks table if it doesn't already exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_TASKS_TABLE)
        connection.execute(_CREATE_NAME_INDEX)


def list_tasks(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """The Tasks owned by user_id, newest edit first, without their JSON.

    The JSON is left out because this feeds a picker: loading a dozen full recipes to draw a
    dropdown is work nobody asked for. `load_task` fetches the one that gets chosen.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT task_id, user_id, name, description, created_at, updated_at FROM tasks "
            "WHERE user_id = ? ORDER BY updated_at DESC;",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _get_owned_task(connection: sqlite3.Connection, task_id: int, user_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM tasks WHERE task_id = ? AND user_id = ?;",
        (task_id, user_id),
    ).fetchone()
    if row is None:
        raise TaskStorageError(f"No task {task_id} found for this account.")
    return row


def load_task(task_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> Task:
    """Reads one saved Task back.

    Raises:
        TaskStorageError: if the Task doesn't belong to user_id, or its JSON can't be read.
    """
    with _get_connection(db_path) as connection:
        row = _get_owned_task(connection, task_id, user_id)
    return from_json(
        row["task_json"], task_id=row["task_id"], name=row["name"], description=row["description"] or ""
    )


def save_task(user_id: int, task: Task, db_path: Path | str = DEFAULT_DB_PATH) -> Task:
    """Inserts or updates a Task, and returns it carrying the `task_id` it now has.

    Raises:
        TaskStorageError: if the name is blank, or on a database failure.
    """
    name = (task.name or "").strip()
    if not name:
        raise TaskStorageError("Give this task a name before saving it.")

    # Serialised before the connection is opened, so a payload that refuses to build fails
    # without having held a write transaction open while it did.
    payload = to_json(task)
    description = (task.description or "").strip()

    with _get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT task_id FROM tasks WHERE user_id = ? AND name = ? COLLATE NOCASE;",
            (user_id, name),
        ).fetchone()
        target_id = existing["task_id"] if existing is not None else task.task_id

        if target_id is not None:
            _get_owned_task(connection, target_id, user_id)
            connection.execute(
                "UPDATE tasks SET name = ?, description = ?, task_json = ?, "
                "updated_at = datetime('now') WHERE task_id = ? AND user_id = ?;",
                (name, description, payload, target_id, user_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO tasks (user_id, name, description, task_json) VALUES (?, ?, ?, ?);",
                (user_id, name, description, payload),
            )
            target_id = cursor.lastrowid

    logger.info("Saved task '%s' (%s) for user %s.", name, target_id, user_id)
    task.task_id = target_id
    task.name = name
    return task


def delete_task(task_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Deletes a Task owned by user_id.

    Raises:
        TaskStorageError: if the Task doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_task(connection, task_id, user_id)
        connection.execute("DELETE FROM tasks WHERE task_id = ? AND user_id = ?;", (task_id, user_id))
    logger.info("Deleted task %s for user %s.", task_id, user_id)
