"""The cleaning actions themselves: one executor, log renderer and validator per action.

Every executor shares the signature `(frame, params) -> (new_frame, warnings)` and
returns a **new** DataFrame — pandas 3 enforces copy-on-write, so `inplace=` and
chained assignment are not options.

All regex work compiles the pattern with `re.compile` before handing it to pandas.
That is not decoration: pandas 3 routes `str.replace(regex=True)` through Arrow's RE2
engine for plain string patterns, which rejects `\\u` escapes and lacks lookarounds,
but falls back to Python's `re` whenever it is given a compiled pattern. Compiling
therefore guarantees the semantics that `validate` checked with `re.compile` are the
semantics that actually run.

`STEP_REGISTRY` at the bottom is an explicit literal dict rather than a decorator-built
one: a decorator lets an import-order slip produce a silently empty registry, and
Stage 8's replay would then skip every step without raising anything.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from cleaner.exceptions import InvalidStepError
from cleaner.profiling import (
    CATEGORICAL,
    COLUMN_TYPES,
    DATE,
    ID,
    INVISIBLE_WHITESPACE,
    NUMERIC,
    TEXT,
    blank_mask,
    parse_datetime_series,
    parse_numeric_series,
    text_columns,
)

logger = logging.getLogger(__name__)

MAX_REGEX_LENGTH = 500
_WARNING_SAMPLE_SIZE = 5

CASE_CHOICES = ["upper", "lower", "title"]

# Rounding directions. "up" is the ceiling and "down" the floor — both toward a fixed
# side of the number line, so -1.234 rounded up to 2dp is -1.23. That is not Excel's
# ROUNDUP, which goes away from zero; the dialog says which one this is.
ROUNDING_DIRECTIONS = ["nearest", "up", "down"]
MAX_ROUNDING_DECIMALS = 10
# Scaling by a power of ten leaves binary floating-point residue — 1.1 * 10 is
# 11.000000000000002, whose ceiling is 12, so "round 1.1 up to 1dp" would return 1.2.
# Nudging the scaled value back to this many digits erases the residue while staying far
# below the ~15 significant digits a float actually carries.
_ROUNDING_GUARD_DIGITS = 9

# "custom" absorbed what used to be a separate "unknown" strategy — that was a custom
# fill with the word hardcoded, and two entries for one idea is one too many. The dialog
# still pre-fills "Unknown", so it stays a zero-typing default.
FILL_STRATEGIES = ["custom", "previous", "next", "zero", "mean", "median", "drop_rows"]
DUPLICATE_KEEP_CHOICES = ["first", "last"]

# Aggregations offered by the three reshape actions. `count` counts filled values rather
# than rows, matching every other blank-aware count on this page.
AGGREGATION_FUNCTIONS = ["sum", "mean", "median", "min", "max", "count", "count_distinct", "first"]
PANDAS_AGGREGATIONS = {
    "sum": "sum",
    "mean": "mean",
    "median": "median",
    "min": "min",
    "max": "max",
    "count": "count",
    "count_distinct": "nunique",
    "first": "first",
}
# The three that need a numeric column. min/max/first order text perfectly sensibly, so
# they are deliberately not in here.
NUMERIC_ONLY_AGGREGATIONS = {"sum", "mean", "median"}

MAX_PIVOT_COLUMNS = 200
DEFAULT_VARIABLE_NAME = "Attribute"
DEFAULT_VALUE_NAME = "Value"

DEFAULT_KEEP_PATTERN = r"A-Za-z0-9 .,_@&()\-/"

_ALL_WHITESPACE = re.compile(rf"[\s{INVISIBLE_WHITESPACE}]+")
_SURROUNDING_WHITESPACE = re.compile(rf"^[\s{INVISIBLE_WHITESPACE}]+|[\s{INVISIBLE_WHITESPACE}]+$")


class RecordPolicy(StrEnum):
    """How an applied action is written into the table's cleaning log.

    This governs log bookkeeping only. It never combines data — no rows, columns, files
    or tables are ever joined or stacked by this utility.
    """

    ADD = "add"
    REPLACE = "replace"
    UPDATE_PER_COLUMN = "update_per_column"


@dataclass(frozen=True)
class StepSpec:
    """Everything about one cleaning action, kept in one place so adding a new action is
    a single self-contained registry entry."""

    action: str
    label: str
    apply: Callable[[pd.DataFrame, dict], tuple[pd.DataFrame, list[str]]]
    describe: Callable[[dict], str]
    validate: Callable[[dict, list[str]], None]
    required_columns: Callable[[dict], list[str]]
    record: RecordPolicy = RecordPolicy.ADD
    pin_rank: int | None = None


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def _present(frame: pd.DataFrame, columns: list[str]) -> tuple[list[str], list[str]]:
    """Splits requested columns into those that exist and those that don't.

    Steps operate on what survives and report the rest, rather than failing outright —
    a column may legitimately have been deleted or renamed by an earlier step.
    """
    existing = [column for column in columns if column in frame.columns]
    missing = [column for column in columns if column not in frame.columns]
    return existing, missing


def _missing_warning(missing: list[str]) -> list[str]:
    if not missing:
        return []
    return [f"Skipped missing column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}."]


def _samples(series: pd.Series, index: pd.Index) -> str:
    values = series.loc[index].dropna().astype("string").head(_WARNING_SAMPLE_SIZE).tolist()
    return ", ".join(f'"{value}"' for value in values)


def _coercion_warning(column: str, series: pd.Series, failed: pd.Index, kind: str) -> list[str]:
    if len(failed) == 0:
        return []
    present = int(series.notna().sum())
    return [
        f"'{column}': {len(failed):,} of {present:,} values couldn't be read as a {kind} "
        f"and are now blank. Examples: {_samples(series, failed)}."
    ]


def _compile(pattern: str, *, case_sensitive: bool = True) -> re.Pattern:
    """Compiles a user-supplied regex, raising InvalidStepError on a bad pattern."""
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as error:
        raise InvalidStepError(f"'{pattern}' isn't a valid regular expression: {error}.") from error


def _dedupe_columns(columns: list[str]) -> list[str]:
    """Makes promoted header labels unique the way pandas does (`Amount`, `Amount.1`)."""
    seen: dict[str, int] = {}
    result = []
    for column in columns:
        if column in seen:
            seen[column] += 1
            result.append(f"{column}.{seen[column]}")
        else:
            seen[column] = 0
            result.append(column)
    return result


def _require_keys(params: dict, keys: set[str], action: str) -> None:
    unexpected = set(params) - keys
    if unexpected:
        raise InvalidStepError(f"{action} received unexpected parameters: {', '.join(sorted(unexpected))}.")


def _require_columns_exist(columns: list[str], available: list[str], action: str) -> None:
    if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
        raise InvalidStepError(f"{action} needs a list of column names.")
    if not columns:
        raise InvalidStepError(f"{action} needs at least one column.")
    unknown = [column for column in columns if column not in available]
    if unknown:
        raise InvalidStepError(f"{action}: no such column{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}.")


# --------------------------------------------------------------------------------------
# skip_rows
# --------------------------------------------------------------------------------------


def _apply_skip_rows(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    top = int(params.get("top", 0))
    bottom = int(params.get("bottom", 0))
    promote_header = bool(params.get("promote_header", False))
    warnings_out: list[str] = []

    result = frame
    if top or bottom:
        end = max(len(frame) - bottom, 0)
        if top >= end:
            warnings_out.append(
                f"Skipping {top} row(s) from the top and {bottom} from the bottom removes every one of "
                f"the {len(frame):,} rows in this table."
            )
            return frame.iloc[0:0].copy(), warnings_out
        result = frame.iloc[top:end]

    if promote_header:
        if result.empty:
            warnings_out.append("There was no row left to use as the header.")
        else:
            header = [str(value) if pd.notna(value) else "" for value in result.iloc[0]]
            header = [name if name else f"column_{position + 1}" for position, name in enumerate(header)]
            result = result.iloc[1:]
            result.columns = _dedupe_columns(header)

    return result.reset_index(drop=True), warnings_out


def _describe_skip_rows(params: dict) -> str:
    parts = []
    if params.get("top"):
        parts.append(f"{params['top']} row(s) from the top")
    if params.get("bottom"):
        parts.append(f"{params['bottom']} row(s) from the bottom")
    skipped = " and ".join(parts) if parts else "no rows"
    header_note = ", using the next row as the header" if params.get("promote_header") else ""
    return f"Skipped {skipped}{header_note}"


def _validate_skip_rows(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"top", "bottom", "promote_header"}, "Skip rows")
    for key in ("top", "bottom"):
        value = params.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise InvalidStepError(f"Skip rows: '{key}' must be zero or a positive whole number.")


# --------------------------------------------------------------------------------------
# set_column_types
# --------------------------------------------------------------------------------------


def _apply_set_column_types(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    by_column = params.get("by_column", {})
    existing, missing = _present(frame, list(by_column))
    result = frame.copy()
    warnings_out = _missing_warning(missing)

    for column in existing:
        settings = by_column[column]
        target = settings.get("target_type", TEXT)
        series = result[column]

        if target == NUMERIC:
            parsed, failed = parse_numeric_series(
                series, settings.get("decimal_separator", "."), settings.get("parentheses_are_negative", True)
            )
            result[column] = parsed
            warnings_out.extend(_coercion_warning(column, series, failed, "number"))
        elif target == DATE:
            parsed, failed = parse_datetime_series(series, settings.get("date_format"))
            result[column] = parsed
            warnings_out.extend(_coercion_warning(column, series, failed, "date"))
        else:
            # text, categorical and id all stay as text. categorical and id are recorded
            # intent rather than a different storage format: keeping them as strings means
            # the text-cleanup actions still reach them, and an id column keeps the leading
            # zeros that made it an id in the first place.
            result[column] = series.astype("string")

    return result, warnings_out


def _describe_set_column_types(params: dict) -> str:
    by_column = params.get("by_column", {})
    if not by_column:
        return "Set column types"
    pairs = ", ".join(f"{column} -> {settings.get('target_type', TEXT)}" for column, settings in by_column.items())
    return f"Set column types ({pairs})"


def _validate_set_column_types(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"by_column"}, "Set column types")
    by_column = params.get("by_column")
    if not isinstance(by_column, dict) or not by_column:
        raise InvalidStepError("Set column types needs at least one column.")
    for column, settings in by_column.items():
        if not isinstance(settings, dict):
            raise InvalidStepError(f"Set column types: settings for '{column}' must be a mapping.")
        target = settings.get("target_type")
        if target not in COLUMN_TYPES:
            raise InvalidStepError(
                f"Set column types: '{target}' isn't a valid type for '{column}'. "
                f"Choose one of {', '.join(COLUMN_TYPES)}."
            )


# --------------------------------------------------------------------------------------
# remove_empty_rows
# --------------------------------------------------------------------------------------


def _apply_remove_empty_rows(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    requested = params.get("columns")
    blank_counts = bool(params.get("blank_strings_count_as_empty", True))

    if requested:
        existing, missing = _present(frame, requested)
        warnings_out = _missing_warning(missing)
    else:
        existing, warnings_out = list(frame.columns), []

    if not existing:
        return frame.copy(), warnings_out

    subset = frame[existing]
    # `blank_mask` rather than a local strip-and-compare: it is the one definition of
    # blank the stats panel and the fill step also use, and it catches the non-breaking
    # space that `str.strip()` leaves behind.
    is_blank = subset.apply(blank_mask) if blank_counts else subset.isna()

    result = frame.loc[~is_blank.all(axis=1)].reset_index(drop=True)
    return result, warnings_out


def _describe_remove_empty_rows(params: dict) -> str:
    scope = f" across {', '.join(params['columns'])}" if params.get("columns") else " (all columns blank)"
    return f"Removed empty rows{scope}"


def _validate_remove_empty_rows(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"columns", "blank_strings_count_as_empty"}, "Remove empty rows")
    if params.get("columns"):
        _require_columns_exist(params["columns"], columns, "Remove empty rows")


# --------------------------------------------------------------------------------------
# delete_columns
# --------------------------------------------------------------------------------------


def _apply_delete_columns(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    existing, missing = _present(frame, params.get("columns", []))
    return frame.drop(columns=existing), _missing_warning(missing)


def _describe_delete_columns(params: dict) -> str:
    return f"Deleted column(s): {', '.join(params.get('columns', []))}"


def _validate_delete_columns(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"columns"}, "Delete columns")
    _require_columns_exist(params.get("columns", []), columns, "Delete columns")
    if not set(columns) - set(params["columns"]):
        raise InvalidStepError("Delete columns would remove every column in the table.")


# --------------------------------------------------------------------------------------
# rename_columns
# --------------------------------------------------------------------------------------


def _apply_rename_columns(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    renames = params.get("renames", {})
    existing, missing = _present(frame, list(renames))
    applicable = {old: renames[old] for old in existing}
    return frame.rename(columns=applicable), _missing_warning(missing)


def _describe_rename_columns(params: dict) -> str:
    pairs = ", ".join(f"{old} -> {new}" for old, new in params.get("renames", {}).items())
    return f"Renamed column(s): {pairs}"


def _validate_rename_columns(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"renames"}, "Rename columns")
    renames = params.get("renames")
    if not isinstance(renames, dict) or not renames:
        raise InvalidStepError("Rename columns needs at least one column to rename.")

    _require_columns_exist(list(renames), columns, "Rename columns")

    for old, new in renames.items():
        if not isinstance(new, str) or not new.strip():
            raise InvalidStepError(f"Rename columns: '{old}' needs a non-empty new name.")

    resulting = [renames.get(column, column) for column in columns]
    duplicates = sorted({name for name in resulting if resulting.count(name) > 1})
    if duplicates:
        raise InvalidStepError(
            f"Rename columns would create duplicate column name(s): {', '.join(duplicates)}."
        )


# --------------------------------------------------------------------------------------
# fix_numeric_text
# --------------------------------------------------------------------------------------


def _apply_fix_numeric_text(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    existing, missing = _present(frame, params.get("columns", []))
    result = frame.copy()
    warnings_out = _missing_warning(missing)

    for column in existing:
        series = result[column]
        parsed, failed = parse_numeric_series(
            series, params.get("decimal_separator", "."), params.get("parentheses_are_negative", True)
        )
        result[column] = parsed
        warnings_out.extend(_coercion_warning(column, series, failed, "number"))

    return result, warnings_out


def _describe_fix_numeric_text(params: dict) -> str:
    return f"Converted text to numbers in: {', '.join(params.get('columns', []))}"


def _validate_fix_numeric_text(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"columns", "decimal_separator", "parentheses_are_negative"}, "Fix numbers stored as text")
    _require_columns_exist(params.get("columns", []), columns, "Fix numbers stored as text")
    if params.get("decimal_separator", ".") not in (".", ","):
        raise InvalidStepError("Fix numbers stored as text: the decimal separator must be '.' or ','.")


# --------------------------------------------------------------------------------------
# round_numbers
# --------------------------------------------------------------------------------------


def _round_series(series: pd.Series, decimals: int, direction: str) -> pd.Series:
    """Rounds one numeric column, preserving blanks and the column's own dtype."""
    scale = 10**decimals
    scaled = (series * scale).round(_ROUNDING_GUARD_DIGITS)

    if direction == "up":
        rounded = np.ceil(scaled)
    elif direction == "down":
        rounded = np.floor(scaled)
    else:
        # Half away from zero, the way a spreadsheet rounds. pandas' own `.round` is
        # half-to-even, which turns 2.5 into 2 and surprises anyone who checked the
        # figure in Excel first.
        rounded = np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)

    return rounded / scale


def _apply_round_numbers(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    existing, missing = _present(frame, params.get("columns", []))
    decimals = int(params.get("decimals", 0))
    direction = params.get("direction", "nearest")
    result = frame.copy()
    warnings_out = _missing_warning(missing)

    for column in existing:
        series = result[column]
        if not is_numeric_dtype(series):
            warnings_out.append(
                f"'{column}' isn't a numeric column, so it was left alone. Set its type, or run "
                f"'Fix numbers stored as text' on it, first."
            )
            continue
        # Whole numbers are already rounded to any of the decimal places on offer, and
        # rounding one would only turn an integer column into a float.
        if pd.api.types.is_integer_dtype(series):
            continue
        result[column] = _round_series(series, decimals, direction)

    return result, warnings_out


def _describe_round_numbers(params: dict) -> str:
    direction = params.get("direction", "nearest")
    phrase = {"up": "up", "down": "down"}.get(direction, "to the nearest value")
    decimals = params.get("decimals", 0)
    return (
        f"Rounded {phrase} to {decimals} decimal place(s): {', '.join(params.get('columns', []))}"
    )


def _validate_round_numbers(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"columns", "decimals", "direction"}, "Round numbers")
    _require_columns_exist(params.get("columns", []), columns, "Round numbers")

    decimals = params.get("decimals")
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= MAX_ROUNDING_DECIMALS:
        raise InvalidStepError(
            f"Round numbers: decimal places must be a whole number between 0 and {MAX_ROUNDING_DECIMALS}."
        )
    if params.get("direction") not in ROUNDING_DIRECTIONS:
        raise InvalidStepError(
            f"Round numbers: '{params.get('direction')}' isn't a rounding direction. "
            f"Choose one of {', '.join(ROUNDING_DIRECTIONS)}."
        )


# --------------------------------------------------------------------------------------
# trim_whitespace
# --------------------------------------------------------------------------------------


def _apply_trim_whitespace(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    collapse_internal = bool(params.get("collapse_internal", True))
    result = frame.copy()

    for column in text_columns(result):
        series = result[column]
        if collapse_internal:
            series = series.str.replace(_ALL_WHITESPACE, " ", regex=True)
        result[column] = series.str.replace(_SURROUNDING_WHITESPACE, "", regex=True)

    return result, []


def _describe_trim_whitespace(params: dict) -> str:
    suffix = " and collapsed repeated spaces" if params.get("collapse_internal", True) else ""
    return f"Trimmed whitespace from all text columns{suffix}"


def _validate_trim_whitespace(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"collapse_internal"}, "Trim whitespace")


# --------------------------------------------------------------------------------------
# remove_special_characters
# --------------------------------------------------------------------------------------


def _apply_remove_special_characters(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    keep_pattern = params.get("keep_pattern", DEFAULT_KEEP_PATTERN)
    replacement = params.get("replacement", "")
    pattern = _compile(f"[^{keep_pattern}]")
    result = frame.copy()

    for column in text_columns(result):
        result[column] = result[column].str.replace(pattern, replacement, regex=True)

    return result, []


def _describe_remove_special_characters(params: dict) -> str:
    replacement = params.get("replacement", "")
    action = f"replaced with '{replacement}'" if replacement else "removed"
    return f"Special characters {action} in all text columns"


def _validate_remove_special_characters(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"keep_pattern", "replacement"}, "Remove special characters")
    keep_pattern = params.get("keep_pattern", DEFAULT_KEEP_PATTERN)
    if not isinstance(keep_pattern, str) or not keep_pattern:
        raise InvalidStepError("Remove special characters needs a set of characters to keep.")
    _compile(f"[^{keep_pattern}]")


# --------------------------------------------------------------------------------------
# change_case
# --------------------------------------------------------------------------------------


def _apply_change_case(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    by_column = params.get("by_column", {})
    existing, missing = _present(frame, list(by_column))
    result = frame.copy()
    warnings_out = _missing_warning(missing)

    for column in existing:
        series = result[column]
        if not pd.api.types.is_string_dtype(series):
            warnings_out.append(f"'{column}' isn't a text column, so its letter case was left alone.")
            continue
        choice = by_column[column]
        if choice == "upper":
            result[column] = series.str.upper()
        elif choice == "lower":
            result[column] = series.str.lower()
        else:
            result[column] = series.str.title()

    return result, warnings_out


def _describe_change_case(params: dict) -> str:
    pairs = ", ".join(f"{column} -> {choice}case" for column, choice in params.get("by_column", {}).items())
    return f"Changed letter case ({pairs})"


def _validate_change_case(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"by_column"}, "Change letter case")
    by_column = params.get("by_column")
    if not isinstance(by_column, dict) or not by_column:
        raise InvalidStepError("Change letter case needs at least one column.")
    _require_columns_exist(list(by_column), columns, "Change letter case")
    for column, choice in by_column.items():
        if choice not in CASE_CHOICES:
            raise InvalidStepError(
                f"Change letter case: '{choice}' isn't valid for '{column}'. Choose one of {', '.join(CASE_CHOICES)}."
            )


# --------------------------------------------------------------------------------------
# find_replace
# --------------------------------------------------------------------------------------


def _apply_find_replace(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    existing, missing = _present(frame, params.get("columns", []))
    find = params.get("find", "")
    replacement = params.get("replace", "")
    use_regex = bool(params.get("regex", False))
    case_sensitive = bool(params.get("case_sensitive", True))

    # A literal search is escaped and still compiled, so both modes take the same
    # Python-re code path and case-insensitivity works identically for each.
    pattern = _compile(find if use_regex else re.escape(find), case_sensitive=case_sensitive)

    result = frame.copy()
    warnings_out = _missing_warning(missing)

    for column in existing:
        series = result[column]
        if not pd.api.types.is_string_dtype(series):
            warnings_out.append(
                f"'{column}' isn't a text column, so find & replace skipped it. "
                "Set it back to text first if you meant to edit its values."
            )
            continue
        result[column] = series.str.replace(pattern, replacement, regex=True)

    return result, warnings_out


def _describe_find_replace(params: dict) -> str:
    mode = "regex" if params.get("regex") else "text"
    sensitivity = "case-sensitive" if params.get("case_sensitive", True) else "case-insensitive"
    return (
        f"Replaced {mode} '{params.get('find', '')}' with '{params.get('replace', '')}' "
        f"({sensitivity}) in: {', '.join(params.get('columns', []))}"
    )


def _validate_find_replace(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"columns", "find", "replace", "regex", "case_sensitive"}, "Find & replace")
    _require_columns_exist(params.get("columns", []), columns, "Find & replace")

    find = params.get("find", "")
    if not isinstance(find, str) or not find:
        raise InvalidStepError("Find & replace needs something to search for.")
    if len(find) > MAX_REGEX_LENGTH:
        raise InvalidStepError(f"Find & replace: the search pattern is longer than {MAX_REGEX_LENGTH} characters.")
    if params.get("regex"):
        _compile(find)


# --------------------------------------------------------------------------------------
# fill_missing
# --------------------------------------------------------------------------------------


def _fill_strategy(settings: dict) -> str:
    return settings.get("strategy", "") if isinstance(settings, dict) else ""


def _apply_custom_fill(column: str, series: pd.Series, value: str) -> tuple[pd.Series, list[str]]:
    """Fills blanks with a literal the user typed, keeping a numeric column numeric where
    the literal allows it and saying so plainly when it doesn't."""
    if is_numeric_dtype(series):
        as_number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
        if pd.notna(as_number):
            return series.fillna(as_number), []
        return series.astype("string").fillna(value), [
            f"'{column}' was a numeric column, and filling it with '{value}' turned it into text."
        ]
    return series.astype("string").fillna(value), []


def _apply_fill_missing(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    by_column = params.get("by_column", {})
    existing, missing = _present(frame, list(by_column))
    warnings_out = _missing_warning(missing)
    result = frame.copy()

    # Text that only looks empty — a stray space, or the non-breaking space Excel exports
    # are full of — counts as missing here. Otherwise a column that reads as blank on
    # screen would report itself complete and quietly refuse every fill.
    for column in existing:
        result[column] = result[column].mask(blank_mask(result[column]))

    # Every drop_rows column is dropped together in one pass before any fill runs.
    # Otherwise "drop rows where A is blank, fill B with its mean" would compute a
    # different mean depending on which column happened to be processed first.
    drop_columns = [column for column in existing if _fill_strategy(by_column[column]) == "drop_rows"]
    if drop_columns:
        result = result.dropna(subset=drop_columns).reset_index(drop=True)

    for column in existing:
        settings = by_column[column]
        strategy = _fill_strategy(settings)
        if strategy == "drop_rows":
            continue

        series = result[column]
        if strategy == "zero":
            result[column] = series.fillna(0)
        elif strategy == "custom":
            filled, custom_warnings = _apply_custom_fill(column, series, settings.get("value", ""))
            result[column] = filled
            warnings_out.extend(custom_warnings)
        elif strategy in ("previous", "next"):
            # Row order carries the meaning here, which is why this runs on the frame as
            # the earlier steps left it rather than on the raw file: skipped rows and
            # removed duplicates change what "the row above" is.
            filled = series.ffill() if strategy == "previous" else series.bfill()
            still_blank = int(filled.isna().sum())
            if still_blank:
                edge = "top" if strategy == "previous" else "bottom"
                neighbour = "earlier" if strategy == "previous" else "later"
                warnings_out.append(
                    f"'{column}': {still_blank:,} blank value(s) at the {edge} of the table had no "
                    f"{neighbour} value to copy, so they are still blank."
                )
            result[column] = filled
        elif strategy in ("mean", "median"):
            if not is_numeric_dtype(series):
                warnings_out.append(
                    f"'{column}' isn't a numeric column, so its missing values couldn't be filled with the "
                    f"{strategy}. Set it to numeric first."
                )
                continue
            replacement = series.mean() if strategy == "mean" else series.median()
            if pd.isna(replacement):
                warnings_out.append(f"'{column}' has no values to average, so it was left as-is.")
                continue
            result[column] = series.fillna(replacement)

    return result, warnings_out


def _describe_fill_missing(params: dict) -> str:
    labels = {
        "zero": "fill with 0",
        "mean": "fill with the mean",
        "median": "fill with the median",
        "previous": "copy the value from the row above",
        "next": "copy the value from the row below",
        "drop_rows": "drop affected rows",
    }

    def phrase(settings: dict) -> str:
        strategy = _fill_strategy(settings)
        if strategy == "custom":
            return f"fill with '{settings.get('value', '')}'"
        return labels.get(strategy, strategy)

    pairs = ", ".join(f"{column}: {phrase(settings)}" for column, settings in params.get("by_column", {}).items())
    return f"Handled missing values ({pairs})"


def _validate_fill_missing(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"by_column"}, "Missing values")
    by_column = params.get("by_column")
    if not isinstance(by_column, dict) or not by_column:
        raise InvalidStepError("Missing values needs at least one column.")
    _require_columns_exist(list(by_column), columns, "Missing values")

    for column, settings in by_column.items():
        if not isinstance(settings, dict):
            raise InvalidStepError(f"Missing values: settings for '{column}' must be a mapping.")
        strategy = settings.get("strategy")
        if strategy not in FILL_STRATEGIES:
            raise InvalidStepError(
                f"Missing values: '{strategy}' isn't valid for '{column}'. "
                f"Choose one of {', '.join(FILL_STRATEGIES)}."
            )
        if strategy == "custom" and not (isinstance(settings.get("value"), str) and settings["value"]):
            raise InvalidStepError(f"Missing values: '{column}' needs a value to fill blanks with.")


# --------------------------------------------------------------------------------------
# drop_duplicates
# --------------------------------------------------------------------------------------


def _apply_drop_duplicates(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    requested = params.get("columns")
    keep = params.get("keep", "first")

    if requested:
        existing, missing = _present(frame, requested)
        warnings_out = _missing_warning(missing)
        subset = existing or None
    else:
        subset, warnings_out = None, []

    result = frame.drop_duplicates(subset=subset, keep=keep).reset_index(drop=True)
    return result, warnings_out


def _describe_drop_duplicates(params: dict) -> str:
    scope = f" based on {', '.join(params['columns'])}" if params.get("columns") else " (matching on every column)"
    kept = params.get("keep", "first")
    return f"Removed duplicate rows{scope}, keeping the {kept} of each"


def _validate_drop_duplicates(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"columns", "keep"}, "Remove duplicates")
    if params.get("columns"):
        _require_columns_exist(params["columns"], columns, "Remove duplicates")
    if params.get("keep", "first") not in DUPLICATE_KEEP_CHOICES:
        raise InvalidStepError("Remove duplicates: 'keep' must be 'first' or 'last'.")


# --------------------------------------------------------------------------------------
# Reshape actions — the three that change a table's grain rather than its contents
#
# These are the only actions whose output has a different meaning per row than their
# input, which is why the page never records them onto the table being cleaned: it saves
# each as a separate derived table instead. They are ordinary registry entries all the
# same, so the log line, the validator and the missing-column tolerance all come free.
# --------------------------------------------------------------------------------------


def _flatten_label(label) -> str:
    """Turns a pivot's MultiIndex column label into one plain string.

    Not cosmetic: DuckDB registration in the Chat with Data handoff rejects a frame whose
    column labels aren't unique strings, so a tuple label would break the export rather
    than merely look untidy.
    """
    if isinstance(label, tuple):
        return " / ".join(str(part) for part in label if str(part) != "")
    return str(label)


def _coerce_fill_value(value):
    """Reads a typed-in fill value as a number when it looks like one, else as text.

    Whole numbers come back as `int`, not `float`. A pivot of a whole-number column is
    itself whole-number (pandas' nullable `Int64`, since the cleaner parses numbers out of
    text), and such a column cannot hold `0.0` — the difference decides whether the fill
    lands or the column has to be widened.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _fill_pivot_gaps(frame: pd.DataFrame, labels: list[str], fill_value) -> list[str]:
    """Fills the empty cells a pivot leaves behind, in place, one column at a time.

    Done here rather than through `pivot_table(fill_value=...)`, which fills the whole
    result in one go and raises an unreadable `KeyError: dtype('float64')` out of pandas'
    internals whenever the value doesn't fit the column's dtype — `0` into a whole-number
    column being the obvious case a person would type.

    A column that can't hold the value is widened rather than left with gaps: to `Float64`
    when a whole-number column is asked to hold a fraction, otherwise to text. Both are
    reported back as warnings, since a numeric column turning into text changes what can
    be charted or summed downstream.
    """
    warnings_out: list[str] = []
    widened: list[str] = []
    to_text: list[str] = []

    for label in labels:
        column = frame[label]
        if not column.isna().any():
            continue
        try:
            frame[label] = column.fillna(fill_value)
            continue
        except (TypeError, ValueError):
            logger.info("Fill value %r doesn't fit column '%s' (%s).", fill_value, label, column.dtype)

        if isinstance(fill_value, (int, float)) and not isinstance(fill_value, bool) and is_numeric_dtype(column):
            frame[label] = column.astype("Float64").fillna(float(fill_value))
            widened.append(label)
        else:
            frame[label] = column.astype("string").fillna(str(fill_value))
            to_text.append(label)

    if widened:
        warnings_out.append(
            f"'{fill_value}' isn't a whole number, so these columns now hold decimals: {', '.join(widened)}."
        )
    if to_text:
        warnings_out.append(
            f"'{fill_value}' isn't a number, so these columns became text and can no longer be "
            f"totalled or charted: {', '.join(to_text)}."
        )
    return warnings_out


def _aggregation_label(column: str, function: str) -> str:
    return f"{function}_of_{column}"


def _apply_group_summarise(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    group_by = list(params.get("group_by") or [])
    aggregations = list(params.get("aggregations") or [])

    existing_keys, missing_keys = _present(frame, group_by)
    warnings_out = _missing_warning(missing_keys)

    requested: list[tuple[str, str]] = []
    for aggregation in aggregations:
        column = aggregation.get("column")
        function = aggregation.get("function")
        if column not in frame.columns:
            warnings_out.append(f"Skipped the {function} of '{column}': it isn't in the table any more.")
            continue
        if function in NUMERIC_ONLY_AGGREGATIONS and not is_numeric_dtype(frame[column]):
            warnings_out.append(
                f"'{column}' isn't a numeric column, so its {function} was skipped. Set it to numeric first."
            )
            continue
        requested.append((column, function))

    if not requested:
        warnings_out.append("None of the chosen aggregations could be applied, so the table is unchanged.")
        return frame, warnings_out

    # Deduped against the group keys as well as each other, because the aggregated columns
    # sit alongside those keys in the result and two identical labels would be unusable.
    labels = _dedupe_columns(existing_keys + [_aggregation_label(column, function) for column, function in requested])
    named = {
        label: (column, PANDAS_AGGREGATIONS[function])
        for label, (column, function) in zip(labels[len(existing_keys) :], requested)
    }

    if existing_keys:
        result = frame.groupby(existing_keys, dropna=False, observed=True).agg(**named).reset_index()
    else:
        # No group keys means one totals row. Grouping on a constant rather than calling
        # the aggregations directly keeps this on the identical pandas path to the keyed
        # case, so "sum of Amount" can never quietly mean two different things.
        constant = pd.Series(0, index=frame.index)
        result = frame.groupby(constant, observed=True).agg(**named).reset_index(drop=True)

    return result, warnings_out


def _describe_group_summarise(params: dict) -> str:
    pairs = ", ".join(
        f"{aggregation.get('function')} of {aggregation.get('column')}"
        for aggregation in params.get("aggregations") or []
    )
    keys = ", ".join(params.get("group_by") or [])
    scope = f" by {keys}" if keys else " across the whole table"
    return f"Summarised{scope}: {pairs}"


def _validate_group_summarise(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"group_by", "aggregations"}, "Summarise")

    group_by = params.get("group_by") or []
    if group_by:
        _require_columns_exist(group_by, columns, "Summarise")

    aggregations = params.get("aggregations")
    if not isinstance(aggregations, list) or not aggregations:
        raise InvalidStepError("Summarise needs at least one column to total up.")

    for aggregation in aggregations:
        if not isinstance(aggregation, dict):
            raise InvalidStepError("Summarise: every aggregation must be a mapping.")
        column = aggregation.get("column")
        function = aggregation.get("function")
        if column not in columns:
            raise InvalidStepError(f"Summarise: no such column: {column}.")
        if function not in AGGREGATION_FUNCTIONS:
            raise InvalidStepError(
                f"Summarise: '{function}' isn't a valid function for '{column}'. "
                f"Choose one of {', '.join(AGGREGATION_FUNCTIONS)}."
            )
        if column in group_by:
            raise InvalidStepError(
                f"Summarise: '{column}' is one of the group columns, so it can't also be totalled up."
            )


def _summarise_columns(params: dict) -> list[str]:
    aggregated = [aggregation.get("column") for aggregation in params.get("aggregations") or []]
    return list(params.get("group_by") or []) + [column for column in aggregated if isinstance(column, str)]


def _apply_pivot(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    index = list(params.get("index") or [])
    pivot_column = params.get("columns")
    value_column = params.get("values")
    function = params.get("function", "sum")

    existing_index, missing_index = _present(frame, index)
    warnings_out = _missing_warning(missing_index)

    absent = [column for column in (pivot_column, value_column) if column not in frame.columns]
    if absent or not existing_index:
        warnings_out.append("Pivot needs its row, column and value columns, so the table is unchanged.")
        return frame, warnings_out
    if function in NUMERIC_ONLY_AGGREGATIONS and not is_numeric_dtype(frame[value_column]):
        warnings_out.append(
            f"'{value_column}' isn't a numeric column, so it can't be pivoted with {function}. "
            f"Set it to numeric first, or pivot with count."
        )
        return frame, warnings_out

    result = pd.pivot_table(
        frame,
        index=existing_index,
        columns=pivot_column,
        values=value_column,
        aggfunc=PANDAS_AGGREGATIONS[function],
        dropna=False,
        observed=True,
    ).reset_index()
    result.columns = _dedupe_columns([_flatten_label(label) for label in result.columns])

    fill_value = _coerce_fill_value(params.get("fill_value"))
    if fill_value is not None:
        # Everything past the row columns, taken by position: `_dedupe_columns` may have
        # renamed a spread-across column that clashed with a row column's name.
        warnings_out.extend(_fill_pivot_gaps(result, list(result.columns)[len(existing_index) :], fill_value))

    if len(result.columns) > MAX_PIVOT_COLUMNS:
        warnings_out.append(
            f"This pivot produced {len(result.columns):,} columns. Anything past about "
            f"{MAX_PIVOT_COLUMNS:,} is hard to read and slow to export — pick a column with fewer "
            f"distinct values to spread across."
        )
    return result, warnings_out


def _describe_pivot(params: dict) -> str:
    return (
        f"Pivoted {params.get('function')} of {params.get('values')} with "
        f"{', '.join(params.get('index') or [])} down the side and {params.get('columns')} across the top"
    )


def _validate_pivot(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"index", "columns", "values", "function", "fill_value"}, "Pivot")
    _require_columns_exist(params.get("index") or [], columns, "Pivot")

    index = params["index"]
    pivot_column = params.get("columns")
    value_column = params.get("values")

    for role, column in (("column to spread across the top", pivot_column), ("value column", value_column)):
        if column not in columns:
            raise InvalidStepError(f"Pivot: no such {role}: {column}.")
    if pivot_column == value_column:
        raise InvalidStepError("Pivot: the column spread across the top can't also be the value column.")
    if pivot_column in index or value_column in index:
        raise InvalidStepError("Pivot: a row column can't also be the column spread across the top or the value.")
    if params.get("function") not in AGGREGATION_FUNCTIONS:
        raise InvalidStepError(
            f"Pivot: '{params.get('function')}' isn't a valid function. Choose one of {', '.join(AGGREGATION_FUNCTIONS)}."
        )


def _pivot_columns(params: dict) -> list[str]:
    named = [params.get("columns"), params.get("values")]
    return list(params.get("index") or []) + [column for column in named if isinstance(column, str)]


def _apply_unpivot(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    variable_name = params.get("variable_name") or DEFAULT_VARIABLE_NAME
    value_name = params.get("value_name") or DEFAULT_VALUE_NAME

    existing_ids, missing_ids = _present(frame, list(params.get("id_columns") or []))
    warnings_out = _missing_warning(missing_ids)

    requested_values = list(params.get("value_columns") or [])
    if requested_values:
        existing_values, missing_values = _present(frame, requested_values)
        warnings_out.extend(_missing_warning(missing_values))
    else:
        existing_values = [column for column in frame.columns if column not in existing_ids]

    if not existing_values:
        warnings_out.append("Unpivot had no value columns left to stack up, so the table is unchanged.")
        return frame, warnings_out

    result = frame.melt(
        id_vars=existing_ids,
        value_vars=existing_values,
        var_name=variable_name,
        value_name=value_name,
    ).reset_index(drop=True)

    # Stacking columns of different types lands them in one object column, which has no
    # meaningful type downstream and which DuckDB can't register. Text is the honest
    # answer there; a single-type stack keeps whatever type it already had.
    if result[value_name].dtype == "object":
        result[value_name] = result[value_name].astype("string")
    return result, warnings_out


def _describe_unpivot(params: dict) -> str:
    stacked = ", ".join(params.get("value_columns") or []) or "every other column"
    return (
        f"Unpivoted {stacked} into '{params.get('variable_name') or DEFAULT_VARIABLE_NAME}' and "
        f"'{params.get('value_name') or DEFAULT_VALUE_NAME}', keyed by {', '.join(params.get('id_columns') or [])}"
    )


def _validate_unpivot(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"id_columns", "value_columns", "variable_name", "value_name"}, "Unpivot")
    _require_columns_exist(params.get("id_columns") or [], columns, "Unpivot")

    id_columns = params["id_columns"]
    value_columns = params.get("value_columns") or []
    if value_columns:
        _require_columns_exist(value_columns, columns, "Unpivot")
        overlap = sorted(set(id_columns) & set(value_columns))
        if overlap:
            raise InvalidStepError(
                f"Unpivot: {', '.join(overlap)} can't be both a column to keep and a column to stack up."
            )
    elif not [column for column in columns if column not in id_columns]:
        raise InvalidStepError("Unpivot: every column was kept as-is, so there is nothing left to stack up.")

    variable_name = params.get("variable_name") or DEFAULT_VARIABLE_NAME
    value_name = params.get("value_name") or DEFAULT_VALUE_NAME
    if variable_name == value_name:
        raise InvalidStepError("Unpivot: the name and value columns need different names.")
    clashing = sorted({variable_name, value_name} & set(id_columns))
    if clashing:
        raise InvalidStepError(f"Unpivot: {', '.join(clashing)} is already a column you're keeping — pick another name.")


def _unpivot_columns(params: dict) -> list[str]:
    return list(params.get("id_columns") or []) + list(params.get("value_columns") or [])


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


def _columns_param(params: dict) -> list[str]:
    return list(params.get("columns") or [])


def _by_column_param(params: dict) -> list[str]:
    return list(params.get("by_column") or {})


STEP_REGISTRY: dict[str, StepSpec] = {
    "skip_rows": StepSpec(
        action="skip_rows",
        label="Skip rows",
        apply=_apply_skip_rows,
        describe=_describe_skip_rows,
        validate=_validate_skip_rows,
        required_columns=lambda params: [],
        record=RecordPolicy.REPLACE,
        pin_rank=0,
    ),
    "set_column_types": StepSpec(
        action="set_column_types",
        label="Set column types",
        apply=_apply_set_column_types,
        describe=_describe_set_column_types,
        validate=_validate_set_column_types,
        required_columns=_by_column_param,
        record=RecordPolicy.UPDATE_PER_COLUMN,
        pin_rank=1,
    ),
    "remove_empty_rows": StepSpec(
        action="remove_empty_rows",
        label="Remove empty rows",
        apply=_apply_remove_empty_rows,
        describe=_describe_remove_empty_rows,
        validate=_validate_remove_empty_rows,
        required_columns=_columns_param,
    ),
    "delete_columns": StepSpec(
        action="delete_columns",
        label="Delete columns",
        apply=_apply_delete_columns,
        describe=_describe_delete_columns,
        validate=_validate_delete_columns,
        required_columns=_columns_param,
    ),
    "rename_columns": StepSpec(
        action="rename_columns",
        label="Rename columns",
        apply=_apply_rename_columns,
        describe=_describe_rename_columns,
        validate=_validate_rename_columns,
        required_columns=lambda params: list(params.get("renames") or {}),
    ),
    "fix_numeric_text": StepSpec(
        action="fix_numeric_text",
        label="Fix numbers stored as text",
        apply=_apply_fix_numeric_text,
        describe=_describe_fix_numeric_text,
        validate=_validate_fix_numeric_text,
        required_columns=_columns_param,
    ),
    "round_numbers": StepSpec(
        action="round_numbers",
        label="Round numbers",
        apply=_apply_round_numbers,
        describe=_describe_round_numbers,
        validate=_validate_round_numbers,
        required_columns=_columns_param,
    ),
    "trim_whitespace": StepSpec(
        action="trim_whitespace",
        label="Trim whitespace",
        apply=_apply_trim_whitespace,
        describe=_describe_trim_whitespace,
        validate=_validate_trim_whitespace,
        required_columns=lambda params: [],
        record=RecordPolicy.REPLACE,
    ),
    "remove_special_characters": StepSpec(
        action="remove_special_characters",
        label="Remove special characters",
        apply=_apply_remove_special_characters,
        describe=_describe_remove_special_characters,
        validate=_validate_remove_special_characters,
        required_columns=lambda params: [],
        record=RecordPolicy.REPLACE,
    ),
    "change_case": StepSpec(
        action="change_case",
        label="Change letter case",
        apply=_apply_change_case,
        describe=_describe_change_case,
        validate=_validate_change_case,
        required_columns=_by_column_param,
        record=RecordPolicy.UPDATE_PER_COLUMN,
    ),
    "find_replace": StepSpec(
        action="find_replace",
        label="Find & replace",
        apply=_apply_find_replace,
        describe=_describe_find_replace,
        validate=_validate_find_replace,
        required_columns=_columns_param,
    ),
    "fill_missing": StepSpec(
        action="fill_missing",
        label="Missing values",
        apply=_apply_fill_missing,
        describe=_describe_fill_missing,
        validate=_validate_fill_missing,
        required_columns=_by_column_param,
        record=RecordPolicy.UPDATE_PER_COLUMN,
    ),
    "drop_duplicates": StepSpec(
        action="drop_duplicates",
        label="Remove duplicates",
        apply=_apply_drop_duplicates,
        describe=_describe_drop_duplicates,
        validate=_validate_drop_duplicates,
        required_columns=_columns_param,
    ),
    "group_summarise": StepSpec(
        action="group_summarise",
        label="Group & total",
        apply=_apply_group_summarise,
        describe=_describe_group_summarise,
        validate=_validate_group_summarise,
        required_columns=_summarise_columns,
    ),
    "pivot": StepSpec(
        action="pivot",
        label="Pivot",
        apply=_apply_pivot,
        describe=_describe_pivot,
        validate=_validate_pivot,
        required_columns=_pivot_columns,
    ),
    "unpivot": StepSpec(
        action="unpivot",
        label="Unpivot",
        apply=_apply_unpivot,
        describe=_describe_unpivot,
        validate=_validate_unpivot,
        required_columns=_unpivot_columns,
    ),
}

# The actions that produce a new table rather than editing one in place. The Data Cleaner
# page saves these as derived tables; nothing else may record them onto a table's recipe.
RESHAPE_ACTIONS = ["group_summarise", "pivot", "unpivot"]


def get_spec(action: str) -> StepSpec:
    """Returns the StepSpec for `action`.

    Raises:
        InvalidStepError: if no such action is registered.
    """
    try:
        return STEP_REGISTRY[action]
    except KeyError as error:
        logger.error("Unknown cleaning action requested: %s", action)
        raise InvalidStepError(f"'{action}' isn't a known cleaning action.") from error
