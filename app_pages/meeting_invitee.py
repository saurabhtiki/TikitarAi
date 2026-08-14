"""The invitee's private chat (requirement 6.7, Phase 1, spec 2).

**Not registered as an `st.Page`.** `streamlit_app.py` calls `render_invitee_page` directly
when the URL carries an invitee link, before the login gate — an invitee has no account, so
this screen has to exist entirely outside `st.navigation`. `checks_view.py` set the
precedent for a page module rendered by call rather than by registration.

Nothing here touches `auth/`. The invitee's unlock lives in its own `invitee_*` session
keys, so `st.session_state["user_id"]` stays empty and nothing that checks it can mistake
an invitee for an employee.

The model used is the *creator's* default profile — see `chat_agent`.
"""

import logging

import streamlit as st

from llm.client import LLMConnectionError
from llm.session import default_profile
from meetings import chat_agent, db, running_summary, session, storage, summary_agent
from meetings.access import verify_code
from meetings.exceptions import MeetingAgentError, MeetingError
from meetings.model import SENDER_AI, SENDER_USER, Meeting

logger = logging.getLogger(__name__)


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

    if not messages and not chat_session.closed:
        _open_conversation(meeting, profile, meeting_id, invitee_id)
        return

    for message in messages:
        with st.chat_message("assistant" if message.is_from_ai() else "user"):
            st.write(message.text)

    if chat_session.closed:
        _render_closed(meeting_id, invitee_id)
        return

    _render_uploads(meeting_id, invitee_id)

    prompt = st.chat_input("Type your reply", key="invitee_chat_input")
    if prompt:
        _handle_turn(meeting, profile, meeting_id, invitee_id, chat_session.running_summary, messages, prompt)

    if st.button(
        "Close chat",
        key="invitee_close_chat",
        icon=":material/task_alt:",
        help="Finish and generate your summary. You won't be able to add anything afterwards.",
    ):
        _handle_close(meeting, profile, meeting_id, invitee_id)


def _open_conversation(meeting: Meeting, profile: dict, meeting_id: int, invitee_id: int) -> None:
    """Generates and stores the AI's opening message, then reruns to show it."""
    try:
        with st.spinner("Starting the conversation..."):
            opening = chat_agent.opening_message(meeting, profile)
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
                meeting, profile, summary_so_far, running_summary.recent_messages(messages), prompt
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


def _handle_close(meeting: Meeting, profile: dict, meeting_id: int, invitee_id: int) -> None:
    """Generates the final MoM from the **full** transcript and locks the session.

    The message list is re-read here rather than reused from the render above: it is the
    source of truth for the permanent record, and the rolling summary is deliberately not
    involved at any point.
    """
    try:
        with st.spinner("Preparing your summary..."):
            full_history = db.list_messages(meeting_id, invitee_id)
            summary = summary_agent.generate_summary(meeting, profile, full_history)
        db.close_session(meeting_id, invitee_id, summary_agent.to_json(summary))
    except (MeetingAgentError, MeetingError) as error:
        logger.exception("Could not close the chat for invitee %s.", invitee_id)
        st.error(f"{error} Your conversation is safe — try closing again shortly.", icon=":material/error:")
        return
    st.rerun()


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
