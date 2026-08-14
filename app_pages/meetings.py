"""Creating meetings and following what invitees said (requirement 6.7, spec 1 & 8).

Two views in one page — a list and a detail — switched by `meetings.session.open_meeting_id`
rather than by separate `st.Page` entries, because "which meeting" is a selection inside one
workflow, not a different place in the app.

A **table agenda item's data is attached after the meeting exists**, from the Overview tab,
not in the creation dialog. Uploading a sheet and then marking each of its columns locked or
editable is a two-step operation over a file the dialog would have to hold across reruns
while every other field in it stays live — and the item itself is what the grid hangs off, so
it has to exist first either way.
"""

import logging
import os

import pandas as pd
import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from llm.session import default_profile
from meetings import (
    access,
    db,
    extraction_agent,
    matrix,
    session,
    storage,
    summary_agent,
    tables,
)
from meetings.exceptions import MeetingAgentError, MeetingError
from meetings.model import (
    DISCUSSION_ITEM,
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    EvaluationField,
    Meeting,
    evaluation_buckets_from_text,
    evaluation_buckets_to_text,
)
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

# Where an invitee link points. An env var rather than `st.secrets`, which raises outright
# when no secrets file exists — and a missing base URL should give a working local link, not
# take the page down.
BASE_URL = os.environ.get("TIKITARAI_BASE_URL", "http://localhost:8501").rstrip("/")

AGENDA_ROWS_KEY = "meetings_agenda_rows"
INVITEE_ROWS_KEY = "meetings_invitee_rows"
EVALUATION_ROWS_KEY = "meetings_evaluation_rows"

# What the creator picks in the agenda grid, and what it means in the stored agenda.
TYPE_DISCUSSION = "Discussion"
TYPE_TABLE = "Table"
ITEM_TYPE_BY_LABEL = {TYPE_DISCUSSION: DISCUSSION_ITEM, TYPE_TABLE: TABLE_ITEM}
LABEL_BY_ITEM_TYPE = {value: key for key, value in ITEM_TYPE_BY_LABEL.items()}


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
            [{"Agenda item": "", "Type": TYPE_DISCUSSION, "AI note": ""}]
        )
    return st.session_state[AGENDA_ROWS_KEY]


def _invitee_rows() -> pd.DataFrame:
    if INVITEE_ROWS_KEY not in st.session_state:
        st.session_state[INVITEE_ROWS_KEY] = pd.DataFrame([{"Name": "", "Email": ""}])
    return st.session_state[INVITEE_ROWS_KEY]


def _evaluation_rows() -> pd.DataFrame:
    if EVALUATION_ROWS_KEY not in st.session_state:
        st.session_state[EVALUATION_ROWS_KEY] = pd.DataFrame([{"Question": "", "Buckets": ""}])
    return st.session_state[EVALUATION_ROWS_KEY]


def _clear_creation_rows() -> None:
    st.session_state.pop(AGENDA_ROWS_KEY, None)
    st.session_state.pop(INVITEE_ROWS_KEY, None)
    st.session_state.pop(EVALUATION_ROWS_KEY, None)


def _agenda_editor(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """The agenda grid, shared by the creation dialog and the detail page's setup editor.

    `Type` is a fixed-option column rather than free text: it decides which table the item's
    substance is stored in, so a typo would have to become a silent fallback to Discussion —
    an agenda item quietly demoted from a grid to a chat question.
    """
    return st.data_editor(
        frame,
        num_rows="dynamic",
        width="stretch",
        key=key,
        column_config={
            "Type": st.column_config.SelectboxColumn(
                "Type",
                options=list(ITEM_TYPE_BY_LABEL),
                default=TYPE_DISCUSSION,
                required=True,
                help="Discussion items are talked through in the chat. Table items get their own grid tab.",
            ),
        },
    )


def _evaluation_editor(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """The evaluation-questions grid (spec 3b). Leaving it empty turns the feature off."""
    return st.data_editor(
        frame,
        num_rows="dynamic",
        width="stretch",
        key=key,
        column_config={
            "Buckets": st.column_config.TextColumn(
                "Buckets (optional)",
                help="Comma-separated options to classify the answer into, e.g. Low, Medium, High.",
            ),
        },
    )


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

    st.caption(
        "Agenda — one item per row. The AI note is the ground truth for that item. A Table "
        "item's data is attached after the meeting is created."
    )
    agenda_frame = _agenda_editor(_agenda_rows(), "meetings_new_agenda")

    st.caption(
        "Evaluation questions (optional) — short questions the AI works into the conversation, "
        "so the answers can be compared across invitees. Leave blank to skip."
    )
    evaluation_frame = _evaluation_editor(_evaluation_rows(), "meetings_new_evaluation")

    st.caption("Invitees — each gets their own private link and access code.")
    invitee_frame = st.data_editor(
        _invitee_rows(),
        num_rows="dynamic",
        width="stretch",
        key="meetings_new_invitees"
    )

    save_default = st.checkbox(
        "Save this persona as my default",
        key="meetings_new_save_default",
        help="Pre-fills the persona on every meeting you create from now on.",
    )

    if st.button("Create meeting", key="meetings_new_save", icon=":material/save:", type="primary"):
        _handle_create_meeting(
            user_id,
            subject,
            meeting_context,
            persona,
            context_sop,
            agenda_frame,
            evaluation_frame,
            invitee_frame,
            save_default,
        )


def _agenda_from_frame(frame: pd.DataFrame) -> list[AgendaItem]:
    items = []
    for _, row in frame.iterrows():
        title = str(row.get("Agenda item") or "").strip()
        if not title:
            continue
        label = str(row.get("Type") or TYPE_DISCUSSION).strip()
        items.append(
            AgendaItem(
                item=title,
                ai_note=str(row.get("AI note") or "").strip(),
                item_type=ITEM_TYPE_BY_LABEL.get(label, DISCUSSION_ITEM),
            )
        )
    return items


def _agenda_to_frame(agenda: list[AgendaItem]) -> pd.DataFrame:
    """A stored agenda back into the editor's shape, with a blank row to grow into."""
    rows = [
        {
            "Agenda item": item.item,
            "Type": LABEL_BY_ITEM_TYPE.get(item.item_type, TYPE_DISCUSSION),
            "AI note": item.ai_note,
        }
        for item in agenda
    ]
    rows.append({"Agenda item": "", "Type": TYPE_DISCUSSION, "AI note": ""})
    return pd.DataFrame(rows)


def _evaluation_from_frame(frame: pd.DataFrame, existing: list[EvaluationField]) -> list[EvaluationField]:
    """The evaluation grid back into fields, keeping ids where the question is unchanged.

    Matching on the question text is what stops an edit elsewhere in the grid from looking
    like a delete-and-recreate to `db.replace_evaluation_fields` — which would take every
    answer already extracted for that question with it.
    """
    by_question = {field_spec.question.strip().lower(): field_spec for field_spec in existing}

    fields = []
    for _, row in frame.iterrows():
        question = str(row.get("Question") or "").strip()
        if not question:
            continue
        previous = by_question.get(question.lower())
        fields.append(
            EvaluationField(
                field_id=previous.field_id if previous is not None else None,
                question=question,
                buckets=evaluation_buckets_from_text(row.get("Buckets")),
            )
        )
    return fields


def _evaluation_to_frame(fields: list[EvaluationField]) -> pd.DataFrame:
    rows = [
        {"Question": field_spec.question, "Buckets": evaluation_buckets_to_text(field_spec.buckets)}
        for field_spec in fields
    ]
    rows.append({"Question": "", "Buckets": ""})
    return pd.DataFrame(rows)


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
    evaluation_frame: pd.DataFrame,
    invitee_frame: pd.DataFrame,
    save_default: bool,
) -> None:
    agenda = _agenda_from_frame(agenda_frame)
    fields = _evaluation_from_frame(evaluation_frame, [])
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
        if fields:
            db.replace_evaluation_fields(saved.meeting_id, user_id, fields)
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
    if saved.table_items():
        # Said now rather than left to be discovered: an invitee who opens their link before
        # the sheet is attached finds a table tab that can't be filled in.
        session.flash(
            f"Created '{saved.display_subject()}'. Attach the data for each table item in "
            "Overview before sharing the links."
        )
    else:
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


def _render_overview(meeting: Meeting, user_id: int, fields: list[EvaluationField]) -> None:
    st.markdown("**Meeting context**")
    st.write(meeting.meeting_context or "_None given._")

    st.markdown("**Persona**")
    st.write(meeting.persona or "_None given._")

    st.markdown("**Context / SOP**")
    st.write(meeting.context_sop or "_None given._")

    st.markdown("**Agenda**")
    if meeting.agenda:
        for item in meeting.agenda:
            kind = " · table" if item.is_table() else ""
            st.markdown(f"- **{item.item}**{kind}" + (f" — {item.ai_note}" if item.ai_note else ""))
    else:
        st.write("_No agenda items._")

    if fields:
        st.markdown("**Evaluation questions**")
        for field_spec in fields:
            buckets = evaluation_buckets_to_text(field_spec.buckets)
            st.markdown(f"- {field_spec.question}" + (f" — _{buckets}_" if buckets else ""))

    _render_setup_editor(meeting, user_id, fields)

    for item in meeting.table_items():
        _render_table_setup(meeting, user_id, item)

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


def _render_setup_editor(meeting: Meeting, user_id: int, fields: list[EvaluationField]) -> None:
    """Editing the agenda and the evaluation questions after the meeting exists.

    Collapsed by default and separate from the read-only summary above it, because reading
    what a meeting is set up to do is the common visit and rewriting it is the rare one.
    """
    with st.expander("Edit agenda & questions"):
        st.caption("Agenda — changing an item's title detaches any table data attached to it.")
        agenda_frame = _agenda_editor(
            _agenda_to_frame(meeting.agenda), f"meetings_edit_agenda_{meeting.meeting_id}"
        )

        st.caption("Evaluation questions — removing one also removes the answers extracted for it.")
        evaluation_frame = _evaluation_editor(
            _evaluation_to_frame(fields), f"meetings_edit_evaluation_{meeting.meeting_id}"
        )

        if st.button(
            "Save setup",
            key=f"meetings_save_setup_{meeting.meeting_id}",
            icon=":material/save:",
            help="Applies to every invitee's next message, including chats already in progress.",
        ):
            _handle_save_setup(meeting, user_id, agenda_frame, evaluation_frame, fields)


def _handle_save_setup(
    meeting: Meeting,
    user_id: int,
    agenda_frame: pd.DataFrame,
    evaluation_frame: pd.DataFrame,
    fields: list[EvaluationField],
) -> None:
    meeting.agenda = _agenda_from_frame(agenda_frame)
    try:
        db.update_meeting(user_id, meeting)
        db.replace_evaluation_fields(meeting.meeting_id, user_id, _evaluation_from_frame(evaluation_frame, fields))
    except MeetingError as error:
        logger.exception("Could not save the setup for meeting %s.", meeting.meeting_id)
        st.error(str(error), icon=":material/error:")
        return

    session.flash("Setup saved.")
    st.rerun()


def _render_table_setup(meeting: Meeting, user_id: int, item: AgendaItem) -> None:
    """Attaching a sheet to one table agenda item, and marking its columns (spec 3a).

    Opened by default while there is no sheet attached: until there is, the item is a tab
    every invitee can see and none of them can fill in.
    """
    try:
        attached = db.find_agenda_table(meeting.meeting_id, item.item)
    except MeetingError as error:
        logger.exception("Could not read the table for '%s'.", item.item)
        st.error(str(error), icon=":material/error:")
        return

    with st.expander(f"Table data — {item.item}", expanded=attached is None):
        if attached is not None:
            st.caption(
                f"{attached.source_file or 'Uploaded sheet'} · {attached.row_count()} row(s) · "
                f"locked: {', '.join(attached.locked_columns) or 'none'} · "
                f"editable: {', '.join(attached.editable_columns) or 'none'}"
            )
            if st.button(
                "Remove this table",
                key=f"meetings_table_remove_{meeting.meeting_id}_{item.item}",
                icon=":material/delete:",
                help="Deletes the sheet and every answer invitees have filled into it.",
            ):
                _handle_remove_table(meeting.meeting_id, user_id, item.item)
            st.caption("Uploading another sheet replaces this one and clears every answer to it.")

        uploaded = st.file_uploader(
            "Upload the sheet for this item",
            type=["csv", "xlsx", "xls"],
            key=f"meetings_table_upload_{meeting.meeting_id}_{item.item}",
            help="One row per thing the invitee has to respond about.",
        )
        if uploaded is None:
            return

        try:
            frame = tables.read_source(uploaded.getvalue(), uploaded.name)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            return

        st.dataframe(frame.head(5), width="stretch", hide_index=True)

        editable = st.multiselect(
            "Columns the invitee fills in",
            options=list(frame.columns),
            default=list(attached.editable_columns) if attached is not None else [],
            key=f"meetings_table_editable_{meeting.meeting_id}_{item.item}",
            help="Everything else is shown read-only, for reference.",
        )

        if st.button(
            "Attach table",
            key=f"meetings_table_save_{meeting.meeting_id}_{item.item}",
            icon=":material/table:",
            type="primary",
            help="Saves this sheet as the grid every invitee fills in for this item.",
        ):
            _handle_attach_table(meeting.meeting_id, user_id, item.item, uploaded.name, frame, editable)


def _handle_attach_table(
    meeting_id: int,
    user_id: int,
    item_ref: str,
    filename: str,
    frame: pd.DataFrame,
    editable: list[str],
) -> None:
    if not editable:
        st.warning(
            "Mark at least one column as editable — a grid with nothing to fill in is only a document.",
            icon=":material/error:",
        )
        return

    table = AgendaTable(
        meeting_id=meeting_id,
        item_ref=item_ref,
        source_file=filename,
        # Column order is the sheet's own, minus the editable ones — the creator laid the
        # columns out in an order that reads, and re-sorting them would undo that.
        locked_columns=[column for column in frame.columns if column not in editable],
        editable_columns=[column for column in frame.columns if column in editable],
        base_data=tables.base_data_from_frame(frame),
    )

    try:
        db.save_agenda_table(meeting_id, user_id, table)
    except MeetingError as error:
        logger.exception("Could not attach a table to '%s'.", item_ref)
        st.error(str(error), icon=":material/error:")
        return

    session.flash(f"Table attached to '{item_ref}'.")
    st.rerun()


def _handle_remove_table(meeting_id: int, user_id: int, item_ref: str) -> None:
    try:
        db.delete_agenda_table(meeting_id, user_id, item_ref)
    except MeetingError as error:
        logger.exception("Could not remove the table on '%s'.", item_ref)
        st.error(str(error), icon=":material/error:")
        return

    session.flash(f"Table removed from '{item_ref}'.")
    st.rerun()


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


def _render_invitee_status(
    meeting: Meeting, user_id: int, invitees: list[dict], fields: list[EvaluationField]
) -> None:
    if not invitees:
        st.info("This meeting has no invitees.", icon=":material/info:")
        return

    discussion_count = len(meeting.discussion_items())

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
                f"{len(covered)} of {discussion_count} agenda item(s) covered"
            )

            # Spec 3a tracks a table item by rows filled, so it gets its own line rather
            # than being folded into the agenda count above.
            for item_ref, (filled, total) in _table_progress(meeting.meeting_id, invitee["invitee_id"]).items():
                st.caption(f"{item_ref}: {tables.format_completion(filled, total)}")

            if not closed and joined:
                if st.button(
                    "Generate status",
                    key=f"meetings_status_{invitee['invitee_id']}",
                    icon=":material/summarize:",
                    help="A provisional summary of this in-progress chat. Overwrites the previous one.",
                ):
                    _handle_generate_status(meeting, user_id, invitee["invitee_id"], messages, fields)

            if closed and fields:
                if st.button(
                    "Re-extract answers",
                    key=f"meetings_extract_{invitee['invitee_id']}",
                    icon=":material/fact_check:",
                    help="Reads the evaluation answers out of this finished chat again.",
                ):
                    _handle_extract(meeting, user_id, invitee["invitee_id"], messages, fields)

            snapshot = summary_agent.from_json(invitee["live_status_json"] or "")
            if snapshot is not None and not closed:
                with st.expander(f"Live status — generated {invitee['live_status_at']}"):
                    _render_summary(snapshot)


def _table_progress(meeting_id: int, invitee_id: int) -> dict[str, tuple[int, int]]:
    """One invitee's row counts per table item, or nothing if they can't be read."""
    try:
        return db.table_progress(meeting_id, invitee_id)
    except MeetingError:
        logger.exception("Could not read table progress for invitee %s.", invitee_id)
        return {}


def _handle_generate_status(
    meeting: Meeting, user_id: int, invitee_id: int, messages: list, fields: list[EvaluationField]
) -> None:
    """The on-demand snapshot: a provisional MoM, and the evaluation answers alongside it.

    Both, on one press, because spec 3 makes them the same question asked of the same
    conversation — "where has this invitee got to". The extraction failing is reported but
    does not discard the status that was already generated.
    """
    profile = default_profile(user_id)
    if profile is None:
        st.error("Set a default model in Settings before generating a summary.", icon=":material/error:")
        return

    try:
        summary = summary_agent.generate_summary(
            meeting, profile, messages, table_progress=_table_progress(meeting.meeting_id, invitee_id)
        )
        db.save_live_status(meeting.meeting_id, invitee_id, user_id, summary_agent.to_json(summary))
    except (MeetingAgentError, MeetingError) as error:
        logger.exception("Could not generate a live status for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    if fields:
        try:
            db.save_evaluation_answers(
                invitee_id, extraction_agent.extract_answers(meeting, profile, fields, messages)
            )
        except (MeetingAgentError, MeetingError) as error:
            logger.exception("Could not extract evaluation answers for invitee %s.", invitee_id)
            st.warning(f"Status generated, but the evaluation answers weren't: {error}", icon=":material/error:")

    session.flash("Status generated.")
    st.rerun()


def _handle_extract(
    meeting: Meeting, user_id: int, invitee_id: int, messages: list, fields: list[EvaluationField]
) -> None:
    """Re-reads a finished chat's evaluation answers, leaving its locked MoM alone."""
    profile = default_profile(user_id)
    if profile is None:
        st.error("Set a default model in Settings before extracting answers.", icon=":material/error:")
        return

    try:
        db.save_evaluation_answers(
            invitee_id, extraction_agent.extract_answers(meeting, profile, fields, messages)
        )
    except (MeetingAgentError, MeetingError) as error:
        logger.exception("Could not extract evaluation answers for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    session.flash("Evaluation answers extracted.")
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
        if entry.is_table:
            # A table item is never "discussed" — its line is a row count, so a tick beside a
            # half-filled grid would be claiming something the number underneath contradicts.
            st.markdown(f":grey[:material/table:] **{entry.item}**")
        else:
            icon = ":material/check_circle:" if entry.discussed else ":material/radio_button_unchecked:"
            st.markdown(f":{'green' if entry.discussed else 'grey'}[{icon}] **{entry.item}**")
        st.write(entry.notes or "_Not discussed._")

    if summary.other_extra.strip():
        st.markdown("**Other / Extra**")
        st.write(summary.other_extra)

    if summary.closing_message.strip():
        st.caption(summary.closing_message)


def _render_comparisons(
    meeting: Meeting, invitees: list[dict], fields: list[EvaluationField]
) -> None:
    """Spec 8's three cross-invitee matrices, as sub-tabs.

    None of them makes a provider call: every cell was written when a chat closed, when a
    status was generated, or when an invitee pressed Save progress.
    """
    if not invitees:
        st.info("This meeting has no invitees to compare.", icon=":material/info:")
        return

    consolidated, evaluation, table_comparison = st.tabs(
        ["Consolidated MoM", "Evaluation answers", "Table comparisons"]
    )

    with consolidated:
        _render_consolidated(meeting, invitees)

    with evaluation:
        _render_evaluation_matrix(meeting, invitees, fields)

    with table_comparison:
        _render_table_comparisons(meeting, invitees)


def _render_consolidated(meeting: Meeting, invitees: list[dict]) -> None:
    if not meeting.agenda:
        st.info("This meeting has no agenda items to compare.", icon=":material/info:")
        return

    summaries = {}
    for invitee in invitees:
        # The final MoM where there is one, the live snapshot otherwise — spec 8's
        # "whichever is available". A closed chat's record always wins over a provisional one.
        stored = invitee["summary_json"] or invitee["live_status_json"] or ""
        parsed = summary_agent.from_json(stored)
        if parsed is not None:
            summaries[invitee["invitee_id"]] = parsed

    st.caption("Rows are agenda items, columns are invitees. Nothing here calls the model.")
    st.dataframe(
        matrix.consolidated_frame(meeting, invitees, summaries),
        width="stretch",
        hide_index=True,
    )


def _render_evaluation_matrix(meeting: Meeting, invitees: list[dict], fields: list[EvaluationField]) -> None:
    if not fields:
        st.info(
            "This meeting has no evaluation questions. Add them in Overview to compare short "
            "answers across invitees.",
            icon=":material/info:",
        )
        return

    try:
        answers = db.list_evaluation_answers(meeting.meeting_id)
    except MeetingError as error:
        logger.exception("Could not read evaluation answers for meeting %s.", meeting.meeting_id)
        st.error(str(error), icon=":material/error:")
        return

    st.caption("Extracted when each chat closed. Use Generate status or Re-extract to refresh.")
    st.dataframe(
        matrix.evaluation_frame(fields, invitees, answers),
        width="stretch",
        hide_index=True,
    )


def _render_table_comparisons(meeting: Meeting, invitees: list[dict]) -> None:
    table_items = meeting.table_items()
    if not table_items:
        st.info("This meeting has no table agenda items.", icon=":material/info:")
        return

    for item in table_items:
        st.markdown(f"**{item.item}**")
        try:
            table = db.find_agenda_table(meeting.meeting_id, item.item)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            continue

        if table is None:
            st.caption("No data attached to this item yet.")
            continue
        if not table.editable_columns:
            st.caption("This table has no columns for invitees to fill in.")
            continue

        column = st.selectbox(
            "Column to compare",
            options=table.editable_columns,
            key=f"meetings_compare_column_{table.table_id}",
            help="One column at a time — every column at once is too wide to read down.",
        )

        try:
            responses = db.load_all_table_responses(table.table_id)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            continue

        st.dataframe(
            matrix.table_comparison_frame(table, column, invitees, responses),
            width="stretch",
            hide_index=True,
        )


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
        fields = db.list_evaluation_fields(meeting_id)
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

    overview, status, chat, summary, comparisons, share = st.tabs(
        ["Overview", "Invitees & status", "View chat", "View summary", "Comparisons", "Share"]
    )

    with overview:
        _render_overview(meeting, user_id, fields)

    with status:
        _render_invitee_status(meeting, user_id, invitees, fields)

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

    with comparisons:
        _render_comparisons(meeting, invitees, fields)

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
