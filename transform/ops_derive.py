"""Operations that add a new column worked out from the existing ones.

These are the ones users describe as "the calculation". Between them they cover what the
requirements document (section 4.3) says must be reachable without any free-form code:

- `bonus = basic * 0.12` is `add_calculated_column`.
- "10% if the region is North, otherwise 5%" is `add_conditional_column`.
- "weighted average rate per stock item" is a calculated column (`rate * qty`), two group
  totals and a second calculated column — four ordinary steps, no custom code.

A simple numeric branch can also be written straight into a formula - `price * 0.9 if
quantity > 100 else price` is one `add_calculated_column` step. That is a comparison and a
choice between two numbers, nothing more; the expression grammar still has no functions and
no names other than columns, which is what section 2.1 rules out.

`add_conditional_column` stays a separate operation because it does the things a formula
cannot: a **text** answer ("North" -> "high"), the comparison kinds that are not maths
(`contains`, `is not`, `is blank` - see `ops_rows.COMPARISONS`), and carrying another
column's text across unchanged. A formula only ever produces numbers.
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

#: How far `shift_date` will move a date, in days — a century either way. Beyond this pandas
#: runs out of timestamp range and raises about nanoseconds, which tells a user nothing.
MAX_DAY_SHIFT = 36500

#: Which end of a date's month `month_edge` gives back.
MONTH_EDGES = ["start of month", "end of month"]

#: Whether `absolute_value` overwrites the column or writes the answer beside it.
ABSOLUTE_MODES = ["update the same column", "add a new column"]

#: Which way `row_min_max` compares across a row's columns.
ROW_EXTREMES = ["largest", "smallest"]


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

    An answer that is nothing but one column's name is a special case, checked before the
    formula path: this is how "keep whatever Remarks already said" is written, and
    Remarks is usually text. Running it through the formula evaluator would read it as
    arithmetic and turn every row into a number (or a blank, if it isn't one) - fine for
    `amount * 0.1`, wrong for a plain carry-over of a text column.
    """
    text = str(answer).strip()
    referenced = _sole_column_reference(text, frame)
    if referenced is not None:
        return frame[referenced], []
    try:
        values, warnings_out = evaluate(frame, text)
        return values, warnings_out
    except InvalidStepParamsError:
        logger.debug("The %s answer '%s' isn't a formula; using it as a fixed value.", side, text)
        return pd.Series([answer] * len(frame), index=frame.index), []


def _sole_column_reference(text: str, frame: pd.DataFrame) -> str | None:
    """The column `text` names, if it names nothing but one column - else `None`.

    Matches `[net sales]` or a bare `net_sales`, same spellings the formula grammar
    accepts, but only when the whole answer is that one name with nothing else around it.
    """
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    return text if text in frame.columns else None


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


# --------------------------------------------------------------------------------------
# add_today_date
# --------------------------------------------------------------------------------------


def apply_add_today_date(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding today's date, the same value in every row.

    The stamp a finance user puts on a working file so that "as at" is written down rather
    than remembered: `Loaded_On` = 19/09/2026 on every row. The time of day is dropped, so
    the column is a plain date and a later date difference counts whole days.

    The date is read when the step *runs*, not when it was added, which is the behaviour a
    saved pipeline wants: next month's replay stamps next month's date.
    """
    frame = source_frame(frames_by_role)
    today = pd.Timestamp.today().normalize()

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column") or "today"))
    result[new_name] = pd.Series([today] * len(result), index=result.index, dtype="datetime64[ns]")
    return result, []


def describe_add_today_date(step: dict) -> str:
    params = step.get("params", {})
    return f"Added {params.get('new_column') or 'today'} holding today's date"


def validate_add_today_date(columns_by_role: dict, params: dict) -> None:
    """Nothing to check: there is no column to read and the name has a fallback."""
    return None


def required_add_today_date(params: dict) -> dict[str, list[str]]:
    return {"source": []}


# --------------------------------------------------------------------------------------
# shift_date
# --------------------------------------------------------------------------------------


def apply_shift_date(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding a date column moved forward or back by a number of days.

    `Due Date = Invoice Date + 30`. A negative number moves backwards, which is how a
    reminder date ("7 days before it is due") is written.
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Add or subtract days")

    days = _day_shift(params.get("days", 0))
    dates, warnings_out = to_datetime(frame[column], column)

    result = frame.copy()
    default_name = f"{column} {'plus' if days >= 0 else 'minus'} {abs(days)} days"
    new_name = unique_column_name(result, str(params.get("new_column") or default_name))
    result[new_name] = dates + pd.Timedelta(days=days)
    return result, warnings_out


def whole_number(value: object, label: str) -> int:
    """A parameter as a whole number, or a message naming the box it came from.

    Raises:
        InvalidStepParamsError: if the value isn't a whole number.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise InvalidStepParamsError(
            f"'{label}' needs a whole number, but got '{value}'."
        ) from error


def _day_shift(value: object) -> int:
    """The number of days `shift_date` will move by.

    Raises:
        InvalidStepParamsError: if it isn't a whole number, or is further than
            `MAX_DAY_SHIFT` — which pandas would refuse anyway, in a message about
            nanosecond bounds that no user of this page can act on.
    """
    days = whole_number(value, "Days to add")
    if abs(days) > MAX_DAY_SHIFT:
        raise InvalidStepParamsError(
            f"'Days to add' can move a date by at most {MAX_DAY_SHIFT:,} days (about 100 "
            f"years), but got {days:,}."
        )
    return days


def _safe_int(value: object) -> int:
    """A whole number for a description line, or 0 - a description never raises."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def describe_shift_date(step: dict) -> str:
    params = step.get("params", {})
    days = _safe_int(params.get("days", 0))
    direction = "back" if days < 0 else "forward"
    return f"Moved {params.get('column')} {direction} by {abs(days)} day(s)"


def validate_shift_date(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the date column to move.")
    _day_shift(params.get("days", 0))


def required_shift_date(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# month_edge
# --------------------------------------------------------------------------------------


def apply_month_edge(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding the first or the last day of a date's month.

    What a monthly report is dated by: every date in March 2026 becomes 01/03/2026 or
    31/03/2026, so rows group together however untidy the day numbers are.
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    edge = params.get("edge", MONTH_EDGES[0])
    require_columns(frame, [column], "Start or end of month")
    if edge not in MONTH_EDGES:
        raise InvalidStepParamsError(f"'{edge}' isn't a choice this step knows.")

    dates, warnings_out = to_datetime(frame[column], column)
    # `MonthEnd(1)` on a date already sitting on the last day would step into the *next*
    # month, so both answers are worked out from the month's first day instead.
    starts = dates.dt.to_period("M").dt.to_timestamp()
    values = starts if edge == "start of month" else starts + pd.offsets.MonthEnd(1)

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column") or f"{column} {edge}"))
    result[new_name] = values
    return result, warnings_out


def describe_month_edge(step: dict) -> str:
    params = step.get("params", {})
    return f"Took the {params.get('edge', MONTH_EDGES[0])} of {params.get('column')}"


def validate_month_edge(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the date column.")
    if params.get("edge") not in MONTH_EDGES:
        raise InvalidStepParamsError(f"'{params.get('edge')}' isn't a choice this step knows.")


def required_month_edge(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# row_percentage_of_total
# --------------------------------------------------------------------------------------


def apply_row_percentage_of_total(
    frames_by_role: dict, params: dict
) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding each row's share of the column's total, as a percentage.

    "What fraction of sales is this branch?" - the whole column is totalled once and every
    row is divided by that one total, so the new column adds up to 100.

    A column totalling zero gives a blank in every row rather than an error or an infinity,
    the same divide-by-zero rule the calculated-column formula follows.
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Percentage of total")

    numbers, warnings_out = to_numeric(frame[column], column)
    total = float(numbers.sum(skipna=True))

    if total == 0:
        shares = pd.Series(float("nan"), index=frame.index, dtype="float64")
        warnings_out.append(
            f"'{column}' adds up to zero, so every row's share is blank rather than an error."
        )
    else:
        shares = numbers / total * 100
        decimals = params.get("decimals")
        if decimals is not None:
            shares = shares.round(whole_number(decimals, "Decimal places"))

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column") or f"{column} % of total"))
    result[new_name] = shares
    return result, warnings_out


def describe_row_percentage_of_total(step: dict) -> str:
    params = step.get("params", {})
    return f"Worked out each row's share of the total {params.get('column')}"


def validate_row_percentage_of_total(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the number column to share out.")
    if params.get("decimals") is not None:
        whole_number(params.get("decimals"), "Decimal places")


def required_row_percentage_of_total(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# absolute_value
# --------------------------------------------------------------------------------------


def apply_absolute_value(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Drops the minus sign: -500 becomes 500.

    Ledger extracts hand out credits as negatives; a report that wants "how much moved",
    not "which way", needs the size alone. Accounting negatives in brackets - `(500)` - are
    already understood by `to_numeric`, so they come out as 500 too.

    A value that can't be read as a number becomes blank and is reported, the same rule
    `change_dtype` follows.
    """
    frame = source_frame(frames_by_role)
    column = str(params.get("column", ""))
    require_columns(frame, [column], "Absolute value")

    numbers, warnings_out = to_numeric(frame[column], column)
    sizes = numbers.abs()

    result = frame.copy()
    if params.get("mode", ABSOLUTE_MODES[0]) == "add a new column":
        new_name = unique_column_name(result, str(params.get("new_column") or f"{column} size"))
        result[new_name] = sizes
    else:
        result[column] = sizes
    return result, warnings_out


def describe_absolute_value(step: dict) -> str:
    params = step.get("params", {})
    return f"Dropped the minus sign from {params.get('column')}"


def validate_absolute_value(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("column", "")).strip():
        raise InvalidStepParamsError("Choose the number column.")
    if params.get("mode") is not None and params.get("mode") not in ABSOLUTE_MODES:
        raise InvalidStepParamsError(f"'{params.get('mode')}' isn't a choice this step knows.")


def required_absolute_value(params: dict) -> dict[str, list[str]]:
    return {"source": [str(params.get("column", ""))]}


# --------------------------------------------------------------------------------------
# row_min_max
# --------------------------------------------------------------------------------------


def apply_row_min_max(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column holding the smallest or largest of several columns, row by row.

    This compares *sideways*: on each row it looks across `Budget` and `Actual` and keeps
    the higher of the two. That is not `groupby_aggregate`, which totals one column
    downwards over many rows - a distinction worth spelling out, because both get called
    "max" in conversation.

    A row where none of the chosen columns holds a number comes out blank rather than
    failing the step.
    """
    frame = source_frame(frames_by_role)
    requested = [str(column) for column in params.get("columns", [])]
    which = params.get("which", ROW_EXTREMES[0])
    require_columns(frame, requested, "Smallest or largest across columns")
    if which not in ROW_EXTREMES:
        raise InvalidStepParamsError(f"'{which}' isn't a choice this step knows.")
    if len(requested) < 2:
        raise InvalidStepParamsError("Choose at least two columns to compare.")

    warnings_out: list[str] = []
    converted: dict[str, pd.Series] = {}
    for column in requested:
        numbers, conversion_warnings = to_numeric(frame[column], column)
        converted[column] = numbers
        warnings_out.extend(conversion_warnings)

    side_by_side = pd.DataFrame(converted, index=frame.index)
    values = side_by_side.min(axis=1) if which == "smallest" else side_by_side.max(axis=1)

    blank_rows = int(values.isna().sum())
    if blank_rows:
        warnings_out.append(
            f"{blank_rows:,} row(s) had no number in any of those columns, so their answer "
            f"is blank."
        )

    result = frame.copy()
    new_name = unique_column_name(
        result, str(params.get("new_column") or f"{which} of the columns")
    )
    result[new_name] = values
    return result, warnings_out


def describe_row_min_max(step: dict) -> str:
    params = step.get("params", {})
    columns = ", ".join(str(column) for column in params.get("columns", []))
    return f"Took the {params.get('which', ROW_EXTREMES[0])} of {columns} on each row"


def validate_row_min_max(columns_by_role: dict, params: dict) -> None:
    chosen = [str(column) for column in params.get("columns", [])]
    if len(chosen) < 2:
        raise InvalidStepParamsError("Choose at least two columns to compare.")
    if params.get("which") not in ROW_EXTREMES:
        raise InvalidStepParamsError(f"'{params.get('which')}' isn't a choice this step knows.")


def required_row_min_max(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}
