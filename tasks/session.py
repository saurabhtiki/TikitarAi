"""The Task being built, in session state (requirement 7).

The only module in `tasks/` that imports Streamlit, matching the convention every other
package here follows — the model and the database are testable without `AppTest`.

Very little state of its own. A Task is **assembled from the state the other packages already
hold** — the engine's tables and links, `report_items`' list, `checks`' set, the report — so
what lives here is just the four things a Task has that nothing else owns: its name, its
description, its persona, and which saved row it came from.

`TB_REPORT_KEY` is the second half of requirement 7's "Pin to report". `dashboard/session.py`
holds an *active* report key; the Task Builder page declares this one at the top of every run,
and every producer below it — the report items view, the checks view — then pins into the
Task's report with no idea it is doing anything different from usual.
"""

import logging

import streamlit as st

from checks import session as checks_session
from dashboard import session as dashboard_session
from engine import session as engine_session
from report_items import session as report_items_session
from tasks.exceptions import TaskStorageError
from tasks.model import Task, capture, recipe_fingerprint

logger = logging.getLogger(__name__)

TB_NAME_KEY = "tb_task_name"
TB_DESCRIPTION_KEY = "tb_task_description"
TB_PERSONA_KEY = "tb_persona"
TB_TASK_ID_KEY = "tb_task_id"
TB_DIALOG_KEY = "tb_open_dialog"
TB_FLASH_KEY = "tb_flash"
# The three text boxes a loaded Task has to refill, queued rather than written. Opening a
# Task happens in a dialog drawn *below* the task bar, and Streamlit forbids writing a
# widget's own key once it exists this run — the same deferral `engine.session.queue_step_state`
# makes for the steps.
TB_PENDING_FIELDS_KEY = "tb_pending_fields"

# Is a Task open? Until one is, the page shows the gate — open an existing Task, or create a
# new one — and nothing else. One question first, so that every control below it is talking
# about a Task the user has already chosen.
TB_OPEN_KEY = "tb_task_open"
# `recipe_fingerprint` as of the last save or open. What "Unsaved changes" is measured
# against; absent means "never saved this session", which is not the same as "unchanged".
TB_SAVED_FINGERPRINT_KEY = "tb_saved_fingerprint"

# The Task's own report, kept apart from the session Dashboard's `db_report`. Two reports is
# the whole reason `dashboard.session` grew an active key — see that module.
TB_REPORT_KEY = "tb_report"


# --------------------------------------------------------------------------------------
# The four fields a Task has that nothing else owns
# --------------------------------------------------------------------------------------


def task_name() -> str:
    return str(st.session_state.get(TB_NAME_KEY) or "")


def task_description() -> str:
    return str(st.session_state.get(TB_DESCRIPTION_KEY) or "")


def persona() -> str:
    """Requirement 7.2's persona and context, set once on the Setup view.

    Read by both the Report-Items view and the Checks view, which is the point of it being
    one value: a Task speaks with one voice, and two boxes would be two answers to one
    question.
    """
    return str(st.session_state.get(TB_PERSONA_KEY) or "")


def task_id() -> int | None:
    """The saved row this Task came from, or None if it has never been saved."""
    return st.session_state.get(TB_TASK_ID_KEY)


def set_task_id(value: int | None) -> None:
    st.session_state[TB_TASK_ID_KEY] = value


def set_task_name(value: str) -> None:
    """Renames the open Task. Not a widget's key any more — the name is shown as a heading
    and changed through Rename, so a Save can no longer quietly retarget another Task."""
    st.session_state[TB_NAME_KEY] = (value or "").strip()


# --------------------------------------------------------------------------------------
# Which Task is open
# --------------------------------------------------------------------------------------


def is_task_open() -> bool:
    """True once the user has chosen — opened a saved Task, or created a new one.

    Until then the page draws the gate. The flag is separate from `task_id()`, because a
    brand-new Task is open and unsaved, and a `None` id cannot tell that apart from "the user
    hasn't decided yet".
    """
    return bool(st.session_state.get(TB_OPEN_KEY))


def start_new_task(name: str) -> None:
    """Opens a blank Task under `name`, discarding whatever recipe was on screen.

    The **loaded tables are deliberately left alone**: writing a second Task against the files
    already uploaded is the normal reason to do this, and re-uploading them would be a chore
    with no purpose. What goes is the recipe — the report items, the criteria and the report —
    since those are what the new Task is about to replace.

    The caller must have declared the Task's report active (`dashboard.session.use_report`)
    before this runs, or the reset lands on the session Dashboard's report instead.
    """
    report_items_session.reset_report_items()
    checks_session.reset_checks()
    dashboard_session.reset_dashboard()

    # Queued rather than written for `load_task`'s reason: the persona and description are
    # widgets, and this can run on a run where they already exist.
    st.session_state[TB_PENDING_FIELDS_KEY] = {
        TB_NAME_KEY: (name or "").strip(),
        TB_DESCRIPTION_KEY: "",
        TB_PERSONA_KEY: "",
    }
    set_task_id(None)
    _remember(Task(name=name))
    st.session_state[TB_OPEN_KEY] = True
    logger.info("Started a new task '%s'.", (name or "").strip())


def close_task() -> None:
    """Puts the gate back up. Nothing is discarded — whichever choice follows replaces it."""
    st.session_state.pop(TB_OPEN_KEY, None)


def _recipe_only_task() -> Task:
    """The parts `recipe_fingerprint` reads, and nothing else.

    `capture_task` would do, but it also asks DuckDB for every table's column types to build
    the schema — work this runs once a run to word a caption, and which the fingerprint then
    ignores anyway.
    """
    return Task(
        task_id=task_id(),
        name=task_name().strip(),
        description=task_description().strip(),
        persona=persona(),
        report_items=report_items_session.get_items(),
        checks=checks_session.get_set(),
        report=dashboard_session.get_report(),
    )


def _remember(task: Task) -> None:
    """Records what "saved" looks like, for `has_unsaved_changes`.

    A fingerprint that refuses to build is dropped rather than stored: the comparison then has
    nothing to match, and the Task reads as changed — which is the safe way round.
    """
    try:
        st.session_state[TB_SAVED_FINGERPRINT_KEY] = recipe_fingerprint(task)
    except TaskStorageError:
        logger.exception("Could not fingerprint task '%s' — it will read as unsaved.", task.display_name())
        st.session_state.pop(TB_SAVED_FINGERPRINT_KEY, None)


def mark_saved(task: Task) -> None:
    """Called with the Task that was just written, so the header stops saying "Unsaved"."""
    _remember(task)


def has_unsaved_changes() -> bool:
    """Whether the recipe on screen differs from the one last saved or opened.

    Read once a run to word the header, and again when the user asks to switch Tasks — the
    one moment unsaved work could actually be lost. Anything it cannot measure counts as
    changed.
    """
    stored = st.session_state.get(TB_SAVED_FINGERPRINT_KEY)
    if stored is None:
        return True
    try:
        return recipe_fingerprint(_recipe_only_task()) != stored
    except TaskStorageError:
        logger.exception("Could not compare the open task against its saved copy.")
        return True


# --------------------------------------------------------------------------------------
# Capture and load
# --------------------------------------------------------------------------------------


def capture_task() -> Task:
    """Assembles a Task from everything currently on screen.

    Reads from five packages, and deliberately reads rather than being told: a Save that took
    its inputs as arguments would let the page hand over a stale copy of any one of them, and
    the failure — a Task missing the criteria the user had just added — would not surface
    until the Task was next run.
    """
    return capture(
        task_name(),
        description=task_description(),
        persona=persona(),
        semantic_types_by_table=engine_session.semantic_types_by_table(),
        relationships=engine_session.get_relationships(),
        dictionary=engine_session.get_dictionary(),
        statements=engine_session.get_statements(),
        report_items=report_items_session.get_items(),
        checks=checks_session.get_set(),
        report=dashboard_session.get_report(),
        task_id=task_id(),
    )


def load_task(task: Task) -> None:
    """Puts a saved Task back on screen, ready to edit.

    What comes back is the **recipe**: the persona, the report items, the criteria and the
    report's arrangement. What deliberately does *not* happen is applying the saved schema to
    the data currently loaded, or replaying the column steps against it — that is requirement
    8's Run a Task, and doing half of it here would leave the session in a state neither
    stage owns. Every restored item carries its SQL, so one press per item re-runs it.

    The name, description and persona have to reach their **widgets' own keys** — a text box
    that already exists ignores `value=` for the rest of the session — but cannot be written
    here: this runs inside a dialog opened from the task bar, so those widgets already exist
    this run and Streamlit refuses. They are queued instead, and `consume_pending_fields`
    applies them at the top of the next run, before the bar is drawn.
    """
    st.session_state[TB_PENDING_FIELDS_KEY] = {
        TB_NAME_KEY: task.name,
        TB_DESCRIPTION_KEY: task.description,
        TB_PERSONA_KEY: task.persona,
    }
    set_task_id(task.task_id)

    report_items_session.replace_items(task.report_items)
    checks_session.replace_set(task.checks)
    dashboard_session.set_report(task.report)

    # Fingerprinted from the Task itself rather than from `capture_task()`, which cannot run
    # yet: the three fields above are still queued. It is the same value capture will produce
    # on the next run, which is what makes a just-opened Task read as unchanged.
    _remember(task)
    st.session_state[TB_OPEN_KEY] = True

    logger.info(
        "Loaded task '%s': %d report item(s), %d criteria.",
        task.display_name(),
        len(task.report_items),
        len(task.checks.checks),
    )


def consume_pending_fields() -> None:
    """Applies whatever `load_task` queued. Call once, before the task bar is drawn."""
    pending = st.session_state.pop(TB_PENDING_FIELDS_KEY, None)
    if not pending:
        return
    for key, value in pending.items():
        st.session_state[key] = value


# --------------------------------------------------------------------------------------
# Flash messages
# --------------------------------------------------------------------------------------


def queue_flash(message: str) -> None:
    """Holds a message to be shown after the rerun that follows a press.

    Anything drawn during a handler that ends in `st.rerun` is thrown away with the rest of
    that run's output, so "Saved" written just before the rerun would never reach the person
    it is for — the same reason `settings.py` queues its own.
    """
    st.session_state[TB_FLASH_KEY] = message


def consume_flash() -> str | None:
    """Returns and clears the queued message. Shown exactly once, because it describes a
    press that has already happened."""
    return st.session_state.pop(TB_FLASH_KEY, None)


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def open_dialog(action: str, payload: dict | None = None) -> None:
    """Flags which dialog should be showing.

    Held in session state rather than read from a button's return value, for the reason
    `engine/session.py::open_dialog` documents: a dialog containing widgets closes mid-edit if
    it depends on a control's return value surviving the rerun.
    """
    st.session_state[TB_DIALOG_KEY] = {"action": action, "payload": payload or {}}


def close_dialog() -> None:
    st.session_state.pop(TB_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    """The dialog that should render this run, as `(action, payload)`, or None."""
    pending = st.session_state.get(TB_DIALOG_KEY)
    if not pending:
        return None
    return pending["action"], pending.get("payload", {})


# --------------------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------------------


def reset_task_builder() -> None:
    """Clears every `tb_*` key, the Task's report included.

    Called by Start over, which discards the loaded tables. A saved Task in SQLite is
    untouched: it is a recipe, and the whole point of one is that it outlives the data it was
    written for.
    """
    for key in (
        TB_NAME_KEY,
        TB_DESCRIPTION_KEY,
        TB_PERSONA_KEY,
        TB_TASK_ID_KEY,
        TB_DIALOG_KEY,
        TB_FLASH_KEY,
        TB_PENDING_FIELDS_KEY,
        TB_REPORT_KEY,
        TB_OPEN_KEY,
        TB_SAVED_FINGERPRINT_KEY,
    ):
        st.session_state.pop(key, None)
