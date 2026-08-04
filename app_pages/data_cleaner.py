"""Data Cleaner utility — upload one or more CSV/Excel files, clean each table, download
a single multi-sheet workbook.

Open to every logged-in role (requirements 2.2 grants Utilities to all three), so this
page follows `settings.py` and carries no `require_role` guard.

Each table's tab body is an `st.fragment` and the tab set is gated on `tab.open`, so a
widget interaction costs one panel's rerun rather than re-running every loaded table's
pipeline. Actions are plain widgets rather than forms precisely so the impact sentence
under each Apply button updates live as the user configures it.
"""

import logging

import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from cleaner import loaders, pipeline, profiling, session
from cleaner.exceptions import DataCleanerError, InvalidStepError
from cleaner.steps import (
    CASE_CHOICES,
    DEFAULT_KEEP_PATTERN,
    DUPLICATE_KEEP_CHOICES,
    FILL_STRATEGIES,
    get_spec,
)
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

_FILL_LABELS = {
    "zero": "Fill with 0",
    "mean": "Fill with the mean",
    "median": "Fill with the median",
    "unknown": "Fill with 'Unknown'",
    "drop_rows": "Drop affected rows",
}

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


# --------------------------------------------------------------------------------------
# Step handling
# --------------------------------------------------------------------------------------


def _apply_step(table_id: str, action: str, params: dict, *, to_all_tables: bool = False) -> None:
    """Validates and records a step, then reruns so every panel sees the new data.

    Validation runs against the columns the step will actually see, so an invalid step
    is rejected before it can enter the recipe — that is what keeps a stored recipe
    well-formed for Stage 7 to serialize later.
    """
    targets = list(session.get_tables().values()) if to_all_tables else [session.get_table(table_id)]
    applied = 0

    for table in targets:
        if table is None:
            continue
        try:
            frame = _current_frame(table)
            if frame is None:
                continue
            step = pipeline.make_step(action, params)
            pipeline.validate_step(step, list(frame.columns))
            session.set_steps(table.table_id, pipeline.add_step(table.steps, step))
            applied += 1
        except InvalidStepError as error:
            if not to_all_tables:
                st.warning(str(error), icon=":material/error:")
                return
            logger.info("Skipped '%s' for table %s: %s", action, table.table_id, error)
        except DataCleanerError as error:
            logger.exception("Could not apply '%s' to table %s.", action, table.table_id)
            st.error(str(error))
            return

    if applied == 0:
        st.warning("That action couldn't be applied to any table.", icon=":material/error:")
        return
    st.rerun(scope="app")


def _remove_step(table_id: str, index: int) -> None:
    table = session.get_table(table_id)
    if table is None:
        return
    try:
        session.set_steps(table_id, pipeline.remove_step(table.steps, index))
    except IndexError:
        logger.warning("Tried to remove step %s from table %s, which no longer has it.", index, table_id)
        return
    st.rerun(scope="app")


def _current_frame(table: session.TableState):
    """The table as it stands after its recorded steps, or None if it can't be read."""
    file_bytes = _uploaded_bytes().get(table.file_id)
    if file_bytes is None:
        return None
    try:
        frame, _ = session.cleaned_table(table, file_bytes)
    except DataCleanerError:
        logger.exception("Could not derive the cleaned table for %s.", table.table_id)
        return None
    return frame


def _preview_impact(frame, action: str, params: dict) -> str | None:
    """Dry-runs a step to describe its effect before the user commits to it.

    Runs the real executor rather than a parallel estimate, so the sentence shown can
    never drift from what the action actually does.
    """
    try:
        spec = get_spec(action)
        spec.validate(params, list(frame.columns))
        result, warnings_out = spec.apply(frame, params)
    except (InvalidStepError, ValueError, TypeError, KeyError):
        return None

    parts = []
    removed_rows = len(frame) - len(result)
    removed_columns = len(frame.columns) - len(result.columns)
    if removed_rows > 0:
        parts.append(f"removes {removed_rows:,} row(s)")
    if removed_columns > 0:
        parts.append(f"removes {removed_columns:,} column(s)")

    changed = _changed_cells(frame, result)
    if changed:
        parts.append(f"changes {changed:,} cell(s)")
    if warnings_out:
        parts.append(warnings_out[0])

    return "This " + ", ".join(parts) + "." if parts else "This makes no change to the current data."


def _changed_cells(before, after) -> int:
    shared = [column for column in before.columns if column in after.columns]
    if not shared or len(before) != len(after):
        return 0
    try:
        left = before[shared].astype("string")
        right = after[shared].astype("string")
        return int((left.fillna("") != right.fillna("")).to_numpy().sum())
    except (ValueError, TypeError):
        return 0


# --------------------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------------------


def _uploaded_bytes() -> dict[str, bytes]:
    uploads = st.session_state.get(session.DC_UPLOADER_KEY) or []
    return {upload.file_id: upload.getvalue() for upload in uploads}


def _render_upload() -> list[session.TableState]:
    # Consumed before the uploader is instantiated: Streamlit won't allow a widget's own
    # session_state key to be written once that widget exists this run.
    session.consume_start_over()

    uploads = st.file_uploader(
        "Upload CSV or Excel files",
        type=["csv", "txt", "tsv", "xlsx", "xlsm"],
        accept_multiple_files=True,
        key=session.DC_UPLOADER_KEY,
        max_upload_size=session.MAX_UPLOAD_SIZE_MB,
        help="Every cell is read as text first, so leading zeros in IDs and account numbers survive.",
    )

    sheet_selection: dict[str, list[str]] = {}
    for upload in uploads or []:
        if loaders.is_csv(upload.name):
            continue
        try:
            available = loaders.list_sheet_names(upload.getvalue(), upload.name)
        except DataCleanerError as error:
            st.error(str(error))
            continue
        sheet_selection[upload.file_id] = st.multiselect(
            f"Sheets to clean from {upload.name}",
            options=available,
            default=available,
            key=f"dc_sheets_{upload.file_id}",
            help="Each selected sheet becomes its own tab and its own sheet in the download.",
        )

    try:
        return session.sync_tables(uploads, sheet_selection)
    except DataCleanerError as error:
        logger.exception("Could not load one or more uploaded tables.")
        st.error(str(error))
        return []


# --------------------------------------------------------------------------------------
# Action panels
# --------------------------------------------------------------------------------------


def _render_structure_panel(table: session.TableState, frame) -> None:
    table_id = table.table_id
    columns = list(frame.columns)

    with st.expander("1. Structure", icon=":material/table_rows:"):
        top = st.number_input(
            "Skip rows from the top",
            min_value=0,
            value=0,
            step=1,
            key=f"dc_skip_top_{table_id}",
            help="Junk rows above the real header, such as a report title.",
        )
        bottom = st.number_input(
            "Skip rows from the bottom",
            min_value=0,
            value=0,
            step=1,
            key=f"dc_skip_bottom_{table_id}",
            help="Trailing rows such as totals or footnotes.",
        )
        promote_header = st.checkbox(
            "Use the first remaining row as the header",
            value=True,
            key=f"dc_promote_header_{table_id}",
            help="Almost always what you want when skipping junk rows — otherwise the real header stays a data row.",
        )
        skip_params = {"top": int(top), "bottom": int(bottom), "promote_header": promote_header}
        _impact_caption(frame, "skip_rows", skip_params)
        if st.button(
            "Apply row skipping",
            key=f"dc_apply_skip_{table_id}",
            icon=":material/content_cut:",
            type="primary",
            help="Record this as a cleaning step.",
        ):
            _apply_step(table_id, "skip_rows", skip_params)

        st.divider()
        empty_scope = st.multiselect(
            "Remove rows blank across these columns",
            options=columns,
            key=f"dc_empty_columns_{table_id}",
            help="Leave empty to remove only rows that are blank in every column.",
        )
        empty_params = {"columns": empty_scope or None, "blank_strings_count_as_empty": True}
        _impact_caption(frame, "remove_empty_rows", empty_params)
        if st.button(
            "Remove empty rows",
            key=f"dc_apply_empty_{table_id}",
            icon=":material/delete_sweep:",
            type="primary",
            help="Drop rows that are blank across the chosen columns.",
        ):
            _apply_step(table_id, "remove_empty_rows", empty_params)

        st.divider()
        to_delete = st.multiselect(
            "Columns to delete",
            options=columns,
            key=f"dc_delete_columns_{table_id}",
            help="Remove columns you don't need in the output.",
        )
        if st.button(
            "Delete columns",
            key=f"dc_apply_delete_{table_id}",
            icon=":material/delete:",
            type="primary",
            help="Drop the selected columns.",
            disabled=not to_delete,
        ):
            _apply_step(table_id, "delete_columns", {"columns": to_delete})

        st.divider()
        rename_source = st.selectbox(
            "Column to rename",
            options=columns,
            key=f"dc_rename_source_{table_id}",
            help="Pick the column whose name you want to change.",
        )
        rename_target = st.text_input(
            "New name",
            key=f"dc_rename_target_{table_id}",
            help="Duplicate names are blocked — every column must stay uniquely addressable.",
        )
        if st.button(
            "Rename column",
            key=f"dc_apply_rename_{table_id}",
            icon=":material/edit:",
            type="primary",
            help="Record this rename as a cleaning step.",
            disabled=not (rename_source and rename_target.strip()),
        ):
            _apply_step(table_id, "rename_columns", {"renames": {rename_source: rename_target.strip()}})


def _render_types_panel(table: session.TableState, frame) -> None:
    table_id = table.table_id

    with st.expander("2. Column types", icon=":material/category:"):
        st.caption("Detected types are applied automatically on upload. Override any of them here.")
        selected = st.multiselect(
            "Columns to retype",
            options=list(frame.columns),
            key=f"dc_type_columns_{table_id}",
            help="Choose one or more columns, then the type they should be.",
        )
        target_type = st.selectbox(
            "Set them to",
            options=profiling.COLUMN_TYPES,
            key=f"dc_type_target_{table_id}",
            help="'id' keeps values as text so leading zeros survive; 'categorical' marks a low-cardinality label column.",
        )

        settings: dict = {"target_type": target_type}
        if target_type == profiling.NUMERIC:
            settings["decimal_separator"] = st.selectbox(
                "Decimal separator",
                options=[".", ","],
                key=f"dc_type_decimal_{table_id}",
                help="Never guessed: '1.200' is twelve hundred in some locales and 1.2 in others.",
            )
        elif target_type == profiling.DATE:
            date_format = st.text_input(
                "Date format (optional)",
                key=f"dc_type_date_format_{table_id}",
                help="Leave blank to detect automatically, or give a format such as %d/%m/%Y.",
            )
            settings["date_format"] = date_format.strip() or None

        params = {"by_column": {column: settings for column in selected}}
        if selected:
            _impact_caption(frame, "set_column_types", params)
        if st.button(
            "Apply types",
            key=f"dc_apply_types_{table_id}",
            icon=":material/done_all:",
            type="primary",
            help="Record the type change as a cleaning step.",
            disabled=not selected,
        ):
            _apply_step(table_id, "set_column_types", params)


def _render_text_panel(table: session.TableState, frame) -> None:
    table_id = table.table_id
    text_options = profiling.text_columns(frame)

    with st.expander("3. Text cleanup", icon=":material/text_fields:"):
        collapse = st.checkbox(
            "Also collapse repeated spaces inside values",
            value=True,
            key=f"dc_trim_collapse_{table_id}",
            help="Turns 'New   York' into 'New York'. Non-breaking and zero-width characters are always removed.",
        )
        trim_all = st.checkbox(
            "Apply to all loaded tables",
            key=f"dc_trim_all_{table_id}",
            help="Run this on every table you've uploaded, not just this one.",
        )
        if st.button(
            "Trim whitespace",
            key=f"dc_apply_trim_{table_id}",
            icon=":material/format_clear:",
            type="primary",
            help="Strip leading, trailing and invisible whitespace from every text column.",
        ):
            _apply_step(table_id, "trim_whitespace", {"collapse_internal": collapse}, to_all_tables=trim_all)

        st.divider()
        keep_pattern = st.text_input(
            "Characters to keep",
            value=DEFAULT_KEEP_PATTERN,
            key=f"dc_keep_pattern_{table_id}",
            help="A regular-expression character set. Anything outside it is removed from text columns.",
        )
        replacement = st.text_input(
            "Replace removed characters with",
            key=f"dc_special_replacement_{table_id}",
            help="Leave blank to delete them outright.",
        )
        special_all = st.checkbox(
            "Apply to all loaded tables",
            key=f"dc_special_all_{table_id}",
            help="Run this on every table you've uploaded, not just this one.",
        )
        if st.button(
            "Remove special characters",
            key=f"dc_apply_special_{table_id}",
            icon=":material/cleaning_services:",
            type="primary",
            help="Strip characters outside the set above from every text column.",
        ):
            _apply_step(
                table_id,
                "remove_special_characters",
                {"keep_pattern": keep_pattern, "replacement": replacement},
                to_all_tables=special_all,
            )

        st.divider()
        case_columns = st.multiselect(
            "Change letter case in",
            options=text_options,
            key=f"dc_case_columns_{table_id}",
            help="Only text columns can have their case changed.",
        )
        case_choice = st.selectbox(
            "Case",
            options=CASE_CHOICES,
            key=f"dc_case_choice_{table_id}",
            help="UPPER, lower, or Title Case.",
        )
        if st.button(
            "Change case",
            key=f"dc_apply_case_{table_id}",
            icon=":material/match_case:",
            type="primary",
            help="Record the case change as a cleaning step.",
            disabled=not case_columns,
        ):
            _apply_step(table_id, "change_case", {"by_column": {column: case_choice for column in case_columns}})

        st.divider()
        st.caption("Find & replace")
        fr_columns = st.multiselect(
            "In columns",
            options=text_options,
            key=f"dc_fr_columns_{table_id}",
            help="Find & replace only reaches text columns — set a column back to text first if you need it here.",
        )
        find = st.text_input("Find", key=f"dc_fr_find_{table_id}", help="The text or pattern to search for.")
        replace = st.text_input(
            "Replace with", key=f"dc_fr_replace_{table_id}", help="Leave blank to delete what was found."
        )
        use_regex = st.checkbox(
            "Treat as a regular expression",
            key=f"dc_fr_regex_{table_id}",
            help="Enables patterns such as ^N\\.Y\\.$ and capture groups in the replacement.",
        )
        case_sensitive = st.checkbox(
            "Match case",
            value=True,
            key=f"dc_fr_case_{table_id}",
            help="Uncheck to match regardless of capitalisation.",
        )
        fr_params = {
            "columns": fr_columns,
            "find": find,
            "replace": replace,
            "regex": use_regex,
            "case_sensitive": case_sensitive,
        }
        if fr_columns and find:
            _impact_caption(frame, "find_replace", fr_params)
        if st.button(
            "Find & replace",
            key=f"dc_apply_fr_{table_id}",
            icon=":material/find_replace:",
            type="primary",
            help="Record this replacement as a cleaning step.",
            disabled=not (fr_columns and find),
        ):
            _apply_step(table_id, "find_replace", fr_params)


def _render_missing_panel(table: session.TableState, frame) -> None:
    table_id = table.table_id
    columns = list(frame.columns)

    with st.expander("4. Missing values", icon=":material/help_center:"):
        fill_columns = st.multiselect(
            "Columns",
            options=columns,
            key=f"dc_fill_columns_{table_id}",
            help="Choose the columns whose blanks you want to handle.",
        )
        strategy = st.selectbox(
            "Strategy",
            options=FILL_STRATEGIES,
            format_func=lambda choice: _FILL_LABELS[choice],
            key=f"dc_fill_strategy_{table_id}",
            help="Mean and median need a numeric column — set the type first if needed.",
        )
        fill_params = {"by_column": {column: strategy for column in fill_columns}}
        if fill_columns:
            _impact_caption(frame, "fill_missing", fill_params)
        if st.button(
            "Apply to missing values",
            key=f"dc_apply_fill_{table_id}",
            icon=":material/water_drop:",
            type="primary",
            help="Record this missing-value handling as a cleaning step.",
            disabled=not fill_columns,
        ):
            _apply_step(table_id, "fill_missing", fill_params)

        st.divider()
        numeric_columns = st.multiselect(
            "Fix numbers stored as text in",
            options=profiling.text_columns(frame),
            key=f"dc_fixnum_columns_{table_id}",
            help="Strips currency symbols and thousands separators, and reads (300) as -300.",
        )
        decimal_separator = st.selectbox(
            "Decimal separator",
            options=[".", ","],
            key=f"dc_fixnum_decimal_{table_id}",
            help="Never guessed — pick the one your source file uses.",
        )
        fixnum_params = {
            "columns": numeric_columns,
            "decimal_separator": decimal_separator,
            "parentheses_are_negative": True,
        }
        if numeric_columns:
            _impact_caption(frame, "fix_numeric_text", fixnum_params)
        if st.button(
            "Fix numbers stored as text",
            key=f"dc_apply_fixnum_{table_id}",
            icon=":material/functions:",
            type="primary",
            help="Convert these text columns into real numbers.",
            disabled=not numeric_columns,
        ):
            _apply_step(table_id, "fix_numeric_text", fixnum_params)


def _render_duplicates_panel(table: session.TableState, frame) -> None:
    table_id = table.table_id

    with st.expander("5. Duplicates", icon=":material/content_copy:"):
        subset = st.multiselect(
            "A row is a duplicate when these columns match",
            options=list(frame.columns),
            key=f"dc_dupe_columns_{table_id}",
            help="Leave empty to require every column to match.",
        )
        keep = st.selectbox(
            "Keep",
            options=DUPLICATE_KEEP_CHOICES,
            key=f"dc_dupe_keep_{table_id}",
            help="Which of each duplicate group to keep.",
        )
        dupe_params = {"columns": subset or None, "keep": keep}
        _impact_caption(frame, "drop_duplicates", dupe_params)
        if st.button(
            "Remove duplicate rows",
            key=f"dc_apply_dupes_{table_id}",
            icon=":material/filter_alt:",
            type="primary",
            help="Record duplicate removal as a cleaning step.",
        ):
            _apply_step(table_id, "drop_duplicates", dupe_params)


def _impact_caption(frame, action: str, params: dict) -> None:
    impact = _preview_impact(frame, action, params)
    if impact:
        st.caption(impact)


# --------------------------------------------------------------------------------------
# Preview, log and metrics
# --------------------------------------------------------------------------------------


def _render_metrics(raw, cleaned) -> None:
    rows_col, columns_col, missing_col, dupes_col = st.columns(4)
    cell_count = cleaned.size
    missing_pct = round(int(cleaned.isna().sum().sum()) / cell_count * 100, 1) if cell_count else 0.0

    rows_col.metric(
        "Rows",
        f"{len(cleaned):,}",
        delta=f"{len(cleaned) - len(raw):,}" if len(cleaned) != len(raw) else None,
        help="Rows remaining after cleaning, against the raw file.",
        border=True,
    )
    columns_col.metric(
        "Columns",
        f"{len(cleaned.columns):,}",
        delta=f"{len(cleaned.columns) - len(raw.columns):,}" if len(cleaned.columns) != len(raw.columns) else None,
        help="Columns remaining after cleaning, against the raw file.",
        border=True,
    )
    missing_col.metric("Missing", f"{missing_pct}%", help="Share of cells that are blank.", border=True)
    dupes_col.metric(
        "Duplicate rows",
        f"{int(cleaned.duplicated().sum()):,}",
        help="Fully identical rows still present.",
        border=True,
    )


def _render_log(table: session.TableState, report) -> None:
    st.subheader("Cleaning log", divider="grey")

    if not table.steps:
        st.info("No cleaning steps yet. Apply an action on the left.", icon=":material/info:")
        return

    outcomes = {outcome.index: outcome for outcome in report}
    for index, line in enumerate(pipeline.describe_steps(table.steps)):
        text_col, remove_col = st.columns([9, 1], vertical_alignment="center")
        outcome = outcomes.get(index)
        prefix = "~~" if outcome is not None and outcome.status == "skipped" else ""
        text_col.markdown(f"{index + 1}. {prefix}{line}{prefix}")
        if outcome is not None and outcome.message:
            text_col.caption(outcome.message)
        remove_col.button(
            "",
            key=f"dc_remove_step_{table.table_id}_{index}",
            icon=":material/close:",
            help="Remove just this step and replay the rest.",
            on_click=_remove_step,
            args=(table.table_id, index),
        )

    if st.button(
        "Reset to raw",
        key=f"dc_reset_{table.table_id}",
        icon=":material/restart_alt:",
        type="primary",
        help="Discard every cleaning step for this table.",
    ):
        st.session_state[session.DC_RESET_DIALOG_KEY] = table.table_id
        st.rerun(scope="app")


@st.dialog("Reset this table?")
def _open_reset_dialog(table_id: str) -> None:
    table = session.get_table(table_id)
    if table is None:
        st.session_state.pop(session.DC_RESET_DIALOG_KEY, None)
        return

    st.warning(
        f"Discard all {len(table.steps)} cleaning step(s) for '{table.source_label}'? This cannot be undone.",
        icon=":material/warning:",
    )
    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        if st.button(
            "Confirm reset",
            key="dc_confirm_reset_button",
            icon=":material/restart_alt:",
            type="primary",
            help="Return this table to exactly what the file contained.",
        ):
            session.set_steps(table_id, [])
            st.session_state.pop(session.DC_RESET_DIALOG_KEY, None)
            st.rerun(scope="app")
    with cancel_col:
        if st.button(
            "Cancel", key="dc_cancel_reset_button", type="primary", help="Keep the cleaning steps."
        ):
            st.session_state.pop(session.DC_RESET_DIALOG_KEY, None)
            st.rerun(scope="app")


def _render_preview(table: session.TableState, cleaned, report) -> None:
    st.subheader("Preview", divider="grey")
    if len(cleaned) > session.PREVIEW_ROWS:
        st.caption(f"Showing the first {session.PREVIEW_ROWS:,} of {len(cleaned):,} rows.")
    st.dataframe(
        cleaned.head(session.PREVIEW_ROWS),
        key=f"dc_preview_{table.table_id}",
        width="stretch",
        hide_index=True,
    )

    with st.expander("Column details", icon=":material/analytics:"):
        st.dataframe(
            profiling.column_stats(cleaned),
            key=f"dc_stats_{table.table_id}",
            width="stretch",
            hide_index=True,
            column_config={
                "column": "Column",
                "detected_type": st.column_config.TextColumn("Detected type", help="Suggested from the values."),
                "non_null": "Filled",
                "missing": "Blank",
                "missing_pct": st.column_config.NumberColumn("Blank %", format="%.1f"),
                "unique": "Distinct",
                "sample_values": "Examples",
            },
        )

    sheet_name = st.text_input(
        "Output sheet name",
        value=table.output_sheet_name,
        key=f"dc_output_sheet_name_{table.table_id}",
        help="The worksheet name in the download. Sanitized to Excel's rules and de-duplicated automatically.",
    )
    if sheet_name != table.output_sheet_name:
        session.set_output_sheet_name(table.table_id, sheet_name)

    _render_log(table, report)


@st.fragment
def _render_table_tab(table: session.TableState, file_bytes: bytes) -> None:
    try:
        raw = session.raw_table(table, file_bytes)
        cleaned, report = session.cleaned_table(table, file_bytes)
    except DataCleanerError as error:
        logger.exception("Could not prepare table %s for display.", table.table_id)
        st.error(str(error))
        return

    if len(raw) > session.LARGE_TABLE_ROWS:
        st.warning(
            f"This table has {len(raw):,} rows. Cleaning stays responsive, but each action takes a moment.",
            icon=":material/hourglass:",
        )

    mangled = loaders.has_mangled_duplicate_columns(raw)
    if mangled:
        st.info(
            f"The source file repeats a column heading, so these were renamed to keep them distinct: "
            f"{', '.join(mangled)}.",
            icon=":material/info:",
        )

    _render_metrics(raw, cleaned)
    actions_col, preview_col = st.columns([2, 3])

    with actions_col:
        _render_structure_panel(table, cleaned)
        _render_types_panel(table, cleaned)
        _render_text_panel(table, cleaned)
        _render_missing_panel(table, cleaned)
        _render_duplicates_panel(table, cleaned)

    with preview_col:
        _render_preview(table, cleaned, report)


# --------------------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------------------


def _render_download(tables: list[session.TableState]) -> None:
    st.subheader("Download", divider="grey")
    sheet_names = session.export_sheet_names(tables)
    st.caption(f"One workbook, {len(tables)} sheet(s): {', '.join(sheet_names)}, plus a cleaning log.")

    try:
        workbook = session.build_download(tables, _uploaded_bytes())
    except DataCleanerError as error:
        logger.exception("Could not build the cleaned workbook.")
        st.error(str(error))
        return

    download_col, reset_col = st.columns(2)
    with download_col:
        st.download_button(
            "Download cleaned workbook",
            data=workbook,
            file_name="cleaned_data.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dc_download_button",
            icon=":material/download:",
            type="primary",
            on_click="ignore",
            help="One .xlsx containing every cleaned table and the cleaning log.",
        )
    with reset_col:
        if st.button(
            "Start over",
            key="dc_start_over_button",
            icon=":material/refresh:",
            type="primary",
            help="Discard every uploaded file and all cleaning steps.",
        ):
            session.queue_start_over()
            st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


if profile is not None:
    render_sidebar(profile)

    st.title("Data Cleaner")
    st.write(":blue[**Upload files, clean each table, and download one multi-sheet workbook.**]")

    loaded_tables = _render_upload()

    if not loaded_tables:
        st.info("Upload a CSV or Excel file to get started.", icon=":material/upload_file:")
    else:
        pending_reset = st.session_state.get(session.DC_RESET_DIALOG_KEY)
        if pending_reset:
            _open_reset_dialog(pending_reset)

        uploaded_bytes = _uploaded_bytes()
        tabs = st.tabs(session.tab_labels(loaded_tables), key=session.DC_TABS_KEY, on_change="rerun")
        for tab, loaded_table in zip(tabs, loaded_tables):
            # Only the visible tab's pipeline runs; without this gate every rerun would
            # re-derive every loaded table.
            if not tab.open:
                continue
            with tab:
                file_bytes = uploaded_bytes.get(loaded_table.file_id)
                if file_bytes is None:
                    st.error("This file is no longer available. Please upload it again.")
                    continue
                _render_table_tab(loaded_table, file_bytes)

        _render_download(loaded_tables)
