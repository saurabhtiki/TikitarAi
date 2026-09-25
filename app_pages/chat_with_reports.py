"""Chat with Reports — ask questions of a report's current data, without uploading it (Phase 13).

Running a report saves the data it ran on (`reports/session.py`). This page opens that
saved data and lets anyone ask questions of it: no uploads, no setup steps, no waiting for
files. Pick a report, see when its data was last refreshed and by whom, and ask.

**Shared on purpose.** Every report that has data is listed here, whoever built it. A
report's recipe stays owned by the account that made it, but the data it produced is the
month's numbers and anyone who can log in may ask questions of them.

**Read-only, and that is enforced rather than assumed.** Chat with Data lets a question
change a column (requirement 5.4) because the tables there belong to that one session. Here
they don't — one person's "remove the blank rows" would rewrite what everyone else is
reading — so a message that looks like a column change is answered with a refusal instead.
The agent itself only ever had read-only tools (`analyst.tools.READ_ONLY_TOOLS`), and its
SQL still passes `engine.guards` on the way to DuckDB.

**Nothing is kept.** No transcript is stored and there is no Pin to Dashboard: this is
on-the-spot Q&A. Anything worth keeping is built in Chat with Data, which is where the
Dashboard's pinning lives.

The agent is given the report's saved schema — every table, every column with its type,
description and synonyms, and the confirmed links written out as joinable pairs. That is
the identical `engine.dictionary.schema_context` block Chat with Data builds, rebuilt from
storage rather than from session state, so a stored report answers the way a live one does.
"""

import logging

import streamlit as st

from analyst import pipeline, routing
from app_pages import dashboard_viewer
from analyst.session import ChatMessage, ROLE_ASSISTANT
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from auth.service import is_authenticated
from llm import session as llm_session
from reports import db as reports_db
from reports import session as reports_session
from reports.exceptions import ReportDataError
from sidebar import render_sidebar
from utils.dates import show_dataframe

logger = logging.getLogger(__name__)

# What a message asking to change a column is told instead. Written as guidance rather
# than a rejection: the thing they want is possible, just not against shared data.
READ_ONLY_REFUSAL = (
    "This chat can only read a report's data — it's shared with everyone, so a change here "
    "would change what your colleagues see. Ask a question about the numbers instead, or "
    "use **Chat with data** to work on your own copy of the files."
)


if not is_authenticated():
    st.error("Please log in to chat with a report.", icon=":material/lock:")
    st.stop()

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


# --------------------------------------------------------------------------------------
# The picker
# --------------------------------------------------------------------------------------


def _render_picker() -> None:
    """Every report that has data, newest refresh first."""
    try:
        available = reports_db.list_reports_with_data()
    except ReportDataError as error:
        logger.exception("Could not list the reports that have saved data.")
        st.error(str(error), icon=":material/error:")
        return

    if not available:
        st.info(
            "No report has any data saved yet. Run a report once on the **Reports** page "
            "and its data will be waiting here.",
            icon=":material/info:",
        )
        return

    query = st.text_input(
        "Find a report",
        key="cr_search",
        placeholder="Type part of a report's name",
        help="Filters the list below by name. Leave it empty to see every report.",
    )

    matches = _filter_reports(available, query)
    if not matches:
        st.caption(f":orange[No report matches “{query}”.]")
        return

    for row in matches:
        _render_report_row(row)


def _filter_reports(available: list[dict], query: str) -> list[dict]:
    """Case-insensitive match on the report's name and description."""
    needle = str(query or "").strip().lower()
    if not needle:
        return available
    return [
        row
        for row in available
        if needle in str(row.get("name", "")).lower() or needle in str(row.get("description", "")).lower()
    ]


def _render_report_row(row: dict) -> None:
    """One line: the report, when its data was refreshed, and the button in."""
    task_id = int(row["task_id"])
    with st.container(horizontal=True, vertical_alignment="center", border=True):
        with st.container():
            st.markdown(f"**{row.get('name') or 'Untitled report'}**")
            st.caption(_refreshed_line(row))

        if st.button(
            "Chat with data",
            key=f"cr_open_{task_id}",
            icon=":material/chat:",
            type="primary",
            help="Opens this report's saved data and starts a new conversation about it.",
        ):
            _open(task_id, str(row.get("name") or ""))

        # Phase 46: the dashboard saved with the report, over this same data. Offered here
        # too because this list is everyone's, while the Reports list is only the owner's.
        if st.button(
            "Dashboard",
            key=f"cr_dashboard_{task_id}",
            icon=":material/dashboard:",
            help="View or download this report's dashboard with the data above. View only.",
        ):
            dashboard_viewer.open_viewer(
                task_id, str(row.get("name") or ""), back_page="app_pages/chat_with_reports.py"
            )


def _refreshed_line(row: dict) -> str:
    """The one line that stops anyone reasoning about figures without knowing their age."""
    who = str(row.get("refreshed_by") or "").strip() or "someone"
    when = str(row.get("refreshed_at") or "").strip() or "an unknown time"
    rows = int(row.get("row_count") or 0)
    owner = str(row.get("owner_name") or "").strip()
    built = f" · built by {owner}" if owner else ""
    return f"Data last refreshed: {when} by {who} · {rows:,} row(s){built}"


def _open(task_id: int, name: str) -> None:
    """Opens a report for chatting, or says why it couldn't be opened."""
    try:
        reports_session.open_report(task_id, name)
    except ReportDataError as error:
        logger.exception("Could not open report %s for chat.", task_id)
        st.error(str(error), icon=":material/error:")
        return
    st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# The chat
# --------------------------------------------------------------------------------------


def _render_chat_header(report: dict) -> None:
    """Which report is open, how old its data is, and the way back out."""
    task_id = int(report["task_id"])

    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"##### 💬 {report.get('name') or 'Untitled report'}")
        if st.button(
            "Pick another report",
            key="cr_back",
            icon=":material/arrow_back:",
            help="Closes this conversation and goes back to the list of reports.",
        ):
            reports_session.close_report()
            st.rerun(scope="app")

    try:
        info = reports_db.dataset_info(task_id)
    except ReportDataError:
        logger.exception("Could not read the refresh details for report %s.", task_id)
        info = None

    if info is not None:
        st.caption(f":blue[{_refreshed_line(info)}]")

    tables = reports_session.table_names()
    if tables:
        st.caption(f"Tables you can ask about: {', '.join(tables)}")


def _render_transcript() -> None:
    for index, message in enumerate(reports_session.get_messages()):
        _render_message(message, index)


def _render_message(message: ChatMessage, index: int) -> None:
    """Paints one turn. Everything shown was stored when the question was answered, so a
    rerun re-runs no SQL."""
    with st.chat_message(message.role):
        if message.role != ROLE_ASSISTANT:
            st.markdown(message.text)
            return

        if message.is_error:
            st.error(message.text, icon=":material/error:")
            return

        if message.sql:
            with st.expander("SQL that ran", icon=":material/code:"):
                st.code(message.sql, language="sql")

        if message.figure is not None:
            st.plotly_chart(message.figure, key=f"cr_chart_{index}", width="stretch")
            for warning in message.chart_warnings:
                st.caption(f":orange[{warning}]")

        if routing.OUTPUT_DATAFRAME in message.outputs and message.frame is not None:
            show_dataframe(message.frame, key=f"cr_frame_{index}", width="stretch", hide_index=True)

        if message.text:
            st.markdown(message.text)

        for warning in message.warnings:
            st.caption(f":orange[{warning}]")


def _answer_pending_question(active_profile: dict, task_id: int) -> None:
    """Answers the question captured on the previous run, then reruns to paint it.

    Two runs on purpose, the same split `chat_with_data.py` makes: the question appears the
    instant it is sent, and the spinner sits underneath it.
    """
    question = reports_session.pending_question()
    if question is None:
        return

    # Refused before any model is called: a column change against shared data is not a
    # question this page is willing to route, so there is nothing to ask about.
    if routing.looks_like_column_action(question) is not None:
        reports_session.clear_pending_question()
        reports_session.append_message(
            ChatMessage(role=ROLE_ASSISTANT, text=READ_ONLY_REFUSAL, question=question, is_error=True)
        )
        st.rerun(scope="app")

    try:
        connection = reports_session.read_connection(task_id)
    except ReportDataError as error:
        reports_session.clear_pending_question()
        reports_session.append_message(
            ChatMessage(role=ROLE_ASSISTANT, text=str(error), question=question, is_error=True)
        )
        st.rerun(scope="app")

    previous_frame, previous_sql = reports_session.last_result()

    with st.chat_message(ROLE_ASSISTANT):
        with st.spinner("Working through this report's data…"):
            answer = pipeline.answer(
                active_profile,
                connection,
                reports_session.schema_context(),
                question,
                # No `relationships=`: that argument exists only so a conversational column
                # change can reach across a link, and this page refuses those above. The
                # links still reach the agent — they are in the schema context.
                previous_frame=previous_frame,
                previous_sql=previous_sql,
            )

    reports_session.clear_pending_question()
    reports_session.append_message(
        ChatMessage(
            role=ROLE_ASSISTANT,
            text=answer.text,
            question=answer.question,
            sql=answer.sql,
            frame=answer.frame,
            figure=answer.figure,
            choices=answer.choices,
            style=answer.style,
            outputs=answer.outputs,
            warnings=list(answer.warnings),
            chart_warnings=answer.chart_warnings,
            is_error=answer.is_error,
        )
    )
    st.rerun(scope="app")


def _render_chat(user_id: int, report: dict) -> None:
    """The conversation, its input, and the button that clears it."""
    _render_transcript()

    active_profile = llm_session.active_profile(user_id)
    if active_profile is None:
        st.chat_input("Ask a question about this report", key="cr_chat_input", disabled=True)
        st.caption(
            ":orange[Pick a session model in the sidebar first — that's the model that "
            "answers your questions.]"
        )
        return

    # Ends in a rerun whenever there was a question, so everything below only runs with the
    # transcript already up to date.
    _answer_pending_question(active_profile, int(report["task_id"]))

    # No `help=` — `st.chat_input` is the one widget in this app with no tooltip parameter,
    # so the guidance goes in the caption underneath it.
    question = st.chat_input("Ask a question about this report", key="cr_chat_input")
    st.caption("Ask for a number, a table or a chart — e.g. *top 10 customers by sales this month*.")

    if question:
        reports_session.ask(question)
        st.rerun(scope="app")

    if reports_session.get_messages():
        if st.button(
            "Clear this chat",
            key="cr_clear",
            icon=":material/delete_sweep:",
            help="Empties this conversation. The report's data is not affected.",
        ):
            reports_session.clear_messages()
            st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


if profile is not None:
    render_sidebar(profile)
    user_id = st.session_state["user_id"]

    st.subheader("💬 Chat with reports")
    st.write(
        ":blue[**Pick any report and ask questions about the data it last ran on — "
        "nothing to upload, and the answers use that report's own links and column "
        "meanings.**]"
    )

    open_report = reports_session.current_report()

    if open_report is None:
        _render_picker()
    else:
        _render_chat_header(open_report)
        st.divider()
        _render_chat(user_id, open_report)
