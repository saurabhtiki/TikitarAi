"""The report item list in session state (requirement 7.3 step 3).

The only module in `report_items/` that imports Streamlit, matching the convention
`cleaner/session.py`, `engine/session.py`, `analyst/session.py`, `dashboard/session.py` and
`checks/session.py` already set — the model and the SQL builder are testable without
`AppTest` and without a provider.

Two things live here, with deliberately different lifetimes, exactly as in `checks/session.py`:

- **The list** — headings, requests, hints, SQL, comments, chart specs, column-step
  statements. Kept in its own `ri_*` namespace and written into a Task's JSON on demand,
  because a report is meant to be produced again next month.
- **An `ItemRuntime` per item** — the rows the last **Run** press returned, or why it
  failed, plus anything a press needs to say on the next run. Session-only and never stored:
  it describes tables that are gone the moment the session ends.

The runtime is **one record per item** rather than three dicts keyed the same way, so "an
item has rows or an error, never both" is structural instead of two `pop` calls someone has
to remember, and discarding an item is one delete that cannot leave a third dict behind.

The persona is **not** here. One persona belongs to the whole Task and is `tasks/session.py`'s
— both this list and the Checks view read the same one, which is the point of it being set
once (requirement 7.2).
"""

import logging
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

from report_items.model import ReportItem, can_remove, find_item

logger = logging.getLogger(__name__)

RI_ITEMS_KEY = "ri_items"
RI_RUNTIME_KEY = "ri_runtime"
RI_DIALOG_KEY = "ri_open_dialog"


@dataclass
class ItemRuntime:
    """What one item has produced in this session, outside the list itself.

    Attributes:
        frame: the rows the last successful run returned, or None.
        error: why the last run failed, or None. Never set at the same time as `frame` —
            the two are the same question answered two ways, and showing last attempt's rows
            under this attempt's error would be a wrong answer rather than a stale one.

            Held here as well as on `ReportItem.last_error` because the two serve different
            readers: this one is what to show on screen now, and the one on the item is what
            to tell the model on the next refine. Loading a saved Task brings back the second
            and not the first, which is right — a stored error describes data that is gone.
        notices: messages raised while handling a press, to be shown on the next run.
    """

    frame: pd.DataFrame | None = None
    error: str | None = None
    notices: list[str] = field(default_factory=list)


def _runtimes() -> dict[str, ItemRuntime]:
    return st.session_state.setdefault(RI_RUNTIME_KEY, {})


def runtime(item_id: str) -> ItemRuntime:
    """This item's session-only record. Created empty on first access."""
    return _runtimes().setdefault(item_id, ItemRuntime())


def discard_runtime(item_id: str) -> None:
    """Forgets everything this item produced this session."""
    _runtimes().pop(item_id, None)


def get_items() -> list[ReportItem]:
    """The ordered list being built. Created empty on first access.

    Returned by reference on purpose: the view mutates items in place (a heading edited, SQL
    stored after a run), and a copy would drop every one of those edits on the next rerun.
    """
    items = st.session_state.get(RI_ITEMS_KEY)
    if items is None:
        items = []
        st.session_state[RI_ITEMS_KEY] = items
    return items


def replace_items(items: list[ReportItem]) -> None:
    """Swaps in a list loaded from a saved Task.

    Every runtime goes with it: they belong to the items that were on screen a moment ago,
    and showing one Task's rows under another Task's headings would be a wrong answer rather
    than a stale one.
    """
    st.session_state[RI_ITEMS_KEY] = items
    st.session_state.pop(RI_RUNTIME_KEY, None)
    logger.info("Loaded %d report item(s).", len(items))


def remove_item(item_id: str) -> tuple[bool, str]:
    """Drops an item from the list and forgets what it produced.

    Returns `(removed, reason)` — the reason is `model.can_remove`'s, so a refused delete
    says why rather than appearing to do nothing. Unpinning whatever it put in the report is
    the caller's, because only the page knows which report is active.
    """
    items = get_items()
    allowed, reason = can_remove(items, item_id)
    if not allowed:
        return False, reason

    items.remove(find_item(items, item_id))
    discard_runtime(item_id)
    return True, ""


# --------------------------------------------------------------------------------------
# Run results
# --------------------------------------------------------------------------------------


def record_result(item_id: str, frame: pd.DataFrame) -> None:
    """Records a successful run, clearing the error it just replaced."""
    current = runtime(item_id)
    current.frame = frame
    current.error = None


def record_error(item_id: str, message: str) -> None:
    """Records a failed run, dropping the rows a previous attempt left behind."""
    current = runtime(item_id)
    current.error = message
    current.frame = None


def clear_error(item_id: str) -> None:
    """Forgets a failure without claiming a result.

    The state a **column step** succeeds into: it has no rows to record, so `record_result`
    would be a lie and `record_error(None)` reads as recording an error. Leaving the previous
    failure on screen under a change that has just been applied would be worse than either.
    """
    runtime(item_id).error = None


# --------------------------------------------------------------------------------------
# Notices
# --------------------------------------------------------------------------------------


def queue_notices(item_id: str, messages: list[str]) -> None:
    """Holds messages raised while handling a press, to be shown on the next run.

    Anything drawn *during* a handler that ends in `st.rerun` is thrown away with the rest of
    that run's output — so "couldn't write the comment" would never reach the person it is
    for. Queuing survives the rerun; `consume_notices` then shows it exactly once, because it
    describes the press that has already happened.
    """
    if not messages:
        return
    runtime(item_id).notices.extend(messages)


def consume_notices(item_id: str) -> list[str]:
    """Returns and clears this item's queued messages."""
    current = runtime(item_id)
    pending, current.notices = current.notices, []
    return pending


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def open_dialog(action: str, payload: dict | None = None) -> None:
    """Flags which dialog should be showing.

    Held in session state rather than read from a button's return value, for the reason
    `engine/session.py::open_dialog` documents: a dialog containing widgets closes mid-edit
    if it depends on a control's return value surviving the rerun.
    """
    st.session_state[RI_DIALOG_KEY] = {"action": action, "payload": payload or {}}


def close_dialog() -> None:
    st.session_state.pop(RI_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    """The dialog that should render this run, as `(action, payload)`, or None."""
    pending = st.session_state.get(RI_DIALOG_KEY)
    if not pending:
        return None
    return pending["action"], pending.get("payload", {})


# --------------------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------------------


def reset_report_items() -> None:
    """Clears every `ri_*` key.

    Called by Start over, which discards the loaded tables — every item was written against
    those, and every run describes them. A saved *Task* in SQLite is untouched: it is a
    recipe, and the whole point of one is that it outlives the data it was written for.
    """
    for key in (RI_ITEMS_KEY, RI_RUNTIME_KEY, RI_DIALOG_KEY):
        st.session_state.pop(key, None)
