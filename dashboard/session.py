"""The report in session state (requirement 6.3).

The only module in `dashboard/` that imports Streamlit, matching the convention
`cleaner/session.py`, `engine/session.py` and `analyst/session.py` already set — the tree,
the exporters and the stylesheet validator are all testable without `AppTest`.

Kept in its own `db_*` namespace rather than folded into `analyst/session.py`, because a
pinned item and a chat message have different lifetimes and that difference is the whole
point of this module. `analyst.session._trim_payloads` releases the frame and figure of
transcript messages older than `FULL_PAYLOAD_MESSAGES`, so a chat scrolls without growing
without bound. A pinned item must survive that: the user pinned it precisely because they
want to keep it. So `pin` **copies** the frame out of the message rather than referencing
the message, and the copy is what the report owns from then on.

Nothing here is written to disk. Requirement 6.3 is explicit that the dashboard lives for
the session only — reloading the browser starts a new report, and that is the design, not
a gap. Saving reports is Task Builder's job (requirement 7).
"""

import logging

import streamlit as st

from analyst.session import ChatMessage
from dashboard.model import PinnedItem, Report

logger = logging.getLogger(__name__)

DB_REPORT_KEY = "db_report"
DB_DIALOG_KEY = "db_open_dialog"
DB_CSS_KEY = "db_accepted_css"
DB_PRESET_KEY = "db_css_preset"
DB_VIEW_KEY = "db_view"


def get_report() -> Report:
    """The report being built. Created empty on first access."""
    report = st.session_state.get(DB_REPORT_KEY)
    if report is None:
        report = Report()
        st.session_state[DB_REPORT_KEY] = report
    return report


def set_title(title: str) -> None:
    get_report().title = str(title or "").strip()


def pin(message: ChatMessage) -> PinnedItem:
    """Copies a chat answer into the unplaced pool.

    Nothing is asked of the user here — requirement 6.1 step 3 puts pinning in the middle
    of a conversation, and a dialog asking "which subsection?" before the user has built
    any would stop the chat dead. The item lands in the pool with its heading already set
    to the question, and is arranged on the Dashboard page afterwards.

    The frame is copied rather than referenced: see this module's docstring. The figure is
    not copied — the customize controls in the chat *reassign* `message.figure` rather than
    mutating it, so the object pinned here is a snapshot of the chart as it looked at pin
    time, which is what the user pressed the button on.
    """
    item = PinnedItem(
        question=message.question or message.text,
        heading=(message.question or "").strip(),
        comment=(message.text or "").strip(),
        sql=message.sql,
        frame=None if message.frame is None else message.frame.copy(),
        figure=message.figure,
        outputs=set(message.outputs),
    )
    get_report().pool.append(item)
    logger.info("Pinned an answer to the dashboard (item %s).", item.item_id)
    return item


def pool_count() -> int:
    """How many pinned items are still unplaced. Shown on the chat page so the user knows
    the pin landed somewhere they can get back to."""
    return len(get_report().pool)


# --------------------------------------------------------------------------------------
# Stylesheet
# --------------------------------------------------------------------------------------


def accepted_css() -> str | None:
    """The hand-edited stylesheet currently in force, or None to use the chosen preset.

    Only ever set by `set_accepted_css`, which the page calls after `validate_css` returns
    no problems — requirement 6.4's "accepted only after validation" is enforced by there
    being no other way in.
    """
    return st.session_state.get(DB_CSS_KEY)


def set_accepted_css(css: str | None) -> None:
    if css is None:
        st.session_state.pop(DB_CSS_KEY, None)
        return
    st.session_state[DB_CSS_KEY] = css


def selected_preset(default: str) -> str:
    return st.session_state.get(DB_PRESET_KEY, default)


def set_selected_preset(name: str) -> None:
    """Switching preset drops any hand-edited stylesheet: the edit was made against a
    different starting point, and silently keeping it would make the preset buttons look
    broken."""
    st.session_state[DB_PRESET_KEY] = name
    set_accepted_css(None)


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def open_dialog(action: str, payload: dict | None = None) -> None:
    """Flags which dialog should be showing.

    Held in session state rather than read from a button's return value, for the reason
    `engine/session.py::open_dialog` documents: a dialog containing widgets closes mid-edit
    if it depends on a control's return value surviving the rerun.
    """
    st.session_state[DB_DIALOG_KEY] = {"action": action, "payload": payload or {}}


def close_dialog() -> None:
    st.session_state.pop(DB_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    """The open dialog's `(action, payload)`, or None."""
    pending = st.session_state.get(DB_DIALOG_KEY)
    if not pending:
        return None
    return pending["action"], pending.get("payload", {})


def reset_dashboard() -> None:
    """Drops every `db_*` key.

    Called when the engine is reset, from the page rather than from `engine/` or
    `analyst/` — the same arrangement `chat_session.reset_chat()` already uses, so no
    package below the page layer needs to know this one exists. A report about tables that
    no longer exist would otherwise read as if it described whatever is loaded next.
    """
    for key in (DB_REPORT_KEY, DB_DIALOG_KEY, DB_CSS_KEY, DB_PRESET_KEY, DB_VIEW_KEY):
        st.session_state.pop(key, None)
