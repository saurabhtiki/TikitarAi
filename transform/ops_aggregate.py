"""Operations that summarise or rank: group & total, pivot, rank, running total.

`groupby_aggregate` looks like a duplicate of the Data Cleaner's `group_summarise`, and is
not. There a summary is a read-only derived view hanging off one table's tab. Here the
answer is a first-class named table that later steps read, join and filter — which is what
makes "weighted average rate per stock item" reachable as four ordinary steps.

`rank_within_group` and `running_total` have no counterpart anywhere in the app.
"""

import logging

import pandas as pd

from cleaner.steps import MAX_PIVOT_COLUMNS
from transform.exceptions import InvalidStepParamsError
from transform.ops_common import (
    AGGREGATIONS,
    PANDAS_AGGREGATIONS,
    check_aggregation,
    require_columns,
    source_frame,
    to_numeric,
    unique_column_name,
)

logger = logging.getLogger(__name__)

RANK_METHODS = ["1, 2, 3 (ties share the best rank)", "1, 2, 2, 4 (ties share, then skip)", "dense"]

#: This page's wording -> the pandas `rank(method=...)` name.
_RANK_METHODS = {
    "1, 2, 3 (ties share the best rank)": "min",
    "1, 2, 2, 4 (ties share, then skip)": "min",
    "dense": "dense",
}


# --------------------------------------------------------------------------------------
# groupby_aggregate
# --------------------------------------------------------------------------------------


def apply_groupby_aggregate(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Groups rows and works out one figure per group.

    One aggregation applied to one or more value columns, rather than a different one per
    column. Two different calculations are two steps, which keeps the form to three plain
    widgets and reads more clearly in the step list.
    """
    frame = source_frame(frames_by_role)
    group_columns = [str(column) for column in params.get("group_by", [])]
    value_columns = [str(column) for column in params.get("value_columns", [])]
    aggregation = params.get("aggregation", "sum")

    require_columns(frame, group_columns, "Group and total")
    require_columns(frame, value_columns, "Group and total")
    if aggregation not in PANDAS_AGGREGATIONS:
        raise InvalidStepParamsError(f"'{aggregation}' isn't a calculation this step knows.")

    warnings_out: list[str] = []
    working = frame.copy()

    for column in value_columns:
        check_aggregation(working, column, aggregation, "Group and total")
        if aggregation in ("sum", "mean", "median"):
            numbers, conversion_warnings = to_numeric(working[column], column)
            working[column] = numbers
            warnings_out.extend(conversion_warnings)

    # dropna=False so a blank grouping key becomes its own row rather than vanishing —
    # silently dropping rows from a total is the worst thing a summary can do.
    grouped = (
        working.groupby(group_columns, dropna=False, observed=True)[value_columns]
        .agg(PANDAS_AGGREGATIONS[aggregation])
        .reset_index()
    )

    renamed = {column: f"{aggregation} of {column}" for column in value_columns}
    grouped = grouped.rename(columns=renamed)

    if params.get("include_row_count"):
        counts = (
            working.groupby(group_columns, dropna=False, observed=True)
            .size()
            .reset_index(name="row count")
        )
        grouped = grouped.merge(counts, on=group_columns, how="left")

    warnings_out.append(f"{len(frame):,} row(s) became {len(grouped):,} group(s).")
    return grouped, warnings_out


def describe_groupby_aggregate(step: dict) -> str:
    params = step.get("params", {})
    return (
        f"Grouped by {', '.join(params.get('group_by', []))} and took the "
        f"{params.get('aggregation', 'sum')} of {', '.join(params.get('value_columns', []))}"
    )


def validate_groupby_aggregate(columns_by_role: dict, params: dict) -> None:
    if not params.get("group_by"):
        raise InvalidStepParamsError("Choose at least one column to group by.")
    if not params.get("value_columns"):
        raise InvalidStepParamsError("Choose at least one column to calculate.")
    if params.get("aggregation") not in AGGREGATIONS:
        raise InvalidStepParamsError(f"'{params.get('aggregation')}' isn't a calculation this step knows.")
    overlap = set(params["group_by"]) & set(params["value_columns"])
    if overlap:
        raise InvalidStepParamsError(
            f"{', '.join(sorted(overlap))} can't be both grouped by and calculated."
        )


def required_groupby_aggregate(params: dict) -> dict[str, list[str]]:
    return {
        "source": [str(column) for column in params.get("group_by", [])]
        + [str(column) for column in params.get("value_columns", [])]
    }


# --------------------------------------------------------------------------------------
# pivot
# --------------------------------------------------------------------------------------


def apply_pivot(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Turns one column's values into columns of their own — a cross-tab."""
    frame = source_frame(frames_by_role)
    row_columns = [str(column) for column in params.get("row_columns", [])]
    column_column = str(params.get("column_column", ""))
    value_column = str(params.get("value_column", ""))
    aggregation = params.get("aggregation", "sum")

    require_columns(frame, [*row_columns, column_column, value_column], "Pivot")

    distinct = int(frame[column_column].nunique(dropna=False))
    if distinct > MAX_PIVOT_COLUMNS:
        raise InvalidStepParamsError(
            f"'{column_column}' has {distinct:,} different values, which would make "
            f"{distinct:,} columns (the limit is {MAX_PIVOT_COLUMNS:,}). Pick a column with "
            f"fewer values, or group them into ranges first."
        )

    warnings_out: list[str] = []
    working = frame.copy()
    check_aggregation(working, value_column, aggregation, "Pivot")
    if aggregation in ("sum", "mean", "median"):
        numbers, conversion_warnings = to_numeric(working[value_column], value_column)
        working[value_column] = numbers
        warnings_out.extend(conversion_warnings)

    pivoted = pd.pivot_table(
        working,
        index=row_columns,
        columns=column_column,
        values=value_column,
        aggfunc=PANDAS_AGGREGATIONS[aggregation],
        dropna=False,
        observed=True,
    ).reset_index()
    pivoted.columns = [str(label) for label in pivoted.columns]
    pivoted.columns.name = None
    return pivoted, warnings_out


def describe_pivot(step: dict) -> str:
    params = step.get("params", {})
    return (
        f"Pivoted {params.get('value_column')} ({params.get('aggregation', 'sum')}) with "
        f"{', '.join(params.get('row_columns', []))} down and {params.get('column_column')} across"
    )


def validate_pivot(columns_by_role: dict, params: dict) -> None:
    if not params.get("row_columns"):
        raise InvalidStepParamsError("Choose at least one column for the rows.")
    if not str(params.get("column_column", "")).strip():
        raise InvalidStepParamsError("Choose the column whose values become the new columns.")
    if not str(params.get("value_column", "")).strip():
        raise InvalidStepParamsError("Choose the column to calculate.")
    if params.get("column_column") in params.get("row_columns", []):
        raise InvalidStepParamsError("The across column can't also be one of the down columns.")


def required_pivot(params: dict) -> dict[str, list[str]]:
    return {
        "source": [str(column) for column in params.get("row_columns", [])]
        + [str(params.get("column_column", "")), str(params.get("value_column", ""))]
    }


# --------------------------------------------------------------------------------------
# rank_within_group
# --------------------------------------------------------------------------------------


def apply_rank_within_group(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Numbers the rows 1, 2, 3 ... within each group, ordered by a chosen column.

    Combined with a filter on the new column, this is also how "the top 3 per customer" is
    done — which is why there is no separate top-N operation.
    """
    frame = source_frame(frames_by_role)
    group_columns = [str(column) for column in params.get("group_by", [])]
    order_column = str(params.get("order_by", ""))

    require_columns(frame, [*group_columns, order_column], "Rank within a group")

    numbers, warnings_out = to_numeric(frame[order_column], order_column)
    readable = int(numbers.notna().sum())
    filled = int(frame[order_column].notna().sum())
    # Rank text as text when it clearly isn't numeric, rather than ranking a column of
    # names as a column of blanks.
    values = numbers if (filled and readable >= filled) else frame[order_column]

    ascending = bool(params.get("ascending", False))
    method = _RANK_METHODS.get(params.get("method", RANK_METHODS[0]), "min")

    result = frame.copy()
    ranking = pd.DataFrame({"value": values})
    if group_columns:
        ranking[group_columns] = frame[group_columns]
        ranks = ranking.groupby(group_columns, dropna=False, observed=True)["value"].rank(
            method=method, ascending=ascending
        )
    else:
        ranks = ranking["value"].rank(method=method, ascending=ascending)

    new_name = unique_column_name(result, str(params.get("new_column") or "rank"))
    result[new_name] = ranks.astype("Int64")
    return result, warnings_out


def describe_rank_within_group(step: dict) -> str:
    params = step.get("params", {})
    direction = "smallest first" if params.get("ascending") else "largest first"
    within = f" within {', '.join(params['group_by'])}" if params.get("group_by") else ""
    return f"Ranked by {params.get('order_by')} ({direction}){within}"


def validate_rank_within_group(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("order_by", "")).strip():
        raise InvalidStepParamsError("Choose the column that decides the order.")


def required_rank_within_group(params: dict) -> dict[str, list[str]]:
    return {
        "source": [str(column) for column in params.get("group_by", [])]
        + [str(params.get("order_by", ""))]
    }


# --------------------------------------------------------------------------------------
# running_total
# --------------------------------------------------------------------------------------


def apply_running_total(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Adds a column that accumulates down the table, optionally restarting per group."""
    frame = source_frame(frames_by_role)
    value_column = str(params.get("value_column", ""))
    group_columns = [str(column) for column in params.get("group_by", [])]
    order_column = str(params.get("order_by", "") or "")

    require_columns(frame, [value_column, *group_columns], "Running total")
    if order_column:
        require_columns(frame, [order_column], "Running total")

    numbers, warnings_out = to_numeric(frame[value_column], value_column)
    working = frame.copy()
    working["_running_value"] = numbers

    # Sort by the chosen order, accumulate, then restore the table's original order — so
    # the running total is correct without the step silently reordering the user's rows.
    ordered_index = working.index
    if order_column:
        ordered_index = working.sort_values(by=order_column, kind="stable").index

    ordered = working.loc[ordered_index]
    if group_columns:
        totals = ordered.groupby(group_columns, dropna=False, observed=True)["_running_value"].cumsum()
    else:
        totals = ordered["_running_value"].cumsum()

    result = frame.copy()
    new_name = unique_column_name(result, str(params.get("new_column") or f"running {value_column}"))
    result[new_name] = totals.reindex(result.index)
    return result, warnings_out


def describe_running_total(step: dict) -> str:
    params = step.get("params", {})
    within = f" within {', '.join(params['group_by'])}" if params.get("group_by") else ""
    ordered = f", ordered by {params['order_by']}" if params.get("order_by") else ""
    return f"Running total of {params.get('value_column')}{within}{ordered}"


def validate_running_total(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("value_column", "")).strip():
        raise InvalidStepParamsError("Choose the column to total.")


def required_running_total(params: dict) -> dict[str, list[str]]:
    columns = [str(params.get("value_column", ""))]
    columns += [str(column) for column in params.get("group_by", [])]
    if params.get("order_by"):
        columns.append(str(params["order_by"]))
    return {"source": columns}
