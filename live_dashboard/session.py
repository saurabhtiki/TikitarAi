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

from live_dashboard import payload
from live_dashboard.exceptions import DashboardStorageError
from live_dashboard.model import DashboardSpec, from_json, to_json

logger = logging.getLogger(__name__)

LD_SPEC_KEY = "ld_spec"
LD_DATA_KEY = "ld_built_data"

#: What each round of the conversation did, newest first - the instruction, the one-line
#: summary and the sentences the user should read. Session-only: it describes changes to a
#: dashboard, and the dashboard itself is what gets saved.
LD_ROUNDS_KEY = "ld_ai_rounds"

#: The dashboard as JSON, taken immediately *before* the last round, for Undo. One step only
#: - see `stash_for_undo`.
#:
#: Deliberately not "ld_ai_undo": that is the Undo *button's* widget key, and Streamlit
#: refuses to let a widget's key be written from session state. Sharing the two names throws
#: on every render, which is why the two are spelled apart.
LD_UNDO_KEY = "ld_ai_undo_spec"

#: How many rounds are listed back. Older ones are dropped rather than scrolled past: the
#: dashboard is the record of what was built, and this is only the story of getting there.
MAX_ROUNDS_SHOWN = 10


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


def get_built_data() -> BuiltData | None:
    data = st.session_state.get(LD_DATA_KEY)
    return data if isinstance(data, BuiltData) else None


def store_built_data(data: BuiltData) -> None:
    st.session_state[LD_DATA_KEY] = data


def clear_built_data() -> None:
    """Forgets the flattened data, so the next render rebuilds it."""
    st.session_state.pop(LD_DATA_KEY, None)


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


def stash_for_undo(spec: DashboardSpec) -> None:
    """Remembers the dashboard as it is *now*, so the round about to run can be undone.

    JSON rather than a copy of the object, because `model.to_json` / `from_json` is the one
    conversion already trusted to round-trip a dashboard - a `deepcopy` would be a second
    answer to the same question, and the one that silently stops matching.

    **One step only.** A second round overwrites this. An unlimited history held in session
    state is not free when a dashboard carries a logo, and the round people want back is
    almost always the one they just ran.
    """
    try:
        st.session_state[LD_UNDO_KEY] = to_json(spec)
    except (TypeError, ValueError):
        # Losing Undo costs a button; failing the round would cost the user's instruction.
        logger.exception("Could not stash the dashboard for Undo - the round still runs.")
        st.session_state.pop(LD_UNDO_KEY, None)


def can_undo() -> bool:
    return bool(st.session_state.get(LD_UNDO_KEY))


def discard_undo() -> None:
    """Forgets the stash, for a round that turned out to change nothing.

    Without this, a round the model declined would leave Undo lit up offering to restore the
    dashboard to exactly what it already is - a button that does nothing is worse than no
    button, because the user presses it and learns to distrust the next one.
    """
    st.session_state.pop(LD_UNDO_KEY, None)


def undo_last_round() -> bool:
    """Puts the dashboard back as it was before the last round. Returns whether it worked."""
    stored = st.session_state.pop(LD_UNDO_KEY, None)
    if not stored:
        return False
    try:
        replace_spec(from_json(stored))
    except DashboardStorageError:
        logger.exception("The stashed dashboard could not be read back.")
        return False
    return True


def reset_dashboard_spec() -> None:
    """Clears the dashboard entirely - a new Task, or Start over.

    Everything in the `ld_*` namespace goes, the built data and the conversation included:
    rounds describing a dashboard that no longer exists would be read as the story of the
    new one.
    """
    for key in (LD_SPEC_KEY, LD_DATA_KEY, LD_ROUNDS_KEY, LD_UNDO_KEY):
        st.session_state.pop(key, None)
