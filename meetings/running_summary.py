"""Keeping a long chat inside the model's context window (requirement 6.7, spec 3).

The database keeps every message forever. What the *live chat agent* is handed each turn is
the last `RECENT_TURNS` turns plus a rolling summary of everything older — so a
forty-message conversation still costs one turn's worth of context.

The one rule that matters here: **this summary is never an input to the MoM.** A rolling
summary is lossy and compounds — summarising a summary six times over will quietly drop the
detail that mattered — which is fine for keeping a conversation coherent and completely
unacceptable for a permanent record. `summary_agent` re-reads the full transcript from the
database instead, and takes a message list rather than a session so it has no way to reach
this field even by accident.

Nothing here imports Streamlit, so the fold is testable with a stubbed `run_structured`.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from llm.client import LLMConnectionError, run_structured
from meetings.db import ensure_session, list_messages, update_running_summary
from meetings.model import ChatMessage

logger = logging.getLogger(__name__)

# Spec 3: "last 10 turns + running_summary". A turn is one invitee message and one AI
# reply, so the message-level window is twice this.
RECENT_TURNS = 10
RECENT_MESSAGES = RECENT_TURNS * 2

_FOLD_INSTRUCTIONS = (
    "You maintain a running summary of an ongoing conversation. "
    "Rewrite the existing summary so it also covers the new turns, and return the whole "
    "updated summary — not a description of what changed. "
    "Keep every commitment, figure, date and decision the participants stated, quoting "
    "figures exactly. Drop pleasantries and repetition. Aim for under 200 words."
)


class FoldedSummary(BaseModel):
    """What the folding call returns: the summary, rewritten to include the older turns."""

    summary: str = Field(description="The complete updated running summary.")


def recent_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """The tail of the conversation the agent sees verbatim."""
    return messages[-RECENT_MESSAGES:]


def render_turns(messages: list[ChatMessage]) -> str:
    """Messages as labelled lines, for either the fold prompt or the agent's history block."""
    return "\n".join(
        f"{'AI' if message.is_from_ai() else 'Invitee'}: {message.text.strip()}"
        for message in messages
        if message.text.strip()
    )


def pending_fold(messages: list[ChatMessage], folded_through_message_id: int) -> list[ChatMessage]:
    """The messages that have aged out of the verbatim window but aren't summarised yet.

    Everything outside the last `RECENT_MESSAGES`, minus whatever a previous fold already
    covered. Returning a list (rather than a bool plus a slice) keeps the "should we fold"
    question and the "fold what" question as one decision, which is what stops the cutoff
    and the summary from ever describing different sets of turns.
    """
    if len(messages) <= RECENT_MESSAGES:
        return []
    aged_out = messages[:-RECENT_MESSAGES]
    return [message for message in aged_out if (message.message_id or 0) > folded_through_message_id]


def maybe_fold(
    profile: dict,
    meeting_id: int,
    invitee_id: int,
    *,
    key_path: Path | str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Folds the oldest unsummarised turns into the session's running summary, if any.

    Called after each AI reply is saved, and **after** that write has committed rather than
    inside its transaction. The fold is derived from messages that are already durable, so
    retrying it later costs nothing — whereas sharing a transaction would let a provider
    timeout roll back the invitee's message, which is the one thing that must not happen.

    Returns:
        True if a fold happened. False means there was nothing to fold, or the attempt
        failed — in both cases the caller carries on, because a stale running summary costs
        the agent some context and costs the invitee nothing.

    Never raises: this runs on the ordinary chat path, where any exception would surface as
    a failed message the invitee has no way to interpret or retry.
    """
    kwargs = {"db_path": db_path} if db_path is not None else {}

    try:
        session = ensure_session(meeting_id, invitee_id, **kwargs)
        messages = list_messages(meeting_id, invitee_id, **kwargs)
    except Exception:  # noqa: BLE001 — a fold must never be able to break the chat
        logger.exception("Could not read the session to fold for meeting %s invitee %s.", meeting_id, invitee_id)
        return False

    to_fold = pending_fold(messages, session.folded_through_message_id)
    if not to_fold:
        return False

    prompt = (
        f"Existing summary so far:\n{session.running_summary or '(nothing summarised yet)'}\n\n"
        f"New turns to fold in:\n{render_turns(to_fold)}"
    )

    try:
        result = run_structured(
            profile,
            prompt,
            FoldedSummary,
            instructions=_FOLD_INSTRUCTIONS,
            text_field="summary",
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Folding the running summary for meeting %s failed: %s", meeting_id, error)
        return False

    summary = str(result.summary or "").strip()
    if not summary:
        logger.warning("The folding call returned an empty summary; keeping the previous one.")
        return False

    cutoff = max((message.message_id or 0) for message in to_fold)

    try:
        update_running_summary(meeting_id, invitee_id, summary, cutoff, **kwargs)
    except Exception:  # noqa: BLE001 — see above
        logger.exception("Could not store the folded summary for meeting %s.", meeting_id)
        return False

    logger.info("Folded %d message(s) into the running summary for meeting %s.", len(to_fold), meeting_id)
    return True
