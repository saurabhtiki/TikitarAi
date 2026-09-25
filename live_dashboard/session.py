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

#: The dialog **Generate Dashboard** opens (phase 45): the checklist, shown before anything
#: is built. It replaced phase 39's two-press "Yes, replace it" - the dialog is already a
#: second step, so the replace warning is said inside it rather than asked separately.
GENERATE_DIALOG = "generate"

#: The checklist and the "Anything else?" wishes, kept for this sitting so that closing the
#: dialog and opening it again finds the list as the user left it. Plain keys that the text
#: boxes are seeded from, because Streamlit forgets a widget's own key on any run where the
#: widget is not drawn - which is every run while the dialog is closed. Never saved into the
#: Task: the dashboard is the record of what was built, and this is only the plan for it.
LD_CHECKLIST_KEY = "ld_checklist"
LD_CHECKLIST_WISHES_KEY = "ld_checklist_wishes_saved"

#: The widget keys of those two boxes.
LD_CHECKLIST_BOX_KEY = "ld_checklist_text"
LD_WISHES_BOX_KEY = "ld_checklist_wishes"

#: Set when the list should be (re)drafted on the next run - on opening with no list yet, and
#: on Draft again. A flag rather than drafting inside the button press, because the box it
#: fills has already been drawn by then and cannot be changed in the same run.
LD_CHECKLIST_REDRAFT_KEY = "ld_checklist_redraft"

#: What the last draft had to say - "we couldn't reach the model", "only the first 20 lines
#: were kept". Shown in the dialog above the list it is about.
LD_CHECKLIST_NOTES_KEY = "ld_checklist_notes"

#: The **Update the dashboard** toggle: on, a round runs on the session's own model rather
#: than the Light Model. A plain widget key rather than a helper pair, because nothing reads
#: it but the one `st.toggle` that writes it, and it is deliberately not saved into the Task
#: - which model answered is a choice for this sitting, not a property of the dashboard.
LD_USE_ACTIVE_MODEL_KEY = "ld_use_my_model"

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


def open_generate() -> None:
    """Generate Dashboard was pressed: opens the checklist dialog.

    A list is drafted only if there isn't one from earlier in this sitting - reopening the
    dialog finds the user's edits where they left them, and Draft again is there for a
    fresh one.
    """
    open_dialog(GENERATE_DIALOG)
    if not checklist_text():
        request_draft()


def request_draft() -> None:
    st.session_state[LD_CHECKLIST_REDRAFT_KEY] = True


def take_draft_request() -> bool:
    """Whether a draft was asked for, clearing the request so it runs once."""
    return bool(st.session_state.pop(LD_CHECKLIST_REDRAFT_KEY, False))


def store_draft(lines: list[str], notes: list[str]) -> None:
    """Puts a freshly drafted list in the box, replacing what was there."""
    text = "\n".join(lines)
    st.session_state[LD_CHECKLIST_KEY] = text
    st.session_state[LD_CHECKLIST_BOX_KEY] = text
    st.session_state[LD_CHECKLIST_NOTES_KEY] = list(notes)


def seed_checklist_boxes() -> None:
    """Refills the two boxes from the kept copies, when Streamlit has forgotten them."""
    if LD_CHECKLIST_BOX_KEY not in st.session_state:
        st.session_state[LD_CHECKLIST_BOX_KEY] = checklist_text()
    if LD_WISHES_BOX_KEY not in st.session_state:
        st.session_state[LD_WISHES_BOX_KEY] = checklist_wishes()


def keep_checklist(text: str, wishes: str) -> None:
    """Remembers what is in the two boxes now, for the next time the dialog opens."""
    st.session_state[LD_CHECKLIST_KEY] = str(text or "")
    st.session_state[LD_CHECKLIST_WISHES_KEY] = str(wishes or "")


def checklist_text() -> str:
    return str(st.session_state.get(LD_CHECKLIST_KEY) or "")


def checklist_wishes() -> str:
    return str(st.session_state.get(LD_CHECKLIST_WISHES_KEY) or "")


def checklist_notes() -> list[str]:
    stored = st.session_state.get(LD_CHECKLIST_NOTES_KEY)
    return [str(one) for one in stored] if isinstance(stored, list) else []


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
                LD_DIALOG_KEY, LD_CONFIRM_REMOVE_KEY, LD_CHECKLIST_KEY,
                LD_CHECKLIST_WISHES_KEY, LD_CHECKLIST_BOX_KEY, LD_WISHES_BOX_KEY,
                LD_CHECKLIST_REDRAFT_KEY, LD_CHECKLIST_NOTES_KEY):
        st.session_state.pop(key, None)
