"""Deciding what a question wants back, without asking a model (requirements 5.4, 6.2).

Requirement 6.2 is written as a keyword table, and it is implemented here as a keyword
table. That is not a shortcut: because the routing is deterministic, the chart, the
dataframe and the commentary for one question are all derived from the *same* computed
result set, which is precisely what 6.2 asks for when it says the three outputs "never
disagree with one another". Handing the decision to a model would reintroduce exactly the
inconsistency the requirement rules out, and would cost a round trip per question.

The same reasoning applies to `looks_like_column_action`. It is a cheap first filter that
decides *which pipeline* a message enters — the read-only agent, or the guarded
calculated/delete/update path of requirement 5.4. Getting that wrong is recoverable (the
column-action pipeline reports "that didn't look like a column change" and stops), whereas
routing every message through an LLM classifier would put a model call in front of every
question the user ever asks.

Nothing here imports Streamlit, pandas is used only to read a frame's shape.
"""

import logging
import re

import pandas as pd

logger = logging.getLogger(__name__)

# Requirement 6.2's three output types.
OUTPUT_CHART = "chart"
OUTPUT_DATAFRAME = "dataframe"
OUTPUT_COMMENTARY = "commentary"

# One pattern per row of requirement 6.2's table. Word boundaries matter: "listen" must
# not trigger the "list" row, and "charter" must not trigger the chart row.
_CHART_PATTERN = re.compile(r"\b(charts?|graphs?|plots?|visuali[sz]e[ds]?|visuali[sz]ations?)\b", re.IGNORECASE)
_DATAFRAME_PATTERN = re.compile(
    r"\b(tables?|dataframes?|list|lists|show me the data)\b", re.IGNORECASE
)
_COMMENTARY_PATTERN = re.compile(r"\b(why|explain|explains?|summar[yi]\w*|insights?)\b", re.IGNORECASE)
_ALL_PATTERN = re.compile(r"\b(all|everything|full breakdown)\b", re.IGNORECASE)

# Requirement 5.4's three conversational actions. `add` deliberately requires the word
# "column" or an assignment, so "add up the totals" stays an ordinary question.
_ADD_PATTERN = re.compile(
    r"\b(add|create|calculate|compute|derive)\b(?=[\s\S]*?(\bcolumns?\b|=))", re.IGNORECASE
)
_DELETE_PATTERN = re.compile(r"\b(delete|remove|drop)\b", re.IGNORECASE)
_UPDATE_PATTERN = re.compile(
    r"\b(update|set|mark|flag|replace|change|rename the values|overwrite)\b", re.IGNORECASE
)

# A question phrased as a question is a question, whatever verbs it contains. "Which
# customers should we mark as overdue?" asks for a list; it does not ask to write to the
# table. Checked before the action patterns.
_QUESTION_PATTERN = re.compile(
    r"^\s*(what|which|who|whom|whose|when|where|why|how|is|are|was|were|do|does|did|can|could|"
    r"should|would|will|list|show|give|tell|find|count|compare)\b",
    re.IGNORECASE,
)

ACTION_ADD = "add"
ACTION_DELETE = "delete"
ACTION_UPDATE = "update"


def classify_output(question: str, frame: pd.DataFrame | None) -> set[str]:
    """Returns the output types a question and its result call for (requirement 6.2).

    The keyword rows are additive — "show me a table and a chart" yields both — and the
    two defaults at the bottom of 6.2's table apply only when no keyword matched at all.

    Args:
        question: what the user typed.
        frame: the computed result, or None when nothing was returned.
    """
    text = str(question or "")
    outputs: set[str] = set()

    if _ALL_PATTERN.search(text):
        return {OUTPUT_CHART, OUTPUT_DATAFRAME, OUTPUT_COMMENTARY}

    if _CHART_PATTERN.search(text):
        outputs.add(OUTPUT_CHART)
    if _DATAFRAME_PATTERN.search(text):
        outputs.add(OUTPUT_DATAFRAME)
    if _COMMENTARY_PATTERN.search(text):
        outputs.add(OUTPUT_COMMENTARY)

    if outputs:
        return outputs

    # No keyword: the shape of the answer decides. A single value reads as a sentence; a
    # table of rows reads as a table, with a sentence to say what it shows.
    if frame is None or frame.empty:
        return {OUTPUT_COMMENTARY}
    if is_single_value(frame):
        return {OUTPUT_COMMENTARY}
    return {OUTPUT_DATAFRAME, OUTPUT_COMMENTARY}


def is_single_value(frame: pd.DataFrame | None) -> bool:
    """True when a result is one cell — the "single value" row of requirement 6.2."""
    return frame is not None and frame.shape == (1, 1)


def looks_like_column_action(message: str) -> str | None:
    """Routes a chat message to requirement 5.4's column pipeline, or to the agent.

    Returns `"add"`, `"delete"`, `"update"` or None. A message phrased as a question is
    always None: *"which invoices should be marked overdue?"* asks to see rows, not to
    write them.
    """
    text = str(message or "").strip()
    if not text:
        return None

    if _QUESTION_PATTERN.match(text) or text.endswith("?"):
        return None

    # Order matters. "delete the tax column" contains no add/update verb, but "add a
    # column and set it to 0" contains both — and it is an add.
    if _ADD_PATTERN.search(text):
        return ACTION_ADD
    if _DELETE_PATTERN.search(text):
        return ACTION_DELETE
    if _UPDATE_PATTERN.search(text):
        return ACTION_UPDATE
    return None
