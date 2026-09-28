"""The organiser's FAQ and the side questions it couldn't answer (phase 53).

The whole FAQ goes into the chat prompt rather than being searched: at the 50–300 rows an
organiser keeps in a spreadsheet it fits comfortably, and a model that sees every answer
can say truthfully when none of them fits — which is what decides whether a question is
logged for the organiser.

No Streamlit and no database here; `meetings/db.py` stores what these functions build.
"""

import datetime
import logging

import pandas as pd

from meetings.exceptions import MeetingStorageError
from meetings.model import Faq, FaqEntry, FaqMiss
from utils.dates import DATE_TEXT_FORMAT

logger = logging.getLogger(__name__)

MAX_FAQ_ENTRIES = 300
QUESTION_COLUMN = "Question"
ANSWER_COLUMN = "Answer"


def guess_columns(columns: list[str]) -> tuple[str, str]:
    """The upload's question and answer columns: the ones named so, else the first two."""
    names = [str(column) for column in columns]

    def _find(word: str) -> str | None:
        return next((name for name in names if word in name.casefold()), None)

    question = _find("question") or (names[0] if names else "")
    answer = _find("answer") or next((name for name in names if name != question), "")
    return question, answer


def template_frame() -> pd.DataFrame:
    """A sample FAQ sheet for the organiser to fill in and upload back (phase 55)."""
    return pd.DataFrame(
        [
            {QUESTION_COLUMN: "What is the notice period?", ANSWER_COLUMN: "60 days."},
            {QUESTION_COLUMN: "Is parking free?", ANSWER_COLUMN: "Yes, for all staff."},
        ],
        columns=[QUESTION_COLUMN, ANSWER_COLUMN],
    )


def entries_from_frame(frame: pd.DataFrame, question_column: str, answer_column: str) -> list[FaqEntry]:
    """The uploaded sheet as FAQ entries. Rows missing a question or an answer are dropped.

    Raises:
        MeetingStorageError: if the columns are wrong, nothing is left, or there are too many.
    """
    if question_column not in frame.columns or answer_column not in frame.columns:
        raise MeetingStorageError("Pick the Question and Answer columns from the uploaded file.")
    if question_column == answer_column:
        raise MeetingStorageError("The Question and Answer columns must be two different columns.")

    entries = []
    for question, answer in zip(frame[question_column], frame[answer_column]):
        question, answer = str(question).strip(), str(answer).strip()
        if question and answer:
            entries.append(FaqEntry(question=question, answer=answer))

    if not entries:
        raise MeetingStorageError("The file has no rows with both a question and an answer.")
    if len(entries) > MAX_FAQ_ENTRIES:
        raise MeetingStorageError(
            f"The FAQ has {len(entries)} questions — more than the {MAX_FAQ_ENTRIES} the bot can hold. "
            "Keep the ones invitees are most likely to ask."
        )
    return entries


def faq_block(faq: Faq | None) -> str:
    """The FAQ as text for the chat model's instructions, or "" if there is none."""
    if faq is None or not faq.entries:
        return ""
    return "\n".join(f"Q: {entry.question}\nA: {entry.answer}" for entry in faq.entries)


def same_question(text: str) -> str:
    """A question reduced for comparing: spacing, case and a trailing "?" ignored."""
    return " ".join(str(text or "").split()).casefold().rstrip("?. ")


def add_answers(faq: Faq | None, answered: list[FaqEntry]) -> Faq:
    """The FAQ with the organiser's new answers added; a question already in it is updated.

    Raises:
        MeetingStorageError: if the result would hold more than `MAX_FAQ_ENTRIES`.
    """
    base = faq or Faq(source_file="Added by hand")
    entries = list(base.entries)
    for new in answered:
        position = next(
            (index for index, entry in enumerate(entries) if same_question(entry.question) == same_question(new.question)),
            None,
        )
        if position is None:
            entries.append(new)
        else:
            entries[position] = new
    if len(entries) > MAX_FAQ_ENTRIES:
        raise MeetingStorageError(
            f"The FAQ would have {len(entries)} questions — more than the {MAX_FAQ_ENTRIES} the bot can hold."
        )
    return Faq(source_file=base.source_file, entries=entries)


def asked_on(created_at: str) -> str:
    """A stored UTC timestamp as dd-mm-yyyy, or the raw text if it doesn't parse."""
    try:
        return datetime.datetime.strptime(created_at[:10], "%Y-%m-%d").strftime(DATE_TEXT_FORMAT)
    except (TypeError, ValueError):
        logger.warning("Could not read the timestamp %r.", created_at)
        return str(created_at or "")


def misses_frame(misses: list[FaqMiss]) -> pd.DataFrame:
    """The unanswered questions as a table, with an empty Answer column to fill in."""
    return pd.DataFrame(
        [
            {
                "Invitee": miss.invitee_name,
                QUESTION_COLUMN: miss.question,
                "Asked during": miss.agenda_tag,
                "Asked on": asked_on(miss.created_at),
                ANSWER_COLUMN: "",
            }
            for miss in misses
        ],
        columns=["Invitee", QUESTION_COLUMN, "Asked during", "Asked on", ANSWER_COLUMN],
    )
