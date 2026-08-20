"""Run a Task — pick a saved recipe, upload this month's files, get the report (requirement 8).

Task Builder records a whole analysis; this page plays it back. Nothing is typed and, when
every stored statement still works, **nothing is asked of a model to produce the numbers**:
every report item and every criteria came out of storage carrying the SQL that produced it.
The one AI call a run makes by design is redrafting the comments for this month's figures,
and the checkbox beside Run is how that is declined.

Available to **any logged-in user** (requirement 8's first line), unlike Task Builder — a
Task is built by an admin and run by whoever runs the month. Each account still only sees its
own Tasks: `tasks/db.py` scopes every read to `user_id`, and shared ownership is explicitly
out of scope (requirement 9).

Two screens, in the order requirement 8.1 asks for.

**The picker.** Saved Tasks as cards, each with **Run** and **Show schema** — the dialog
§8.1 step 2 asks for, listing the expected files, every column's type, the links and the
column meanings, so the user knows what to upload before they upload it.

**The run screen.** Upload, then the match report (§8.1 step 4, `chat_types.matching`, which
is the schema matcher this stage needed and already had), then the remap controls where it
doesn't line up (§8.1 step 5), then Run, then the summary and the report.

Four things about the shape are load-bearing and are not obvious from reading the page:

**A remap is a rename applied while the file is read, never after.** Changing one drops the
uploaded tables so the next run reads them again under it (`runner.session._reload_for_mapping`).
Renaming a loaded table's column instead would leave it typed by detection under a name the
recipe declares a type for — the wrong-rows-with-no-error failure the declared-load path
exists to prevent.

**The upload widgets are mounted on every run, including on the picker.** A widget that stops
being rendered stops reporting its value and `sync_tables` drops every loaded table — so off
the run screen they are mounted inside a container hidden by this page's one stylesheet. That
container is not dead UI: deleting it loses the user's files the first time they go back to
pick a different Task. This is the arrangement `chat_with_data.py` documents at length.

**The page's top level is the same shape in every state**, for the reason `task_builder.py`
found the hard way: Streamlit addresses an element by its position among its parent's
children, and an element that appears only sometimes shifts everything below it. So the flash
message, the two screens and the upload mount each own a container that is always there.

**Running rewrites this session's setup.** The links and column meanings become the Task's,
because that is what replaying a recipe means (requirement 8.2 step 1). The run panel says so
before the button is pressed.
"""

import logging

import pandas as pd
import streamlit as st

from analyst import session as chat_session
from app_pages import report_view, setup_view
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from auth.service import is_authenticated
from chat_types import matching
from chat_types import session as chat_types_session
from checks import session as checks_session
from dashboard import session as dashboard_session
from engine import session as engine_session
from report_items import session as report_items_session
from runner import session as runner_session
from runner import templates
from runner.exceptions import TaskRunError
from runner.model import STATUS_FAILED, STATUS_FALLBACK, STATUS_OK, STATUS_SKIPPED
from sidebar import render_sidebar
from tasks import db as tasks_db
from tasks.exceptions import TaskStorageError
from tasks.model import Task

logger = logging.getLogger(__name__)

# The container the upload widgets live in whenever they are not meant to be seen, hidden by
# this page's one stylesheet. Named here because the CSS rule has to spell the same key.
HIDDEN_UPLOAD_MOUNT = "rt_upload_mount"

# Three keyed containers that exist on **every** run, whatever the page is showing. See the
# module docstring: they are not styling.
FLASH_SLOT = "rt_flash_slot"
PICKER_BODY = "rt_picker_body"
RUN_BODY = "rt_run_body"
UPLOAD_SLOT = "rt_upload_slot"

# One badge per step status, so the summary can be read without reading it.
STATUS_ICONS = {
    STATUS_OK: ":material/check_circle:",
    STATUS_FALLBACK: ":material/auto_awesome:",
    STATUS_FAILED: ":material/error:",
    STATUS_SKIPPED: ":material/remove:",
}


if not is_authenticated():
    st.error("Please log in to run a task.", icon=":material/lock:")
    st.stop()

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def _dismiss_dialog() -> None:
    """Clears the flag when a dialog is dismissed by the X, ESC or a click outside.

    Without it the flag survives, and the next unrelated rerun reopens the same dialog.
    """
    runner_session.close_dialog()


@st.dialog("What this task expects", width="large", on_dismiss=_dismiss_dialog)
def _dialog_schema(payload: dict) -> None:
    """Requirement 8.1 step 2: the files and columns to have ready, before uploading any.

    Read from the Task's own recorded schema rather than described in prose, so it cannot
    drift from what the match in step 4 actually measures the upload against.
    """
    task: Task = payload["task"]

    st.markdown(f"**{task.display_name()}**")
    if task.description:
        st.caption(task.description)

    if not task.schema.tables:
        st.warning(
            "This task was saved without any file schema, so there is nothing to match an "
            "upload against. Open it in Task builder and save it again with the files loaded.",
            icon=":material/warning:",
        )
    if task.schema.tables:
        st.caption(
            "Each table below is one file. **Download a blank file** gives you its headers to "
            "paste this month's data into — uploaded back, it matches with nothing to remap."
        )

    for table in task.schema.tables:
        st.markdown(f"##### {table.table_name}")
        st.download_button(
            f"Download a blank {table.table_name} file",
            data=templates.template_csv(table),
            file_name=templates.template_name(table),
            mime="text/csv",
            key=f"rt_template_{table.table_name}",
            icon=":material/download:",
            on_click="ignore",
            help="A CSV with these column headings and no rows, ready to fill in.",
        )
        st.dataframe(
            pd.DataFrame(
                {
                    "Column": [column.name for column in table.columns],
                    "Type": [matching.type_label(column.semantic_type) for column in table.columns],
                    "Meaning": [_meaning(task, table.table_name, column.name) for column in table.columns],
                }
            ),
            width="stretch",
            hide_index=True,
            key=f"rt_schema_{table.table_name}",
        )

    if task.schema.relationships:
        st.markdown("##### How the files link together")
        for link in task.schema.relationships:
            st.markdown(f"- {link.explained_label()}")

    if st.button("Close", key="rt_schema_close", icon=":material/close:", help="Close this list."):
        runner_session.close_dialog()
        st.rerun(scope="app")


def _meaning(task: Task, table: str, column: str) -> str:
    """The saved description for one column, or blank. Matched case-insensitively, as every
    other lookup of a table name in this app is — the name came from a filename."""
    for saved in task.schema.descriptions:
        if saved.table.strip().lower() == table.strip().lower() and saved.column == column:
            return saved.description
    return ""


DIALOGS = {
    # Steps 2 and 3's three, shared with Chat with data — the run screen renders the same
    # loaded-table controls, and Remove opens one of them.
    **setup_view.SETUP_DIALOGS,
    "schema": _dialog_schema,
}


def _render_pending_dialog() -> None:
    """The engine's dialogs and this page's, resolved from one flag each.

    Two registries rather than one because they are two owners: `engine.session` opens the
    setup dialogs from inside `setup_view`, and this page opens its own.
    """
    pending = engine_session.pending_dialog()
    if pending is not None:
        action, payload = pending
        if action in DIALOGS:
            DIALOGS[action](payload)
        else:
            engine_session.close_dialog()

    pending = runner_session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action in DIALOGS:
        DIALOGS[action](payload)
    else:
        runner_session.close_dialog()


# --------------------------------------------------------------------------------------
# The picker
# --------------------------------------------------------------------------------------


def _render_picker(user_id: int) -> None:
    """Requirement 8.1 step 1: which task, asked on a screen with nothing else on it."""
    st.markdown("#### Pick a task to run")

    try:
        saved = tasks_db.list_tasks(user_id)
    except TaskStorageError as error:
        logger.exception("Could not list saved tasks for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return

    if not saved:
        st.info(
            "You have no saved tasks yet. A task is built once in **Task builder** — the "
            "setup, the report items, the checks and the report — and run here every month.",
            icon=":material/info:",
        )
        return

    st.caption(
        "A task brings its own schema, links, column meanings, calculated columns, report "
        "items and checks. Upload this month's files on the next screen and it produces the "
        "same report over the new numbers."
    )

    for row in saved:
        _render_task_card(user_id, row)


def _render_task_card(user_id: int, row: dict) -> None:
    """One saved Task: what it is, what it expects, and the button that opens it."""
    task_id = row["task_id"]
    with st.container(border=True, key=f"rt_card_{task_id}"):
        st.markdown(f"**{row['name']}**")
        if row.get("description"):
            st.caption(row["description"])
        st.caption(f"Last saved {row['updated_at']}")

        run_column, schema_column = st.columns([2, 1])
        with run_column:
            if st.button(
                "Run this task",
                key=f"rt_open_{task_id}",
                type="primary",
                icon=":material/play_arrow:",
                width="stretch",
                help="Open this task, upload your files and produce its report.",
            ):
                task = _load_task(task_id, user_id)
                if task is not None:
                    runner_session.open_task(task)
                    runner_session.queue_flash(f"Opened “{row['name']}”. Upload this month's files below.")
                    st.rerun(scope="app")
        with schema_column:
            if st.button(
                "Show schema",
                key=f"rt_schema_{task_id}",
                icon=":material/table_chart:",
                width="stretch",
                help="See which files and columns this task expects, before you upload anything.",
            ):
                task = _load_task(task_id, user_id)
                if task is not None:
                    runner_session.open_dialog("schema", {"task": task})
                    st.rerun(scope="app")


def _load_task(task_id: int, user_id: int) -> Task | None:
    """Reads one saved Task, or says why it couldn't. None means don't act on it."""
    try:
        return tasks_db.load_task(task_id, user_id)
    except TaskStorageError as error:
        logger.exception("Could not load task %s for user %s.", task_id, user_id)
        st.error(str(error), icon=":material/error:")
        return None


# --------------------------------------------------------------------------------------
# The run screen — matching and remapping
# --------------------------------------------------------------------------------------


def _render_run_header(task: Task) -> None:
    """Which Task is open, and the two ways out of it."""
    title_column, schema_column, switch_column = st.columns([4, 1.2, 1.2], vertical_alignment="bottom")

    with title_column:
        st.markdown(f"#### {task.display_name()}")
        st.caption(task.description or "No description.")

    with schema_column:
        if st.button(
            "Show schema",
            key="rt_header_schema",
            icon=":material/table_chart:",
            width="stretch",
            help="See which files and columns this task expects.",
        ):
            runner_session.open_dialog("schema", {"task": task})
            st.rerun(scope="app")

    with switch_column:
        if st.button(
            "Pick another",
            key="rt_switch_task",
            icon=":material/swap_horiz:",
            width="stretch",
            help="Go back to the task list. Your uploaded files stay loaded.",
        ):
            runner_session.close_task()
            st.rerun(scope="app")


def _render_match(report: matching.MatchReport, loaded_tables: list) -> None:
    """Requirement 8.1 step 4: exactly how the upload stands against the recipe.

    Everything at once rather than one refused load at a time — the rule
    `chat_types.matching` is built around — and the blocking problems kept apart from the
    things merely worth knowing, because mixing them either buries the real problem or makes
    an ordinary tidy-up look like a failure.
    """
    if not loaded_tables:
        st.info(
            "Upload this month's files above. Press **Show schema** if you need to check what "
            "this task expects.",
            icon=":material/upload_file:",
        )
        return

    if report.ok:
        st.success(report.summary(), icon=":material/check_circle:")
    else:
        st.error(report.summary(), icon=":material/error:")
        for problem in report.problems():
            st.markdown(f"- {problem}")

    for note in report.notes():
        st.caption(f":grey[{note}]")


def _render_remap(task: Task, report: matching.MatchReport, loaded_tables: list) -> None:
    """Requirement 8.1 step 5: fix the mismatch by hand rather than abort the whole run.

    Two remaps, because two things can be named differently in this month's files: which
    **file** is which of the recipe's tables, and which **column** is which of its columns.

    The table remap is not in §8.1 step 5's letter, which names only columns, but it is the
    first mismatch every user meets: a table's name comes from its filename, so
    `salary_august.xlsx` against a recipe recorded on `salary.xlsx` is a missing table and
    three missing columns and an extra file, all describing one renamed file. Telling the user
    to rename their files would be a worse answer than the one this screen exists to give.
    """
    if report.ok:
        return

    st.markdown("##### Fix the mismatch")
    st.caption(
        "Say which uploaded file is which of the task's tables, and which column is which. "
        "Your files are read again with the mapping applied, so the task's saved types are "
        "used rather than guessed at."
    )

    _render_table_remap(report, loaded_tables)
    _render_column_remap(task, report)


def _render_active_mapping(loaded_tables: list) -> None:
    """What the mapping currently says, and the one button that takes it all back.

    Shown whether or not anything still mismatches, because a mapping that has *worked* takes
    its own control off the screen — the column stops being missing, so the dropdown that
    fixed it stops being drawn. Without this the user would be looking at a green banner with
    no way to see, or undo, the remap that earned it.
    """
    tables = runner_session.table_map()
    columns = runner_session.column_map()
    lines = []

    by_id = {table.table_id: table for table in loaded_tables}
    for table_id, name in tables.items():
        source = by_id[table_id].source_label if table_id in by_id else "a file no longer loaded"
        lines.append(f"**{source}** is being read as **{name}**.")
    for table, renames in columns.items():
        for found, expected in renames.items():
            lines.append(f"**{table}.{found}** is being read as **{expected}**.")

    if not lines:
        return

    with st.container(border=True, key="rt_active_mapping"):
        st.markdown("**Mapping in effect**")
        for line in lines:
            st.caption(line)
        if st.button(
            "Clear the mapping",
            key="rt_clear_mapping",
            icon=":material/undo:",
            help="Forget every remap and read your files again as they are named.",
        ):
            runner_session.clear_mapping()
            runner_session.reload_after_mapping()
            st.rerun(scope="app")


def _render_table_remap(report: matching.MatchReport, loaded_tables: list) -> None:
    """A dropdown per unmatched file, naming which of the recipe's missing tables it is."""
    spare = runner_session.unmatched_tables(report)
    if not report.missing_tables or not spare:
        return

    by_name = {table.table_name: table for table in loaded_tables}
    for name in spare:
        table = by_name.get(name)
        if table is None:
            continue
        chosen = st.selectbox(
            f"“{table.source_label}” is which table?",
            options=["— not part of this task —", *report.missing_tables],
            key=f"rt_map_table_{table.table_id}",
            help=(
                "Pick the task's table this file holds. Leave it unmapped to have the file "
                "ignored — extra files are dropped rather than refused."
            ),
        )
        wanted = None if chosen.startswith("—") else chosen
        if wanted != runner_session.table_map().get(table.table_id):
            runner_session.map_table(table.table_id, wanted)
            st.rerun(scope="app")


def _render_column_remap(task: Task, report: matching.MatchReport) -> None:
    """A dropdown per missing column, naming which column in the file holds it."""
    if not report.missing_columns:
        return

    types = engine_session.semantic_types_by_table()
    for missing in report.missing_columns:
        expected = task.schema.table(missing.table)
        spare = [
            column
            for column in types.get(missing.table, {})
            if expected is None or column not in expected.column_names
        ]
        if not spare:
            st.caption(
                f":grey[**{missing.column}** is missing from **{missing.table}** and there is "
                "no spare column in that file to map it to — the column has to be added to "
                "the file itself.]"
            )
            continue

        chosen = st.selectbox(
            f"“{missing.column}” in {missing.table} is which column in your file?",
            options=["— not in this file —", *spare],
            key=f"rt_map_column_{missing.table}_{missing.column}",
            help="Pick the column in your file that holds this. It is renamed as the file is read.",
        )
        wanted = None if chosen.startswith("—") else chosen
        current = {
            found: target
            for found, target in runner_session.column_map().get(missing.table, {}).items()
            if target == missing.column
        }
        if wanted != (next(iter(current), None)):
            for found in current:
                runner_session.map_column(missing.table, found, None)
            if wanted:
                runner_session.map_column(missing.table, wanted, missing.column)
            st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# The run screen — running
# --------------------------------------------------------------------------------------


def _render_run_controls(user_id: int, report: matching.MatchReport, loaded_tables: list) -> None:
    """The one button, and the two honest warnings that belong beside it."""
    st.markdown("##### Run")

    if not loaded_tables:
        return

    if not report.ok:
        st.warning(
            "Fix the problems above before running — the task's report items are written "
            "against the columns it expects, and running without them would fail item by item.",
            icon=":material/warning:",
        )
        return

    st.checkbox(
        "Rewrite the comments for this month's numbers",
        key=runner_session.RT_REWRITE_KEY,
        value=runner_session.rewrite_comments(),
        help=(
            "Redrafts each item's comment and each criteria's remarks with the task's persona, "
            "using an AI call per item. Turn it off to keep the wording saved with the task — "
            "right when you have written the comments yourself."
        ),
    )

    st.caption(
        ":grey[Running restores the task's links and column meanings onto the files you have "
        "loaded, replacing any you set up by hand in this session.]"
    )

    if st.button(
        "Run task",
        key="rt_run",
        type="primary",
        icon=":material/play_arrow:",
        help="Replay the whole recipe against the files above and build the report.",
    ):
        _run(user_id)


def _run(user_id: int) -> None:
    """Presses Run: the replay, narrated (requirement 8.2 step 7).

    `st.status` rather than `st.spinner`, because a run against a real month's files is
    minutes of work with a dozen named stages, and "please wait" for that long reads as a
    screen that has hung. The callback is what `runner.replay` calls as it moves on.
    """
    with st.status("Running this task…", expanded=True) as status:
        def announce(message: str) -> None:
            status.update(label=message)
            st.write(message)

        try:
            result = runner_session.execute_run(user_id, on_stage=announce)
        except TaskRunError as error:
            logger.exception("A task run could not start for user %s.", user_id)
            status.update(label="The run couldn't start.", state="error")
            st.error(str(error), icon=":material/error:")
            return

        status.update(
            label=result.headline(),
            state="complete" if not result.failures() else "error",
            expanded=False,
        )
    st.rerun(scope="app")


def _render_summary(result) -> None:
    """Requirement 8.2 step 5: what succeeded, what needed the fallback, and what failed.

    Before the report and before the download buttons, deliberately — this is the screen that
    decides whether the report below it is fit to send, and putting it after would be putting
    it where nobody reads it.
    """
    st.markdown("##### What happened")

    if result.clean():
        st.success(result.headline(), icon=":material/check_circle:")
    elif result.failures():
        st.error(result.headline(), icon=":material/error:")
    else:
        st.warning(result.headline(), icon=":material/auto_awesome:")

    st.caption(f":grey[Run finished {result.finished_at}.]")

    with st.expander(
        "Step by step",
        expanded=not result.clean(),
        icon=":material/list:",
    ):
        for position, step in enumerate(result.steps):
            with st.container(border=True, key=f"rt_step_{position}"):
                st.markdown(f"{STATUS_ICONS.get(step.status, '')} **{step.label}** — {step.status_word()}")
                if step.detail:
                    st.caption(step.detail)
                for note in step.notes:
                    st.caption(f":orange[{note}]")


def _render_start_over() -> None:
    """Discards this session's data and everything written against it.

    The saved Tasks in SQLite are untouched: they are recipes, and the whole point of one is
    that it outlives the data it was written for. It closes the open Task too, since the run
    it describes is about files that are being discarded.
    """
    if st.button(
        "Start over",
        key="rt_start_over_button",
        icon=":material/refresh:",
        help="Discard every loaded table and this run's report. Your saved tasks are not affected.",
    ):
        engine_session.queue_start_over()
        # Cleared here rather than inside `reset_engine`, so `engine/` keeps no dependency on
        # the packages above it. Ordered so the report goes while it is still the active one.
        dashboard_session.reset_dashboard()
        runner_session.reset_runner()
        report_items_session.reset_report_items()
        checks_session.reset_checks()
        chat_session.reset_chat()
        chat_types_session.forget_upload()
        st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


if profile is not None:
    render_sidebar(profile)
    user_id = st.session_state["user_id"]

    st.subheader("▶️ Run a task")
    st.write(
        ":blue[**Pick a saved task, upload this month's files, and get its report — nothing "
        "to type, and no AI needed to produce the numbers while the saved SQL still runs.**]"
    )

    # The whole reason `dashboard.session` has an active report key. Said at the top of the
    # run, before anything below can read or write a report, so this run's results land here
    # rather than in the session Dashboard or a Task Builder's report.
    dashboard_session.use_report(runner_session.RT_REPORT_KEY)

    # A container that is always here and usually empty. Writing the message straight onto the
    # page would make every element below it sit one position lower on the runs that have
    # something to say, which is how a browser ends up showing both positions at once.
    with st.container(key=FLASH_SLOT):
        flash = runner_session.consume_flash()
        if flash:
            st.success(flash, icon=":material/check_circle:")

    # Emitted on every run, whatever is showing. The rule only bites when the container it
    # names exists, and a stylesheet that comes and goes is one more element moving the page
    # around underneath itself.
    st.html(f"<style>.st-key-{HIDDEN_UPLOAD_MOUNT} {{ display: none; }}</style>")

    # Applied before any expander is created, since Streamlit forbids writing a widget's own
    # key once it exists this run.
    engine_session.consume_step_state()

    task = runner_session.current_task()

    if task is None:
        with st.container(key=PICKER_BODY):
            # The upload widgets are mounted even here. A widget that stops being rendered
            # stops reporting its value, and `sync_tables` would then drop every loaded table —
            # so a trip back to the picker to run a second task against the same files would
            # arrive with the files gone.
            with st.container(key=UPLOAD_SLOT), st.container(key=HIDDEN_UPLOAD_MOUNT):
                setup_view.mount_upload()
            _render_pending_dialog()
            _render_picker(user_id)
        st.stop()

    with st.container(key=RUN_BODY):
        tables_before = set(engine_session.get_tables())
        _render_run_header(task)

        with st.container(key=UPLOAD_SLOT):
            with st.expander(
                "Step 1 · This month's files",
                key=engine_session.STEP_UPLOAD,
                on_change="rerun",
                # A **constant**, like every `expanded=` in this app: Streamlit re-applies the
                # argument whenever its value changes, so anything dynamic would force the step
                # shut the instant a file loaded and keep it shut.
                expanded=True,
                icon=":material/upload_file:",
            ) as upload_step:
                # Ungated on `upload_step.open`: a collapsed step must still instantiate the
                # uploader, and the mapping must still be in force when it does.
                loaded_tables = setup_view.mount_upload(
                    runner_session.declared_types(),
                    table_names=runner_session.table_map(),
                    column_renames=runner_session.column_map(),
                )
                if upload_step.open:
                    setup_view.render_upload_report(loaded_tables)

        if set(engine_session.get_tables()) != tables_before:
            # The panels below read the table set as it stood at the top of this run, which the
            # upload has just changed. One rerun brings them back in step.
            st.rerun(scope="app")

        _render_pending_dialog()

        match_report = runner_session.match_report()
        st.markdown("##### Step 2 · Does it match the task?")
        _render_match(match_report, loaded_tables)
        _render_remap(task, match_report, loaded_tables)
        _render_active_mapping(loaded_tables)

        _render_run_controls(user_id, match_report, loaded_tables)

        result = runner_session.last_result()
        if result is not None:
            st.divider()
            _render_summary(result)
            st.divider()
            st.markdown("##### The report")
            report_view.render_report_output(dashboard_session.get_report())

        if loaded_tables:
            st.divider()
            _render_start_over()
