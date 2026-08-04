import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from llm.crypto import encrypt_api_key
from llm.exceptions import LLMDatabaseError

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

_CREATE_LLM_PROFILES_TABLE = """
CREATE TABLE IF NOT EXISTS llm_profiles (
    profile_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    nickname          TEXT NOT NULL,
    provider_type     TEXT NOT NULL CHECK (provider_type IN ('local', 'cloud', 'custom')),
    base_url          TEXT,
    api_key_encrypted TEXT,
    default_model     TEXT NOT NULL,
    is_light_model    INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See auth.db.get_connection
    for the rationale (connections aren't safe to share across Streamlit's per-session
    threads, so one is opened per call rather than cached).

    Raises:
        LLMDatabaseError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise LLMDatabaseError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise LLMDatabaseError("A database operation failed while accessing LLM provider profiles.") from error
    finally:
        connection.close()


def init_llm_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the llm_profiles table if it doesn't already exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_LLM_PROFILES_TABLE)


def list_profiles(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Returns every LLM profile owned by user_id, ordered by nickname. Includes the
    light-model profile, if one is set, distinguished by is_light_model."""
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM llm_profiles WHERE user_id = ? ORDER BY nickname COLLATE NOCASE;",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _get_owned_profile(connection: sqlite3.Connection, profile_id: int, user_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM llm_profiles WHERE profile_id = ? AND user_id = ?;",
        (profile_id, user_id),
    ).fetchone()
    if row is None:
        raise LLMDatabaseError(f"No profile {profile_id} found for this account.")
    return row


def create_profile(
    user_id: int,
    nickname: str,
    provider_type: str,
    base_url: str | None,
    api_key: str | None,
    default_model: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    """Creates a new LLM provider profile. api_key is encrypted before storage if provided,
    otherwise stored as NULL (e.g. for local providers)."""
    api_key_encrypted = encrypt_api_key(api_key, key_path=Path(db_path).with_name("encryption.key")) if api_key else None
    with _get_connection(db_path) as connection:
        cursor = connection.execute(
            """INSERT INTO llm_profiles (user_id, nickname, provider_type, base_url, api_key_encrypted, default_model)
               VALUES (?, ?, ?, ?, ?, ?);""",
            (user_id, nickname, provider_type, base_url, api_key_encrypted, default_model),
        )
        new_id = cursor.lastrowid

    with _get_connection(db_path) as connection:
        return dict(_get_owned_profile(connection, new_id, user_id))


def update_profile(
    profile_id: int,
    user_id: int,
    nickname: str,
    provider_type: str,
    base_url: str | None,
    api_key: str | None,
    default_model: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    """Updates a profile owned by user_id.

    api_key semantics: None leaves the stored key unchanged, "" clears it, any other
    value replaces it (re-encrypted).

    Raises:
        LLMDatabaseError: if profile_id doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        current = _get_owned_profile(connection, profile_id, user_id)

        if api_key is None:
            api_key_encrypted = current["api_key_encrypted"]
        elif api_key == "":
            api_key_encrypted = None
        else:
            api_key_encrypted = encrypt_api_key(api_key, key_path=Path(db_path).with_name("encryption.key"))

        connection.execute(
            """UPDATE llm_profiles
               SET nickname = ?, provider_type = ?, base_url = ?, api_key_encrypted = ?, default_model = ?
               WHERE profile_id = ? AND user_id = ?;""",
            (nickname, provider_type, base_url, api_key_encrypted, default_model, profile_id, user_id),
        )

    with _get_connection(db_path) as connection:
        return dict(_get_owned_profile(connection, profile_id, user_id))


def delete_profile(profile_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Deletes a profile owned by user_id.

    Raises:
        LLMDatabaseError: if profile_id doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_profile(connection, profile_id, user_id)
        connection.execute("DELETE FROM llm_profiles WHERE profile_id = ? AND user_id = ?;", (profile_id, user_id))


def set_light_model(profile_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> dict:
    """Flags profile_id as user_id's light model, clearing the flag from any other profile
    of theirs first, so at most one light model ever exists per user."""
    with _get_connection(db_path) as connection:
        _get_owned_profile(connection, profile_id, user_id)
        connection.execute("UPDATE llm_profiles SET is_light_model = 0 WHERE user_id = ?;", (user_id,))
        connection.execute(
            "UPDATE llm_profiles SET is_light_model = 1 WHERE profile_id = ? AND user_id = ?;",
            (profile_id, user_id),
        )

    with _get_connection(db_path) as connection:
        return dict(_get_owned_profile(connection, profile_id, user_id))


def unset_light_model(profile_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> dict:
    """Clears the light-model flag from profile_id, leaving it as a regular profile."""
    with _get_connection(db_path) as connection:
        _get_owned_profile(connection, profile_id, user_id)
        connection.execute(
            "UPDATE llm_profiles SET is_light_model = 0 WHERE profile_id = ? AND user_id = ?;",
            (profile_id, user_id),
        )

    with _get_connection(db_path) as connection:
        return dict(_get_owned_profile(connection, profile_id, user_id))
