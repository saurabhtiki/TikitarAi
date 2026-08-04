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

import pandas as pd
import streamlit as st

from cleaner import loaders, naming, pipeline, profiling
from cleaner.exceptions import DataCleanerError
from cleaner.pipeline import CLEANING_RECIPE_VERSION, Step, StepOutcome

logger = logging.getLogger(__name__)

DC_TABLES_KEY = "dc_tables"
DC_UPLOADER_KEY = "dc_uploader"
DC_TABS_KEY = "dc_tabs"
DC_RESET_DIALOG_KEY = "dc_reset_dialog_table_id"
DC_START_OVER_KEY = "dc_start_over_pending"

MAX_UPLOAD_SIZE_MB = 50
PREVIEW_ROWS = 500
LARGE_TABLE_ROWS = 250_000


@dataclass
class TableState:
    """One cleanable table: where it came from, what to call it, and its recipe."""

    table_id: str
    file_id: str
    file_name: str
    sheet_name: str | None
    source_label: str
    output_sheet_name: str
    steps: list[Step] = field(default_factory=list)
    recipe_version: int = CLEANING_RECIPE_VERSION


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


def cleaned_table(table: TableState, file_bytes: bytes) -> tuple[pd.DataFrame, list[StepOutcome]]:
    """Returns the cleaned table and the per-step report.

    Raises:
        DataCleanerError: if the file can no longer be read, or a recorded step names an
            unknown action.
    """
    return _cached_cleaned_table(table.table_id, table.steps, raw_table(table, file_bytes))


def get_tables() -> dict[str, TableState]:
    """Returns the current working set, keyed by table_id."""
    return st.session_state.setdefault(DC_TABLES_KEY, {})


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

    Raises:
        DataCleanerError: if a newly added file can't be read.
    """
    existing = get_tables()
    reconciled: dict[str, TableState] = {}
    taken_sheet_names: set[str] = set()

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

    dropped = set(existing) - set(reconciled)
    if dropped:
        logger.info("Dropped %d table(s) no longer selected in the uploader.", len(dropped))

    st.session_state[DC_TABLES_KEY] = reconciled
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
        log[sheet_name] = pipeline.describe_steps(table.steps)

    return export.build_workbook(payload, log)
