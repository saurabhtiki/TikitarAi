"""Pulling evaluation answers out of a conversation (requirement 6.7, Phase 2, spec 3b).

Spec 3b's premise is that the same short questions go to several comparable invitees and are
answered *inside the ordinary discussion* rather than on a form. So the answers have to be
read back out of what was said, which is what this module does.

It follows `summary_agent`'s rule for the same reason and by the same mechanism: the
signature takes a `list[ChatMessage]`, so there is no argument on it that a rolling summary
could arrive through. An evaluation cell is a permanent, comparable claim about an invitee —
"8 yrs (High)" — and a lossy fold is the last thing that should be behind one.

Two things are decided here rather than trusted from the model. A **classification tag that
isn't one of the creator's buckets is dropped**, because the matrix groups by that string and
a near-miss would quietly become a fourth bucket. And an **answer to a question that wasn't
asked is dropped**, because it has no field to be stored against.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from llm.client import LLMConnectionError, run_structured
from meetings.exceptions import MeetingAgentError
from meetings.model import ChatMessage, EvaluationAnswer, EvaluationField, Meeting
from meetings.running_summary import render_turns

logger = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "You read a conversation and extract short factual answers to a fixed list of questions. "
    "For every question listed, give the shortest answer the invitee actually gave — a "
    "figure, a date, a name, a yes or no — quoted as they gave it, never rounded or inferred. "
    "Leave raw_answer empty if the conversation does not answer that question; never guess. "
    "Where a question lists classification options, also set classified_tag to exactly one of "
    "those options, copied character for character. Where it lists none, leave classified_tag "
    "empty. Repeat each question back in the question field exactly as it was given to you."
)


class FieldAnswer(BaseModel):
    """One question's answer, as the model read it out of the conversation."""

    question: str = Field(description="The question, exactly as it was listed.")
    raw_answer: str = Field(default="", description="The invitee's own short answer, or empty.")
    classified_tag: str = Field(
        default="", description="One of the listed options for this question, or empty."
    )


class ExtractedAnswers(BaseModel):
    """Every question's answer from one conversation."""

    answers: list[FieldAnswer] = Field(default_factory=list)


def build_prompt(meeting: Meeting, fields: list[EvaluationField], messages: list[ChatMessage]) -> str:
    """The questions, their options, and the entire transcript."""
    lines = []
    for field_spec in fields:
        options = ", ".join(field_spec.buckets)
        suffix = f" [classify as one of: {options}]" if options else ""
        lines.append(f"- {field_spec.question}{suffix}")

    transcript = render_turns(messages)
    return (
        f"Meeting subject: {meeting.display_subject()}\n\n"
        f"Questions to answer:\n" + "\n".join(lines) + "\n\n"
        f"The full conversation:\n{transcript or '(the invitee sent no messages)'}"
    )


def extract_answers(
    meeting: Meeting,
    profile: dict,
    fields: list[EvaluationField],
    messages: list[ChatMessage],
    *,
    key_path: Path | str | None = None,
) -> list[EvaluationAnswer]:
    """One answer per evaluation field, read from the messages given.

    Every field comes back, including the ones the conversation never touched — an empty
    answer is a real finding in a comparison ("this vendor never said"), and a field missing
    from the matrix instead reads as one nobody was asked.

    Raises:
        MeetingAgentError: if the provider fails. The callers close the chat or generate a
            status *before* calling this, so a failure here costs the extraction and is
            re-runnable from the creator's page; it never costs the MoM.
    """
    if not fields:
        # No provider call at all: an evaluation-free meeting is the common case, and the
        # toggle being off should not cost a request per close.
        return []

    try:
        result = run_structured(
            profile,
            build_prompt(meeting, fields, messages),
            ExtractedAnswers,
            instructions=_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Extracting evaluation answers for meeting %s failed: %s", meeting.meeting_id, error)
        raise MeetingAgentError(f"Couldn't extract the evaluation answers: {error}") from error

    return _match_to_fields(result, fields)


def _match_to_fields(result: ExtractedAnswers, fields: list[EvaluationField]) -> list[EvaluationAnswer]:
    """Pairs the model's answers back to the fields they belong to.

    Matched on the question text, case- and whitespace-insensitively, because that is the
    only handle the model was given. An answer matching nothing is dropped rather than
    appended: it has no `field_id`, so there is no cell it could be shown in.
    """
    by_question = {}
    for answer in result.answers:
        by_question.setdefault(answer.question.strip().lower(), answer)

    matched = []
    for field_spec in fields:
        answer = by_question.get(field_spec.question.strip().lower())
        matched.append(
            EvaluationAnswer(
                field_id=field_spec.field_id,
                raw_answer=(answer.raw_answer or "").strip() if answer else "",
                classified_tag=_valid_bucket(answer.classified_tag if answer else "", field_spec),
            )
        )
    return matched


def _valid_bucket(tag: str, field_spec: EvaluationField) -> str:
    """The creator's own bucket this tag names, or "".

    Case-insensitive, but the *creator's* spelling is what comes back — the matrix groups on
    this string, so "high" and "High" arriving from two invitees have to end up identical.
    """
    wanted = (tag or "").strip().lower()
    if not wanted or not field_spec.buckets:
        return ""
    for bucket in field_spec.buckets:
        if bucket.strip().lower() == wanted:
            return bucket
    logger.info(
        "Dropping classification '%s' for '%s' — not one of the defined buckets.",
        tag,
        field_spec.question,
    )
    return ""
