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

from cleaner import loaders, naming, pipeline, profiling
from cleaner.exceptions import DataCleanerError
from cleaner.pipeline import CLEANING_RECIPE_VERSION, Step, StepOutcome

logger = logging.getLogger(__name__)

DC_TABLES_KEY = "dc_tables"
DC_UPLOADER_KEY = "dc_uploader"
DC_TABS_KEY = "dc_tabs"
DC_DIALOG_KEY = "dc_open_dialog"
DC_START_OVER_KEY = "dc_start_over_pending"
DC_FILE_BYTES_KEY = "dc_file_bytes"

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
    """Applies a queued start-over, if one is pending. Call before the uploader renders."""
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
