"""The AI persona an invitee talks to (requirement 6.7, spec 4).

Built on `llm.client.run_structured` rather than on `analyst/agent.py`'s `build_analyst`,
and the differences are all deliberate:

- **No tools.** The analyst queries DuckDB; this agent has nothing to query.
- **No Agno-managed history.** `build_analyst` passes `db=`/`session_id`/`num_history_runs`
  and lets Agno keep the last five runs. That is a fixed window with no summarisation, and
  spec 3 asks for a window *plus* a rolling summary of what fell out of it. So history is
  assembled as text here — the same way `checks/sql_builder.py` hands the schema over — and
  `running_summary.py` owns the folding.
- **Structured output.** Every reply has to carry the agenda item it was about, because the
  MoM groups by that tag and the creator's status table counts it. Free prose would leave
  nothing to store.

The model is the **meeting creator's** default profile, resolved fresh on every turn. An
invitee has no account and therefore no model choice of their own; resolving it per turn
rather than freezing it at meeting creation means fixing a broken API key in Settings
repairs chats that are already in progress.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from llm.client import run_structured
from meetings.model import (
    OPENING_TAG,
    OTHER_TAG,
    ChatMessage,
    Meeting,
    canonical_tag,
)
from meetings.running_summary import render_turns

logger = logging.getLogger(__name__)


class ChatTurnOutput(BaseModel):
    """One reply, and what it was about."""

    reply: str = Field(description="What to say to the invitee next.")
    agenda_tag: str = Field(
        # Defaulted so a model that answers in plain text still produces a usable turn —
        # `run_structured`'s `text_field` fills the reply, and an untagged exchange lands in
        # the "Other/Extra" section, which exists for exactly that.
        default="",
        description="The exact title of the agenda item this exchange is about, or 'Other/Extra'.",
    )


def system_instructions(meeting: Meeting) -> str:
    """The persona, the context, the SOP and the agenda, as one instruction block.

    Assembled per turn from the meeting as it stands rather than cached, so a creator who
    fixes a wrong figure in an agenda item's note has it apply to the very next message
    instead of only to invitees who haven't started yet.
    """
    parts = []

    persona = meeting.persona.strip()
    parts.append(persona or "You are a professional meeting facilitator.")

    if meeting.meeting_context.strip():
        parts.append(f"Why this conversation is happening:\n{meeting.meeting_context.strip()}")

    if meeting.context_sop.strip():
        parts.append(f"Rules and background you must apply:\n{meeting.context_sop.strip()}")

    if meeting.agenda:
        agenda_lines = []
        for item in meeting.agenda:
            note = f" — {item.ai_note.strip()}" if item.ai_note.strip() else ""
            agenda_lines.append(f"- {item.item}{note}")
        parts.append(
            "Work through these agenda items with the invitee, one at a time:\n"
            + "\n".join(agenda_lines)
        )
    else:
        parts.append("There is no fixed agenda — discuss the subject with the invitee.")

    parts.append(
        "Ask about one item at a time and wait for the answer. Be concise and professional. "
        "Never invent facts about the subject: if something isn't in your instructions and the "
        "invitee hasn't told you, say you don't have it."
    )

    allowed = ", ".join(f'"{title}"' for title in meeting.agenda_titles())
    parts.append(
        "Set agenda_tag to the exact title of the agenda item your reply is about. "
        + (f"The titles are: {allowed}. " if allowed else "")
        + f'Use "{OTHER_TAG}" for anything that does not belong to a listed item.'
    )

    return "\n\n".join(parts)


def build_history_block(running_summary: str, recent: list[ChatMessage]) -> str:
    """The conversation so far: the rolling summary, then the recent turns verbatim."""
    parts = []
    if running_summary.strip():
        parts.append(f"Summary of the earlier part of this conversation:\n{running_summary.strip()}")
    rendered = render_turns(recent)
    if rendered:
        parts.append(f"Recent turns:\n{rendered}")
    return "\n\n".join(parts)


def opening_message(
    meeting: Meeting, profile: dict, *, key_path: Path | str | None = None
) -> ChatTurnOutput:
    """The AI's own first message, generated before the invitee has said anything.

    Spec 2.5 asks for this so no creator has to script an intro. It is tagged `OPENING_TAG`
    rather than an agenda item — listing the agenda is not discussing it, and counting it as
    coverage would show every invitee as having covered item one before they arrived.
    """
    result = run_structured(
        profile,
        "Write your opening message to the invitee now. Introduce yourself in character, say "
        "briefly why this conversation is happening, list the agenda items you will go "
        "through, and invite them to begin with the first one.",
        ChatTurnOutput,
        instructions=system_instructions(meeting),
        text_field="reply",
        key_path=key_path,
    )
    result.agenda_tag = OPENING_TAG
    return result


def send_turn(
    meeting: Meeting,
    profile: dict,
    running_summary: str,
    recent: list[ChatMessage],
    user_message: str,
    *,
    key_path: Path | str | None = None,
) -> ChatTurnOutput:
    """One reply to one invitee message.

    Raises:
        LLMConnectionError: if the provider fails. The caller has already saved the
            invitee's own message by then, so a failure here costs the reply and not the
            conversation.
    """
    history = build_history_block(running_summary, recent)
    prompt = f"{history}\n\nInvitee: {user_message.strip()}" if history else f"Invitee: {user_message.strip()}"

    result = run_structured(
        profile,
        prompt,
        ChatTurnOutput,
        instructions=system_instructions(meeting),
        text_field="reply",
        key_path=key_path,
    )
    # Coerced here rather than trusted from the model: the MoM groups by exact tag, so a
    # near-miss like a pluralised title would silently become a third category.
    result.agenda_tag = canonical_tag(result.agenda_tag, meeting)
    return result
