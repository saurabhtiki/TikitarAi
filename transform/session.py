"""Session state and cached replay for the Transform Data page.

The only module in `transform/` that imports Streamlit, mirroring the convention the
`cleaner` and `engine` packages already follow: everything else in the package stays pure
and testable without `AppTest`.

**No DataFrames live in session state.** What is stored is the uploaded files' bytes, the
table names, and the step list — all small, all JSON-ish. The tables themselves are derived
by `st.cache_data`, so an eviction costs a recompute rather than the user's work. This is
also what makes phase 26 straightforward: what is stored here is very nearly what a saved
pipeline needs to be.

**Every change replays the whole pipeline from the uploads.** Not incremental snapshots.
The step list only ever grows or shrinks at the tail (section 7 of the requirements
document), the replay is memoized, and it is the exact code path phase 27 needs to re-run a
saved pipeline against next month's files — so building an incremental version alongside it
would be two things to keep correct instead of one.
"""

import logging

import pandas as pd
import streamlit as st

from cleaner import loaders
from cleaner.exceptions import DataCleanerError
from transform import matching, pipeline as pipeline_model
from transform import template as template_model
from transform.exceptions import DuplicateFrameNameError, TransformError
from transform.export import build_transform_workbook
from transform.matching import PipelineMatch
from transform.pipeline import StepOutcome, TransformStep
from transform.template import SavedPipeline
from transform.workspace import (
    NamedFrame,
    frame_names,
    name_key,
    normalise_frame_name,
    suggest_frame_name,
)

logger = logging.getLogger(__name__)

TF_UPLOADER_KEY = "tf_uploader"
TF_CONFIRMED_KEY = "tf_confirmed_uploads"
TF_FILE_BYTES_KEY = "tf_file_bytes"
TF_TABLE_NAMES_KEY = "tf_table_names"
TF_SHEETS_KEY = "tf_sheet_choice"
TF_STEPS_KEY = "tf_steps"
TF_DIALOG_KEY = "tf_open_dialog"
TF_FLASH_KEY = "tf_flash"
TF_EXPORT_KEY = "tf_export_selection"
TF_TABS_KEY = "tf_table_tabs"
TF_EXPORT_SEEDED_KEY = "tf_export_seeded_for"
TF_START_OVER_KEY = "tf_start_over_pending"

# Saved pipelines. The picker widget's own key and the "which one was just chosen but not
# yet acted on" flag are separate for the reason `cleaner.session` records: the bar draws
# *above* `st.file_uploader`, and acting on a selection there would end the run before that
# widget exists — which drops every uploaded file.
TF_PIPELINE_ID_KEY = "tf_pipeline_id"
TF_PIPELINE_NAME_KEY = "tf_pipeline_name"
TF_PIPELINE_OBJECT_KEY = "tf_pipeline_object"
TF_PIPELINE_PICK_KEY = "tf_pipeline_pick"
TF_PIPELINE_PENDING_KEY = "tf_pipeline_pending"
TF_PIPELINE_DIALOG_KEY = "tf_pipeline_dialog"
# Which saved pipeline put the current steps there, and only while they are still
# exactly as it wrote them. Any edit of the step list clears it — see `set_steps`.
TF_STEPS_SOURCE_KEY = "tf_steps_from_pipeline"

# Plain English step entry. What the user typed, and what came back from the Light Model,
# held across the reruns the dialog's own widgets cause. Every one starts `tf_ai_` so
# `close_dialog` can forget the whole conversation in one sweep — a stale parse reappearing
# under a freshly typed instruction would be worse than no parse at all.
TF_AI_INSTRUCTION_KEY = "tf_ai_instruction"
TF_AI_STEPS_KEY = "tf_ai_steps"
TF_AI_WARNINGS_KEY = "tf_ai_warnings"
TF_AI_CLARIFICATION_KEY = "tf_ai_clarification"

MAX_UPLOAD_SIZE_MB = 50

#: What a table's tab shows. The download always carries every row — the same rule phase 16
#: set for the report tables.
PREVIEW_ROWS = 500

#: What the Add-a-step dialog's Preview shows. Smaller because it sits inside a dialog and
#: is there to answer "is this the right step?", not to be read.
DIALOG_PREVIEW_ROWS = 50


# --------------------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------------------


def cached_file_bytes() -> dict[str, bytes]:
    """The uploaded files' bytes, by Streamlit's per-upload id.

    Held rather than re-read on every rerun because `UploadedFile.getvalue()` is a fresh
    read each time, and the replay wants the same bytes repeatedly.
    """
    return st.session_state.setdefault(TF_FILE_BYTES_KEY, {})


def confirmed_upload_ids() -> set[str]:
    """The uploads the user has pressed Load on.

    A file sitting in the uploader is not read until it has been confirmed — the phase-16
    gate, so that dropping five files in doesn't start five parses before the user has
    finished choosing sheets.
    """
    return st.session_state.setdefault(TF_CONFIRMED_KEY, set())


def confirm_uploads(file_ids: list[str]) -> None:
    """Marks these uploads as ready to read."""
    confirmed_upload_ids().update(file_ids)


def sheet_choice() -> dict[str, list[str]]:
    """Which sheets of each Excel upload the user wants, by upload id."""
    return st.session_state.setdefault(TF_SHEETS_KEY, {})


def table_names() -> dict[str, str]:
    """The name each uploaded table goes by, keyed by `file_id::sheet`.

    Stored separately from the tables themselves so that renaming survives a cache
    eviction, and so that the name the user chose is never recomputed from the filename
    behind their back.
    """
    return st.session_state.setdefault(TF_TABLE_NAMES_KEY, {})


def upload_key(file_id: str, sheet_name: str | None) -> str:
    """The stable identity of one uploaded table.

    Streamlit's per-upload id rather than the filename or the upload's position: position
    renumbers when a file is removed, and two uploads are often both called `data.csv`.
    """
    return f"{file_id}::{sheet_name or ''}"


@st.cache_data(show_spinner=False, max_entries=32, scope="session")
def _read_uploaded_table(
    file_id: str, file_name: str, sheet_name: str | None, _file_bytes: bytes
) -> pd.DataFrame:
    """Reads one uploaded table.

    `_file_bytes` is excluded from the cache key by its leading underscore; `file_id`
    already identifies the upload, and hashing megabytes on every rerun would cost more
    than the read.

    Raises:
        DataCleanerError: if the file or sheet can't be read.
    """
    return loaders.read_table(_file_bytes, file_name, sheet_name=sheet_name)


def sync_uploads(uploaded_files) -> tuple[dict[str, NamedFrame], list[str]]:
    """Builds the workspace of uploaded tables from what is currently in the uploader.

    Reconciles in **both** directions every rerun: a newly confirmed file gains a table,
    and a table whose file has been removed from the uploader is dropped along with its
    name. Registering only additions would keep exporting tables the user believes they
    removed.

    Returns:
        `(workspace_of_uploaded_tables, problems)`. A file that can't be read becomes a
        problem message rather than an exception, so one bad file doesn't take the page
        down with it.
    """
    confirmed = confirmed_upload_ids()
    sheets_wanted = sheet_choice()
    names = table_names()
    bytes_cache = cached_file_bytes()

    workspace: dict[str, NamedFrame] = {}
    problems: list[str] = []
    live_keys: set[str] = set()
    live_file_ids: set[str] = set()

    for uploaded_file in uploaded_files or []:
        if uploaded_file.file_id not in confirmed:
            continue

        live_file_ids.add(uploaded_file.file_id)
        if uploaded_file.file_id not in bytes_cache:
            bytes_cache[uploaded_file.file_id] = uploaded_file.getvalue()
        file_bytes = bytes_cache[uploaded_file.file_id]

        if loaders.is_csv(uploaded_file.name):
            sheets: list[str | None] = [None]
        else:
            sheets = list(sheets_wanted.get(uploaded_file.file_id) or [])

        for sheet_name in sheets:
            key = upload_key(uploaded_file.file_id, sheet_name)
            live_keys.add(key)
            try:
                frame = _read_uploaded_table(
                    uploaded_file.file_id, uploaded_file.name, sheet_name, file_bytes
                )
            except DataCleanerError as error:
                logger.exception("Could not read %s.", uploaded_file.name)
                problems.append(str(error))
                continue

            if key not in names:
                base = _base_name(uploaded_file.name, sheet_name)
                names[key] = suggest_frame_name(base, list(workspace))
            name = names[key]

            workspace[name] = NamedFrame(
                name=name,
                frame=frame,
                origin="upload",
                source_label=_source_label(uploaded_file.name, sheet_name),
                upload_file_id=uploaded_file.file_id,
            )

    for stale in set(names) - live_keys:
        del names[stale]
    for stale in set(bytes_cache) - live_file_ids:
        del bytes_cache[stale]
    for stale in set(confirmed) - live_file_ids:
        confirmed.discard(stale)

    return workspace, problems


def _base_name(file_name: str, sheet_name: str | None) -> str:
    """The name a freshly uploaded table starts with: the filename, or filename + sheet."""
    stem = (file_name or "").rsplit(".", 1)[0].strip()
    return f"{stem} {sheet_name}".strip() if sheet_name else stem


def _source_label(file_name: str, sheet_name: str | None) -> str:
    return f"{file_name} - {sheet_name}" if sheet_name else file_name


def rename_uploaded_table(old_name: str, new_name: str) -> None:
    """Renames one uploaded table, keeping the stored names in step.

    Raises:
        DuplicateFrameNameError: if the new name is taken.
    """
    names = table_names()
    cleaned = normalise_frame_name(new_name)
    taken = [name for name in names.values() if name_key(name) != name_key(old_name)]
    if any(name_key(name) == name_key(cleaned) for name in taken):
        raise DuplicateFrameNameError(
            f"A table named '{cleaned}' already exists - choose another name."
        )

    for key, name in names.items():
        if name_key(name) == name_key(old_name):
            names[key] = cleaned
            break

    # A step naming the old table would otherwise break on the next replay.
    _rename_in_steps(old_name, cleaned)


def _rename_in_steps(old_name: str, new_name: str) -> None:
    """Points every step that read or wrote the old name at the new one."""
    updated: list[TransformStep] = []
    for step in get_steps():
        inputs = {}
        for role, value in step.get("inputs", {}).items():
            if isinstance(value, list):
                inputs[role] = [
                    new_name if name_key(str(name)) == name_key(old_name) else name for name in value
                ]
            else:
                inputs[role] = (
                    new_name if name_key(str(value)) == name_key(old_name) else value
                )
        output = dict(step.get("output", {}))
        if name_key(str(output.get("name", ""))) == name_key(old_name):
            output["name"] = new_name
        updated.append({**step, "inputs": inputs, "output": output})
    set_steps(updated)


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------


def get_steps() -> list[TransformStep]:
    """The pipeline as it stands."""
    return st.session_state.setdefault(TF_STEPS_KEY, [])


def set_steps(steps: list[TransformStep]) -> None:
    """Replaces the step list, and forgets that a pipeline wrote it.

    Every route that changes a step — add, edit, undo, a table rename — comes through
    here, so clearing the marker in one place is what keeps "these are still the saved
    pipeline's own steps" honest. `apply_pipeline` re-stamps it immediately afterwards.
    """
    st.session_state[TF_STEPS_KEY] = steps
    st.session_state.pop(TF_STEPS_SOURCE_KEY, None)


def steps_came_from_pipeline() -> int | None:
    """The id of the pipeline whose steps are on the page untouched, else None."""
    return st.session_state.get(TF_STEPS_SOURCE_KEY)


def discard_pipeline_steps() -> None:
    """Throws away steps a pipeline installed and nobody has since changed.

    Leaving them behind was a real fault: after running last month's pipeline, picking
    "New pipeline" and uploading a completely different set of files replayed the old
    steps against them, and the page opened on `There's no table called 'Stock with
    difference August 20'`. Only ever called when `steps_came_from_pipeline` says the
    steps are the pipeline's own, so hand-built work is never discarded.
    """
    st.session_state[TF_STEPS_KEY] = []
    st.session_state.pop(TF_STEPS_SOURCE_KEY, None)
    st.session_state.pop(TF_EXPORT_KEY, None)
    st.session_state.pop(TF_EXPORT_SEEDED_KEY, None)


def append_step(step: TransformStep) -> None:
    set_steps(pipeline_model.append_step(get_steps(), step))


def replace_last_step(step: TransformStep) -> None:
    set_steps(pipeline_model.replace_last_step(get_steps(), step))


def remove_last_step() -> None:
    set_steps(pipeline_model.remove_last_step(get_steps()))


def last_step() -> TransformStep | None:
    steps = get_steps()
    return steps[-1] if steps else None


# --------------------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------------------


def run_pipeline(
    upload_workspace: dict[str, NamedFrame], steps: list[TransformStep] | None = None
) -> tuple[dict[str, NamedFrame], list[StepOutcome]]:
    """Runs the whole pipeline over the uploaded tables.

    Not cached with `st.cache_data`: a DataFrame-valued workspace is not hashable as an
    argument, and the frames it returns would have to be copied in and out on every hit. The
    per-table read underneath (`_read_uploaded_table`) is cached, which is where the real
    cost is — the steps themselves are vectorised pandas over data already in memory.

    Never raises for a data problem; a failed step comes back in the report with
    `status="failed"`, and the run stops there.
    """
    return pipeline_model.apply_steps_with_report(
        upload_workspace, get_steps() if steps is None else steps
    )


def preview_step(
    workspace: dict[str, NamedFrame], step: TransformStep
) -> tuple[pd.DataFrame | None, list[str], str | None]:
    """Runs one step against the current workspace, for the dialog's Preview button.

    Returns:
        `(frame, warnings, error)` — exactly one of `frame` and `error` is set. The error is
        returned rather than raised so the dialog can show it and stay open with the user's
        half-filled form intact.
    """
    try:
        after, report = pipeline_model.apply_steps_with_report(workspace, [step])
    except TransformError as error:
        return None, [], str(error)

    if not report:
        return None, [], "This step didn't run."
    outcome = report[0]
    if outcome.status == "failed":
        return None, [], outcome.message

    warnings_out = [outcome.message] if outcome.message else []
    return after[outcome.output_name].frame, warnings_out, None


# --------------------------------------------------------------------------------------
# Dialog, flash and start-over
# --------------------------------------------------------------------------------------


def open_dialog(mode: str) -> None:
    """Marks the add/edit dialog as showing.

    Which dialog is open lives in session state rather than in a button's return value.
    `if st.button(...): show_dialog()` looks equivalent but breaks the moment the dialog
    holds widgets: interacting with one triggers a rerun, on which the button reads False,
    so the dialog closes and the half-filled form is lost. A flag survives those reruns.
    """
    st.session_state[TF_DIALOG_KEY] = mode


def close_dialog() -> None:
    """Dismisses the dialog and forgets the form — or the parse — it held."""
    st.session_state.pop(TF_DIALOG_KEY, None)
    for key in [
        key
        for key in st.session_state
        if str(key).startswith("tf_form_") or str(key).startswith("tf_ai_")
    ]:
        st.session_state.pop(key, None)


def pending_dialog() -> str | None:
    """`add`, `edit`, `ai_add`, `delete`, or None when no dialog is open."""
    return st.session_state.get(TF_DIALOG_KEY)


# --------------------------------------------------------------------------------------
# Plain English parse results
# --------------------------------------------------------------------------------------


def set_ai_parse(steps: list[TransformStep], warnings: list[str], clarification: str | None) -> None:
    """Remembers what the Light Model made of the user's sentence."""
    st.session_state[TF_AI_STEPS_KEY] = steps
    st.session_state[TF_AI_WARNINGS_KEY] = warnings
    st.session_state[TF_AI_CLARIFICATION_KEY] = clarification


def ai_parse() -> tuple[list[TransformStep], list[str], str | None]:
    """The last parse, or three empties before anything has been read."""
    return (
        st.session_state.get(TF_AI_STEPS_KEY, []),
        st.session_state.get(TF_AI_WARNINGS_KEY, []),
        st.session_state.get(TF_AI_CLARIFICATION_KEY),
    )


def clear_ai_parse() -> None:
    """Throws the last parse away, leaving the typed instruction alone.

    Pressed Read again, or changed the sentence: the old steps must not linger underneath
    a new one, which is the only way a user could add steps they never saw described.
    """
    for key in (TF_AI_STEPS_KEY, TF_AI_WARNINGS_KEY, TF_AI_CLARIFICATION_KEY):
        st.session_state.pop(key, None)


def flash(message: str) -> None:
    """Queues a message to show after the next rerun."""
    st.session_state[TF_FLASH_KEY] = message


def consume_flash() -> str | None:
    """Takes the queued message, if there is one."""
    return st.session_state.pop(TF_FLASH_KEY, None)


def queue_start_over() -> None:
    """Marks the whole page to be cleared on the next run.

    Queued rather than done here because clearing has to happen *before*
    `st.file_uploader` is drawn — its key can't be deleted once the widget exists in the
    same run.
    """
    st.session_state[TF_START_OVER_KEY] = True


def consume_start_over() -> bool:
    """Clears everything if a start-over was queued. Called at the very top of the page."""
    if not st.session_state.pop(TF_START_OVER_KEY, False):
        return False

    for key in (
        TF_UPLOADER_KEY,
        TF_CONFIRMED_KEY,
        TF_FILE_BYTES_KEY,
        TF_TABLE_NAMES_KEY,
        TF_SHEETS_KEY,
        TF_STEPS_KEY,
        TF_DIALOG_KEY,
        TF_EXPORT_KEY,
        TF_EXPORT_SEEDED_KEY,
        TF_TABS_KEY,
        # The selection goes with the rest. Start over means "begin again", and leaving a
        # pipeline selected over an empty page would show its expected-files panel
        # reporting every file missing, which reads as a fault rather than a fresh start.
        TF_PIPELINE_ID_KEY,
        TF_PIPELINE_NAME_KEY,
        TF_PIPELINE_OBJECT_KEY,
        TF_PIPELINE_DIALOG_KEY,
        TF_STEPS_SOURCE_KEY,
    ):
        st.session_state.pop(key, None)
    # Queued, not written: this runs inside `_render_upload`, which is *below* the picker,
    # and Streamlit forbids writing a widget's own key once that widget exists this run.
    # `consume_pipeline_selection` applies it at the top of the next run.
    queue_pipeline_selection(None)
    logger.info("Transform Data workspace cleared.")
    return True


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------


def seed_export_selection(workspace: dict[str, NamedFrame]) -> None:
    """Points the download picker at the newest step's table, once per new step.

    A keyed `st.multiselect` ignores its `default` after the first run it is drawn on, so
    passing one would mean the picker stayed on whatever was chosen when the page had no
    steps — and the table the user just built would never be pre-selected. Seeding the
    widget's own state instead is what makes the default actually follow the pipeline.

    Seeded only when the last step's output *changes*, so a user who deliberately picks
    something else keeps their choice until they add another step.
    """
    names = frame_names(workspace)
    step = last_step()
    target = step.get("output", {}).get("name", "") if step else ""
    resolved = next((held for held in names if name_key(held) == name_key(target)), None)

    if st.session_state.get(TF_EXPORT_SEEDED_KEY) != resolved:
        st.session_state[TF_EXPORT_SEEDED_KEY] = resolved
        if resolved is not None:
            st.session_state[TF_EXPORT_KEY] = [resolved]
            return

    # Whatever is selected, drop anything that is no longer a table, or the widget refuses
    # its own stored value.
    held = [name for name in st.session_state.get(TF_EXPORT_KEY, []) if name in names]
    st.session_state[TF_EXPORT_KEY] = held or names


def build_download(workspace: dict[str, NamedFrame], selected_names: list[str]) -> bytes:
    """The workbook bytes for the chosen tables.

    Raises:
        TransformExportError: if nothing is selected or Excel's limits are exceeded.
    """
    return build_transform_workbook(workspace, selected_names, get_steps())


# --------------------------------------------------------------------------------------
# Saved pipelines
# --------------------------------------------------------------------------------------


def capture_pipeline(
    upload_workspace: dict[str, NamedFrame],
    name: str,
    *,
    description: str = "",
    pipeline_id: int | None = None,
) -> SavedPipeline:
    """Reads the whole working set out as a saved pipeline.

    The uploaded tables with their columns, the step list as it stands, and whichever
    tables the download picker is pointing at — which is what "the whole set" means here:
    next month, one name brings back the files to expect, what to do to them, and which
    answer to hand back.
    """
    return template_model.capture(
        name,
        description=description,
        upload_workspace=upload_workspace,
        steps=get_steps(),
        outputs=list(st.session_state.get(TF_EXPORT_KEY) or []),
        pipeline_id=pipeline_id,
    )


def match_pipeline(
    pipeline: SavedPipeline, upload_workspace: dict[str, NamedFrame]
) -> PipelineMatch:
    """Measures the current upload against a pipeline. Never raises — see `transform.matching`.

    Only `origin="upload"` tables are offered: a step table has no file behind it, and the
    pipeline rebuilds it anyway.
    """
    uploaded = {
        held.name: (
            template_model.file_name_of(held),
            template_model.sheet_name_of(held),
            [str(column) for column in held.frame.columns],
        )
        for held in upload_workspace.values()
        if held.origin == "upload"
    }
    return matching.check_upload(pipeline, uploaded)


def apply_pipeline(pipeline: SavedPipeline, match: PipelineMatch) -> tuple[int, list[str]]:
    """Renames the matched uploads to the names the steps expect, then installs the steps.

    Renaming is the whole trick. A saved step names its table (`sales`), and this month's
    file may have arrived as `sales_march`; pointing the steps at the new name instead
    would mean rewriting every step on load and re-deriving which of them meant the same
    table. Renaming the table once is the same operation the user could do by hand, and it
    leaves the saved steps exactly as they were written.

    Done in **two passes** through temporary names. A one-pass rename breaks on the
    ordinary case of two tables swapping names — the first rename would collide with the
    table still holding its target.

    Returns `(tables matched, problems)`. A problem is a name the pipeline wanted that
    something outside it is holding; the steps are installed regardless, so the user sees
    the pipeline with one step failing on a named table rather than a page that silently
    did nothing.
    """
    problems: list[str] = []
    pending = [
        (found, expected)
        for expected, found in match.matched.items()
        if name_key(found) != name_key(expected)
    ]

    staged: list[tuple[str, str, str]] = []
    for position, (found, expected) in enumerate(pending):
        temporary = suggest_frame_name(f"tfmove{position}", list(table_names().values()))
        try:
            rename_uploaded_table(found, temporary)
        except TransformError as error:
            logger.warning(
                "Could not park '%s' before renaming it to '%s': %s", found, expected, error
            )
            problems.append(f"'{found}' couldn't be renamed to '{expected}' ({error}).")
            continue
        staged.append((temporary, expected, found))

    for temporary, expected, original in staged:
        try:
            rename_uploaded_table(temporary, expected)
        except TransformError as error:
            logger.warning("Could not rename a table to '%s': %s", expected, error)
            problems.append(
                f"This pipeline needs a table called '{expected}', but that name is already "
                f"taken by a table it doesn't use ({error})."
            )
            # Put it back under the name the user knows it by. Without this the table is
            # left called `tfmove0` — a name from this function's internals that means
            # nothing to anyone, on a table the user never asked to rename.
            try:
                rename_uploaded_table(temporary, original)
            except TransformError:
                logger.exception("Could not restore '%s' to '%s'.", temporary, original)

    set_steps([dict(step) for step in pipeline.steps])
    # Stamped *after* `set_steps`, which clears it — this is the one place the steps are
    # known to be the pipeline's own rather than something the user built.
    st.session_state[TF_STEPS_SOURCE_KEY] = pipeline.pipeline_id
    _seed_pipeline_outputs(pipeline)
    logger.info(
        "Applied transform pipeline '%s': %d table(s) matched, %d step(s) installed.",
        pipeline.display_name(),
        len(match.matched),
        len(pipeline.steps),
    )
    return len(match.matched), problems


def _seed_pipeline_outputs(pipeline: SavedPipeline) -> None:
    """Points the download picker at the tables the pipeline was saved with.

    `TF_EXPORT_SEEDED_KEY` is written alongside, to the name `seed_export_selection` would
    otherwise seed from — otherwise that function would see a new last step on the very
    next run and overwrite the saved choice with it.
    """
    if not pipeline.outputs:
        return
    st.session_state[TF_EXPORT_KEY] = list(pipeline.outputs)
    last = pipeline.steps[-1] if pipeline.steps else None
    st.session_state[TF_EXPORT_SEEDED_KEY] = (
        last.get("output", {}).get("name") if last else None
    )


def active_pipeline() -> tuple[int | None, str]:
    """The selected pipeline's id and name, or `(None, "")` when none is selected."""
    return (
        st.session_state.get(TF_PIPELINE_ID_KEY),
        st.session_state.get(TF_PIPELINE_NAME_KEY, ""),
    )


def active_pipeline_object() -> SavedPipeline | None:
    """The selected pipeline itself, held since it was chosen.

    Kept in session state rather than re-read from SQLite each run, because the match
    report is redrawn on **every** rerun — the upload can change under a selected pipeline
    at any time — and a database round trip per keystroke to say "3 files matched" would be
    a cost with nothing to show for it.
    """
    return st.session_state.get(TF_PIPELINE_OBJECT_KEY)


def set_active_pipeline(pipeline: SavedPipeline) -> None:
    """Records which saved pipeline the working set is being transformed under."""
    st.session_state[TF_PIPELINE_ID_KEY] = pipeline.pipeline_id
    st.session_state[TF_PIPELINE_NAME_KEY] = pipeline.display_name()
    st.session_state[TF_PIPELINE_OBJECT_KEY] = pipeline


def clear_active_pipeline() -> None:
    """Goes back to "New pipeline" without touching a single step.

    Deselecting is not undoing: the steps a pipeline put on the page stay exactly where
    they are. Undoing them is what Start over is for, and quietly reverting a page's worth
    of work because a dropdown changed would be the worst kind of surprise.
    """
    st.session_state.pop(TF_PIPELINE_ID_KEY, None)
    st.session_state.pop(TF_PIPELINE_NAME_KEY, None)
    st.session_state.pop(TF_PIPELINE_OBJECT_KEY, None)


def queue_pipeline_selection(selection: object) -> None:
    """Asks for the picker to be showing `selection` on the next run.

    Deferred, not written: Streamlit forbids writing a widget's own session_state key once
    that widget exists this run, and both callers — saving, and deleting — run in a dialog
    below the bar, well past that point. Leaving the key alone is not an option either:
    after a delete it would still hold an id that is no longer one of the picker's options.
    """
    st.session_state[TF_PIPELINE_PENDING_KEY] = selection


def consume_pipeline_selection() -> None:
    """Applies a queued picker selection. Call before the picker is created."""
    if TF_PIPELINE_PENDING_KEY not in st.session_state:
        return
    st.session_state[TF_PIPELINE_PICK_KEY] = st.session_state.pop(TF_PIPELINE_PENDING_KEY)


def open_pipeline_dialog(name: str, payload: dict | None = None) -> None:
    """Marks which pipeline dialog should be showing, on the same grounds as `open_dialog`."""
    st.session_state[TF_PIPELINE_DIALOG_KEY] = {"name": name, "payload": payload or {}}


def close_pipeline_dialog() -> None:
    """Dismisses whichever pipeline dialog is open."""
    st.session_state.pop(TF_PIPELINE_DIALOG_KEY, None)


def pending_pipeline_dialog() -> tuple[str, dict] | None:
    """The (name, payload) of the open pipeline dialog, or None if none is open."""
    pending = st.session_state.get(TF_PIPELINE_DIALOG_KEY)
    if not pending:
        return None
    return pending["name"], pending.get("payload") or {}
