"""The chat transcript in session state (requirement 6.1).

The only module in `analyst/` that imports Streamlit, matching the convention
`cleaner/session.py` and `engine/session.py` already set — every other module in the
package is testable without `AppTest`.

Kept in its own `an_*` namespace rather than folded into `engine/session.py`, because the
transcript and the data are not the same thing: a user can reload files without wanting
their questions erased, and clearing the chat should not drop their tables.

Two different lifetimes live here, and the difference matters:

- **The rendered transcript** is session-only, exactly as requirement 6.1 describes. It
  holds result frames and Plotly figures so it can be replayed on every rerun without
  re-running any SQL, which is also why it is bounded — a session-state list of unbounded
  dataframes is a memory leak with a chat interface attached.
- **The agent's own session records** are written to disk by Agno, so past conversations
  can be listed back to the user later. This is a deliberate departure from requirement
  6's "nothing here is written to the database": what is stored is the questions and
  answers, never the uploaded data, and a stored conversation cannot be re-run because
  the tables it described are gone with the session.
"""

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from agno.db.sqlite import SqliteDb
from sqlalchemy.exc import SQLAlchemyError

from analyst.exceptions import ChatStorageError

logger = logging.getLogger(__name__)

# Agno's own file, beside `data/tikitarai.db` rather than inside it: Agno creates and
# migrates its session schema itself, and it has no business doing that in the same file
# as the users and LLM profile tables this app owns.
CHAT_DB_PATH = Path("data") / "chat_sessions.db"

# Agno names its tables from these, so they are part of the stored schema — changing one
# later starts a fresh table and orphans the history already written.
CHAT_SESSION_TABLE = "chat_sessions"

AN_MESSAGES_KEY = "an_messages"
AN_PENDING_KEY = "an_pending_question"
AN_DIALOG_KEY = "an_open_dialog"
AN_AGENT_DB_KEY = "an_agent_db"
AN_AGENT_SESSION_KEY = "an_agent_session_id"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# How many answered messages keep their full payload. Older ones keep their text and SQL —
# still readable, still copyable — but release their frame and figure.
FULL_PAYLOAD_MESSAGES = 12


@dataclass
class ChatMessage:
    """One turn in the transcript.

    Attributes:
        role: `user` or `assistant`.
        text: the question, or the assistant's written answer.
        question: the question this answers, kept so the automatic chart can be worked out
            again when the user resets their customizations.
        sql: the statement(s) behind the answer, shown for transparency (requirement 5.4).
        frame: the rows the answer was built from, or None.
        figure: the Plotly figure, or None.
        choices: what that figure plots, so the customize controls open showing what is
            actually on screen rather than resetting to a guess.
        style: how it is drawn — title, colours, stacking. Separate from `choices` so it
            survives a change of chart type.
        outputs: which of requirement 6.2's output types this message renders.
        warnings: anything worth saying that isn't a failure.
        chart_warnings: the subset describing the chart, replaced wholesale when the user
            redraws it — the others still apply and are left alone.
        is_error: renders the message as a failure rather than an answer.
        pinned_item_id: the id of the dashboard item this answer was pinned as, or None.
            An opaque string here on purpose — it is what stops a second press of "Pin to
            Dashboard" making a second copy of the same answer, and recording it on the
            message is what makes that survive a rerun. `analyst/` does not import
            `dashboard/` to read it; the page checks whether the id is still in the report,
            so discarding the pinned copy offers the button again.
    """

    role: str
    text: str = ""
    question: str = ""
    sql: str | None = None
    frame: pd.DataFrame | None = None
    figure: Any = None
    choices: Any = None
    style: Any = None
    outputs: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    chart_warnings: list[str] = field(default_factory=list)
    is_error: bool = False
    pinned_item_id: str | None = None

    def release_payload(self) -> None:
        """Drops the heavy attachments, keeping the readable parts.

        The choices and style go with them: without the frame there is nothing left to
        redraw, so keeping them would only offer controls that couldn't do anything.
        """
        self.frame = None
        self.figure = None
        self.choices = None
        self.style = None


def get_messages() -> list[ChatMessage]:
    """The transcript, oldest first."""
    return st.session_state.setdefault(AN_MESSAGES_KEY, [])


def append_message(message: ChatMessage) -> None:
    """Adds a turn to the transcript and releases payloads that have scrolled out of reach."""
    messages = get_messages()
    messages.append(message)
    _trim_payloads(messages)


def _trim_payloads(messages: list[ChatMessage]) -> None:
    """Keeps only the most recent `FULL_PAYLOAD_MESSAGES` answers holding their data."""
    with_payload = [message for message in messages if message.frame is not None or message.figure is not None]
    for message in with_payload[:-FULL_PAYLOAD_MESSAGES]:
        message.release_payload()


def last_result() -> tuple[pd.DataFrame | None, str | None]:
    """The rows and SQL of the most recent answer that queried anything, or `(None, None)`.

    What "show that as a chart" refers to when the model declines to re-run the query
    itself — see `analyst.pipeline._chart_the_previous_result`. Only answers still holding
    their payload qualify: `_trim_payloads` releases the older ones, and a question about
    rows that scrolled that far out of the transcript is not a follow-up any more.
    """
    for message in reversed(get_messages()):
        if message.role != ROLE_ASSISTANT or message.is_error:
            continue
        if message.frame is not None and not message.frame.empty:
            return message.frame, message.sql
    return None, None


def clear_messages() -> None:
    """Empties the transcript. Also drops any question still waiting to be answered, so
    clearing mid-run doesn't leave one to be answered into an empty chat."""
    st.session_state.pop(AN_MESSAGES_KEY, None)
    st.session_state.pop(AN_PENDING_KEY, None)
    # The agent keeps its own copy of the conversation, and a cleared chat that the model
    # still remembers is worse than no clear button at all — the next answer would refer
    # back to questions the user can no longer see.
    start_new_agent_session()


# --------------------------------------------------------------------------------------
# Agno session storage
# --------------------------------------------------------------------------------------


def agent_db(db_path: Path | str = CHAT_DB_PATH) -> SqliteDb:
    """The Agno database backing the agent's own session records.

    On disk, so past conversations can be listed back to the user later — the runs Agno
    writes here are the raw material for a chat history screen. Note that this outlives
    the DuckDB tables a conversation was about, which are session-only: a restored chat is
    a record of what was asked, not something that can be re-run.

    The handle is cached in session state because building it opens a SQLAlchemy engine
    and checks the schema; doing that on every rerun would put a file check in front of
    every keystroke.

    Raises:
        ChatStorageError: if the file can't be created or opened. The caller is expected
            to carry on without history rather than lose the chat itself.
    """
    database = st.session_state.get(AN_AGENT_DB_KEY)
    if database is not None:
        return database

    path = Path(db_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        database = SqliteDb(db_file=str(path), session_table=CHAT_SESSION_TABLE)
    except (SQLAlchemyError, OSError) as error:
        logger.exception("Could not open the chat session store at %s.", path)
        raise ChatStorageError(
            f"Chat history couldn't be opened at {path} ({error}). Your questions will "
            "still be answered, but this conversation won't be saved."
        ) from error

    st.session_state[AN_AGENT_DB_KEY] = database
    logger.info("Opened the Agno chat session store at %s.", path)
    return database


def agent_session_id() -> str:
    """The id tying this chat's runs together, so follow-ups like 'now as a chart' resolve."""
    session_id = st.session_state.get(AN_AGENT_SESSION_KEY)
    if not session_id:
        session_id = f"chat-{uuid.uuid4().hex[:12]}"
        st.session_state[AN_AGENT_SESSION_KEY] = session_id
    return session_id


def start_new_agent_session() -> None:
    """Begins a new stored conversation, leaving the previous one on disk.

    Rotating the id rather than deleting anything is what makes "Clear chat" safe now the
    store is persistent: the model starts clean, and the conversation just cleared is
    still there to be listed back later.
    """
    st.session_state.pop(AN_AGENT_SESSION_KEY, None)


def set_pending_question(question: str) -> None:
    """Records a question to answer on the next run.

    Answering happens on the run *after* the one that captured the input, so the user's
    message is painted immediately and the spinner appears under it — rather than the page
    freezing on a blank chat while the model thinks.
    """
    st.session_state[AN_PENDING_KEY] = question


def pending_question() -> str | None:
    return st.session_state.get(AN_PENDING_KEY)


def clear_pending_question() -> None:
    st.session_state.pop(AN_PENDING_KEY, None)


def open_dialog(action: str, payload: dict | None = None) -> None:
    """Flags which Actions dialog should be showing.

    Held in session state rather than read from the menu's return value, for the reason
    `engine/session.py::open_dialog` documents: a dialog containing widgets closes
    mid-edit if it depends on a control's return value surviving the rerun.
    """
    st.session_state[AN_DIALOG_KEY] = {"action": action, "payload": payload or {}}


def close_dialog() -> None:
    st.session_state.pop(AN_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    """The open dialog's `(action, payload)`, or None."""
    pending = st.session_state.get(AN_DIALOG_KEY)
    if not pending:
        return None
    return pending["action"], pending.get("payload", {})


def reset_chat() -> None:
    """Drops every `an_*` key. Called when the engine is reset — a transcript about tables
    that no longer exist reads as if it still applies to whatever is loaded next."""
    for key in (AN_MESSAGES_KEY, AN_PENDING_KEY, AN_DIALOG_KEY, AN_AGENT_DB_KEY, AN_AGENT_SESSION_KEY):
        st.session_state.pop(key, None)
