"""A rule in plain language becomes SQL that runs and answers it (requirement 6.5).

One narrow `run_structured` call, not the `analyst.agent` Agno agent. The agent exists to
explore an open question across several queries and correct itself along the way; a
criteria is one deterministic job with a fixed output shape, saved and re-run for months.
Giving it tools and conversation history would only give it room to answer a slightly
different question than the one the user saved — this is requirement 6.2's "each call has
one job" reasoning applied a second time.

The output contract is the part that matters. `docs/addtion.md` asks for `criteria_met`
and `criteria_result`; this module additionally requires the identifying columns, because
**a failure nobody can attribute cannot be actioned** — an email about a bonus breach has
to name the employee. So the statement is not accepted until its result carries all of it.

Failure is handled in two stages, and the split is deliberate:

1. **One automatic repair.** Whatever went wrong — the guard refusing it, DuckDB rejecting
   it, a missing `criteria_met` alias — goes straight back to the model once. Models fix
   their own missing alias reliably, and making the user drive that turns a two-second
   correction into a manual refine cycle.
2. **Then it becomes the user's.** After the retry the message lands on `check.last_error`,
   shown beside the SQL, and is fed into the next generation along with the previous
   statement. That is `addtion.md` step 4: refining, never starting over.

Nothing here imports Streamlit, so the whole path is testable with a stubbed
`run_structured` and a real in-memory DuckDB.
"""

import logging
from pathlib import Path

import duckdb
import pandas as pd
from pydantic import BaseModel, Field

from checks.exceptions import CheckSqlError
from checks.model import (
    COLUMN_MET,
    COLUMN_RESULT,
    MET_NO,
    MET_YES,
    REQUIRED_COLUMNS,
    Check,
    met_values,
)
from engine.duckdb_session import run_query
from engine.exceptions import DataEngineError
from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

# How many rows of a result the model is shown when drafting. Not used for generation —
# generation sees only the schema — but `remarks` and `actions` share the cap, so it lives
# with the other result-shape constants.
MAX_MET_DISTINCT = 10

INSTRUCTIONS = [
    "You write a single DuckDB SQL query that tests one business rule against the tables described below.",
    "Return exactly one SELECT (or WITH … SELECT) statement. No semicolon, no explanation "
    "inside the SQL, and never a CREATE, INSERT, UPDATE, DELETE, DROP or ALTER.",
    "The result must have one row per record the rule applies to, with these columns:",
    "  1. First, the columns that identify the row — the person, department, invoice or "
    "account the rule is about, and an email or owner column if the schema has one. "
    "Someone reading a failing row must be able to tell who or what it refers to. Return "
    "these names in `identity_columns`, in the order you selected them.",
    "  1b. Then every remaining column of the table the rule is mainly about (select it as "
    "`t.*` where you can), plus any joined columns the rule uses. The user chooses which of "
    "these to show, so select them even when the rule does not need them. Alias anything "
    "whose name would otherwise clash across joins so every column in the result is unique, "
    "and do not group or aggregate purely to shorten the result.",
    f"  2. `{COLUMN_RESULT}` — the value the rule turns on (the calculated percentage, the "
    "difference, the count). Always aliased with exactly that name.",
    f"  3. `{COLUMN_MET}` — the literal string '{MET_YES}' when the row satisfies the rule "
    f"and '{MET_NO}' when it breaches it. Always aliased with exactly that name, and never "
    "a boolean, a number or any other wording.",
    "Do not filter out the passing rows: return both, so the user can review passes as well "
    "as failures.",
    "Use only the tables and columns in the schema. The user's suggested tables and columns "
    "are a hint from someone who may not know the exact names — if they don't match the "
    "schema, use the correct ones and ignore the hint.",
    "Join tables using the listed relationships, preferring LEFT JOIN so rows without a "
    "match are still tested rather than silently dropped.",
]


class GeneratedSql(BaseModel):
    """The structured shape the generation call returns."""

    sql: str = Field(description="One DuckDB SELECT statement, with no trailing semicolon.")
    identity_columns: list[str] = Field(
        default_factory=list,
        description="The columns in the result that identify each row, in the order selected.",
    )
    explanation: str = Field(
        default="", description="One sentence on how the rule was translated. Not shown in the SQL."
    )


def build_prompt(
    persona: str,
    check: Check,
    schema_context: str,
    *,
    previous_sql: str | None = None,
    previous_error: str | None = None,
) -> str:
    """Assembles the generation prompt.

    `previous_sql` and `previous_error` are what make this a refine loop rather than a
    retry: the model is told to change the least it can, so a rule the user has already
    tuned doesn't come back rewritten from scratch with a different shape.
    """
    parts = []
    if persona and persona.strip():
        parts.append(f"Your role and the context you are working in:\n{persona.strip()}")

    parts.append(f"The rule to test, in the user's own words:\n{check.criteria_text.strip()}")

    hints = []
    if check.hint_tables:
        hints.append(f"tables: {', '.join(check.hint_tables)}")
    if check.hint_columns:
        hints.append(f"columns: {', '.join(check.hint_columns)}")
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
            "that already work and adjust only what the updated rule requires."
        )
    if previous_error:
        parts.append(f"What was wrong with it:\n{previous_error.strip()}")

    return "\n\n".join(parts)


def generate_sql(
    profile: dict,
    persona: str,
    check: Check,
    schema_context: str,
    *,
    previous_sql: str | None = None,
    previous_error: str | None = None,
    key_path: Path | str | None = None,
) -> GeneratedSql:
    """Asks the model for one statement.

    Raises:
        CheckSqlError: if the criteria is empty, or the provider fails.
    """
    if not (check.criteria_text or "").strip():
        raise CheckSqlError("Write the rule in plain language first.")

    try:
        result = run_structured(
            profile,
            build_prompt(
                persona, check, schema_context, previous_sql=previous_sql, previous_error=previous_error
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
        logger.warning("SQL generation failed for criteria '%s': %s", check.display_name(), error)
        raise CheckSqlError(f"Couldn't write the SQL for this criteria: {error}") from error

    sql = _clean_sql(result.sql)
    if not sql:
        raise CheckSqlError("The model returned no SQL. Try rewording the rule, or a more capable model.")
    result.sql = sql
    return result


def _clean_sql(sql: str) -> str:
    """Strips the markdown fencing and trailing semicolon models add unbidden.

    A fenced statement is not a syntax the guard or DuckDB accept, and a trailing semicolon
    makes `assert_safe_sql` see two statements — both would be reported to the user as if
    the rule were at fault.
    """
    text = str(sql or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return text.rstrip(";").strip()


def validate_result(frame: pd.DataFrame) -> None:
    """Checks a result against the contract this module's docstring describes.

    Raises:
        CheckSqlError: with a message written to be fed straight back to the model, because
            that is exactly what `generate_and_run` does with it.
    """
    missing = [name for name in REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        raise CheckSqlError(
            f"The query ran but its result is missing {' and '.join(missing)}. "
            f"Alias the calculated value as {COLUMN_RESULT} and the Yes/No verdict as {COLUMN_MET}."
        )

    if frame.empty:
        # Not an error. A rule that matches no rows is a legitimate answer — "no employee
        # is in scope this month" — and refusing it would send the model chasing a problem
        # that isn't there.
        return

    allowed = {MET_YES.casefold(), MET_NO.casefold()}
    seen = set(met_values(frame).dropna().unique())
    unexpected = sorted(value for value in seen if value not in allowed)
    if unexpected:
        shown = ", ".join(repr(value) for value in unexpected[:MAX_MET_DISTINCT])
        raise CheckSqlError(
            f"{COLUMN_MET} must contain only '{MET_YES}' or '{MET_NO}', but the result also "
            f"contains {shown}. Return the literal strings, not a boolean or a number."
        )

    identity_columns = [name for name in frame.columns if name not in (COLUMN_RESULT, COLUMN_MET)]
    if not identity_columns:
        raise CheckSqlError(
            f"The result has only {COLUMN_RESULT} and {COLUMN_MET}, so a failing row can't be "
            "traced back to anything. Also select the columns that identify each row."
        )


def run_check(connection: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    """Runs one generated statement and returns its rows, or explains why it can't.

    `allowed_tables=None` is the guard's tightest setting — no table may be mutated at all —
    which is right for a statement that exists only to read.

    Raises:
        CheckSqlError: if the statement is refused, fails in DuckDB, or breaks the result
            contract.
    """
    if not (sql or "").strip():
        raise CheckSqlError("There is no SQL to run yet — generate it first.")

    try:
        frame = run_query(connection, sql, allowed_tables=None)
    except DataEngineError as error:
        logger.info("A criteria query was refused or failed: %s", error)
        raise CheckSqlError(f"That query could not run: {error}") from error
    except duckdb.Error as error:
        logger.info("A criteria query failed in DuckDB: %s", error)
        raise CheckSqlError(f"That query could not run: {error}") from error

    validate_result(frame)
    return frame


def generate_and_run(
    profile: dict,
    persona: str,
    check: Check,
    schema_context: str,
    connection: duckdb.DuckDBPyConnection,
    *,
    key_path: Path | str | None = None,
) -> tuple[str, pd.DataFrame, list[str]]:
    """Generates, runs and validates a criteria's SQL — addtion.md steps 3 and 4 in one call.

    Regenerates from `check.sql` when there is one, so pressing the button again after
    editing the rule refines the statement rather than replacing it. One automatic repair
    attempt is made before the failure is handed back.

    Returns:
        `(sql, frame, identity_columns)` — the statement that worked, the rows it returned,
        and the columns the model says identify each row. The caller stores all three on the
        check; nothing is mutated here.

        The identity columns are what seeds the criteria's display selection: the result now
        carries the whole source row, and showing all of it by default would change every
        existing criteria's screen. Seeding from these keeps it exactly as it was until the
        user widens it themselves.

    Raises:
        CheckSqlError: if both attempts failed. The message is written for the user *and*
            is what the next refine sends back to the model.
    """
    generated = generate_sql(
        profile,
        persona,
        check,
        schema_context,
        previous_sql=check.sql,
        previous_error=check.last_error,
        key_path=key_path,
    )

    try:
        return generated.sql, run_check(connection, generated.sql), list(generated.identity_columns)
    except CheckSqlError as error:
        # Copied out of the `except` block: Python unbinds the exception name on exit, so
        # reading it below would be a NameError rather than the retry it looks like.
        first_failure = str(error)
        logger.info("First attempt at '%s' failed, repairing: %s", check.display_name(), first_failure)

    repaired = generate_sql(
        profile,
        persona,
        check,
        schema_context,
        previous_sql=generated.sql,
        previous_error=first_failure,
        key_path=key_path,
    )
    # Not caught: a second failure is the user's to see. It arrives carrying the reason,
    # which is what the refine loop needs to do better than this did.
    return repaired.sql, run_check(connection, repaired.sql), list(repaired.identity_columns)
