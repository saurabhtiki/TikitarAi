"""The Agno agent that answers questions about the session's tables (requirement 5.5).

One `Agent`, one guarded `DuckDbTools` (see `analyst/tools.py`), and the schema context
the Data Engine already builds — table and column names, the confirmed relationships, and
the column descriptions and aliases, exactly as requirement 5.5 specifies.

The agent's prose is deliberately *not* the answer. The answer is the dataframe its query
returned, captured by the toolkit on the way past; the prose is only used as a fallback
when a question resolves without a query at all. That split is what lets requirement 6.2
build the chart, the table and the commentary from one result set that cannot disagree
with itself.

Nothing here imports Streamlit, so the whole module is testable with a stub model and no
network — which is how `tests/test_analyst_agent.py` exercises it.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd
from agno.agent import Agent
from agno.db.base import BaseDb

from analyst.exceptions import AgentRunError
from analyst.tools import SessionDuckDbTools
from llm.client import LLMConnectionError, build_model

logger = logging.getLogger(__name__)

# A question needing more than this many queries has gone wrong — usually the model
# looping on a column that doesn't exist. Better to stop and say so than to spend a
# user's tokens circling.
MAX_TOOL_CALLS = 8

# How many previous turns the agent sees. Enough for "now show that as a chart" and "break
# it down by month instead" to resolve, without resending a long chat on every question —
# each earlier turn carries its tool calls and query results with it.
HISTORY_RUNS = 5

SYSTEM_INSTRUCTIONS = [
    "You are a data analyst answering questions about the tables described below.",
    "Always answer by calling `run_query` with DuckDB SQL. Never answer from memory, "
    "never invent numbers, and never describe data you have not queried.",
    "Use only the tables and columns listed in the schema. If a question names something "
    "that is not there, say so plainly instead of guessing a similar column.",
    "Write exactly one SQL statement per `run_query` call, and no trailing semicolon.",
    "Join tables using the listed relationships. Prefer LEFT JOIN so rows without a match "
    "are still counted, unless the question asks only for matched rows.",
    "Alias aggregates to readable names (`SUM(basic) AS total_basic`), and put the label "
    "column first and the measure second so the result charts sensibly.",
    "You cannot create, alter, drop or update tables — those are handled elsewhere. If the "
    "question asks for one, say it needs the Actions menu.",
    "Earlier turns in this conversation are available to you. When a question refers back "
    "to one ('now by month instead', 'and the same for last year'), carry the previous "
    "query forward and change only what was asked — do not start from nothing.",
    "When the query succeeds, reply with one or two short sentences stating the answer. "
    "Do not repeat the whole table back, and do not show the SQL — both are displayed "
    "separately.",
]


@dataclass
class QueryResult:
    """What one answered question produced.

    Attributes:
        question: what the user asked, verbatim.
        sql: the SQL behind the answer, or None if the agent never ran one.
        frame: the rows it returned — the single source every output type is built from.
        narrative: the agent's own short reply, used when there is no frame to render.
        warnings: anything the user should know that isn't a failure.
    """

    question: str
    sql: str | None = None
    frame: pd.DataFrame | None = None
    narrative: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def has_rows(self) -> bool:
        return self.frame is not None and not self.frame.empty


def build_analyst(
    profile: dict,
    connection: duckdb.DuckDBPyConnection,
    schema_context: str,
    *,
    db: BaseDb | None = None,
    session_id: str | None = None,
    key_path: Path | str | None = None,
) -> tuple[Agent, SessionDuckDbTools]:
    """Assembles the agent and the toolkit whose executions it will be read from.

    The toolkit is returned alongside the agent because it is the only place the result
    dataframe exists — `Agent.run()` hands back prose.

    Args:
        db: where Agno stores this chat's session records. A new agent object is built for
            every question (the DuckDB connection and schema it is bound to can change
            between them), so without a store the model would meet each question with no
            memory of the last. Passing None keeps that stateless behaviour, which is what
            the tests want.
        session_id: which conversation in `db` this run belongs to.

    Raises:
        AgentRunError: if the session's provider profile can't be turned into a model.
    """
    try:
        model = build_model(profile, key_path=key_path)
    except LLMConnectionError as error:
        logger.exception("Could not build the session model for the analyst agent.")
        raise AgentRunError(str(error)) from error

    toolkit = SessionDuckDbTools(connection)
    agent = Agent(
        model=model,
        tools=[toolkit],
        instructions=SYSTEM_INSTRUCTIONS,
        additional_context=schema_context,
        tool_call_limit=MAX_TOOL_CALLS,
        db=db,
        session_id=session_id,
        add_history_to_context=db is not None,
        num_history_runs=HISTORY_RUNS,
        markdown=True,
        telemetry=False,
    )
    return agent, toolkit


def answer_question(
    profile: dict,
    connection: duckdb.DuckDBPyConnection,
    schema_context: str,
    question: str,
    *,
    db: BaseDb | None = None,
    session_id: str | None = None,
    key_path: Path | str | None = None,
) -> QueryResult:
    """Answers one question against the session's tables (requirement 5.5).

    Returns:
        A `QueryResult` carrying the SQL, the rows, and the agent's short reply.

    Raises:
        AgentRunError: if the provider fails, or the agent produced neither rows nor an
            explanation. The message is written for the person who asked.
    """
    question = str(question or "").strip()
    if not question:
        raise AgentRunError("Ask a question first.")

    agent, toolkit = build_analyst(
        profile, connection, schema_context, db=db, session_id=session_id, key_path=key_path
    )

    try:
        response = agent.run(question)
    except Exception as error:  # noqa: BLE001 — providers raise their own exception types
        logger.exception("The analyst agent failed on: %s", question)
        raise AgentRunError(_readable_failure(error, toolkit)) from error

    narrative = str(getattr(response, "content", "") or "").strip()
    result = QueryResult(question=question, narrative=narrative)

    last = toolkit.last_result
    if last is not None:
        result.sql, result.frame = last
        # A query that succeeded after earlier attempts failed is normal self-correction,
        # not something to put in front of the user.
        return result

    if toolkit.failures:
        failed_sql, reason = toolkit.failures[-1]
        logger.warning("Every agent query failed for '%s'. Last: %s", question, failed_sql)
        raise AgentRunError(
            f"I couldn't get that from your data. The last query I tried failed: {reason}"
        )

    if not narrative:
        raise AgentRunError(
            "The model returned nothing. Check the connection in the sidebar, then try again."
        )

    # No query, but something to say — usually "that column isn't in your data". Worth
    # showing: it is the model telling the user their question doesn't match the schema.
    result.warnings.append("This answer came from the model without running a query.")
    return result


def _readable_failure(error: Exception, toolkit: SessionDuckDbTools) -> str:
    """Explains a failed run, preferring the SQL error over the provider's stack noise."""
    if toolkit.failures:
        return f"I couldn't answer that. The last query failed: {toolkit.failures[-1][1]}"
    text = str(error).strip() or error.__class__.__name__
    return f"The model couldn't be reached: {text.splitlines()[0][:300]}"
