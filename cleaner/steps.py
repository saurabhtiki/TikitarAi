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
    parse_datetime_series,
    parse_numeric_series,
    text_columns,
)

logger = logging.getLogger(__name__)

MAX_REGEX_LENGTH = 500
_WARNING_SAMPLE_SIZE = 5

CASE_CHOICES = ["upper", "lower", "title"]
FILL_STRATEGIES = ["zero", "mean", "median", "unknown", "drop_rows"]
DUPLICATE_KEEP_CHOICES = ["first", "last"]

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
    is_blank = subset.isna()
    if blank_counts:
        text_like = subset.apply(lambda series: series.astype("string").str.strip().eq(""))
        is_blank = is_blank | text_like.fillna(False)

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


def _apply_fill_missing(frame: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, list[str]]:
    by_column = params.get("by_column", {})
    existing, missing = _present(frame, list(by_column))
    warnings_out = _missing_warning(missing)
    result = frame.copy()

    # Every drop_rows column is dropped together in one pass before any fill runs.
    # Otherwise "drop rows where A is blank, fill B with its mean" would compute a
    # different mean depending on which column happened to be processed first.
    drop_columns = [column for column in existing if by_column[column] == "drop_rows"]
    if drop_columns:
        result = result.dropna(subset=drop_columns).reset_index(drop=True)

    for column in existing:
        strategy = by_column[column]
        if strategy == "drop_rows":
            continue

        series = result[column]
        if strategy == "zero":
            result[column] = series.fillna(0)
        elif strategy == "unknown":
            result[column] = series.astype("string").fillna("Unknown")
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
        "unknown": "fill with 'Unknown'",
        "drop_rows": "drop affected rows",
    }
    pairs = ", ".join(
        f"{column}: {labels.get(strategy, strategy)}" for column, strategy in params.get("by_column", {}).items()
    )
    return f"Handled missing values ({pairs})"


def _validate_fill_missing(params: dict, columns: list[str]) -> None:
    _require_keys(params, {"by_column"}, "Missing values")
    by_column = params.get("by_column")
    if not isinstance(by_column, dict) or not by_column:
        raise InvalidStepError("Missing values needs at least one column.")
    _require_columns_exist(list(by_column), columns, "Missing values")
    for column, strategy in by_column.items():
        if strategy not in FILL_STRATEGIES:
            raise InvalidStepError(
                f"Missing values: '{strategy}' isn't valid for '{column}'. "
                f"Choose one of {', '.join(FILL_STRATEGIES)}."
            )


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
}


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
