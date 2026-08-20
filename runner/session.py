"""A Task run in session state — the only module in `runner/` that imports Streamlit.

Same convention as every other package here: `runner/model.py` and `runner/replay.py` stay
pure, so the whole of requirement 8.2 is testable with an in-memory DuckDB and no `AppTest`.

Three things live here, with deliberately different lifetimes:

- **Which Task is open**, and the pristine copy of it read out of SQLite. Pristine matters:
  a replay mutates the recipe it is handed (a fallback rewrites an item's SQL), so every run
  works on a **deep copy** and the second run starts from the same recipe the first did.
- **The mapping** — requirement 8.1 step 5's manual remap, `{table_id: name}` and
  `{table: {found: expected}}`. Held here rather than derived, because it is the one thing on
  the page the user authored and it has to survive the reruns that reload the files.
- **The run's result**, kept so the summary and the report stay on screen after the run that
  produced them has ended.

`RT_REPORT_KEY` is the third report in the app, after the session Dashboard's and a Task
Builder's. `dashboard/session.py`'s active-report key is what makes that free: the Run page
says once, at the top of its run, which report it means, and the exports and the preview then
work on it with no idea they are doing anything different.
"""

import copy
import logging

import streamlit as st

from chat_types import matching
from chat_types import session as chat_types_session
from chat_types.model import ChatType
from dashboard import session as dashboard_session
from engine import session as engine_session
from llm import session as llm_session
from runner import replay
from runner.exceptions import TaskRunError
from runner.model import KIND_SETUP, STATUS_OK, RunResult, StepResult
from tasks.model import Task

logger = logging.getLogger(__name__)

RT_TASK_KEY = "rt_task"
RT_MAPPING_TABLES_KEY = "rt_map_tables"
RT_MAPPING_COLUMNS_KEY = "rt_map_columns"
RT_RESULT_KEY = "rt_result"
RT_DIALOG_KEY = "rt_open_dialog"
RT_FLASH_KEY = "rt_flash"
RT_REWRITE_KEY = "rt_rewrite_comments"

# This run's report, kept apart from `db_report` and `tb_report`. See the module docstring.
RT_REPORT_KEY = "rt_report"


# --------------------------------------------------------------------------------------
# Which Task is open
# --------------------------------------------------------------------------------------


def current_task() -> Task | None:
    """The Task chosen on the picker, exactly as it came out of storage, or None."""
    return st.session_state.get(RT_TASK_KEY)


def is_task_open() -> bool:
    return current_task() is not None


def open_task(task: Task) -> None:
    """Chooses a Task to run, and forgets everything about the last one.

    The mapping and the previous run's result both go: they describe a *different* recipe,
    and a remap that said "this file is the salary table" means nothing once the recipe being
    matched against has changed. The loaded tables are deliberately left alone — running two
    Tasks against one month's upload is a perfectly ordinary thing to want.

    The files already loaded are dropped for re-reading, for the reason
    `chat_types.session.select` gives: a table already in DuckDB was typed by detection, and
    the text it was typed from is gone, so measuring it against a Task's saved types would
    report a clean match over a table nothing had ever applied them to.
    """
    st.session_state[RT_TASK_KEY] = task
    clear_mapping()
    clear_result()
    chat_types_session.forget_upload()
    engine_session.reload_uploaded_tables()
    logger.info("Opened task '%s' to run.", task.display_name())


def close_task() -> None:
    """Puts the picker back up, and forgets the mapping and the result with it."""
    st.session_state.pop(RT_TASK_KEY, None)
    clear_mapping()
    clear_result()


# --------------------------------------------------------------------------------------
# The mapping (requirement 8.1 step 5)
# --------------------------------------------------------------------------------------


def table_map() -> dict[str, str]:
    """`{table_id: the name this file should load under}`."""
    return st.session_state.setdefault(RT_MAPPING_TABLES_KEY, {})


def column_map() -> dict[str, dict[str, str]]:
    """`{table name: {name in the file: name the recipe expects}}`."""
    return st.session_state.setdefault(RT_MAPPING_COLUMNS_KEY, {})


def map_table(table_id: str, name: str | None) -> None:
    """Says which of the recipe's tables an uploaded file is, or takes the mapping back.

    Any column remap recorded under a name no table is mapped to any more is left in place
    rather than swept: `sync_tables` looks the map up by the name a table *has*, so a stale
    entry is inert, and clearing it would lose the user's work the moment they mis-clicked a
    dropdown and put it back.
    """
    if name:
        table_map()[table_id] = name
    else:
        table_map().pop(table_id, None)
    reload_after_mapping()


def map_column(table: str, found: str, expected: str | None) -> None:
    """Says which of the recipe's columns a column in the file is, or takes it back."""
    renames = column_map().setdefault(table, {})
    if expected:
        renames[found] = expected
    else:
        renames.pop(found, None)
    reload_after_mapping()


def clear_mapping() -> None:
    st.session_state.pop(RT_MAPPING_TABLES_KEY, None)
    st.session_state.pop(RT_MAPPING_COLUMNS_KEY, None)


def reload_after_mapping() -> None:
    """Drops the uploaded tables so the next run reads them again under the new mapping.

    A remap has to be applied **while the file is being read** — see
    `engine.loading.rename_columns` — so changing one and leaving the loaded tables alone
    would show a mapping that is on screen and not in effect.
    """
    chat_types_session.forget_upload()
    engine_session.reload_uploaded_tables()


# --------------------------------------------------------------------------------------
# Matching the upload against the recipe (requirement 8.1 steps 3–4)
# --------------------------------------------------------------------------------------


def declared_types() -> dict[str, dict[str, str]]:
    """The open Task's saved column types, in the shape `sync_tables` takes.

    Empty when no Task is open, which makes the upload widgets on the picker screen behave
    exactly as an ordinary ad-hoc upload does.
    """
    task = current_task()
    if task is None:
        return {}
    return {table.table_name: table.types_by_column() for table in task.schema.tables}


def match_report() -> matching.MatchReport | None:
    """How the upload currently stands against the open Task, or None when none is open.

    Recomputed every run rather than stored: it is a reading of state that four different
    controls on this page can change, and a stored copy is one that can be a rerun out of
    date — which on this screen would mean a green banner over a file that no longer matches.
    """
    task = current_task()
    if task is None:
        return None
    return matching.check_upload(
        task.schema, engine_session.load_outcomes(), engine_session.semantic_types_by_table()
    )


def unmatched_tables(report: matching.MatchReport) -> list[str]:
    """Loaded tables the recipe has no place for — the candidates for a table remap."""
    return list(report.extra_tables)


# --------------------------------------------------------------------------------------
# Running (requirement 8.2)
# --------------------------------------------------------------------------------------


def rewrite_comments() -> bool:
    """Whether to redraft the commentary for this month's numbers. On unless turned off."""
    return bool(st.session_state.get(RT_REWRITE_KEY, True))


def apply_recorded_setup(schema: ChatType, table_names: list[str]) -> list[str]:
    """Requirement 8.2 step 1: the recorded links and column meanings, back onto these tables.

    `chat_types.session.apply_setup` already *is* this — a Task's schema is a `ChatType` for
    precisely that reason — so this is the two things that have to happen around it rather
    than a second implementation.

    Both of them are about not inheriting the session's own state. The relationships are
    cleared first, because `apply_setup` sets them only when the recipe has some and a Task
    with no links would otherwise be run against whatever links the user had confirmed by
    hand. The statement list is cleared because `relationships.enforce` replays it as part of
    the rebuild, and on a **second** run it holds the first run's calculated columns — which
    would then be applied twice, failing on the `ALTER TABLE ADD` of a column that exists.

    This is the reason a run is not a read-only act on the session: it replaces the loaded
    tables' links and column meanings with the Task's. The page says so before it is pressed.
    """
    engine_session.set_relationships([])
    engine_session.set_statements([])
    return chat_types_session.apply_setup(schema, table_names)


def execute_run(user_id: int, *, on_stage=None) -> RunResult:
    """Runs the open Task against the loaded tables and keeps the result.

    Raises:
        TaskRunError: if there is no Task open, or nothing loaded to run it against. Every
            other failure is one step of the run and is recorded on the result — a run that
            produced eleven of twelve report items is worth having.
    """
    task = current_task()
    if task is None:
        raise TaskRunError("Pick a task to run first.")

    tables = engine_session.table_names()
    if not tables:
        raise TaskRunError("Upload this month's files before running the task.")

    # Deep copied, so a fallback rewriting an item's SQL edits this run's copy and not the
    # recipe — which is what lets the same Task be run twice from the same screen.
    running = copy.deepcopy(task)

    if on_stage is not None:
        on_stage("Restoring the recorded links and column meanings…")
    warnings = apply_recorded_setup(running.schema, tables)

    setup_step = StepResult(
        KIND_SETUP,
        "Recorded setup",
        STATUS_OK,
        f"{len(running.schema.relationships)} link(s) and "
        f"{len(running.schema.descriptions)} column description(s) restored.",
        notes=list(warnings),
    )

    result = replay.run_recipe(
        engine_session.connection(),
        running,
        loaded_tables=tables,
        schema_context=engine_session.schema_context(),
        profile=llm_session.active_profile(user_id),
        rewrite_comments=rewrite_comments(),
        on_stage=on_stage,
    )
    # The setup goes first in the list because it happened first, and the list's order is
    # what a reader uses to work out which failure caused which.
    result.steps.insert(0, setup_step)

    # Recorded *after* the replay applied them: `relationships.enforce` replays this list on
    # every later rebuild, so setting it before would have applied every statement twice.
    engine_session.set_statements(running.calculated_columns)

    dashboard_session.set_report(result.report)
    st.session_state[RT_RESULT_KEY] = result
    logger.info("Ran task '%s': %s", running.display_name(), result.headline())
    return result


def last_result() -> RunResult | None:
    """The most recent run's summary and report, or None if this Task hasn't been run."""
    return st.session_state.get(RT_RESULT_KEY)


def clear_result() -> None:
    st.session_state.pop(RT_RESULT_KEY, None)
    st.session_state.pop(RT_REPORT_KEY, None)


# --------------------------------------------------------------------------------------
# Flash messages and dialogs
# --------------------------------------------------------------------------------------


def queue_flash(message: str) -> None:
    """Holds a message to be shown after the rerun that follows a press.

    Anything drawn during a handler that ends in `st.rerun` is thrown away with the rest of
    that run's output, so "Opened" written just before the rerun would never reach the person
    it is for — the same reason `tasks/session.py` queues its own.
    """
    st.session_state[RT_FLASH_KEY] = message


def consume_flash() -> str | None:
    return st.session_state.pop(RT_FLASH_KEY, None)


def open_dialog(action: str, payload: dict | None = None) -> None:
    """Flags which dialog should be showing.

    Held in session state rather than read from a button's return value, for the reason
    `engine/session.py::open_dialog` documents: a dialog containing widgets closes mid-edit
    if it depends on a control's return value surviving the rerun.
    """
    st.session_state[RT_DIALOG_KEY] = {"action": action, "payload": payload or {}}


def close_dialog() -> None:
    st.session_state.pop(RT_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    pending = st.session_state.get(RT_DIALOG_KEY)
    if not pending:
        return None
    return pending["action"], pending.get("payload", {})


# --------------------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------------------


def reset_runner() -> None:
    """Clears every `rt_*` key, this run's report included.

    Called by Start over, which discards the loaded tables — a run describes those. The saved
    Task in SQLite is untouched: it is a recipe, and the whole point of one is that it
    outlives the data it was written for.
    """
    for key in (
        RT_TASK_KEY,
        RT_MAPPING_TABLES_KEY,
        RT_MAPPING_COLUMNS_KEY,
        RT_RESULT_KEY,
        RT_DIALOG_KEY,
        RT_FLASH_KEY,
        RT_REPORT_KEY,
    ):
        st.session_state.pop(key, None)
