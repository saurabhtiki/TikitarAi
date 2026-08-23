"""A report's saved rows, in one DuckDB file per report (Phase 13).

**One file per report, and only the latest data in it.** A refresh replaces what is there,
so storage stays flat instead of growing a copy per run, and deleting a report is deleting
one file. Nothing here is scoped by user: a report's dataset is shared, so any account may
refresh it and any account may read it.

Two things follow from the file being on disk rather than in memory, and both are the
point of the exercise:

1. **RAM stops being the ceiling.** DuckDB reads the columns a question needs straight out
   of the file, so several people chatting with a large report cost far less than several
   in-memory copies of it would.
2. **The data outlives the session.** That is the whole feature — nobody has to re-upload
   last month's files to ask a question about them.

Only the working tables are stored. `engine.duckdb_session`'s immutable `base_` layer is
what a *rebuild* restores from, and a saved dataset is never rebuilt — it is replaced by
the next run — so copying it would double the file for nothing.

Statements here are written by this module against names it owns, so they go straight to
DuckDB rather than through `engine.guards`, exactly as `duckdb_session.register_table`
does. `engine.guards` stands between the **agent** and the data, and it still does: the
report chat queries through `engine.duckdb_session.run_query` like every other reader.
"""

import logging
from pathlib import Path

import duckdb

from engine.duckdb_session import BASE_TABLE_PREFIX, quote_identifier
from reports.exceptions import ReportDataError
from reports.model import StoredTable

logger = logging.getLogger(__name__)

DEFAULT_DATASET_ROOT = Path("data") / "reports"

# The name the incoming frame is registered under while a table is written. A local name,
# so a table in the store called `frame` can't shadow the view — `register_table`'s reason.
_INCOMING_VIEW = "_report_incoming"


def dataset_path(task_id: int, root: Path | str = DEFAULT_DATASET_ROOT) -> Path:
    """Where one report's data file lives.

    Named from the report's id rather than its title: a title can be edited, and a renamed
    report must not lose the data that belongs to it.
    """
    return Path(root) / f"report_{int(task_id)}.duckdb"


def has_dataset(task_id: int, root: Path | str = DEFAULT_DATASET_ROOT) -> bool:
    """Whether this report has data saved on disk."""
    return dataset_path(task_id, root).exists()


def open_store(task_id: int, root: Path | str = DEFAULT_DATASET_ROOT) -> duckdb.DuckDBPyConnection:
    """Opens (creating if needed) one report's dataset file.

    Only **one** connection per file should exist in the process — DuckDB allows a single
    writer, and sharing one connection is what keeps two sessions off each other. See
    `reports.session.store_connection`, which is where that is arranged.

    Raises:
        ReportDataError: if the file can't be created or opened.
    """
    path = dataset_path(task_id, root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(path))
    except (OSError, duckdb.Error) as error:
        logger.exception("Could not open the dataset file for report %s at %s.", task_id, path)
        raise ReportDataError("This report's saved data couldn't be opened.") from error


def stored_tables(store: duckdb.DuckDBPyConnection) -> list[str]:
    """The table names held in a dataset file, in name order.

    Raises:
        ReportDataError: if the file's catalogue can't be read.
    """
    try:
        rows = store.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
    except duckdb.Error as error:
        logger.exception("Could not list the tables in a report dataset.")
        raise ReportDataError("This report's saved data couldn't be read.") from error

    return [name for (name,) in rows if not str(name).startswith(BASE_TABLE_PREFIX)]


def write_tables(
    store: duckdb.DuckDBPyConnection,
    source: duckdb.DuckDBPyConnection,
    table_names: list[str],
) -> list[StoredTable]:
    """Replaces a report's saved data with the tables currently loaded in `source`.

    Copied one table at a time through a dataframe rather than by attaching one database to
    the other. `ATTACH` would need a second connection to the file open at the same time as
    the shared one, which is the one thing DuckDB will not allow — and the rows were read
    from a dataframe in the first place, so this costs nothing new.

    Tables that are no longer part of the report are dropped, so a report that lost a file
    does not keep answering questions from last month's copy of it.

    Returns:
        What is now in the file, with each table's row count.

    Raises:
        ReportDataError: if any table can't be copied. A refresh that fails part way is
            reported rather than hidden — the run summary says the data wasn't saved.
    """
    written: list[StoredTable] = []

    for table in table_names:
        quoted = quote_identifier(table)
        try:
            frame = source.execute(f"SELECT * FROM {quoted}").df()
        except duckdb.Error as error:
            logger.exception("Could not read table '%s' out of the session for saving.", table)
            raise ReportDataError(f"'{table}' couldn't be read while saving this report's data.") from error

        try:
            store.register(_INCOMING_VIEW, frame)
            try:
                store.execute(f"CREATE OR REPLACE TABLE {quoted} AS SELECT * FROM {_INCOMING_VIEW}")
            finally:
                store.unregister(_INCOMING_VIEW)
        except duckdb.Error as error:
            logger.exception("Could not write table '%s' into a report dataset.", table)
            raise ReportDataError(f"'{table}' couldn't be saved into this report's data.") from error

        written.append(StoredTable(name=table, row_count=int(len(frame))))

    _drop_stale_tables(store, keep=set(table_names))
    logger.info("Saved %s table(s) into a report dataset.", len(written))
    return written


def _drop_stale_tables(store: duckdb.DuckDBPyConnection, keep: set[str]) -> None:
    """Removes tables in the file that this refresh didn't write."""
    for table in stored_tables(store):
        if table in keep:
            continue
        try:
            store.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
            logger.info("Dropped '%s' from a report dataset — it is no longer part of the report.", table)
        except duckdb.Error as error:
            logger.exception("Could not drop the stale table '%s' from a report dataset.", table)
            raise ReportDataError(
                f"'{table}' is no longer part of this report but couldn't be removed."
            ) from error


def close_store(store: duckdb.DuckDBPyConnection) -> None:
    """Closes a dataset connection, so its file can be deleted.

    Never raises: this is called on the way to deleting the file, and a connection that is
    already closed is exactly the state the caller wanted.
    """
    try:
        store.close()
    except duckdb.Error as error:
        logger.warning("A report dataset connection didn't close cleanly: %s", error)


def delete_dataset(task_id: int, root: Path | str = DEFAULT_DATASET_ROOT) -> bool:
    """Deletes one report's data file. Returns whether there was one to delete.

    Raises:
        ReportDataError: if the file exists and can't be removed — most often because a
            connection to it is still open.
    """
    path = dataset_path(task_id, root)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError as error:
        logger.exception("Could not delete the dataset file for report %s at %s.", task_id, path)
        raise ReportDataError("This report's saved data couldn't be deleted.") from error

    logger.info("Deleted the dataset file for report %s.", task_id)
    return True
