"""Which reports have saved data, and what that data looks like (Phase 13).

Same shape as `tasks/db.py` — a short-lived connection per call, because connections
aren't safe to share across Streamlit's per-session threads — with **one deliberate
difference: nothing here is scoped by `user_id`.**

That is the point of this phase, not an oversight. A report's *recipe* stays owned by the
account that built it, exactly as `tasks/db.py` has it. A report's *data* is company
data: whoever ran the month refreshed it, and anyone may ask questions of it. So
`list_reports_with_data` deliberately returns every report that has data, whoever owns it,
and `record_dataset` lets any account overwrite any report's data.

What is stored here is the small stuff: the schema block the chat needs (see
`reports/model.py`), and who refreshed the data and when, which is what the chat shows at
the top so nobody reasons about figures without knowing how old they are. The rows
themselves live in `reports/store.py`'s DuckDB files.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from reports.exceptions import ReportDataError
from reports.model import ReportSetup, from_json, to_json

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

# `task_id` is the primary key, not merely a reference: a report has exactly one dataset,
# because only the latest data is kept. The cascade means deleting the report clears this
# row — the file beside it still has to be deleted explicitly, which `delete_dataset` does.
_CREATE_DATASETS_TABLE = """
CREATE TABLE IF NOT EXISTS report_datasets (
    task_id      INTEGER PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
    setup_json   TEXT NOT NULL,
    row_count    INTEGER NOT NULL DEFAULT 0,
    refreshed_by TEXT NOT NULL DEFAULT '',
    refreshed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `tasks.db._get_connection`.

    The table is created here rather than only at start-up, unlike every other `db.py` in
    this app. The difference is who calls it: saving a dataset happens **inside a report
    run**, and a missing table would turn a finished report into a failed step over a
    schema that costs nothing to guarantee. `CREATE TABLE IF NOT EXISTS` is a no-op on
    every call after the first.

    Raises:
        ReportDataError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute(_CREATE_DATASETS_TABLE)
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise ReportDataError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise ReportDataError("A database operation failed while accessing saved report data.") from error
    finally:
        connection.close()


def init_report_datasets_table(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates the table if it doesn't exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_DATASETS_TABLE)


def record_dataset(
    task_id: int,
    setup: ReportSetup,
    refreshed_by: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Records that this report's data has just been replaced.

    Not scoped by user, by design — see the module docstring.

    Raises:
        ReportDataError: if the setup can't be serialised, or the write fails.
    """
    payload = to_json(setup)
    with _get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO report_datasets (task_id, setup_json, row_count, refreshed_by, refreshed_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "setup_json = excluded.setup_json, row_count = excluded.row_count, "
            "refreshed_by = excluded.refreshed_by, refreshed_at = excluded.refreshed_at;",
            (int(task_id), payload, setup.total_rows(), str(refreshed_by or "")),
        )
    logger.info("Recorded a refreshed dataset for report %s by %s.", task_id, refreshed_by)


def load_setup(task_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> ReportSetup:
    """Reads back the schema the chat needs for one report's saved data.

    Raises:
        ReportDataError: if this report has no saved data, or its schema can't be read.
    """
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT setup_json FROM report_datasets WHERE task_id = ?;", (int(task_id),)
        ).fetchone()

    if row is None:
        raise ReportDataError("This report has no saved data yet — run it once first.")
    return from_json(row["setup_json"])


def dataset_info(task_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> dict | None:
    """When this report's data was last refreshed and by whom, or None if never."""
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT task_id, row_count, refreshed_by, refreshed_at FROM report_datasets WHERE task_id = ?;",
            (int(task_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def list_reports_with_data(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Every report that has data saved, newest refresh first — **whoever owns it**.

    The join reaches into `tasks` for the name and the owner's name only. The owner is
    shown, not enforced: the list is what the "Chat with reports" picker offers, and a
    report anybody can refresh is a report anybody can read.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT d.task_id, t.name, t.description, u.name AS owner_name, "
            "d.row_count, d.refreshed_by, d.refreshed_at "
            "FROM report_datasets AS d "
            "JOIN tasks AS t ON t.task_id = d.task_id "
            "LEFT JOIN users AS u ON u.user_id = t.user_id "
            "ORDER BY d.refreshed_at DESC;"
        ).fetchall()
    return [dict(row) for row in rows]


def forget_dataset(task_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Removes the record of a report's data.

    Called alongside `reports.store.delete_dataset`, which removes the file itself. Kept
    separate because the cascade already clears this row when the report is deleted, and a
    dataset can also be dropped on its own.
    """
    with _get_connection(db_path) as connection:
        connection.execute("DELETE FROM report_datasets WHERE task_id = ?;", (int(task_id),))
    logger.info("Forgot the saved dataset record for report %s.", task_id)
