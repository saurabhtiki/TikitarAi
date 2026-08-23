"""Saved report data in session state — the only module in `reports/` that imports Streamlit.

Same convention as `engine/session.py` and `analyst/session.py`: everything else in the
package stays pure, so saving, reading and serialising a dataset are all testable without
`AppTest`.

**The one place this app uses `st.cache_resource` on purpose.** `engine/session.py`
documents at length why the *session's* DuckDB connection must never be process-wide: one
user's uploaded files appearing in another user's session would be a data leak. A saved
report dataset is the opposite case by design — the data is shared, every account is meant
to see the same rows, and DuckDB allows only one connection to write a file. So exactly one
connection per report file is opened for the whole process and handed to every session.

Each session takes its own `cursor()` off that shared connection rather than using it
directly, because Streamlit answers every session on its own thread and a single DuckDB
connection is not meant to be driven by several at once. A cursor is a lightweight second
handle onto the same database, which is precisely what is wanted: same data, separate
statement state.

The transcript here is deliberately thinner than `analyst/session.py`'s. This chat is
on-the-spot Q&A over shared data: nothing is written to the dashboard, and nothing is
stored, so there is no Agno session store and no pinning.
"""

import logging
import threading
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

from analyst.session import ChatMessage, ROLE_ASSISTANT, ROLE_USER
from engine import dictionary
from engine import session as engine_session
from reports import db as reports_db
from reports import store as reports_store
from reports.exceptions import ReportDataError
from reports.model import ReportSetup

logger = logging.getLogger(__name__)

CR_REPORT_KEY = "cr_report"
CR_SETUP_KEY = "cr_setup"
CR_MESSAGES_KEY = "cr_messages"
CR_PENDING_KEY = "cr_pending_question"
CR_CURSOR_KEY = "cr_cursor"
CR_CURSOR_REPORT_KEY = "cr_cursor_report"

# How many answers keep their table and chart. Older ones keep their text and SQL — the
# same bound `analyst.session` sets, and for the same reason: a list of dataframes in
# session state is a memory leak with a chat interface attached.
FULL_PAYLOAD_MESSAGES = 12

# Guards the registry below. Two sessions asking for the same report's connection at the
# same moment are two threads, and both opening the file would be the one thing DuckDB
# refuses.
_REGISTRY_LOCK = threading.Lock()


# --------------------------------------------------------------------------------------
# The shared connection to one report's data file
# --------------------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _open_stores() -> dict:
    """The process-wide registry of open dataset connections, keyed by file path.

    A registry rather than one cached function per report, so that closing a file is
    possible: a cached function can only be *called*, and calling it to find out whether a
    connection exists would create the very file a delete is about to remove.
    """
    return {}


def _store_key(task_id: int, root: Path | str) -> str:
    """Which file a connection is to, stated so two names for one directory agree."""
    return str(reports_store.dataset_path(task_id, root).resolve())


def store_connection(
    task_id: int, root: Path | str = reports_store.DEFAULT_DATASET_ROOT
) -> duckdb.DuckDBPyConnection:
    """The process's single writable connection to a report's data file.

    Shared across sessions on purpose — see the module docstring. Keyed on the file's
    **resolved** path rather than the report id: `root` defaults to a relative path, and two
    different directories reached by the same relative name are two different files that
    must not share one connection.

    Raises:
        ReportDataError: if the file can't be opened.
    """
    key = _store_key(task_id, root)
    registry = _open_stores()
    with _REGISTRY_LOCK:
        connection = registry.get(key)
        if connection is None:
            connection = reports_store.open_store(task_id, root)
            registry[key] = connection
            logger.info("Opened the shared dataset connection for report %s.", task_id)
    return connection


def release_store(task_id: int, root: Path | str = reports_store.DEFAULT_DATASET_ROOT) -> None:
    """Closes and forgets the shared connection to one report's file, if one is open.

    Needed before the file itself can be deleted: on Windows an open handle makes the file
    undeletable, so "delete the report" has to let go of it first.
    """
    key = _store_key(task_id, root)
    with _REGISTRY_LOCK:
        connection = _open_stores().pop(key, None)

    if connection is not None:
        reports_store.close_store(connection)
        logger.info("Released the shared dataset connection for report %s.", task_id)

    if st.session_state.get(CR_CURSOR_REPORT_KEY) == int(task_id):
        st.session_state.pop(CR_CURSOR_KEY, None)
        st.session_state.pop(CR_CURSOR_REPORT_KEY, None)


def read_connection(
    task_id: int, root: Path | str = reports_store.DEFAULT_DATASET_ROOT
) -> duckdb.DuckDBPyConnection:
    """This session's own handle onto a report's data, created on first use.

    A `cursor()` off the shared connection, kept in session state so a rerun doesn't open a
    new one per keystroke, and replaced when the user switches report.

    Raises:
        ReportDataError: if the file can't be opened.
    """
    task_id = int(task_id)
    if st.session_state.get(CR_CURSOR_REPORT_KEY) == task_id:
        existing = st.session_state.get(CR_CURSOR_KEY)
        if existing is not None:
            return existing

    try:
        cursor = store_connection(task_id, root).cursor()
    except duckdb.Error as error:
        logger.exception("Could not take a read handle onto report %s's data.", task_id)
        raise ReportDataError("This report's saved data couldn't be opened.") from error

    st.session_state[CR_CURSOR_KEY] = cursor
    st.session_state[CR_CURSOR_REPORT_KEY] = task_id
    return cursor


# --------------------------------------------------------------------------------------
# Saving a run's data
# --------------------------------------------------------------------------------------


def current_setup() -> ReportSetup:
    """What the session's loaded tables would be saved as.

    Read straight out of the Data Engine: the table names, the dictionary entries and the
    confirmed links. A run has just rewritten all three to the report's own (requirement
    8.2 step 1), which is why this is worth saving — it is the report's schema, not
    whatever the person happened to have loaded before.
    """
    return ReportSetup(
        tables=[],
        dictionary=list(engine_session.get_dictionary()),
        relationships=list(engine_session.get_relationships()),
    )


def save_current_data(
    task_id: int,
    refreshed_by: str,
    *,
    root: Path | str = reports_store.DEFAULT_DATASET_ROOT,
    db_path: Path | str = reports_db.DEFAULT_DB_PATH,
) -> ReportSetup:
    """Replaces one report's saved data with what is loaded in this session right now.

    The rows go to the report's DuckDB file, the schema and the "who and when" go to
    SQLite, and both describe the same moment — so the chat can never show a refresh time
    that belongs to different rows.

    Raises:
        ReportDataError: if there is nothing loaded, or either half can't be written.
    """
    table_names = engine_session.table_names()
    if not table_names:
        raise ReportDataError("There are no tables loaded to save for this report.")

    store = store_connection(task_id, root)
    written = reports_store.write_tables(store, engine_session.connection(), table_names)

    setup = current_setup()
    setup.tables = written
    reports_db.record_dataset(task_id, setup, refreshed_by, db_path)

    logger.info(
        "Saved %s table(s) and %s row(s) as report %s's current data.",
        len(written),
        setup.total_rows(),
        task_id,
    )
    return setup


def drop_saved_data(
    task_id: int,
    *,
    root: Path | str = reports_store.DEFAULT_DATASET_ROOT,
    db_path: Path | str = reports_db.DEFAULT_DB_PATH,
) -> None:
    """Removes a report's saved data entirely — the file and the record of it.

    Called when a report is deleted. The connection is released first, because a file
    still open cannot be removed.

    Raises:
        ReportDataError: if the file or its record can't be removed.
    """
    release_store(task_id, root)
    reports_store.delete_dataset(task_id, root)
    reports_db.forget_dataset(task_id, db_path)


# --------------------------------------------------------------------------------------
# Which report the chat is open on
# --------------------------------------------------------------------------------------


def open_report(
    task_id: int,
    name: str,
    *,
    root: Path | str = reports_store.DEFAULT_DATASET_ROOT,
    db_path: Path | str = reports_db.DEFAULT_DB_PATH,
) -> ReportSetup:
    """Opens one report's saved data for chatting, and starts a fresh conversation.

    The transcript is cleared rather than carried over: the questions above it were asked
    of a different report's tables, and leaving them there invites a follow-up that can't
    be answered.

    Raises:
        ReportDataError: if the report has no saved data, or it can't be opened.
    """
    setup = reports_db.load_setup(task_id, db_path)
    read_connection(task_id, root)

    st.session_state[CR_REPORT_KEY] = {"task_id": int(task_id), "name": str(name or "")}
    st.session_state[CR_SETUP_KEY] = setup
    clear_messages()
    logger.info("Opened report %s for chat with %s table(s).", task_id, len(setup.tables))
    return setup


def current_report() -> dict | None:
    """The report the chat is open on — `{task_id, name}` — or None."""
    return st.session_state.get(CR_REPORT_KEY)


def current_report_id() -> int | None:
    report = current_report()
    return report["task_id"] if report else None


def close_report() -> None:
    """Goes back to the picker, forgetting the conversation with it."""
    st.session_state.pop(CR_REPORT_KEY, None)
    st.session_state.pop(CR_SETUP_KEY, None)
    st.session_state.pop(CR_CURSOR_KEY, None)
    st.session_state.pop(CR_CURSOR_REPORT_KEY, None)
    clear_messages()


def setup() -> ReportSetup | None:
    """The open report's saved schema, or None if no report is open."""
    return st.session_state.get(CR_SETUP_KEY)


def schema_context() -> str:
    """The schema block the agent receives with every question about this report.

    The same `engine.dictionary.schema_context` Chat with Data uses, given the saved
    entries and the saved links instead of the session's — so a question asked of stored
    data meets exactly the context it would have met the day the report ran: every table,
    every column with its type and meaning, and the joins spelled out as column pairs.
    """
    open_setup = setup()
    if open_setup is None:
        return "No report is open."
    return dictionary.schema_context(open_setup.dictionary, open_setup.relationships)


def table_names() -> list[str]:
    """The open report's saved table names."""
    open_setup = setup()
    return open_setup.table_names() if open_setup else []


# --------------------------------------------------------------------------------------
# The transcript
# --------------------------------------------------------------------------------------


def get_messages() -> list[ChatMessage]:
    """The conversation so far, oldest first."""
    return st.session_state.setdefault(CR_MESSAGES_KEY, [])


def append_message(message: ChatMessage) -> None:
    """Adds a turn and releases the attachments that have scrolled out of reach."""
    messages = get_messages()
    messages.append(message)
    with_payload = [item for item in messages if item.frame is not None or item.figure is not None]
    for item in with_payload[:-FULL_PAYLOAD_MESSAGES]:
        item.release_payload()


def clear_messages() -> None:
    """Empties the conversation, including any question still waiting to be answered."""
    st.session_state.pop(CR_MESSAGES_KEY, None)
    st.session_state.pop(CR_PENDING_KEY, None)


def ask(question: str) -> None:
    """Records a question and shows it immediately, to be answered on the next run.

    The same two-step `chat_with_data.py` uses: the question is painted, then the model is
    called underneath it, rather than the page hanging on a model call before anything is
    drawn.
    """
    question = str(question or "").strip()
    if not question:
        return
    append_message(ChatMessage(role=ROLE_USER, text=question))
    st.session_state[CR_PENDING_KEY] = question


def pending_question() -> str | None:
    return st.session_state.get(CR_PENDING_KEY)


def clear_pending_question() -> None:
    st.session_state.pop(CR_PENDING_KEY, None)


def last_result() -> tuple[pd.DataFrame | None, str | None]:
    """The rows and SQL behind the most recent answer that queried anything.

    What "now show that as a pie chart" refers to — see
    `analyst.pipeline._chart_the_previous_result`.
    """
    for message in reversed(get_messages()):
        if message.role != ROLE_ASSISTANT or message.is_error:
            continue
        if message.frame is not None and not message.frame.empty:
            return message.frame, message.sql
    return None, None
