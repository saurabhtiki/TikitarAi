"""Operations that add a new column worked out from the existing ones.

These are the ones users describe as "the calculation". Between them they cover what the
requirements document (section 4.3) says must be reachable without any free-form code:

- `bonus = basic * 0.12` is `add_calculated_column`.
- "10% if the region is North, otherwise 5%" is `add_conditional_column`.
- "weighted average rate per stock item" is a calculated column (`rate * qty`), two group
  totals and a second calculated column — four ordinary steps, no custom code.

`add_conditional_column` exists as its own operation rather than as `IF()` inside the
expression grammar on purpose. Adding functions to the expression parser is how a small
whitelist turns into a language, and a language is the thing section 2.1 rules out.
"""

import logging

import pandas as pd

from transform.exceptions import InvalidStepParamsError
from transform.expressions import evaluate, referenced_columns
from transform.ops_common import (
    require_columns,
    source_frame,
    to_datetime,
    to_numeric,
    unique_column_name,
)
from transform.ops_rows import COMPARISONS, comparison_mask

logger = logging.getLogger(__name__)

#: Parts of a date this app's users ask for. `fiscal year` and `fiscal quarter` honour the
#: `fiscal_year_starts` parameter, which defaults to April — the Indian and UK financial
#: year, which is what this app is used for.
DATE_PARTS = [
    "year",
    "month number",
    "month name",
    "quarter",
    "fiscal year",
    "fiscal quarter",
    "week number",
    "day of month",
    "day name",
    "year-month",
]

MONTH_CHOICES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

DATE_UNITS = ["days", "months", "years"]


# --------------------------------------------------------------------------------------
# add_calculated_column
# --------------------------------------------------------------------------------------


def apply_add_calculated_column(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column worked out by an arithmetic formula over the other columns."""
    frame = source_frame(frames_by_role)
    expression = str(params.get("expression", ""))

    values, warnings_out = evaluate(frame, expression)
    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column", "")).strip())
    if new_name != str(params.get("new_column", "")).strip():
        warnings_out.append(
            f"A column called '{params.get('new_column')}' already existed, so the answer "
            f"was added as '{new_name}'."
        )

    decimals = params.get("decimals")
    if decimals is not None:
        values = values.round(int(decimals))

    result[new_name] = values
    return result, warnings_out


def describe_add_calculated_column(step: dict) -> str:
    params = step.get("params", {})
    return f"Added {params.get('new_column')} = {params.get('expression')}"


def validate_add_calculated_column(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("new_column", "")).strip():
        raise InvalidStepParamsError("Type a name for the new column.")
    # Parsing here rather than at run time is what keeps a stored pipeline well-formed:
    # a formula that cannot be read never enters the step list in the first place.
    referenced_columns(str(params.get("expression", "")), columns_by_role.get("source") or [])


def required_add_calculated_column(params: dict) -> dict[str, list[str]]:
    """The columns the formula reads.

    Best-effort: without the table's column list the formula cannot be tokenized, so an
    empty answer is returned rather than a guess. Phase 27 resolves this against the saved
    schema, which does have the columns.
    """
    return {"source": []}


# --------------------------------------------------------------------------------------
# add_conditional_column
# --------------------------------------------------------------------------------------


def apply_add_conditional_column(
    frames_by_role: dict, params: dict
) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding one value where a test passes and another where it doesn't.

    Both answers may be a typed-in value or another column's value, which is what lets
    "10% of net sales for North, 5% for everyone else" be one step: the two answers are
    themselves formulas.
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Add an if/else column")

    mask, warnings_out = comparison_mask(
        frame, column, params.get("comparison", "is"), params.get("value", "")
    )

    when_true, true_warnings = _answer_values(frame, params.get("result_if_true", ""), "if-true")
    when_false, false_warnings = _answer_values(frame, params.get("result_if_false", ""), "if-false")
    warnings_out = list(warnings_out) + true_warnings + false_warnings

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column", "")).strip())
    result[new_name] = when_true.where(mask, when_false)
    return result, warnings_out


def _answer_values(frame: pd.DataFrame, answer: object, side: str) -> tuple[pd.Series, list[str]]:
    """One side of the if/else, as a full column of values.

    A formula that reads columns is evaluated; anything else becomes a constant repeated
    down the table. Tried as a formula first, because that is the interesting case, and
    falling back silently means `North` is a plain word rather than an error about an
    unknown column.
    """
    text = str(answer).strip()
    try:
        values, warnings_out = evaluate(frame, text)
        return values, warnings_out
    except InvalidStepParamsError:
        logger.debug("The %s answer '%s' isn't a formula; using it as a fixed value.", side, text)
        return pd.Series([answer] * len(frame), index=frame.index), []


def describe_add_conditional_column(step: dict) -> str:
    params = step.get("params", {})
    return (
        f"Added {params.get('new_column')}: {params.get('result_if_true')} where "
        f"{params.get('column')} {params.get('comparison')} {params.get('value')}, "
        f"otherwise {params.get('result_if_false')}"
    )


def validate_add_conditional_column(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("new_column", "")).strip():
        raise InvalidStepParamsError("Type a name for the new column.")
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the column to test.")
    if params.get("comparison") not in COMPARISONS:
        raise InvalidStepParamsError(f"'{params.get('comparison')}' isn't a test this step knows.")


def required_add_conditional_column(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# extract_date_part
# --------------------------------------------------------------------------------------


def apply_extract_date_part(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding one part of a date — the year, the quarter, the month name."""
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    part = params.get("part", "year")
    require_columns(frame, [column], "Get a date part")

    dates, warnings_out = to_datetime(frame[column], column)
    start_month = _month_number(params.get("fiscal_year_starts", "April"))

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column") or f"{column} {part}"))
    result[new_name] = _date_part(dates, part, start_month)
    return result, warnings_out


def _month_number(month_name: str) -> int:
    """April -> 4. Falls back to April, the financial year this app is used with."""
    try:
        return MONTH_CHOICES.index(str(month_name)) + 1
    except ValueError:
        logger.warning("Unknown fiscal-year start month '%s'; using April.", month_name)
        return 4


def _date_part(dates: pd.Series, part: str, start_month: int) -> pd.Series:
    """One part of a date column.

    The fiscal parts are shifted so that the year running from `start_month` is labelled by
    the calendar year it starts in: with an April start, March 2026 belongs to fiscal 2025.
    """
    if part == "year":
        return dates.dt.year.astype("Int64")
    if part == "month number":
        return dates.dt.month.astype("Int64")
    if part == "month name":
        return dates.dt.month_name().astype("string")
    if part == "quarter":
        return dates.dt.quarter.astype("Int64")
    if part == "week number":
        return dates.dt.isocalendar().week.astype("Int64")
    if part == "day of month":
        return dates.dt.day.astype("Int64")
    if part == "day name":
        return dates.dt.day_name().astype("string")
    if part == "year-month":
        return dates.dt.strftime("%Y-%m").astype("string")

    shifted = dates - pd.DateOffset(months=start_month - 1)
    if part == "fiscal year":
        return shifted.dt.year.astype("Int64")
    if part == "fiscal quarter":
        return shifted.dt.quarter.astype("Int64")

    raise InvalidStepParamsError(f"'{part}' isn't a date part this step knows.")


def describe_extract_date_part(step: dict) -> str:
    params = step.get("params", {})
    return f"Took the {params.get('part')} of {params.get('column')}"


def validate_extract_date_part(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the date column.")
    if params.get("part") not in DATE_PARTS:
        raise InvalidStepParamsError(f"'{params.get('part')}' isn't a date part this step knows.")


def required_extract_date_part(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# date_difference
# --------------------------------------------------------------------------------------


def apply_date_difference(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding how long there is between two dates.

    The "holding period" the requirements document names: `sold_on` minus `bought_on`, in
    days, months or years.
    """
    frame = source_frame(frames_by_role)
    from_column = str(params.get("from_column", ""))
    to_column = str(params.get("to_column", ""))
    require_columns(frame, [from_column, to_column], "Days between two dates")

    starts, start_warnings = to_datetime(frame[from_column], from_column)
    ends, end_warnings = to_datetime(frame[to_column], to_column)
    warnings_out = start_warnings + end_warnings

    elapsed_days = (ends - starts).dt.days
    unit = params.get("unit", "days")
    if unit == "days":
        values = elapsed_days.astype("Int64")
    elif unit == "months":
        # Calendar months, not 30-day blocks: the gap between 31 Jan and 28 Feb is one
        # month to anyone reading a report, whatever the day count says.
        values = ((ends.dt.year - starts.dt.year) * 12 + (ends.dt.month - starts.dt.month)).astype(
            "Int64"
        )
    elif unit == "years":
        values = (elapsed_days / 365.25).round(2).astype("float64")
    else:
        raise InvalidStepParamsError(f"'{unit}' isn't a unit this step knows.")

    negative = int((elapsed_days < 0).sum())
    if negative:
        warnings_out.append(
            f"{negative:,} row(s) have '{to_column}' before '{from_column}', so their answer "
            f"is negative."
        )

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column") or f"{unit} between"))
    result[new_name] = values
    return result, warnings_out


def describe_date_difference(step: dict) -> str:
    params = step.get("params", {})
    return (
        f"Worked out the {params.get('unit', 'days')} from {params.get('from_column')} to "
        f"{params.get('to_column')}"
    )


def validate_date_difference(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("from_column", "")).strip():
        raise InvalidStepParamsError("Choose the earlier date column.")
    if not str(params.get("to_column", "")).strip():
        raise InvalidStepParamsError("Choose the later date column.")
    if params.get("unit") not in DATE_UNITS:
        raise InvalidStepParamsError(f"'{params.get('unit')}' isn't a unit this step knows.")


def required_date_difference(params: dict) -> dict[str, list[str]]:
    return {
        "source": [str(params.get("from_column", "")), str(params.get("to_column", ""))]
    }


# --------------------------------------------------------------------------------------
# bucket_numeric
# --------------------------------------------------------------------------------------


def apply_bucket_numeric(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Sorts a number column into named ranges — age groups, price bands, ageing buckets.

    The edges are the *upper* limit of each band, typed as a comma-separated list, and each
    band is "up to and including" its edge. Anything above the last edge falls into a final
    open-ended band, so no row is ever left unlabelled.
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Put numbers into ranges")

    edges = _parse_edges(params.get("edges", ""))
    numbers, warnings_out = to_numeric(frame[column], column)

    labels = [f"up to {_tidy(edge)}" for edge in edges] + [f"over {_tidy(edges[-1])}"]
    bounds = [float("-inf"), *edges, float("inf")]
    bands = pd.cut(numbers, bins=bounds, labels=labels, include_lowest=True, right=True)

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column") or f"{column} band"))
    result[new_name] = bands.astype("string")

    unbanded = int(bands.isna().sum() - numbers.isna().sum())
    if unbanded > 0:
        warnings_out.append(f"{unbanded:,} row(s) didn't fall into any range.")
    return result, warnings_out


def _parse_edges(raw: object) -> list[float]:
    """`10, 20, 50` -> [10.0, 20.0, 50.0], sorted, de-duplicated.

    Raises:
        InvalidStepParamsError: if it holds no usable number.
    """
    pieces = [piece.strip() for piece in str(raw).split(",") if piece.strip()]
    edges: list[float] = []
    for piece in pieces:
        try:
            edges.append(float(piece.replace(",", "")))
        except ValueError as error:
            raise InvalidStepParamsError(
                f"'{piece}' isn't a number. Type the range limits separated by commas, "
                f"for example: 30, 60, 90"
            ) from error
    if not edges:
        raise InvalidStepParamsError(
            "Type the range limits separated by commas, for example: 30, 60, 90"
        )
    return sorted(set(edges))


def _tidy(number: float) -> str:
    """Shows 30.0 as 30 but leaves 29.5 alone, so labels read like a person wrote them."""
    return str(int(number)) if float(number).is_integer() else str(number)


def describe_bucket_numeric(step: dict) -> str:
    params = step.get("params", {})
    return f"Put {params.get('column')} into ranges at {params.get('edges')}"


def validate_bucket_numeric(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the number column to band.")
    _parse_edges(params.get("edges", ""))


def required_bucket_numeric(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}
