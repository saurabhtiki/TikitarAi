"""Helpers every operation executor shares.

An executor's signature is `(frames_by_role, params) -> (new_frame, warnings)`, where
`frames_by_role` maps an `InputRole.name` to a DataFrame (or, for a `multi` role, a list of
them). That is one level richer than the cleaner's `(frame, params)` because a step here can
read two tables; everything else about the contract is the same, including the rule that an
executor **always returns a new frame** and never mutates its input — pandas 3 enforces
copy-on-write, so `inplace=` and chained assignment are not options anyway.

Warnings are how an executor reports something the user should know without failing the
step: a column that had gone missing, values that wouldn't convert, rows that multiplied in
a join. A step that genuinely cannot proceed raises `InvalidStepParamsError` instead.
"""

import logging

import numpy as np
import pandas as pd

from cleaner.profiling import blank_mask
from transform.exceptions import InvalidStepParamsError

logger = logging.getLogger(__name__)

#: How many example values a conversion warning quotes. Enough to recognise the pattern
#: ("they all have a currency symbol"), short enough to read in a toast.
WARNING_SAMPLE_SIZE = 5

#: Aggregations offered wherever a step totals something. Mirrors
#: `cleaner.steps.AGGREGATION_FUNCTIONS` so the two pages name the same maths the same way.
AGGREGATIONS = ["sum", "mean", "median", "min", "max", "count", "count_distinct", "first", "last"]

PANDAS_AGGREGATIONS = {
    "sum": "sum",
    "mean": "mean",
    "median": "median",
    "min": "min",
    "max": "max",
    "count": "count",
    "count_distinct": "nunique",
    "first": "first",
    "last": "last",
}

#: The three that need numbers. min/max/first/last order text perfectly sensibly, so they
#: are deliberately absent.
NUMERIC_ONLY_AGGREGATIONS = {"sum", "mean", "median"}


def source_frame(frames_by_role: dict, role: str = "source") -> pd.DataFrame:
    """The DataFrame for one role.

    Raises:
        InvalidStepParamsError: if the role is absent. This is a programming error rather
            than a user one — the pipeline resolves every declared role before calling an
            executor — so the message names the role rather than trying to be friendly.
    """
    frame = frames_by_role.get(role)
    if not isinstance(frame, pd.DataFrame):
        raise InvalidStepParamsError(f"This step needs a table for '{role}'.")
    return frame


def present_columns(frame: pd.DataFrame, columns: list[str]) -> tuple[list[str], list[str]]:
    """Splits requested columns into those the table has and those it doesn't.

    Steps work on what survives and report the rest rather than failing outright — a column
    may legitimately have been dropped or renamed by an earlier step, and one stale
    reference should not stop the run.
    """
    held = {str(column) for column in frame.columns}
    existing = [column for column in columns if column in held]
    missing = [column for column in columns if column not in held]
    return existing, missing


def missing_warning(missing: list[str]) -> list[str]:
    """The standard "skipped these" warning, or nothing when none were missing."""
    if not missing:
        return []
    plural = "s" if len(missing) > 1 else ""
    return [f"Skipped missing column{plural}: {', '.join(missing)}."]


def require_columns(frame: pd.DataFrame, columns: list[str], operation: str) -> None:
    """Insists every named column exists.

    For the columns a step cannot do anything without — a join key, the column being
    ranked. Tolerance is right for "also drop these three"; it is wrong for the one column
    the whole step is about.

    Raises:
        InvalidStepParamsError: naming the missing columns and what the table does have.
    """
    _, missing = present_columns(frame, columns)
    if missing:
        available = ", ".join(str(column) for column in frame.columns)
        raise InvalidStepParamsError(
            f"{operation} needs column(s) {', '.join(missing)}, which the table doesn't "
            f"have. Columns available: {available}."
        )


def samples(series: pd.Series, mask: pd.Series) -> str:
    """A few quoted example values from the masked rows, for a conversion warning."""
    values = series[mask].dropna().astype("string").head(WARNING_SAMPLE_SIZE).tolist()
    return ", ".join(f'"{value}"' for value in values)


def to_numeric(series: pd.Series, column: str) -> tuple[pd.Series, list[str]]:
    """A column as numbers, plus a warning naming what wouldn't convert.

    Handles the two shapes real uploads carry that `pd.to_numeric` alone does not: thousands
    separators (`1,250.00`) and accounting negatives (`(500)`). Everything arrives as text
    by design (`cleaner.loaders` reads `dtype=str` so leading zeros survive), so this runs
    far more often than it looks like it should.
    """
    if pd.api.types.is_numeric_dtype(series):
        return series.astype("float64"), []

    text = series.astype("string").str.strip()
    negated = text.str.match(r"^\(.*\)$", na=False)
    text = text.str.replace(r"^\((.*)\)$", r"\1", regex=True)
    text = text.str.replace(",", "", regex=False)
    numbers = pd.to_numeric(text, errors="coerce")
    numbers = numbers.where(~negated, -numbers).astype("float64")

    failed = numbers.isna() & series.notna() & ~blank_mask(series)
    if not failed.any():
        return numbers, []

    present = int(series.notna().sum())
    return numbers, [
        f"'{column}': {int(failed.sum()):,} of {present:,} value(s) couldn't be read as a "
        f"number and are now blank. Examples: {samples(series, failed)}."
    ]


def _matching_time_unit(converted: pd.Series, second_pass: pd.Series) -> pd.Series:
    """A copy of `converted` that the second pass's dates can be written into.

    The two passes can come back at different time precisions — pandas reads a column of
    plain numbers as whole seconds and a column of text as microseconds, for example.
    Writing the finer one into the coarser one then raises instead of filling in the
    blanks, so both are lifted to the finer precision first.
    """
    try:
        common_unit = np.result_type(converted.dtype, second_pass.dtype)
        return converted.astype(common_unit)
    except (TypeError, ValueError) as error:
        # Time zones or a column pandas gave back as plain objects: nothing to line up,
        # so leave the first pass as it is and let the assignment below do what it can.
        logger.debug("Could not line up date precisions for a column: %s", error)
        return converted.copy()


def to_datetime(series: pd.Series, column: str) -> tuple[pd.Series, list[str]]:
    """A column as dates, plus a warning naming what wouldn't convert.

    Day-first, because this app's users work in Indian and UK date conventions where
    `03/04/2025` is the 3rd of April. pandas would otherwise read it as the 4th of March
    and give a silently wrong answer — the worst kind for a date difference.
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return series, []

    # ISO dates (2026-09-03) are unambiguous, but pandas' per-row "mixed" format
    # guesser can still flip day/month on them when dayfirst=True is set — e.g.
    # reading 2026-09-03 as 9 March instead of 3 September. Parse ISO strings on
    # their own first, so only genuinely ambiguous formats (03/04/2025) go through
    # the dayfirst guesser.
    converted = pd.to_datetime(series, errors="coerce", format="ISO8601")
    still_needed = converted.isna() & series.notna() & ~blank_mask(series)
    if still_needed.any():
        second_pass = pd.to_datetime(
            series[still_needed], errors="coerce", dayfirst=True, format="mixed"
        )
        converted = _matching_time_unit(converted, second_pass)
        converted[still_needed] = second_pass

    failed = converted.isna() & series.notna() & ~blank_mask(series)
    if not failed.any():
        return converted, []

    present = int(series.notna().sum())
    return converted, [
        f"'{column}': {int(failed.sum()):,} of {present:,} value(s) couldn't be read as a "
        f"date and are now blank. Examples: {samples(series, failed)}."
    ]


def unique_column_name(frame: pd.DataFrame, wanted: str) -> str:
    """A column name free in this table: `rank`, else `rank_2`, `rank_3`, ...

    Every step that adds a column routes through this, so adding `bonus` twice gives
    `bonus` and `bonus_2` rather than one silently overwriting the other.
    """
    existing = {str(column) for column in frame.columns}
    candidate = (wanted or "").strip() or "new_column"
    if candidate not in existing:
        return candidate
    suffix_number = 2
    while f"{candidate}_{suffix_number}" in existing:
        suffix_number += 1
    return f"{candidate}_{suffix_number}"


def check_aggregation(frame: pd.DataFrame, column: str, aggregation: str, operation: str) -> None:
    """Refuses maths that cannot mean anything on the column it was asked for.

    `sum` of a text column is the classic: pandas happily concatenates every string in the
    group and returns a wall of text, which looks like a bug in the app rather than a
    mistake in the step.

    Raises:
        InvalidStepParamsError: if a numeric-only aggregation was asked of a column whose
            values won't read as numbers.
    """
    if aggregation not in NUMERIC_ONLY_AGGREGATIONS:
        return
    numbers, _ = to_numeric(frame[column], column)
    if numbers.notna().sum() == 0 and frame[column].notna().sum() > 0:
        raise InvalidStepParamsError(
            f"{operation} can't take the {aggregation} of '{column}' - its values aren't "
            f"numbers. Change that column's type first, or pick a different calculation."
        )
