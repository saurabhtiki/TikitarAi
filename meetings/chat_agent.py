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

import datetime
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from llm.client import run_structured
from meetings.model import (
    ANSWER_DATE,
    ANSWER_NUMBER,
    OPENING_TAG,
    OTHER_TAG,
    AgendaItem,
    ChatMessage,
    EvaluationField,
    Faq,
    Meeting,
    canonical_tag,
)
from meetings.faq import faq_block
from meetings.running_summary import render_turns
from meetings.steps import describe_rule

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
    unanswered_question: str = Field(
        # Phase 53. Only read when the meeting has a FAQ; see `send_turn`.
        default="",
        description="If the invitee asked a question the FAQ does not answer, their question "
        "in a few words; otherwise empty.",
    )


ANSWER_KIND = "answer"
QUESTION_KIND = "question"


class AnswerReading(BaseModel):
    """What the invitee's message was, and their answer rewritten in the expected form."""

    kind: str = Field(
        default=ANSWER_KIND,
        description="'answer' if the message tries to answer the question, 'question' if the "
        "invitee is asking something instead.",
    )
    value: str = Field(
        default="",
        description="The answer in the expected form, or empty if there is no usable answer.",
    )


def read_answer(
    profile: dict,
    item: AgendaItem,
    message: str,
    today: datetime.date,
    *,
    row_context: str = "",
    key_path: Path | str | None = None,
) -> AnswerReading:
    """Converts the invitee's reply to one question into the form its rule is checked in.

    The model only *reads* here — "45k" becomes "45000", "next Monday" becomes a date. It
    never decides whether the answer is good enough: `steps.check_answer` does that, so a
    model that is too generous cannot move the meeting on.

    Raises:
        LLMConnectionError: if the provider fails.
    """
    form_hint = {
        ANSWER_DATE: "Write a date as dd-mm-yyyy.",
        ANSWER_NUMBER: "Write a number as plain digits with no commas or currency (45k is 45000).",
    }.get(item.answer_type, "")
    prompt = (
        f"Today is {today.strftime('%d-%m-%Y')} ({today.strftime('%A')}).\n"
        f"The question was: {item.item}\n"
        f"The expected answer is {describe_rule(item)}.\n"
        f"Background: {item.ai_note.strip() or 'none'}\n"
        + (f"The question is about this row of the list '{item.loop}': {row_context}\n" if row_context else "")
        + f"The invitee replied: {message.strip()}\n\n"
        "Decide whether the reply attempts to answer the question (kind 'answer') or asks "
        "something else instead (kind 'question'). For an answer, rewrite it in the expected "
        f"form. {form_hint} For yes or no, write Yes or No. For a choice, write the option "
        "exactly as listed. For text, copy the answer. Do not judge whether the answer is "
        "acceptable and do not invent one: if the reply gives no usable answer, leave value empty."
    )
    result = run_structured(profile, prompt, AnswerReading, key_path=key_path)
    result.kind = QUESTION_KIND if str(result.kind).strip().lower() == QUESTION_KIND else ANSWER_KIND
    return result


def question_guidance(
    item: AgendaItem, progress: str, *, just_happened: str = "", row_context: str = ""
) -> str:
    """The step instruction for the chat model: what just happened, and what to ask now.

    `row_context` is the list row a For each question is about (phase 52); the model names
    the row itself, so the organiser never has to write placeholders into the question.
    """
    parts = []
    if just_happened:
        parts.append(just_happened)
    note = f" Background for this question: {item.ai_note.strip()}" if item.ai_note.strip() else ""
    row = (
        f" It is about this row of the list '{item.loop}': {row_context}. Name the row in a few "
        "words (for example its number or name) so the invitee knows which one you mean."
        if row_context
        else ""
    )
    parts.append(
        f"Now ask this question ({progress}): \"{item.item}\". The answer must be "
        f"{describe_rule(item)}.{note}{row} Ask only this question and nothing else."
    )
    return " ".join(parts)


def finished_questions_guidance(meeting: Meeting, *, just_happened: str = "") -> str:
    """The step instruction once every question is answered or given up on."""
    parts = [just_happened] if just_happened else []
    if any(not item.is_question() and not item.is_table() for item in meeting.agenda):
        parts.append("All the set questions are done. Now move on to the discussion agenda items.")
    else:
        parts.append(
            "All the set questions are done. Thank the invitee and ask them to press the "
            "Close chat button to finish."
        )
    return " ".join(parts)


def system_instructions(
    meeting: Meeting,
    evaluation_fields: list[EvaluationField] | None = None,
    step_guidance: str = "",
    faq: Faq | None = None,
) -> str:
    """The persona, the context, the SOP and the agenda, as one instruction block.

    Assembled per turn from the meeting as it stands rather than cached, so a creator who
    fixes a wrong figure in an agenda item's note has it apply to the very next message
    instead of only to invitees who haven't started yet.

    Table agenda items (spec 3a) are described but **not** asked about: they are filled in on
    their own tab, and an agent that started reading out bill numbers would be asking the
    invitee to do in prose the thing the grid exists to spare them.
    """
    parts = []

    persona = meeting.persona.strip()
    parts.append(persona or "You are a professional meeting facilitator.")

    if meeting.meeting_context.strip():
        parts.append(f"Why this conversation is happening:\n{meeting.meeting_context.strip()}")

    if meeting.context_sop.strip():
        parts.append(f"Rules and background you must apply:\n{meeting.context_sop.strip()}")

    discussion_items = [item for item in meeting.discussion_items() if not item.is_question()]
    if meeting.question_items():
        parts.append(
            "This meeting has set questions that the app asks one at a time, in order. Follow "
            "the CURRENT STEP instruction below exactly: never skip a question, never accept "
            "an answer on your own, and never ask two questions at once."
        )
    if discussion_items:
        agenda_lines = []
        for item in discussion_items:
            note = f" — {item.ai_note.strip()}" if item.ai_note.strip() else ""
            agenda_lines.append(f"- {item.item}{note}")
        parts.append(
            "Work through these agenda items with the invitee, one at a time:\n"
            + "\n".join(agenda_lines)
        )
    else:
        parts.append("There is no fixed discussion agenda — discuss the subject with the invitee.")

    table_items = meeting.table_items()
    if table_items:
        table_lines = []
        for item in table_items:
            note = f" — {item.ai_note.strip()}" if item.ai_note.strip() else ""
            table_lines.append(f"- {item.item}{note}")
        parts.append(
            "These items are filled in on their own tabs as tables, not in this chat:\n"
            + "\n".join(table_lines)
            + "\nMention them once so the invitee knows to open those tabs, and answer "
            "questions about them, but never ask for their contents row by row here."
        )

    if evaluation_fields:
        question_lines = [f"- {field_spec.question}" for field_spec in evaluation_fields]
        parts.append(
            "Somewhere in the conversation you also need short answers to these questions. "
            "Work them in naturally, one at a time, as part of the discussion — do not read "
            "them out as a form or a checklist:\n" + "\n".join(question_lines)
        )

    faq_text = faq_block(faq)
    if faq_text:
        # Phase 53: the organiser's answers are the only ones the bot may give, and a
        # question they don't cover is logged (via `unanswered_question`) rather than guessed.
        parts.append(
            "FAQ — the organiser's answers to questions invitees often ask:\n"
            + faq_text
            + "\nWhen the invitee asks you a question of their own, answer it only from this FAQ, "
            "in your own words and briefly (you may still explain what one of your own questions "
            "means). If the FAQ does not answer it, do not guess: say you don't have that answer, "
            "that you have noted it for the organiser who will get back to them, and set "
            "unanswered_question to their question. Leave unanswered_question empty otherwise. "
            "Then carry on with the conversation where it was."
        )

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

    # Last, so it is the freshest instruction the model reads.
    if step_guidance.strip():
        parts.append(f"CURRENT STEP: {step_guidance.strip()}")

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
    meeting: Meeting,
    profile: dict,
    *,
    evaluation_fields: list[EvaluationField] | None = None,
    step_guidance: str = "",
    step_tag: str = "",
    faq: Faq | None = None,
    key_path: Path | str | None = None,
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
        instructions=system_instructions(meeting, evaluation_fields, step_guidance, faq),
        text_field="reply",
        key_path=key_path,
    )
    # A meeting that opens on a question is already discussing it, unlike an agenda list.
    result.agenda_tag = step_tag or OPENING_TAG
    result.unanswered_question = ""
    return result


def send_turn(
    meeting: Meeting,
    profile: dict,
    running_summary: str,
    recent: list[ChatMessage],
    user_message: str,
    *,
    evaluation_fields: list[EvaluationField] | None = None,
    step_guidance: str = "",
    step_tag: str = "",
    faq: Faq | None = None,
    key_path: Path | str | None = None,
) -> ChatTurnOutput:
    """One reply to one invitee message.

    `step_guidance` and `step_tag` come from the question steps (phase 50): the reply is
    told what to ask, and is tagged with the question it was about rather than the model's
    own guess. With a `faq` (phase 53) the reply may carry `unanswered_question`; without
    one it is always empty, so a meeting with no FAQ logs nothing.

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
        instructions=system_instructions(meeting, evaluation_fields, step_guidance, faq),
        text_field="reply",
        key_path=key_path,
    )
    result.unanswered_question = " ".join(str(result.unanswered_question or "").split()) if faq_block(faq) else ""
    if step_tag:
        result.agenda_tag = step_tag
        return result
    # Coerced here rather than trusted from the model: the MoM groups by exact tag, so a
    # near-miss like a pluralised title would silently become a third category.
    result.agenda_tag = canonical_tag(result.agenda_tag, meeting)
    return result
