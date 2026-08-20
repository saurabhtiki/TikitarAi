"""A report item in plain language becomes SQL that runs and answers it (requirement 7.3).

The same shape as `checks/sql_builder.py`, and for the same reasons: one narrow
`run_structured` call rather than the `analyst.agent` Agno agent, because a report item is
one deterministic job with a fixed place in a report, saved and re-run for months. Giving it
tools and conversation history would only give it room to answer a slightly different
question than the one the user saved.

**The one deliberate difference is the output contract — there isn't one.** A criteria must
return `criteria_result` and `criteria_met`, because its whole purpose is a per-row Yes/No
that gets counted and actioned. A report item has no such shape: "headcount by department"
is two columns and twelve rows, "total payroll cost" is one cell, and demanding a verdict
column of either would be inventing a question the user didn't ask. So what is validated
here is only that the statement is safe, that it runs, and that it returned *something* to
put in a report.

Failure is handled in two stages, and the split is the one `checks/` already settled on:

1. **One automatic repair.** Whatever went wrong goes straight back to the model once.
   Models fix their own mistakes reliably, and making the user drive that turns a
   two-second correction into a manual refine cycle.
2. **Then it becomes the user's.** After the retry the message lands on `item.last_error`,
   shown beside the SQL, and is fed into the next generation along with the previous
   statement — refining, never starting over.

Nothing here imports Streamlit, so the whole path is testable with a stubbed
`run_structured` and a real in-memory DuckDB.
"""

import logging
from pathlib import Path

import duckdb
import pandas as pd
from pydantic import BaseModel, Field

from engine.duckdb_session import run_query
from engine.exceptions import DataEngineError
from llm.client import LLMConnectionError, run_structured
from report_items.exceptions import ReportItemSqlError
from report_items.model import ReportItem

logger = logging.getLogger(__name__)

INSTRUCTIONS = [
    "You write a single DuckDB SQL query that answers one point of a report, against the "
    "tables described below.",
    "Return exactly one SELECT (or WITH … SELECT) statement. No semicolon, no explanation "
    "inside the SQL, and never a CREATE, INSERT, UPDATE, DELETE, DROP or ALTER.",
    "Answer exactly what was asked and nothing more. If the request asks for a total, "
    "return the total — do not also return the rows behind it.",
    "Give every calculated column a short, readable alias, because the column names are "
    "printed as the table's headings in a report a person will read.",
    "Round money and percentages to two decimal places unless the request says otherwise.",
    "Order the result the way a reader would expect it — largest first for a ranking, by "
    "date for a series, alphabetically otherwise — since this is the order it is printed in.",
    "Use only the tables and columns in the schema. The user's suggested tables and columns "
    "are a hint from someone who may not know the exact names — if they don't match the "
    "schema, use the correct ones and ignore the hint.",
    "Join tables using the listed relationships, preferring LEFT JOIN so rows without a "
    "match are still counted rather than silently dropped.",
]


class GeneratedSql(BaseModel):
    """The structured shape the generation call returns."""

    sql: str = Field(description="One DuckDB SELECT statement, with no trailing semicolon.")
    explanation: str = Field(
        default="", description="One sentence on how the request was translated. Not shown in the SQL."
    )


def build_prompt(
    persona: str,
    item: ReportItem,
    schema_context: str,
    *,
    previous_sql: str | None = None,
    previous_error: str | None = None,
) -> str:
    """Assembles the generation prompt.

    `previous_sql` and `previous_error` are what make this a refine loop rather than a
    retry: the model is told to change the least it can, so an item the user has already
    tuned doesn't come back rewritten from scratch with different column names — which in a
    report means a heading that silently changed between one month and the next.
    """
    parts = []
    if persona and persona.strip():
        parts.append(f"Your role and the context you are working in:\n{persona.strip()}")

    if (item.heading or "").strip():
        parts.append(f"This point of the report is headed:\n{item.heading.strip()}")

    parts.append(f"What this point should show, in the user's own words:\n{item.request.strip()}")

    hints = []
    if item.hint_tables:
        hints.append(f"tables: {', '.join(item.hint_tables)}")
    if item.hint_columns:
        hints.append(f"columns: {', '.join(item.hint_columns)}")
    if hints:
        parts.append(
            "The user suggested these are involved (a hint only — correct it against the "
            f"schema if it is wrong):\n{'; '.join(hints)}"
        )

    parts.append(f"Schema:\n{schema_context.strip()}")

    if previous_sql:
        parts.append(f"Your previous attempt:\n{previous_sql.strip()}")
        parts.append(
            "Change as little of it as possible — keep the columns, joins and aliases that "
            "already work and adjust only what the problem below requires."
            if previous_error
            else "Change as little of it as possible — keep the columns, joins and aliases "
            "that already work and adjust only what the updated request requires."
        )
    if previous_error:
        parts.append(f"What was wrong with it:\n{previous_error.strip()}")

    return "\n\n".join(parts)


def generate_sql(
    profile: dict,
    persona: str,
    item: ReportItem,
    schema_context: str,
    *,
    previous_sql: str | None = None,
    previous_error: str | None = None,
    key_path: Path | str | None = None,
) -> GeneratedSql:
    """Asks the model for one statement.

    Raises:
        ReportItemSqlError: if the request is empty, or the provider fails.
    """
    if not (item.request or "").strip():
        raise ReportItemSqlError("Say what this point of the report should show first.")

    try:
        result = run_structured(
            profile,
            build_prompt(
                persona, item, schema_context, previous_sql=previous_sql, previous_error=previous_error
            ),
            GeneratedSql,
            instructions=INSTRUCTIONS,
            # A model that answers "write me one SELECT" with the SELECT itself has done the
            # job; only the envelope is missing. The statement still faces the guard, DuckDB
            # and `validate_result` below, so nothing is accepted on the strength of this.
            text_field="sql",
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("SQL generation failed for report item '%s': %s", item.display_heading(), error)
        raise ReportItemSqlError(f"Couldn't write the SQL for this item: {error}") from error

    sql = _clean_sql(result.sql)
    if not sql:
        raise ReportItemSqlError(
            "The model returned no SQL. Try rewording the request, or a more capable model."
        )
    result.sql = sql
    return result


def _clean_sql(sql: str) -> str:
    """Strips the markdown fencing and trailing semicolon models add unbidden.

    A fenced statement is not a syntax the guard or DuckDB accept, and a trailing semicolon
    makes `assert_safe_sql` see two statements — both would be reported to the user as if
    the request were at fault.
    """
    text = str(sql or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return text.rstrip(";").strip()


def assert_read_only(sql: str) -> None:
    """Refuses anything that isn't a single SELECT.

    `engine.guards.assert_safe_sql` blocks the statements that could escape the session —
    `ATTACH`, `COPY`, `DROP TABLE`, a filesystem function — but `DELETE FROM salary` is none
    of those and passes it. `checks/` never noticed, because its result contract rejects a
    row count where `criteria_met` should be; a report item has no such contract by design,
    so the check that a report item only *reads* has to be made explicitly, here.

    Raises:
        ReportItemSqlError: naming the keyword, because the fix is to make it a column step
            instead rather than to reword the request.
    """
    text = str(sql or "").lstrip()
    # Skip a leading comment block, which is otherwise the whole trick to getting past this.
    while text.startswith("--") or text.startswith("/*"):
        if text.startswith("--"):
            _, _, text = text.partition("\n")
        else:
            _, _, text = text.partition("*/")
        text = text.lstrip()

    keyword = (text.split() or [""])[0].upper().strip("(")
    if keyword not in ("SELECT", "WITH"):
        raise ReportItemSqlError(
            f"A report item can only read data, but this statement starts with '{keyword}'. "
            "To change a column, add a column step instead."
        )


def validate_result(frame: pd.DataFrame) -> None:
    """Checks a result is something that can go into a report.

    Deliberately almost nothing: see this module's docstring on why a report item has no
    column contract. What *is* refused is a result with no columns at all, which is not a
    table with no rows — it is a statement that selected nothing, and printing it would put
    an empty box in the report with no explanation.

    An **empty** result is allowed and is not an error: "no invoice was overdue this month"
    is a legitimate answer, and refusing it would send the model chasing a problem that
    isn't there.

    Raises:
        ReportItemSqlError: with a message written to be fed straight back to the model,
            because that is exactly what `generate_and_run` does with it.
    """
    if len(frame.columns) == 0:
        raise ReportItemSqlError(
            "The query ran but returned no columns, so there is nothing to put in the report. "
            "Select the values this point should show."
        )


def run_item(connection: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    """Runs one generated statement and returns its rows, or explains why it can't.

    `allowed_tables=None` is the guard's tightest setting — no table may be mutated at all —
    which is right for a statement that exists only to read. A report item that needs to
    *change* data is a column step instead, and goes through `engine/columns.py`'s guarded
    functions rather than through here.

    Raises:
        ReportItemSqlError: if the statement is refused, fails in DuckDB, or comes back
            unusable.
    """
    if not (sql or "").strip():
        raise ReportItemSqlError("There is no SQL to run yet — generate it first.")

    assert_read_only(sql)

    try:
        frame = run_query(connection, sql, allowed_tables=None)
    except DataEngineError as error:
        logger.info("A report item query was refused or failed: %s", error)
        raise ReportItemSqlError(f"That query could not run: {error}") from error
    except duckdb.Error as error:
        logger.info("A report item query failed in DuckDB: %s", error)
        raise ReportItemSqlError(f"That query could not run: {error}") from error

    validate_result(frame)
    return frame


def generate_and_run(
    profile: dict,
    persona: str,
    item: ReportItem,
    schema_context: str,
    connection: duckdb.DuckDBPyConnection,
    *,
    key_path: Path | str | None = None,
) -> tuple[str, pd.DataFrame]:
    """Generates, runs and validates a report item's SQL.

    Regenerates from `item.sql` when there is one, so pressing the button again after editing
    the request refines the statement rather than replacing it. One automatic repair attempt
    is made before the failure is handed back.

    Returns:
        `(sql, frame)` — the statement that worked and the rows it returned. The caller
        stores both on the item; nothing is mutated here.

    Raises:
        ReportItemSqlError: if both attempts failed. The message is written for the user
            *and* is what the next refine sends back to the model.
    """
    generated = generate_sql(
        profile,
        persona,
        item,
        schema_context,
        previous_sql=item.sql,
        previous_error=item.last_error,
        key_path=key_path,
    )

    try:
        return generated.sql, run_item(connection, generated.sql)
    except ReportItemSqlError as error:
        # Copied out of the `except` block: Python unbinds the exception name on exit, so
        # reading it below would be a NameError rather than the retry it looks like.
        first_failure = str(error)
        logger.info(
            "First attempt at report item '%s' failed, repairing: %s",
            item.display_heading(),
            first_failure,
        )

    repaired = generate_sql(
        profile,
        persona,
        item,
        schema_context,
        previous_sql=generated.sql,
        previous_error=first_failure,
        key_path=key_path,
    )
    # Not caught: a second failure is the user's to see. It arrives carrying the reason,
    # which is what the refine loop needs to do better than this did.
    return repaired.sql, run_item(connection, repaired.sql)
