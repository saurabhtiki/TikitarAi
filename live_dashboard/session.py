"""The dashboard spec in session state (phase 32).

The only module in `live_dashboard/` that imports Streamlit, matching the convention
`report_items/session.py`, `engine/session.py`, `dashboard/session.py` and
`checks/session.py` already set - the model, the flattener, the spec builder and the exporter
are all testable without `AppTest`.

Two things live here, with deliberately different lifetimes:

- **The spec** - panels, titles, theme, which table to build on. Kept in its own `ld_*`
  namespace and written into a Task's JSON, because a dashboard is meant to be produced again
  next month against next month's file.
- **The built data** - the flattened table and the plan that produced it. Session-only and
  never stored: it describes rows that are gone the moment the session ends. Held here rather
  than recomputed per rerun because flattening is a real query, and Streamlit reruns the whole
  script on every keystroke.

The cached data is keyed by the *choices that produced it* (which tables exist and the
engine's rebuild token), so it refreshes by itself when a table is added or the tables
underneath are rebuilt. That is the one piece of state here that could silently go stale, so
it is the one piece that carries its own key.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

from live_dashboard import payload
from live_dashboard.exceptions import DashboardStorageError
from live_dashboard.model import DashboardSpec, PanelSpec, from_json, remove_panel, to_json

logger = logging.getLogger(__name__)

LD_SPEC_KEY = "ld_spec"
LD_DATA_KEY = "ld_built_data"

#: What each round of the conversation did, newest first - the instruction, the one-line
#: summary and the sentences the user should read. Session-only: it describes changes to a
#: dashboard, and the dashboard itself is what gets saved.
LD_ROUNDS_KEY = "ld_ai_rounds"

#: How many rounds are listed back. Older ones are dropped rather than scrolled past: the
#: dashboard is the record of what was built, and this is only the story of getting there.
#:
#: Since phase 39 each round also carries the dashboard from before it, so this is what caps
#: what Undo remembers: a round that falls off the end takes its copy with it.
MAX_ROUNDS_SHOWN = 10

#: Which dialog is open, if any (phase 37): `ASK_DIALOG` for the box that changes the whole
#: dashboard, or a panel's `panel_id` for the one scoped to a single visual. A flag rather
#: than a button's return value, for the reason `data_cleaner` gives: a `st.dialog` holding
#: widgets reruns the script, and the press that opened it is long gone by then.
LD_DIALOG_KEY = "ld_dialog"

#: The dialog that changes the whole dashboard. Any other value is a `panel_id`.
ASK_DIALOG = "ask"

#: The `panel_id` whose Remove button has been pressed once and is waiting for "Yes". A flag
#: for the same reason as `LD_DIALOG_KEY`: the press is long gone by the time the dialog
#: reruns to show the question.
LD_CONFIRM_REMOVE_KEY = "ld_confirm_remove"

#: Set once **Generate Dashboard** has been pressed on a page that already has visuals, and
#: is waiting for "Yes, replace it". The same two-press idiom as Remove, for the same
#: reason: Generate throws away every round the user has had, so one stray click must not
#: be enough to do it.
LD_CONFIRM_GENERATE_KEY = "ld_confirm_generate"

#: A one-line message queued for the run right after this one - `st.rerun` throws away
#: anything written before it, the same reason `cleaner.session.queue_flash` exists.
LD_FLASH_KEY = "ld_ai_flash"


@dataclass
class BuiltData:
    """The flattened data behind the dashboard, and what produced it.

    Attributes:
        tables: each embedded table by the name panels refer to it by.
        plan_description: the one sentence naming what was joined, shown under the picker so
            the user can always see what the app chose.
        signature: the choices this was built from. When it stops matching, the data is
            rebuilt rather than served stale.
    """

    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    plan_description: str = ""
    signature: tuple = ()

    def available_columns(self) -> dict[str, list[str]]:
        """Each table's real columns - what `model.panel_problems` checks a panel against."""
        return {name: [str(column) for column in frame.columns]
                for name, frame in self.tables.items()}

    def date_columns(self) -> frozenset[str]:
        """Which of those columns hold dates - what `model.panel_problems` checks a running
        total against, and what the export uses to space a line by elapsed time."""
        return payload.date_columns(self.tables)

    def total_row_count(self) -> int:
        """Every embedded row, across every table.

        All of them since phase 39, where the size of the download stopped being one
        table's business: a page carrying Salary and Attendance whole is as big as both.
        """
        return sum(len(frame) for frame in self.tables.values())


def get_spec() -> DashboardSpec:
    """The dashboard being edited, created empty on first use.

    Returns the live object, not a copy: callers mutate it in place and Streamlit's session
    state keeps the same reference across reruns, which is what makes an edit in a dialog
    show up in the table behind it.
    """
    spec = st.session_state.get(LD_SPEC_KEY)
    if not isinstance(spec, DashboardSpec):
        spec = DashboardSpec()
        st.session_state[LD_SPEC_KEY] = spec
    return spec


def replace_spec(spec: DashboardSpec) -> None:
    """Swaps in a whole dashboard - loading a saved Task, or starting over.

    The built data goes with it: it was flattened for the tables the old spec was built on
    and would otherwise be checked against the new spec's panels, reporting problems that
    aren't real.
    """
    st.session_state[LD_SPEC_KEY] = spec
    st.session_state.pop(LD_DATA_KEY, None)


def get_built_data() -> BuiltData | None:
    data = st.session_state.get(LD_DATA_KEY)
    return data if isinstance(data, BuiltData) else None


def store_built_data(data: BuiltData) -> None:
    st.session_state[LD_DATA_KEY] = data


# --------------------------------------------------------------------------------------
# The conversation (phase 35)
# --------------------------------------------------------------------------------------


@dataclass
class Round:
    """One exchange: what was asked, what changed, and what the user should know.

    The panels are kept as sentences rather than as `PanelSpec` objects, because a panel the
    user has since edited or removed would make the history describe a page that no longer
    exists. A round says what it did at the time, and stays true.
    """

    instruction: str = ""
    summary: str = ""
    details: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clarification: str = ""
    failed: bool = False

    #: The dashboard as JSON from immediately *before* this round - what its own Undo
    #: button puts back (phase 39). Empty when the round changed nothing, which is what
    #: leaves it with no button rather than one that restores the page to what it already
    #: is. JSON rather than a copy of the object because `to_json` / `from_json` is the one
    #: conversion already trusted to round-trip a dashboard.
    before_json: str = ""

    def can_be_undone(self) -> bool:
        return bool(self.before_json)


def record_round(entry: Round) -> None:
    """Puts one round at the top of the history, keeping the last `MAX_ROUNDS_SHOWN`."""
    history = [entry, *rounds()][:MAX_ROUNDS_SHOWN]
    st.session_state[LD_ROUNDS_KEY] = history


def rounds() -> list[Round]:
    """The conversation so far, newest first."""
    stored = st.session_state.get(LD_ROUNDS_KEY)
    return [entry for entry in stored if isinstance(entry, Round)] if isinstance(stored, list) else []


def clear_rounds() -> None:
    st.session_state.pop(LD_ROUNDS_KEY, None)


def snapshot(spec: DashboardSpec) -> str:
    """The dashboard as it is *now*, as JSON, for the round about to change it.

    Handed to `Round.before_json` rather than kept in a slot of its own, which is phase 39's
    change: there used to be one stash and one Undo button, so the second round of an
    afternoon threw away the only copy that could take you back to where you started. A
    round now owns the dashboard it replaced, and Undo is listed beside the round it undoes.

    Returns an empty string if the dashboard cannot be serialised - losing Undo for one
    round costs a button, where raising would cost the user's instruction.
    """
    try:
        return to_json(spec)
    except (TypeError, ValueError):
        logger.exception("Could not copy the dashboard for Undo - the round still runs.")
        return ""


def undo_round(position: int) -> bool:
    """Puts the dashboard back to before the round at `position`. Returns whether it worked.

    **Only the newest round can be undone**, and the older buttons are drawn disabled
    saying so. Restoring an older copy would silently throw away every round after it, which
    is not what a button labelled Undo means anywhere else in this app.

    The round is dropped from `rounds()` as it is undone: leaving it there had the history
    describing a change that is no longer on the page.
    """
    history = rounds()
    if position != 0 or not history or not history[0].can_be_undone():
        return False

    undone = history[0]
    try:
        replace_spec(from_json(undone.before_json))
    except DashboardStorageError:
        logger.exception("The copy saved before that round could not be read back.")
        return False

    st.session_state[LD_ROUNDS_KEY] = history[1:]
    queue_flash(f"Undone: {undone.summary}" if undone.summary else "Undone.")
    return True


def open_dialog(which: str) -> None:
    """Marks a dialog as open. `ASK_DIALOG`, or the `panel_id` of one visual."""
    st.session_state[LD_DIALOG_KEY] = which


def close_dialog() -> None:
    st.session_state.pop(LD_DIALOG_KEY, None)
    st.session_state.pop(LD_CONFIRM_REMOVE_KEY, None)


def ask_to_generate() -> None:
    """First press of Generate on a page that already has visuals: asks before replacing."""
    st.session_state[LD_CONFIRM_GENERATE_KEY] = True


def cancel_generate() -> None:
    st.session_state.pop(LD_CONFIRM_GENERATE_KEY, None)


def is_confirming_generate() -> bool:
    return bool(st.session_state.get(LD_CONFIRM_GENERATE_KEY))


def ask_to_remove(panel_id: str) -> None:
    """First press of Remove: shows "Are you sure?" instead of removing."""
    st.session_state[LD_CONFIRM_REMOVE_KEY] = panel_id


def cancel_remove() -> None:
    st.session_state.pop(LD_CONFIRM_REMOVE_KEY, None)


def is_confirming_remove(panel_id: str) -> bool:
    return st.session_state.get(LD_CONFIRM_REMOVE_KEY) == panel_id


def remove_visual(spec: DashboardSpec, panel: PanelSpec) -> bool:
    """Takes one visual off the page, as a round Undo can reverse. Returns whether it went.

    No model is asked: the user pressed a button that says exactly what it does. The copy is
    taken first and simply goes unused if the visual was already gone, so no round - and so
    no Undo button - is recorded for a change that did not happen.
    """
    title = panel.display_title()
    before = snapshot(spec)
    if not remove_panel(spec, panel.panel_id):
        return False
    record_round(Round(
        instruction=f"Remove {title}",
        summary=f"Removed {title}",
        details=[f"Removed **{title}**"],
        before_json=before,
    ))
    return True


def current_dialog() -> str:
    """Which dialog should be drawn this run - empty for none."""
    return str(st.session_state.get(LD_DIALOG_KEY) or "")


def queue_flash(message: str) -> None:
    """Holds a message for the next run."""
    st.session_state[LD_FLASH_KEY] = message


def consume_flash() -> str | None:
    """Takes the queued message, if there is one."""
    return st.session_state.pop(LD_FLASH_KEY, None)


def reset_dashboard_spec() -> None:
    """Clears the dashboard entirely - a new Task, or Start over.

    Everything in the `ld_*` namespace goes, the built data and the conversation included:
    rounds describing a dashboard that no longer exists would be read as the story of the
    new one.
    """
    for key in (LD_SPEC_KEY, LD_DATA_KEY, LD_ROUNDS_KEY, LD_FLASH_KEY,
                LD_DIALOG_KEY, LD_CONFIRM_REMOVE_KEY, LD_CONFIRM_GENERATE_KEY):
        st.session_state.pop(key, None)
