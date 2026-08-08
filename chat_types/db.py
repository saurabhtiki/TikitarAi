"""Saved chat types in SQLite (requirement 6.6).

Follows `checks/db.py` line for line, and for the same reasons: a short-lived connection
per call (connections aren't safe to share across Streamlit's per-session threads), and
every read or write scoped to the owning `user_id`, so one account cannot open another's
setups.

What is stored is a **recipe, not data**: the expected tables and their column types, the
links, and the column descriptions. No rows — see `chat_types.model`.

One cross-table concern lives here. A criteria set can belong to a chat type
(`check_sets.chat_type_id`), and deleting the chat type must not take the set with it: a
tested set of rules is expensive to rebuild, and a set with no chat type is still perfectly
usable. `delete_type` therefore un-scopes them explicitly rather than declaring
`ON DELETE SET NULL` on the column — a real foreign key would force `check_sets` to be
created after `chat_types` on every fresh database and in every test fixture, which is a
lot of ordering to buy one clause.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from chat_types.exceptions import ChatTypeStorageError
from chat_types.model import ChatType, from_json, to_json

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

_CREATE_CHAT_TYPES_TABLE = """
CREATE TABLE IF NOT EXISTS chat_types (
    chat_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    config_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# One name per account, so "Save" on a chat type the user has already saved updates it
# instead of quietly accumulating six setups called "Salary processing".
_CREATE_NAME_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_types_user_name
ON chat_types (user_id, name COLLATE NOCASE);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `auth.db.get_connection`
    for the rationale.

    Raises:
        ChatTypeStorageError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise ChatTypeStorageError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise ChatTypeStorageError("A database operation failed while accessing saved chat types.") from error
    finally:
        connection.close()


def init_chat_types_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the chat_types table if it doesn't already exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_CHAT_TYPES_TABLE)
        connection.execute(_CREATE_NAME_INDEX)


def list_types(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Every chat type owned by user_id, newest edit first, without its JSON.

    The JSON is left out because this feeds a picker rendered on every run of the page:
    parsing a dozen full setups to draw a dropdown is work nobody asked for. `load_type`
    fetches the one that gets chosen.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT chat_type_id, user_id, name, created_at, updated_at FROM chat_types "
            "WHERE user_id = ? ORDER BY updated_at DESC;",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _get_owned_type(connection: sqlite3.Connection, chat_type_id: int, user_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM chat_types WHERE chat_type_id = ? AND user_id = ?;",
        (chat_type_id, user_id),
    ).fetchone()
    if row is None:
        raise ChatTypeStorageError(f"No chat type {chat_type_id} found for this account.")
    return row


def load_type(chat_type_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> ChatType:
    """Reads one saved chat type back into a `ChatType`.

    Raises:
        ChatTypeStorageError: if it doesn't belong to user_id, or its JSON can't be read.
    """
    with _get_connection(db_path) as connection:
        row = _get_owned_type(connection, chat_type_id, user_id)
    return from_json(row["config_json"], chat_type_id=row["chat_type_id"], name=row["name"])


def save_type(user_id: int, chat_type: ChatType, db_path: Path | str = DEFAULT_DB_PATH) -> ChatType:
    """Inserts or updates a chat type, and returns it carrying the id it now has.

    Matched on name rather than only on `chat_type_id`, so that a setup the user built from
    scratch and then named the same as an existing one updates that one instead of hitting
    the unique index with an error they can do nothing useful about.

    Raises:
        ChatTypeStorageError: if the name is blank, or on a database failure.
    """
    name = (chat_type.name or "").strip()
    if not name:
        raise ChatTypeStorageError("Give this chat type a name before saving it.")

    payload = to_json(chat_type)

    with _get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT chat_type_id FROM chat_types WHERE user_id = ? AND name = ? COLLATE NOCASE;",
            (user_id, name),
        ).fetchone()
        target_id = existing["chat_type_id"] if existing is not None else chat_type.chat_type_id

        if target_id is not None:
            _get_owned_type(connection, target_id, user_id)
            connection.execute(
                "UPDATE chat_types SET name = ?, config_json = ?, updated_at = datetime('now') "
                "WHERE chat_type_id = ? AND user_id = ?;",
                (name, payload, target_id, user_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO chat_types (user_id, name, config_json) VALUES (?, ?, ?);",
                (user_id, name, payload),
            )
            target_id = cursor.lastrowid

    logger.info("Saved chat type '%s' (%s) for user %s.", name, target_id, user_id)
    chat_type.chat_type_id = target_id
    chat_type.name = name
    return chat_type


def _unscope_check_sets(
    connection: sqlite3.Connection, chat_type_id: int, user_id: int, chat_type_name: str
) -> int:
    """Moves a deleted chat type's criteria sets out from under it, renaming any that clash.

    A rename is needed because `check_sets` is unique on
    `(user_id, COALESCE(chat_type_id, 0), name)`: if the user already has an unscoped
    "Payroll checks" and the chat type owns one too, plain un-scoping violates that index —
    which rolls the whole delete back and leaves them with an error and no way forward.
    Suffixing the incoming one keeps both, which is the point of not deleting them.

    `check_sets` may not exist at all on a database that has never opened the Checks view,
    and a missing table there must not block deleting a chat type.
    """
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'check_sets';"
    ).fetchone():
        return 0

    moving = connection.execute(
        "SELECT set_id, name FROM check_sets WHERE chat_type_id = ? AND user_id = ?;",
        (chat_type_id, user_id),
    ).fetchall()
    if not moving:
        return 0

    taken = {
        str(row["name"]).lower()
        for row in connection.execute(
            "SELECT name FROM check_sets WHERE user_id = ? AND chat_type_id IS NULL;", (user_id,)
        ).fetchall()
    }

    for row in moving:
        name = _free_name(str(row["name"]), chat_type_name, taken)
        taken.add(name.lower())
        connection.execute(
            "UPDATE check_sets SET chat_type_id = NULL, name = ? WHERE set_id = ? AND user_id = ?;",
            (name, row["set_id"], user_id),
        )

    return len(moving)


def _free_name(name: str, chat_type_name: str, taken: set[str]) -> str:
    """`name` if nothing else has it, otherwise the same name marked with where it came from."""
    if name.lower() not in taken:
        return name

    candidate = f"{name} ({chat_type_name})"
    suffix = 2
    while candidate.lower() in taken:
        candidate = f"{name} ({chat_type_name} {suffix})"
        suffix += 1
    logger.info("Renamed criteria set '%s' to '%s' while un-scoping it.", name, candidate)
    return candidate


def delete_type(chat_type_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> int:
    """Deletes a chat type owned by user_id, and un-scopes any criteria sets under it.

    The sets deliberately survive as unscoped rather than being deleted alongside: a tested
    set of rules costs far more to rebuild than the setup that happened to own it.

    Returns:
        How many criteria sets were un-scoped, so the page can say so.

    Raises:
        ChatTypeStorageError: if it doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        row = _get_owned_type(connection, chat_type_id, user_id)
        orphaned = _unscope_check_sets(connection, chat_type_id, user_id, row["name"])
        connection.execute(
            "DELETE FROM chat_types WHERE chat_type_id = ? AND user_id = ?;", (chat_type_id, user_id)
        )

    logger.info("Deleted chat type %s for user %s; un-scoped %d criteria set(s).", chat_type_id, user_id, orphaned)
    return orphaned
