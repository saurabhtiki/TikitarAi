"""Session state for the Data Engine — the only module in `engine/` that imports Streamlit.

Same convention as `cleaner/session.py`: everything else in the package stays pure and
testable without `AppTest`.

The DuckDB connection lives in `st.session_state`, deliberately **not** in
`st.cache_resource`. `cache_resource` is process-wide, so one user's uploaded tables
would appear in another user's session — flatly against requirement 5.1's session
scoping, and a data leak between accounts rather than a mere bug. Session state is
per-session by construction, and `logout()`'s existing `st.session_state.clear()`
therefore already tears the whole engine down.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd
import streamlit as st

from cleaner import loaders, pipeline
from cleaner import session as cleaner_session
from cleaner.exceptions import DataCleanerError
from engine import dictionary, duckdb_session, loading, relationships
from engine.exceptions import DataEngineError, TableLoadError
from engine.relationships import Relationship

logger = logging.getLogger(__name__)

DE_CONNECTION_KEY = "de_connection"
DE_TABLES_KEY = "de_tables"
DE_RELATIONSHIPS_KEY = "de_relationships"
DE_CANDIDATES_KEY = "de_candidates"
DE_DICTIONARY_KEY = "de_dictionary"
DE_STATEMENTS_KEY = "de_calculated_statements"
DE_UPLOADER_KEY = "de_uploader"
DE_DIALOG_KEY = "de_open_dialog"
DE_PENDING_STEPS_KEY = "de_pending_step_state"
DE_AUTOCOLLAPSED_KEY = "de_autocollapsed_steps"
DE_REBUILD_KEY = "de_rebuild_count"
DE_START_OVER_KEY = "de_start_over_pending"

STEP_UPLOAD = "de_step_upload"
STEP_LINKS = "de_step_links"
STEP_DICTIONARY = "de_step_dictionary"

MAX_UPLOAD_SIZE_MB = 50
PREVIEW_ROWS = 100


@dataclass
class EngineTable:
    """One table loaded into the engine."""

    table_id: str
    table_name: str
    source_label: str
    file_id: str | None = None
    sheet_name: str | None = None
    semantic_types: dict[str, str] = field(default_factory=dict)
    row_count: int = 0
    from_cleaner: bool = False


# --------------------------------------------------------------------------------------
# Connection and raw state
# --------------------------------------------------------------------------------------


def connection():
    """The session's DuckDB instance, created on first use."""
    existing = st.session_state.get(DE_CONNECTION_KEY)
    if existing is not None:
        return existing

    created = duckdb_session.open_connection()
    st.session_state[DE_CONNECTION_KEY] = created
    logger.info("Opened a DuckDB instance for this session.")
    return created


def get_tables() -> dict[str, EngineTable]:
    """The loaded tables, keyed by table_id."""
    return st.session_state.setdefault(DE_TABLES_KEY, {})


def table_names() -> list[str]:
    """The working table names, in load order."""
    return [table.table_name for table in get_tables().values()]


def get_relationships() -> list[Relationship]:
    """The confirmed relationships — used for querying whether or not they're enforced
    as a real foreign key (see `engine.relationships.enforceable`)."""
    return st.session_state.setdefault(DE_RELATIONSHIPS_KEY, [])


def set_relationships(confirmed: list[Relationship]) -> None:
    st.session_state[DE_RELATIONSHIPS_KEY] = list(confirmed)


def get_candidates() -> list:
    """The suggestions currently under review in step 2."""
    return st.session_state.setdefault(DE_CANDIDATES_KEY, [])


def set_candidates(candidates: list) -> None:
    st.session_state[DE_CANDIDATES_KEY] = list(candidates)


def get_statements() -> list[str]:
    """The ordered calculated-column statements.

    Replayed after every relationship rebuild, and the exact list requirement 7.5 has
    Task Builder persist as part of the recipe.
    """
    return st.session_state.setdefault(DE_STATEMENTS_KEY, [])


def add_statements(statements: list[str]) -> None:
    get_statements().extend(statements)


def get_dictionary() -> list:
    """The column dictionary entries."""
    return st.session_state.setdefault(DE_DICTIONARY_KEY, [])


def set_dictionary(entries: list) -> None:
    st.session_state[DE_DICTIONARY_KEY] = list(entries)


def rebuild_count() -> int:
    """Bumped on every schema change, so cached previews invalidate exactly then."""
    return st.session_state.setdefault(DE_REBUILD_KEY, 0)


def bump_rebuild() -> None:
    st.session_state[DE_REBUILD_KEY] = rebuild_count() + 1


# --------------------------------------------------------------------------------------
# Dialogs and deferred widget writes
# --------------------------------------------------------------------------------------


def open_dialog(action: str, payload: dict | None = None) -> None:
    """Flags which dialog should be showing.

    Held in session state rather than read from a button's return value, for the reason
    `cleaner/session.py` documents: `if st.button(...): show_dialog()` breaks as soon as
    the dialog contains widgets, because interacting with one reruns the script, the
    button reads False, and the dialog closes mid-edit.
    """
    st.session_state[DE_DIALOG_KEY] = {"action": action, "payload": payload or {}}


def close_dialog() -> None:
    st.session_state.pop(DE_DIALOG_KEY, None)


def pending_dialog() -> tuple[str, dict] | None:
    """The open dialog's `(action, payload)`, or None."""
    pending = st.session_state.get(DE_DIALOG_KEY)
    if not pending:
        return None
    return pending["action"], pending.get("payload", {})


def queue_step_state(step_key: str, expanded: bool) -> None:
    """Queues an expander to be opened or collapsed on the next run.

    Streamlit forbids writing a widget's own session-state key once that widget exists
    this run, and by the time "Confirm links" is pressed its expander certainly does.
    So the change is deferred and applied at the top of the next run — the same pattern
    `cleaner/session.py::queue_start_over` uses for the uploader.

    This is the **only** way a step's open state is changed programmatically. Passing a
    varying `expanded=` to `st.expander` looks like it would do the same job and does
    not: Streamlit re-applies that argument whenever its value changes, overriding the
    stored state. An `expanded=not loaded_tables` that flips to False the moment data
    arrives therefore force-collapses the step *and keeps it collapsed*, taking the
    uploader inside it out of reach for good. So every expander on the page passes a
    constant, and everything dynamic comes through here.
    """
    st.session_state.setdefault(DE_PENDING_STEPS_KEY, {})[step_key] = bool(expanded)


def collapse_once(step_key: str) -> None:
    """Collapses a step the first time its work is done, and never again.

    "Once" matters: re-collapsing on every rerun would fight a user who deliberately
    re-opened the step to change something.
    """
    already = st.session_state.setdefault(DE_AUTOCOLLAPSED_KEY, set())
    if step_key in already:
        return
    already.add(step_key)
    queue_step_state(step_key, False)


def consume_step_state() -> None:
    """Applies any queued expander changes. Call before the expanders are created."""
    for step_key, expanded in st.session_state.pop(DE_PENDING_STEPS_KEY, {}).items():
        st.session_state[step_key] = expanded


def step_is_open(step_key: str, default: bool) -> bool:
    """Whether a step's expander should render its body.

    `.open` on the container is the authority once the widget exists; this answers the
    same question before it does, which is what the `expanded=` argument needs.
    """
    return bool(st.session_state.get(step_key, default))


# --------------------------------------------------------------------------------------
# Loading tables
# --------------------------------------------------------------------------------------


def _register(
    table_id: str,
    table_name: str,
    source_label: str,
    frame: pd.DataFrame,
    semantic_types: dict[str, str],
    *,
    file_id: str | None = None,
    sheet_name: str | None = None,
    from_cleaner: bool = False,
) -> EngineTable:
    duckdb_session.register_table(connection(), table_name, frame)
    return EngineTable(
        table_id=table_id,
        table_name=table_name,
        source_label=source_label,
        file_id=file_id,
        sheet_name=sheet_name,
        semantic_types=semantic_types,
        row_count=len(frame),
        from_cleaner=from_cleaner,
    )


def sync_tables(uploaded_files, sheet_selection: dict[str, list[str]]) -> list[EngineTable]:
    """Reconciles the loaded tables with what is currently in the uploader.

    Runs in **both** directions every rerun, exactly as the Data Cleaner's equivalent
    does: new file/sheet combinations are loaded, and any table whose file or sheet is no
    longer selected is dropped. Registering only additions would leave the user querying
    tables they believe they removed.

    Tables adopted from the Data Cleaner are left alone — they have no uploader entry
    here, so a one-directional read of the uploader would wrongly delete them.

    Returns the loaded tables. Never raises: a file that can't be read is skipped with an
    error on screen, and the others still load.
    """
    existing = get_tables()
    reconciled: dict[str, EngineTable] = {
        table_id: table for table_id, table in existing.items() if table.from_cleaner
    }
    taken = {table.table_name for table in reconciled.values()}

    for uploaded_file in uploaded_files or []:
        if loaders.is_csv(uploaded_file.name):
            sheets: list[str | None] = [None]
        else:
            sheets = list(sheet_selection.get(uploaded_file.file_id) or [])

        for sheet_name in sheets:
            table_id = f"{uploaded_file.file_id}::{sheet_name or ''}"
            source_label = f"{uploaded_file.name} — {sheet_name}" if sheet_name else uploaded_file.name

            already = existing.get(table_id)
            if already is not None:
                reconciled[table_id] = already
                taken.add(already.table_name)
                continue

            base_label = sheet_name or uploaded_file.name.rsplit(".", 1)[0]
            table_name = duckdb_session.slugify_table_name(base_label, taken)
            try:
                frame, semantic_types = loading.prepare_table(
                    uploaded_file.getvalue(), uploaded_file.name, sheet_name
                )
                reconciled[table_id] = _register(
                    table_id,
                    table_name,
                    source_label,
                    frame,
                    semantic_types,
                    file_id=uploaded_file.file_id,
                    sheet_name=sheet_name,
                )
                taken.add(table_name)
            except (TableLoadError, DataEngineError) as error:
                logger.exception("Could not load '%s' into the engine.", source_label)
                st.error(f"{source_label}: {error}")

    _drop_removed(existing, reconciled)
    st.session_state[DE_TABLES_KEY] = reconciled
    return list(reconciled.values())


def _drop_removed(existing: dict[str, EngineTable], reconciled: dict[str, EngineTable]) -> None:
    """Removes tables that are no longer selected, and any relationship touching them.

    Relationships go first and the tables are then rebuilt, because DuckDB refuses to
    drop a table another still references through a foreign key — the drop would fail
    with a catalog error that means nothing to the user.
    """
    removed = [existing[table_id] for table_id in set(existing) - set(reconciled)]
    if not removed:
        return

    removed_names = {table.table_name for table in removed}
    surviving = [
        relationship
        for relationship in get_relationships()
        if relationship.child_table not in removed_names and relationship.parent_table not in removed_names
    ]

    if len(surviving) != len(get_relationships()):
        set_relationships(surviving)
        remaining_names = [table.table_name for table in reconciled.values()]
        try:
            clean = relationships.enforceable(connection(), surviving)
            relationships.enforce(connection(), remaining_names, clean, get_statements())
        except DataEngineError:
            logger.exception("Could not rebuild after removing tables.")

    for table in removed:
        try:
            duckdb_session.drop_table(connection(), table.table_name)
        except DataEngineError:
            logger.exception("Could not drop '%s' after it was removed from the uploader.", table.table_name)

    logger.info("Dropped %d table(s) no longer selected.", len(removed))
    bump_rebuild()


# --------------------------------------------------------------------------------------
# Data Cleaner handoff
# --------------------------------------------------------------------------------------


def cleaner_tables_available() -> list[str]:
    """The source labels of tables currently loaded in the Data Cleaner.

    Both pages share one `st.session_state`, so this is a plain read — no persistence and
    no round trip through a downloaded file.
    """
    return [table.source_label for table in cleaner_session.get_tables().values()]


def adopt_cleaner_tables() -> tuple[list[EngineTable], list[str]]:
    """Loads the Data Cleaner's cleaned tables straight into the engine.

    Uses `cleaner.session.cleaned_table`, the cached derivation the Data Cleaner page
    already renders from, so what lands here is exactly what the user saw there —
    including every recorded cleaning step — with no second cleaning implementation.

    The tables are snapshotted by copy. Cleaning further afterwards does not
    retroactively change what is loaded here; the button simply stays available to
    re-adopt, which re-registers under the same name rather than accumulating copies.

    Returns:
        `(adopted_tables, warnings)`.
    """
    # Not `st.session_state[DC_UPLOADER_KEY]`: `file_uploader`'s own widget state is
    # ephemeral and only reliable on the page that renders it (the Data Cleaner), so
    # reading it from here came back empty after navigating — the cached bytes below
    # are a plain session_state dict and don't have that restriction.
    bytes_by_file = cleaner_session.cached_file_bytes()

    existing = get_tables()
    adopted: dict[str, EngineTable] = {
        table_id: table for table_id, table in existing.items() if not table.from_cleaner
    }
    taken = {table.table_name for table in adopted.values()}
    warnings: list[str] = []

    for cleaner_table in cleaner_session.get_tables().values():
        table_id = f"cleaner::{cleaner_table.table_id}"
        file_bytes = bytes_by_file.get(cleaner_table.file_id)
        if file_bytes is None:
            warnings.append(f"'{cleaner_table.source_label}' is no longer available in the Data Cleaner.")
            continue

        try:
            frame, _ = cleaner_session.cleaned_table(cleaner_table, file_bytes)
            frame, semantic_types = loading.prepare_cleaned_frame(
                frame, pipeline.declared_column_types(cleaner_table.steps)
            )
            table_name = duckdb_session.slugify_table_name(cleaner_table.output_sheet_name, taken)
            adopted[table_id] = _register(
                table_id,
                table_name,
                cleaner_table.source_label,
                frame,
                semantic_types,
                from_cleaner=True,
            )
            taken.add(table_name)
        except (TableLoadError, DataEngineError, DataCleanerError) as error:
            # DataCleanerError comes from `cleaner_session.cleaned_table` above (e.g. a
            # corrupted upload) — a different exception hierarchy than the engine's own,
            # so it needs naming here too rather than only the engine's exceptions.
            logger.exception("Could not adopt '%s' from the Data Cleaner.", cleaner_table.source_label)
            warnings.append(f"'{cleaner_table.source_label}': {error}")

    st.session_state[DE_TABLES_KEY] = adopted
    bump_rebuild()
    logger.info("Adopted %d table(s) from the Data Cleaner.", sum(1 for t in adopted.values() if t.from_cleaner))
    return list(adopted.values()), warnings


# --------------------------------------------------------------------------------------
# Derived views
# --------------------------------------------------------------------------------------


@st.cache_data(show_spinner=False, max_entries=32, scope="session")
def _cached_preview(table_name: str, rebuild_token: int, limit: int) -> pd.DataFrame:
    """Keyed on the rebuild counter rather than the frame, so a schema change
    invalidates exactly the previews it should and nothing else."""
    return duckdb_session.preview(connection(), table_name, limit)


def preview(table_name: str, limit: int = PREVIEW_ROWS) -> pd.DataFrame:
    """The first rows of a table, cached until the schema next changes."""
    return _cached_preview(table_name, rebuild_count(), limit)


def semantic_types_by_table() -> dict[str, dict[str, str]]:
    """`{table_name: {column: semantic_type}}` across every loaded table."""
    return {table.table_name: table.semantic_types for table in get_tables().values()}


def refresh_dictionary() -> list:
    """Rebuilds the dictionary against the current schema, keeping typed descriptions.

    Called after anything that changes the columns — a new upload, a rebuild, an added
    calculated column — so the grid always matches the tables, without the user losing
    what they have already written.
    """
    entries = dictionary.build_dictionary(
        connection(), table_names(), semantic_types_by_table(), existing=get_dictionary()
    )
    set_dictionary(entries)
    return entries


def schema_context() -> str:
    """The schema block Stage 6's agent will receive with every question."""
    return dictionary.schema_context(get_dictionary(), get_relationships())


# --------------------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------------------


def queue_start_over() -> None:
    """Marks the session to be cleared on the next run.

    Deferred for the same two reasons as the Data Cleaner's: clearing the tables alone
    would be undone within the same run by `sync_tables` re-reading the still-populated
    uploader, and the uploader's own key can't be written after the widget exists.
    """
    st.session_state[DE_START_OVER_KEY] = True


def consume_start_over() -> bool:
    """Applies a queued reset. Call before the uploader renders."""
    if not st.session_state.pop(DE_START_OVER_KEY, False):
        return False
    reset_engine()
    st.session_state.pop(DE_UPLOADER_KEY, None)
    return True


def reset_engine() -> None:
    """Closes the DuckDB instance and clears every `de_*` key."""
    existing = st.session_state.get(DE_CONNECTION_KEY)
    if existing is not None:
        try:
            existing.close()
        except Exception:  # noqa: BLE001 — a connection that won't close must not block a reset
            logger.warning("The DuckDB connection didn't close cleanly.", exc_info=True)

    for key in (
        DE_CONNECTION_KEY,
        DE_TABLES_KEY,
        DE_RELATIONSHIPS_KEY,
        DE_CANDIDATES_KEY,
        DE_DICTIONARY_KEY,
        DE_STATEMENTS_KEY,
        DE_DIALOG_KEY,
        DE_PENDING_STEPS_KEY,
        DE_AUTOCOLLAPSED_KEY,
    ):
        st.session_state.pop(key, None)

    _cached_preview.clear()
    bump_rebuild()
