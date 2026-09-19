"""Transform Data: upload several tables, build steps across them, preview, download.

The Data Cleaner cleans one table at a time. This page is for the work that needs more than
one: joining two files, looking a cost up in a price list, appending twelve monthly exports,
then summarising the result. Every step names the table it reads and the table it writes, so
a workspace of named tables grows as the steps run.

Two layout rules inherited from elsewhere in the app, both load-bearing:

**`st.file_uploader` is drawn on every run, and nothing above it may end a run.** Ending a
run before that widget exists drops every file it holds. So the flash message sits in an
always-present container, and every button that reruns is below the uploader.

**A dialog is opened by a session-state flag, never by a button's return value.**
`if st.button(...): show_dialog()` closes itself the moment the user touches a widget
inside, because the button reads False on that rerun. See `transform.session.open_dialog`.
"""

import logging

import streamlit as st

from app_pages import saved_picker
from app_pages.transform_form import preset_source_table, render_step_form, seed_form
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from cleaner import loaders, profiling
from cleaner.exceptions import DataCleanerError
from engine import session as engine_session
from sidebar import render_sidebar
from transform import db as transform_db
from transform import session
from transform.exceptions import PipelineStorageError, TransformError
from transform.matching import required_columns_by_table
from transform.pipeline import describe_steps, frames_created_by, step_headline, validate_step
from transform.registry import get_operation, operations_by_category
from transform.workspace import NamedFrame, frame_names

logger = logging.getLogger(__name__)

#: The two picker widgets. Named constants because the shortcut buttons write them before
#: the widgets are drawn, and a typo there would fail silently as "the picker ignored me".
PICK_CATEGORY_KEY = "tf_pick_category"
PICK_OPERATION_KEY = "tf_pick_operation"

# Which pages can receive the transformed tables, and where each one lives. Chat with Data
# is the only consumer so far (it's the only page with an adoption path into the Data
# Engine) — add an entry here when a future page grows one. The same constant the Data
# Cleaner keeps, for the same reason.
EXPORT_DESTINATIONS: dict[str, str] = {
    "Chat with Data": "app_pages/chat_with_data.py",
}

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception(
        "Database error while loading profile for user_id %s.", st.session_state.get("user_id")
    )
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


# --------------------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------------------


def _render_upload() -> tuple[dict[str, NamedFrame], list[str]]:
    """The uploader, the per-file sheet choice, and the Load gate.

    Returns the workspace of uploaded tables and any problems reading them.
    """
    with st.container(border=True):
        # Consumed before the uploader is instantiated: Streamlit won't allow a widget's
        # own session_state key to be written once that widget exists this run.
        session.consume_start_over()

        uploads = st.file_uploader(
            "Upload CSV or Excel files. Each sheet of an Excel file becomes its own table.",
            type=["csv", "txt", "tsv", "xlsx", "xlsm"],
            accept_multiple_files=True,
            key=session.TF_UPLOADER_KEY,
            max_upload_size=session.MAX_UPLOAD_SIZE_MB,
            help="Every cell is read as text first, so leading zeros in invoice numbers and codes survive. Use 'Change column type' to make a column numeric before totalling or joining on it.",
        )

        _render_sheet_choice(uploads)
        _render_load_gate(uploads)

    return session.sync_uploads(uploads)


def _render_sheet_choice(uploads) -> None:
    """One sheet multiselect per Excel upload, defaulting to every sheet."""
    wanted = session.sheet_choice()
    for upload in uploads or []:
        if loaders.is_csv(upload.name):
            continue
        try:
            sheets = loaders.list_sheet_names(upload.getvalue(), upload.name)
        except DataCleanerError as error:
            logger.exception("Could not list the sheets in %s.", upload.name)
            st.warning(str(error), icon=":material/error:")
            continue

        wanted[upload.file_id] = st.multiselect(
            f"Sheets to use from **{upload.name}**",
            options=sheets,
            default=wanted.get(upload.file_id, sheets),
            key=f"tf_sheets_{upload.file_id}",
            help="Each sheet you pick becomes a table of its own that steps can read.",
        )


def _render_load_gate(uploads) -> None:
    """Holds newly dropped files back until the user presses Load.

    The phase-16 pattern: dropping five files in shouldn't start five parses before the
    user has finished choosing sheets.

    Ending the run here is safe — and tidier than letting `sync_uploads` pick the files up
    later in this same run, which would leave the "waiting to load" caption on screen
    underneath the tables it had just loaded. It is safe because this draws *below*
    `st.file_uploader`, so that widget already exists and keeps its files.
    """
    confirmed = session.confirmed_upload_ids()
    waiting = [upload for upload in (uploads or []) if upload.file_id not in confirmed]
    if not waiting:
        return

    st.caption(
        ":red[Waiting to load: " + ", ".join(upload.name for upload in waiting) + "]"
    )
    if st.button(
        f"Load {len(waiting)} file(s)",
        key="tf_load_files",
        type="primary",
        icon=":material/table_view:",
        help="Reads the files you've added and turns each one into a named table.",
    ):
        session.confirm_uploads([upload.file_id for upload in waiting])
        st.rerun(scope="app")


def _render_table_names(upload_workspace: dict[str, NamedFrame]) -> None:
    """The list of uploaded tables, each name editable and checked for uniqueness."""
    st.caption(":red[Rename a table if you'd like a shorter name to use in your steps.]")

    for name, held in list(upload_workspace.items()):
        left, middle, right = st.columns([2, 3, 2], vertical_alignment="center")
        with left:
            typed = st.text_input(
                "Table name",
                value=name,
                key=f"tf_name_{held.upload_file_id}_{held.source_label}",
                label_visibility="collapsed",
                help="What this table is called in your steps, and its sheet name in the download.",
            )
        with middle:
            st.caption(f":red[{held.source_label}]")
        with right:
            st.caption(f":red[{held.shape_label}]")

        if typed.strip() and typed != name:
            try:
                session.rename_uploaded_table(name, typed)
            except TransformError as error:
                logger.warning("Rename refused: %s", error)
                st.error(str(error), icon=":material/error:")
            else:
                st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------


def _render_tables(workspace: dict[str, NamedFrame]) -> None:
    """A tab per table, showing the first PREVIEW_ROWS rows."""
    names = frame_names(workspace)
    if not names:
        return

    # `key` + `on_change="rerun"` is what makes tabs stateful, which is what makes
    # `tab.open` meaningful — without it every tab's body is computed on every run.
    tabs = st.tabs(names, key=session.TF_TABS_KEY, on_change="rerun")
    for tab, name in zip(tabs, names, strict=True):
        # Only the visible tab renders; without this gate every rerun would draw every
        # table, which on a dozen tables is the difference between instant and sluggish.
        if not tab.open:
            continue
        with tab:
            _render_one_table(workspace[name])


def _render_one_table(held: NamedFrame) -> None:
    shown = held.frame.head(session.PREVIEW_ROWS)
    st.caption(f":red[{held.source_label} - {held.shape_label}]")
    st.dataframe(shown, key=f"tf_preview_{held.name}", width="stretch", hide_index=True)
    if len(held.frame) > len(shown):
        st.caption(
            f":red[Showing {len(shown):,} of {len(held.frame):,} rows. "
            f"The download has all of them.]"
        )
    _render_column_details(held)
    _render_fix_headers(held)


def _render_column_details(held: NamedFrame) -> None:
    """One row per column: type, how much is filled, how varied, a few example values.

    The same panel Data Cleaner shows, and for the same reason: the preview only shows the
    first few rows, so "this column is 40% blank" or "this looks like text, not numbers" is
    invisible until a step goes wrong. Collapsed, and over the *whole* table rather than the
    preview rows, so the numbers mean what they say.

    Folded away rather than dropped on a failure: a profile that can't be computed is a
    reason to hide one panel, not to take the table's preview down with it.
    """
    with st.expander("Column details", icon=":material/analytics:"):
        try:
            stats = profiling.column_stats(held.frame)
        except Exception:
            logger.exception("Could not profile the columns of '%s'.", held.name)
            st.caption(
                ":red[These columns couldn't be summarised. The table above is unaffected.]"
            )
            return

        st.dataframe(
            stats,
            key=f"tf_stats_{held.name}",
            width="stretch",
            hide_index=True,
            column_config={
                "column": "Column",
                "column_type": st.column_config.TextColumn(
                    "Type", help="A guess at the kind of value this column holds."
                ),
                "non_null": "Filled",
                "missing": "Blank",
                "missing_pct": st.column_config.NumberColumn("Blank %", format="%.1f"),
                "unique": "Distinct",
                "sample_values": "Examples",
            },
        )
        st.caption(":red[Counted over every row in this table, not just the preview above.]")


def _render_fix_headers(held: NamedFrame) -> None:
    """The Skip rows shortcut, on uploaded tables only.

    Skip rows is an ordinary step, not an upload setting, so that it re-runs next month with
    everything else and appears in the step list where it can be read. But it is also the
    step you need *before* you can use the page at all — until the junk rows above a file's
    real headers are gone, every column picker is offering `Unnamed: 1` — so burying it in a
    dropdown of twenty-nine would be the wrong place for it. This opens that same dialog with
    the operation and the table already chosen.

    Only on uploaded tables: a derived table's headers came from a step, not from a file.
    """
    if held.origin != "upload":
        return

    unnamed = [column for column in held.frame.columns if str(column).startswith("Unnamed")]
    if unnamed:
        st.caption(
            f":red[{len(unnamed)} column(s) have no name, so this file probably has rows "
            f"above its headers.]"
        )

    st.button(
        "Fix headers",
        key=f"tf_fix_headers_{held.name}",
        icon=":material/table_rows:",
        type="primary" if unnamed else "secondary",
        help="Opens the Add-a-step box set to 'Skip rows', so you can drop the junk rows "
        "above your column headers.",
        on_click=_open_fix_headers,
        args=(held.name,),
    )


def _open_fix_headers(table_name: str) -> None:
    """Opens the add dialog with Skip rows and `table_name` already chosen."""
    spec = get_operation("skip_rows")
    st.session_state[PICK_CATEGORY_KEY] = spec.category
    st.session_state[PICK_OPERATION_KEY] = spec
    preset_source_table(table_name)
    session.open_dialog("add")


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------


def _render_steps(workspace: dict[str, NamedFrame], report) -> None:
    """The numbered step list. Only the last step offers Edit and Delete."""
    steps = session.get_steps()
    st.subheader("Steps")

    if not steps:
        st.caption(":red[No steps yet. Add one to join, calculate, filter or summarise.]")
    else:
        outcomes = {outcome.index: outcome for outcome in report}
        for index, step in enumerate(steps):
            _render_one_step(index, step, outcomes.get(index), is_last=index == len(steps) - 1)

    st.button(
        "Add a step",
        key="tf_add_step",
        type="primary",
        icon=":material/add:",
        disabled=not workspace,
        help="Pick what you want to do and which table to do it to."
        if workspace
        else "Upload and load a file first.",
        on_click=session.open_dialog,
        args=("add",),
    )


def _render_one_step(index: int, step: dict, outcome, is_last: bool) -> None:
    with st.container(border=True):
        headline, actions = st.columns([5, 2], vertical_alignment="center")
        with headline:
            st.write(step_headline(step, index))
            if outcome is None:
                st.caption(":red[Not run - an earlier step stopped.]")
            elif outcome.status == "failed":
                st.error(outcome.message, icon=":material/error:")
            else:
                changed = f"{outcome.rows_before:,} to {outcome.rows_after:,} rows"
                st.caption(f":red[{changed}]")
                if outcome.message:
                    st.caption(f":red[{outcome.message}]")

        with actions:
            if not is_last:
                st.caption(":red[Only the last step can be changed.]")
                return
            edit, delete = st.columns(2)
            with edit:
                st.button(
                    "Edit",
                    key=f"tf_edit_{index}",
                    icon=":material/edit:",
                    help="Reopen this step with everything filled in as you left it.",
                    on_click=session.open_dialog,
                    args=("edit",),
                )
            with delete:
                st.button(
                    "Delete",
                    key=f"tf_delete_{index}",
                    icon=":material/delete:",
                    help="Remove this step. To change an earlier step, delete back to it.",
                    on_click=session.open_dialog,
                    args=("delete",),
                )


@st.dialog("Delete the last step?", on_dismiss=session.close_dialog)
def _render_delete_dialog() -> None:
    steps = session.get_steps()
    if not steps:
        session.close_dialog()
        return

    index = len(steps) - 1
    st.write(step_headline(steps[index], index))

    created = frames_created_by(steps, index)
    if created:
        st.warning(
            f"This also removes the table **{', '.join(created)}**, and anything you were "
            f"going to download from it.",
            icon=":material/warning:",
        )

    confirm, cancel = st.columns(2)
    with confirm:
        if st.button(
            "Delete step",
            key="tf_delete_confirm",
            type="primary",
            width="stretch",
            help="Removes this step for good.",
        ):
            session.remove_last_step()
            session.close_dialog()
            session.flash("Step deleted.")
            st.rerun(scope="app")
    with cancel:
        if st.button(
            "Keep it",
            key="tf_delete_cancel",
            width="stretch",
            help="Closes this box and changes nothing.",
        ):
            session.close_dialog()
            st.rerun(scope="app")


@st.dialog("Add a step", width="large", on_dismiss=session.close_dialog)
def _render_add_dialog(workspace: dict[str, NamedFrame]) -> None:
    _render_step_dialog(workspace, editing=False)


@st.dialog("Edit this step", width="large", on_dismiss=session.close_dialog)
def _render_edit_dialog(workspace: dict[str, NamedFrame]) -> None:
    _render_step_dialog(workspace, editing=True)


def _render_step_dialog(workspace: dict[str, NamedFrame], *, editing: bool) -> None:
    """The picker, the generated form, Preview, and Add / Save.

    When editing, the workspace the form validates against is the one that existed *before*
    the step being edited ran — otherwise the step would be checked against its own output,
    and a step that creates a new table would collide with the table it created last time.
    """
    existing = session.last_step() if editing else None
    steps = session.get_steps()
    base_workspace = workspace
    if editing and steps:
        base_workspace, _ = session.run_pipeline(
            {name: held for name, held in workspace.items() if held.origin == "upload"},
            steps[:-1],
        )

    spec = _render_operation_picker(existing)
    if spec is None:
        return

    seed_form(spec, existing)
    st.caption(f":red[{spec.summary}]")
    st.divider()

    step, missing = render_step_form(spec, base_workspace, existing)

    if missing:
        st.caption(":red[Still needed: " + ", ".join(missing) + "]")

    st.divider()
    preview, commit, cancel = st.columns([2, 2, 1])

    with preview:
        if st.button(
            "Preview",
            key="tf_preview_step",
            width="stretch",
            disabled=step is None,
            icon=":material/visibility:",
            help="Runs just this step and shows the first few rows of the answer.",
        ):
            _render_preview(base_workspace, step)

    with commit:
        if st.button(
            "Save changes" if editing else "Add step",
            key="tf_commit_step",
            type="primary",
            width="stretch",
            disabled=step is None,
            icon=":material/check:",
            help="Adds this step to the list and runs it.",
        ):
            _commit_step(step, base_workspace, editing=editing)

    with cancel:
        if st.button(
            "Cancel",
            key="tf_cancel_step",
            width="stretch",
            help="Closes this box without adding anything.",
        ):
            session.close_dialog()
            st.rerun(scope="app")


def _render_operation_picker(existing: dict | None):
    """Category, then operation within it. Locked to its operation when editing."""
    grouped = operations_by_category()

    if existing is not None:
        spec = get_operation(existing["operation"])
        st.write(f"**{spec.label}**")
        st.caption(
            ":red[A step's kind can't be changed. Delete it and add a new one to do "
            "something else.]"
        )
        return spec

    category = st.selectbox(
        "What do you want to do?",
        options=list(grouped),
        key=PICK_CATEGORY_KEY,
        help="Steps are grouped by the kind of job they do.",
    )
    specs = grouped.get(category, [])
    if not specs:
        return None

    return st.selectbox(
        "Step",
        options=specs,
        format_func=lambda spec: spec.label,
        key=PICK_OPERATION_KEY,
        help="Pick the step to add. Its description appears underneath.",
    )


def _render_preview(workspace: dict[str, NamedFrame], step: dict | None) -> None:
    """Runs one step and shows the answer, or says why it wouldn't run."""
    if step is None:
        return

    frame, warnings_out, error = session.preview_step(workspace, step)
    if error:
        st.error(error, icon=":material/error:")
        return

    for warning in warnings_out:
        st.warning(warning, icon=":material/info:")

    st.caption(f":red[{len(frame):,} row(s), {len(frame.columns):,} column(s)]")
    st.dataframe(
        frame.head(session.DIALOG_PREVIEW_ROWS),
        key="tf_step_preview",
        width="stretch",
        hide_index=True,
    )


def _commit_step(step: dict | None, workspace: dict[str, NamedFrame], *, editing: bool) -> None:
    """Validates the step and adds it, or shows why it can't be added."""
    if step is None:
        return

    try:
        validate_step(step, workspace)
    except TransformError as error:
        logger.info("Step refused: %s", error)
        st.error(str(error), icon=":material/error:")
        return

    if editing:
        session.replace_last_step(step)
    else:
        session.append_step(step)

    session.close_dialog()
    session.flash("Step saved." if editing else "Step added.")
    st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Saved pipelines
#
# The promise of this page, per section 1 of the requirements document: define the steps
# once, save them, and next month upload the new file and get the same answer. The whole
# mechanism is the Data Cleaner's cleaning template with the names changed — the same
# picker, the same four buttons, the same "choose it, then press Apply" order — because a
# user who has learned one should not have to learn the other.
#
# **Choosing a pipeline selects it; Apply runs it.** The two are deliberately separate, on
# the grounds `app_pages/data_cleaner.py` records: choosing has to come first, because the
# expected-files panel is what tells the user which files to go and find.
#
# **The bar records intent only.** It draws above `st.file_uploader`, and anything that
# ends a run up there drops every uploaded file — so selecting, applying and every dialog
# happen further down the page.
# --------------------------------------------------------------------------------------

PIPELINE_NEW_LABEL = "— New pipeline —"


def _saved_pipelines(user_id: int) -> list[dict] | None:
    """Every saved pipeline for this account, or None when the list couldn't be read.

    None rather than `[]`: "you have none" and "we couldn't look" lead to two different
    next actions.
    """
    try:
        return transform_db.list_pipelines(user_id)
    except PipelineStorageError as error:
        logger.exception("Could not list transform pipelines for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return None


def _pipeline_name_taken(user_id: int, name: str, ignoring: int | None = None) -> bool:
    """Whether another saved pipeline already answers to this name.

    Worth checking before saving: `transform.db.save_pipeline` treats a name already in use
    as "update that row", so a collision is not an error later — it is a silent overwrite of
    somebody's other pipeline.

    A lookup that fails returns False: refusing a name because the database hiccuped would
    block the user for a reason that has nothing to do with them.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return False
    try:
        rows = transform_db.list_pipelines(user_id)
    except PipelineStorageError:
        logger.exception("Could not check pipeline names for user %s.", user_id)
        return False
    return any(
        str(row["name"]).strip().casefold() == wanted and row["pipeline_id"] != ignoring
        for row in rows
    )


def _there_is_something_to_save() -> bool:
    """Whether the Save/Update buttons should be live.

    The uploader's own session_state key is consulted rather than the workspace, and that
    is the half that matters: the bar draws *above* `st.file_uploader`, so on the run where
    a file first arrives `sync_uploads` has not run yet and the workspace is still the
    previous run's — empty. Streamlit fills a widget's key from the browser before the
    script starts, so the uploader's value is already the new one at this point.
    """
    return bool(st.session_state.get(session.TF_UPLOADER_KEY) or session.get_steps())


def _render_pipeline_bar(rows: list[dict] | None) -> None:
    """The picker and its four buttons, above the uploader. Records intent only."""
    active_id, active_name = session.active_pipeline()
    listing = rows or []
    savable = _there_is_something_to_save()

    with st.container(border=True):
        picker_column, status_column = st.columns([2, 3], vertical_alignment="center")

        with picker_column:
            if rows is None:
                st.caption(":red[Your saved pipelines couldn't be listed — the message above says why.]")
            else:
                saved_picker.select_saved(
                    listing,
                    key=session.TF_PIPELINE_PICK_KEY,
                    id_key="pipeline_id",
                    label="Saved pipeline",
                    help=(
                        "A saved set of files and the steps that turn them into your answer. "
                        "Choosing one selects it — press Run this pipeline, below the uploader, "
                        "to run it against your files. Start typing to filter."
                    ),
                    include_none=True,
                    none_label=PIPELINE_NEW_LABEL,
                    index=saved_picker.option_index(
                        listing, id_key="pipeline_id", selected_id=active_id, include_none=True
                    ),
                )

        with status_column:
            if active_id is None:
                st.caption(
                    ":red[Build your steps as usual, then **Save as pipeline** to do it in "
                    "one click next month.]"
                )
            else:
                st.caption(f":red[Selected: **{active_name}**.]")

        schema_column, save_column, update_column, delete_column = st.columns(4)
        with schema_column:
            # Disabled rather than hidden on "New pipeline", so the bar keeps its shape.
            if st.button(
                "Show expected files",
                key="tf_pipeline_schema",
                icon=":material/schema:",
                width="stretch",
                disabled=active_id is None,
                help="What this pipeline expects to be uploaded, and what it does with it.",
            ):
                session.open_pipeline_dialog("schema")
        with save_column:
            if st.button(
                "Save as pipeline",
                key="tf_pipeline_save_as",
                icon=":material/bookmark_add:",
                width="stretch",
                disabled=not savable,
                help="Save your uploaded tables and every step under a new name.",
            ):
                session.open_pipeline_dialog("save_as")
        with update_column:
            if st.button(
                "Update pipeline",
                key="tf_pipeline_update",
                icon=":material/save:",
                type="primary",
                width="stretch",
                disabled=active_id is None or not savable,
                help="Overwrite this pipeline with the steps currently on screen.",
            ):
                session.open_pipeline_dialog(
                    "update", {"pipeline_id": active_id, "name": active_name}
                )
        with delete_column:
            if st.button(
                "",
                key="tf_pipeline_delete",
                icon=":material/delete:",
                width="stretch",
                disabled=active_id is None,
                help="Permanently delete this saved pipeline.",
            ):
                session.open_pipeline_dialog(
                    "delete", {"pipeline_id": active_id, "name": active_name}
                )


def _select_pipeline(user_id: int) -> None:
    """Reads the picker back and records the choice. Applies nothing.

    Called below `st.file_uploader` and below `sync_uploads`, which is both halves of the
    reason it isn't done in the bar: a run ending up there would drop the uploaded files,
    and the workspace it reports on wouldn't yet include what was just uploaded.
    """
    raw = st.session_state.get(session.TF_PIPELINE_PICK_KEY)
    # The "new pipeline" entry is a sentinel in the option list, not `None`: `st.selectbox`
    # reserves `None` for "nothing is selected", so an option carrying it could be offered
    # and never chosen. Both forms mean the same thing here.
    selected_id = None if raw in (None, saved_picker.NONE_OPTION) else raw
    active_id, _ = session.active_pipeline()
    if selected_id == active_id:
        return

    # Moving off a pipeline whose steps are still exactly as it installed them takes those
    # steps with it. Without this, last month's steps sat waiting on the page and replayed
    # against whatever was uploaded next — which is a failed step naming a table that was
    # never in this month's files, on a page that says "New pipeline" at the top.
    # Steps the user has since touched are never thrown away: that is Start over's job.
    installed_by = session.steps_came_from_pipeline()
    if installed_by is not None and installed_by == active_id:
        session.discard_pipeline_steps()
        session.flash(
            "Cleared the steps the previous pipeline installed. Your uploaded tables are "
            "untouched."
        )

    if selected_id is None:
        session.clear_active_pipeline()
        # The bar has already drawn "Selected: X" further up this run, so the caption would
        # contradict the picker until the next interaction without this.
        st.rerun(scope="app")
        return

    try:
        pipeline = transform_db.load_pipeline(selected_id, user_id)
    except PipelineStorageError as error:
        logger.exception("Could not load transform pipeline %s.", selected_id)
        st.error(str(error), icon=":material/error:")
        session.clear_active_pipeline()
        # The picker still holds the id that just refused to load, and leaving it there
        # would retry — and re-report — the same failure on every rerun from now on.
        session.queue_pipeline_selection(saved_picker.NONE_OPTION)
        return

    session.set_active_pipeline(pipeline)
    st.rerun(scope="app")


def _render_pipeline_status(upload_workspace: dict[str, NamedFrame]) -> None:
    """How the current upload measures up against the selected pipeline.

    Drawn after the uploader for the same reason the selection is acted on there — before
    `sync_uploads` the workspace is still the previous run's, so this would report on files
    the user has already replaced.
    """
    pipeline = session.active_pipeline_object()
    if pipeline is None:
        return

    match = session.match_pipeline(pipeline, upload_workspace)
    if match.ok and not match.has_notes:
        st.success(match.summary(), icon=":material/check_circle:")
        return

    with st.expander(f"{pipeline.display_name()} — {match.status_word()}", expanded=not match.ok):
        st.caption(f":red[{match.summary()}]")
        for problem in match.problems():
            st.warning(problem, icon=":material/error:")
        for note in match.notes():
            st.info(note, icon=":material/info:")
        if not match.ok:
            st.caption(
                ":red[Nothing runs until every expected file is here with the columns the "
                "steps need. Upload the missing one(s), then press Run this pipeline.]"
            )


def _render_pipeline_apply(upload_workspace: dict[str, NamedFrame]) -> None:
    """The Run button, and what happens when it is pressed.

    The whole action of a pipeline, in one visible press. It is drawn below the uploader
    because it reruns, and because the workspace it measures itself against is only
    complete once `sync_uploads` has seen this run's upload.

    A missing file or a missing required column **refuses the whole run** — section 6 of
    the requirements document. Half a working set is not what the pipeline means, and a
    step reading a table an earlier step was supposed to build would compute an answer from
    the wrong data and present it as fine.
    """
    pipeline = session.active_pipeline_object()
    if pipeline is None:
        return

    pressed = st.button(
        f"Run this pipeline: {pipeline.display_name()}",
        key="tf_pipeline_apply",
        icon=":material/play_arrow:",
        type="primary",
        width="stretch",
        disabled=not upload_workspace,
        help=(
            "Checks your uploaded files against this pipeline, renames them to the names "
            "its steps expect, then runs every saved step in order. Safe to press again "
            "after uploading more files."
        ),
    )
    if not pressed:
        return

    match = session.match_pipeline(pipeline, upload_workspace)
    if not match.ok:
        for problem in match.problems():
            st.error(problem, icon=":material/error:")
        st.caption(":red[Nothing was run. Fix the file(s) above and press Run again.]")
        return

    applied, problems = session.apply_pipeline(pipeline, match)
    for problem in problems:
        st.warning(problem, icon=":material/error:")
    session.flash(
        f"Ran “{pipeline.display_name()}” — {applied} table(s) matched, "
        f"{len(pipeline.steps)} step(s) applied."
    )
    # The tab strip is built from the workspace, which this has just changed: applying a
    # pipeline renames tables and adds every step, so the run has to start again to draw them.
    st.rerun(scope="app")


def _dismiss_pipeline_dialog() -> None:
    """Clears the flag when a dialog is dismissed by the X, ESC or a click outside.

    Without it the flag survives and the next unrelated rerun reopens the same dialog — the
    same reason `session.open_dialog` exists for the step actions.
    """
    session.close_pipeline_dialog()


@st.dialog("What this pipeline expects", width="large", on_dismiss=_dismiss_pipeline_dialog)
def _dialog_pipeline_schema(user_id: int, payload: dict) -> None:
    """The saved schema, read on demand rather than announced above the uploader.

    "This one expects sales and customer_master" is the answer to a question the user asks
    once — printing it permanently would put a box between them and their data on every run.
    """
    pipeline = session.active_pipeline_object()
    if pipeline is None:
        st.info("No pipeline is selected.", icon=":material/info:")
        return

    st.caption(f":red[{pipeline.summary_line()}]")
    if pipeline.description:
        st.write(pipeline.description)

    required = required_columns_by_table(
        pipeline.steps, {table.name: table.columns for table in pipeline.tables}
    )
    for table in pipeline.tables:
        needed = required.get(table.name) or []
        with st.expander(f"{table.name} — {len(needed)} required column(s)", icon=":material/table:"):
            st.caption(f":red[Saved from **{table.source_label()}**.]")
            if needed:
                st.caption("These columns must be in the file, by these exact names:")
                st.write(", ".join(f"`{column}`" for column in needed))
            else:
                st.caption(
                    "No particular column is needed — the steps use this table as a whole."
                )
            if table.columns:
                st.caption("Every column it had when the pipeline was saved:")
                st.write(", ".join(f"`{column}`" for column in table.columns))

    described = describe_steps(pipeline.steps)
    if described:
        st.caption(f":red[{len(described)} step(s), in this order:]")
        for position, line in enumerate(described, start=1):
            st.write(f"{position}. {line}")
    if pipeline.outputs:
        st.caption(":red[Downloads: " + ", ".join(f"**{name}**" for name in pipeline.outputs) + "]")

    if st.button(
        "Close",
        key="tf_pipeline_schema_close",
        width="stretch",
        help="Close this and carry on.",
    ):
        session.close_pipeline_dialog()
        st.rerun(scope="app")


@st.dialog("Save as pipeline", on_dismiss=_dismiss_pipeline_dialog)
def _dialog_save_pipeline(user_id: int, payload: dict) -> None:
    """Names the working set and writes it.

    A name already belonging to another pipeline is refused rather than obeyed:
    `save_pipeline` reads it as "update that row", which from here would be a silent
    overwrite of somebody else's month of work.
    """
    upload_workspace = payload.get("upload_workspace") or {}
    st.caption(
        f":red[{len(upload_workspace)} uploaded table(s) and {len(session.get_steps())} "
        "step(s), stored under one name.]"
    )

    typed = st.text_input(
        "Pipeline name",
        key="tf_pipeline_new_name",
        placeholder="e.g. Monthly sales pack",
        help="What this pipeline is called when you come back to pick it.",
    )
    description = st.text_area(
        "What it's for (optional)",
        key="tf_pipeline_new_description",
        placeholder="e.g. Joins sales to the customer master and totals by region.",
        help="Shown under the picker, so this can be identified six months from now.",
    )

    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Save pipeline",
            key="tf_pipeline_save_confirm",
            type="primary",
            width="stretch",
            disabled=not typed.strip(),
            help="Write these tables and their steps to a new saved pipeline.",
        ):
            if _pipeline_name_taken(user_id, typed):
                st.error(
                    f"You already have a pipeline called “{typed.strip()}” — pick it above "
                    "and use Update pipeline, or choose another name.",
                    icon=":material/error:",
                )
                return
            _write_pipeline(
                user_id, typed, description=description, pipeline_id=None,
                upload_workspace=upload_workspace,
            )
    with cancel_column:
        if st.button(
            "Cancel",
            key="tf_pipeline_save_cancel",
            width="stretch",
            help="Close without saving anything.",
        ):
            session.close_pipeline_dialog()
            st.rerun(scope="app")


@st.dialog("Update this pipeline?", on_dismiss=_dismiss_pipeline_dialog)
def _dialog_update_pipeline(user_id: int, payload: dict) -> None:
    """Confirms overwriting the selected pipeline with what is on screen.

    Asked rather than assumed: the button sits beside a picker, and overwriting a saved
    recipe is not something to discover afterwards.
    """
    name = payload.get("name") or ""
    upload_workspace = payload.get("upload_workspace") or {}
    pipeline = session.active_pipeline_object()
    st.write(
        f"**{name}** will be replaced by the {len(upload_workspace)} uploaded table(s) and "
        f"{len(session.get_steps())} step(s) as they stand now. The previous version isn't kept."
    )

    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Update pipeline",
            key="tf_pipeline_update_confirm",
            type="primary",
            width="stretch",
            help="Overwrite the saved pipeline with what is on screen.",
        ):
            _write_pipeline(
                user_id,
                name,
                description=pipeline.description if pipeline is not None else "",
                pipeline_id=payload.get("pipeline_id"),
                upload_workspace=upload_workspace,
            )
    with cancel_column:
        if st.button(
            "Cancel",
            key="tf_pipeline_update_cancel",
            width="stretch",
            help="Leave the saved pipeline as it is.",
        ):
            session.close_pipeline_dialog()
            st.rerun(scope="app")


@st.dialog("Delete this pipeline?", on_dismiss=_dismiss_pipeline_dialog)
def _dialog_delete_pipeline(user_id: int, payload: dict) -> None:
    """Confirms deleting a saved pipeline. The steps on screen are untouched either way."""
    name = payload.get("name") or ""
    pipeline_id = payload.get("pipeline_id")
    st.write(f"**{name}** will be permanently deleted. This can't be undone.")
    st.caption(":red[The tables and steps currently on screen aren't affected.]")

    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Delete pipeline",
            key="tf_pipeline_delete_confirm",
            type="primary",
            width="stretch",
            help="Permanently delete this saved pipeline.",
        ):
            try:
                transform_db.delete_pipeline(pipeline_id, user_id)
            except PipelineStorageError as error:
                logger.exception("Could not delete transform pipeline %s.", pipeline_id)
                st.error(str(error), icon=":material/error:")
                return
            session.clear_active_pipeline()
            # The picker's own key still holds the id that has just stopped being an
            # option, and this is well past the point where Streamlit will let it be
            # written — so the reset is queued for the top of the next run.
            session.queue_pipeline_selection(saved_picker.NONE_OPTION)
            session.close_pipeline_dialog()
            session.flash(f"Deleted “{name}”.")
            st.rerun(scope="app")
    with cancel_column:
        if st.button(
            "Cancel",
            key="tf_pipeline_delete_cancel",
            width="stretch",
            help="Keep this pipeline.",
        ):
            session.close_pipeline_dialog()
            st.rerun(scope="app")


def _write_pipeline(
    user_id: int,
    name: str,
    *,
    description: str,
    pipeline_id: int | None,
    upload_workspace: dict[str, NamedFrame],
) -> None:
    """Captures the working set and stores it, or says why it couldn't.

    Shared by Save as and Update because the two differ only in whether an id is carried:
    `transform.db.save_pipeline` inserts or updates from that alone.
    """
    try:
        captured = session.capture_pipeline(
            upload_workspace, name, description=description, pipeline_id=pipeline_id
        )
        saved = transform_db.save_pipeline(user_id, captured)
    except TransformError as error:
        logger.exception("Could not save transform pipeline '%s' for user %s.", name, user_id)
        st.error(str(error), icon=":material/error:")
        return

    session.set_active_pipeline(saved)
    session.queue_pipeline_selection(saved.pipeline_id)
    session.close_pipeline_dialog()
    session.flash(f"Saved “{saved.display_name()}” — {saved.summary_line()}.")
    st.rerun(scope="app")


PIPELINE_DIALOGS = {
    "schema": _dialog_pipeline_schema,
    "save_as": _dialog_save_pipeline,
    "update": _dialog_update_pipeline,
    "delete": _dialog_delete_pipeline,
}


def _render_pending_pipeline_dialog(user_id: int, upload_workspace: dict[str, NamedFrame]) -> None:
    """Opens whichever pipeline dialog is flagged.

    Rendered below the uploader, never from the bar that sets the flag: a dialog's own
    buttons end their run with `st.rerun`, and doing that above `st.file_uploader` would
    drop every uploaded file.

    The workspace is passed through the payload rather than read in each dialog, because
    Save and Update both need the uploaded tables and only this caller has them.
    """
    pending = session.pending_pipeline_dialog()
    if pending is None:
        return

    name, payload = pending
    if name not in PIPELINE_DIALOGS:
        session.close_pipeline_dialog()
        return

    PIPELINE_DIALOGS[name](user_id, {**payload, "upload_workspace": upload_workspace})


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------


def _render_export(workspace: dict[str, NamedFrame]) -> None:
    """Choose the tables to download, and the download button itself."""
    st.subheader("Download")

    names = frame_names(workspace)
    # Seeded before the widget is drawn — see `session.seed_export_selection` for why a
    # `default=` argument cannot do this job.
    session.seed_export_selection(workspace)

    chosen = st.multiselect(
        "Tables to download",
        options=names,
        key=session.TF_EXPORT_KEY,
        help="Each table becomes a sheet in one workbook. The workbook has every row, not just the 500 shown above.",
    )

    download, send, reset = st.columns([3, 2, 1])
    with download:
        _render_download_button(workspace, chosen)
    with send:
        _render_export_menu(workspace, chosen)
    with reset:
        if st.button(
            "Start over",
            key="tf_start_over",
            width="stretch",
            icon=":material/restart_alt:",
            help="Clears every table and every step so you can begin again.",
        ):
            session.queue_start_over()
            st.rerun(scope="app")


def _render_export_menu(workspace: dict[str, NamedFrame], chosen: list[str]) -> None:
    """Sends the chosen tables straight into another page's Data Engine and jumps there.

    The tables handed over are **the ones ticked for download**, not every named table in
    the workspace: a join's two raw halves aren't useful to chat over, only the result, and
    the box above is already where the user says which table is the answer.

    Adoption happens here, where the frames are in hand, rather than on the destination
    page — Transform Data keeps no DataFrames in session state, so a cross-page read would
    mean replaying the whole pipeline a second time. `adopt_transform_tables` reports a
    per-table failure as a warning rather than raising, so one bad table is skipped and the
    rest still switch.
    """
    if not chosen:
        st.button(
            "Export to",
            key="tf_export_disabled",
            disabled=True,
            width="stretch",
            icon=":material/output:",
            help="Tick at least one table above first.",
        )
        return

    destination = st.menu_button(
        "Export to",
        options=list(EXPORT_DESTINATIONS),
        key="tf_export_menu",
        icon=":material/output:",
        type="primary",
        width="stretch",
        help="Load the ticked tables into another page's Data Engine, then jump there.",
    )
    if destination is None:
        return

    frames = {name: workspace[name].frame for name in chosen if name in workspace}
    adopted, warnings = engine_session.adopt_transform_tables(frames)
    for warning in warnings:
        st.warning(warning, icon=":material/error:")
    if not adopted:
        return

    engine_session.refresh_dictionary()
    st.switch_page(EXPORT_DESTINATIONS[destination])


def _render_download_button(workspace: dict[str, NamedFrame], chosen: list[str]) -> None:
    """Builds the workbook up front, because a download button needs its bytes ready."""
    if not chosen:
        st.button(
            "Download workbook",
            key="tf_download_disabled",
            disabled=True,
            width="stretch",
            help="Choose at least one table first.",
        )
        return

    try:
        workbook = session.build_download(workspace, chosen)
    except TransformError as error:
        logger.exception("Could not build the transform workbook.")
        st.error(str(error), icon=":material/error:")
        return

    st.download_button(
        "Download workbook",
        data=workbook,
        file_name="transformed_data.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="tf_download",
        type="primary",
        width="stretch",
        icon=":material/download:",
        on_click="ignore",
        help="One .xlsx holding the tables you picked, plus a sheet listing your steps.",
    )


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


if profile is not None:
    render_sidebar(profile)

    with st.container(horizontal=True):
        st.subheader("🔀 Transform Data")
        st.write(
            ":blue[**Upload tables, build steps across them, and download one workbook.**]"
        )

    # Always here, usually empty. Writing the message straight onto the page would shift
    # everything below it down on the runs that have something to say.
    with st.container():
        message = session.consume_flash()
        if message:
            st.success(message, icon=":material/check_circle:")

    user_id = st.session_state["user_id"]
    # Applied before the picker is created, since Streamlit forbids writing a widget's own
    # key once it exists this run. Saving and deleting are what queue one.
    session.consume_pipeline_selection()

    pipeline_rows = _saved_pipelines(user_id)
    # Above the uploader, and records intent only — see that section's header comment.
    _render_pipeline_bar(pipeline_rows)

    upload_workspace, problems = _render_upload()

    # Everything that can end a run is below `st.file_uploader`, so ending one cannot drop
    # the files it holds.
    for problem in problems:
        st.warning(problem, icon=":material/error:")

    # Below the uploader for both halves of the reason in `_select_pipeline`: a rerun up
    # there drops the files, and the workspace is only current once `sync_uploads` has run.
    _select_pipeline(user_id)
    _render_pipeline_status(upload_workspace)
    _render_pipeline_apply(upload_workspace)
    _render_pending_pipeline_dialog(user_id, upload_workspace)

    if not upload_workspace:
        st.info(
            "Upload a CSV or Excel file, then press Load to get started.",
            icon=":material/upload_file:",
        )
    else:
        with st.expander("Tables you uploaded", expanded=False):
            _render_table_names(upload_workspace)

        workspace, report = session.run_pipeline(upload_workspace)

        failed = next((outcome for outcome in report if outcome.status == "failed"), None)
        if failed is not None:
            st.error(
                f"Step {failed.index + 1} ({failed.label}) couldn't run, so the steps after "
                f"it were skipped. {failed.message}",
                icon=":material/error:",
            )

        _render_tables(workspace)
        _render_steps(workspace, report)

        pending = session.pending_dialog()
        if pending == "add":
            _render_add_dialog(workspace)
        elif pending == "edit":
            _render_edit_dialog(workspace)
        elif pending == "delete":
            _render_delete_dialog()

        _render_export(workspace)
