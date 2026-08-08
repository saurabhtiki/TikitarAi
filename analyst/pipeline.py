"""One question in, one answer out (requirements 5.4, 5.5, 6.2).

This is the seam the page calls. It decides which of the two pipelines a message enters —
requirement 5.4's guarded column change, or requirement 5.5's read-only agent — then, for
the agent path, applies requirement 6.2's output-type table and builds whichever of the
three outputs were asked for.

All three are built from the **one** dataframe the SQL returned: the chart plots it, the
table shows it, and the commentary call receives it. That is requirement 6.2's own reason
for the arrangement — the outputs cannot disagree because there is only one result.

Streamlit-free on purpose, so the whole decision path is testable without `AppTest`. The
page's job is reduced to rendering an `Answer`.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from agno.db.base import BaseDb

from analyst import charts, commentary, routing
from analyst.agent import NO_QUERY_WARNING, QueryResult, answer_question
from analyst.column_intent import handle_message
from analyst.exceptions import AnalystError
from engine.relationships import Relationship

logger = logging.getLogger(__name__)


@dataclass
class Answer:
    """Everything one question produced, ready to render."""

    question: str
    text: str = ""
    sql: str | None = None
    frame: pd.DataFrame | None = None
    figure: object | None = None
    choices: charts.ChartChoices | None = None
    style: charts.ChartStyle | None = None
    outputs: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    # Kept apart from `warnings` because they describe the chart specifically, and the page
    # lets the user redraw it — at which point these are stale and the others are not.
    chart_warnings: list[str] = field(default_factory=list)
    is_error: bool = False
    statements: list[str] = field(default_factory=list)


def answer(
    profile: dict,
    connection: duckdb.DuckDBPyConnection,
    schema_context: str,
    question: str,
    *,
    knowledge_base: str | None = None,
    relationships: list[Relationship] | None = None,
    db: BaseDb | None = None,
    session_id: str | None = None,
    previous_frame: pd.DataFrame | None = None,
    previous_sql: str | None = None,
    key_path: Path | str | None = None,
) -> Answer:
    """Answers one chat message.

    Args:
        relationships: the session's confirmed links. Only used by a conversational
            column-add that reaches across one of them — see
            `engine.columns.add_calculated_column`.
        db: the Agno store holding this chat's history, so follow-up questions resolve
            against what came before. See `analyst.session.agent_db`.
        session_id: which conversation in `db` this message belongs to.
        previous_frame: the rows the last answer was built from, used only to rescue a
            chart request the model answered without querying — see
            `_chart_the_previous_result`.
        previous_sql: the statement behind `previous_frame`, shown as this answer's SQL
            when that rescue happens, since it is what the chart is plotting.

    Never raises: every failure becomes an `Answer` with `is_error` set, because a bad
    question must not take the transcript down with it.
    """
    question = str(question or "").strip()
    if not question:
        return Answer(question=question, text="Ask a question first.", is_error=True)

    action = routing.looks_like_column_action(question)
    if action is not None:
        return _column_change(
            profile, connection, schema_context, question, action, relationships=relationships, key_path=key_path
        )

    return _query_answer(
        profile,
        connection,
        schema_context,
        question,
        knowledge_base=knowledge_base,
        db=db,
        session_id=session_id,
        previous_frame=previous_frame,
        previous_sql=previous_sql,
        key_path=key_path,
    )


def _column_change(
    profile: dict,
    connection: duckdb.DuckDBPyConnection,
    schema_context: str,
    question: str,
    hint: str,
    *,
    relationships: list[Relationship] | None,
    key_path: Path | str | None,
) -> Answer:
    """Requirement 5.4's conversational path — the same engine functions the dialogs call."""
    try:
        change = handle_message(
            profile, connection, schema_context, question, hint, relationships=relationships, key_path=key_path
        )
    except AnalystError as error:
        logger.info("Conversational column change failed: %s", error)
        return Answer(question=question, text=str(error), is_error=True)

    return Answer(
        question=question,
        text=change.summary,
        # Requirement 5.4: the actual statement executed is shown in the chat.
        sql=";\n".join(change.statements),
        statements=change.statements,
        warnings=change.warnings,
    )


def _query_answer(
    profile: dict,
    connection: duckdb.DuckDBPyConnection,
    schema_context: str,
    question: str,
    *,
    knowledge_base: str | None,
    db: BaseDb | None,
    session_id: str | None,
    previous_frame: pd.DataFrame | None = None,
    previous_sql: str | None = None,
    key_path: Path | str | None = None,
) -> Answer:
    """Requirement 5.5's agent path, with requirement 6.2's output logic on top."""
    try:
        result = answer_question(
            profile,
            connection,
            schema_context,
            question,
            db=db,
            session_id=session_id,
            key_path=key_path,
        )
    except AnalystError as error:
        logger.info("Agent could not answer '%s': %s", question, error)
        return Answer(question=question, text=str(error), is_error=True)

    outputs = routing.classify_output(question, result.frame)
    if routing.OUTPUT_CHART in outputs and not result.has_rows:
        _chart_the_previous_result(result, previous_frame, previous_sql)

    built = Answer(
        question=question,
        sql=result.sql,
        frame=result.frame,
        outputs=outputs,
        warnings=list(result.warnings),
    )

    if routing.OUTPUT_CHART in outputs:
        # Choosing and drawing are kept apart so the choices travel with the answer: they
        # are what the page's customize controls open pre-filled with.
        choices, choice_warnings = charts.choose_chart(result.frame, question)
        figure, draw_warnings = (
            charts.render_chart(result.frame, choices) if choices is not None else (None, [])
        )
        built.figure = figure
        chart_warnings = choice_warnings + draw_warnings
        if figure is None:
            # A chart was asked for and can't be drawn, so the rows become the answer
            # instead of the user getting nothing at all. The reason travels with the
            # message rather than the chart — there is no chart for it to sit under.
            outputs.add(routing.OUTPUT_DATAFRAME)
            built.warnings.extend(chart_warnings)
        else:
            built.choices = choices
            # The untouched default, so the page's style controls have somewhere to start
            # and the chart on screen is fully described by `choices` + `style`.
            built.style = charts.ChartStyle()
            built.chart_warnings = chart_warnings

    if routing.OUTPUT_COMMENTARY in outputs:
        text, commentary_warnings = commentary.write_commentary(
            profile, question, result.frame, result.sql, knowledge_base=knowledge_base, key_path=key_path
        )
        built.text = text
        built.warnings.extend(commentary_warnings)

    # The agent's own one-line reply is the message body whenever there is no written
    # commentary to put there — a chart-only or table-only question, or a commentary call
    # that failed. An assistant turn with no text at all reads as a broken chat, and the
    # reply describes the same result set as everything else in the message.
    if not built.text:
        built.text = result.narrative or "That query returned no rows."

    return built


def _chart_the_previous_result(
    result: QueryResult, previous_frame: pd.DataFrame | None, previous_sql: str | None
) -> None:
    """Charts the last answer's rows when a chart was asked for and no query ran.

    "Show that as a pie chart" is a follow-up about rows the user is already looking at, so
    the model is meant to re-run the query behind them and let this module plot the result.
    Weaker models sometimes read it as a question about their own abilities instead, reply
    "I can't draw charts" and never call `run_query` — which leaves nothing to plot and
    costs the user the one output they explicitly asked for.

    Re-using the previous frame is honest here in a way inventing data never would be: they
    are the same rows, from the same statement, already on screen one turn above. The model
    isn't given a second chance to phrase it, because the prose it produced is a refusal —
    it is replaced, and the SQL shown is the one the rows actually came from.

    Mutates `result` in place; does nothing when there is no previous frame to fall back on.
    """
    if previous_frame is None or previous_frame.empty:
        return

    logger.info("Charting the previous result: the agent answered a chart request without querying.")
    result.frame = previous_frame
    result.sql = previous_sql
    result.narrative = "Here is the previous answer as a chart."
    # No longer true of an answer that now carries rows, and it would sit directly beside
    # the warning below saying which rows they are.
    result.warnings = [warning for warning in result.warnings if warning != NO_QUERY_WARNING]
    result.warnings.append(
        "This chart plots the previous answer's rows — the question didn't run a new query."
    )
