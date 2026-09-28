"""The invitee's private chat (requirement 6.7, spec 2 and 3a).

**Not registered as an `st.Page`.** `streamlit_app.py` calls `render_invitee_page` directly
when the URL carries an invitee link, before the login gate — an invitee has no account, so
this screen has to exist entirely outside `st.navigation`. `checks_view.py` set the
precedent for a page module rendered by call rather than by registration.

Nothing here touches `auth/`. The invitee's unlock lives in its own `invitee_*` session
keys, so `st.session_state["user_id"]` stays empty and nothing that checks it can mistake
an invitee for an employee.

The model used is the *creator's* default profile — see `chat_agent`.

Phase 2 adds the tab strip spec 3a asks for: every discussion item shares one **General
Discussion** tab, and each table item gets its own grid tab. Ten discussion items and two
tables are three tabs, not twelve.
"""

import datetime
import logging

import streamlit as st

from llm.client import LLMConnectionError
from llm.session import default_profile
from meetings import (
    chat_agent,
    db,
    extraction_agent,
    loops,
    running_summary,
    session,
    steps,
    storage,
    summary_agent,
    tables,
)
from meetings.access import verify_code
from meetings.exceptions import MeetingAgentError, MeetingError
from meetings.model import (
    SENDER_AI,
    SENDER_USER,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    AgendaTable,
    Faq,
    Meeting,
    StepAnswer,
)

logger = logging.getLogger(__name__)

DISCUSSION_TAB = "💬 General Discussion"


def render_invitee_page(meeting_id: int, token: str) -> None:
    """The whole invitee experience: resolve the link, take the code, then chat."""
    invitee = _resolve(token)
    if invitee is None:
        return

    try:
        meeting = db.load_meeting_for_invitee(invitee["meeting_id"])
    except MeetingError as error:
        st.error(str(error), icon=":material/error:")
        return

    st.title(meeting.display_subject())

    if not session.is_verified(invitee["meeting_id"], invitee["invitee_id"]):
        _render_code_gate(invitee)
        return

    _render_chat(meeting, invitee)


def _resolve(token: str) -> dict | None:
    """The invitee this token belongs to, or None with a message on screen.

    The `m=` in the URL is deliberately not used to look anything up — the token decides
    which meeting opens, so editing the id by hand changes nothing.
    """
    try:
        invitee = db.resolve_token(token)
    except MeetingError:
        logger.exception("Could not resolve an invitee token.")
        st.error("We couldn't open this invitation. Please try again shortly.", icon=":material/error:")
        return None

    if invitee is None:
        # Deliberately vague: a precise "no such token" would confirm to someone probing
        # links which ones exist.
        st.error("This invitation link isn't valid. Check the link in your email.", icon=":material/link_off:")
        return None

    return invitee


def _render_code_gate(invitee: dict) -> None:
    st.write(f"Hello {invitee['name']} — please enter the access code from your invitation to continue.")

    with st.form("invitee_code_form"):
        entered = st.text_input(
            "Access code",
            key="invitee_code_input",
            help="The code sent alongside this link.",
        )
        submitted = st.form_submit_button("Continue", icon=":material/login:")

    if not submitted:
        return

    try:
        ok, reason = verify_code(invitee, entered)
    except MeetingError as error:
        logger.exception("Verifying an access code failed for invitee %s.", invitee["invitee_id"])
        st.error(str(error), icon=":material/error:")
        return

    if not ok:
        st.error(reason, icon=":material/lock:")
        return

    session.mark_verified(invitee["meeting_id"], invitee["invitee_id"])
    st.rerun()


def _profile_for(meeting: Meeting) -> dict | None:
    """The creator's default model. An invitee has no model choice of their own."""
    if meeting.created_by is None:
        return None
    return default_profile(meeting.created_by)


def _evaluation_fields(meeting_id: int) -> list:
    """The meeting's evaluation questions, or none if the feature is off for it.

    A failure here costs the questions, not the conversation: they are woven into the chat
    as a nicety, and losing them is a far smaller harm than an invitee meeting an error page
    because one extra query failed.
    """
    try:
        return db.list_evaluation_fields(meeting_id)
    except MeetingError:
        logger.exception("Could not read the evaluation fields for meeting %s.", meeting_id)
        return []


def _invitee_lists(meeting: Meeting, invitee: dict) -> loops.InviteeLists:
    """This invitee's For each lists and their rows in them (phase 52).

    A failure here costs the list questions, not the conversation: with no rows they are
    skipped, which is what happens anyway for a list the organiser hasn't attached yet.
    """
    if not steps.loop_names(meeting):
        return loops.InviteeLists()
    try:
        tables = loops.list_tables(meeting, db.list_agenda_tables(invitee["meeting_id"]))
    except MeetingError:
        logger.exception("Could not read the lists for meeting %s.", invitee["meeting_id"])
        return loops.InviteeLists()
    return loops.invitee_lists(tables, invitee)


def _meeting_faq(meeting_id: int) -> Faq | None:
    """The organiser's FAQ (phase 53), or None.

    A failure here costs the FAQ, not the conversation: the bot then answers side questions
    as it did before there was one, and nothing is logged.
    """
    try:
        return db.load_faq(meeting_id)
    except MeetingError:
        logger.exception("Could not read the FAQ for meeting %s.", meeting_id)
        return None


def _log_unanswered(meeting_id: int, invitee_id: int, question: str, agenda_tag: str) -> None:
    """Logs a side question the FAQ couldn't answer. Never costs the invitee their reply."""
    if not question:
        return
    try:
        db.add_faq_miss(meeting_id, invitee_id, question, agenda_tag)
    except MeetingError:
        logger.exception("Could not log an unanswered question for invitee %s.", invitee_id)


def _render_chat(meeting: Meeting, invitee: dict) -> None:
    meeting_id = invitee["meeting_id"]
    invitee_id = invitee["invitee_id"]

    try:
        chat_session = db.ensure_session(meeting_id, invitee_id)
        messages = db.list_messages(meeting_id, invitee_id)
        answers = db.load_step_answers(meeting_id, invitee_id)
    except MeetingError as error:
        logger.exception("Could not load the chat for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    profile = _profile_for(meeting)
    if profile is None:
        st.error(
            "This meeting isn't ready yet — its organiser hasn't finished setting up. "
            "Please try again later.",
            icon=":material/error:",
        )
        return

    fields = _evaluation_fields(meeting_id)
    lists = _invitee_lists(meeting, invitee)

    if not messages and not chat_session.closed:
        _open_conversation(meeting, profile, meeting_id, invitee_id, fields, answers, lists)
        return

    table_items = meeting.table_items()
    if table_items:
        # Spec 3a's tab strip: one shared discussion tab, one tab per grid. Built only when
        # there is a grid, so a meeting without one keeps `st.chat_input` at page level and
        # pinned to the bottom, exactly as it was before this phase.
        tabs = st.tabs([DISCUSSION_TAB, *[f"📋 {item.item}" for item in table_items]])
        with tabs[0]:
            _render_discussion(meeting, profile, invitee, chat_session, messages, fields, answers, lists)
        for tab, item in zip(tabs[1:], table_items):
            with tab:
                _render_table_tab(meeting_id, invitee_id, item, chat_session.closed)
    else:
        _render_discussion(meeting, profile, invitee, chat_session, messages, fields, answers, lists)

    # Below the tabs, because closing and its summary belong to the whole session rather
    # than to whichever tab happens to be open.
    if chat_session.closed:
        _render_closed(meeting_id, invitee_id)
        return

    if st.button(
        "Close chat",
        key="invitee_close_chat",
        icon=":material/task_alt:",
        help="Finish and generate your summary. You won't be able to add anything afterwards.",
    ):
        _handle_close(meeting, profile, meeting_id, invitee_id, fields)


def _render_discussion(
    meeting: Meeting,
    profile: dict,
    invitee: dict,
    chat_session,
    messages: list,
    fields: list,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
) -> None:
    """The conversation itself — every discussion agenda item, as one flowing chat."""
    meeting_id = invitee["meeting_id"]
    invitee_id = invitee["invitee_id"]

    for message in messages:
        with st.chat_message("assistant" if message.is_from_ai() else "user"):
            st.write(message.text)

    if chat_session.closed:
        return

    _render_uploads(meeting_id, invitee_id)

    current = steps.next_question(meeting, answers, lists.rows)
    if current is not None:
        st.caption(f":red[{steps.progress_text(meeting, current, answers, lists.rows)}]")

    prompt = st.chat_input("Type your reply", key="invitee_chat_input")
    if prompt:
        _handle_turn(
            meeting,
            profile,
            meeting_id,
            invitee_id,
            chat_session.running_summary,
            messages,
            prompt,
            fields,
            answers,
            lists,
        )


def _render_table_tab(meeting_id: int, invitee_id: int, item, closed: bool) -> None:
    """One table agenda item's grid: locked columns to read, editable ones to fill.

    Loaded fresh on every run rather than cached in session state — the invitee is expected
    to leave and come back, and what is on the screen has to be what is in the database.
    """
    try:
        table = db.find_agenda_table(meeting_id, item.item)
    except MeetingError as error:
        logger.exception("Could not load the grid for '%s'.", item.item)
        st.error(str(error), icon=":material/error:")
        return

    if table is None:
        st.info(
            "This table isn't ready yet — the organiser hasn't attached its data. "
            "The rest of the meeting is unaffected.",
            icon=":material/info:",
        )
        return

    if item.ai_note.strip():
        st.caption(item.ai_note)

    try:
        responses = db.load_table_responses(table.table_id, invitee_id)
    except MeetingError as error:
        logger.exception("Could not load saved rows for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    st.caption(tables.completion_label(table, len(responses)))

    edited = st.data_editor(
        tables.display_frame(table, responses),
        num_rows="fixed",
        # The whole grid, not just the locked columns, once the chat is closed: a closed
        # session is a locked record, and an editable cell that silently saves nothing would
        # be worse than one that can't be typed in.
        disabled=True if closed else table.locked_columns,
        hide_index=True,
        width="stretch",
        key=f"invitee_table_{table.table_id}",
    )

    if closed:
        return

    if st.button(
        "Save progress",
        key=f"invitee_table_save_{table.table_id}",
        icon=":material/save:",
        help="Store what you've filled in so far. You can come back and finish later.",
    ):
        _handle_save_table(table, invitee_id, edited)


def _handle_save_table(table: AgendaTable, invitee_id: int, edited) -> None:
    """Stores the invitee's rows. Explicit rather than per-cell, as spec 3a asks."""
    try:
        saved = db.save_table_responses(
            table.table_id, invitee_id, tables.responses_from_frame(table, edited)
        )
    except MeetingError as error:
        logger.exception("Could not save table rows for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    st.success(tables.completion_label(table, saved) + " saved.", icon=":material/check_circle:")


def _open_conversation(
    meeting: Meeting,
    profile: dict,
    meeting_id: int,
    invitee_id: int,
    fields: list,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
) -> None:
    """Generates and stores the AI's opening message, then reruns to show it.

    A meeting with question steps opens by asking the first one, so the invitee's first
    reply is already an answer to something the app is tracking.
    """
    first = steps.next_question(meeting, answers, lists.rows)
    guidance = ""
    if first is not None:
        guidance = chat_agent.question_guidance(
            first.question,
            steps.progress_text(meeting, first, answers, lists.rows),
            just_happened="Open with a short welcome in character and say why this conversation is happening.",
            row_context=lists.row_context(first),
        )
    try:
        with st.spinner("Starting the conversation..."):
            opening = chat_agent.opening_message(
                meeting,
                profile,
                evaluation_fields=fields,
                step_guidance=guidance,
                step_tag=first.item if first is not None else "",
                faq=_meeting_faq(meeting_id),
            )
        db.add_message(meeting_id, invitee_id, SENDER_AI, opening.reply, opening.agenda_tag)
    except (LLMConnectionError, MeetingError) as error:
        logger.exception("Could not open the conversation for invitee %s.", invitee_id)
        st.error(f"We couldn't start the conversation: {error}", icon=":material/error:")
        return
    st.rerun()


def _handle_turn(
    meeting: Meeting,
    profile: dict,
    meeting_id: int,
    invitee_id: int,
    summary_so_far: str,
    messages: list,
    prompt: str,
    fields: list,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
) -> None:
    """Saves the invitee's message, replies to it, then folds if the history has grown.

    The invitee's own message is written **first and on its own**, so a provider failure
    costs them the reply and not what they typed. While a question step is open the reply is
    steered by `_advance_question`; a failure while reading the answer uses no try.
    """
    current = steps.next_question(meeting, answers, lists.rows)
    try:
        db.add_message(meeting_id, invitee_id, SENDER_USER, prompt, current.item if current else "")
    except MeetingError as error:
        logger.exception("Could not save an invitee message for %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    # Set once an answer is saved: the plain-words next question, posted if the reply fails,
    # so the chat never moves on to a question the invitee was not asked.
    fallback = ""
    try:
        with st.spinner("Thinking..."):
            guidance, step_tag = "", ""
            if current is not None and not _was_asked(messages, current):
                guidance, step_tag = _ask_new_question(meeting, current, answers, lists), current.item
            elif current is not None:
                guidance, step_tag, fallback = _advance_question(
                    meeting, profile, meeting_id, invitee_id, current, answers, prompt, lists
                )
            turn = chat_agent.send_turn(
                meeting,
                profile,
                summary_so_far,
                running_summary.recent_messages(messages),
                prompt,
                evaluation_fields=fields,
                step_guidance=guidance,
                step_tag=step_tag,
                faq=_meeting_faq(meeting_id),
            )
        db.add_message(meeting_id, invitee_id, SENDER_AI, turn.reply, turn.agenda_tag)
        _log_unanswered(meeting_id, invitee_id, turn.unanswered_question, turn.agenda_tag)
    except (LLMConnectionError, MeetingError) as error:
        logger.exception("Could not generate a reply for invitee %s.", invitee_id)
        if fallback and _post_fallback(meeting_id, invitee_id, fallback, step_tag):
            st.rerun()
            return
        st.error(f"We couldn't get a reply: {error}. Your message was saved — try again.", icon=":material/error:")
        st.rerun()
        return

    # After the writes have committed, never inside them — see `running_summary.maybe_fold`.
    running_summary.maybe_fold(profile, meeting_id, invitee_id)
    st.rerun()


def _advance_question(
    meeting: Meeting,
    profile: dict,
    meeting_id: int,
    invitee_id: int,
    current: steps.Step,
    answers: dict[str, StepAnswer],
    reply: str,
    lists: loops.InviteeLists,
) -> tuple[str, str, str]:
    """Reads, checks and records one reply to the open question (for its list row, if any).

    Returns the step guidance for the chat model, the title its reply is about, and a plain
    reply to post if the model then fails ("" when nothing was saved). The model only
    converts the reply (`read_answer`); `steps.check_answer` decides if it passes.

    Raises:
        LLMConnectionError: if reading the reply fails.
        MeetingError: if the answer can't be saved.
    """
    today = datetime.date.today()
    question = current.question
    reading = chat_agent.read_answer(profile, question, reply, today, row_context=lists.row_context(current))
    if reading.kind == chat_agent.QUESTION_KIND:
        # Asking something is not a wrong answer, so no try is used.
        guidance = chat_agent.question_guidance(
            question,
            steps.progress_text(meeting, current, answers, lists.rows),
            just_happened="The invitee asked something instead of answering. Answer it briefly "
            "from your instructions and the FAQ if there is one (say so if you don't know), then "
            "ask the question again.",
            row_context=lists.row_context(current),
        )
        return guidance, current.item, ""

    check = steps.check_answer(question, reading.value, today)
    updated = steps.record_attempt(question, answers.get(current.key), check, reply, key=current.key)
    db.save_step_answer(meeting_id, invitee_id, updated)

    if updated.status == STEP_ANSWERED:
        happened = f'The answer "{updated.value}" was accepted. Thank them in a few words.'
        plain = f"Thanks, noted: {updated.value}."
    elif updated.status == STEP_NOT_ANSWERED:
        happened = (
            "The invitee could not give a usable answer within the allowed tries. Say politely "
            "that you have noted it for the organiser and are moving on."
        )
        plain = "I've noted that for the organiser and will move on."
    else:
        tries_left = question.max_tries - updated.tries
        happened = (
            f"That answer can't be accepted. {check.reason} Explain this in simple words with a "
            f"small example of a good answer. Tries left: {tries_left}."
        )
        plain = f"{check.reason} Please try again ({tries_left} tries left)."

    after = {**answers, current.key: updated}
    upcoming = steps.next_question(meeting, after, lists.rows)
    if upcoming is None:
        return (
            chat_agent.finished_questions_guidance(meeting, just_happened=happened),
            current.item,
            f"{plain} That's all my set questions.",
        )
    if upcoming.key != current.key:
        plain += f" Next: {_plain_question(upcoming, lists)}"
    return (
        chat_agent.question_guidance(
            upcoming.question,
            steps.progress_text(meeting, upcoming, after, lists.rows),
            just_happened=happened,
            row_context=lists.row_context(upcoming),
        ),
        upcoming.item,
        plain,
    )


def _plain_question(step: steps.Step, lists: loops.InviteeLists) -> str:
    """The question in plain words, for when the model can't word it."""
    text = f"{step.item} — {steps.describe_rule(step.question)}."
    row = lists.row_context(step)
    return f"{text} ({row})" if row else text


def _was_asked(messages: list, current: steps.Step) -> bool:
    """Whether the bot's last message was about this question.

    A question the organiser added (or a list they uploaded) after the chat started is open
    on the path but was never put to the invitee; their next message is not an answer to it.
    """
    last_ai = next((message for message in reversed(messages) if message.is_from_ai()), None)
    return last_ai is not None and last_ai.agenda_tag == current.item


def _ask_new_question(meeting: Meeting, current: steps.Step, answers: dict, lists: loops.InviteeLists) -> str:
    return chat_agent.question_guidance(
        current.question,
        steps.progress_text(meeting, current, answers, lists.rows),
        just_happened="Reply briefly to what the invitee just said, then ask this question. It was "
        "added after the conversation started, so their message was not an answer to it.",
        row_context=lists.row_context(current),
    )


def _post_fallback(meeting_id: int, invitee_id: int, text: str, tag: str) -> bool:
    """Posts the plain reply when the model failed after an answer was saved. False if even
    that can't be stored, so the caller shows the usual error."""
    try:
        db.add_message(meeting_id, invitee_id, SENDER_AI, text, tag)
    except MeetingError:
        logger.exception("Could not post the plain reply for invitee %s.", invitee_id)
        return False
    return True


def _handle_close(
    meeting: Meeting, profile: dict, meeting_id: int, invitee_id: int, fields: list
) -> None:
    """Generates the final MoM from the **full** transcript and locks the session.

    The message list is re-read here rather than reused from the render above: it is the
    source of truth for the permanent record, and the rolling summary is deliberately not
    involved at any point.
    """
    try:
        with st.spinner("Preparing your summary..."):
            full_history = db.list_messages(meeting_id, invitee_id)
            progress = db.table_progress(meeting_id, invitee_id)
            summary = summary_agent.generate_summary(
                meeting, profile, full_history, table_progress=progress
            )
        db.close_session(meeting_id, invitee_id, summary_agent.to_json(summary))
    except (MeetingAgentError, MeetingError) as error:
        logger.exception("Could not close the chat for invitee %s.", invitee_id)
        st.error(f"{error} Your conversation is safe — try closing again shortly.", icon=":material/error:")
        return

    _extract_evaluations(meeting, profile, invitee_id, fields, full_history)
    st.rerun()


def _extract_evaluations(
    meeting: Meeting, profile: dict, invitee_id: int, fields: list, full_history: list
) -> None:
    """Pulls the evaluation answers out of the conversation, after it has already closed.

    Deliberately runs **after** `close_session` and swallows its own failures. The MoM is
    the record the invitee is owed; the evaluation answers are a convenience for the
    creator, who can re-extract them from their own page. Ordering it this way means a
    provider failure here can never cost somebody their closing summary.
    """
    if not fields:
        return
    try:
        answers = extraction_agent.extract_answers(meeting, profile, fields, full_history)
        db.save_evaluation_answers(invitee_id, answers)
    except (MeetingAgentError, MeetingError):
        logger.exception("Could not extract evaluation answers for invitee %s.", invitee_id)


def _render_uploads(meeting_id: int, invitee_id: int) -> None:
    with st.expander("Attach a file"):
        uploaded = st.file_uploader(
            "Upload",
            accept_multiple_files=True,
            key="invitee_upload",
            help="Stored privately against your own conversation.",
        )
        if uploaded and st.button("Save files", key="invitee_upload_save", icon=":material/upload:"):
            try:
                for upload in uploaded:
                    path = storage.save_upload(upload.getvalue(), upload.name, meeting_id, invitee_id)
                    db.add_file(meeting_id, upload.name, str(path), invitee_id)
            except MeetingError as error:
                st.error(str(error), icon=":material/error:")
                return
            st.rerun()

        try:
            for record in db.list_files(meeting_id, invitee_id):
                st.caption(record["filename"])
        except MeetingError:
            logger.exception("Could not list uploads for invitee %s.", invitee_id)


def _render_closed(meeting_id: int, invitee_id: int) -> None:
    st.success("This conversation is complete. Thank you.", icon=":material/check_circle:")

    try:
        stored = db.get_session_summary(meeting_id, invitee_id)
    except MeetingError:
        logger.exception("Could not read the stored summary for invitee %s.", invitee_id)
        return

    summary = summary_agent.from_json(stored)
    if summary is None:
        return

    with st.expander("Your summary", expanded=True):
        for entry in summary.agenda_items:
            st.markdown(f"**{entry.item}**")
            st.write(entry.notes or "_Not discussed._")
        if summary.other_extra.strip():
            st.markdown("**Other / Extra**")
            st.write(summary.other_extra)
        if summary.closing_message.strip():
            st.caption(summary.closing_message)
