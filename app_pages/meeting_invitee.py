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

import logging

import streamlit as st

from llm.client import LLMConnectionError
from llm.session import default_profile
from meetings import (
    chat_agent,
    db,
    extraction_agent,
    running_summary,
    session,
    storage,
    summary_agent,
    tables,
)
from meetings.access import verify_code
from meetings.exceptions import MeetingAgentError, MeetingError
from meetings.model import SENDER_AI, SENDER_USER, AgendaTable, Meeting

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


def _render_chat(meeting: Meeting, invitee: dict) -> None:
    meeting_id = invitee["meeting_id"]
    invitee_id = invitee["invitee_id"]

    try:
        chat_session = db.ensure_session(meeting_id, invitee_id)
        messages = db.list_messages(meeting_id, invitee_id)
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

    if not messages and not chat_session.closed:
        _open_conversation(meeting, profile, meeting_id, invitee_id, fields)
        return

    table_items = meeting.table_items()
    if table_items:
        # Spec 3a's tab strip: one shared discussion tab, one tab per grid. Built only when
        # there is a grid, so a meeting without one keeps `st.chat_input` at page level and
        # pinned to the bottom, exactly as it was before this phase.
        tabs = st.tabs([DISCUSSION_TAB, *[f"📋 {item.item}" for item in table_items]])
        with tabs[0]:
            _render_discussion(meeting, profile, invitee, chat_session, messages, fields)
        for tab, item in zip(tabs[1:], table_items):
            with tab:
                _render_table_tab(meeting_id, invitee_id, item, chat_session.closed)
    else:
        _render_discussion(meeting, profile, invitee, chat_session, messages, fields)

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

    prompt = st.chat_input("Type your reply", key="invitee_chat_input")
    if prompt:
        _handle_turn(
            meeting, profile, meeting_id, invitee_id, chat_session.running_summary, messages, prompt, fields
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
    meeting: Meeting, profile: dict, meeting_id: int, invitee_id: int, fields: list
) -> None:
    """Generates and stores the AI's opening message, then reruns to show it."""
    try:
        with st.spinner("Starting the conversation..."):
            opening = chat_agent.opening_message(meeting, profile, evaluation_fields=fields)
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
) -> None:
    """Saves the invitee's message, replies to it, then folds if the history has grown.

    The invitee's own message is written **first and on its own**, so a provider failure
    costs them the reply and not what they typed.
    """
    try:
        db.add_message(meeting_id, invitee_id, SENDER_USER, prompt)
    except MeetingError as error:
        logger.exception("Could not save an invitee message for %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    try:
        with st.spinner("Thinking..."):
            turn = chat_agent.send_turn(
                meeting,
                profile,
                summary_so_far,
                running_summary.recent_messages(messages),
                prompt,
                evaluation_fields=fields,
            )
        db.add_message(meeting_id, invitee_id, SENDER_AI, turn.reply, turn.agenda_tag)
    except (LLMConnectionError, MeetingError) as error:
        logger.exception("Could not generate a reply for invitee %s.", invitee_id)
        st.error(f"We couldn't get a reply: {error}. Your message was saved — try again.", icon=":material/error:")
        st.rerun()
        return

    # After the writes have committed, never inside them — see `running_summary.maybe_fold`.
    running_summary.maybe_fold(profile, meeting_id, invitee_id)
    st.rerun()


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
