"""Calculated, deleted and updated columns against the working tables (requirement 5.4).

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

A calculated column can also reach across a **confirmed relationship** into one other
table — `salary.performance_bonus` computed from `employee_master.department`, say. This
only works one direction: `table` must be the relationship's *child* (the "many" side)
and `related_table` its *parent* (the "one" side), because the parent's key is enforced
unique — every child row is then guaranteed to match exactly one parent row, so the join
can never fan a row out or leave it unmatched. The reverse — a parent column computed by
combining several child rows — would need an aggregate (`sum`, `count`, …) and isn't
supported here; `_resolve_join` refuses it with a message naming the actual mistake
rather than trying to fake an aggregation nobody asked for precisely. Which relationship
to use is never taken on trust from a caller-supplied join condition: it is looked up by
table name in the relationships the user already confirmed in Setup, so the join itself
is exactly as trustworthy as `engine/relationships.py` already made it.

Every incoming fragment first goes through `quote_known_columns`, which double-quotes the
session's own column names where they appear bare. Real spreadsheet headers look like
`Sales Amount (Actual)`, and neither a model nor a user typing a formula reliably quotes
them — unrepaired, DuckDB stops at the first space with a syntax error naming the second
word, which tells the user nothing about what went wrong.

Updating values in place (*"mark status as Over due if due_date is before today"*) is the
third action requirement 5.4 asks for, and it is a single `UPDATE … SET … WHERE`. Both
halves are user-supplied SQL fragments, so both go through `assert_safe_expression`, and
both are probed before anything is written.

Every function returns the exact SQL it executed. That serves two purposes at once:
requirement 5.4 shows it in the chat for transparency, and requirement 7.5 records the
ordered statement list as part of the Task recipe — which is the same list
`relationships.enforce` replays after a rebuild.
"""

import logging
import re

import duckdb

from engine.duckdb_session import describe_table, quote_identifier
from engine.exceptions import CalculatedColumnError
from engine.guards import assert_safe_expression
from engine.relationships import Relationship

logger = logging.getLogger(__name__)


# A name DuckDB reads correctly without quotes: a letter or underscore, then letters,
# digits, underscores or dollars. Anything else — a space, a bracket, a hyphen — has to be
# double-quoted, or the binder stops at the first space and reports a syntax error there.
_BARE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Runs the repair below must not reach into: text literals, and names already quoted.
_LITERAL_OR_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")


def quote_known_columns(expression: str, columns: list[str]) -> str:
    """Double-quotes any of `columns` that appear unquoted in `expression`.

    A column called `Sales Amount (Actual)` is only legal SQL in quotes: written bare,
    DuckDB binds `Sales`, meets `Amount` and reports `syntax error at or near "Amount"`.
    Both sources of an expression forget those quotes — a model however firmly the prompt
    asks for them, and a user typing a formula into the dialog — so the fragment is
    repaired here rather than trusted.

    Only names that actually need quoting are considered, and only where they appear
    outside string literals and outside identifiers that are already quoted, so an
    ordinary expression comes back untouched and repairing twice changes nothing.
    """
    expression = str(expression or "")
    needy = sorted(
        {name for name in columns if name and _BARE_IDENTIFIER.match(name) is None},
        key=len,
        reverse=True,
    )
    if not expression.strip() or not needy:
        return expression

    # Longest name first, so `Sales Amount (Actual)` wins over a shorter `Amount (Actual)`.
    # The guards stop a match mid-word; `.` is deliberately allowed before a name, because
    # `employee_master.Sales Amount (Actual)` needs quoting on the column half.
    pattern = re.compile(
        r'(?<![A-Za-z0-9_$"])(?:' + "|".join(re.escape(name) for name in needy) + r")(?![A-Za-z0-9_$])",
        re.IGNORECASE,
    )
    real_names = {name.lower(): name for name in needy}

    def _quote(match: re.Match) -> str:
        return quote_identifier(real_names.get(match.group(0).lower(), match.group(0)))

    repaired: list[str] = []
    position = 0
    for skipped in _LITERAL_OR_QUOTED.finditer(expression):
        repaired.append(pattern.sub(_quote, expression[position : skipped.start()]))
        repaired.append(skipped.group(0))
        position = skipped.end()
    repaired.append(pattern.sub(_quote, expression[position:]))

    result = "".join(repaired)
    if result != expression:
        logger.info("Quoted column names that needed it in an expression fragment.")
    return result


def _assert_table_exists(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    try:
        return [column for column, _ in describe_table(connection, table)]
    except Exception as error:
        raise CalculatedColumnError(f"'{table}' isn't a table in this session.") from error


def _resolve_join(
    table: str, related_table: str, relationships: list[Relationship]
) -> tuple[str, str]:
    """Finds the confirmed relationship that lets `table` read across into `related_table`.

    Returns:
        `(quoted_parent_table, join_condition)` — a SQL fragment naming the other table
        and the exact equality that connects the two, both built from the relationship's
        own column names, never from anything a caller supplied.

    Raises:
        CalculatedColumnError: no confirmed link between the two tables at all, or the
            link runs the other way (`table` is the parent) — combining several rows from
            `related_table` into one needs an aggregate, which this doesn't attempt.
    """
    for relationship in relationships:
        if (
            relationship.child_table.lower() == table.lower()
            and relationship.parent_table.lower() == related_table.lower()
        ):
            condition = (
                f"{quote_identifier(table)}.{quote_identifier(relationship.child_column)} = "
                f"{quote_identifier(relationship.parent_table)}.{quote_identifier(relationship.parent_column)}"
            )
            return quote_identifier(relationship.parent_table), condition

        if (
            relationship.parent_table.lower() == table.lower()
            and relationship.child_table.lower() == related_table.lower()
        ):
            raise CalculatedColumnError(
                f"Each '{table}' row can match several rows in '{related_table}', so this "
                "would need to combine them — a sum, count or average, for example. That's "
                f"not supported yet; consider adding the column to '{related_table}' instead."
            )

    raise CalculatedColumnError(
        f"'{table}' and '{related_table}' aren't linked yet — confirm that link in Setup first."
    )


def _probe_type(connection: duckdb.DuckDBPyConnection, expression: str, from_sql: str) -> str:
    """Runs the actual type probe shared by the single-table and joined paths.

    `LIMIT 0` means DuckDB binds and type-checks the expression but computes no rows, so
    this is cheap even on a large table.

    Raises:
        CalculatedColumnError: if the expression doesn't compile, carrying DuckDB's own
            message (`Binder Error: Referenced column "bsic" not found …`), which names
            the mistake far better than a paraphrase could.
    """
    try:
        result = connection.execute(f"SELECT ({expression}) AS probe FROM {from_sql} LIMIT 0")
        return str(result.description[0][1])
    except duckdb.Error as error:
        logger.info("Calculated-column probe failed against '%s': %s", from_sql, error)
        raise CalculatedColumnError(str(error)) from error


def expression_type(connection: duckdb.DuckDBPyConnection, table: str, expression: str) -> str:
    """Returns the SQL type an expression produces against a table, without running it."""
    columns = _assert_table_exists(connection, table)
    expression = quote_known_columns(expression, columns)
    assert_safe_expression(expression)
    return _probe_type(connection, expression, quote_identifier(table))


def add_calculated_column(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    expression: str,
    *,
    related_table: str | None = None,
    relationships: list[Relationship] | None = None,
) -> list[str]:
    """Adds a calculated column to a working table, optionally reading across one
    confirmed relationship into `related_table` (see the module docstring for the
    child-to-parent direction this supports).

    Returns:
        The SQL statements executed, in order — shown to the user and recorded for
        replay.

    Raises:
        CalculatedColumnError: if the name is empty or already taken, the expression
            doesn't compile, or `related_table` isn't validly linked to `table`. Nothing
            is altered in any of those cases, because the probe runs first.
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

    related_table = str(related_table or "").strip() or None
    join_suffix = ""
    if related_table is None:
        # Repaired here as well as inside `expression_type`, because the same text goes
        # into the `UPDATE` below — probing a quoted expression and then writing an
        # unquoted one would fail after the `ALTER` had already run.
        expression = quote_known_columns(expression, existing)
        assert_safe_expression(expression)
        column_type = expression_type(connection, table, expression)
    else:
        related_columns = _assert_table_exists(connection, related_table)
        expression = quote_known_columns(expression, existing + related_columns)
        assert_safe_expression(expression)
        quoted_parent, join_condition = _resolve_join(table, related_table, relationships or [])
        probe_from = f"{quote_identifier(table)} JOIN {quoted_parent} ON {join_condition}"
        column_type = _probe_type(connection, expression, probe_from)
        join_suffix = f" FROM {quoted_parent} WHERE {join_condition}"

    quoted_table = quote_identifier(table)
    quoted_column = quote_identifier(column)
    statements = [
        f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} {column_type}",
        f"UPDATE {quoted_table} SET {quoted_column} = ({expression.strip()}){join_suffix}",
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


def _resolve_column(connection: duckdb.DuckDBPyConnection, table: str, column: str) -> str:
    """Returns a column's real, correctly-cased name, or raises if the table hasn't got it.

    DuckDB identifiers are case-insensitive but the stored name is what has to go into the
    SQL, so a user typing `Status` for a `status` column should work rather than fail.
    """
    column = str(column or "").strip()
    if not column:
        raise CalculatedColumnError("Choose which column to change.")

    for existing in _assert_table_exists(connection, table):
        if existing.lower() == column.lower():
            return existing
    raise CalculatedColumnError(f"'{table}' has no column called '{column}'.")


def affected_row_count(
    connection: duckdb.DuckDBPyConnection, table: str, condition: str | None = None
) -> int:
    """Counts the rows an update would touch, so the user sees the blast radius first.

    A blank condition means every row, which is exactly what the `UPDATE` would do.

    Raises:
        CalculatedColumnError: if the condition doesn't compile, carrying DuckDB's own
            message.
    """
    quoted_table = quote_identifier(table)
    columns = _assert_table_exists(connection, table)

    where_clause = ""
    if condition and condition.strip():
        condition = quote_known_columns(condition, columns)
        assert_safe_expression(condition)
        where_clause = f" WHERE ({condition.strip()})"

    try:
        result = connection.execute(f"SELECT count(*) FROM {quoted_table}{where_clause}").fetchone()
    except duckdb.Error as error:
        logger.info("Row-count probe failed on '%s': %s", table, error)
        raise CalculatedColumnError(str(error)) from error

    return int(result[0]) if result else 0


def update_column_values(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    value_expression: str,
    condition: str | None = None,
) -> list[str]:
    """Sets a column's values, optionally only on rows matching a condition.

    This is requirement 5.4's third action — *"mark status as Over due if due date is less
    than today"* becomes `UPDATE invoices SET status = ('Over due') WHERE (due_date <
    current_date)`.

    Both the value expression and the condition are probed before anything is written, so
    a typo in either leaves the table exactly as it was.

    Returns:
        The single SQL statement executed — shown to the user and recorded for replay.

    Raises:
        CalculatedColumnError: if the column doesn't exist, or either fragment fails to
            compile or to assign.
    """
    resolved_column = _resolve_column(connection, table, column)
    columns = _assert_table_exists(connection, table)

    value_expression = str(value_expression or "").strip()
    if not value_expression:
        raise CalculatedColumnError("Give the column a new value to be set to.")
    value_expression = quote_known_columns(value_expression, columns)
    assert_safe_expression(value_expression)

    condition = str(condition or "").strip()
    if condition:
        condition = quote_known_columns(condition, columns)
        assert_safe_expression(condition)

    # Probing both fragments separately means the error message names the one that is
    # actually wrong, rather than pointing at a combined statement the user never wrote.
    expression_type(connection, table, value_expression)
    if condition:
        affected_row_count(connection, table, condition)

    quoted_table = quote_identifier(table)
    quoted_column = quote_identifier(resolved_column)
    statement = f"UPDATE {quoted_table} SET {quoted_column} = ({value_expression})"
    if condition:
        statement += f" WHERE ({condition})"

    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute(statement)
        connection.execute("COMMIT")
    except duckdb.Error as error:
        connection.execute("ROLLBACK")
        logger.exception("Could not update '%s' in '%s'.", resolved_column, table)
        raise CalculatedColumnError(
            f"'{resolved_column}' couldn't be updated ({error})."
        ) from error

    logger.info("Updated %s.%s%s.", table, resolved_column, " with a condition" if condition else "")
    return [statement]


# The `UPDATE` half of a calculated column — `UPDATE "t" SET "c" = (expr)`, no WHERE, and
# `add_calculated_column` never emits one. Matched only to hide it; see `describe_statements`.
_ADDED_COLUMN = re.compile(r'^ALTER\s+TABLE\s+(".+?"|\S+)\s+ADD\s+COLUMN\s+(".+?"|\S+)\s', re.IGNORECASE)
_FILLED_COLUMN = re.compile(
    r'^UPDATE\s+(".+?"|\S+)\s+SET\s+(".+?"|\S+)\s*=(?![\s\S]*\sWHERE\s)', re.IGNORECASE
)


def describe_statements(statements: list[str]) -> list[str]:
    """Collapses the recorded statement list into one readable line per column change.

    A calculated column is two statements but one user action, so the log reads the way
    the user thinks about it rather than the way DuckDB executes it.

    Only the `UPDATE` that *fills in* a column just added by the preceding `ALTER TABLE` is
    hidden. A standalone `UPDATE` is a user action in its own right (requirement 5.4's
    update-values case) and has to stay visible — dropping every `UPDATE` would make that
    entire action invisible in the log.
    """
    lines: list[str] = []
    previous_added: tuple[str, str] | None = None

    for statement in statements:
        collapsed = " ".join(statement.split())

        filled = _FILLED_COLUMN.match(collapsed)
        if filled is not None and previous_added == (filled.group(1), filled.group(2)):
            previous_added = None
            continue

        added = _ADDED_COLUMN.match(collapsed)
        previous_added = (added.group(1), added.group(2)) if added is not None else None
        lines.append(collapsed)

    return lines
