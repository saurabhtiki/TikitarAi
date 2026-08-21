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

Above the uploader sits the **cleaning template** bar: a saved working set — every expected
file with its steps, plus the summary tables saved off them — picked by name. A bar rather
than Task Builder's gate, and one that only ever records intent; see the "Cleaning
templates" section below for both reasons, the second of which is load-bearing.
"""

import logging

import streamlit as st

from app_pages import saved_picker
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from cleaner import db as cleaner_db
from cleaner import display, loaders, naming, pipeline, profiling, session
from cleaner.exceptions import DataCleanerError, InvalidStepError, TemplateStorageError
from cleaner.steps import (
    AGGREGATION_FUNCTIONS,
    CASE_CHOICES,
    DEFAULT_KEEP_PATTERN,
    DEFAULT_VALUE_NAME,
    DEFAULT_VARIABLE_NAME,
    DUPLICATE_KEEP_CHOICES,
    FILL_STRATEGIES,
    MAX_PIVOT_COLUMNS,
    MAX_ROUNDING_DECIMALS,
    NUMERIC_ONLY_AGGREGATIONS,
    RESHAPE_ACTIONS,
    ROUNDING_DIRECTIONS,
    apply_output_names,
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

_ROUNDING_LABELS = {
    "nearest": "Nearest",
    "up": "Up",
    "down": "Down",
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
            ("round_numbers", "Round numbers", ":material/123:"),
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
    "round_numbers": "Round numbers",
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


def _dialog_round_numbers(table, frame) -> None:
    columns = st.multiselect(
        "Columns to round",
        options=profiling.numeric_columns(frame),
        key=f"dc_round_columns_{table.table_id}",
        help="Only numeric columns can be rounded — run 'Numbers as text' first if yours is still text.",
    )
    decimals_column, direction_column = st.columns(2)
    with decimals_column:
        decimals = st.number_input(
            "Decimal places",
            min_value=0,
            max_value=MAX_ROUNDING_DECIMALS,
            value=2,
            step=1,
            key=f"dc_round_decimals_{table.table_id}",
            help="0 gives whole numbers, 2 gives figures like 1234.57.",
        )
    with direction_column:
        direction = st.segmented_control(
            "Round",
            options=ROUNDING_DIRECTIONS,
            format_func=lambda choice: _ROUNDING_LABELS[choice],
            default="nearest",
            # Without this a second click on the selected option clears it, and the step
            # would fall back to a direction nobody chose.
            required=True,
            key=f"dc_round_direction_{table.table_id}",
            help=(
                "Nearest rounds .5 away from zero, as a spreadsheet does. Up and down go "
                "toward larger and smaller values, so -1.234 rounds up to -1.23."
            ),
        )

    params = {
        "columns": columns,
        "decimals": int(decimals),
        "direction": direction,
    }
    if columns:
        _impact_caption(frame, "round_numbers", params)
    _footer(table.table_id, "round_numbers", params, disabled=not columns)


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


def _run_reshape(table: session.TableState, action: str, frame, params: dict):
    """Runs the reshape for the dialog, returning `(result, warnings)` or None if it can't.

    Split from the preview below because the "Rename columns" boxes need the reshape's
    output column names before anything is drawn, and running the reshape a second time
    each rerun just to learn them would double the work on a large table.
    """
    try:
        spec = get_spec(action)
        spec.validate(params, list(frame.columns))
        return spec.apply(frame, params)
    except InvalidStepError as error:
        st.info(str(error), icon=":material/info:")
        return None
    except (ValueError, TypeError, KeyError) as error:
        logger.exception("Could not preview the '%s' reshape.", action)
        st.error(f"This couldn't be worked out from the current data: {error}")
        return None


def _rename_output_columns(table: session.TableState, action: str, saved: dict, columns: list[str]) -> dict:
    """The "Rename columns" section: one box per column the reshape produces.

    An expander rather than a second dialog, because Streamlit will not open a dialog from
    inside a dialog. Blank means "leave this heading alone", so the renames saved with the
    step are only the ones actually typed.
    """
    saved_names = saved.get("output_names") or {}
    with st.expander("Rename columns", icon=":material/edit:"):
        st.caption("Give the new table friendlier headings. Leave a box empty to keep the heading as it is.")
        renames = {}
        for column in columns:
            typed = st.text_input(
                column,
                value=str(saved_names.get(column, "")),
                placeholder=column,
                key=f"dc_rename_output_{action}_{table.table_id}_{column}",
                help=f"The heading '{column}' will carry in the new table and in the download.",
            ).strip()
            if typed and typed != column:
                renames[column] = typed
    return renames


def _show_reshape_preview(table: session.TableState, action: str, result, warnings_out: list[str]) -> None:
    """Shows the reshape's result.

    A live grid rather than the `_impact_caption` sentence the cleaning dialogs use: "this
    removes 40 rows" says nothing useful about a pivot, whereas seeing the shape is the
    whole decision.
    """
    for warning in warnings_out:
        st.warning(warning, icon=":material/error:")

    st.caption(f"Preview — {len(result):,} row(s) × {len(result.columns):,} column(s).")
    st.dataframe(
        display.arrow_safe(result.head(PREVIEW_ROWS_IN_DIALOG)),
        key=f"dc_reshape_preview_{action}_{table.table_id}",
        width="stretch",
        hide_index=True,
    )


def _render_reshape(table: session.TableState, action: str, frame, params: dict, ready: bool):
    """Runs the reshape, offers a rename of its output columns, and previews the result.

    Returns that result together with the params that produced it: the renames are part of
    the saved step, so they have to be folded back in before the Save footer runs.
    """
    if not ready:
        return None, params

    ran = _run_reshape(table, action, frame, params)
    if ran is None:
        return None, params
    result, warnings_out = ran

    output_names = _rename_output_columns(table, action, _saved_params(table, action), list(result.columns))
    if output_names:
        params = {**params, "output_names": output_names}
        result = apply_output_names(result, params)

    _show_reshape_preview(table, action, result, warnings_out)
    return result, params


def _aggregation_rows(
    table: session.TableState, action: str, frame, columns: list[str], saved: list[dict]
) -> list[dict]:
    """One "Using" picker per chosen column, and the aggregations they add up to.

    A single shared picker would cross every column with every function, so asking for the
    min of Salary and the count of Days would also produce a meaningless min of Days. Each
    column is offered only the functions its own type supports.
    """
    numeric = set(profiling.numeric_columns(frame))
    aggregations = []
    for column in columns:
        options = (
            AGGREGATION_FUNCTIONS
            if column in numeric
            else [function for function in AGGREGATION_FUNCTIONS if function not in NUMERIC_ONLY_AGGREGATIONS]
        )
        saved_functions = [
            aggregation.get("function") for aggregation in saved if aggregation.get("column") == column
        ]
        default = _keep_options(saved_functions, options) or ["sum" if column in numeric else "count"]
        functions = st.multiselect(
            f"Using — {column}",
            options=options,
            default=default,
            key=f"dc_agg_functions_{action}_{table.table_id}_{column}",
            help=f"What to work out for {column}. Pick more than one to get a column each, such as a min and a max.",
        )
        aggregations.extend({"column": column, "function": function} for function in functions)
    return aggregations


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
    saved_aggregations = saved.get("aggregations") or []
    saved_values = [aggregation.get("column") for aggregation in saved_aggregations]

    columns = st.multiselect(
        "Total up",
        options=value_options,
        default=_keep_options(list(dict.fromkeys(saved_values)), value_options),
        format_func=lambda column: column if column in numeric else f"{column} (text)",
        key=f"dc_summarise_values_{table.table_id}",
        help="Sum, average and median need a numeric column — the others work on text too.",
    )
    aggregations = _aggregation_rows(table, "group_summarise", frame, columns, saved_aggregations)

    params = {"group_by": group_by, "aggregations": aggregations}
    result, params = _render_reshape(table, "group_summarise", frame, params, ready=bool(aggregations))
    _save_summary(table, "group_summarise", params, result)


def _saved_pivot_aggregations(saved: dict) -> list[dict]:
    """The saved pivot's value columns, in either params shape.

    A pivot saved before several values were allowed holds one `values` column and one
    `function`; reading that as a one-entry list lets Edit open it without losing anything.
    """
    aggregations = saved.get("aggregations")
    if isinstance(aggregations, list) and aggregations:
        return aggregations
    if saved.get("values"):
        return [{"column": saved["values"], "function": saved.get("function") or "sum"}]
    return []


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
    numeric = set(profiling.numeric_columns(frame))
    saved_aggregations = _saved_pivot_aggregations(saved)
    saved_values = [aggregation.get("column") for aggregation in saved_aggregations]

    values = st.multiselect(
        "Values",
        options=value_options,
        default=_keep_options(list(dict.fromkeys(saved_values)), value_options),
        format_func=lambda column: column if column in numeric else f"{column} (text)",
        key=f"dc_pivot_values_{table.table_id}",
        help="The columns filling the grid. Pick more than one to show, say, a min and a max side by side.",
    )
    aggregations = _aggregation_rows(table, "pivot", frame, values, saved_aggregations)

    fill_value = st.text_input(
        "Fill empty cells with",
        value=str(saved.get("fill_value") or ""),
        key=f"dc_pivot_fill_{table.table_id}",
        help="Leave blank to leave empty cells empty. Type 0 to show gaps as zeroes.",
    ).strip()

    params = {
        "index": index,
        "columns": across,
        "aggregations": aggregations,
        "fill_value": fill_value or None,
    }
    result, params = _render_reshape(table, "pivot", frame, params, ready=bool(index and across and aggregations))
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
    result, params = _render_reshape(table, "unpivot", frame, params, ready=bool(id_columns))
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
    "round_numbers": _dialog_round_numbers,
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
# Cleaning templates
#
# A named, saved working set — every expected file with its steps, plus the Pivot / Group &
# total / Unpivot tables saved off them — so that next month's files are cleaned by picking
# a name instead of by repeating a dozen dialogs. `cleaner/template.py` says what one is,
# `cleaner/matching.py` measures an upload against one, `cleaner/db.py` stores it.
#
# A **bar, not a gate.** Task Builder makes choosing a Task the whole first screen because a
# Task *is* that page. Cleaning files nobody has a template for is this page's entire
# existing purpose, so a wall in front of the uploader would be a regression. The picker's
# `— New template —` is the same "open one, or start a new one" offer, with nothing gated
# behind it.
#
# **The bar records intent; nothing here acts.** It draws above `st.file_uploader`, and a
# run that ends before that widget is created drops every uploaded file — so a button that
# reran from here would trade the user's data for their recipe. Buttons set a flag and let
# the run continue; the selection is read back by `_select_template` below the uploader, and
# every dialog renders there too. This is the same rule `chat_with_data.py` follows for its
# chat type bar, and it is load-bearing.
#
# **Choosing a template selects it; Apply cleaning steps runs it.** The two used to be one
# motion — picking a name applied its recipes there and then — which read as broken in the
# one order users actually work in: choose the template, *then* upload this month's files.
# The picker had not changed by the time the files arrived, so nothing ran and the page sat
# there showing raw data under a template it claimed to be cleaning under. The rule now is
# the one a user can see: the recipes run when, and only when, the button is pressed. That
# also makes it repeatable — upload a file you forgot, press it again — which is safe
# because `session.apply_template` restates a matched table's recipe rather than adding to
# it. The button lives below the uploader with everything else that reruns.
# --------------------------------------------------------------------------------------

TEMPLATE_NEW_LABEL = "— New template —"


def _saved_templates(user_id: int) -> list[dict] | None:
    """Every saved template for this account, or None when the list couldn't be read.

    None rather than `[]`, on the grounds `task_builder._saved_tasks` gives: "you have none"
    and "we couldn't look" lead to two different next actions.
    """
    try:
        return cleaner_db.list_templates(user_id)
    except TemplateStorageError as error:
        logger.exception("Could not list cleaning templates for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return None


def _template_name_taken(user_id: int, name: str, ignoring: int | None = None) -> bool:
    """Whether another saved template already answers to this name.

    Worth checking before saving: `cleaner.db.save_template` treats a name already in use as
    "update that row", so a collision is not an error later — it is a silent overwrite of
    somebody's other template.

    A lookup that fails returns False: refusing a name because the database hiccuped would
    block the user for a reason that has nothing to do with them.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return False
    try:
        rows = cleaner_db.list_templates(user_id)
    except TemplateStorageError:
        logger.exception("Could not check template names for user %s.", user_id)
        return False
    return any(
        str(row["name"]).strip().casefold() == wanted and row["template_id"] != ignoring
        for row in rows
    )


def _there_is_something_to_save() -> bool:
    """Whether the Save/Update buttons should be live.

    The uploader's own session_state key is consulted alongside the working set, and it is
    the half that matters: the bar draws *above* `st.file_uploader`, so on the run where a
    file first arrives `sync_tables` has not run yet and the working set is still the
    previous run's — empty. Streamlit fills a widget's key from the browser before the
    script starts, so the uploader's value is already the new one at this point, while
    `source_tables()` would leave Save greyed out over a page full of freshly loaded data
    until the user happened to click something else.
    """
    return bool(session.source_tables() or st.session_state.get(session.DC_UPLOADER_KEY))


def _render_template_bar(rows: list[dict] | None) -> None:
    """The picker and its four buttons, above the uploader. Records intent only.

    A select box rather than a list of cards, because an account is free to save a hundred
    templates and a dropdown has type-to-filter for nothing.
    """
    active_id, active_name = session.active_template()
    listing = rows or []
    savable = _there_is_something_to_save()

    with st.container(border=True):
        picker_column, status_column = st.columns([2, 3], vertical_alignment="center")

        with picker_column:
            if rows is None:
                st.caption("Your saved templates couldn't be listed — the message above says why.")
            else:
                saved_picker.select_saved(
                    listing,
                    key=session.DC_TEMPLATE_PICK_KEY,
                    id_key="template_id",
                    label="Cleaning template",
                    help=(
                        "A saved set of files and the steps that clean each one. Choosing one "
                        "selects it — press Apply cleaning steps, below the uploader, to run "
                        "it against your files. Start typing to filter."
                    ),
                    include_none=True,
                    none_label=TEMPLATE_NEW_LABEL,
                    index=saved_picker.option_index(
                        listing, id_key="template_id", selected_id=active_id, include_none=True
                    ),
                )

        with status_column:
            if active_id is None:
                st.caption(
                    "Clean your files as usual, then **Save as template** to do it in one "
                    "click next month."
                )
            else:
                st.caption(f"Selected: **{active_name}**.")

        schema_column, save_column, update_column, delete_column = st.columns(4)
        with schema_column:
            # Disabled rather than hidden on `— New template —`, so the bar keeps its shape
            # — the same call `chat_with_data.py`'s Show schema button makes.
            if st.button(
                "Show expected files",
                key="dc_template_schema",
                icon=":material/schema:",
                width="stretch",
                disabled=active_id is None,
                help="What this template expects to be uploaded, and what it does to each file.",
            ):
                session.open_template_dialog("schema")
        with save_column:
            if st.button(
                "Save as template",
                key="dc_template_save_as",
                icon=":material/bookmark_add:",
                width="stretch",
                disabled=not savable,
                help="Save every uploaded file's steps, and the summary tables, under a new name.",
            ):
                session.open_template_dialog("save_as")
        with update_column:
            if st.button(
                "Update template",
                key="dc_template_update",
                icon=":material/save:",
                type="primary",
                width="stretch",
                disabled=active_id is None or not savable,
                help="Overwrite this template with the steps currently on screen.",
            ):
                session.open_template_dialog("update", {"template_id": active_id, "name": active_name})
        with delete_column:
            if st.button(
                "",
                key="dc_template_delete",
                icon=":material/delete:",
                width="stretch",
                disabled=active_id is None,
                help="Permanently delete this saved template.",
            ):
                session.open_template_dialog("delete", {"template_id": active_id, "name": active_name})


def _select_template(user_id: int) -> None:
    """Reads the picker back and records the choice. Applies nothing.

    Called below `st.file_uploader` and below `sync_tables`, which is both halves of the
    reason it isn't done in the bar: a run ending up there would drop the uploaded files,
    and the working set it reports on wouldn't yet include what was just uploaded.

    Selecting is deliberately not applying — see this section's header comment. Choosing
    `— New template —` deselects without touching a single step either: deselecting is not
    undoing, see `session.clear_active_template`.
    """
    raw = st.session_state.get(session.DC_TEMPLATE_PICK_KEY)
    # `— New template —` is a sentinel in the option list, not `None`: `st.selectbox` reserves
    # `None` for "nothing is selected", so an option carrying it could be offered and never
    # chosen. Both forms mean the same thing here.
    selected_id = None if raw in (None, saved_picker.NONE_OPTION) else raw
    active_id, _ = session.active_template()
    if selected_id == active_id:
        return

    if selected_id is None:
        session.clear_active_template()
        # The bar has already drawn "Selected: X" further up this run, so the caption would
        # contradict the picker until the next interaction without this.
        st.rerun(scope="app")
        return

    try:
        template = cleaner_db.load_template(selected_id, user_id)
    except TemplateStorageError as error:
        logger.exception("Could not load cleaning template %s.", selected_id)
        st.error(str(error), icon=":material/error:")
        session.clear_active_template()
        # The picker still holds the id that just refused to load, and leaving it there
        # would retry — and re-report — the same failure on every rerun from now on.
        session.queue_template_selection(saved_picker.NONE_OPTION)
        return

    session.set_active_template(template)
    st.rerun(scope="app")


def _render_template_apply() -> None:
    """The Apply button, and what happens when it is pressed.

    The whole action of a template, in one visible press. It is drawn below the uploader
    because it reruns, and because the working set it measures itself against is only
    complete once `sync_tables` has seen this run's upload.

    A missing expected file **refuses the whole apply** rather than cleaning the rest: half
    a working set is not what "Receivables" means, the summaries a template rebuilds can
    read from any of its tables, and a user who has just been told three files are expected
    can upload the third. Nothing is lost by waiting — the button is still there.
    """
    template = session.active_template_object()
    if template is None:
        return

    pressed = st.button(
        f"Apply cleaning steps from “{template.display_name()}”",
        key="dc_template_apply",
        icon=":material/play_arrow:",
        type="primary",
        width="stretch",
        disabled=not session.source_tables(),
        help=(
            "Checks your uploaded files against this template, then runs each file's saved "
            "steps and rebuilds its summary tables. Safe to press again after uploading "
            "more files."
        ),
    )
    if not pressed:
        return

    match = session.match_template(template)
    if not match.ok:
        for problem in match.problems():
            st.error(problem, icon=":material/error:")
        st.caption("Nothing was applied. Upload the missing file(s) and press Apply again.")
        return

    cleaned, rebuilt = session.apply_template(template, match)
    session.queue_flash(
        f"Applied “{template.display_name()}” — {cleaned} file(s) cleaned, "
        f"{rebuilt} summary table(s) rebuilt."
    )
    # The tab strip is built from the working set, which this has just changed: applying a
    # template adds and removes whole tables, so the run has to start again to draw them.
    st.rerun(scope="app")


def _render_template_status() -> None:
    """How the current upload measures up against the selected template.

    Drawn after the uploader for the same reason the selection is acted on there — before
    `sync_tables` the working set is still the previous run's, so this would report on files
    the user has already replaced.
    """
    template = session.active_template_object()
    if template is None:
        return

    match = session.match_template(template)
    if match.ok and not match.has_notes:
        st.success(match.summary(), icon=":material/check_circle:")
        return

    with st.expander(f"{template.display_name()} — {match.status_word()}", expanded=not match.ok):
        st.caption(match.summary())
        for problem in match.problems():
            st.warning(problem, icon=":material/error:")
        for note in match.notes():
            st.info(note, icon=":material/info:")
        if not match.ok:
            st.caption(
                "Apply is refused until every expected file is here. Upload the missing "
                "one(s), then press Apply cleaning steps."
            )


def _dismiss_template_dialog() -> None:
    """Clears the flag when a dialog is dismissed by the X, ESC or a click outside.

    Without it the flag survives and the next unrelated rerun reopens the same dialog — the
    same reason `session.open_dialog` exists for the cleaning actions.
    """
    session.close_template_dialog()


@st.dialog("What this template expects", on_dismiss=_dismiss_template_dialog)
def _dialog_template_schema(user_id: int, payload: dict) -> None:
    """The saved schema, read on demand rather than announced above the uploader.

    "Receivables expects billwise_due, customer_master and sales" is the answer to a
    question the user asks once — printing it permanently would put a box between them and
    their data on every run after that.
    """
    template = session.active_template_object()
    if template is None:
        st.info("No template is selected.", icon=":material/info:")
        return

    st.caption(template.summary_line())
    if template.description:
        st.write(template.description)

    match = session.match_template(template)
    for table in template.tables:
        found = table.name in match.matched
        icon = ":material/check_circle:" if found else ":material/error:"
        with st.expander(f"{table.name} — {'uploaded' if found else 'not uploaded'}", icon=icon):
            st.caption(f"Saved from **{table.file_name or table.name}**.")
            if table.columns:
                st.caption("Columns after cleaning:")
                st.write(", ".join(f"`{column}`" for column in table.columns))
            else:
                st.caption("No column list was saved with this file.")

            described = pipeline.describe_steps(table.steps)
            if described:
                st.caption(f"{len(described)} cleaning step(s):")
                for position, line in enumerate(described, start=1):
                    st.write(f"{position}. {line}")
            else:
                st.caption("No cleaning steps — this file is used as uploaded.")

            for summary in template.summaries_of(table.name):
                st.caption(f"Summary table **{summary.name}** — {pipeline.describe_step(summary.reshape)}")

    if st.button(
        "Close",
        key="dc_template_schema_close",
        width="stretch",
        help="Close this and carry on cleaning.",
    ):
        session.close_template_dialog()
        st.rerun(scope="app")


@st.dialog("Save as template", on_dismiss=_dismiss_template_dialog)
def _dialog_save_template(user_id: int, payload: dict) -> None:
    """Names the working set and writes it.

    A name already belonging to another template is refused rather than obeyed: `save_template`
    reads it as "update that row", which from here would be a silent overwrite of somebody
    else's month of work.
    """
    tables = session.source_tables()
    st.caption(
        f"{len(tables)} uploaded file(s) and their steps, plus every summary table saved off "
        "them, stored under one name."
    )

    typed = st.text_input(
        "Template name",
        key="dc_template_new_name",
        placeholder="e.g. Receivables",
        help="What this template is called when you come back to pick it.",
    )
    description = st.text_area(
        "What it's for (optional)",
        key="dc_template_new_description",
        placeholder="e.g. Monthly receivables pack — billwise due, customer master and sales.",
        help="Shown under the picker, so this can be identified six months from now.",
    )

    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Save template",
            key="dc_template_save_confirm",
            type="primary",
            width="stretch",
            disabled=not typed.strip(),
            help="Write these files and their steps to a new saved template.",
        ):
            if _template_name_taken(user_id, typed):
                st.error(
                    f"You already have a template called “{typed.strip()}” — pick it above and "
                    "use Update template, or choose another name.",
                    icon=":material/error:",
                )
                return
            _write_template(user_id, typed, description=description, template_id=None)
    with cancel_column:
        if st.button(
            "Cancel",
            key="dc_template_save_cancel",
            width="stretch",
            help="Close without saving anything.",
        ):
            session.close_template_dialog()
            st.rerun(scope="app")


@st.dialog("Update this template?", on_dismiss=_dismiss_template_dialog)
def _dialog_update_template(user_id: int, payload: dict) -> None:
    """Confirms overwriting the selected template with what is on screen.

    Asked rather than assumed: the button sits beside a picker, and overwriting a saved
    recipe is not something to discover afterwards.
    """
    name = payload.get("name") or ""
    template = session.active_template_object()
    st.write(
        f"**{name}** will be replaced by the {len(session.source_tables())} uploaded file(s) "
        "and their steps as they stand now. The previous version isn't kept."
    )

    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Update template",
            key="dc_template_update_confirm",
            type="primary",
            width="stretch",
            help="Overwrite the saved template with what is on screen.",
        ):
            _write_template(
                user_id,
                name,
                description=template.description if template is not None else "",
                template_id=payload.get("template_id"),
            )
    with cancel_column:
        if st.button(
            "Cancel",
            key="dc_template_update_cancel",
            width="stretch",
            help="Leave the saved template as it is.",
        ):
            session.close_template_dialog()
            st.rerun(scope="app")


@st.dialog("Delete this template?", on_dismiss=_dismiss_template_dialog)
def _dialog_delete_template(user_id: int, payload: dict) -> None:
    """Confirms deleting a saved template. The cleaning on screen is untouched either way."""
    name = payload.get("name") or ""
    template_id = payload.get("template_id")
    st.write(f"**{name}** will be permanently deleted. This can't be undone.")
    st.caption("The files and steps currently on screen aren't affected.")

    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        if st.button(
            "Delete template",
            key="dc_template_delete_confirm",
            type="primary",
            width="stretch",
            help="Permanently delete this saved template.",
        ):
            try:
                cleaner_db.delete_template(template_id, user_id)
            except TemplateStorageError as error:
                logger.exception("Could not delete cleaning template %s.", template_id)
                st.error(str(error), icon=":material/error:")
                return
            session.clear_active_template()
            # The picker's own key still holds the id that has just stopped being an option,
            # and this is well past the point where Streamlit will let it be written — so the
            # reset is queued for the top of the next run.
            session.queue_template_selection(saved_picker.NONE_OPTION)
            session.close_template_dialog()
            session.queue_flash(f"Deleted “{name}”.")
            st.rerun(scope="app")
    with cancel_column:
        if st.button(
            "Cancel",
            key="dc_template_delete_cancel",
            width="stretch",
            help="Keep this template.",
        ):
            session.close_template_dialog()
            st.rerun(scope="app")


def _write_template(user_id: int, name: str, *, description: str, template_id: int | None) -> None:
    """Captures the working set and stores it, or says why it couldn't.

    Shared by Save as and Update because the two differ only in whether an id is carried:
    `cleaner.db.save_template` inserts or updates from that alone.
    """
    try:
        captured = session.capture_template(name, description=description, template_id=template_id)
        saved = cleaner_db.save_template(user_id, captured)
    except DataCleanerError as error:
        logger.exception("Could not save cleaning template '%s' for user %s.", name, user_id)
        st.error(str(error), icon=":material/error:")
        return

    session.set_active_template(saved)
    session.queue_template_selection(saved.template_id)
    session.close_template_dialog()
    session.queue_flash(f"Saved “{saved.display_name()}” — {saved.summary_line()}.")
    st.rerun(scope="app")


TEMPLATE_DIALOGS = {
    "schema": _dialog_template_schema,
    "save_as": _dialog_save_template,
    "update": _dialog_update_template,
    "delete": _dialog_delete_template,
}


def _render_pending_template_dialog(user_id: int) -> None:
    """Opens whichever template dialog is flagged.

    Rendered below the uploader, never from the bar that sets the flag: a dialog's own
    buttons end their run with `st.rerun`, and doing that above `st.file_uploader` would
    drop every uploaded file.
    """
    pending = session.pending_template_dialog()
    if pending is None:
        return

    name, payload = pending
    if name not in TEMPLATE_DIALOGS:
        session.close_template_dialog()
        return

    TEMPLATE_DIALOGS[name](user_id, payload)


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
    shown, as_text = display.to_arrow_safe(cleaned)
    st.dataframe(
        shown,
        key=f"dc_preview_{table.table_id}",
        width="stretch",
        hide_index=True,
    )
    if as_text:
        st.caption(
            "Shown as text because the column holds a mix of numbers and text: "
            + ", ".join(f"**{name}**" for name in as_text)
            + ". The data itself is untouched — set a type, or clean the odd values, to fix it."
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

    user_id = st.session_state["user_id"]

    st.subheader("🧹Data Cleaner")
    st.write(":blue[**Upload files, clean each table, and download one multi-sheet workbook.**]")

    # A container that is always here and usually empty. Writing the message straight onto
    # the page would make every element below it sit one position lower on the runs that
    # have something to say, which is how a browser ends up showing both positions at once.
    with st.container():
        template_flash = session.consume_flash()
        if template_flash:
            st.success(template_flash, icon=":material/check_circle:")

    # Applied before the picker is created, since Streamlit forbids writing a widget's own
    # key once it exists this run. Saving and deleting are what queue one.
    session.consume_template_selection()

    template_rows = _saved_templates(user_id)
    # Above the uploader, and records intent only — see this section's header comment.
    _render_template_bar(template_rows)

    loaded_tables = _render_upload()

    # Everything that can end a run now runs *below* `st.file_uploader`, so ending one
    # cannot drop the files it holds.
    _select_template(user_id)
    _render_pending_template_dialog(user_id)
    _render_template_status()
    _render_template_apply()

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
