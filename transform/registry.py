"""The operation catalog: what each step is called, what it asks for, and what runs it.

This is the "source of truth" the requirements document's section 2.1 describes. A step
stored in a pipeline names an entry here and nothing else; there is no path by which a
pipeline can carry code.

`OPERATION_REGISTRY` is an **explicit literal dict**, not built by a decorator, for the
reason `cleaner/steps.py` records: a decorator lets an import-order slip produce a silently
empty registry, and replay would then skip every step in a pipeline without raising
anything at all.

Adding an operation is one entry here plus one executor function. The form is drawn from
the `params` tuple and the step list line from `describe`, so **no UI code changes** — which
is requirement 4.2, and what keeps the later phases cheap.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from transform import ops_aggregate, ops_clean, ops_columns, ops_combine, ops_derive, ops_rows
from transform.exceptions import InvalidOperationError
from transform.ops_common import AGGREGATIONS
from transform.params import InputRole, ParamKind, ParamSpec

logger = logging.getLogger(__name__)

# Categories, in the order the picker shows them. Roughly "what do I want to do": change
# the shape of this table, cut it down, work something out, summarise it, put two together.
CATEGORY_COLUMN = "Columns"
CATEGORY_FILTER = "Filter & sort"
CATEGORY_CALCULATE = "Calculated columns"
CATEGORY_AGGREGATE = "Summarise"
CATEGORY_COMBINE = "Combine tables"
CATEGORY_DATES = "Dates"
CATEGORY_CLEAN = "Tidy up"

CATEGORY_ORDER = [
    CATEGORY_COMBINE,
    CATEGORY_CALCULATE,
    CATEGORY_AGGREGATE,
    CATEGORY_FILTER,
    CATEGORY_COLUMN,
    CATEGORY_DATES,
    CATEGORY_CLEAN,
]

OutputMode = Literal["in_place", "new"]

#: The one input most operations have. Defined once so the common case is a single name.
SOURCE_ROLE = InputRole(
    name="source",
    label="Table",
    help="The table this step reads and changes.",
)


@dataclass(frozen=True)
class OperationSpec:
    """Everything about one operation, in one place.

    Attributes:
        operation: the key a stored step names. Never changed once shipped — a rename would
            orphan every saved pipeline that used it.
        label: what it is called on screen.
        category: which group of the picker it appears under.
        summary: the one-line explanation shown under the picker.
        inputs: the tables it reads, in the order the form asks for them.
        params: the boxes the form draws.
        default_output: whether it usually updates its source table or makes a new one.
            A groupby makes a new table; a filter changes the one it was given.
        output_name_hint: the pre-filled new-table name, with `{source}` standing for the
            first input's name.
        apply: the executor. `(frames_by_role, params) -> (new_frame, warnings)`.
        describe: the step list line. Takes the whole step, so it can name the input tables.
        validate: checks a step before it joins the pipeline.
            `(columns_by_role, params) -> None`, raising on a problem.
        required_columns: which columns of which input the step depends on. Nothing reads
            this yet; phase 27's "are the required columns present?" check is what it is for.
    """

    operation: str
    label: str
    category: str
    summary: str
    inputs: tuple[InputRole, ...]
    params: tuple[ParamSpec, ...]
    default_output: OutputMode
    output_name_hint: str
    apply: Callable[[dict, dict], tuple[pd.DataFrame, list[str]]]
    describe: Callable[[dict], str]
    validate: Callable[[dict, dict], None]
    required_columns: Callable[[dict], dict[str, list[str]]]

    @property
    def role_names(self) -> list[str]:
        return [role.name for role in self.inputs]

    @property
    def primary_role(self) -> str:
        """The input a new table's name is suggested from."""
        return self.inputs[0].name if self.inputs else "source"


OPERATION_REGISTRY: dict[str, OperationSpec] = {
    # ----------------------------------------------------------------------------------
    # Combine tables
    # ----------------------------------------------------------------------------------
    "merge": OperationSpec(
        operation="merge",
        label="Join two tables",
        category=CATEGORY_COMBINE,
        summary="Match rows in two tables on a shared column and put their columns side by side.",
        inputs=(
            InputRole(name="left", label="First table", help="The table whose rows you want to keep."),
            InputRole(
                name="right",
                label="Second table",
                help="The table whose columns are added to the first.",
            ),
        ),
        params=(
            ParamSpec(
                name="left_on",
                label="Matching column(s) in the first table",
                kind=ParamKind.COLUMNS,
                from_role="left",
                help="The column both tables share, such as customer_id. Pick more than one to match on a combination.",
            ),
            ParamSpec(
                name="right_on",
                label="Matching column(s) in the second table",
                kind=ParamKind.COLUMNS,
                from_role="right",
                required=False,
                help="Leave empty if the column has the same name in both tables. Otherwise pick the matching column(s), in the same order.",
            ),
            ParamSpec(
                name="join_type",
                label="Which rows to keep",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_combine.JOIN_TYPES),
                default=ops_combine.JOIN_TYPES[0],
                help="Keeping all rows from the left table is the usual choice: you keep everything you started with, and unmatched rows get blanks.",
            ),
            ParamSpec(
                name="suffix",
                label="Add this to duplicate column names",
                kind=ParamKind.TEXT,
                required=False,
                default="_from_right",
                help="When both tables have a column with the same name, the second table's copy gets this added to its name.",
            ),
        ),
        default_output="new",
        output_name_hint="{source}_joined",
        apply=ops_combine.apply_merge,
        describe=ops_combine.describe_merge,
        validate=ops_combine.validate_merge,
        required_columns=ops_combine.required_merge,
    ),
    "lookup": OperationSpec(
        operation="lookup",
        label="Look up columns from another table",
        category=CATEGORY_COMBINE,
        summary="Bring a few columns across from another table, matched on a shared column. Like VLOOKUP.",
        inputs=(
            SOURCE_ROLE,
            InputRole(
                name="lookup_table",
                label="Look them up in",
                help="The table holding the extra columns, such as a price list or a customer master.",
            ),
        ),
        params=(
            ParamSpec(
                name="source_key",
                label="Matching column in this table",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column to match on, such as stock_item.",
            ),
            ParamSpec(
                name="lookup_key",
                label="Matching column in the other table",
                kind=ParamKind.COLUMN,
                from_role="lookup_table",
                required=False,
                help="Leave empty if it has the same name in both tables.",
            ),
            ParamSpec(
                name="bring_columns",
                label="Columns to bring across",
                kind=ParamKind.COLUMNS,
                from_role="lookup_table",
                help="Just the columns you want, such as standard_cost. Everything else in the other table is ignored.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_with_lookup",
        apply=ops_combine.apply_lookup,
        describe=ops_combine.describe_lookup,
        validate=ops_combine.validate_lookup,
        required_columns=ops_combine.required_lookup,
    ),
    "concat": OperationSpec(
        operation="concat",
        label="Append tables",
        category=CATEGORY_COMBINE,
        summary="Stack tables on top of each other, lining up columns with the same name.",
        inputs=(
            InputRole(
                name="frames",
                label="Tables to append",
                help="Pick two or more tables with the same sort of columns, such as twelve monthly files.",
                multi=True,
            ),
        ),
        params=(
            ParamSpec(
                name="add_source_column",
                label="Add a column saying which table each row came from",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=True,
                help="Adds a 'source table' column. Useful when you stack monthly files and still want to tell them apart.",
            ),
        ),
        default_output="new",
        output_name_hint="{source}_appended",
        apply=ops_combine.apply_concat,
        describe=ops_combine.describe_concat,
        validate=ops_combine.validate_concat,
        required_columns=ops_combine.required_concat,
    ),
    # ----------------------------------------------------------------------------------
    # Calculated columns
    # ----------------------------------------------------------------------------------
    "add_calculated_column": OperationSpec(
        operation="add_calculated_column",
        label="Add a calculated column",
        category=CATEGORY_CALCULATE,
        summary="Work out a new column from the others with a formula, such as basic * 0.12.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="new_column",
                label="New column name",
                kind=ParamKind.TEXT,
                placeholder="bonus",
                help="What the answer column is called.",
            ),
            ParamSpec(
                name="expression",
                label="Formula",
                kind=ParamKind.EXPRESSION,
                from_role="source",
                placeholder="basic * 0.12",
                help="Use column names, numbers, + - * / and brackets. Put a name in square brackets if it has spaces, like [net sales] * 0.1.",
            ),
            ParamSpec(
                name="decimals",
                label="Round to this many decimals",
                kind=ParamKind.INTEGER,
                required=False,
                min_value=0,
                max_value=10,
                help="Leave empty to keep the full answer.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_calculated",
        apply=ops_derive.apply_add_calculated_column,
        describe=ops_derive.describe_add_calculated_column,
        validate=ops_derive.validate_add_calculated_column,
        required_columns=ops_derive.required_add_calculated_column,
    ),
    "add_conditional_column": OperationSpec(
        operation="add_conditional_column",
        label="Add an if/else column",
        category=CATEGORY_CALCULATE,
        summary="One answer where a test passes, another where it doesn't - such as 10% for North, 5% elsewhere.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="new_column",
                label="New column name",
                kind=ParamKind.TEXT,
                placeholder="discount",
                help="What the answer column is called.",
            ),
            ParamSpec(
                name="column",
                label="Test this column",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column the test looks at, such as region.",
            ),
            ParamSpec(
                name="comparison",
                label="Test",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_rows.COMPARISONS),
                default="is",
                help="How to compare that column against the value below.",
            ),
            ParamSpec(
                name="value",
                label="Compare against",
                kind=ParamKind.TEXT,
                required=False,
                placeholder="North",
                help="The value the test compares against. Not needed for 'is blank' or 'is not blank'.",
            ),
            ParamSpec(
                name="result_if_true",
                label="Answer when the test passes",
                kind=ParamKind.TEXT,
                placeholder="net_sales * 0.1",
                help="A fixed value like 'Yes', or a formula using column names like net_sales * 0.1.",
            ),
            ParamSpec(
                name="result_if_false",
                label="Answer when it doesn't",
                kind=ParamKind.TEXT,
                required=False,
                placeholder="net_sales * 0.05",
                help="A fixed value or a formula, same as above. Leave empty for a blank.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_calculated",
        apply=ops_derive.apply_add_conditional_column,
        describe=ops_derive.describe_add_conditional_column,
        validate=ops_derive.validate_add_conditional_column,
        required_columns=ops_derive.required_add_conditional_column,
    ),
    "bucket_numeric": OperationSpec(
        operation="bucket_numeric",
        label="Put numbers into ranges",
        category=CATEGORY_CALCULATE,
        summary="Turn a number column into bands - age groups, price bands, 30/60/90 day ageing.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="column",
                label="Number column",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column to band, such as days_overdue.",
            ),
            ParamSpec(
                name="edges",
                label="Range limits",
                kind=ParamKind.TEXT,
                placeholder="30, 60, 90",
                help="The upper limit of each band, separated by commas. 30, 60, 90 gives: up to 30, up to 60, up to 90, over 90.",
            ),
            ParamSpec(
                name="new_column",
                label="New column name",
                kind=ParamKind.TEXT,
                required=False,
                help="Leave empty to name it after the column being banded.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_banded",
        apply=ops_derive.apply_bucket_numeric,
        describe=ops_derive.describe_bucket_numeric,
        validate=ops_derive.validate_bucket_numeric,
        required_columns=ops_derive.required_bucket_numeric,
    ),
    # ----------------------------------------------------------------------------------
    # Summarise
    # ----------------------------------------------------------------------------------
    "groupby_aggregate": OperationSpec(
        operation="groupby_aggregate",
        label="Group and total",
        category=CATEGORY_AGGREGATE,
        summary="One row per group, with a total, average or count worked out for each.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="group_by",
                label="Group by",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="One row comes out for each different combination of these columns, such as customer.",
            ),
            ParamSpec(
                name="value_columns",
                label="Calculate",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The columns to work out a figure from, such as amount.",
            ),
            ParamSpec(
                name="aggregation",
                label="Calculation",
                kind=ParamKind.CHOICE,
                choices=tuple(AGGREGATIONS),
                default="sum",
                help="sum adds them up, mean averages them, count counts the filled values, count_distinct counts the different ones.",
            ),
            ParamSpec(
                name="include_row_count",
                label="Also add a row count",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=False,
                help="Adds a 'row count' column saying how many rows went into each group.",
            ),
        ),
        default_output="new",
        output_name_hint="{source}_summary",
        apply=ops_aggregate.apply_groupby_aggregate,
        describe=ops_aggregate.describe_groupby_aggregate,
        validate=ops_aggregate.validate_groupby_aggregate,
        required_columns=ops_aggregate.required_groupby_aggregate,
    ),
    "pivot": OperationSpec(
        operation="pivot",
        label="Pivot",
        category=CATEGORY_AGGREGATE,
        summary="Turn one column's values into columns of their own - months across the top, customers down the side.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="row_columns",
                label="Down the side",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The columns that stay as rows, such as customer.",
            ),
            ParamSpec(
                name="column_column",
                label="Across the top",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="Each different value in this column becomes a column of its own, such as month.",
            ),
            ParamSpec(
                name="value_column",
                label="Values",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column to work out for each cell, such as amount.",
            ),
            ParamSpec(
                name="aggregation",
                label="Calculation",
                kind=ParamKind.CHOICE,
                choices=tuple(AGGREGATIONS),
                default="sum",
                help="What to do when several rows land in the same cell.",
            ),
        ),
        default_output="new",
        output_name_hint="{source}_pivot",
        apply=ops_aggregate.apply_pivot,
        describe=ops_aggregate.describe_pivot,
        validate=ops_aggregate.validate_pivot,
        required_columns=ops_aggregate.required_pivot,
    ),
    "unpivot": OperationSpec(
        operation="unpivot",
        label="Unpivot",
        category=CATEGORY_AGGREGATE,
        summary="The opposite of a pivot - turn twelve month columns into one Month column and one Value column.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="id_columns",
                label="Columns to keep as they are",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The columns that stay put and get repeated down the rows, such as customer.",
            ),
            ParamSpec(
                name="value_columns",
                label="Columns to stack up",
                kind=ParamKind.COLUMNS,
                from_role="source",
                required=False,
                help="The columns to fold into two, such as Jan to Dec. Leave empty to stack every column you didn't keep.",
            ),
            ParamSpec(
                name="variable_name",
                label="Name for the new name column",
                kind=ParamKind.TEXT,
                default=ops_clean.DEFAULT_VARIABLE_NAME,
                help="What to call the column that holds the old column headings, such as Month.",
            ),
            ParamSpec(
                name="value_name",
                label="Name for the new value column",
                kind=ParamKind.TEXT,
                default=ops_clean.DEFAULT_VALUE_NAME,
                help="What to call the column that holds the numbers, such as Amount.",
            ),
        ),
        default_output="new",
        output_name_hint="{source}_unpivot",
        apply=ops_clean.apply_unpivot,
        describe=ops_clean.describe_unpivot,
        validate=ops_clean.validate_unpivot,
        required_columns=ops_clean.required_unpivot,
    ),
    "rank_within_group": OperationSpec(
        operation="rank_within_group",
        label="Rank rows",
        category=CATEGORY_AGGREGATE,
        summary="Number the rows 1, 2, 3 by any column - overall, or starting again for each group.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="order_by",
                label="Rank by",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column that decides the order, such as sales.",
            ),
            ParamSpec(
                name="ascending",
                label="Smallest first",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=False,
                help="Off means the biggest value is rank 1, which is what 'top 10' usually means.",
            ),
            ParamSpec(
                name="group_by",
                label="Start again for each",
                kind=ParamKind.COLUMNS,
                from_role="source",
                required=False,
                help="Leave empty to rank the whole table. Pick a column to rank within each group, such as ranking products inside each region.",
            ),
            ParamSpec(
                name="method",
                label="How to handle ties",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_aggregate.RANK_METHODS),
                default=ops_aggregate.RANK_METHODS[0],
                help="What happens when two rows have the same value.",
            ),
            ParamSpec(
                name="new_column",
                label="New column name",
                kind=ParamKind.TEXT,
                required=False,
                default="rank",
                help="What the rank column is called.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_ranked",
        apply=ops_aggregate.apply_rank_within_group,
        describe=ops_aggregate.describe_rank_within_group,
        validate=ops_aggregate.validate_rank_within_group,
        required_columns=ops_aggregate.required_rank_within_group,
    ),
    "running_total": OperationSpec(
        operation="running_total",
        label="Running total",
        category=CATEGORY_AGGREGATE,
        summary="A column that adds up as it goes down the table, optionally restarting for each group.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="value_column",
                label="Total this column",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The numbers to accumulate, such as amount.",
            ),
            ParamSpec(
                name="order_by",
                label="In this order",
                kind=ParamKind.COLUMN,
                from_role="source",
                required=False,
                help="Usually a date. Leave empty to use the table's current row order.",
            ),
            ParamSpec(
                name="group_by",
                label="Start again for each",
                kind=ParamKind.COLUMNS,
                from_role="source",
                required=False,
                help="Leave empty for one running total down the whole table.",
            ),
            ParamSpec(
                name="new_column",
                label="New column name",
                kind=ParamKind.TEXT,
                required=False,
                help="Leave empty to name it after the column being totalled.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_running",
        apply=ops_aggregate.apply_running_total,
        describe=ops_aggregate.describe_running_total,
        validate=ops_aggregate.validate_running_total,
        required_columns=ops_aggregate.required_running_total,
    ),
    # ----------------------------------------------------------------------------------
    # Filter & sort
    # ----------------------------------------------------------------------------------
    "filter_rows": OperationSpec(
        operation="filter_rows",
        label="Filter rows",
        category=CATEGORY_FILTER,
        summary="Keep only the rows that match a test - or throw those rows away.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="column",
                label="Column",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column the test looks at.",
            ),
            ParamSpec(
                name="comparison",
                label="Test",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_rows.COMPARISONS),
                default="is",
                help="How to compare. Text tests ignore capitals, so 'north' matches 'North'.",
            ),
            ParamSpec(
                name="value",
                label="Value",
                kind=ParamKind.TEXT,
                required=False,
                help="What to compare against. For 'is one of', separate the values with commas. Not needed for 'is blank' or 'is not blank'.",
            ),
            ParamSpec(
                name="remove_matching",
                label="Remove the matching rows instead of keeping them",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=False,
                help="Off keeps the rows that match. On throws them away and keeps the rest.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_filtered",
        apply=ops_rows.apply_filter_rows,
        describe=ops_rows.describe_filter_rows,
        validate=ops_rows.validate_filter_rows,
        required_columns=ops_rows.required_filter_rows,
    ),
    "sort_rows": OperationSpec(
        operation="sort_rows",
        label="Sort rows",
        category=CATEGORY_FILTER,
        summary="Put the rows in order by one or more columns.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Sort by",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The first column decides the order; later ones break ties.",
            ),
            ParamSpec(
                name="ascending",
                label="Smallest / A-Z first",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=True,
                help="Off puts the largest, or Z, first. Number columns sort as numbers, so 9 comes before 10.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_sorted",
        apply=ops_rows.apply_sort_rows,
        describe=ops_rows.describe_sort_rows,
        validate=ops_rows.validate_sort_rows,
        required_columns=ops_rows.required_sort_rows,
    ),
    # ----------------------------------------------------------------------------------
    # Columns
    # ----------------------------------------------------------------------------------
    "change_dtype": OperationSpec(
        operation="change_dtype",
        label="Change column type",
        category=CATEGORY_COLUMN,
        summary="Tell the app a column holds numbers, or dates - needed before totalling or joining on it.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Columns",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The columns to change. Uploaded files are read as text so that leading zeros survive, so amounts and dates usually need this first.",
            ),
            ParamSpec(
                name="target_type",
                label="Change to",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_columns.DTYPE_CHOICES),
                default="number",
                help="Anything that can't be read as the new type becomes blank, and you'll be told how many.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_typed",
        apply=ops_columns.apply_change_dtype,
        describe=ops_columns.describe_change_dtype,
        validate=ops_columns.validate_change_dtype,
        required_columns=ops_columns.required_change_dtype,
    ),
    "drop_columns": OperationSpec(
        operation="drop_columns",
        label="Drop columns",
        category=CATEGORY_COLUMN,
        summary="Remove columns you don't need - handy straight after a join.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Columns to remove",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="Everything not picked stays.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_trimmed",
        apply=ops_columns.apply_drop_columns,
        describe=ops_columns.describe_drop_columns,
        validate=ops_columns.validate_drop_columns,
        required_columns=ops_columns.required_drop_columns,
    ),
    "rename_column": OperationSpec(
        operation="rename_column",
        label="Rename a column",
        category=CATEGORY_COLUMN,
        summary="Give one column a different name.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="column",
                label="Column",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column to rename.",
            ),
            ParamSpec(
                name="new_name",
                label="New name",
                kind=ParamKind.TEXT,
                help="What it should be called instead.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_renamed",
        apply=ops_columns.apply_rename_column,
        describe=ops_columns.describe_rename_column,
        validate=ops_columns.validate_rename_column,
        required_columns=ops_columns.required_rename_column,
    ),
    "split_column": OperationSpec(
        operation="split_column",
        label="Split a column",
        category=CATEGORY_COLUMN,
        summary="Break one column into several on a separator - a full name into first and last.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="column",
                label="Column to split",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The text column to break up.",
            ),
            ParamSpec(
                name="separator",
                label="Split on",
                kind=ParamKind.TEXT,
                placeholder="-",
                help="The character or word to split at, such as a comma, a dash or a space. Taken literally.",
            ),
            ParamSpec(
                name="mode",
                label="Keep",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_columns.SPLIT_MODES),
                default=ops_columns.SPLIT_MODES[0],
                help="Either every piece becomes its own column, or just one chosen piece does.",
            ),
            ParamSpec(
                name="piece_number",
                label="Which piece",
                kind=ParamKind.INTEGER,
                default=1,
                min_value=1,
                required=False,
                depends_on="mode",
                depends_value="one piece only",
                help="1 is the part before the first separator, 2 the next, and so on.",
            ),
            ParamSpec(
                name="new_name",
                label="New column name",
                kind=ParamKind.TEXT,
                required=False,
                depends_on="mode",
                depends_value="one piece only",
                help="Leave empty to name it after the column being split.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_split",
        apply=ops_columns.apply_split_column,
        describe=ops_columns.describe_split_column,
        validate=ops_columns.validate_split_column,
        required_columns=ops_columns.required_split_column,
    ),
    # ----------------------------------------------------------------------------------
    # Dates
    # ----------------------------------------------------------------------------------
    "extract_date_part": OperationSpec(
        operation="extract_date_part",
        label="Get a date part",
        category=CATEGORY_DATES,
        summary="Pull the year, month, quarter or financial year out of a date into its own column.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="column",
                label="Date column",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The column holding the dates. Dates are read day-first, so 03/04/2025 is the 3rd of April.",
            ),
            ParamSpec(
                name="part",
                label="Take the",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_derive.DATE_PARTS),
                default="year",
                help="Which part of the date to pull out. Group by the answer to get monthly or quarterly totals.",
            ),
            ParamSpec(
                name="fiscal_year_starts",
                label="Financial year starts in",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_derive.MONTH_CHOICES),
                default="April",
                required=False,
                depends_on="part",
                depends_value="fiscal year",
                help="With an April start, March 2026 belongs to financial year 2025.",
            ),
            ParamSpec(
                name="new_column",
                label="New column name",
                kind=ParamKind.TEXT,
                required=False,
                help="Leave empty to name it after the date column and the part.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_dated",
        apply=ops_derive.apply_extract_date_part,
        describe=ops_derive.describe_extract_date_part,
        validate=ops_derive.validate_extract_date_part,
        required_columns=ops_derive.required_extract_date_part,
    ),
    "date_difference": OperationSpec(
        operation="date_difference",
        label="Time between two dates",
        category=CATEGORY_DATES,
        summary="How long between two date columns - a holding period, an age, days overdue.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="from_column",
                label="From",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The earlier date, such as bought_on.",
            ),
            ParamSpec(
                name="to_column",
                label="To",
                kind=ParamKind.COLUMN,
                from_role="source",
                help="The later date, such as sold_on. Rows where this is earlier give a negative answer.",
            ),
            ParamSpec(
                name="unit",
                label="Measured in",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_derive.DATE_UNITS),
                default="days",
                help="Months are counted as calendar months, so 31 January to 28 February is one month.",
            ),
            ParamSpec(
                name="new_column",
                label="New column name",
                kind=ParamKind.TEXT,
                required=False,
                help="Leave empty to name it after the unit.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_dated",
        apply=ops_derive.apply_date_difference,
        describe=ops_derive.describe_date_difference,
        validate=ops_derive.validate_date_difference,
        required_columns=ops_derive.required_date_difference,
    ),
    # ----------------------------------------------------------------------------------
    # Tidy up
    # ----------------------------------------------------------------------------------
    # `skip_rows` is listed first because it is the one that usually has to run first: until
    # the junk rows above a file's real headers are gone, every column picker on this page
    # is offering `Unnamed: 1`.
    "skip_rows": OperationSpec(
        operation="skip_rows",
        label="Skip rows",
        category=CATEGORY_CLEAN,
        summary="Drop junk rows from the top or bottom of a file, and use the next row as the column headers.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="top",
                label="Rows to skip from the top",
                kind=ParamKind.INTEGER,
                required=False,
                default=0,
                min_value=0,
                help="How many rows to throw away from the start, such as a title and a blank line above the real headers.",
            ),
            ParamSpec(
                name="bottom",
                label="Rows to skip from the bottom",
                kind=ParamKind.INTEGER,
                required=False,
                default=0,
                min_value=0,
                help="How many rows to throw away from the end, such as a totals line.",
            ),
            ParamSpec(
                name="promote_header",
                label="Use the next row as the column headers",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=False,
                help="Turns the first row that is left into the column names. Tick this when your columns are showing as Unnamed.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_trimmed",
        apply=ops_clean.apply_skip_rows,
        describe=ops_clean.describe_skip_rows,
        validate=ops_clean.validate_skip_rows,
        required_columns=ops_clean.required_skip_rows,
    ),
    "remove_empty_rows": OperationSpec(
        operation="remove_empty_rows",
        label="Remove empty rows",
        category=CATEGORY_CLEAN,
        summary="Throw away rows that have nothing in them.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Only look at these columns",
                kind=ParamKind.COLUMNS,
                from_role="source",
                required=False,
                help="Leave empty to drop a row only when every column is blank. Pick columns to drop a row when just those are blank.",
            ),
            ParamSpec(
                name="blank_strings_count_as_empty",
                label="Treat spaces as blank",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=True,
                help="On means a cell holding only spaces counts as empty, which is what a file exported from another system usually has.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_cleaned",
        apply=ops_clean.apply_remove_empty_rows,
        describe=ops_clean.describe_remove_empty_rows,
        validate=ops_clean.validate_remove_empty_rows,
        required_columns=ops_clean.required_remove_empty_rows,
    ),
    "trim_whitespace": OperationSpec(
        operation="trim_whitespace",
        label="Trim spaces",
        category=CATEGORY_CLEAN,
        summary="Strip spaces from around every text value - the usual reason a join finds no matches.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="collapse_internal",
                label="Also squeeze repeated spaces inside the text",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=True,
                help="On turns 'ACME   Ltd' into 'ACME Ltd'. Off only trims the ends.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_trimmed",
        apply=ops_clean.apply_trim_whitespace,
        describe=ops_clean.describe_trim_whitespace,
        validate=ops_clean.validate_trim_whitespace,
        required_columns=ops_clean.required_trim_whitespace,
    ),
    "remove_special_characters": OperationSpec(
        operation="remove_special_characters",
        label="Remove special characters",
        category=CATEGORY_CLEAN,
        summary="Keep only the characters you want in every text column.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="keep_pattern",
                label="Characters to keep",
                kind=ParamKind.TEXT,
                default=ops_clean.DEFAULT_KEEP_PATTERN,
                help="Letters, numbers and ordinary punctuation by default. Anything not listed here is taken out.",
            ),
            ParamSpec(
                name="replacement",
                label="Put this in their place",
                kind=ParamKind.TEXT,
                required=False,
                help="Leave empty to simply delete them. Type a space to turn each one into a space instead.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_cleaned",
        apply=ops_clean.apply_remove_special_characters,
        describe=ops_clean.describe_remove_special_characters,
        validate=ops_clean.validate_remove_special_characters,
        required_columns=ops_clean.required_remove_special_characters,
    ),
    "change_case": OperationSpec(
        operation="change_case",
        label="Change letter case",
        category=CATEGORY_CLEAN,
        summary="Put text into UPPERCASE, lowercase or Title Case - handy before matching two tables.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Columns",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The text columns to change. They all get the same case - add a second step if you need a different one.",
            ),
            ParamSpec(
                name="case",
                label="Change to",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_clean.CASE_CHOICES),
                default="UPPERCASE",
                help="Title Case puts a capital on each word.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_cased",
        apply=ops_clean.apply_change_case,
        describe=ops_clean.describe_change_case,
        validate=ops_clean.validate_change_case,
        required_columns=ops_clean.required_change_case,
    ),
    "find_replace": OperationSpec(
        operation="find_replace",
        label="Find & replace",
        category=CATEGORY_CLEAN,
        summary="Swap one piece of text for another in the columns you choose.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Columns to search in",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="Only these columns are changed.",
            ),
            ParamSpec(
                name="find",
                label="Find",
                kind=ParamKind.TEXT,
                placeholder="Pvt Ltd",
                help="The text to look for.",
            ),
            ParamSpec(
                name="replace",
                label="Replace with",
                kind=ParamKind.TEXT,
                required=False,
                placeholder="Private Limited",
                help="Leave empty to delete the text you found.",
            ),
            ParamSpec(
                name="case_sensitive",
                label="Match upper and lower case exactly",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=True,
                help="Off finds 'acme' as well as 'ACME'.",
            ),
            ParamSpec(
                name="regex",
                label="Treat what I typed as a pattern",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=False,
                help="For advanced use. Off is what you want unless you know regular expressions.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_replaced",
        apply=ops_clean.apply_find_replace,
        describe=ops_clean.describe_find_replace,
        validate=ops_clean.validate_find_replace,
        required_columns=ops_clean.required_find_replace,
    ),
    "fix_numeric_text": OperationSpec(
        operation="fix_numeric_text",
        label="Fix numbers stored as text",
        category=CATEGORY_CLEAN,
        summary="Store 1,250.00 and (500) as real numbers - useful before rounding, or so Excel treats them as numbers.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Columns",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The columns holding numbers that came in as text.",
            ),
            ParamSpec(
                name="decimal_separator",
                label="Decimal point",
                kind=ParamKind.CHOICE,
                choices=ops_clean.DECIMAL_SEPARATORS,
                default=".",
                help="Pick ',' only for a file written the European way, where 1.234,56 means one thousand two hundred and thirty four point five six.",
            ),
            ParamSpec(
                name="parentheses_are_negative",
                label="Read (500) as minus 500",
                kind=ParamKind.BOOLEAN,
                required=False,
                default=True,
                help="On is the accounting convention, where brackets mean a negative number.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_numbers",
        apply=ops_clean.apply_fix_numeric_text,
        describe=ops_clean.describe_fix_numeric_text,
        validate=ops_clean.validate_fix_numeric_text,
        required_columns=ops_clean.required_fix_numeric_text,
    ),
    "round_numbers": OperationSpec(
        operation="round_numbers",
        label="Round numbers",
        category=CATEGORY_CLEAN,
        summary="Round number columns to the decimal places you want.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Columns",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The columns to round. A column still stored as text is left alone and says so.",
            ),
            ParamSpec(
                name="decimals",
                label="Decimal places",
                kind=ParamKind.INTEGER,
                default=2,
                min_value=0,
                max_value=ops_clean.MAX_ROUNDING_DECIMALS,
                help="0 gives whole numbers, 2 gives paise or cents.",
            ),
            ParamSpec(
                name="direction",
                label="Round to",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_clean.ROUNDING_CHOICES),
                default="the nearest value",
                help="'The nearest value' is ordinary rounding. Up and down always go that way.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_rounded",
        apply=ops_clean.apply_round_numbers,
        describe=ops_clean.describe_round_numbers,
        validate=ops_clean.validate_round_numbers,
        required_columns=ops_clean.required_round_numbers,
    ),
    "fill_missing": OperationSpec(
        operation="fill_missing",
        label="Fill blanks",
        category=CATEGORY_CLEAN,
        summary="Put something in the empty cells - useful after a join leaves gaps.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Columns",
                kind=ParamKind.COLUMNS,
                from_role="source",
                help="The columns whose blanks should be filled. All of them get the same treatment.",
            ),
            ParamSpec(
                name="strategy",
                label="Fill with",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_rows.FILL_STRATEGIES),
                default="zero",
                help="A cell holding only spaces counts as blank too.",
            ),
            ParamSpec(
                name="value",
                label="Value",
                kind=ParamKind.TEXT,
                required=False,
                depends_on="strategy",
                depends_value="a value I type",
                help="What to put in the blank cells, such as Unknown or 0.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_filled",
        apply=ops_rows.apply_fill_missing,
        describe=ops_rows.describe_fill_missing,
        validate=ops_rows.validate_fill_missing,
        required_columns=ops_rows.required_fill_missing,
    ),
    "drop_duplicates": OperationSpec(
        operation="drop_duplicates",
        label="Remove duplicate rows",
        category=CATEGORY_CLEAN,
        summary="Keep one row of each repeat - useful when a join has multiplied rows.",
        inputs=(SOURCE_ROLE,),
        params=(
            ParamSpec(
                name="columns",
                label="Rows count as the same when these match",
                kind=ParamKind.COLUMNS,
                from_role="source",
                required=False,
                help="Leave empty to treat rows as duplicates only when every column matches.",
            ),
            ParamSpec(
                name="keep",
                label="Keep the",
                kind=ParamKind.CHOICE,
                choices=tuple(ops_rows.KEEP_CHOICES),
                default="first",
                help="Which of each set of duplicates to keep. Sort the table first if it matters which one that is.",
            ),
        ),
        default_output="in_place",
        output_name_hint="{source}_deduped",
        apply=ops_rows.apply_drop_duplicates,
        describe=ops_rows.describe_drop_duplicates,
        validate=ops_rows.validate_drop_duplicates,
        required_columns=ops_rows.required_drop_duplicates,
    ),
}


def get_operation(operation: str) -> OperationSpec:
    """The spec for `operation`.

    Raises:
        InvalidOperationError: if it isn't in the registry. The likely cause is a pipeline
            saved by a newer version of the app, so the message says so rather than
            implying the user mistyped something.
    """
    try:
        return OPERATION_REGISTRY[operation]
    except KeyError as error:
        logger.error("Unknown transform operation requested: %s", operation)
        raise InvalidOperationError(
            f"'{operation}' isn't a step this version of the app knows about."
        ) from error


def operations_by_category() -> dict[str, list[OperationSpec]]:
    """Every operation grouped for the picker, categories in `CATEGORY_ORDER`.

    Grouped rather than one flat list because the catalog is meant to grow — requirement
    4.2 anticipates dozens — and a flat dropdown of seventy items stops being usable long
    before that.
    """
    grouped: dict[str, list[OperationSpec]] = {category: [] for category in CATEGORY_ORDER}
    for spec in OPERATION_REGISTRY.values():
        grouped.setdefault(spec.category, []).append(spec)
    return {category: specs for category, specs in grouped.items() if specs}
