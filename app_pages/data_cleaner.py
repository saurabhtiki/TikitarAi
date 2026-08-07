"""Data Cleaner utility — upload one or more CSV/Excel files, clean each table, download
a single multi-sheet workbook.

Open to every logged-in role (requirements 2.2 grants Utilities to all three), so this
page follows `settings.py` and carries no `require_role` guard.

Cleaning actions are grouped into `st.segmented_control` bars, and picking one opens that
action's dialog. That keeps the tab body to a compact command bar plus a full-width
preview, and gives each action's inputs real room instead of a narrow column. The dialogs
are driven from a session-state flag rather than the `if st.button(...): _open_dialog()`
idiom, which breaks once a dialog holds widgets — interacting with one reruns the script,
the button reads False, and the dialog closes mid-edit.

Each table's tab body is an `st.fragment` and the tab set is gated on `tab.open`, so a
widget interaction costs one panel's rerun rather than re-running every loaded table's
pipeline.
"""

import logging

import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from cleaner import loaders, naming, pipeline, profiling, session
from cleaner.exceptions import DataCleanerError, InvalidStepError
from cleaner.steps import (
    AGGREGATION_FUNCTIONS,
    CASE_CHOICES,
    DEFAULT_KEEP_PATTERN,
    DEFAULT_VALUE_NAME,
    DEFAULT_VARIABLE_NAME,
    DUPLICATE_KEEP_CHOICES,
    FILL_STRATEGIES,
    MAX_PIVOT_COLUMNS,
    RESHAPE_ACTIONS,
    get_spec,
)
from engine import session as engine_session
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

RESET_ACTION = "reset"
DELETE_SUMMARY_ACTION = "delete_summary"

# The reshape actions, which save a *new* table instead of recording a step on this one.
# Kept as a page-level constant rather than read off `steps.RESHAPE_ACTIONS` at each use,
# so the dialog wiring below reads in one place.
SUMMARY_ACTIONS = RESHAPE_ACTIONS

# A reshape's preview grid is a sanity check, not the deliverable — the saved summary's
# own tab shows every row.
PREVIEW_ROWS_IN_DIALOG = 50

# Which pages can receive the cleaned tables, and where each one lives. Chat with Data
# is the only consumer so far (it's the only page with an adoption path into the Data
# Engine) — add an entry here when a future page grows one.
EXPORT_DESTINATIONS: dict[str, str] = {
    "Chat with Data": "app_pages/chat_with_data.py",
}

_FILL_LABELS = {
    "custom": "Fill with a value I choose",
    "previous": "Copy down from the row above",
    "next": "Copy up from the row below",
    "zero": "Fill with 0",
    "mean": "Fill with the mean",
    "median": "Fill with the median",
    "drop_rows": "Drop affected rows",
}

# Command bar layout: which actions appear under which heading, and the face each shows
# in its segmented control. Order here is the order people actually clean in.
COMMAND_GROUPS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    (
        "structure",
        "Structure",
        [
            ("skip_rows", "Skip rows", ":material/content_cut:"),
            ("remove_empty_rows", "Empty rows", ":material/delete_sweep:"),
            ("delete_columns", "Delete columns", ":material/delete:"),
            ("rename_columns", "Rename column", ":material/edit:"),
        ],
    ),
    (
        "values",
        "Values",
        [
            ("set_column_types", "Column types", ":material/category:"),
            ("fill_missing", "Missing values", ":material/water_drop:"),
            ("fix_numeric_text", "Numbers as text", ":material/functions:"),
        ],
    ),
    (
        "text",
        "Text",
        [
            ("trim_whitespace", "Trim whitespace", ":material/format_clear:"),
            ("remove_special_characters", "Special characters", ":material/cleaning_services:"),
            ("change_case", "Letter case", ":material/match_case:"),
            ("find_replace", "Find & replace", ":material/find_replace:"),
        ],
    ),
    (
        "rows",
        "Rows",
        [
            ("drop_duplicates", "Duplicates", ":material/filter_alt:"),
            (RESET_ACTION, "Reset to raw", ":material/restart_alt:"),
        ],
    ),
    (
        "summarise",
        "Summarise",
        [
            ("group_summarise", "Group & total", ":material/functions:"),
            ("pivot", "Pivot", ":material/pivot_table_chart:"),
            ("unpivot", "Unpivot", ":material/table_rows:"),
        ],
    ),
]

# Segmented-control option faces, flattened once so `format_func` is a plain lookup.
# `.get` rather than `[...]`: an option face is presentation, and a missing one should
# degrade to the raw action name rather than break the command bar.
COMMAND_FACES = {
    action: f"{icon} {label}" for _, _, commands in COMMAND_GROUPS for action, label, icon in commands
}

COMMAND_GROUP_HELP = {
    "structure": "Change which rows and columns exist, and what they're called.",
    "values": "Decide how values are read, and what to do about blanks.",
    "text": "Tidy the contents of text columns.",
    "rows": "Work on whole rows, or start this table again.",
    "summarise": "Reshape this table into a new one — totals, a pivot, or long form.",
}

DIALOG_TITLES = {
    "skip_rows": "Skip rows",
    "remove_empty_rows": "Remove empty rows",
    "delete_columns": "Delete columns",
    "rename_columns": "Rename a column",
    "set_column_types": "Set column types",
    "fill_missing": "Handle missing values",
    "fix_numeric_text": "Fix numbers stored as text",
    "trim_whitespace": "Trim whitespace",
    "remove_special_characters": "Remove special characters",
    "change_case": "Change letter case",
    "find_replace": "Find & replace",
    "drop_duplicates": "Remove duplicate rows",
    "group_summarise": "Group & total",
    "pivot": "Pivot",
    "unpivot": "Unpivot",
    RESET_ACTION: "Reset this table?",
    DELETE_SUMMARY_ACTION: "Delete this summary?",
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


def _uploaded_bytes() -> dict[str, bytes]:
    uploads = st.session_state.get(session.DC_UPLOADER_KEY) or []
    return {upload.file_id: upload.getvalue() for upload in uploads}


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


def _apply_step(table_id: str, action: str, params: dict, *, to_all_tables: bool = False) -> None:
    """Validates and records a step, closes the dialog, and reruns.

    Validation runs against the columns the step will actually see, so an invalid step is
    rejected before it can enter the recipe — that is what keeps a stored recipe
    well-formed for Stage 7 to serialize later.
    """
    if action in SUMMARY_ACTIONS:
        # A reshape changes the table's grain, so recording it here would silently replace
        # the table someone is cleaning. `_save_summary` is the only route for these.
        logger.error("Refused to record the reshape '%s' as a cleaning step on table %s.", action, table_id)
        st.error("That action creates a new table rather than changing this one.")
        return

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
                # Left open deliberately, so the user can correct the input in place.
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

    session.close_dialog()
    st.rerun(scope="app")


def _remove_step(table_id: str, index: int) -> None:
    """Drops one step and replays the rest.

    Called from the fragment body on the button's return value, not as an `on_click`
    callback. `st.rerun` is a no-op inside a callback, and inside a *fragment's* callback
    Streamlit reports that by writing a warning element, which then lands at the top of
    the app instead of in this tab. Reading the button's return value keeps the rerun in
    the body, where app scope actually works.
    """
    table = session.get_table(table_id)
    if table is None:
        return
    try:
        session.set_steps(table_id, pipeline.remove_step(table.steps, index))
    except IndexError:
        logger.warning("Tried to remove step %s from table %s, which no longer has it.", index, table_id)
        return
    st.rerun(scope="app")


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
# Dialog bodies — one per action, each rendering its inputs then the shared footer
# --------------------------------------------------------------------------------------


def _impact_caption(frame, action: str, params: dict) -> None:
    impact = _preview_impact(frame, action, params)
    if impact:
        st.caption(impact)


def _footer(table_id: str, action: str, params: dict, *, to_all_tables: bool = False, disabled: bool = False) -> None:
    """The Apply/Cancel pair every action dialog ends with."""
    apply_column, cancel_column = st.columns(2)
    with apply_column:
        if st.button(
            "Apply",
            key=f"dc_apply_{action}_{table_id}",
            icon=":material/check:",
            type="primary",
            help="Record this as a cleaning step.",
            disabled=disabled,
            width="stretch",
        ):
            _apply_step(table_id, action, params, to_all_tables=to_all_tables)
    with cancel_column:
        if st.button(
            "Cancel",
            key=f"dc_cancel_{action}_{table_id}",
            help="Close without changing anything.",
            width="stretch",
        ):
            session.close_dialog()
            st.rerun(scope="app")


def _apply_to_all_tables(action: str, table_id: str) -> bool:
    if len(session.get_tables()) < 2:
        return False
    return st.checkbox(
        "Apply to all loaded tables",
        key=f"dc_{action}_all_{table_id}",
        help="Run this on every table you've uploaded, not just this one.",
    )


def _dialog_skip_rows(table, frame) -> None:
    top = st.number_input(
        "Skip rows from the top",
        min_value=0,
        value=0,
        step=1,
        key=f"dc_skip_top_{table.table_id}",
        help="Junk rows above the real header, such as a report title.",
    )
    bottom = st.number_input(
        "Skip rows from the bottom",
        min_value=0,
        value=0,
        step=1,
        key=f"dc_skip_bottom_{table.table_id}",
        help="Trailing rows such as totals or footnotes.",
    )
    promote_header = st.checkbox(
        "Use the first remaining row as the header",
        value=True,
        key=f"dc_promote_header_{table.table_id}",
        help="Almost always what you want — otherwise the real header stays a data row.",
    )
    params = {"top": int(top), "bottom": int(bottom), "promote_header": promote_header}
    _impact_caption(frame, "skip_rows", params)
    if promote_header and (top or bottom):
        st.info(
            "This renames every column, so any earlier step targeting a column by name may be skipped.",
            icon=":material/info:",
        )
    _footer(table.table_id, "skip_rows", params)


def _dialog_remove_empty_rows(table, frame) -> None:
    scope = st.multiselect(
        "Remove rows that are blank across these columns",
        options=list(frame.columns),
        key=f"dc_empty_columns_{table.table_id}",
        help="Leave empty to remove only rows that are blank in every column.",
    )
    params = {"columns": scope or None, "blank_strings_count_as_empty": True}
    _impact_caption(frame, "remove_empty_rows", params)
    _footer(table.table_id, "remove_empty_rows", params)


def _dialog_delete_columns(table, frame) -> None:
    columns = st.multiselect(
        "Columns to delete",
        options=list(frame.columns),
        key=f"dc_delete_columns_{table.table_id}",
        help="Remove columns you don't need in the output.",
    )
    params = {"columns": columns}
    if columns:
        _impact_caption(frame, "delete_columns", params)
    _footer(table.table_id, "delete_columns", params, disabled=not columns)


def _dialog_rename_columns(table, frame) -> None:
    source = st.selectbox(
        "Column to rename",
        options=list(frame.columns),
        key=f"dc_rename_source_{table.table_id}",
        help="Pick the column whose name you want to change.",
    )
    target = st.text_input(
        "New name",
        key=f"dc_rename_target_{table.table_id}",
        help="Duplicate names are blocked — every column must stay uniquely addressable.",
    )
    params = {"renames": {source: target.strip()}} if source and target.strip() else {"renames": {}}
    _footer(table.table_id, "rename_columns", params, disabled=not (source and target.strip()))


def _dialog_set_column_types(table, frame) -> None:
    st.caption("Detected types are applied automatically on upload. Override any of them here.")
    columns = st.multiselect(
        "Columns to retype",
        options=list(frame.columns),
        key=f"dc_type_columns_{table.table_id}",
        help="Choose one or more columns, then the type they should be.",
    )
    target_type = st.selectbox(
        "Set them to",
        options=profiling.COLUMN_TYPES,
        key=f"dc_type_target_{table.table_id}",
        help="'id' keeps values as text so leading zeros survive; 'categorical' marks a low-cardinality label column.",
    )

    settings: dict = {"target_type": target_type}
    if target_type == profiling.NUMERIC:
        settings["decimal_separator"] = st.selectbox(
            "Decimal separator",
            options=[".", ","],
            key=f"dc_type_decimal_{table.table_id}",
            help="Never guessed: '1.200' is twelve hundred in some locales and 1.2 in others.",
        )
    elif target_type == profiling.DATE:
        date_format = st.text_input(
            "Date format (optional)",
            key=f"dc_type_date_format_{table.table_id}",
            help="Leave blank to detect automatically, or give a format such as %d/%m/%Y.",
        )
        settings["date_format"] = date_format.strip() or None

    params = {"by_column": {column: settings for column in columns}}
    if columns:
        _impact_caption(frame, "set_column_types", params)
    _footer(table.table_id, "set_column_types", params, disabled=not columns)


def _dialog_fill_missing(table, frame) -> None:
    st.caption("A cell holding only spaces counts as blank here, the way it looks on screen.")
    columns = st.multiselect(
        "Columns",
        options=list(frame.columns),
        key=f"dc_fill_columns_{table.table_id}",
        help="Choose the columns whose blanks you want to handle.",
    )
    strategy = st.selectbox(
        "Strategy",
        options=FILL_STRATEGIES,
        format_func=lambda choice: _FILL_LABELS[choice],
        key=f"dc_fill_strategy_{table.table_id}",
        help=(
            "Mean and median need a numeric column — set the type first if needed. "
            "Copy down and copy up follow the current row order, so apply row-changing "
            "steps before this one."
        ),
    )

    settings: dict = {"strategy": strategy}
    if strategy == "custom":
        settings["value"] = st.text_input(
            "Value to fill blanks with",
            value="Unknown",
            key=f"dc_fill_value_{table.table_id}",
            help="Any text. A numeric column stays numeric if what you type is a number.",
        ).strip()

    params = {"by_column": {column: settings for column in columns}}
    incomplete = strategy == "custom" and not settings.get("value")
    if columns and not incomplete:
        _impact_caption(frame, "fill_missing", params)
    _footer(table.table_id, "fill_missing", params, disabled=not columns or incomplete)


def _dialog_fix_numeric_text(table, frame) -> None:
    columns = st.multiselect(
        "Columns to convert",
        options=profiling.text_columns(frame),
        key=f"dc_fixnum_columns_{table.table_id}",
        help="Strips currency symbols and thousands separators, and reads (300) as -300.",
    )
    decimal_separator = st.selectbox(
        "Decimal separator",
        options=[".", ","],
        key=f"dc_fixnum_decimal_{table.table_id}",
        help="Never guessed — pick the one your source file uses.",
    )
    params = {
        "columns": columns,
        "decimal_separator": decimal_separator,
        "parentheses_are_negative": True,
    }
    if columns:
        _impact_caption(frame, "fix_numeric_text", params)
    _footer(table.table_id, "fix_numeric_text", params, disabled=not columns)


def _dialog_trim_whitespace(table, frame) -> None:
    collapse = st.checkbox(
        "Also collapse repeated spaces inside values",
        value=True,
        key=f"dc_trim_collapse_{table.table_id}",
        help="Turns 'New   York' into 'New York'. Non-breaking and zero-width characters are always removed.",
    )
    params = {"collapse_internal": collapse}
    _impact_caption(frame, "trim_whitespace", params)
    to_all = _apply_to_all_tables("trim", table.table_id)
    _footer(table.table_id, "trim_whitespace", params, to_all_tables=to_all)


def _dialog_remove_special_characters(table, frame) -> None:
    keep_pattern = st.text_input(
        "Characters to keep",
        value=DEFAULT_KEEP_PATTERN,
        key=f"dc_keep_pattern_{table.table_id}",
        help="A regular-expression character set. Anything outside it is removed from text columns.",
    )
    replacement = st.text_input(
        "Replace removed characters with",
        key=f"dc_special_replacement_{table.table_id}",
        help="Leave blank to delete them outright.",
    )
    params = {"keep_pattern": keep_pattern, "replacement": replacement}
    _impact_caption(frame, "remove_special_characters", params)
    to_all = _apply_to_all_tables("special", table.table_id)
    _footer(table.table_id, "remove_special_characters", params, to_all_tables=to_all)


def _dialog_change_case(table, frame) -> None:
    columns = st.multiselect(
        "Change letter case in",
        options=profiling.text_columns(frame),
        key=f"dc_case_columns_{table.table_id}",
        help="Only text columns can have their case changed.",
    )
    choice = st.selectbox(
        "Case",
        options=CASE_CHOICES,
        key=f"dc_case_choice_{table.table_id}",
        help="UPPER, lower, or Title Case.",
    )
    params = {"by_column": {column: choice for column in columns}}
    if columns:
        _impact_caption(frame, "change_case", params)
    _footer(table.table_id, "change_case", params, disabled=not columns)


def _dialog_find_replace(table, frame) -> None:
    columns = st.multiselect(
        "In columns",
        options=profiling.text_columns(frame),
        key=f"dc_fr_columns_{table.table_id}",
        help="Find & replace only reaches text columns — set a column back to text first if you need it here.",
    )
    find = st.text_input("Find", key=f"dc_fr_find_{table.table_id}", help="The text or pattern to search for.")
    replace = st.text_input(
        "Replace with", key=f"dc_fr_replace_{table.table_id}", help="Leave blank to delete what was found."
    )
    regex_column, case_column = st.columns(2)
    with regex_column:
        use_regex = st.checkbox(
            "Treat as a regular expression",
            key=f"dc_fr_regex_{table.table_id}",
            help="Enables patterns such as ^N\\.Y\\.$ and capture groups in the replacement.",
        )
    with case_column:
        case_sensitive = st.checkbox(
            "Match case",
            value=True,
            key=f"dc_fr_case_{table.table_id}",
            help="Uncheck to match regardless of capitalisation.",
        )

    params = {
        "columns": columns,
        "find": find,
        "replace": replace,
        "regex": use_regex,
        "case_sensitive": case_sensitive,
    }
    if columns and find:
        _impact_caption(frame, "find_replace", params)
    _footer(table.table_id, "find_replace", params, disabled=not (columns and find))


def _dialog_drop_duplicates(table, frame) -> None:
    subset = st.multiselect(
        "A row is a duplicate when these columns match",
        options=list(frame.columns),
        key=f"dc_dupe_columns_{table.table_id}",
        help="Leave empty to require every column to match.",
    )
    keep = st.selectbox(
        "Keep",
        options=DUPLICATE_KEEP_CHOICES,
        key=f"dc_dupe_keep_{table.table_id}",
        help="Which of each duplicate group to keep.",
    )
    params = {"columns": subset or None, "keep": keep}
    _impact_caption(frame, "drop_duplicates", params)
    _footer(table.table_id, "drop_duplicates", params)


def _dialog_reset(table, frame) -> None:
    st.warning(
        f"Discard all {len(table.steps)} cleaning step(s) for '{table.source_label}'? This cannot be undone.",
        icon=":material/warning:",
    )
    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Confirm reset",
            key="dc_confirm_reset_button",
            icon=":material/restart_alt:",
            type="primary",
            help="Return this table to exactly what the file contained.",
            width="stretch",
        ):
            session.set_steps(table.table_id, [])
            session.close_dialog()
            st.rerun(scope="app")
    with cancel_column:
        if st.button(
            "Cancel", key="dc_cancel_reset_button", help="Keep the cleaning steps.", width="stretch"
        ):
            session.close_dialog()
            st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Summary dialogs — the three that save a new table instead of recording a step
# --------------------------------------------------------------------------------------


def _saved_params(table: session.TableState, action: str) -> dict:
    """The reshape's stored parameters, when this dialog was opened to edit one."""
    if table.reshape is None or table.reshape.get("action") != action:
        return {}
    return table.reshape.get("params", {})


def _keep_options(saved: list, options: list[str]) -> list:
    """Filters a saved multiselect value down to what still exists.

    A saved column may have been renamed or deleted by later cleaning; handing Streamlit a
    default that isn't in `options` raises rather than degrading.
    """
    return [value for value in saved if value in options]


def _option_index(saved, options: list, fallback: int = 0) -> int:
    return options.index(saved) if saved in options else fallback


def _still_an_option(value, options: list):
    """Corrects a selectbox value that its own options no longer contain.

    `st.multiselect` filters stale selections out itself when its options change, but
    `st.selectbox` hands back the stored value verbatim. These pickers narrow each other —
    choosing a column for the rows removes it from the columns picker — so without this a
    stale pick would flow into the params and be rejected by the validator instead of just
    moving on.
    """
    if value in options:
        return value
    return options[0] if options else None


def _render_reshape_preview(table: session.TableState, action: str, frame, params: dict):
    """Runs the reshape and shows the result, returning it so the footer can gate on it.

    A live grid rather than the `_impact_caption` sentence the cleaning dialogs use: "this
    removes 40 rows" says nothing useful about a pivot, whereas seeing the shape is the
    whole decision.
    """
    try:
        spec = get_spec(action)
        spec.validate(params, list(frame.columns))
        result, warnings_out = spec.apply(frame, params)
    except InvalidStepError as error:
        st.info(str(error), icon=":material/info:")
        return None
    except (ValueError, TypeError, KeyError) as error:
        logger.exception("Could not preview the '%s' reshape.", action)
        st.error(f"This couldn't be worked out from the current data: {error}")
        return None

    for warning in warnings_out:
        st.warning(warning, icon=":material/error:")

    st.caption(f"Preview — {len(result):,} row(s) × {len(result.columns):,} column(s).")
    st.dataframe(
        result.head(PREVIEW_ROWS_IN_DIALOG),
        key=f"dc_reshape_preview_{action}_{table.table_id}",
        width="stretch",
        hide_index=True,
    )
    return result


def _save_summary(table: session.TableState, action: str, params: dict, result) -> None:
    """The Save/Cancel footer for a reshape, and the save itself.

    Editing writes the reshape back onto the summary rather than creating a second table,
    so a summary keeps its identity — and with it its worksheet name and its place in the
    tab strip — across edits.
    """
    editing = table.derived_from is not None
    too_wide = result is not None and len(result.columns) > MAX_PIVOT_COLUMNS
    if too_wide:
        st.error(
            f"This produces {len(result.columns):,} columns, past the {MAX_PIVOT_COLUMNS:,} limit. "
            f"Choose a column with fewer distinct values."
        )

    # Only when creating. An existing summary is renamed through the "Output sheet name"
    # box on its own tab, and offering a second field here would let the two disagree —
    # that box holds the pre-edit text in its own widget state and would write it straight
    # back over anything typed in the dialog.
    name = table.output_sheet_name
    if not editing:
        name = st.text_input(
            "Save as",
            value=f"{table.output_sheet_name} summary",
            key=f"dc_summary_name_{action}_{table.table_id}",
            help="Names the new tab, the worksheet in the download, and the table in Chat with Data.",
        ).strip()

    save_column, cancel_column = st.columns(2)
    with save_column:
        if st.button(
            "Save summary",
            key=f"dc_save_summary_{action}_{table.table_id}",
            icon=":material/save:",
            type="primary",
            help="Add this as a new table alongside the cleaned ones.",
            disabled=result is None or too_wide or not name,
            width="stretch",
        ):
            step = pipeline.make_step(action, params)
            if editing:
                session.set_reshape(table.table_id, step)
                session.set_output_sheet_name(table.table_id, name)
            elif session.add_summary_table(table.table_id, step, name) is None:
                st.error("That table is no longer loaded, so the summary couldn't be saved.")
                return
            session.close_dialog()
            st.rerun(scope="app")
    with cancel_column:
        if st.button(
            "Cancel",
            key=f"dc_cancel_summary_{action}_{table.table_id}",
            help="Close without saving anything.",
            width="stretch",
        ):
            session.close_dialog()
            st.rerun(scope="app")


def _dialog_group_summarise(table, frame) -> None:
    saved = _saved_params(table, "group_summarise")
    all_columns = list(frame.columns)

    group_by = st.multiselect(
        "Group by",
        options=all_columns,
        default=_keep_options(saved.get("group_by") or [], all_columns),
        key=f"dc_summarise_groupby_{table.table_id}",
        help="One row comes back per combination of these. Leave empty for a single totals row.",
    )
    value_options = [column for column in all_columns if column not in group_by]
    numeric = set(profiling.numeric_columns(frame))
    saved_values = [aggregation.get("column") for aggregation in saved.get("aggregations") or []]

    columns = st.multiselect(
        "Total up",
        options=value_options,
        default=_keep_options(list(dict.fromkeys(saved_values)), value_options),
        format_func=lambda column: column if column in numeric else f"{column} (text)",
        key=f"dc_summarise_values_{table.table_id}",
        help="Sum, average and median need a numeric column — the others work on text too.",
    )
    functions = st.multiselect(
        "Using",
        options=AGGREGATION_FUNCTIONS,
        default=list(dict.fromkeys(aggregation.get("function") for aggregation in saved.get("aggregations") or []))
        or ["sum"],
        key=f"dc_summarise_functions_{table.table_id}",
        help="Pick more than one to get a column per function, such as both a sum and a count.",
    )

    params = {
        "group_by": group_by,
        "aggregations": [{"column": column, "function": function} for column in columns for function in functions],
    }
    result = _render_reshape_preview(table, "group_summarise", frame, params) if columns and functions else None
    _save_summary(table, "group_summarise", params, result)


def _dialog_pivot(table, frame) -> None:
    saved = _saved_params(table, "pivot")
    all_columns = list(frame.columns)

    index = st.multiselect(
        "Rows (down the side)",
        options=all_columns,
        default=_keep_options(saved.get("index") or [], all_columns),
        key=f"dc_pivot_index_{table.table_id}",
        help="The columns that stay as rows, one row per combination.",
    )
    across_options = [column for column in all_columns if column not in index]
    across = _still_an_option(
        st.selectbox(
            "Columns (across the top)",
            options=across_options,
            index=_option_index(saved.get("columns"), across_options) if across_options else None,
            key=f"dc_pivot_columns_{table.table_id}",
            help="Each distinct value here becomes its own column. Best on something with few values.",
        ),
        across_options,
    )
    value_options = [column for column in across_options if column != across]
    values = _still_an_option(
        st.selectbox(
            "Values",
            options=value_options,
            index=_option_index(saved.get("values"), value_options) if value_options else None,
            key=f"dc_pivot_values_{table.table_id}",
            help="The column filling the grid.",
        ),
        value_options,
    )
    function_column, fill_column = st.columns(2)
    with function_column:
        function = st.selectbox(
            "Using",
            options=AGGREGATION_FUNCTIONS,
            index=_option_index(saved.get("function"), AGGREGATION_FUNCTIONS),
            key=f"dc_pivot_function_{table.table_id}",
            help="How to combine rows that land in the same cell.",
        )
    with fill_column:
        fill_value = st.text_input(
            "Fill empty cells with",
            value=str(saved.get("fill_value") or ""),
            key=f"dc_pivot_fill_{table.table_id}",
            help="Leave blank to leave empty cells empty. Type 0 to show gaps as zeroes.",
        ).strip()

    params = {
        "index": index,
        "columns": across,
        "values": values,
        "function": function,
        "fill_value": fill_value or None,
    }
    result = _render_reshape_preview(table, "pivot", frame, params) if index and across and values else None
    _save_summary(table, "pivot", params, result)


def _dialog_unpivot(table, frame) -> None:
    saved = _saved_params(table, "unpivot")
    all_columns = list(frame.columns)

    id_columns = st.multiselect(
        "Keep these columns as-is",
        options=all_columns,
        default=_keep_options(saved.get("id_columns") or [], all_columns),
        key=f"dc_unpivot_ids_{table.table_id}",
        help="The columns that identify each row, such as a region or a date.",
    )
    stack_options = [column for column in all_columns if column not in id_columns]
    value_columns = st.multiselect(
        "Stack these columns into rows",
        options=stack_options,
        default=_keep_options(saved.get("value_columns") or [], stack_options),
        key=f"dc_unpivot_values_{table.table_id}",
        help="Leave empty to stack every column you aren't keeping.",
    )
    variable_column, value_column = st.columns(2)
    with variable_column:
        variable_name = st.text_input(
            "Name the column of headings",
            value=saved.get("variable_name") or DEFAULT_VARIABLE_NAME,
            key=f"dc_unpivot_varname_{table.table_id}",
            help="Holds the old column headings, one per row.",
        ).strip()
    with value_column:
        value_name = st.text_input(
            "Name the column of values",
            value=saved.get("value_name") or DEFAULT_VALUE_NAME,
            key=f"dc_unpivot_valuename_{table.table_id}",
            help="Holds what was in each of those columns.",
        ).strip()

    params = {
        "id_columns": id_columns,
        "value_columns": value_columns,
        "variable_name": variable_name or DEFAULT_VARIABLE_NAME,
        "value_name": value_name or DEFAULT_VALUE_NAME,
    }
    result = _render_reshape_preview(table, "unpivot", frame, params) if id_columns else None
    _save_summary(table, "unpivot", params, result)


def _dialog_delete_summary(table, frame) -> None:
    st.warning(
        f"Delete the summary '{table.source_label}'? This cannot be undone, though you can build it again.",
        icon=":material/warning:",
    )
    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Delete summary",
            key=f"dc_confirm_delete_summary_{table.table_id}",
            icon=":material/delete:",
            type="primary",
            help="Remove this summary from the tabs, the download and the export.",
            width="stretch",
        ):
            session.remove_table(table.table_id)
            session.close_dialog()
            st.rerun(scope="app")
    with cancel_column:
        if st.button(
            "Cancel",
            key=f"dc_cancel_delete_summary_{table.table_id}",
            help="Keep this summary.",
            width="stretch",
        ):
            session.close_dialog()
            st.rerun(scope="app")


DIALOG_BODIES = {
    "skip_rows": _dialog_skip_rows,
    "remove_empty_rows": _dialog_remove_empty_rows,
    "delete_columns": _dialog_delete_columns,
    "rename_columns": _dialog_rename_columns,
    "set_column_types": _dialog_set_column_types,
    "fill_missing": _dialog_fill_missing,
    "fix_numeric_text": _dialog_fix_numeric_text,
    "trim_whitespace": _dialog_trim_whitespace,
    "remove_special_characters": _dialog_remove_special_characters,
    "change_case": _dialog_change_case,
    "find_replace": _dialog_find_replace,
    "drop_duplicates": _dialog_drop_duplicates,
    "group_summarise": _dialog_group_summarise,
    "pivot": _dialog_pivot,
    "unpivot": _dialog_unpivot,
    RESET_ACTION: _dialog_reset,
    DELETE_SUMMARY_ACTION: _dialog_delete_summary,
}


def _build_dialog(action: str, title: str):
    """Wraps one action's body in an `st.dialog` at import time.

    `st.dialog` takes its title when the decorator runs, so each action needs its own
    decorated function rather than one generic dialog — a shared title would leave every
    dialog headed "Cleaning action".
    """

    @st.dialog(title, width="large", on_dismiss=session.close_dialog)
    def _dialog(table, frame) -> None:
        DIALOG_BODIES[action](table, frame)

    return _dialog


DIALOGS = {action: _build_dialog(action, title) for action, title in DIALOG_TITLES.items()}


def _render_pending_dialog(table: session.TableState, frame) -> None:
    """Opens whichever action dialog is flagged for this table.

    Rendered inside the tab's fragment, alongside the command buttons that open it.
    Putting it at the page's top level instead would silently never appear: a button
    click inside a fragment reruns only that fragment, so top-level code never
    re-executes to notice the flag.
    """
    pending = session.pending_dialog()
    if pending is None:
        return

    table_id, action = pending
    if table_id != table.table_id:
        return
    if action not in DIALOGS:
        session.close_dialog()
        return

    DIALOGS[action](table, frame)


# --------------------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------------------


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
# Command bar, metrics, preview and log
# --------------------------------------------------------------------------------------


def _on_command_pick(table_id: str, widget_key: str) -> None:
    """Opens the dialog for whichever command was just picked, then clears the selection.

    Clearing matters: a segmented control fires `on_change` only when its value actually
    changes, so leaving the last pick selected would make re-opening the same dialog a
    dead click. Writing a widget's own key is legal here because callbacks run before the
    script re-executes — the same rule that makes the deferred start-over reset necessary
    elsewhere on this page.
    """
    action = st.session_state.get(widget_key)
    if not action:
        return
    st.session_state[widget_key] = None
    session.open_dialog(table_id, action)


def _render_command_bar(table: session.TableState) -> None:
    for group_key, group_name, commands in COMMAND_GROUPS:
        # Reset is offered only once there is something to discard. Omitted rather than
        # disabled because a segmented control disables as a whole, not per option.
        actions = [action for action, _, _ in commands if action != RESET_ACTION or table.steps]
        if not actions:
            continue

        widget_key = f"dc_cmd_{group_key}_{table.table_id}"
        st.segmented_control(
            group_name,
            options=actions,
            format_func=lambda action: COMMAND_FACES.get(action, action),
            key=widget_key,
            help=COMMAND_GROUP_HELP[group_key],
            on_change=_on_command_pick,
            args=(table.table_id, widget_key),
        )


def _render_metrics(raw, cleaned) -> None:
    rows_col, columns_col, missing_col, dupes_col = st.columns(4)
    cell_count = cleaned.size
    # Counted with `blank_mask`, so this agrees with the column panel and with what the
    # fill step will actually reach — a cell holding only spaces is blank in all three.
    missing_pct = round(profiling.blank_count(cleaned) / cell_count * 100, 1) if cell_count else 0.0

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
        st.info("No cleaning steps yet. Pick an action above.", icon=":material/info:")
        return

    outcomes = {outcome.index: outcome for outcome in report}
    for index, line in enumerate(pipeline.describe_steps(table.steps)):
        text_col, remove_col = st.columns([9, 1], vertical_alignment="center")
        outcome = outcomes.get(index)
        prefix = "~~" if outcome is not None and outcome.status == "skipped" else ""
        text_col.markdown(f"{index + 1}. {prefix}{line}{prefix}")
        if outcome is not None and outcome.message:
            text_col.caption(outcome.message)
        if remove_col.button(
            "",
            key=f"dc_remove_step_{table.table_id}_{index}",
            icon=":material/close:",
            help="Remove just this step and replay the rest.",
        ):
            _remove_step(table.table_id, index)


def _export_name(table: session.TableState) -> str:
    """The worksheet name this table will actually land under in the download.

    Resolved against every loaded table rather than this one alone, because
    de-duplication is what finally decides the name — two tables both called `Sales`
    export as `Sales` and `Sales_2`, and the second one's owner should see that.
    """
    tables = list(session.get_tables().values())
    resolved = dict(zip((loaded.table_id for loaded in tables), session.export_sheet_names(tables)))
    return resolved.get(table.table_id, table.output_sheet_name)


def _render_preview(table: session.TableState, cleaned) -> None:
    st.subheader("Preview", divider="grey")
    #if len(cleaned) > session.PREVIEW_ROWS:
        #st.caption(f"Showing the first {session.PREVIEW_ROWS:,} of {len(cleaned):,} rows.")
    st.dataframe(
        cleaned,
        key=f"dc_preview_{table.table_id}",
        width="stretch",
        hide_index=True,
    )

    with st.expander("Column details", icon=":material/analytics:"):
        st.dataframe(
            profiling.column_stats(cleaned, declared_types=pipeline.declared_column_types(table.steps)),
            key=f"dc_stats_{table.table_id}",
            width="stretch",
            hide_index=True,
            column_config={
                "column": "Column",
                "column_type": st.column_config.TextColumn(
                    "Type",
                    help="The type you set for this column, or — where you haven't set one — a guess from its values.",
                ),
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
    # Compared against the *sanitized* name, not the raw input: sanitizing is what gets
    # stored, so comparing raw text would see a difference on every rerun and loop.
    if naming.sanitize_sheet_name(sheet_name) != table.output_sheet_name:
        session.set_output_sheet_name(table.table_id, sheet_name)
        # App-scoped, because the download button is built at page level outside this
        # fragment. A fragment-scoped rerun would leave the already-materialized workbook
        # carrying the old sheet name — st.download_button has to have its bytes ready
        # before the click, so a stale build is a silently wrong file.
        st.rerun(scope="app")

    final_name = _export_name(table)
    if final_name != sheet_name:
        st.caption(f"Saved as worksheet '{final_name}'.")


@st.fragment
def _render_summary_tab(table: session.TableState, file_bytes: bytes) -> None:
    """A saved summary's tab: what it is, what it produced, and how to change it.

    Deliberately without the cleaning command bar. A summary's recipe is its parent's
    steps plus one reshape, resolved live by `session.effective_steps`; letting it carry
    steps of its own would make "reset this table" and the per-step undo ambiguous about
    which half they act on. Clean the source instead and the summary follows.
    """
    parent = session.get_table(table.derived_from) if table.derived_from else None
    if parent is None or table.reshape is None:
        st.error("The table this summary came from is no longer loaded.")
        return

    try:
        parent_frame, _ = session.cleaned_table(parent, file_bytes)
        summary_frame, _ = session.cleaned_table(table, file_bytes)
    except DataCleanerError as error:
        logger.exception("Could not prepare summary %s for display.", table.table_id)
        st.error(str(error))
        return

    st.caption(f"From **{parent.source_label}** — {pipeline.describe_step(table.reshape)}.")
    _render_metrics(parent_frame, summary_frame)

    edit_column, delete_column, _ = st.columns([1, 1, 3])
    with edit_column:
        st.button(
            "Edit summary",
            key=f"dc_edit_summary_{table.table_id}",
            icon=":material/edit:",
            help="Change the columns or the function, keeping this tab and its name.",
            on_click=session.open_dialog,
            args=(table.table_id, table.reshape["action"]),
            width="stretch",
        )
    with delete_column:
        st.button(
            "Delete summary",
            key=f"dc_delete_summary_{table.table_id}",
            icon=":material/delete:",
            help="Remove this summary. The table it came from is untouched.",
            on_click=session.open_dialog,
            args=(table.table_id, DELETE_SUMMARY_ACTION),
            width="stretch",
        )

    # Options come from the parent's frame, since that is what the reshape runs against.
    _render_pending_dialog(table, parent_frame)
    _render_preview(table, summary_frame)


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
    _render_command_bar(table)
    _render_pending_dialog(table, cleaned)

    preview_column, log_column = st.columns([3, 2])
    with preview_column:
        _render_preview(table, cleaned)
    with log_column:
        _render_log(table, report)


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

    download_col, export_col, reset_col = st.columns(3)
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
    with export_col:
        _render_export_menu()
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


def _render_export_menu() -> None:
    """Sends the cleaned tables straight into another page's Data Engine and jumps there.

    Adoption happens on this page, where the uploaded bytes are guaranteed to still be
    cached (see `cleaner.session.cached_file_bytes`), rather than on the destination
    page — so it can never hit the "no longer available" case that a cross-page read of
    the uploader itself would. `adopt_cleaner_tables` reports any per-table failure as a
    warning rather than raising, so a bad table is skipped and the rest still switch.
    """
    destination = st.menu_button(
        "Export to",
        options=list(EXPORT_DESTINATIONS),
        key="dc_export_menu",
        icon=":material/output:",
        type="primary",
        help="Load these cleaned tables into another page's Data Engine, then jump there.",
    )
    if destination is None:
        return

    adopted, warnings = engine_session.adopt_cleaner_tables()
    for warning in warnings:
        st.warning(warning, icon=":material/error:")
    if not adopted:
        return

    engine_session.refresh_dictionary()
    st.switch_page(EXPORT_DESTINATIONS[destination])


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


if profile is not None:
    render_sidebar(profile)

    st.subheader("🧹Data Cleaner")
    st.write(":blue[**Upload files, clean each table, and download one multi-sheet workbook.**]")

    loaded_tables = _render_upload()

    if not loaded_tables:
        st.info("Upload a CSV or Excel file to get started.", icon=":material/upload_file:")
    else:
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
                if loaded_table.derived_from is not None:
                    _render_summary_tab(loaded_table, file_bytes)
                else:
                    _render_table_tab(loaded_table, file_bytes)

        _render_download(loaded_tables)
