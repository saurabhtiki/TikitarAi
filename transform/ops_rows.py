"""Row-level operations: filter, sort, fill blanks, remove duplicates.

`filter_rows` is the one this page could least do without. You filter *before* you
aggregate — "total this year's sales by customer" is a filter and a group — and nothing in
the Data Cleaner filters at all.

`fill_missing` and `drop_duplicates` delegate to the executors already written and tested in
`cleaner.steps`. They are here because a join is exactly what creates blanks (an unmatched
row) and duplicate rows (a many-to-many key), so a page that can join but not clean up after
itself would send the user somewhere else mid-task.
"""

import logging

import pandas as pd

from cleaner.profiling import blank_mask
from cleaner.steps import STEP_REGISTRY
from transform.exceptions import InvalidStepParamsError
from transform.ops_common import (
    missing_warning,
    present_columns,
    require_columns,
    source_frame,
    to_numeric,
)

logger = logging.getLogger(__name__)

#: The comparisons a filter offers. Worded as a person would say them rather than as
#: operators, because the dropdown is read left-to-right with the column name in front:
#: "amount | is greater than | 1000".
COMPARISONS = [
    "is",
    "is not",
    "is greater than",
    "is greater than or equal to",
    "is less than",
    "is less than or equal to",
    "contains",
    "does not contain",
    "starts with",
    "ends with",
    "is one of",
    "is blank",
    "is not blank",
]

#: Comparisons that need no value typed alongside them.
_VALUELESS = {"is blank", "is not blank"}

#: Comparisons that compare as numbers rather than as text.
_NUMERIC_COMPARISONS = {
    "is greater than",
    "is greater than or equal to",
    "is less than",
    "is less than or equal to",
}

KEEP_CHOICES = ["first", "last"]

FILL_STRATEGIES = ["a value I type", "zero", "the value above", "the value below", "drop those rows"]

#: This page's wording -> the strategy name `cleaner.steps._apply_fill_missing` expects.
_FILL_STRATEGY_NAMES = {
    "a value I type": "custom",
    "zero": "zero",
    "the value above": "previous",
    "the value below": "next",
    "drop those rows": "drop_rows",
}


# --------------------------------------------------------------------------------------
# filter_rows
# --------------------------------------------------------------------------------------


def apply_filter_rows(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Keeps (or removes) the rows matching one comparison."""
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    comparison = params.get("comparison", "is")
    value = params.get("value", "")

    require_columns(frame, [column], "Filter rows")

    mask, warnings_out = comparison_mask(frame, column, comparison, value)
    if params.get("remove_matching"):
        mask = ~mask

    result = frame[mask].reset_index(drop=True)
    if result.empty and not frame.empty:
        warnings_out.append("No rows matched, so this table is now empty.")
    return result, warnings_out


def comparison_mask(
    frame: pd.DataFrame, column: str, comparison: str, value: object
) -> tuple[pd.Series, list[str]]:
    """The True/False row mask for one comparison, plus any conversion warnings."""
    series = frame[column]

    if comparison == "is blank":
        return blank_mask(series), []
    if comparison == "is not blank":
        return ~blank_mask(series), []

    if comparison in _NUMERIC_COMPARISONS:
        numbers, warnings_out = to_numeric(series, column)
        try:
            threshold = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError) as error:
            raise InvalidStepParamsError(
                f"'{value}' isn't a number, so '{comparison}' can't compare against it."
            ) from error
        comparisons = {
            "is greater than": numbers > threshold,
            "is greater than or equal to": numbers >= threshold,
            "is less than": numbers < threshold,
            "is less than or equal to": numbers <= threshold,
        }
        return comparisons[comparison].fillna(False).astype(bool), warnings_out

    text = series.astype("string").str.strip()
    wanted = str(value).strip()

    if comparison == "is one of":
        options = [piece.strip() for piece in wanted.split(",") if piece.strip()]
        if not options:
            raise InvalidStepParamsError(
                "Type the values to match, separated by commas - for example: North, South"
            )
        return text.str.casefold().isin([option.casefold() for option in options]).fillna(False), []

    # Case-insensitive throughout: a user filtering for "north" means the rows that say
    # "North", and requiring them to match capitalisation would be a bug report.
    lowered = text.str.casefold()
    target = wanted.casefold()
    masks = {
        "is": lowered == target,
        "is not": lowered != target,
        "contains": lowered.str.contains(target, regex=False, na=False),
        "does not contain": ~lowered.str.contains(target, regex=False, na=False),
        "starts with": lowered.str.startswith(target, na=False),
        "ends with": lowered.str.endswith(target, na=False),
    }
    if comparison not in masks:
        raise InvalidStepParamsError(f"'{comparison}' isn't a comparison this step knows.")
    return masks[comparison].fillna(False).astype(bool), []


def describe_filter_rows(step: dict) -> str:
    params = step.get("params", {})
    verb = "Removed" if params.get("remove_matching") else "Kept"
    comparison = params.get("comparison", "is")
    tail = "" if comparison in _VALUELESS else f" {params.get('value', '')}"
    return f"{verb} rows where {params.get('column')} {comparison}{tail}"


def validate_filter_rows(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the column to filter on.")
    comparison = params.get("comparison", "is")
    if comparison not in COMPARISONS:
        raise InvalidStepParamsError(f"'{comparison}' isn't a comparison this step knows.")
    if comparison not in _VALUELESS and not str(params.get("value", "")).strip():
        raise InvalidStepParamsError("Type the value to compare against.")


def required_filter_rows(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# sort_rows
# --------------------------------------------------------------------------------------


def apply_sort_rows(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Sorts by one or more columns.

    A numeric-looking text column is sorted **as numbers**, so 9 comes before 10 rather
    than after 1. Sorting `1, 10, 9` alphabetically is technically correct and never what
    anybody wanted.
    """
    frame = source_frame(frames_by_role)
    requested = [str(column) for column in params.get("columns", [])]
    existing, missing = present_columns(frame, requested)
    warnings_out = missing_warning(missing)

    if not existing:
        return frame.copy(), warnings_out

    ascending = bool(params.get("ascending", True))
    sort_keys = pd.DataFrame(index=frame.index)
    for column in existing:
        numbers, _ = to_numeric(frame[column], column)
        readable = numbers.notna().sum()
        filled = int(frame[column].notna().sum())
        # Only treat it as numeric when essentially all of it reads as numbers; one
        # numeric-looking value in a text column must not reorder the whole table.
        if filled and readable >= filled:
            sort_keys[column] = numbers
        else:
            sort_keys[column] = frame[column].astype("string").str.casefold()

    order = sort_keys.sort_values(by=existing, ascending=ascending, kind="stable").index
    return frame.loc[order].reset_index(drop=True), warnings_out


def describe_sort_rows(step: dict) -> str:
    params = step.get("params", {})
    direction = "A-Z / smallest first" if params.get("ascending", True) else "Z-A / largest first"
    return f"Sorted by {', '.join(params.get('columns', []))} ({direction})"


def validate_sort_rows(columns_by_role: dict, params: dict) -> None:
    if not params.get("columns"):
        raise InvalidStepParamsError("Choose at least one column to sort by.")


def required_sort_rows(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# fill_missing — delegates to cleaner.steps
# --------------------------------------------------------------------------------------


def apply_fill_missing(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Fills blanks in the chosen columns, all with the same strategy.

    The cleaner's own step takes a per-column mapping, which needs a bespoke editor to fill
    in. Here every chosen column gets the same treatment, which covers the case this page
    actually creates — the blanks a join left behind, which are all the same kind of blank —
    and needs only two ordinary widgets.
    """
    frame = source_frame(frames_by_role)
    requested = [str(column) for column in params.get("columns", [])]
    strategy = _FILL_STRATEGY_NAMES.get(params.get("strategy", ""), "custom")

    settings: dict = {"strategy": strategy}
    if strategy == "custom":
        settings["value"] = str(params.get("value", ""))

    return STEP_REGISTRY["fill_missing"].apply(
        frame, {"by_column": {column: dict(settings) for column in requested}}
    )


def describe_fill_missing(step: dict) -> str:
    params = step.get("params", {})
    strategy = params.get("strategy", "")
    detail = f" '{params.get('value', '')}'" if strategy == "a value I type" else ""
    return f"Filled blanks in {', '.join(params.get('columns', []))} with {strategy}{detail}"


def validate_fill_missing(columns_by_role: dict, params: dict) -> None:
    if not params.get("columns"):
        raise InvalidStepParamsError("Choose at least one column to fill.")
    if params.get("strategy") not in FILL_STRATEGIES:
        raise InvalidStepParamsError(f"'{params.get('strategy')}' isn't a fill this step knows.")


def required_fill_missing(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# drop_duplicates — delegates to cleaner.steps
# --------------------------------------------------------------------------------------


def apply_drop_duplicates(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Removes repeated rows, matching on chosen columns or on the whole row."""
    frame = source_frame(frames_by_role)
    before = len(frame)
    result, warnings_out = STEP_REGISTRY["drop_duplicates"].apply(
        frame,
        {
            "columns": [str(column) for column in params.get("columns", [])],
            "keep": params.get("keep", "first"),
        },
    )
    removed = before - len(result)
    if removed:
        warnings_out = list(warnings_out) + [f"Removed {removed:,} duplicate row(s)."]
    return result, warnings_out


def describe_drop_duplicates(step: dict) -> str:
    params = step.get("params", {})
    columns = params.get("columns") or []
    scope = f" matching on {', '.join(columns)}" if columns else " matching on every column"
    return f"Removed duplicate rows{scope}, keeping the {params.get('keep', 'first')}"


def validate_drop_duplicates(columns_by_role: dict, params: dict) -> None:
    if params.get("keep", "first") not in KEEP_CHOICES:
        raise InvalidStepParamsError("Keep must be the first or the last of each duplicate.")


def required_drop_duplicates(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}
