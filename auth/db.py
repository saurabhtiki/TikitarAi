import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from auth.exceptions import AuthDatabaseError, DuplicateEmailError, ProtectedAccountError
from auth.passwords import hash_password

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

SEED_ADMIN_EMAIL = "admin@admin.com"
SEED_ADMIN_PASSWORD = "nimda"
SEED_ADMIN_NAME = "Admin"
# SQLite AUTOINCREMENT guarantees the first-ever inserted row keeps id 1 forever (ids are
# never reused), and seeding only ever fires on an empty table, so the seeded admin is
# always user_id 1. Identifying it this way (rather than by email) keeps protection intact
# even if a superuser edits the seeded admin's email later.
SEED_ADMIN_USER_ID = 1

_CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    photo_path    TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('superuser', 'admin', 'normal_user')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit.

    A short-lived connection per call is used rather than a shared cached
    connection, since sqlite3 connections are not safe to share across
    Streamlit's per-session threads.

    Raises:
        AuthDatabaseError: if the connection or any statement inside the
            `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise AuthDatabaseError(f"Could not open the user database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise AuthDatabaseError("A database operation failed while accessing user accounts.") from error
    finally:
        connection.close()


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the users table if it doesn't already exist. Safe to call every process start."""
    with get_connection(db_path) as connection:
        connection.execute(_CREATE_USERS_TABLE)


def seed_default_admin(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Inserts the default superuser account, but only if the users table is currently empty.

    Idempotent: safe to call on every process start, and guarantees exactly
    one seeded account even across repeated app restarts.
    """
    with get_connection(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM users;").fetchone()[0]
        if row_count > 0:
            return
        password_hash = hash_password(SEED_ADMIN_PASSWORD)
        connection.execute(
            "INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, ?);",
            (SEED_ADMIN_EMAIL, SEED_ADMIN_NAME, password_hash, "superuser"),
        )
        logger.info("Seeded default superuser account %s.", SEED_ADMIN_EMAIL)


def get_user_by_email(email: str, db_path: Path | str = DEFAULT_DB_PATH) -> dict | None:
    """Looks up a user by email. Returns None if no matching row exists."""
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE email = ?;", (email,)).fetchone()
        return dict(row) if row is not None else None


def get_user_by_id(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> dict | None:
    """Looks up a user by user_id. Returns None if no matching row exists."""
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,)).fetchone()
        return dict(row) if row is not None else None


def list_users(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Returns every user, ordered by name, for display on the User Management screen."""
    with get_connection(db_path) as connection:
        rows = connection.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE;").fetchall()
        return [dict(row) for row in rows]


def create_user(
    email: str,
    name: str,
    password: str,
    role: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    """Creates a new user account with the given role and an initial password.

    Raises:
        DuplicateEmailError: if the email is already registered.
        AuthDatabaseError: on any other database failure.
    """
    try:
        with get_connection(db_path) as connection:
            password_hash = hash_password(password)
            cursor = connection.execute(
                "INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, ?);",
                (email, name, password_hash, role),
            )
            new_id = cursor.lastrowid
    except AuthDatabaseError as error:
        if isinstance(error.__cause__, sqlite3.IntegrityError):
            logger.warning("Attempted to create a duplicate user for email %s.", email)
            raise DuplicateEmailError(f"An account with email {email} already exists.") from error.__cause__
        raise

    return get_user_by_id(new_id, db_path)


def update_user(
    user_id: int,
    name: str,
    email: str,
    role: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict:
    """Updates a user's name, email, and role. Does not touch password_hash or photo_path,
    since those are self-service only (Section 2.4).

    Raises:
        ProtectedAccountError: if user_id is the seeded superuser account and role would change.
        DuplicateEmailError: if email collides with a different existing user.
        AuthDatabaseError: on any other database failure, or if user_id does not exist.
    """
    try:
        with get_connection(db_path) as connection:
            current = connection.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,)).fetchone()
            if current is None:
                raise AuthDatabaseError(f"No user found with id {user_id}.")
            if user_id == SEED_ADMIN_USER_ID and role != "superuser":
                logger.warning("Blocked role change away from superuser for the seeded admin account.")
                raise ProtectedAccountError("The default superuser account's role cannot be changed.")

            connection.execute(
                "UPDATE users SET name = ?, email = ?, role = ? WHERE user_id = ?;",
                (name, email, role, user_id),
            )
    except AuthDatabaseError as error:
        if isinstance(error.__cause__, sqlite3.IntegrityError):
            logger.warning("Attempted to rename a user to a duplicate email %s.", email)
            raise DuplicateEmailError(f"An account with email {email} already exists.") from error.__cause__
        raise

    return get_user_by_id(user_id, db_path)


def delete_user(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Deletes a user account.

    Raises:
        ProtectedAccountError: if user_id is the seeded superuser account.
        AuthDatabaseError: on any other database failure, or if user_id does not exist.
    """
    with get_connection(db_path) as connection:
        current = connection.execute("SELECT * FROM users WHERE user_id = ?;", (user_id,)).fetchone()
        if current is None:
            raise AuthDatabaseError(f"No user found with id {user_id}.")
        if user_id == SEED_ADMIN_USER_ID:
            logger.warning("Blocked delete attempt on the seeded admin account.")
            raise ProtectedAccountError("The default superuser account can never be deleted.")

        connection.execute("DELETE FROM users WHERE user_id = ?;", (user_id,))
