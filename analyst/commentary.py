"""Written commentary on a result, as its own narrow call (requirement 6.2).

Requirement 6.2 is explicit that commentary is "produced via a separate, narrow LLM call,
distinct from the SQL-generation call — each call has one job, which improves
reliability". So this does not reuse the analyst agent: no tools, no schema, no database
access, no chance of it running another query and describing different numbers than the
ones on screen. It receives the rows that were actually computed and writes about those.

`knowledge_base` is requirement 7.2's report-level domain rules. It is unused until Task
Builder exists, and the parameter is here now so that adding it later is a caller change
rather than a signature change.
"""

import logging
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field

from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

# Enough rows for the model to spot the shape of the data without paying to read all of
# it. Totals and extremes live at the top of a sorted result, which is how the agent is
# instructed to return them.
COMMENTARY_ROWS = 40
COMMENTARY_CHARS = 3000

INSTRUCTIONS = (
    "You write short, factual commentary on a query result for a business reader. "
    "State what the numbers show. Quote figures exactly as given — never round, rescale "
    "or recompute them, and never mention a value that is not in the rows provided. "
    "Two to four sentences. No preamble, no bullet lists, no restating the question."
)


class Commentary(BaseModel):
    """The structured shape the commentary call returns."""

    summary: str = Field(description="Two to four sentences describing what the result shows.")


def render_rows(frame: pd.DataFrame) -> str:
    """Renders a result for the commentary prompt, capped in rows and characters."""
    if frame.empty:
        return "The query returned no rows."

    text = frame.head(COMMENTARY_ROWS).to_csv(index=False).strip()
    if len(text) > COMMENTARY_CHARS:
        text = text[:COMMENTARY_CHARS].rsplit("\n", 1)[0] + "\n…"

    if len(frame) > COMMENTARY_ROWS:
        text += f"\n({len(frame)} rows in total, first {COMMENTARY_ROWS} shown)"
    return text


def build_prompt(
    question: str, frame: pd.DataFrame, sql: str | None, knowledge_base: str | None
) -> str:
    """Assembles the commentary prompt from the computed result."""
    parts = [f"Question asked:\n{question}", f"Result:\n{render_rows(frame)}"]
    if sql:
        parts.append(f"SQL that produced it (for context only, do not mention it):\n{sql}")
    if knowledge_base and knowledge_base.strip():
        # Requirement 7.2: domain rules the author attached to the report, applied
        # wherever commentary is generated.
        parts.append(f"Domain rules to apply when interpreting this:\n{knowledge_base.strip()}")
    return "\n\n".join(parts)


def write_commentary(
    profile: dict,
    question: str,
    frame: pd.DataFrame | None,
    sql: str | None = None,
    *,
    knowledge_base: str | None = None,
    key_path: Path | str | None = None,
) -> tuple[str, list[str]]:
    """Writes commentary on a computed result.

    Returns:
        `(commentary, warnings)`. Never raises: commentary is one of three outputs, and a
        provider hiccup must not cost the user the table and chart that already computed
        successfully. A failure comes back as an empty string plus a warning.
    """
    if frame is None or frame.empty:
        return "", ["There were no rows to comment on."]

    try:
        result = run_structured(
            profile,
            build_prompt(question, frame, sql, knowledge_base),
            Commentary,
            instructions=INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Commentary call failed for '%s': %s", question, error)
        return "", [f"Couldn't write the commentary: {error}"]

    summary = str(result.summary or "").strip()
    if not summary:
        return "", ["The model returned empty commentary."]
    return summary, []
