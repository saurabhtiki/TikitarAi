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
    set_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    chat_type_id INTEGER,
    name         TEXT NOT NULL,
    checks_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# One name per account **per chat type** (requirement 6.6), so "Save" on a set the user has
# already saved updates it instead of quietly accumulating six sets called "Payroll checks"
# they then have to tell apart — while still allowing one of that name under each setup.
#
# `COALESCE(chat_type_id, 0)` rather than the bare column: SQLite treats NULLs as distinct
# in a unique index, so a plain three-column index would stop de-duplicating exactly the
# unscoped sets this index was written for.
_CREATE_NAME_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_check_sets_user_scope_name
ON check_sets (user_id, COALESCE(chat_type_id, 0), name COLLATE NOCASE);
"""

# The index this replaced. Left in place it would refuse a second "Payroll checks" saved
# under a different chat type, which is the whole point of the new one.
_DROP_OLD_NAME_INDEX = "DROP INDEX IF EXISTS idx_check_sets_user_name;"


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
        _add_chat_type_column(connection)
        connection.execute(_DROP_OLD_NAME_INDEX)
        connection.execute(_CREATE_NAME_INDEX)


def _add_chat_type_column(connection: sqlite3.Connection) -> None:
    """Adds `chat_type_id` to a table created before requirement 6.6 existed.

    There are live rows in `data/tikitarai.db` from Stage 8, so the column has to arrive by
    migration rather than only in `CREATE TABLE`. Guarded on `PRAGMA table_info` because
    SQLite has no `ADD COLUMN IF NOT EXISTS` and this runs on every process start.

    No `REFERENCES chat_types(...)` clause: that would force `chat_types` to be created
    before `check_sets` on every fresh database and in every test fixture, to buy an
    `ON DELETE SET NULL` that `chat_types.db.delete_type` already does explicitly.
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(check_sets);").fetchall()}
    if "chat_type_id" in columns:
        return
    connection.execute("ALTER TABLE check_sets ADD COLUMN chat_type_id INTEGER;")
    logger.info("Added chat_type_id to check_sets.")


def list_sets(
    user_id: int,
    chat_type_id: int | None = None,
    *,
    every_scope: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> list[dict]:
    """The criteria sets owned by user_id, newest edit first, without their JSON.

    Scoped to one chat type by default (requirement 6.6): with a chat type selected the
    picker offers that setup's sets, and with none selected it offers the sets that belong
    to no setup. `every_scope=True` is the page's "show all my sets" escape hatch, so a set
    saved before chat types existed never becomes unreachable.

    The JSON is left out because this feeds a picker: loading a dozen full recipes to draw
    a dropdown is work nobody asked for. `load_set` fetches the one that gets chosen.
    """
    query = (
        "SELECT set_id, user_id, chat_type_id, name, created_at, updated_at FROM check_sets WHERE user_id = ?"
    )
    parameters: list = [user_id]

    if not every_scope:
        if chat_type_id is None:
            query += " AND chat_type_id IS NULL"
        else:
            query += " AND chat_type_id = ?"
            parameters.append(chat_type_id)

    with _get_connection(db_path) as connection:
        rows = connection.execute(f"{query} ORDER BY updated_at DESC;", parameters).fetchall()
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


def save_set(
    user_id: int,
    check_set: CheckSet,
    chat_type_id: int | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> CheckSet:
    """Inserts or updates a set, and returns it carrying the `set_id` it now has.

    Matched on name **within the chat type being saved under**, so that a set the user
    built from scratch and then named the same as an existing one updates that one instead
    of hitting the unique index with an error they can do nothing useful about — while the
    same name under a different setup stays a different set.

    A set always takes the scope it is saved under. Loading an unscoped set while "Salary
    processing" is selected and pressing Save therefore adopts it into that chat type,
    which is the only reading of Save that doesn't need a second control to explain it.

    Raises:
        ChecksStorageError: if the name is blank, or on a database failure.
    """
    name = (check_set.name or "").strip()
    if not name:
        raise ChecksStorageError("Give this criteria set a name before saving it.")

    payload = to_json(check_set)

    with _get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT set_id FROM check_sets "
            "WHERE user_id = ? AND COALESCE(chat_type_id, 0) = COALESCE(?, 0) AND name = ? COLLATE NOCASE;",
            (user_id, chat_type_id, name),
        ).fetchone()
        target_id = existing["set_id"] if existing is not None else check_set.set_id

        if target_id is not None:
            _get_owned_set(connection, target_id, user_id)
            connection.execute(
                "UPDATE check_sets SET name = ?, checks_json = ?, chat_type_id = ?, "
                "updated_at = datetime('now') WHERE set_id = ? AND user_id = ?;",
                (name, payload, chat_type_id, target_id, user_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO check_sets (user_id, chat_type_id, name, checks_json) VALUES (?, ?, ?, ?);",
                (user_id, chat_type_id, name, payload),
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
