"""The in-memory DuckDB instance: connections, table registration, and query execution.

Two layers of table exist per uploaded file, and the distinction is requirement 5.1's:

- `base_<name>` — immutable, exactly as loaded. Never altered, never queried by the
  agent. It is what a rebuild restores from, which is what makes "a clean path back to
  the original" real rather than aspirational.
- `<name>` — the working table. Everything queries this. Relationship enforcement drops
  and re-creates it; calculated columns alter it.

Every statement runs through `run_query`/`execute`, which apply `engine.guards` first.
Having exactly one execution point is what makes the guardrail claim in requirements 5.5
checkable: there is no second path that skips it.
"""

import logging
import re

import duckdb
import pandas as pd

from engine.exceptions import DataEngineError, TableLoadError
from engine.guards import assert_safe_sql

logger = logging.getLogger(__name__)

BASE_TABLE_PREFIX = "base_"

_NON_IDENTIFIER = re.compile(r"[^0-9a-zA-Z_]+")
_LEADING_NON_ALPHA = re.compile(r"^[^a-zA-Z_]+")

# Reserved so a user-named table can never shadow the base layer or a DuckDB keyword
# that would need quoting everywhere downstream.
_RESERVED_TABLE_NAMES = {"table", "select", "from", "where", "order", "group", "index", "base"}

MAX_TABLE_NAME_LENGTH = 48


def open_connection() -> duckdb.DuckDBPyConnection:
    """Opens a fresh in-memory DuckDB instance.

    One per Streamlit session, held in session state rather than `st.cache_resource` —
    the latter is process-wide, and one user's tables appearing in another user's
    session would violate requirement 5.1's session scoping.

    Raises:
        DataEngineError: if DuckDB can't start.
    """
    try:
        return duckdb.connect(":memory:")
    except duckdb.Error as error:
        logger.exception("Could not open an in-memory DuckDB instance.")
        raise DataEngineError("The analysis engine couldn't start.") from error


def quote_identifier(name: str) -> str:
    """Quotes an identifier for interpolation into SQL.

    Table and column names cannot be passed as query parameters — SQL only parameterizes
    values — so they are quoted here instead, with embedded double quotes doubled.
    """
    return '"' + str(name).replace('"', '""') + '"'


def slugify_table_name(label: str, taken: set[str] | None = None) -> str:
    """Turns a file or sheet name into a short, SQL-safe, unique table name.

    `Jan Sales 2024.csv` becomes `jan_sales_2024`. The name is shown back to the user
    because it is what they will refer to in chat, so readability matters more than
    brevity — but it is truncated, because a 200-character table name makes every error
    message unreadable.
    """
    taken = {name.lower() for name in (taken or set())}

    slug = _NON_IDENTIFIER.sub("_", str(label or "").strip().lower()).strip("_")
    slug = _LEADING_NON_ALPHA.sub("", slug)
    slug = re.sub(r"_+", "_", slug)[:MAX_TABLE_NAME_LENGTH].strip("_")

    if not slug or slug in _RESERVED_TABLE_NAMES or slug.startswith(BASE_TABLE_PREFIX):
        slug = f"table_{slug}" if slug else "table"

    if slug not in taken:
        return slug

    suffix_number = 2
    while True:
        suffix = f"_{suffix_number}"
        candidate = f"{slug[: MAX_TABLE_NAME_LENGTH - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        suffix_number += 1


def base_name(table: str) -> str:
    """The immutable base table backing a working table."""
    return f"{BASE_TABLE_PREFIX}{table}"


def execute(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    params: list | tuple | None = None,
    *,
    allowed_tables: set[str] | None = None,
) -> None:
    """Runs one guarded statement for its side effect.

    Raises:
        UnsafeSqlError: if the statement fails the guardrails.
        DataEngineError: if DuckDB rejects it, carrying DuckDB's own message — which
            names the real problem far better than a paraphrase would.
    """
    assert_safe_sql(sql, allowed_tables)
    try:
        connection.execute(sql, params or [])
    except duckdb.Error as error:
        logger.exception("DuckDB rejected a statement: %s", sql)
        raise DataEngineError(str(error)) from error


def run_query(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    params: list | tuple | None = None,
    *,
    allowed_tables: set[str] | None = None,
) -> pd.DataFrame:
    """Runs one guarded query and returns its result as a DataFrame.

    Raises:
        UnsafeSqlError: if the statement fails the guardrails.
        DataEngineError: if DuckDB rejects it.
    """
    assert_safe_sql(sql, allowed_tables)
    try:
        return connection.execute(sql, params or []).df()
    except duckdb.Error as error:
        logger.exception("DuckDB rejected a query: %s", sql)
        raise DataEngineError(str(error)) from error


def register_table(connection: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame) -> None:
    """Loads a prepared frame as both the immutable base table and the working table.

    Re-registering the same name replaces both, so re-adopting tables from the Data
    Cleaner refreshes them rather than accumulating duplicates.

    Raises:
        TableLoadError: if the frame can't be loaded.
    """
    if frame.columns.duplicated().any():
        duplicates = sorted({str(column) for column in frame.columns[frame.columns.duplicated()]})
        raise TableLoadError(
            f"'{table}' repeats the column name(s) {', '.join(duplicates)}. "
            "Rename them in the Data Cleaner first, so every column stays uniquely addressable."
        )

    quoted_base = quote_identifier(base_name(table))
    quoted_working = quote_identifier(table)

    try:
        # A local name, so a column called `frame` in the caller can't shadow the view.
        connection.register("_engine_incoming", frame)
        try:
            connection.execute(f"DROP TABLE IF EXISTS {quoted_working}")
            connection.execute(f"DROP TABLE IF EXISTS {quoted_base}")
            connection.execute(f"CREATE TABLE {quoted_base} AS SELECT * FROM _engine_incoming")
            connection.execute(f"CREATE TABLE {quoted_working} AS SELECT * FROM {quoted_base}")
        finally:
            connection.unregister("_engine_incoming")
    except duckdb.Error as error:
        logger.exception("Could not register table '%s' into DuckDB.", table)
        raise TableLoadError(f"'{table}' couldn't be loaded into the analysis engine ({error}).") from error


def drop_table(connection: duckdb.DuckDBPyConnection, table: str) -> None:
    """Removes a working table and its base table.

    Raises:
        DataEngineError: if DuckDB refuses — most often because another table still
            references this one through a foreign key.
    """
    try:
        connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
        connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(base_name(table))}")
    except duckdb.Error as error:
        logger.exception("Could not drop table '%s'.", table)
        raise DataEngineError(str(error)) from error


def list_tables(connection: duckdb.DuckDBPyConnection) -> list[str]:
    """Returns the working table names in the session, base tables excluded."""
    try:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
    except duckdb.Error as error:
        logger.exception("Could not list the session's tables.")
        raise DataEngineError("The engine's table list couldn't be read.") from error

    return [name for (name,) in rows if not name.startswith(BASE_TABLE_PREFIX)]


def describe_table(connection: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    """Returns `[(column, sql_type), …]` for one table, in column order.

    Raises:
        DataEngineError: if the table doesn't exist.
    """
    try:
        rows = connection.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
    except duckdb.Error as error:
        logger.exception("Could not describe table '%s'.", table)
        raise DataEngineError(f"'{table}' isn't a table in this session.") from error

    return [(str(row[0]), str(row[1])) for row in rows]


def row_count(connection: duckdb.DuckDBPyConnection, table: str) -> int:
    """Returns how many rows a table currently holds."""
    try:
        result = connection.execute(f"SELECT count(*) FROM {quote_identifier(table)}").fetchone()
    except duckdb.Error as error:
        logger.exception("Could not count rows in '%s'.", table)
        raise DataEngineError(f"'{table}' couldn't be counted.") from error
    return int(result[0]) if result else 0


def preview(connection: duckdb.DuckDBPyConnection, table: str, limit: int = 100) -> pd.DataFrame:
    """Returns the first `limit` rows of a table, for display."""
    return run_query(connection, f"SELECT * FROM {quote_identifier(table)} LIMIT {int(limit)}")
