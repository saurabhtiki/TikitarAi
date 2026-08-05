"""Calculated and deleted columns against the working tables (requirement 5.4).

DuckDB does not support `ALTER TABLE … ADD COLUMN x AS (expr)`, so a calculated column
is two statements, not one:

    ALTER TABLE salaries ADD COLUMN tax DOUBLE;
    UPDATE salaries SET tax = basic * 0.10;

The type comes from probing the expression (`SELECT (basic * 0.10) … LIMIT 0`) rather
than being guessed, so `basic * 0.10` lands as DOUBLE and `upper(name)` as VARCHAR.
Probing first also means a typo is caught by DuckDB's own binder — with its own
message, naming the actual column — before anything is altered.

Because each statement runs sequentially against the same working table, a later
expression can reference a column an earlier one added: after `tax` exists,
`basic - tax` is an ordinary column reference and chaining needs no special handling.

Every function returns the exact SQL it executed. That serves two purposes at once:
requirement 5.4 shows it in the chat for transparency, and requirement 7.5 records the
ordered statement list as part of the Task recipe — which is the same list
`relationships.enforce` replays after a rebuild.
"""

import logging

import duckdb

from engine.duckdb_session import describe_table, quote_identifier
from engine.exceptions import CalculatedColumnError
from engine.guards import assert_safe_expression

logger = logging.getLogger(__name__)


def _assert_table_exists(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    try:
        return [column for column, _ in describe_table(connection, table)]
    except Exception as error:
        raise CalculatedColumnError(f"'{table}' isn't a table in this session.") from error


def expression_type(connection: duckdb.DuckDBPyConnection, table: str, expression: str) -> str:
    """Returns the SQL type an expression produces against a table, without running it.

    `LIMIT 0` means DuckDB binds and type-checks the expression but computes no rows, so
    this is cheap even on a large table.

    Raises:
        CalculatedColumnError: if the expression doesn't compile, carrying DuckDB's own
            message (`Binder Error: Referenced column "bsic" not found …`), which names
            the mistake far better than a paraphrase could.
    """
    assert_safe_expression(expression)
    _assert_table_exists(connection, table)

    try:
        result = connection.execute(
            f"SELECT ({expression}) AS probe FROM {quote_identifier(table)} LIMIT 0"
        )
        return str(result.description[0][1])
    except duckdb.Error as error:
        logger.info("Calculated-column probe failed on '%s': %s", table, error)
        raise CalculatedColumnError(str(error)) from error


def add_calculated_column(
    connection: duckdb.DuckDBPyConnection, table: str, column: str, expression: str
) -> list[str]:
    """Adds a calculated column to a working table.

    Returns:
        The SQL statements executed, in order — shown to the user and recorded for
        replay.

    Raises:
        CalculatedColumnError: if the name is empty or already taken, or the expression
            doesn't compile. Nothing is altered in any of those cases, because the probe
            runs first.
    """
    column = str(column or "").strip()
    if not column:
        raise CalculatedColumnError("Give the new column a name.")

    existing = _assert_table_exists(connection, table)
    if column.lower() in {name.lower() for name in existing}:
        raise CalculatedColumnError(
            f"'{table}' already has a column called '{column}'. Pick a different name, "
            "or remove the existing one first."
        )

    column_type = expression_type(connection, table, expression)

    quoted_table = quote_identifier(table)
    quoted_column = quote_identifier(column)
    statements = [
        f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {column_type}",
        f"UPDATE {quoted_table} SET {quoted_column} = ({expression.strip()})",
    ]

    try:
        connection.execute("BEGIN TRANSACTION")
        for statement in statements:
            connection.execute(statement)
        connection.execute("COMMIT")
    except duckdb.Error as error:
        connection.execute("ROLLBACK")
        logger.exception("Could not add calculated column '%s' to '%s'.", column, table)
        raise CalculatedColumnError(f"'{column}' couldn't be added ({error}).") from error

    logger.info("Added calculated column %s.%s as %s.", table, column, column_type)
    return statements


def drop_column(connection: duckdb.DuckDBPyConnection, table: str, column: str) -> list[str]:
    """Removes a column from a working table.

    Raises:
        CalculatedColumnError: if the column isn't there, or DuckDB refuses — which it
            does when the column carries a foreign key, and that refusal is worth
            surfacing rather than working around: dropping it would silently discard a
            confirmed relationship.
    """
    existing = _assert_table_exists(connection, table)
    if column.lower() not in {name.lower() for name in existing}:
        raise CalculatedColumnError(f"'{table}' has no column called '{column}'.")

    statement = f"ALTER TABLE {quote_identifier(table)} DROP COLUMN {quote_identifier(column)}"

    try:
        connection.execute(statement)
    except duckdb.Error as error:
        logger.exception("Could not drop column '%s' from '%s'.", column, table)
        raise CalculatedColumnError(f"'{column}' couldn't be removed ({error}).") from error

    logger.info("Dropped column %s.%s.", table, column)
    return [statement]


def describe_statements(statements: list[str]) -> list[str]:
    """Collapses the recorded statement list into one readable line per column change.

    A calculated column is two statements but one user action, so the log reads the way
    the user thinks about it rather than the way DuckDB executes it.
    """
    lines: list[str] = []
    for statement in statements:
        collapsed = " ".join(statement.split())
        if collapsed.upper().startswith("UPDATE"):
            continue
        lines.append(collapsed)
    return lines
