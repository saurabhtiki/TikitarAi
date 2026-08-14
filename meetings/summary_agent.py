"""The point-wise MoM (requirement 6.7, spec 4).

**This module reads the full transcript, always.** `generate_summary` takes a
`list[ChatMessage]` — not a session, not a running summary — so there is no field on its
arguments that could stand in for the real history. That signature is the mechanism, not a
convention: `running_summary` is a lossy rolling fold meant to keep a live conversation
inside a context window, and building a permanent record on top of it would mean the MoM
quietly degrades the longer the conversation was.

One schema and one function serve both callers, because spec 3 asks for the same content
either way:

- **Close Chat** (invitee) → stored as the final MoM, locked.
- **Generate Status** (creator, on demand) → stored as the live snapshot, overwritten.

Only the column the caller writes to differs.
"""

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from llm.client import LLMConnectionError, run_structured
from meetings import tables
from meetings.exceptions import MeetingAgentError
from meetings.model import OPENING_TAG, ChatMessage, Meeting
from meetings.running_summary import render_turns

logger = logging.getLogger(__name__)

_INSTRUCTIONS = (
    "You write the minutes of a conversation between an AI facilitator and one invitee. "
    "Produce one entry per agenda item listed, in the order given, using the item's exact "
    "title. Mark an item discussed only if it was genuinely addressed — listing it in the "
    "opening message does not count — and leave its notes empty when it was not. "
    "Summarise point-wise and factually, quoting figures, dates and commitments exactly as "
    "the invitee gave them; never round or infer. "
    "Put anything raised that does not belong to a listed agenda item in other_extra. "
    "End with a short one or two line closing message thanking the invitee."
)


class AgendaItemSummary(BaseModel):
    """What happened on one agenda item.

    A table item reuses this shape rather than getting one of its own: `discussed` becomes
    "any rows filled" and `notes` becomes the completion line. Both are written from the
    stored counts, never by the model — see `_with_every_item`.
    """

    item: str = Field(description="The agenda item's title, exactly as listed.")
    discussed: bool = Field(description="Whether this item was genuinely addressed.")
    notes: str = Field(default="", description="Point-wise summary. Empty if not discussed.")
    is_table: bool = Field(
        default=False, description="Set by the app, not the model: a structured-table item."
    )


class MeetingSummary(BaseModel):
    """The MoM for one invitee's conversation."""

    agenda_items: list[AgendaItemSummary] = Field(default_factory=list)
    other_extra: str = Field(default="", description="Off-agenda points raised, or empty.")
    closing_message: str = Field(default="", description="A short sign-off for the invitee.")


def build_prompt(meeting: Meeting, messages: list[ChatMessage]) -> str:
    """The agenda plus the entire transcript.

    Takes the messages it is given and renders all of them. The opening message is included
    rather than filtered out — it is what the invitee was responding to, and dropping it
    would leave the first answer with no question above it.

    Table items are named but explicitly excluded from what the model reports on: their
    entry is a completion figure the app already knows, and asking a model to restate a
    number it can only estimate is how a wrong one gets into a permanent record.
    """
    parts = [f"Meeting subject: {meeting.display_subject()}"]

    if meeting.meeting_context.strip():
        parts.append(f"Why this meeting happened:\n{meeting.meeting_context.strip()}")

    discussion_items = meeting.discussion_items()
    if discussion_items:
        agenda_lines = []
        for item in discussion_items:
            note = f" — {item.ai_note.strip()}" if item.ai_note.strip() else ""
            agenda_lines.append(f"- {item.item}{note}")
        parts.append("Agenda items to report on:\n" + "\n".join(agenda_lines))
    else:
        parts.append("This meeting had no discussion agenda; report everything under other_extra.")

    table_items = meeting.table_items()
    if table_items:
        parts.append(
            "These items were handled as structured tables and are reported separately — do "
            "not write entries for them:\n"
            + "\n".join(f"- {item.item}" for item in table_items)
        )

    transcript = render_turns(messages)
    parts.append(f"The full conversation:\n{transcript or '(the invitee sent no messages)'}")

    return "\n\n".join(parts)


def generate_summary(
    meeting: Meeting,
    profile: dict,
    messages: list[ChatMessage],
    *,
    table_progress: dict[str, tuple[int, int]] | None = None,
    key_path: Path | str | None = None,
) -> MeetingSummary:
    """The MoM for one conversation, built from the messages given.

    `table_progress` maps a table item's title to its `(filled, total)` row counts. It is
    applied after the call, over whatever the model said, because those counts are a fact the
    app holds and the model can only guess at.

    Raises:
        MeetingAgentError: if the provider fails or returns nothing usable. Unlike the
            drafting calls in `checks/actions.py`, this one raises rather than returning
            warnings: both callers store what comes back, and storing an empty MoM as
            though it were the record would be worse than reporting the failure.
    """
    try:
        result = run_structured(
            profile,
            build_prompt(meeting, messages),
            MeetingSummary,
            instructions=_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Generating a summary for meeting %s failed: %s", meeting.meeting_id, error)
        raise MeetingAgentError(f"Couldn't generate the summary: {error}") from error

    return _with_every_item(result, meeting, table_progress or {})


def _with_every_item(
    summary: MeetingSummary, meeting: Meeting, table_progress: dict[str, tuple[int, int]]
) -> MeetingSummary:
    """Fills in any agenda item the model left out, and overwrites every table item.

    Spec 3 wants the MoM to say *Discussed / Not Discussed per agenda item* — an item
    missing from the reply reads on screen as though it were never on the agenda, which is
    a different and more flattering claim than "nobody got to it".

    A table item's entry is written here outright rather than merged: spec 3a reports it as
    a completion percentage, and anything the model wrote about it is prose about a grid it
    was told not to report on.
    """
    reported = {entry.item.strip().lower(): entry for entry in summary.agenda_items}
    ordered = []
    for item in meeting.agenda:
        if item.is_table():
            filled, total = table_progress.get(item.item, (0, 0))
            ordered.append(
                AgendaItemSummary(
                    item=item.item,
                    discussed=filled > 0,
                    notes=tables.format_completion(filled, total),
                    is_table=True,
                )
            )
            continue
        entry = reported.get(item.item.strip().lower())
        ordered.append(entry if entry is not None else AgendaItemSummary(item=item.item, discussed=False))

    # Anything the model reported that isn't on the agenda is kept rather than dropped, at
    # the end — it is still something it thought worth recording.
    known = {item.item.strip().lower() for item in meeting.agenda}
    ordered.extend(entry for key, entry in reported.items() if key not in known)

    summary.agenda_items = ordered
    return summary


def coverage(messages: list[ChatMessage], meeting: Meeting) -> set[str]:
    """Which discussion agenda items this conversation has actually touched.

    Counted from the stored tags rather than from a summary, so the creator's status table
    doesn't need an LLM call to draw a row. The opening message is excluded — it names
    every item without discussing any of them.

    Table items are excluded too, and not because they can't be tagged: an exchange *about*
    a grid is legitimately tagged with its title. They are excluded because their progress is
    measured in rows filled, so counting a table item here as well would let one passing
    mention of it read as the same thing as having filled it in.
    """
    titles = {item.item for item in meeting.discussion_items()}
    return {
        message.agenda_tag
        for message in messages
        if message.agenda_tag in titles and message.agenda_tag != OPENING_TAG
    }


def to_json(summary: MeetingSummary) -> str:
    """The summary as stored in `summary_json` / `live_status_json`."""
    return summary.model_dump_json(indent=2)


def from_json(text: str) -> MeetingSummary | None:
    """A stored summary, or None if it can't be read.

    None rather than an exception: a summary that won't parse should cost the reader that
    one panel, not the whole meeting-detail page it is a tab of.
    """
    if not (text or "").strip():
        return None
    try:
        return MeetingSummary.model_validate_json(text)
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.exception("A stored meeting summary could not be read.")
        return None
