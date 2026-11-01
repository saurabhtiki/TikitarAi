"""The invitee's private chat (requirement 6.7, spec 2 and 3a).

**Not registered as an `st.Page`.** `streamlit_app.py` calls `render_invitee_page` directly
when the URL carries an invitee link, before the login gate — an invitee has no account, so
this screen has to exist entirely outside `st.navigation`. `checks_view.py` set the
precedent for a page module rendered by call rather than by registration.

Nothing here touches `auth/` sessions. The invitee's unlock lives in its own `invitee_*`
session keys, so `st.session_state["user_id"]` stays empty and nothing that checks it can
mistake an invitee for an employee. (Phase 67 only *reads* the organiser's profile photo,
for the bot's chat icon.)

Phase 67: the bot can show a reference document (picture, video, PDF, HTML) under its
reply; Word, Excel and other files are only pointed to (`meetings.show_files`).

The model used is the one the organiser picked for this meeting, else their default
(phase 56, `meetings.llm_choice`).

Phase 56 adds a left side panel (1/4 width): how to use the meeting, the agenda, and the
file attach box. Close chat waits until every question on the invitee's path is done.
Phase 57 names each table tab and its columns in How to use, and adds each For each list
(with a done / now column) and the organiser's reference documents to that panel.

Phase 64 adds voice: 🎤 Speak above the chat box and 🔊 under each bot reply, both done by
the invitee's browser (`app_pages.voice_controls`).

Phase 70: a step's **Show document** is shown when the step starts, and a step with a **Fill
table** waits until the invitee has filled that tab in; saving it posts "I have filled in the
… table." and the step starts (`_FillTables`).

Phase 2 adds the tab strip spec 3a asks for: every discussion item shares one **General
Discussion** tab, and each table item gets its own grid tab. Ten discussion items and two
tables are three tabs, not twelve.
"""

import base64
import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitAPIException

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from llm.client import LLMConnectionError
from meetings import (
    chat_agent,
    db,
    extraction_agent,
    guide,
    llm_choice,
    loops,
    running_summary,
    session,
    show_files,
    steps,
    storage,
    summary_agent,
    tables,
)
from app_pages.voice_controls import auto_read_switch, listen_button, speak_button
from meetings.access import verify_code
from meetings.exceptions import MeetingAgentError, MeetingError
from meetings.model import (
    ANSWER_CHOICE,
    ANSWER_DATE,
    ANSWER_NUMBER,
    ANSWER_YES_NO,
    SENDER_AI,
    SENDER_USER,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    AgendaItem,
    AgendaTable,
    ColumnRule,
    Faq,
    Meeting,
    StepAnswer,
)
from utils.dates import show_dataframe

logger = logging.getLogger(__name__)

DISCUSSION_TAB = "💬 General Discussion"
AUTO_READ_KEY = "invitee_auto_read"
# Chrome's data: URL limit is 2 MB; an HTML page shown in the chat must fit inside it.
_MAX_HTML_URL_CHARS = 2_000_000


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
    """The meeting's model (else the creator's default). An invitee has no model choice."""
    return llm_choice.meeting_profile(meeting).profile


def _note_model(meeting_id: int, invitee_id: int, profile: dict) -> None:
    """Records which model replied, for the organiser's Status tab. Never costs the reply."""
    try:
        db.set_model_used(meeting_id, invitee_id, llm_choice.profile_label(profile))
    except MeetingError:
        logger.exception("Could not note the model used for invitee %s.", invitee_id)


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


@dataclass
class _FillTables:
    """The Table rows named in Fill table cells, as this invitee has them (phase 70).

    Keyed by lower-case title. A table with no data uploaded yet isn't here, so it never holds
    anyone up.
    """

    sheets: dict[str, AgendaTable] = field(default_factory=dict)
    responses: dict[str, dict[int, dict]] = field(default_factory=dict)

    def unfilled(self) -> set[str]:
        return {
            sheet.item_ref
            for key, sheet in self.sheets.items()
            if not tables.is_filled(sheet, self.responses.get(key, {}))
        }

    def waits(self, item: AgendaItem | None) -> bool:
        """Whether this step can't go on yet because its Fill table isn't filled in."""
        key = item.fill_table.strip().lower() if item is not None else ""
        sheet = self.sheets.get(key)
        return sheet is not None and not tables.is_filled(sheet, self.responses.get(key, {}))

    def columns(self, title: str) -> str:
        sheet = self.sheets.get(title.strip().lower())
        if sheet is None:
            return ""
        return ", ".join(tables.describe_column(column, sheet.rule_for(column)) for column in tables.must_fill_columns(sheet))

    def note(self, title: str) -> str:
        """What the invitee filled in, for the bot; "" when there is no such table."""
        key = title.strip().lower()
        sheet = self.sheets.get(key)
        if sheet is None:
            return ""
        return chat_agent.filled_table_note(sheet.item_ref, tables.filled_text(sheet, self.responses.get(key, {})))


def _fill_tables(meeting: Meeting, meeting_id: int, invitee_id: int) -> _FillTables:
    """This invitee's Fill tables (phase 70). A read failure holds nobody up: it is logged and
    the steps go on as if there were no table to fill."""
    wanted = {item.fill_table.strip().lower() for item in meeting.step_items() if item.fill_table.strip()}
    if not wanted:
        return _FillTables()
    fills = _FillTables()
    try:
        for sheet in db.list_agenda_tables(meeting_id):
            key = sheet.item_ref.strip().lower()
            if key in wanted:
                fills.sheets[key] = sheet
                fills.responses[key] = db.load_table_responses(sheet.table_id, invitee_id)
    except MeetingError:
        logger.exception("Could not read the Fill tables of meeting %s for invitee %s.", meeting_id, invitee_id)
        return _FillTables()
    return fills


def _with_table_note(meeting: Meeting, guidance: str, step_tag: str, fills: _FillTables) -> str:
    """Adds what the invitee filled in to the guidance of a step whose Fill table is done."""
    item = next((item for item in meeting.agenda if item.item == step_tag), None)
    if item is None or not item.fill_table or fills.waits(item):
        return guidance
    note = fills.note(item.fill_table)
    return f"{guidance}\n\n{note}" if guidance and note else guidance


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
    fills = _fill_tables(meeting, meeting_id, invitee_id)

    if not messages and not chat_session.closed:
        _open_conversation(meeting, profile, invitee, fields, answers, lists, fills)
        return

    side, main = st.columns([1, 3])
    with side:
        _render_side_panel(meeting, meeting_id, invitee_id, chat_session.closed, answers, lists)
    with main:
        _render_main(meeting, profile, invitee, chat_session, messages, fields, answers, lists, fills)


def _render_main(
    meeting: Meeting,
    profile: dict,
    invitee: dict,
    chat_session,
    messages: list,
    fields: list,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
    fills: _FillTables,
) -> None:
    """The right 3/4: the chat (and any table tabs), then closing."""
    meeting_id = invitee["meeting_id"]
    invitee_id = invitee["invitee_id"]
    table_items = meeting.table_items()

    def _table_saved(title: str) -> None:
        """Phase 70: saving the table the open step waits for starts that step in the chat."""
        current = steps.next_question(meeting, answers, lists.rows)
        if current is None or current.question.fill_table.strip().lower() != title.strip().lower():
            return
        now = _fill_tables(meeting, meeting_id, invitee_id)
        if now.waits(current.question):
            st.warning(
                f"Saved — but the chat carries on only once every row has {now.columns(title)} filled in.",
                icon=":material/warning:",
            )
            return
        _handle_turn(
            meeting, profile, invitee, chat_session.running_summary, messages,
            f"I have filled in the {title} table.", fields, answers, lists, now, table_saved=True,
        )

    if table_items:
        # Spec 3a's tab strip: one shared discussion tab, one tab per grid. Built only when
        # there is a grid; a meeting without one shows the chat straight in this column.
        tabs = st.tabs([DISCUSSION_TAB, *[f"📋 {item.item}" for item in table_items]])
        with tabs[0]:
            _render_discussion(meeting, profile, invitee, chat_session, messages, fields, answers, lists, fills)
        for tab, item in zip(tabs[1:], table_items):
            with tab:
                _render_table_tab(meeting_id, invitee_id, item, chat_session.closed, on_saved=_table_saved)
    else:
        _render_discussion(meeting, profile, invitee, chat_session, messages, fields, answers, lists, fills)

    # Below the tabs, because closing and its summary belong to the whole session rather
    # than to whichever tab happens to be open.
    if chat_session.closed:
        _render_closed(meeting_id, invitee_id)
        return

    # Phase 56: the questions are the point of the meeting, so they must be done first. Tries
    # make sure nobody is stuck — running out marks a question Not answered and moves on.
    left = steps.questions_left(meeting, answers, lists.rows)
    if left:
        st.caption(f":red[{left} question(s) left — finish them to close.]")
    # Phase 70: a Fill table is mandatory, so it holds Close chat like a question does.
    tables_left = steps.tables_left(meeting, answers, fills.unfilled(), lists.rows)
    if tables_left:
        st.caption(f":red[Still to fill in: {', '.join(tables_left)} — finish to close.]")
    if st.button(
        "Close chat",
        key="invitee_close_chat",
        icon=":material/task_alt:",
        disabled=left > 0 or bool(tables_left),
        help="Finish and generate your summary. You won't be able to add anything afterwards. "
        "Available once every question is done and every table the chat asks for is filled in.",
    ):
        _handle_close(meeting, profile, meeting_id, invitee_id, fields)


def _progress(meeting: Meeting, step: steps.Step, answers: dict[str, StepAnswer], lists: loops.InviteeLists) -> str:
    """ "Question 2 of 5", or "Outstanding invoices — INV-102: row 2 of 3, question 1 of 3"."""
    return steps.progress_text(meeting, step, answers, lists.rows, row_label=lists.row_label(step))


def _table_sheets(meeting: Meeting, meeting_id: int) -> dict[str, AgendaTable]:
    """The grids behind the Table items, by title; none if they can't be read."""
    if not meeting.table_items():
        return {}
    try:
        return {table.item_ref: table for table in db.list_agenda_tables(meeting_id)}
    except MeetingError:
        logger.exception("Could not read the tables of meeting %s.", meeting_id)
        return {}


def _render_side_panel(
    meeting: Meeting,
    meeting_id: int,
    invitee_id: int,
    closed: bool,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
) -> None:
    """The left 1/4: how to use, the agenda, the For each lists, reference documents, attach."""
    sheets = _table_sheets(meeting, meeting_id)
    with st.expander("How to use this meeting", icon=":material/help:", expanded=True):
        list_sizes = {key: len(rows) for key, rows in lists.rows.items()}
        st.markdown(guide.invitee_text(meeting, guide.default_steps(meeting, sheets, list_sizes, _reference_files(meeting_id))))
    with st.expander("Agenda", icon=":material/list:"):
        if meeting.agenda:
            st.markdown("\n".join(f"- {item.item}" for item in meeting.agenda))
        else:
            st.caption(":red[No agenda items.]")
    _render_lists(meeting, answers, lists)
    _render_reference_documents(meeting_id)
    _render_uploads(meeting_id, invitee_id, closed)


def _render_lists(meeting: Meeting, answers: dict[str, StepAnswer], lists: loops.InviteeLists) -> None:
    """Each For each list as a table, with ✓ done / ▶ now, so the invitee can follow along."""
    for name in steps.loop_names(meeting):
        rows = lists.rows.get(steps.loop_key(name), [])
        if not rows:
            continue
        with st.expander(f"{name} ({len(rows)} rows)", icon=":material/table:", expanded=True):
            show_dataframe(loops.invitee_rows_frame(meeting, name, lists, answers), width="stretch", hide_index=True)


def _reference_files(meeting_id: int) -> list[dict]:
    """The organiser's shared documents; none if they can't be read."""
    try:
        return db.list_files(meeting_id)
    except MeetingError:
        logger.exception("Could not list the reference documents of meeting %s.", meeting_id)
        return []


def _bot_avatar(meeting: Meeting) -> str | None:
    """The organiser's profile photo (Settings), or None for the default bot icon (phase 67)."""
    if meeting.created_by is None:
        return None
    try:
        user = get_user_by_id(meeting.created_by)
    except AuthDatabaseError:
        logger.exception("Could not read the organiser of meeting %s for the chat icon.", meeting.meeting_id)
        return None
    photo_path = (user or {}).get("photo_path")
    return photo_path if photo_path and Path(photo_path).is_file() else None


def _shown_file_id(
    turn: chat_agent.ChatTurnOutput, files: list[dict], meeting: Meeting, messages: list
) -> int | None:
    """The id of the document to show under a bot reply, only if it exists and can be shown.

    The step's own Show document comes first (phase 70), once per step. Then the bot's own
    pick. If it made none, a file named in the note of the step the reply is about ("Show
    demo.mp4, then discuss") is shown, once per step: a later step naming the same file shows
    it again.

    A document that is another row's Show document is never shown here: only that row shows it.
    """
    item = next((item for item in meeting.agenda if item.item == turn.agenda_tag), None)
    shown_ids = {
        message.shown_file_id
        for message in messages
        if message.shown_file_id and message.agenda_tag == turn.agenda_tag
    }
    if item is not None and item.show_file:
        chosen = show_files.chosen_file(item.show_file, files, shown_ids)
        if chosen is not None:
            return chosen["file_id"]
    kept = show_files.kept_for_other_steps(meeting.agenda, turn.agenda_tag, files)
    free_files = [record for record in files if record.get("file_id") not in kept]
    name = turn.show_file
    record = show_files.pick_file(name, free_files)
    if record is not None and show_files.shows(record):
        return record["file_id"]
    if name.strip():
        logger.info("Bot asked to show '%s', which is not a showable reference document here.", name)
    note = item.ai_note if item is not None else ""
    step_file = show_files.file_for_step(note, free_files, shown_ids)
    return step_file["file_id"] if step_file else None


@st.cache_resource(max_entries=4, ttl=3600, show_spinner=False)
def _shown_file_bytes(filepath: str, modified: float) -> bytes:
    """A shown document's bytes, read from disk once rather than on every rerun.

    `modified` is only part of the cache key, so a replaced file is read again.
    """
    return storage.read_file(filepath)


def _render_shown_file(file_id: int, record: dict | None, message_index: int) -> None:
    """A reference document under a bot reply: picture, video, PDF or HTML page (phase 67)."""
    if record is None:
        logger.warning("Shown reference document %s is no longer in the meeting's files.", file_id)
        st.caption(":red[This file is no longer available.]")
        return
    if show_files.is_link(record):
        _render_shown_link(record, message_index)
        return
    filename = record["filename"]
    kind = show_files.file_kind(filename)
    filepath = Path(record["filepath"])
    try:
        modified = filepath.stat().st_mtime
        # The PDF viewer reads the file itself, so only the other kinds need its bytes.
        payload = b"" if kind == show_files.PDF_KIND else _shown_file_bytes(str(filepath), modified)
    except (OSError, MeetingError):
        logger.exception("Could not read reference document %s to show it.", record["file_id"])
        st.caption(f":red[{filename} — not available.]")
        return
    try:
        if kind == show_files.IMAGE_KIND:
            st.image(payload, caption=filename)
        elif kind == show_files.VIDEO_KIND:
            st.video(payload, format=show_files.video_format(filename))
        elif kind == show_files.PDF_KIND:
            # The browser's own PDF viewer; st.iframe uploads a Path to media storage.
            st.iframe(filepath, height=600)
        elif kind == show_files.HTML_KIND:
            # A data: URL runs in its own sealed-off origin, so the organiser's page can't
            # reach into the app the way an HTML string passed to st.iframe could.
            encoded = base64.b64encode(payload).decode("ascii")
            if len(encoded) > _MAX_HTML_URL_CHARS:
                # Chrome leaves an over-long data: URL blank without any error to catch.
                st.caption(f":red[{filename} is too big to show here — open it under Reference documents.]")
                return
            st.iframe(f"data:text/html;charset=utf-8;base64,{encoded}", height=500)
    except (OSError, ValueError, StreamlitAPIException):
        logger.exception("Could not show reference document %s.", record["file_id"])
        st.caption(f":red[{filename} couldn't be shown here — open it under Reference documents.]")


def _render_shown_link(record: dict, message_index: int) -> None:
    """A link under a bot reply (phase 68): YouTube/video plays, a picture shows, else an Open button."""
    name, url = record["filename"], record["url"]
    kind = show_files.link_kind(url)
    try:
        if kind == show_files.VIDEO_KIND:
            st.video(url)
        elif kind == show_files.IMAGE_KIND:
            st.image(url, caption=name)
        else:
            st.link_button(
                f"Open {name}",
                url,
                icon=":material/open_in_new:",
                # The same link can be shown under several replies, so the key needs the reply too.
                key=f"invitee_shown_link_{record['file_id']}_{message_index}",
                help="Opens in a new tab.",
            )
    except (ValueError, StreamlitAPIException):
        logger.exception("Could not show reference link %s.", record["file_id"])
        st.caption(f":red[{name} couldn't be shown here — open it under Reference documents.]")


def _render_reference_documents(meeting_id: int) -> None:
    """The organiser's shared documents, read-only; nothing when there are none."""
    files = _reference_files(meeting_id)
    if not files:
        return
    with st.expander("Reference documents", icon=":material/description:"):
        for record in files:
            if show_files.is_link(record):
                st.link_button(
                    record["filename"],
                    record["url"],
                    icon=":material/open_in_new:",
                    key=f"invitee_refdoc_link_{record['file_id']}",
                    help="A link the organiser shared for this meeting; opens in a new tab.",
                )
                continue
            try:
                payload = storage.read_file(record["filepath"])
            except MeetingError:
                st.caption(f":red[{record['filename']} — not available]")
                continue
            st.download_button(
                record["filename"],
                data=payload,
                file_name=record["filename"],
                key=f"invitee_refdoc_dl_{record['file_id']}",
                icon=":material/download:",
                help="A document the organiser shared for this meeting.",
            )


def _render_discussion(
    meeting: Meeting,
    profile: dict,
    invitee: dict,
    chat_session,
    messages: list,
    fields: list,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
    fills: _FillTables,
) -> None:
    """The conversation itself — every discussion agenda item, as one flowing chat."""
    meeting_id = invitee["meeting_id"]
    invitee_id = invitee["invitee_id"]

    # Phase 65: the newest reply is read out by itself — only when the bot spoke last, and
    # never once the chat is closed. The switch is drawn below the messages, so its value is
    # read from session state here (on until the invitee turns it off).
    auto_read = not chat_session.closed and st.session_state.get(AUTO_READ_KEY, True)
    newest = len(messages) - 1
    # Phase 67: the organiser's photo as the bot's icon, and files a reply showed under it.
    bot_avatar = _bot_avatar(meeting)
    files_by_id = {record["file_id"]: record for record in _reference_files(meeting_id)}
    for index, message in enumerate(messages):
        with st.chat_message(
            "assistant" if message.is_from_ai() else "user", avatar=bot_avatar if message.is_from_ai() else None
        ):
            st.write(message.text)
            if message.shown_file_id is not None:
                _render_shown_file(message.shown_file_id, files_by_id.get(message.shown_file_id), index)
            if message.is_from_ai():
                reply_id = f"{meeting_id}-{invitee_id}-{message.message_id or index}"
                listen_button(
                    message.text,
                    key=f"invitee_listen_{message.message_id or index}",
                    reply_id=reply_id,
                    auto=auto_read and index == newest,
                )

    if chat_session.closed:
        return

    current = steps.next_question(meeting, answers, lists.rows)
    if current is not None and fills.waits(current.question):
        st.caption(f":red[Fill in the 📋 {current.question.fill_table} tab and press Save progress to carry on.]")
    elif current is not None:
        st.caption(f":red[{_progress(meeting, current, answers, lists)}]")

    auto_read_switch(key=AUTO_READ_KEY)
    speak_button("invitee_chat_input", key="invitee_speak", turn=len(messages))
    prompt = st.chat_input("Type your reply", key="invitee_chat_input")
    if prompt:
        _handle_turn(
            meeting,
            profile,
            invitee,
            chat_session.running_summary,
            messages,
            prompt,
            fields,
            answers,
            lists,
            fills,
        )


def _render_table_tab(meeting_id: int, invitee_id: int, item, closed: bool, on_saved=None) -> None:
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
    if table.editable_columns and not closed:
        st.markdown(
            ":material/edit: **You can fill in:** "
            + ", ".join(tables.describe_column(column, table.rule_for(column)) for column in table.editable_columns)
            + " — the other columns are for reference. Existing values are shown; change them or leave them."
        )

    edited = st.data_editor(
        tables.display_frame(table, responses),
        num_rows="fixed",
        column_config=_grid_column_config(table),
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
        if _handle_save_table(table, invitee_id, edited) and on_saved is not None:
            on_saved(item.item)


def _grid_column_config(table: AgendaTable) -> dict:
    """Each editable column as its own kind of cell (phase 59): numbers, a calendar, a dropdown."""
    today = datetime.date.today()
    return {column: _column_cell(column, table.rule_for(column), today) for column in table.editable_columns}


def _column_cell(column: str, rule: ColumnRule, today: datetime.date):
    question = tables.column_question(column, rule)
    tip = tables.describe_column(column, rule)
    if rule.answer_type == ANSWER_NUMBER:
        return st.column_config.NumberColumn(help=tip)
    if rule.answer_type == ANSWER_DATE:
        first, last = steps.date_limits(question, today)
        return st.column_config.DateColumn(help=tip, format="DD-MM-YYYY", min_value=first, max_value=last)
    if rule.answer_type == ANSWER_YES_NO:
        # A dropdown, not a tick box: a tick box has no blank, so every row would look filled.
        return st.column_config.SelectboxColumn(help=tip, options=["Yes", "No"])
    if rule.answer_type == ANSWER_CHOICE:
        return st.column_config.SelectboxColumn(help=tip, options=steps.choice_options(rule.rule))
    return st.column_config.TextColumn(help=tip, max_chars=steps.max_length(question))


def _handle_save_table(table: AgendaTable, invitee_id: int, edited) -> bool:
    """Stores the invitee's rows once every column rule passes (phase 59). Explicit rather than
    per-cell, as spec 3a asks; on a problem nothing is saved and the grid keeps what was typed.
    True once saved."""
    rows, problems = tables.check_rows(table, tables.responses_from_frame(table, edited), datetime.date.today())
    if problems:
        shown = "\n".join(f"- {problem}" for problem in problems[:10])
        more = f"\n- …and {len(problems) - 10} more" if len(problems) > 10 else ""
        logger.info("Table %s rows refused for invitee %s: %s", table.table_id, invitee_id, problems)
        st.error(f"Nothing saved yet — please fix these:\n\n{shown}{more}", icon=":material/error:")
        return False
    try:
        saved = db.save_table_responses(table.table_id, invitee_id, rows)
    except MeetingError as error:
        logger.exception("Could not save table rows for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return False

    st.success(tables.completion_label(table, saved) + " saved.", icon=":material/check_circle:")
    return True


def _open_conversation(
    meeting: Meeting,
    profile: dict,
    invitee: dict,
    fields: list,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
    fills: _FillTables,
) -> None:
    """Generates and stores the AI's opening message, then reruns to show it.

    A meeting with question steps opens by asking the first one, so the invitee's first
    reply is already an answer to something the app is tracking.
    """
    meeting_id = invitee["meeting_id"]
    invitee_id = invitee["invitee_id"]
    first = steps.next_question(meeting, answers, lists.rows)
    guidance = ""
    if first is not None:
        guidance = _step_guidance(
            meeting,
            first,
            answers,
            lists,
            fills,
            just_happened="Open with a short welcome in character and say why this conversation is happening.",
        )
        guidance = _with_table_note(meeting, guidance, first.item, fills)
    ref_files = _reference_files(meeting_id)
    try:
        with st.spinner("Starting the conversation..."):
            opening = chat_agent.opening_message(
                meeting,
                profile,
                evaluation_fields=fields,
                step_guidance=guidance,
                step_tag=first.item if first is not None else "",
                faq=_meeting_faq(meeting_id),
                invitee_name=invitee.get("name", ""),
                sheets=_table_sheets(meeting, meeting_id),
                ref_files=ref_files,
            )
        db.add_message(
            meeting_id, invitee_id, SENDER_AI, opening.reply, opening.agenda_tag,
            shown_file_id=_shown_file_id(opening, ref_files, meeting, []),
        )
        _note_model(meeting_id, invitee_id, profile)
    except (LLMConnectionError, MeetingError) as error:
        logger.exception("Could not open the conversation for invitee %s.", invitee_id)
        st.error(f"We couldn't start the conversation: {error}", icon=":material/error:")
        return
    st.rerun()


def _handle_turn(
    meeting: Meeting,
    profile: dict,
    invitee: dict,
    summary_so_far: str,
    messages: list,
    prompt: str,
    fields: list,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
    fills: _FillTables,
    table_saved: bool = False,
) -> None:
    """Saves the invitee's message, replies to it, then folds if the history has grown.

    The invitee's own message is written **first and on its own**, so a provider failure
    costs them the reply and not what they typed. While a question step is open the reply is
    steered by `_advance_question`; a failure while reading the answer uses no try.

    A step waiting for its Fill table reads no answer (phase 70); `table_saved` is the
    "I have filled in…" message posted on saving that table, which starts the step.
    """
    meeting_id = invitee["meeting_id"]
    invitee_id = invitee["invitee_id"]
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
    ref_files = _reference_files(meeting_id)
    try:
        with st.spinner("Thinking..."):
            guidance, step_tag = "", ""
            if current is not None and table_saved:
                guidance = _step_guidance(
                    meeting, current, answers, lists, fills, with_document=False,
                    just_happened=f"The invitee has just filled in and saved the {current.question.fill_table} "
                    "table. Thank them in a few words, then start this step.",
                )
                step_tag = current.item
            elif current is not None and fills.waits(current.question):
                guidance = _step_guidance(
                    meeting, current, answers, lists, fills, with_document=not _was_asked(messages, current),
                    just_happened="Reply briefly to what the invitee just said.",
                )
                step_tag = current.item
            elif current is not None and not _was_asked(messages, current):
                guidance, step_tag = _ask_new_question(meeting, current, answers, lists, fills), current.item
            elif current is not None and current.is_discussion():
                guidance, step_tag, fallback = _advance_discussion(
                    meeting, profile, meeting_id, invitee_id, current, answers, prompt, lists, messages, fills
                )
            elif current is not None:
                guidance, step_tag, fallback = _advance_question(
                    meeting, profile, meeting_id, invitee_id, current, answers, prompt, lists, fills
                )
            guidance = _with_table_note(meeting, guidance, step_tag, fills)
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
                invitee_name=invitee.get("name", ""),
                sheets=_table_sheets(meeting, meeting_id),
                ref_files=ref_files,
            )
        db.add_message(
            meeting_id, invitee_id, SENDER_AI, turn.reply, turn.agenda_tag,
            shown_file_id=_shown_file_id(turn, ref_files, meeting, messages),
        )
        _note_model(meeting_id, invitee_id, profile)
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
    fills: _FillTables,
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
            _progress(meeting, current, answers, lists),
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

    return _after_step(meeting, current, {**answers, current.key: updated}, lists, fills, happened, plain)


def _advance_discussion(
    meeting: Meeting,
    profile: dict,
    meeting_id: int,
    invitee_id: int,
    current: steps.Step,
    answers: dict[str, StepAnswer],
    reply: str,
    lists: loops.InviteeLists,
    messages: list,
    fills: _FillTables,
) -> tuple[str, str, str]:
    """Records one reply on the open discussion step and moves on once it is covered (phase 69).

    The model only says covered or not (`read_discussion`); `steps.record_discussion_reply`
    also moves on after `DISCUSSION_REPLY_LIMIT` replies. Returns what `_advance_question` does.

    Raises:
        LLMConnectionError: if reading the reply fails.
        MeetingError: if the step can't be saved.
    """
    reading = chat_agent.read_discussion(profile, current.question, running_summary.recent_messages(messages), reply)
    updated = steps.record_discussion_reply(current.question, answers.get(current.key), reading.covered, reply, key=current.key)
    db.save_step_answer(meeting_id, invitee_id, updated)
    if updated.status != STEP_ANSWERED:
        guidance = chat_agent.discussion_guidance(
            current.question,
            just_happened="Reply to what the invitee just said and carry on with this item. If they "
            "asked something, answer it briefly from your instructions (say so if you don't know).",
        )
        return guidance, current.item, ""
    happened = "This agenda item is covered now. Thank them in a few words."
    return _after_step(
        meeting, current, {**answers, current.key: updated}, lists, fills, happened, "Thanks for sharing that."
    )


def _after_step(
    meeting: Meeting,
    current: steps.Step,
    after: dict[str, StepAnswer],
    lists: loops.InviteeLists,
    fills: _FillTables,
    happened: str,
    plain: str,
) -> tuple[str, str, str]:
    """The guidance, tag and plain fallback reply once `current` has been saved as `after`."""
    upcoming = steps.next_question(meeting, after, lists.rows)
    if upcoming is None:
        return (
            chat_agent.finished_questions_guidance(meeting, just_happened=happened),
            current.item,
            f"{plain} That's everything on my agenda.",
        )
    if upcoming.key != current.key:
        plain += f" Next: {_plain_question(upcoming, lists, fills)}"
    return _step_guidance(meeting, upcoming, after, lists, fills, just_happened=happened), upcoming.item, plain


def _step_guidance(
    meeting: Meeting,
    step: steps.Step,
    answers: dict[str, StepAnswer],
    lists: loops.InviteeLists,
    fills: _FillTables,
    just_happened: str = "",
    with_document: bool = True,
) -> str:
    """What the chat model is told to do for this step: ask the question, discuss the item, or
    (phase 70) first have the invitee fill in its table. `with_document` adds the line about the
    step's Show document, for the reply that starts the step."""
    item = step.question
    if fills.waits(item):
        guidance = chat_agent.fill_table_guidance(item, fills.columns(item.fill_table), just_happened=just_happened)
    elif step.is_discussion():
        guidance = chat_agent.discussion_guidance(item, just_happened=just_happened)
    else:
        guidance = chat_agent.question_guidance(
            item,
            _progress(meeting, step, answers, lists),
            just_happened=just_happened,
            row_context=lists.row_context(step),
        )
    if with_document and item.show_file:
        guidance += " " + chat_agent.document_note(item.show_file)
    return guidance


def _plain_question(step: steps.Step, lists: loops.InviteeLists, fills: _FillTables) -> str:
    """The question (or discussion item) in plain words, for when the model can't word it."""
    if fills.waits(step.question):
        return f"please fill in the {step.question.fill_table} tab and press Save progress."
    if step.is_discussion():
        return f"let's talk about {step.item}."
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


def _ask_new_question(
    meeting: Meeting, current: steps.Step, answers: dict, lists: loops.InviteeLists, fills: _FillTables
) -> str:
    """The step the invitee hasn't been taken to yet: a new row, or the next one after a reload."""
    return _step_guidance(
        meeting,
        current,
        answers,
        lists,
        fills,
        just_happened="Reply briefly to what the invitee just said, then start this step. Their "
        "message was not an answer to it.",
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


def _render_uploads(meeting_id: int, invitee_id: int, closed: bool = False) -> None:
    with st.expander("Attach a file", icon=":material/attach_file:"):
        uploaded = None
        if not closed:
            uploaded = st.file_uploader(
                "Upload",
                accept_multiple_files=True,
                key="invitee_upload",
                help="Stored privately against your own conversation.",
            )
        if uploaded and st.button(
            "Save files",
            key="invitee_upload_save",
            icon=":material/upload:",
            help="Keep these files with your conversation for the organiser to see.",
        ):
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
