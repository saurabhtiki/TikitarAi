"""Session state and cached derivations for the Data Cleaner page.

This is the only module in `cleaner/` that imports Streamlit, mirroring the convention
already in the codebase — `auth/` has exactly one Streamlit-coupled module
(`service.py`) and `llm/` has none. Everything else in the package stays pure and
testable without `AppTest`.

No DataFrames are kept in session state. Raw and cleaned frames live only in
`st.cache_data` and are always re-derivable from the upload's bytes plus the recipe, so
a cache eviction costs a recompute rather than losing the user's work.
"""

import logging
from dataclasses import dataclass, field
from uuid import uuid4

import pandas as pd
import streamlit as st

from cleaner import loaders, naming, pipeline, profiling, template as template_model
from cleaner.exceptions import DataCleanerError
from cleaner.matching import TemplateMatch, check_upload
from cleaner.pipeline import CLEANING_RECIPE_VERSION, Step, StepOutcome
from cleaner.template import CleaningTemplate, TemplateSummary, TemplateTable

logger = logging.getLogger(__name__)

DC_TABLES_KEY = "dc_tables"
DC_UPLOADER_KEY = "dc_uploader"
DC_TABS_KEY = "dc_tabs"
DC_DIALOG_KEY = "dc_open_dialog"
DC_START_OVER_KEY = "dc_start_over_pending"
DC_FILE_BYTES_KEY = "dc_file_bytes"

# The template currently selected in the bar above the uploader. The id is what saving
# updates; the name is what the bar shows and what the picker's selection is restored from.
DC_TEMPLATE_ID_KEY = "dc_template_id"
DC_TEMPLATE_NAME_KEY = "dc_template_name"
DC_TEMPLATE_OBJECT_KEY = "dc_template_object"
# The picker widget's own key, and the flag saying which template the user just chose but
# which hasn't been acted on yet. The two are separate because the bar draws *above*
# `st.file_uploader`, and acting on a selection there would end the run before that widget
# exists — which drops every uploaded file. See `app_pages/data_cleaner.py`.
DC_TEMPLATE_PICK_KEY = "dc_template_pick"
DC_TEMPLATE_PENDING_KEY = "dc_template_pending"
DC_TEMPLATE_DIALOG_KEY = "dc_template_dialog"
DC_FLASH_KEY = "dc_flash"

MAX_UPLOAD_SIZE_MB = 50
PREVIEW_ROWS = 500
LARGE_TABLE_ROWS = 250_000


@dataclass
class TableState:
    """One cleanable table: where it came from, what to call it, and its recipe.

    A summary (a group-and-total, pivot or unpivot saved from another table) is the same
    type rather than a type of its own, which is what lets it ride the download, the
    sheet-name dedupe and the Chat with Data handoff with no code of its own in any of
    them. It sets `derived_from` to its parent's `table_id` and holds its reshape in
    `reshape`; its own `steps` stay empty, because its recipe is resolved live from the
    parent by `effective_steps`.
    """

    table_id: str
    file_id: str
    file_name: str
    sheet_name: str | None
    source_label: str
    output_sheet_name: str
    steps: list[Step] = field(default_factory=list)
    recipe_version: int = CLEANING_RECIPE_VERSION
    derived_from: str | None = None
    reshape: Step | None = None


def make_table_id(file_id: str, sheet_name: str | None) -> str:
    """Builds the stable identity for one table.

    `file_id` is Streamlit's per-upload UUID, which survives reruns for as long as the
    file stays in the uploader. Upload *position* would be far worse: adding a fourth
    file can renumber the others and silently reattach one table's recipe to a
    different table. Filename would collide whenever two uploads are both named
    `data.csv`.

    The trade-off, stated plainly: removing a file and re-adding it produces a new
    `table_id` and an empty recipe. That is intended — re-uploading is the user's
    signal that the data changed.
    """
    return f"{file_id}::{sheet_name or ''}"


def _source_label(file_name: str, sheet_name: str | None) -> str:
    return f"{file_name} — {sheet_name}" if sheet_name else file_name


@st.cache_data(show_spinner=False, max_entries=32, scope="session")
def _cached_raw_table(file_id: str, file_name: str, sheet_name: str | None, _file_bytes: bytes) -> pd.DataFrame:
    """Reads one uploaded table. `_file_bytes` is excluded from the cache key by its
    leading underscore — `file_id` already identifies those bytes uniquely within the
    session, so hashing the whole payload on every rerun would be pure waste."""
    return loaders.read_table(_file_bytes, file_name, sheet_name)


@st.cache_data(show_spinner=False, max_entries=64, scope="session")
def _cached_cleaned_table(
    table_id: str, steps: list[Step], _raw: pd.DataFrame
) -> tuple[pd.DataFrame, list[StepOutcome]]:
    """Applies a recipe. Keyed on the recipe, which Streamlit hashes order-sensitively,
    so adding or undoing a step invalidates exactly this table and nothing else."""
    return pipeline.apply_steps_with_report(_raw, steps)


def raw_table(table: TableState, file_bytes: bytes) -> pd.DataFrame:
    """Returns the table exactly as uploaded, every cell still text.

    Raises:
        DataCleanerError: if the file can no longer be read.
    """
    return _cached_raw_table(table.file_id, table.file_name, table.sheet_name, file_bytes)


def effective_steps(table: TableState) -> list[Step]:
    """The recipe that actually runs for this table.

    For an uploaded table that is simply its own steps. For a summary it is the parent's
    *current* steps followed by the reshape — resolved on every call rather than frozen at
    save time, which is what keeps a summary honest when its source is cleaned further.
    A summary whose parent has gone returns just the reshape; in practice `sync_tables`
    has already dropped it by then.
    """
    if table.derived_from is None:
        return table.steps
    parent = get_table(table.derived_from)
    inherited = list(parent.steps) if parent is not None else []
    return inherited + ([table.reshape] if table.reshape is not None else [])


def cleaned_table(table: TableState, file_bytes: bytes) -> tuple[pd.DataFrame, list[StepOutcome]]:
    """Returns the cleaned table and the per-step report.

    Raises:
        DataCleanerError: if the file can no longer be read, or a recorded step names an
            unknown action.
    """
    # The parent's steps are part of the key for a summary, so cleaning the parent
    # invalidates the summary's cache entry on its own — no explicit invalidation needed.
    return _cached_cleaned_table(table.table_id, effective_steps(table), raw_table(table, file_bytes))


def get_tables() -> dict[str, TableState]:
    """Returns the current working set, keyed by table_id."""
    return st.session_state.setdefault(DC_TABLES_KEY, {})


def cached_file_bytes() -> dict[str, bytes]:
    """Every uploaded file's bytes this session has read, keyed by `file_id`.

    `st.file_uploader`'s own widget state is ephemeral and only reliable on the page
    that renders it — reading `st.session_state[DC_UPLOADER_KEY]` from another page
    (as the Chat with Data handoff does) can silently come back empty after
    navigation. A plain session_state dict, written once per file here, does not have
    that restriction and is what every cross-page reader should use instead.
    """
    return st.session_state.setdefault(DC_FILE_BYTES_KEY, {})


def get_table(table_id: str) -> TableState | None:
    """Returns one table's state, or None if it is no longer loaded."""
    return get_tables().get(table_id)


def set_steps(table_id: str, steps: list[Step]) -> None:
    """Replaces a table's recipe."""
    table = get_table(table_id)
    if table is None:
        logger.warning("Tried to set steps on table %s, which is no longer loaded.", table_id)
        return
    table.steps = steps


def set_reshape(table_id: str, step: Step) -> None:
    """Replaces a summary's reshape, for the Edit path."""
    table = get_table(table_id)
    if table is None:
        logger.warning("Tried to set the reshape on table %s, which is no longer loaded.", table_id)
        return
    if table.derived_from is None:
        logger.warning("Refused to set a reshape on table %s, which isn't a summary.", table_id)
        return
    table.reshape = step


def add_summary_table(parent_table_id: str, step: Step, name: str) -> TableState | None:
    """Saves a reshape of `parent_table_id` as a new derived table.

    Returns the new table, or None if the parent is no longer loaded.
    """
    parent = get_table(parent_table_id)
    if parent is None:
        logger.warning("Tried to summarise table %s, which is no longer loaded.", parent_table_id)
        return None

    tables = get_tables()
    taken = {table.output_sheet_name.lower() for table in tables.values()}
    output_sheet_name = naming.sanitize_sheet_name(name)
    if output_sheet_name.lower() in taken:
        output_sheet_name = naming.deduplicate_sheet_names([*sorted(taken), output_sheet_name])[-1]

    summary = TableState(
        # Random rather than derived from the name, so renaming a summary can't collide
        # with a sibling and can't orphan the recipe that is already keyed on this id.
        table_id=f"{parent.table_id}::summary::{uuid4().hex[:8]}",
        file_id=parent.file_id,
        file_name=parent.file_name,
        sheet_name=parent.sheet_name,
        source_label=f"{parent.source_label} — {name}",
        output_sheet_name=output_sheet_name,
        derived_from=parent.table_id,
        reshape=step,
    )
    tables[summary.table_id] = summary
    logger.info("Saved summary %s of table %s.", summary.table_id, parent.table_id)
    return summary


def remove_table(table_id: str) -> None:
    """Discards one table and every summary derived from it."""
    tables = get_tables()
    if tables.pop(table_id, None) is None:
        logger.warning("Tried to remove table %s, which is no longer loaded.", table_id)
        return
    for summary_id in [key for key, table in tables.items() if table.derived_from == table_id]:
        del tables[summary_id]


def set_output_sheet_name(table_id: str, name: str) -> None:
    """Updates the worksheet name a table will be exported under, sanitized on the way in."""
    table = get_table(table_id)
    if table is None:
        logger.warning("Tried to rename table %s, which is no longer loaded.", table_id)
        return
    table.output_sheet_name = naming.sanitize_sheet_name(name)


def clear_tables() -> None:
    """Discards every loaded table and its recipe."""
    st.session_state[DC_TABLES_KEY] = {}


def open_dialog(table_id: str, action: str) -> None:
    """Marks which action dialog should be showing.

    Which dialog is open lives in session state rather than in a button's return value.
    `if st.button(...): _open_dialog()` looks equivalent but breaks as soon as the dialog
    contains widgets: interacting with one triggers a rerun, on which the button reads
    False, so the dialog closes and its inputs vanish. A flag survives those reruns.

    One key holds the whole thing because Streamlit only ever shows one dialog at a time,
    so a single value can't get out of sync with itself.
    """
    st.session_state[DC_DIALOG_KEY] = {"table_id": table_id, "action": action}


def close_dialog() -> None:
    """Dismisses whichever action dialog is open."""
    st.session_state.pop(DC_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, str] | None:
    """Returns the (table_id, action) of the open dialog, or None if none is open."""
    pending = st.session_state.get(DC_DIALOG_KEY)
    if not pending:
        return None
    return pending["table_id"], pending["action"]


def queue_start_over() -> None:
    """Marks the whole session to be cleared on the next run.

    Deferred rather than immediate for two reasons. Clearing only the tables would be
    undone within the same run — `sync_tables` re-registers them straight from the still
    populated uploader — so the uploader itself has to be cleared too. And Streamlit
    forbids writing a widget's own session_state key after that widget has been created,
    which the uploader already has by the time this button is reached. Same pattern as
    the table-selection reset in `app_pages/settings.py`.
    """
    st.session_state[DC_START_OVER_KEY] = True


def consume_start_over() -> bool:
    """Applies a queued start-over, if one is pending. Call before the uploader renders.

    The selected cleaning template is deliberately **kept**, as `chat_types.session` keeps
    its selection across a start over: uploading next month's files against the same saved
    steps is the normal reason to clear the working set, not a reason to also forget which
    template those files belong to.
    """
    if not st.session_state.pop(DC_START_OVER_KEY, False):
        return False
    clear_tables()
    st.session_state.pop(DC_UPLOADER_KEY, None)
    st.session_state.pop(DC_FILE_BYTES_KEY, None)
    return True


def _seed_steps(frame: pd.DataFrame) -> list[Step]:
    """Seeds a new table's recipe with its detected column types.

    Typing is recorded as a real step rather than applied invisibly, so it replays
    correctly in Stage 8 and "reset to raw" genuinely returns the file's literal
    contents. Only columns detected as something other than text are seeded — every
    column arrives as text, so a text entry would be a no-op that clutters the log.
    """
    detected = profiling.detect_column_types(frame)
    by_column = {
        column: {"target_type": column_type}
        for column, column_type in detected.items()
        if column_type != profiling.TEXT
    }
    if not by_column:
        return []
    return [pipeline.make_step("set_column_types", {"by_column": by_column})]


def _new_table_state(uploaded_file, sheet_name: str | None, taken_sheet_names: set[str]) -> TableState:
    file_bytes = uploaded_file.getvalue()
    cached_file_bytes()[uploaded_file.file_id] = file_bytes
    table_id = make_table_id(uploaded_file.file_id, sheet_name)

    base_name = sheet_name or uploaded_file.name.rsplit(".", 1)[0]
    output_sheet_name = naming.sanitize_sheet_name(base_name)
    if output_sheet_name.lower() in taken_sheet_names:
        output_sheet_name = naming.deduplicate_sheet_names([*sorted(taken_sheet_names), output_sheet_name])[-1]

    state = TableState(
        table_id=table_id,
        file_id=uploaded_file.file_id,
        file_name=uploaded_file.name,
        sheet_name=sheet_name,
        source_label=_source_label(uploaded_file.name, sheet_name),
        output_sheet_name=output_sheet_name,
        steps=[],
    )
    state.steps = _seed_steps(_cached_raw_table(state.file_id, state.file_name, state.sheet_name, file_bytes))
    return state


def sync_tables(uploaded_files, sheet_selection: dict[str, list[str]]) -> list[TableState]:
    """Reconciles the working set with what is currently in the uploader.

    Reconciliation runs in **both** directions every rerun: new file/sheet combinations
    gain a table, and any table whose file or sheet is no longer selected is dropped
    along with its recipe. Only registering additions would silently keep exporting
    tables the user believes they removed.

    Summaries have no file of their own, so they can't be rebuilt from the uploader and
    are carried over explicitly — each one placed straight after its parent, so the tab
    strip reads source, its summaries, next source. A summary whose parent is gone is
    simply not carried over, which is the cascade delete the page relies on.

    Raises:
        DataCleanerError: if a newly added file can't be read.
    """
    existing = get_tables()
    reconciled: dict[str, TableState] = {}
    taken_sheet_names: set[str] = set()
    summaries_by_parent: dict[str, list[TableState]] = {}
    for table in existing.values():
        if table.derived_from is not None:
            summaries_by_parent.setdefault(table.derived_from, []).append(table)

    for uploaded_file in uploaded_files or []:
        if loaders.is_csv(uploaded_file.name):
            sheets: list[str | None] = [None]
        else:
            sheets = list(sheet_selection.get(uploaded_file.file_id) or [])

        for sheet_name in sheets:
            table_id = make_table_id(uploaded_file.file_id, sheet_name)
            table = existing.get(table_id)
            if table is None:
                table = _new_table_state(uploaded_file, sheet_name, taken_sheet_names)
            reconciled[table_id] = table
            taken_sheet_names.add(table.output_sheet_name.lower())

            for summary in summaries_by_parent.get(table_id, []):
                reconciled[summary.table_id] = summary
                taken_sheet_names.add(summary.output_sheet_name.lower())

    dropped = set(existing) - set(reconciled)
    if dropped:
        logger.info("Dropped %d table(s) no longer selected in the uploader.", len(dropped))

    st.session_state[DC_TABLES_KEY] = reconciled

    still_referenced = {table.file_id for table in reconciled.values()}
    bytes_cache = cached_file_bytes()
    for file_id in set(bytes_cache) - still_referenced:
        del bytes_cache[file_id]

    return list(reconciled.values())


def tab_labels(tables: list[TableState]) -> list[str]:
    """Unique tab labels built from each table's immutable source label.

    Never the editable output sheet name: that is widget-backed, so every keystroke
    would change the tab set's identity and reset the user's tab selection mid-edit.
    """
    return naming.deduplicate_labels([table.source_label for table in tables])


def export_sheet_names(tables: list[TableState]) -> list[str]:
    """The worksheet names the workbook will actually use, sanitized and de-duplicated."""
    return naming.sanitize_sheet_names([table.output_sheet_name for table in tables])


def build_download(tables: list[TableState], file_bytes_by_id: dict[str, bytes]) -> bytes:
    """Builds the multi-sheet workbook for every loaded table, with its cleaning log.

    Raises:
        DataCleanerError: if any table can't be read, cleaned, or written.
    """
    from cleaner import export

    sheet_names = export_sheet_names(tables)
    payload: list[tuple[str, pd.DataFrame]] = []
    log: dict[str, list[str]] = {}

    for sheet_name, table in zip(sheet_names, tables):
        file_bytes = file_bytes_by_id.get(table.file_id)
        if file_bytes is None:
            raise DataCleanerError(f"'{table.source_label}' is no longer available. Please re-upload it.")
        frame, _ = cleaned_table(table, file_bytes)
        payload.append((sheet_name, frame))
        log[sheet_name] = pipeline.describe_steps(effective_steps(table))

    return export.build_workbook(payload, log)


# --------------------------------------------------------------------------------------
# Cleaning templates
#
# The working set read out as a saved recipe, and a saved recipe read back onto it. The
# formats and the matching live in `cleaner/template.py` and `cleaner/matching.py`, which
# know nothing about Streamlit; this is the only part that touches session state.
# --------------------------------------------------------------------------------------


def source_tables() -> list[TableState]:
    """The uploaded tables, in tab order — every table that has a file behind it.

    Derived tables are left out because they have no file of their own: a template stores
    them as reshapes to rebuild, not as expected uploads.
    """
    return [table for table in get_tables().values() if table.derived_from is None]


def summaries_of(table_id: str) -> list[TableState]:
    """Every derived table saved off `table_id`, in the order they were added."""
    return [table for table in get_tables().values() if table.derived_from == table_id]


def _cleaned_columns(table: TableState) -> list[str]:
    """The column names this table has once its recipe has run, for the schema dialog.

    A best effort, not a guarantee: this is shown to a user deciding whether a template is
    the one they want, so a file that can no longer be read costs an empty list rather than
    a refused save.
    """
    file_bytes = cached_file_bytes().get(table.file_id)
    if file_bytes is None:
        return []
    try:
        frame, _ = cleaned_table(table, file_bytes)
    except DataCleanerError:
        logger.warning("Could not read '%s' while capturing a template.", table.source_label)
        return []
    return [str(column) for column in frame.columns]


def capture_template(
    name: str, *, description: str = "", template_id: int | None = None
) -> CleaningTemplate:
    """Reads the whole working set out as a saved template.

    One entry per uploaded file with its recipe, plus every Pivot / Group & total / Unpivot
    saved off one of them — which is what "whole set" means: "Receivables" brings back
    `billwise_due`, `customer_master`, `sales` and the summary tables together.
    """
    tables: list[TemplateTable] = []
    summaries: list[TemplateSummary] = []

    for table in source_tables():
        entry_name = template_model.source_key(table.file_name, table.sheet_name)
        tables.append(
            TemplateTable(
                name=entry_name,
                file_name=table.file_name,
                sheet_name=table.sheet_name,
                output_sheet_name=table.output_sheet_name,
                steps=[dict(step) for step in table.steps],
                columns=_cleaned_columns(table),
                recipe_version=table.recipe_version,
            )
        )
        for summary in summaries_of(table.table_id):
            if summary.reshape is None:
                continue
            summaries.append(
                TemplateSummary(
                    parent=entry_name,
                    name=summary.output_sheet_name,
                    reshape=dict(summary.reshape),
                )
            )

    return template_model.capture(
        name,
        description=description,
        tables=tables,
        summaries=summaries,
        template_id=template_id,
    )


def match_template(template: CleaningTemplate) -> TemplateMatch:
    """Measures the current upload against a template. Never raises — see `cleaner.matching`."""
    loaded = {
        table.table_id: (table.file_name, table.sheet_name) for table in source_tables()
    }
    return check_upload(template, loaded)


def apply_template(template: CleaningTemplate, match: TemplateMatch) -> tuple[int, int]:
    """Writes a template's recipes onto the matched tables and rebuilds its summaries.

    Returns `(tables cleaned, summary tables rebuilt)` so the caller can say what happened.

    A matched table's **existing summaries are discarded first**. Applying a template twice
    would otherwise leave two copies of every derived table, each one a `naming` de-dupe
    away from the last — and the template is the statement of what this working set should
    contain, not an addition to it.

    An unmatched file is left completely alone, steps and summaries both: `cleaner.matching`
    reports it as extra and this does nothing about it, which is the promise that module's
    docstring makes.

    Nothing is validated against the columns actually present. That check already happens
    where the user can see it — `pipeline.apply_steps_with_report` skips a step whose column
    has gone and the table's cleaning log says so.
    """
    cleaned = 0
    rebuilt = 0

    for table_name, table_id in match.matched.items():
        entry = template.table(table_name)
        if entry is None or get_table(table_id) is None:
            continue

        for stale in summaries_of(table_id):
            remove_table(stale.table_id)

        set_steps(table_id, [dict(step) for step in entry.steps])
        cleaned += 1

        for summary in template.summaries_of(table_name):
            if add_summary_table(table_id, dict(summary.reshape), summary.name) is not None:
                rebuilt += 1

    logger.info(
        "Applied cleaning template '%s': %d table(s) cleaned, %d summary table(s) rebuilt.",
        template.display_name(),
        cleaned,
        rebuilt,
    )
    return cleaned, rebuilt


# --------------------------------------------------------------------------------------
# Which template is selected, and the messages that outlive a rerun
# --------------------------------------------------------------------------------------


def active_template() -> tuple[int | None, str]:
    """The selected template's id and name, or `(None, "")` when none is selected."""
    return st.session_state.get(DC_TEMPLATE_ID_KEY), st.session_state.get(DC_TEMPLATE_NAME_KEY, "")


def active_template_object() -> CleaningTemplate | None:
    """The selected template itself, held since it was chosen.

    Kept in session state rather than re-read from SQLite each run, because the match
    report is redrawn on **every** rerun — the upload can change under a selected template
    at any time — and a database round trip per keystroke to say "3 files matched" would be
    a cost with nothing to show for it. It is a recipe, so nothing here can go stale except
    by the user editing the template on another screen, which reselects it anyway.
    """
    return st.session_state.get(DC_TEMPLATE_OBJECT_KEY)


def set_active_template(template: CleaningTemplate) -> None:
    """Records which saved template the working set is being cleaned under."""
    st.session_state[DC_TEMPLATE_ID_KEY] = template.template_id
    st.session_state[DC_TEMPLATE_NAME_KEY] = template.display_name()
    st.session_state[DC_TEMPLATE_OBJECT_KEY] = template


def clear_active_template() -> None:
    """Goes back to `— New template —` without touching a single cleaning step.

    Deselecting is not undoing: the steps a template put on the tables stay exactly where
    they are. Undoing them is what Start over is for, and quietly reverting a page's worth
    of cleaning because a dropdown changed would be the worst kind of surprise.
    """
    st.session_state.pop(DC_TEMPLATE_ID_KEY, None)
    st.session_state.pop(DC_TEMPLATE_NAME_KEY, None)
    st.session_state.pop(DC_TEMPLATE_OBJECT_KEY, None)


def queue_template_selection(selection: object) -> None:
    """Asks for the picker to be showing `selection` on the next run.

    Takes whatever the picker uses as an option value — a template id, or
    `saved_picker.NONE_OPTION` — rather than an id alone, because the two callers want
    opposite ends of that list and neither one is `None`.

    Deferred, not written, for the reason every deferral in this codebase is: Streamlit
    forbids writing a widget's own session_state key once that widget exists this run, and
    both callers — saving, and deleting — run in a dialog below the bar, well past that
    point. Leaving the key alone is not an option either: after a delete it would still hold
    an id that is no longer one of the picker's options.
    """
    st.session_state[DC_TEMPLATE_PENDING_KEY] = selection


def consume_template_selection() -> None:
    """Applies a queued picker selection. Call before the picker is created."""
    if DC_TEMPLATE_PENDING_KEY not in st.session_state:
        return
    st.session_state[DC_TEMPLATE_PICK_KEY] = st.session_state.pop(DC_TEMPLATE_PENDING_KEY)


def open_template_dialog(name: str, payload: dict | None = None) -> None:
    """Marks which template dialog should be showing, on the same grounds as `open_dialog`."""
    st.session_state[DC_TEMPLATE_DIALOG_KEY] = {"name": name, "payload": payload or {}}


def close_template_dialog() -> None:
    """Dismisses whichever template dialog is open."""
    st.session_state.pop(DC_TEMPLATE_DIALOG_KEY, None)


def pending_template_dialog() -> tuple[str, dict] | None:
    """Returns the (name, payload) of the open template dialog, or None if none is open."""
    pending = st.session_state.get(DC_TEMPLATE_DIALOG_KEY)
    if not pending:
        return None
    return pending["name"], pending.get("payload") or {}


def queue_flash(message: str) -> None:
    """Holds a message for the next run.

    Every caller here ends in `st.rerun`, and anything written just before one never
    reaches the screen — the same reason `tasks.session.queue_flash` exists.
    """
    st.session_state[DC_FLASH_KEY] = message


def consume_flash() -> str | None:
    """Takes the queued message, if there is one."""
    return st.session_state.pop(DC_FLASH_KEY, None)
