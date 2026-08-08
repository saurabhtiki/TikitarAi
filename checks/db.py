"""Saved criteria sets in SQLite (requirement 6.5).

The one part of this feature that outlives a session, and the reason it differs from the
Dashboard: requirement 6.3 makes a dashboard session-only because a dashboard is assembled
in one sitting, but a criteria set exists precisely to be run again against next month's
file. Retyping a dozen tested rules every month would make the feature not worth using.

What is stored is a **recipe, not a result**: the persona, the criteria text, the hints and
the SQL. No rows — `checks.model.to_json` drops them, for the reason its docstring gives.

Follows `llm/db.py` exactly: a short-lived connection per call (connections aren't safe to
share across Streamlit's per-session threads), and every read or write scoped to the owning
`user_id`, so one account cannot open another's sets.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from checks.exceptions import ChecksStorageError
from checks.model import CheckSet, from_json, to_json

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

_CREATE_CHECK_SETS_TABLE = """
CREATE TABLE IF NOT EXISTS check_sets (
    set_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# One name per account, so "Save" on a set the user has already saved updates it instead of
# quietly accumulating six sets called "Payroll checks" they then have to tell apart.
_CREATE_NAME_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_check_sets_user_name
ON check_sets (user_id, name COLLATE NOCASE);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `auth.db.get_connection`
    for the rationale.

    Raises:
        ChecksStorageError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise ChecksStorageError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise ChecksStorageError("A database operation failed while accessing saved criteria sets.") from error
    finally:
        connection.close()


def init_check_sets_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the check_sets table if it doesn't already exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_CHECK_SETS_TABLE)
        connection.execute(_CREATE_NAME_INDEX)


def list_sets(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Every criteria set owned by user_id, newest edit first, without its JSON.

    The JSON is left out because this feeds a picker: loading a dozen full recipes to draw
    a dropdown is work nobody asked for. `load_set` fetches the one that gets chosen.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT set_id, user_id, name, created_at, updated_at FROM check_sets "
            "WHERE user_id = ? ORDER BY updated_at DESC;",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _get_owned_set(connection: sqlite3.Connection, set_id: int, user_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM check_sets WHERE set_id = ? AND user_id = ?;",
        (set_id, user_id),
    ).fetchone()
    if row is None:
        raise ChecksStorageError(f"No criteria set {set_id} found for this account.")
    return row


def load_set(set_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> CheckSet:
    """Reads one saved set back into a `CheckSet`.

    The returned checks carry their SQL but no `saved_run` — see `model._check_from_dict`.

    Raises:
        ChecksStorageError: if the set doesn't belong to user_id, or its JSON can't be read.
    """
    with _get_connection(db_path) as connection:
        row = _get_owned_set(connection, set_id, user_id)
    return from_json(row["checks_json"], set_id=row["set_id"], name=row["name"])


def save_set(user_id: int, check_set: CheckSet, db_path: Path | str = DEFAULT_DB_PATH) -> CheckSet:
    """Inserts or updates a set, and returns it carrying the `set_id` it now has.

    Matched on name rather than only on `set_id`, so that a set the user built from scratch
    and then named the same as an existing one updates that one instead of hitting the
    unique index with an error they can do nothing useful about.

    Raises:
        ChecksStorageError: if the name is blank, or on a database failure.
    """
    name = (check_set.name or "").strip()
    if not name:
        raise ChecksStorageError("Give this criteria set a name before saving it.")

    payload = to_json(check_set)

    with _get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT set_id FROM check_sets WHERE user_id = ? AND name = ? COLLATE NOCASE;",
            (user_id, name),
        ).fetchone()
        target_id = existing["set_id"] if existing is not None else check_set.set_id

        if target_id is not None:
            _get_owned_set(connection, target_id, user_id)
            connection.execute(
                "UPDATE check_sets SET name = ?, checks_json = ?, updated_at = datetime('now') "
                "WHERE set_id = ? AND user_id = ?;",
                (name, payload, target_id, user_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO check_sets (user_id, name, checks_json) VALUES (?, ?, ?);",
                (user_id, name, payload),
            )
            target_id = cursor.lastrowid

    logger.info("Saved criteria set '%s' (%s) for user %s.", name, target_id, user_id)
    check_set.set_id = target_id
    check_set.name = name
    return check_set


def delete_set(set_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Deletes a set owned by user_id.

    Raises:
        ChecksStorageError: if the set doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_set(connection, set_id, user_id)
        connection.execute("DELETE FROM check_sets WHERE set_id = ? AND user_id = ?;", (set_id, user_id))
    logger.info("Deleted criteria set %s for user %s.", set_id, user_id)
