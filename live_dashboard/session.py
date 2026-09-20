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

The cached data is keyed by the *choices that produced it* (the main table and the engine's
rebuild token), so it refreshes by itself when the user picks a different table or the tables
underneath are rebuilt. That is the one piece of state here that could silently go stale, so
it is the one piece that carries its own key.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

from live_dashboard.model import DashboardSpec, find_panel, remove_panel

logger = logging.getLogger(__name__)

LD_SPEC_KEY = "ld_spec"
LD_DIALOG_KEY = "ld_open_dialog"
LD_DATA_KEY = "ld_built_data"
LD_TABLE_KEY = "ld_spec_table"


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

    def main_row_count(self, main_table: str) -> int:
        frame = self.tables.get(main_table)
        return 0 if frame is None else len(frame)


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

    The built data goes with it: it was flattened for the old spec's main table and would
    otherwise be checked against the new spec's panels, reporting problems that aren't real.
    """
    st.session_state[LD_SPEC_KEY] = spec
    st.session_state.pop(LD_DATA_KEY, None)
    queue_table_reset()


def get_built_data() -> BuiltData | None:
    data = st.session_state.get(LD_DATA_KEY)
    return data if isinstance(data, BuiltData) else None


def store_built_data(data: BuiltData) -> None:
    st.session_state[LD_DATA_KEY] = data


def clear_built_data() -> None:
    """Forgets the flattened data, so the next render rebuilds it."""
    st.session_state.pop(LD_DATA_KEY, None)


def delete_panel(panel_id: str) -> bool:
    """Removes a panel from the spec and clears any selection pointing at it."""
    spec = get_spec()
    if find_panel(spec, panel_id) is None:
        return False
    removed = remove_panel(spec, panel_id)
    if removed:
        queue_table_reset()
    return removed


# --------------------------------------------------------------------------------------
# The spec table's selection
# --------------------------------------------------------------------------------------


def queue_table_reset() -> None:
    """Asks for the spec table's row selection to be cleared on the next run.

    Deferred rather than done here, for the reason `app_pages/user_management.py` documents:
    a selection cannot be cleared during the run that reads it, so the request is parked and
    `consume_table_reset` acts on it at the top of the next one. Without this, deleting a row
    leaves the *next* row selected at the same index - so the buttons underneath silently
    point at a panel the user never chose.
    """
    st.session_state["ld_table_reset_pending"] = True


def consume_table_reset() -> None:
    """Clears the selection if one was queued. Called once, before the table is drawn."""
    if st.session_state.pop("ld_table_reset_pending", False):
        st.session_state[LD_TABLE_KEY] = {"selection": {"rows": [], "columns": []}}


def selected_panel_id(row_ids: list[str]) -> str:
    """Which panel the spec table has selected, or an empty string.

    Takes the ids in display order rather than reading the spec itself, because the table is
    drawn grouped by row number and its row order is not the spec's list order.
    """
    state = st.session_state.get(LD_TABLE_KEY)
    if not isinstance(state, dict):
        return ""
    rows = state.get("selection", {}).get("rows", [])
    if not rows:
        return ""
    position = rows[0]
    return row_ids[position] if 0 <= position < len(row_ids) else ""


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def open_dialog(action: str, payload: dict | None = None) -> None:
    """Asks for a dialog to be shown on the next run.

    The same deferred shape `report_items/session.py` uses: a dialog opened from inside a
    button press would be drawn before the rest of the page had caught up with what the press
    changed.
    """
    st.session_state[LD_DIALOG_KEY] = (action, payload or {})


def close_dialog() -> None:
    st.session_state.pop(LD_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    pending = st.session_state.get(LD_DIALOG_KEY)
    if not isinstance(pending, tuple) or len(pending) != 2:
        return None
    return pending


def reset_dashboard_spec() -> None:
    """Clears the dashboard entirely - a new Task, or Start over.

    Everything in the `ld_*` namespace goes, including the built data and the table's
    selection: a leftover selection index pointing into a list that no longer exists is
    exactly the kind of state that produces an edit dialog for a panel nobody can see.
    """
    for key in (LD_SPEC_KEY, LD_DIALOG_KEY, LD_DATA_KEY, LD_TABLE_KEY,
                "ld_table_reset_pending"):
        st.session_state.pop(key, None)
