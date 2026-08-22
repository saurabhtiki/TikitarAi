"""Saved report themes in SQLite.

A Custom style is a dozen colours and sizes chosen one at a time. Setting them again for
the next report is the kind of work that makes a feature not get used, so a theme is saved
under a name and picked from a list from then on.

Follows `tasks/db.py` exactly, and for the same reasons: a short-lived connection per call
(connections aren't safe to share across Streamlit's per-session threads), every read and
write scoped to the owning `user_id` so one account cannot open another's themes, and one
name per account — saving a theme whose name is taken **updates that one** rather than
hitting the unique index with an error the user can do nothing useful about.

What is stored is the settings JSON `dashboard.custom_style.to_json` produces, never a
stylesheet. The CSS is regenerated from the settings on load, so a theme saved today still
picks up whatever `build_css` learns to do tomorrow.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dashboard.custom_style import StyleSettings, from_json, to_json
from dashboard.exceptions import DashboardError

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

# Enough for a shelf of themes without turning the picker into a search problem.
MAX_THEMES_PER_USER = 30

_CREATE_THEMES_TABLE = """
CREATE TABLE IF NOT EXISTS report_themes (
    theme_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    style_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_NAME_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_report_themes_user_name
ON report_themes (user_id, name COLLATE NOCASE);
"""


class ThemeStorageError(DashboardError):
    """A saved theme couldn't be read or written.

    A `DashboardError` because that is what the report pages already catch at their
    boundary: losing a theme must cost the theme, never the report being built.
    """


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `tasks.db._get_connection`.

    Raises:
        ThemeStorageError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise ThemeStorageError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise ThemeStorageError("A database operation failed while accessing saved themes.") from error
    finally:
        connection.close()


def init_report_themes_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the themes table if it doesn't already exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_THEMES_TABLE)
        connection.execute(_CREATE_NAME_INDEX)


def list_themes(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """The themes owned by user_id, newest edit first, without their JSON.

    The JSON is left out because this feeds a picker — `load_theme` fetches the one chosen.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT theme_id, name, created_at, updated_at FROM report_themes "
            "WHERE user_id = ? ORDER BY updated_at DESC;",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def load_theme(theme_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> StyleSettings:
    """Reads one saved theme back as settings.

    Raises:
        ThemeStorageError: if the theme doesn't belong to user_id.
    """
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT style_json FROM report_themes WHERE theme_id = ? AND user_id = ?;",
            (theme_id, user_id),
        ).fetchone()

    if row is None:
        raise ThemeStorageError(f"No theme {theme_id} found for this account.")

    # `from_json` falls back to the default rather than raising, so a row written by an
    # older version opens as something to edit instead of an error to dismiss.
    return from_json(row["style_json"])


def save_theme(
    user_id: int, name: str, settings: StyleSettings, db_path: Path | str = DEFAULT_DB_PATH
) -> int:
    """Inserts or updates a theme by name, and returns its id.

    Raises:
        ThemeStorageError: if the name is blank, the shelf is full, or the settings can't be
            serialized.
    """
    name = (name or "").strip()
    if not name:
        raise ThemeStorageError("Give this theme a name before saving it.")

    # Serialized before the connection is opened, so a payload that refuses to build fails
    # without having held a write transaction open while it did.
    try:
        payload = to_json(settings)
    except ValueError as error:
        raise ThemeStorageError(str(error)) from error

    with _get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT theme_id FROM report_themes WHERE user_id = ? AND name = ? COLLATE NOCASE;",
            (user_id, name),
        ).fetchone()

        if existing is not None:
            connection.execute(
                "UPDATE report_themes SET style_json = ?, updated_at = datetime('now') "
                "WHERE theme_id = ? AND user_id = ?;",
                (payload, existing["theme_id"], user_id),
            )
            logger.info("Updated report theme '%s' (%s) for user %s.", name, existing["theme_id"], user_id)
            return int(existing["theme_id"])

        # Counted only on the insert path: re-saving a theme that already exists must never
        # be refused for being one too many.
        total = connection.execute(
            "SELECT COUNT(*) AS total FROM report_themes WHERE user_id = ?;", (user_id,)
        ).fetchone()["total"]
        if total >= MAX_THEMES_PER_USER:
            raise ThemeStorageError(
                f"You already have {MAX_THEMES_PER_USER} saved themes. Delete one before "
                "saving another."
            )

        cursor = connection.execute(
            "INSERT INTO report_themes (user_id, name, style_json) VALUES (?, ?, ?);",
            (user_id, name, payload),
        )
        theme_id = int(cursor.lastrowid)

    logger.info("Saved report theme '%s' (%s) for user %s.", name, theme_id, user_id)
    return theme_id


def delete_theme(theme_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Deletes a theme owned by user_id.

    Raises:
        ThemeStorageError: if the theme doesn't belong to user_id, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT theme_id FROM report_themes WHERE theme_id = ? AND user_id = ?;",
            (theme_id, user_id),
        ).fetchone()
        if row is None:
            raise ThemeStorageError(f"No theme {theme_id} found for this account.")
        connection.execute(
            "DELETE FROM report_themes WHERE theme_id = ? AND user_id = ?;", (theme_id, user_id)
        )
    logger.info("Deleted report theme %s for user %s.", theme_id, user_id)
