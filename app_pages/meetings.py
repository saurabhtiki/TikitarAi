"""Creating meetings and following what invitees said (requirement 6.7, Phase 1, spec 1 & 8).

Two views in one page — a list and a detail — switched by `meetings.session.open_meeting_id`
rather than by separate `st.Page` entries, because "which meeting" is a selection inside one
workflow, not a different place in the app.

Phase 1 covers discussion agenda items only. There is no table-item editor, no evaluation
fields and no cross-invitee comparison matrix here yet.
"""

import logging
import os

import pandas as pd
import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from llm.session import default_profile
from meetings import access, db, session, storage, summary_agent
from meetings.exceptions import MeetingAgentError, MeetingError
from meetings.model import AgendaItem, Meeting
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

# Where an invitee link points. An env var rather than `st.secrets`, which raises outright
# when no secrets file exists — and a missing base URL should give a working local link, not
# take the page down.
BASE_URL = os.environ.get("TIKITARAI_BASE_URL", "http://localhost:8501").rstrip("/")

AGENDA_ROWS_KEY = "meetings_agenda_rows"
INVITEE_ROWS_KEY = "meetings_invitee_rows"


def _current_user_id() -> int | None:
    return st.session_state.get("user_id")


def _render_profile_sidebar() -> None:
    user_id = _current_user_id()
    if user_id is None:
        return
    try:
        profile = get_user_by_id(user_id)
    except AuthDatabaseError:
        logger.exception("Could not load the profile for user %s.", user_id)
        return
    if profile is not None:
        render_sidebar(profile)


# --------------------------------------------------------------------------------------
# Creating a meeting
# --------------------------------------------------------------------------------------


def _agenda_rows() -> pd.DataFrame:
    """The agenda editor's backing frame, seeded with one blank row."""
    if AGENDA_ROWS_KEY not in st.session_state:
        st.session_state[AGENDA_ROWS_KEY] = pd.DataFrame(
            [{"Agenda item": "", "AI note": ""}]
        )
    return st.session_state[AGENDA_ROWS_KEY]


def _invitee_rows() -> pd.DataFrame:
    if INVITEE_ROWS_KEY not in st.session_state:
        st.session_state[INVITEE_ROWS_KEY] = pd.DataFrame([{"Name": "", "Email": ""}])
    return st.session_state[INVITEE_ROWS_KEY]


def _clear_creation_rows() -> None:
    st.session_state.pop(AGENDA_ROWS_KEY, None)
    st.session_state.pop(INVITEE_ROWS_KEY, None)


@st.dialog("New meeting", width="large")
def _open_new_meeting_dialog(user_id: int) -> None:
    try:
        default_persona = db.get_default_persona(user_id)
    except MeetingError:
        logger.exception("Could not read the default persona for user %s.", user_id)
        default_persona = ""

    subject = st.text_input(
        "Subject",
        key="meetings_new_subject",
        help="What this conversation is about, e.g. 'PO No 123'.",
    )
    meeting_context = st.text_area(
        "Meeting context",
        key="meetings_new_context",
        help="Two to four lines on why this conversation is happening. The AI is told this.",
    )
    persona = st.text_area(
        "Persona",
        value=default_persona,
        key="meetings_new_persona",
        help="Who the AI should be, e.g. 'You are the Purchase Manager...'. Saved with this meeting.",
    )
    context_sop = st.text_area(
        "Context / SOP",
        key="meetings_new_sop",
        help="Rules and background the AI must apply throughout the conversation.",
    )

    st.caption("Agenda — one discussion item per row. The AI note is the ground truth for that item.")
    agenda_frame = st.data_editor(
        _agenda_rows(),
        num_rows="dynamic",
        width="stretch",
        key="meetings_new_agenda",
        help="Add a row per item. Leave a row blank to skip it.",
    )

    st.caption("Invitees — each gets their own private link and access code.")
    invitee_frame = st.data_editor(
        _invitee_rows(),
        num_rows="dynamic",
        width="stretch",
        key="meetings_new_invitees",
        help="Names and emails. Existing contacts are remembered for next time.",
    )

    save_default = st.checkbox(
        "Save this persona as my default",
        key="meetings_new_save_default",
        help="Pre-fills the persona on every meeting you create from now on.",
    )

    if st.button("Create meeting", key="meetings_new_save", icon=":material/save:", type="primary"):
        _handle_create_meeting(
            user_id, subject, meeting_context, persona, context_sop, agenda_frame, invitee_frame, save_default
        )


def _agenda_from_frame(frame: pd.DataFrame) -> list[AgendaItem]:
    items = []
    for _, row in frame.iterrows():
        title = str(row.get("Agenda item") or "").strip()
        if not title:
            continue
        items.append(AgendaItem(item=title, ai_note=str(row.get("AI note") or "").strip()))
    return items


def _invitees_from_frame(frame: pd.DataFrame) -> list[tuple[str, str]]:
    people = []
    for _, row in frame.iterrows():
        email = str(row.get("Email") or "").strip()
        if not email:
            continue
        people.append((str(row.get("Name") or "").strip() or email, email))
    return people


def _handle_create_meeting(
    user_id: int,
    subject: str,
    meeting_context: str,
    persona: str,
    context_sop: str,
    agenda_frame: pd.DataFrame,
    invitee_frame: pd.DataFrame,
    save_default: bool,
) -> None:
    agenda = _agenda_from_frame(agenda_frame)
    people = _invitees_from_frame(invitee_frame)

    if not subject.strip():
        st.warning("Give this meeting a subject.", icon=":material/error:")
        return
    if not people:
        st.warning("Add at least one invitee — a meeting with nobody to talk to can't start.", icon=":material/error:")
        return

    meeting = Meeting(
        subject=subject,
        meeting_context=meeting_context,
        persona=persona,
        context_sop=context_sop,
        agenda=agenda,
    )

    try:
        saved = db.create_meeting(user_id, meeting)
        for name, email in people:
            code = access.generate_access_code()
            db.add_invitee(
                saved.meeting_id, user_id, name, email, access.generate_token(), access.encrypt_code(code)
            )
            db.remember_contact(email, name, user_id)
        if save_default:
            db.set_default_persona(user_id, persona)
    except MeetingError as error:
        logger.exception("Could not create a meeting for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return

    _clear_creation_rows()
    session.open_meeting(saved.meeting_id)
    session.flash(f"Created '{saved.display_subject()}'. Share the links below with your invitees.")
    st.rerun()


# --------------------------------------------------------------------------------------
# The meetings list
# --------------------------------------------------------------------------------------


def _render_list(user_id: int) -> None:
    st.subheader("My meetings")

    if st.button("New meeting", key="meetings_new_button", icon=":material/add:", type="primary",
                 help="Set up a subject, persona, agenda and invitees."):
        _open_new_meeting_dialog(user_id)

    try:
        rows = db.list_meetings(user_id)
    except MeetingError as error:
        logger.exception("Could not list meetings for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return

    if not rows:
        st.info("You haven't created any meetings yet.", icon=":material/info:")
        return

    for row in rows:
        with st.container(border=True):
            left, right = st.columns([4, 1], vertical_alignment="center")
            with left:
                st.markdown(f"**{row['subject']}**")
                st.caption(
                    f"Created {row['created_at']} · "
                    f"{row['closed_count']} of {row['invitee_count']} invitee(s) finished"
                )
            with right:
                if st.button(
                    "Open",
                    key=f"meetings_open_{row['meeting_id']}",
                    icon=":material/arrow_forward:",
                    help="View this meeting's invitees, chats and summaries.",
                ):
                    session.open_meeting(row["meeting_id"])
                    st.rerun()


# --------------------------------------------------------------------------------------
# Meeting detail
# --------------------------------------------------------------------------------------


def _render_overview(meeting: Meeting, user_id: int) -> None:
    st.markdown("**Meeting context**")
    st.write(meeting.meeting_context or "_None given._")

    st.markdown("**Persona**")
    st.write(meeting.persona or "_None given._")

    st.markdown("**Context / SOP**")
    st.write(meeting.context_sop or "_None given._")

    st.markdown("**Agenda**")
    if meeting.agenda:
        for item in meeting.agenda:
            st.markdown(f"- **{item.item}**" + (f" — {item.ai_note}" if item.ai_note else ""))
    else:
        st.write("_No agenda items._")

    st.markdown("**Reference documents**")
    uploaded = st.file_uploader(
        "Add reference documents",
        accept_multiple_files=True,
        key=f"meetings_refdocs_{meeting.meeting_id}",
        help="Shared with every invitee, read-only. Stored for reference, not searched by the AI.",
    )
    if uploaded and st.button(
        "Save documents",
        key=f"meetings_refdocs_save_{meeting.meeting_id}",
        icon=":material/upload:",
        help="Store these against the meeting.",
    ):
        _handle_ref_upload(meeting.meeting_id, uploaded)

    try:
        files = db.list_files(meeting.meeting_id)
    except MeetingError:
        logger.exception("Could not list reference documents for meeting %s.", meeting.meeting_id)
        files = []

    for record in files:
        try:
            payload = storage.read_file(record["filepath"])
        except MeetingError:
            st.caption(f"{record['filename']} — no longer on disk")
            continue
        st.download_button(
            record["filename"],
            data=payload,
            file_name=record["filename"],
            key=f"meetings_refdoc_dl_{record['file_id']}",
            help="Download this reference document.",
        )


def _handle_ref_upload(meeting_id: int, uploaded) -> None:
    try:
        for upload in uploaded:
            path = storage.save_upload(upload.getvalue(), upload.name, meeting_id)
            db.add_file(meeting_id, upload.name, str(path))
    except MeetingError as error:
        logger.exception("Could not store reference documents for meeting %s.", meeting_id)
        st.error(str(error), icon=":material/error:")
        return
    session.flash("Reference documents saved.")
    st.rerun()


def _render_invitee_status(meeting: Meeting, user_id: int, invitees: list[dict]) -> None:
    if not invitees:
        st.info("This meeting has no invitees.", icon=":material/info:")
        return

    for invitee in invitees:
        with st.container(border=True):
            joined = invitee["last_active_at"] is not None
            closed = bool(invitee["closed"])
            status = "Finished" if closed else ("In progress" if joined else "Not started")

            try:
                messages = db.list_messages_for_creator(meeting.meeting_id, invitee["invitee_id"], user_id)
            except MeetingError:
                logger.exception("Could not read messages for invitee %s.", invitee["invitee_id"])
                messages = []

            covered = summary_agent.coverage(messages, meeting)
            st.markdown(f"**{invitee['name']}** — {invitee['email']}")
            st.caption(
                f"{status} · last active {invitee['last_active_at'] or '—'} · "
                f"{len(covered)} of {len(meeting.agenda)} agenda item(s) covered"
            )

            if not closed and joined:
                if st.button(
                    "Generate status",
                    key=f"meetings_status_{invitee['invitee_id']}",
                    icon=":material/summarize:",
                    help="A provisional summary of this in-progress chat. Overwrites the previous one.",
                ):
                    _handle_generate_status(meeting, user_id, invitee["invitee_id"], messages)

            snapshot = summary_agent.from_json(invitee["live_status_json"] or "")
            if snapshot is not None and not closed:
                with st.expander(f"Live status — generated {invitee['live_status_at']}"):
                    _render_summary(snapshot)


def _handle_generate_status(meeting: Meeting, user_id: int, invitee_id: int, messages: list) -> None:
    profile = default_profile(user_id)
    if profile is None:
        st.error("Set a default model in Settings before generating a summary.", icon=":material/error:")
        return

    try:
        summary = summary_agent.generate_summary(meeting, profile, messages)
        db.save_live_status(meeting.meeting_id, invitee_id, user_id, summary_agent.to_json(summary))
    except (MeetingAgentError, MeetingError) as error:
        logger.exception("Could not generate a live status for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    session.flash("Status generated.")
    st.rerun()


def _render_transcript(messages: list) -> None:
    """One invitee's conversation, read-only.

    Shared shape with the invitee's own view on purpose — a creator reviewing a chat should
    be reading the same thing the invitee saw, not a reformatted approximation of it.
    """
    if not messages:
        st.info("This invitee hasn't started their chat yet.", icon=":material/info:")
        return
    for message in messages:
        with st.chat_message("assistant" if message.is_from_ai() else "user"):
            st.write(message.text)


def _pick_invitee(invitees: list[dict], key: str, only_closed: bool = False) -> dict | None:
    options = [person for person in invitees if not only_closed or person["closed"]]
    if not options:
        return None
    labels = {person["invitee_id"]: f"{person['name']} ({person['email']})" for person in options}
    chosen = st.selectbox(
        "Invitee",
        options=list(labels),
        format_func=lambda value: labels[value],
        key=key,
        help="Whose conversation to show.",
    )
    return next(person for person in options if person["invitee_id"] == chosen)


def _render_summary(summary) -> None:
    for entry in summary.agenda_items:
        icon = ":material/check_circle:" if entry.discussed else ":material/radio_button_unchecked:"
        st.markdown(f":{'green' if entry.discussed else 'grey'}[{icon}] **{entry.item}**")
        st.write(entry.notes or "_Not discussed._")

    if summary.other_extra.strip():
        st.markdown("**Other / Extra**")
        st.write(summary.other_extra)

    if summary.closing_message.strip():
        st.caption(summary.closing_message)


def _render_share(meeting: Meeting, invitees: list[dict]) -> None:
    st.info(
        "Copy each invitee's link and access code and send them yourself — this app doesn't "
        "send email.",
        icon=":material/info:",
    )
    for invitee in invitees:
        with st.container(border=True):
            st.markdown(f"**{invitee['name']}** — {invitee['email']}")
            st.caption("Link")
            st.code(f"{BASE_URL}/?m={meeting.meeting_id}&t={invitee['token']}", language=None)
            st.caption("Access code")
            try:
                st.code(access.decrypt_code(invitee["access_code_enc"]), language=None)
            except MeetingError:
                logger.exception("Could not decrypt the access code for invitee %s.", invitee["invitee_id"])
                st.error("This invitee's access code can't be read.", icon=":material/error:")


def _render_detail(user_id: int, meeting_id: int) -> None:
    try:
        meeting = db.load_meeting(meeting_id, user_id)
        invitees = db.list_invitees(meeting_id, user_id)
    except MeetingError as error:
        logger.exception("Could not open meeting %s for user %s.", meeting_id, user_id)
        st.error(str(error), icon=":material/error:")
        if st.button("Back to meetings", key="meetings_back_error", icon=":material/arrow_back:"):
            session.open_meeting(None)
            st.rerun()
        return

    if st.button("Back to meetings", key="meetings_back", icon=":material/arrow_back:",
                 help="Return to the list."):
        session.open_meeting(None)
        st.rerun()

    st.subheader(meeting.display_subject())

    overview, status, chat, summary, share = st.tabs(
        ["Overview", "Invitees & status", "View chat", "View summary", "Share"]
    )

    with overview:
        _render_overview(meeting, user_id)

    with status:
        _render_invitee_status(meeting, user_id, invitees)

    with chat:
        chosen = _pick_invitee(invitees, "meetings_chat_pick")
        if chosen is None:
            st.info("This meeting has no invitees.", icon=":material/info:")
        else:
            try:
                _render_transcript(db.list_messages_for_creator(meeting_id, chosen["invitee_id"], user_id))
            except MeetingError as error:
                st.error(str(error), icon=":material/error:")

    with summary:
        chosen = _pick_invitee(invitees, "meetings_summary_pick", only_closed=True)
        if chosen is None:
            st.info("No invitee has finished their chat yet.", icon=":material/info:")
        else:
            parsed = summary_agent.from_json(chosen["summary_json"] or "")
            if parsed is None:
                st.warning("This invitee's summary couldn't be read.", icon=":material/error:")
            else:
                _render_summary(parsed)

    with share:
        _render_share(meeting, invitees)


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


_render_profile_sidebar()

st.title("Meetings")

_flash = session.take_flash()
if _flash:
    st.success(_flash, icon=":material/check_circle:")

_user_id = _current_user_id()
if _user_id is None:
    st.error("Please log in again.", icon=":material/lock:")
    st.stop()

_open_id = session.open_meeting_id()
if _open_id is None:
    _render_list(_user_id)
else:
    _render_detail(_user_id, _open_id)
