"""Column-level operations: change type, drop, rename, split, take part of a code.

`change_dtype` is the most important entry in the whole catalog and the reason it is listed
first. `cleaner.loaders` reads every uploaded cell as text on purpose, so that a leading
zero in an invoice number survives the trip. The cost is that `2,500` is the *word* "2,500"
until somebody says otherwise: a join on it matches by string, a sum of it concatenates, and
a date difference is impossible. So before most useful work, one step declares what a column
actually is — visibly and replayably, never silently.
"""

import logging

import pandas as pd

from transform.exceptions import InvalidStepParamsError
from transform.ops_common import (
    missing_warning,
    present_columns,
    require_columns,
    source_frame,
    to_datetime,
    to_numeric,
    unique_column_name,
)

logger = logging.getLogger(__name__)

#: The types a column can be changed to. Deliberately shorter than pandas' list: these are
#: the distinctions that change what a later step can do, and nothing else.
DTYPE_CHOICES = ["number", "whole number", "date", "text", "true/false"]

#: How a split column's pieces are handed back.
SPLIT_MODES = ["all pieces into new columns", "one piece only"]


# --------------------------------------------------------------------------------------
# change_dtype
# --------------------------------------------------------------------------------------


def apply_change_dtype(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Converts the chosen columns to one type, blanking whatever won't convert."""
    frame = source_frame(frames_by_role)
    requested = [str(column) for column in params.get("columns", [])]
    target_type = params.get("target_type", "number")

    existing, missing = present_columns(frame, requested)
    warnings_out = missing_warning(missing)
    result = frame.copy()

    for column in existing:
        converted, conversion_warnings = _convert(result[column], column, target_type)
        result[column] = converted
        warnings_out.extend(conversion_warnings)

    return result, warnings_out


def _convert(series: pd.Series, column: str, target_type: str) -> tuple[pd.Series, list[str]]:
    """One column converted to one type, with a warning for what wouldn't go."""
    if target_type == "number":
        return to_numeric(series, column)

    if target_type == "whole number":
        numbers, warnings_out = to_numeric(series, column)
        # Int64, not int64: the capital-I type holds blanks, and a column with one
        # unreadable value would otherwise fail outright rather than lose that one cell.
        return numbers.round().astype("Int64"), warnings_out

    if target_type == "date":
        return to_datetime(series, column)

    if target_type == "true/false":
        text = series.astype("string").str.strip().str.lower()
        truthy = text.isin(["true", "yes", "y", "1", "t"])
        falsy = text.isin(["false", "no", "n", "0", "f"])
        unreadable = int((~truthy & ~falsy & series.notna()).sum())
        result = pd.Series(pd.NA, index=series.index, dtype="boolean")
        result[truthy] = True
        result[falsy] = False
        warnings_out = (
            [
                f"'{column}': {unreadable:,} value(s) were neither true nor false and are "
                f"now blank."
            ]
            if unreadable
            else []
        )
        return result, warnings_out

    # text
    return series.astype("string"), []


def describe_change_dtype(step: dict) -> str:
    params = step.get("params", {})
    columns = ", ".join(params.get("columns", []))
    return f"Changed {columns} to {params.get('target_type', 'number')}"


def validate_change_dtype(columns_by_role: dict, params: dict) -> None:
    if not params.get("columns"):
        raise InvalidStepParamsError("Choose at least one column to change.")
    if params.get("target_type") not in DTYPE_CHOICES:
        raise InvalidStepParamsError(f"'{params.get('target_type')}' isn't a type this step knows.")


def required_change_dtype(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# drop_columns
# --------------------------------------------------------------------------------------


def apply_drop_columns(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Removes the chosen columns, skipping any that have already gone."""
    frame = source_frame(frames_by_role)
    existing, missing = present_columns(frame, [str(column) for column in params.get("columns", [])])
    return frame.drop(columns=existing), missing_warning(missing)


def describe_drop_columns(step: dict) -> str:
    return f"Dropped column(s): {', '.join(step.get('params', {}).get('columns', []))}"


def validate_drop_columns(columns_by_role: dict, params: dict) -> None:
    chosen = [str(column) for column in params.get("columns", [])]
    if not chosen:
        raise InvalidStepParamsError("Choose at least one column to drop.")
    available = columns_by_role.get("source") or []
    if available and not set(available) - set(chosen):
        raise InvalidStepParamsError("That would drop every column in the table.")


def required_drop_columns(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# rename_column
# --------------------------------------------------------------------------------------


def apply_rename_column(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Renames one column.

    One at a time rather than the cleaner's bulk `renames` mapping, because the generic
    form renderer draws one widget per parameter and a mapping needs a bespoke editor. Two
    renames are two steps, which also reads better in the step list.
    """
    frame = source_frame(frames_by_role)
    old_name = str(params.get("column", ""))
    new_name = str(params.get("new_name", "")).strip()

    existing, missing = present_columns(frame, [old_name])
    if missing:
        return frame.copy(), missing_warning(missing)
    if new_name in frame.columns and new_name != old_name:
        raise InvalidStepParamsError(
            f"This table already has a column called '{new_name}'. Pick another name."
        )
    return frame.rename(columns={old_name: new_name}), []


def describe_rename_column(step: dict) -> str:
    params = step.get("params", {})
    return f"Renamed {params.get('column')} to {params.get('new_name')}"


def validate_rename_column(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the column to rename.")
    if not str(params.get("new_name", "")).strip():
        raise InvalidStepParamsError("Type the new column name.")


def required_rename_column(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# split_column
# --------------------------------------------------------------------------------------


def apply_split_column(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Splits one text column on a separator.

    Either every piece becomes its own column (`name_1`, `name_2`, ...), or one chosen
    piece becomes a single new column — the common case being "take what's before the dash".
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    separator = str(params.get("separator", ""))
    mode = params.get("mode", SPLIT_MODES[0])

    require_columns(frame, [column], "Split a column")
    if not separator:
        raise InvalidStepParamsError("Type the separator to split on, for example a comma.")

    result = frame.copy()
    text = result[column].astype("string")
    # regex=False: a user typing "." means a full stop, not "any character".
    pieces = text.str.split(separator, regex=False)

    if mode == "one piece only":
        wanted = int(params.get("piece_number", 1))
        if wanted < 1:
            raise InvalidStepParamsError("The piece number starts at 1.")
        index = wanted - 1
        extracted = pieces.str[index] if index >= 0 else pieces.str[index]
        new_name = unique_column_name(result, str(params.get("new_name") or f"{column}_{wanted}"))
        result[new_name] = extracted.str.strip()
        short = int(extracted.isna().sum() - text.isna().sum())
        warnings_out = (
            [f"{short:,} row(s) had fewer than {wanted} piece(s) and are blank."] if short > 0 else []
        )
        return result, warnings_out

    widest = int(pieces.dropna().map(len).max()) if pieces.notna().any() else 0
    if widest <= 1:
        return result, [
            f"'{separator}' wasn't found in '{column}', so nothing was split."
        ]

    expanded = text.str.split(separator, n=widest - 1, expand=True, regex=False)
    for position in range(widest):
        new_name = unique_column_name(result, f"{column}_{position + 1}")
        result[new_name] = expanded[position].str.strip()
    return result, [f"Split '{column}' into {widest} column(s)."]


def describe_split_column(step: dict) -> str:
    params = step.get("params", {})
    if params.get("mode") == "one piece only":
        return (
            f"Split {params.get('column')} on '{params.get('separator')}' and kept piece "
            f"{params.get('piece_number', 1)}"
        )
    return f"Split {params.get('column')} on '{params.get('separator')}' into new columns"


def validate_split_column(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the column to split.")
    if not str(params.get("separator", "")):
        raise InvalidStepParamsError("Type the separator to split on, for example a comma.")


def required_split_column(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# extract_by_position
# --------------------------------------------------------------------------------------


def apply_extract_by_position(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Pulls a fixed run of characters out of a code column, counted by position.

    `split_column` needs something to split *on*; a code like `INV000123` has no separator,
    so the only way to read the `INV` out of it is "the first 3 characters". Both numbers are
    1-based, so "start at 1, length 3" reads the way a person says it.

    A value shorter than the asked-for start has nothing to give and comes out blank, with a
    count of how many rows that happened to.
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Take part of a code")

    start = _position(params.get("start", 1), "Start at character")
    length = _position(params.get("length", 1), "How many characters")

    result = frame.copy()
    text = result[column].astype("string")
    pieces = text.str.slice(start - 1, start - 1 + length)
    # An empty answer is not the same as a short one being trimmed: it means the value ran
    # out before the start position, so it reads better as a blank than as "".
    pieces = pieces.where(pieces.str.len() > 0)

    new_name = unique_column_name(
        result, str(params.get("new_column") or f"{column} {start}-{start + length - 1}")
    )
    result[new_name] = pieces

    too_short = int(pieces.isna().sum() - text.isna().sum())
    warnings_out = (
        [f"{too_short:,} row(s) were shorter than {start} character(s) and are blank."]
        if too_short > 0
        else []
    )
    return result, warnings_out


def _position(value: object, label: str) -> int:
    """A 1-based character position as a whole number of at least 1.

    Raises:
        InvalidStepParamsError: if it isn't a whole number, or is below 1. Counting from
            zero here would quietly shift every answer by one character, so a 0 is refused
            rather than forgiven.
    """
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise InvalidStepParamsError(
            f"'{label}' needs a whole number, but got '{value}'."
        ) from error
    if number < 1:
        raise InvalidStepParamsError(f"'{label}' starts at 1, so {number} can't be used.")
    return number


def describe_extract_by_position(step: dict) -> str:
    params = step.get("params", {})
    return (
        f"Took {params.get('length', 1)} character(s) of {params.get('column')} from "
        f"position {params.get('start', 1)}"
    )


def validate_extract_by_position(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the column holding the codes.")
    _position(params.get("start", 1), "Start at character")
    _position(params.get("length", 1), "How many characters")


def required_extract_by_position(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}
