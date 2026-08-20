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
from dashboard.model import PinnedItem, Report, find_item, find_item_by_source, remove_item

logger = logging.getLogger(__name__)

DB_REPORT_KEY = "db_report"
DB_DIALOG_KEY = "db_open_dialog"
DB_CSS_KEY = "db_accepted_css"
DB_PRESET_KEY = "db_css_preset"
DB_VIEW_KEY = "db_view"

# Which report everything below works on. There is more than one now — the session
# Dashboard, and a Task's own report (requirement 7.3 step 5) — and the producers that pin
# into one (`checks/`, `report_items/`) must not have to know which. So they keep calling
# `pin_result`, and the page says once, at the top of its run, which report that means.
DB_ACTIVE_REPORT_KEY = "db_active_report"


def use_report(key: str = DB_REPORT_KEY) -> None:
    """Points every function in this module at one report, for the rest of this run.

    Called at the top of a page's run, by **every** page that touches a report, including
    the ones that want the default: the choice outlives the run that made it, so a page that
    stayed silent would inherit whichever report the last page to speak had selected — and
    Chat with data would pin into a Task's report because the user had visited Task Builder
    earlier in the session.
    """
    st.session_state[DB_ACTIVE_REPORT_KEY] = key


def active_report_key() -> str:
    """The session-state key of the report in play. The Dashboard's unless told otherwise."""
    return st.session_state.get(DB_ACTIVE_REPORT_KEY, DB_REPORT_KEY)


def get_report() -> Report:
    """The report being built. Created empty on first access."""
    key = active_report_key()
    report = st.session_state.get(key)
    if report is None:
        report = Report()
        st.session_state[key] = report
    return report


def set_report(report: Report) -> None:
    """Replaces the active report wholesale — loading a saved Task, or starting a new one."""
    st.session_state[active_report_key()] = report


def set_title(title: str) -> None:
    get_report().title = str(title or "").strip()


def pin(message: ChatMessage) -> PinnedItem:
    """Copies a chat answer into the unplaced pool, once.

    Nothing is asked of the user here — requirement 6.1 step 3 puts pinning in the middle
    of a conversation, and a dialog asking "which subsection?" before the user has built
    any would stop the chat dead. The item lands in the pool with its heading already set
    to the question, and is arranged on the Dashboard page afterwards.

    An answer that is already in the report returns the item it was pinned as rather than
    making a second copy of it. Pressing the button twice is the natural reading of a
    control that stays clickable, and it used to mean two identical tiles to find and
    discard on the Dashboard — so the id is recorded on the message and the page renders
    the button as already-pinned from then on.

    The frame is copied rather than referenced: see this module's docstring. The figure is
    not copied — the customize controls in the chat *reassign* `message.figure` rather than
    mutating it, so the object pinned here is a snapshot of the chart as it looked at pin
    time, which is what the user pressed the button on.
    """
    already = pinned_item(message)
    if already is not None:
        logger.info("Ignoring a repeat pin of an answer already on the dashboard (item %s).", already.item_id)
        return already

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
    message.pinned_item_id = item.item_id
    logger.info("Pinned an answer to the dashboard (item %s).", item.item_id)
    return item


def pinned_item(message: ChatMessage) -> PinnedItem | None:
    """The dashboard item this answer is currently pinned as, or None.

    Looked up in the report rather than trusted from the message, so discarding the pinned
    copy on the Dashboard — or pressing Start over — offers the chat's button again instead
    of leaving it permanently claiming the answer is already there.
    """
    if not message.pinned_item_id:
        return None
    return find_item(get_report(), message.pinned_item_id)


def pin_result(
    source_id: str,
    *,
    heading: str,
    comment: str = "",
    sql: str | None = None,
    frame=None,
    figure=None,
    outputs: set[str] | None = None,
) -> PinnedItem:
    """Puts a result into the report on its producer's behalf, and keeps it up to date.

    This is `pin`'s sibling for requirement 6.5's criteria, which differ from a chat answer
    in one way that matters: a criteria is **re-run**. The user refines the rule, tests it
    again and saves again, and the report must then show the new numbers rather than the old
    ones sitting next to them. So this is idempotent on `source_id`:

    - An item this producer already owns is updated **where it is**. If the user has filed
      it into a subsection, it stays there — pulling it back to the pool on every refine
      would undo the arrangement they built, which is the opposite of helpful.
    - An item the user removed on the Dashboard comes back as a new one. Saving again is an
      unambiguous request for it to be in the report, and a silently-dropped save would
      leave the user pressing a button that appears to do nothing.

    `png` is cleared on every update, because it is a cache of the *previous* figure and
    keeping it would export a chart of last run's numbers.

    The frame is copied, for the reason this module's docstring gives.
    """
    existing = find_item_by_source(get_report(), source_id)
    copied = None if frame is None else frame.copy()

    if existing is not None:
        existing.heading = heading
        existing.comment = comment
        existing.sql = sql
        existing.frame = copied
        existing.figure = figure
        existing.outputs = set(outputs or set())
        existing.png = None
        logger.info("Updated dashboard item %s from source %s.", existing.item_id, source_id)
        return existing

    item = PinnedItem(
        question=heading,
        heading=heading,
        comment=comment,
        sql=sql,
        frame=copied,
        figure=figure,
        outputs=set(outputs or set()),
        source_id=source_id,
    )
    get_report().pool.append(item)
    logger.info("Pinned a result from source %s to the dashboard (item %s).", source_id, item.item_id)
    return item


def unpin_source(source_id: str) -> bool:
    """Removes the item a producer owns, if it is still in the report.

    Used when a criteria's result stops being something the user wants reported — clearing
    a saved run — rather than leaving an orphan tile that no longer corresponds to anything.
    """
    item = find_item_by_source(get_report(), source_id)
    if item is None:
        return False
    return remove_item(get_report(), item.item_id)


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
    # The *active* report, not `DB_REPORT_KEY` outright: Start over in Task Builder must
    # clear the Task's report and leave the session Dashboard alone, and vice versa.
    for key in (active_report_key(), DB_DIALOG_KEY, DB_CSS_KEY, DB_PRESET_KEY, DB_VIEW_KEY):
        st.session_state.pop(key, None)
