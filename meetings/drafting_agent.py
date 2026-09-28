"""Drafting agenda rows from a plain-English description (phase 54).

The model only writes rows into the grid; it decides nothing. The organiser sees the rows,
and `steps.agenda_problems` still has to pass before the meeting can be created, so a draft
with a bad rule is caught exactly as a hand-typed one would be.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from llm.client import LLMConnectionError, run_structured
from meetings import steps
from meetings.exceptions import MeetingAgentError
from meetings.model import (
    ANSWER_TEXT,
    DISCUSSION_ITEM,
    QUESTION_ITEM,
    TABLE_ITEM,
    AgendaItem,
    clamp_tries,
)

logger = logging.getLogger(__name__)

_TYPE_WORDS = {"question": QUESTION_ITEM, "discussion": DISCUSSION_ITEM, "table": TABLE_ITEM}

_INSTRUCTIONS = (
    "You turn an organiser's plain-English description of a meeting into agenda rows. Keep the "
    "order they describe. Use item_type 'question' for anything with a definite answer (a "
    "number, date, yes/no or one of a few options) and 'discussion' for open talk.\n"
    "For a question, answer_type is one of: Text, Number, Date, Yes/No, Choice. The rule must "
    "use exactly these words, or be empty:\n"
    "- Number: '> 0', '>= 18', '< 100', 'between 20000 and 60000'\n"
    "- Date: 'future', 'past', 'after 01-10-2026', 'before 31-12-2026', 'within 30 days'\n"
    "- Text: 'at least 20 characters'\n"
    "- Yes/No: empty\n"
    "- Choice: the options separated by commas, e.g. 'Morning, Evening, Night'\n"
    "go_to is empty unless the description says to jump. Write it as 'if <condition> go to "
    "<a later question's title>' or 'if <condition> go to End', several separated by ';'. "
    "The condition uses the rule words (e.g. '> 60000') or Yes / No or a Choice option. "
    "A Text question can't have a go_to. Jumps only go forward.\n"
    "for_each is empty unless the description says to ask the questions for every row of a "
    "list (e.g. every outstanding invoice); then give those questions the list's name. "
    "Inside a list, go_to may say 'Next row'.\n"
    "Write numbers as plain digits (1 lakh = 100000). Titles are short (2-5 words) and each "
    "one is different. ai_note holds any extra fact the description gives for that row."
)


class DraftRow(BaseModel):
    title: str = Field(description="Short agenda item title.")
    item_type: str = Field(default="question", description="question or discussion.")
    answer_type: str = Field(default="Text", description="Text, Number, Date, Yes/No or Choice.")
    rule: str = Field(default="", description="The rule words, or empty.")
    tries: int = Field(default=3, description="Wrong answers allowed, 1 to 5.")
    go_to: str = Field(default="", description="'if > 60000 go to End', or empty.")
    for_each: str = Field(default="", description="The list's name, or empty.")
    ai_note: str = Field(default="", description="Extra facts for this row, or empty.")


class DraftAgenda(BaseModel):
    rows: list[DraftRow] = Field(default_factory=list)


def build_prompt(description: str) -> str:
    return f"The organiser's description:\n{description.strip()}"


def _answer_type(word: str) -> str:
    cleaned = " ".join(str(word or "").replace("_", "/").split())
    for label, answer_type in steps.ANSWER_TYPE_BY_LABEL.items():
        if label.lower() == cleaned.lower():
            return answer_type
    return ANSWER_TEXT


def to_agenda(draft: DraftAgenda) -> list[AgendaItem]:
    """The model's rows as agenda items; unknown words fall back to Discussion / Text."""
    items = []
    for row in draft.rows:
        title = " ".join(str(row.title or "").split())
        if not title:
            continue
        item_type = _TYPE_WORDS.get(str(row.item_type or "").strip().lower(), DISCUSSION_ITEM)
        is_question = item_type == QUESTION_ITEM
        items.append(
            AgendaItem(
                item=title,
                ai_note=str(row.ai_note or "").strip(),
                item_type=item_type,
                answer_type=_answer_type(row.answer_type) if is_question else ANSWER_TEXT,
                rule=str(row.rule or "").strip() if is_question else "",
                max_tries=clamp_tries(row.tries),
                branch=str(row.go_to or "").strip() if is_question else "",
                loop=str(row.for_each or "").strip() if is_question else "",
            )
        )
    return steps.tidy_loop_names(items)


def draft_agenda(profile: dict, description: str, *, key_path: Path | str | None = None) -> list[AgendaItem]:
    """Agenda rows drafted from the description.

    Raises:
        MeetingAgentError: if the description is empty, the provider fails, or nothing
            usable comes back.
    """
    if not description.strip():
        raise MeetingAgentError("Describe the meeting first, e.g. 'Ask expected salary; if above 1 lakh end.'")
    try:
        draft = run_structured(profile, build_prompt(description), DraftAgenda, instructions=_INSTRUCTIONS,
                               key_path=key_path)
    except LLMConnectionError as error:
        logger.warning("Drafting an agenda failed: %s", error)
        raise MeetingAgentError(f"Couldn't draft the agenda: {error}") from error

    items = to_agenda(draft)
    if not items:
        logger.warning("Drafting an agenda returned no rows.")
        raise MeetingAgentError("The AI didn't return any agenda rows. Try describing the questions in more detail.")
    return items
