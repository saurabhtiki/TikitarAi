"""Column changes asked for in plain English (requirement 5.4).

Requirement 5.4 is written conversationally — *"Add tax = 10% of basic"*, *"Delete tax"*,
*"Mark status as Over due if Due date is less than Today"* — and this is that path. It is
the second door onto the same room the Actions dialogs open: both end at the same three
functions in `engine/columns.py`.

The division of labour is the point. The model **fills in blanks** — which table, which
column, what formula, what condition — and returns them as a structured object. It never
writes the statement. `engine/columns.py` builds the SQL from those parameters, probes it
before touching anything, and wraps the write in a transaction. So a model that
hallucinates cannot produce a statement the guards never saw, and the recorded statement
list stays identical whichever door the user came through — which is what makes
requirement 7.5's replay work for both.

Requirement 5.4 also asks that the executed SQL be shown in the chat, so every function
here returns it.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, Field

from analyst.exceptions import AnalystError
from analyst.routing import ACTION_ADD, ACTION_DELETE, ACTION_UPDATE
from engine import columns as engine_columns
from engine.exceptions import CalculatedColumnError, UnsafeSqlError
from engine.relationships import Relationship
from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You translate a request to change a table's columns into structured parameters. "
    "You never write SQL statements — only the fragments asked for. "
    "Pick the table and column names exactly as they appear in the schema, matching case. "
    "For `add`, `expression` is normally a DuckDB SQL expression over that table's own "
    "columns (e.g. `basic * 0.10`), and `column` is the new column's name. "
    "If the formula genuinely needs a column that lives on a different table, and the "
    "schema's relationships show that table already linked to this one, set "
    "`related_table` to that other table's exact name and qualify every column you take "
    "from it with the real table name, e.g. `employee_master.department` — never invent "
    "an alias like `em`. This only works when `table` is the *many* side of the link "
    "(e.g. adding to `salary`, reading from `employee_master`); if the request would need "
    "combining several rows from another table into one (a sum, count or average), that "
    "is not supported — set action to `none` and say so in `explanation`. Leave "
    "`related_table` empty whenever the formula only touches `table`'s own columns. "
    "For `update`, `expression` is the new value (quote text literals, e.g. `'Over due'`) "
    "and `condition` is the WHERE fragment (e.g. `due_date < current_date`); leave "
    "`condition` empty to change every row. `related_table` does not apply to `update`. "
    "For `delete`, only `table` and `column` matter. "
    "If the request does not clearly name a table and column, set action to `none` and "
    "explain what is missing."
)


class ColumnAction(BaseModel):
    """The parameters a column change needs — never the statement itself."""

    action: Literal["add", "delete", "update", "none"] = Field(
        description="Which change was asked for, or 'none' if the request was unclear."
    )
    table: str = Field(default="", description="The table to change, exactly as named in the schema.")
    column: str = Field(default="", description="The column to add, remove or update.")
    expression: str = Field(
        default="", description="For add: the formula. For update: the new value. Otherwise empty."
    )
    condition: str = Field(
        default="", description="For update: the WHERE fragment. Empty means every row."
    )
    related_table: str = Field(
        default="",
        description=(
            "For add only: another table the formula reads from, already linked to "
            "`table` via a confirmed relationship. Empty unless genuinely needed."
        ),
    )
    explanation: str = Field(default="", description="One short sentence on what this does.")


@dataclass
class ColumnChange:
    """What a column change did, for the chat transcript and the recorded recipe."""

    action: str
    summary: str
    statements: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_request(
    profile: dict,
    message: str,
    schema_context: str,
    hint: str | None = None,
    *,
    key_path: Path | str | None = None,
) -> ColumnAction:
    """Turns a plain-English column request into structured parameters.

    Args:
        hint: what `routing.looks_like_column_action` guessed. Passed to the model as a
            prior, not as a decision — "remove the rows where x is null" trips the delete
            keyword but isn't a column change, and the model should be free to say so.

    Raises:
        AnalystError: if the provider fails or returns something unparseable.
    """
    prompt = f"Schema:\n{schema_context}\n\nRequest:\n{message}"
    if hint:
        prompt += f"\n\nThis looks like a '{hint}' request, but decide for yourself."

    try:
        return run_structured(profile, prompt, ColumnAction, instructions=INSTRUCTIONS, key_path=key_path)
    except LLMConnectionError as error:
        logger.exception("Could not parse a column request: %s", message)
        raise AnalystError(f"I couldn't read that as a column change: {error}") from error


def apply_action(
    connection: duckdb.DuckDBPyConnection,
    action: ColumnAction,
    relationships: list[Relationship] | None = None,
) -> ColumnChange:
    """Runs a parsed column change through the engine's guarded functions.

    Args:
        relationships: the session's confirmed links, needed only when `action.add` names
            a `related_table` — see `engine.columns.add_calculated_column`.

    Returns:
        A `ColumnChange` carrying the exact SQL executed — requirement 5.4 shows it in the
        chat, and requirement 7.5 records it.

    Raises:
        AnalystError: if the request was unclear, or the engine refused it. The engine's
            message is passed through unchanged: it carries DuckDB's own wording, which
            names the actual mistake.
    """
    table = str(action.table or "").strip()
    column = str(action.column or "").strip()

    if action.action == "none" or not table or not column:
        detail = str(action.explanation or "").strip()
        raise AnalystError(
            detail
            or "I couldn't tell which table and column you meant. Try the Actions menu, "
            "or name them explicitly."
        )

    try:
        if action.action == ACTION_ADD:
            statements = engine_columns.add_calculated_column(
                connection,
                table,
                column,
                action.expression,
                related_table=action.related_table or None,
                relationships=relationships,
            )
            summary = f"Added `{column}` to `{table}`."
        elif action.action == ACTION_DELETE:
            statements = engine_columns.drop_column(connection, table, column)
            summary = f"Removed `{column}` from `{table}`."
        elif action.action == ACTION_UPDATE:
            affected = engine_columns.affected_row_count(connection, table, action.condition)
            statements = engine_columns.update_column_values(
                connection, table, column, action.expression, action.condition
            )
            summary = f"Updated `{column}` in `{table}` — {affected} row(s) changed."
        else:
            raise AnalystError(f"'{action.action}' isn't a column change I can make.")
    except (CalculatedColumnError, UnsafeSqlError) as error:
        logger.info("Column change refused for %s.%s: %s", table, column, error)
        raise AnalystError(str(error)) from error

    logger.info("Applied conversational column change: %s on %s.%s", action.action, table, column)
    return ColumnChange(action=action.action, summary=summary, statements=statements)


def handle_message(
    profile: dict,
    connection: duckdb.DuckDBPyConnection,
    schema_context: str,
    message: str,
    hint: str | None = None,
    *,
    relationships: list[Relationship] | None = None,
    key_path: Path | str | None = None,
) -> ColumnChange:
    """Parses a plain-English column request and applies it. Raises `AnalystError` on any
    failure, which the page turns into an assistant message."""
    action = parse_request(profile, message, schema_context, hint, key_path=key_path)
    return apply_action(connection, action, relationships)
