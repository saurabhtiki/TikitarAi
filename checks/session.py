"""The criteria set in session state (requirement 6.5).

The only module in `checks/` that imports Streamlit, matching the convention
`cleaner/session.py`, `engine/session.py`, `analyst/session.py` and `dashboard/session.py`
already set — the model, the storage, the SQL builder and the drafting calls are all
testable without `AppTest`.

Two things live here, with deliberately different lifetimes:

- **The set** — persona, criteria, hints, SQL, saved runs. Kept in its own `ck_*` namespace
  and written to SQLite on demand by `checks/db.py`, because a set of rules is meant to be
  run again next month.
- **A `CheckRuntime` per criteria** — the rows the last **Test** press returned, or why it
  failed, plus anything a press needs to say on the next run. Session-only and never stored,
  exactly like a chat answer's frame: it describes tables that are gone the moment the
  session ends.

The runtime is **one record per criteria** rather than three dicts keyed the same way, so
"a criteria has rows or an error, never both" is structural instead of two `pop` calls
someone has to remember, and discarding a criteria is one delete that cannot leave a third
dict behind.

A saved run's rows are the one thing held in *both* places at once — on the `SavedRun` here
and, copied again, on the dashboard item. That is intentional: `dashboard.session.pin_result`
copies rather than references for the reason its own docstring gives.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

from checks.model import SOURCE_PREFIX as model_source_prefix
from checks.model import SUMMARY_SOURCE_ID as model_summary_source_id
from checks.model import CheckSet
from checks.model import actions_source_id_for as model_actions_source_id_for
from checks.model import source_id_for as model_source_id_for

logger = logging.getLogger(__name__)

CK_SET_KEY = "ck_set"
CK_RUNTIME_KEY = "ck_runtime"
CK_DIALOG_KEY = "ck_open_dialog"

# The set summary's text box. Named here because two places have to agree on it: the widget
# itself, and the handlers that write an AI draft into it. Assigning to the box's own session
# key is the only way to change what an existing widget shows — `value=` is applied when the
# key is first created and ignored on every run after, the trap this module's siblings all
# document.
CK_SUMMARY_TEXT_KEY = "ck_summary_text"

# What a criteria's dashboard item is filed under. Prefixed rather than the bare check_id,
# so a criteria and the same criteria's action list (`…:actions`) can each own an item
# without either one overwriting the other.
# These four moved to `checks/model.py` and are re-exported here. `runner/replay.py` has to
# ask the same question — which report item does this criteria own? — while filling a saved
# report skeleton, and it may not import a module that pulls in Streamlit. Kept as names on
# this module because every existing caller spells them `checks_session.source_id_for(...)`,
# and renaming those would be churn for nothing.
SOURCE_PREFIX = model_source_prefix
SUMMARY_SOURCE_ID = model_summary_source_id
source_id_for = model_source_id_for
actions_source_id_for = model_actions_source_id_for


@dataclass
class CheckRuntime:
    """What one criteria has produced in this session, outside the set itself.

    Attributes:
        frame: the rows the last successful test returned, or None.
        error: why the last test failed, or None. Never set at the same time as `frame` —
            the two are the same question answered two ways, and showing last attempt's rows
            under this attempt's error would be a wrong answer rather than a stale one.

            Held here as well as on `Check.last_error` because the two serve different
            readers: this one is what to show on screen now, and the one on the check is what
            to tell the model on the next refine. Loading a saved set brings back the second
            and not the first, which is right — a stored error describes data that is gone.
        notices: messages raised while handling a press, to be shown on the next run.
    """

    frame: pd.DataFrame | None = None
    error: str | None = None
    notices: list[str] = field(default_factory=list)


def _runtimes() -> dict[str, CheckRuntime]:
    return st.session_state.setdefault(CK_RUNTIME_KEY, {})


def runtime(check_id: str) -> CheckRuntime:
    """This criteria's session-only record. Created empty on first access."""
    return _runtimes().setdefault(check_id, CheckRuntime())


def discard_runtime(check_id: str) -> None:
    """Forgets everything this criteria produced this session."""
    _runtimes().pop(check_id, None)


def get_set() -> CheckSet:
    """The criteria set being worked on. Created empty on first access."""
    check_set = st.session_state.get(CK_SET_KEY)
    if check_set is None:
        check_set = CheckSet()
        st.session_state[CK_SET_KEY] = check_set
    return check_set


def replace_set(check_set: CheckSet) -> None:
    """Swaps in a set loaded from storage.

    Every runtime goes with it: they belong to the criteria that were on screen a moment
    ago, and showing one set's rows under another set's rules would be a wrong answer rather
    than a stale one.
    """
    st.session_state[CK_SET_KEY] = check_set
    st.session_state.pop(CK_RUNTIME_KEY, None)
    # The summary box goes too, for the reason `CK_SUMMARY_TEXT_KEY` gives: left in place it
    # would keep displaying the previous set's summary over the loaded set's rules, and the
    # next keystroke would write that stale text back onto the new set.
    st.session_state.pop(CK_SUMMARY_TEXT_KEY, None)
    logger.info("Loaded criteria set '%s' with %d criteria.", check_set.display_name(), len(check_set.checks))


# --------------------------------------------------------------------------------------
# Test results
# --------------------------------------------------------------------------------------


def record_result(check_id: str, frame: pd.DataFrame) -> None:
    """Records a successful test, clearing the error it just replaced."""
    current = runtime(check_id)
    current.frame = frame
    current.error = None


def record_error(check_id: str, message: str) -> None:
    """Records a failed test, dropping the rows a previous attempt left behind."""
    current = runtime(check_id)
    current.error = message
    current.frame = None


# --------------------------------------------------------------------------------------
# Notices
# --------------------------------------------------------------------------------------


def queue_notices(check_id: str, messages: list[str]) -> None:
    """Holds messages raised while handling a press, to be shown on the next run.

    Anything drawn *during* a handler that ends in `st.rerun` is thrown away with the rest
    of that run's output — so "couldn't write the remarks" would never reach the person it
    is for. Queuing survives the rerun; `consume_notices` then shows it exactly once,
    because it describes the press that has already happened.
    """
    if not messages:
        return
    runtime(check_id).notices.extend(messages)


def consume_notices(check_id: str) -> list[str]:
    """Returns and clears this criteria's queued messages."""
    current = runtime(check_id)
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
    st.session_state[CK_DIALOG_KEY] = {"action": action, "payload": payload or {}}


def close_dialog() -> None:
    st.session_state.pop(CK_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    """The dialog that should render this run, as `(action, payload)`, or None."""
    pending = st.session_state.get(CK_DIALOG_KEY)
    if not pending:
        return None
    return pending["action"], pending.get("payload", {})


# --------------------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------------------


def reset_checks() -> None:
    """Clears every `ck_*` key.

    Called by the chat page's Start over, which discards the loaded tables — every criteria
    was written against those, and every saved run describes them. Saved *sets* in SQLite
    are untouched: they are recipes, and the whole point of one is that it outlives the data
    it was written for.
    """
    for key in (CK_SET_KEY, CK_RUNTIME_KEY, CK_DIALOG_KEY, CK_SUMMARY_TEXT_KEY):
        st.session_state.pop(key, None)
