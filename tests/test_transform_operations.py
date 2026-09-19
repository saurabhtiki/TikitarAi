"""What each operation actually does to the data.

The shared test at the top matters as much as the specific ones below it: it runs every
registered operation and asserts the input frame comes back untouched. pandas 3 enforces
copy-on-write, so an executor that mutates its input is a bug that would otherwise surface
much later, as a replay producing a different answer the second time it runs.
"""

import pandas as pd
import pytest

from transform.exceptions import InvalidStepParamsError
from transform.ops_aggregate import (
    apply_groupby_aggregate,
    apply_pivot,
    apply_rank_within_group,
    apply_running_total,
)
from transform.ops_columns import (
    apply_change_dtype,
    apply_drop_columns,
    apply_extract_by_position,
    apply_rename_column,
    apply_split_column,
    validate_extract_by_position,
)
from transform.ops_combine import apply_concat, apply_lookup, apply_merge
from transform.ops_derive import (
    apply_absolute_value,
    apply_add_calculated_column,
    apply_add_conditional_column,
    apply_add_today_date,
    apply_bucket_numeric,
    apply_date_difference,
    apply_extract_date_part,
    apply_month_edge,
    apply_row_min_max,
    apply_row_percentage_of_total,
    apply_shift_date,
    validate_absolute_value,
    validate_add_calculated_column,
    validate_month_edge,
    validate_row_min_max,
    validate_row_percentage_of_total,
    validate_shift_date,
)
from transform.ops_rows import (
    apply_drop_duplicates,
    apply_fill_missing,
    apply_filter_rows,
    apply_sort_rows,
)


@pytest.fixture
def sales() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "customer_id": ["1", "2", "1", "3"],
            "region": ["North", "South", "North", "East"],
            "amount": ["100", "200", "300", "400"],
            "sold_on": ["01/04/2025", "15/06/2025", "02/01/2026", "31/03/2026"],
        }
    )


@pytest.fixture
def customers() -> pd.DataFrame:
    return pd.DataFrame(
        {"customer_id": ["1", "2", "3"], "customer": ["Acme", "Bolt", "Cog"], "tier": ["A", "B", "A"]}
    )


# --------------------------------------------------------------------------------------
# The contract every executor keeps
# --------------------------------------------------------------------------------------

EXECUTOR_CASES = [
    ("change_dtype", apply_change_dtype, {"columns": ["amount"], "target_type": "number"}),
    ("drop_columns", apply_drop_columns, {"columns": ["region"]}),
    ("rename_column", apply_rename_column, {"column": "region", "new_name": "area"}),
    ("split_column", apply_split_column, {"column": "sold_on", "separator": "/"}),
    ("filter_rows", apply_filter_rows, {"column": "region", "comparison": "is", "value": "North"}),
    ("sort_rows", apply_sort_rows, {"columns": ["amount"], "ascending": True}),
    ("fill_missing", apply_fill_missing, {"columns": ["region"], "strategy": "zero"}),
    ("drop_duplicates", apply_drop_duplicates, {"columns": ["customer_id"], "keep": "first"}),
    (
        "add_calculated_column",
        apply_add_calculated_column,
        {"new_column": "doubled", "expression": "amount * 2"},
    ),
    (
        "add_conditional_column",
        apply_add_conditional_column,
        {
            "new_column": "flag",
            "column": "region",
            "comparison": "is",
            "value": "North",
            "result_if_true": "Y",
            "result_if_false": "N",
        },
    ),
    ("bucket_numeric", apply_bucket_numeric, {"column": "amount", "edges": "150, 350"}),
    ("extract_date_part", apply_extract_date_part, {"column": "sold_on", "part": "year"}),
    (
        "date_difference",
        apply_date_difference,
        {"from_column": "sold_on", "to_column": "sold_on", "unit": "days"},
    ),
    (
        "groupby_aggregate",
        apply_groupby_aggregate,
        {"group_by": ["region"], "value_columns": ["amount"], "aggregation": "sum"},
    ),
    (
        "pivot",
        apply_pivot,
        {
            "row_columns": ["region"],
            "column_column": "customer_id",
            "value_column": "amount",
            "aggregation": "sum",
        },
    ),
    ("rank_within_group", apply_rank_within_group, {"order_by": "amount"}),
    ("running_total", apply_running_total, {"value_column": "amount"}),
    ("add_today_date", apply_add_today_date, {"new_column": "Loaded_On"}),
    ("shift_date", apply_shift_date, {"column": "sold_on", "days": 30}),
    ("month_edge", apply_month_edge, {"column": "sold_on", "edge": "end of month"}),
    ("row_percentage_of_total", apply_row_percentage_of_total, {"column": "amount"}),
    ("absolute_value", apply_absolute_value, {"column": "amount"}),
    (
        "row_min_max",
        apply_row_min_max,
        {"columns": ["customer_id", "amount"], "which": "largest"},
    ),
    (
        "extract_by_position",
        apply_extract_by_position,
        {"column": "region", "start": 1, "length": 3},
    ),
]


@pytest.mark.parametrize(("name", "executor", "params"), EXECUTOR_CASES, ids=[c[0] for c in EXECUTOR_CASES])
def test_an_executor_never_changes_the_table_it_was_given(sales, name, executor, params):
    before = sales.copy(deep=True)
    result, warnings_out = executor({"source": sales}, params)

    pd.testing.assert_frame_equal(sales, before)
    assert isinstance(result, pd.DataFrame)
    assert isinstance(warnings_out, list)
    assert result is not sales


# --------------------------------------------------------------------------------------
# change_dtype
# --------------------------------------------------------------------------------------


class TestChangeDtype:
    def test_text_amounts_become_numbers(self, sales):
        result, _ = apply_change_dtype({"source": sales}, {"columns": ["amount"], "target_type": "number"})
        assert result["amount"].sum() == 1000.0

    def test_values_that_wont_convert_become_blank_and_are_reported(self):
        frame = pd.DataFrame({"amount": ["100", "oops", "300"]})
        result, warnings_out = apply_change_dtype(
            {"source": frame}, {"columns": ["amount"], "target_type": "number"}
        )
        assert result["amount"].isna().sum() == 1
        assert "couldn't be read as a number" in warnings_out[0]
        assert '"oops"' in warnings_out[0]

    def test_dates_are_read_day_first(self, sales):
        result, _ = apply_change_dtype({"source": sales}, {"columns": ["sold_on"], "target_type": "date"})
        assert result["sold_on"].iloc[0].month == 4

    def test_a_whole_number_column_still_holds_blanks(self):
        frame = pd.DataFrame({"qty": ["1", "", "3"]})
        result, _ = apply_change_dtype(
            {"source": frame}, {"columns": ["qty"], "target_type": "whole number"}
        )
        assert str(result["qty"].dtype) == "Int64"

    def test_yes_and_no_become_true_and_false(self):
        frame = pd.DataFrame({"active": ["Yes", "no", "maybe"]})
        result, warnings_out = apply_change_dtype(
            {"source": frame}, {"columns": ["active"], "target_type": "true/false"}
        )
        assert result["active"].tolist()[:2] == [True, False]
        assert "neither true nor false" in warnings_out[0]

    def test_a_column_that_has_gone_is_skipped_and_reported(self, sales):
        result, warnings_out = apply_change_dtype(
            {"source": sales}, {"columns": ["nowhere"], "target_type": "number"}
        )
        assert "Skipped missing column" in warnings_out[0]
        assert list(result.columns) == list(sales.columns)


# --------------------------------------------------------------------------------------
# Joining
# --------------------------------------------------------------------------------------


class TestMerge:
    def test_a_left_join_keeps_every_row_and_adds_the_other_tables_columns(self, sales, customers):
        result, _ = apply_merge(
            {"left": sales, "right": customers},
            {"left_on": ["customer_id"], "join_type": "keep all rows from the left table"},
        )
        assert len(result) == 4
        assert "customer" in result.columns

    def test_an_inner_join_drops_rows_that_found_no_match(self, sales):
        partial = pd.DataFrame({"customer_id": ["1"], "customer": ["Acme"]})
        result, _ = apply_merge(
            {"left": sales, "right": partial},
            {"left_on": ["customer_id"], "join_type": "keep only rows that match"},
        )
        assert len(result) == 2

    def test_keys_of_different_types_are_matched_as_text_and_the_mismatch_is_named(self, sales):
        numeric_keys = pd.DataFrame({"customer_id": [1, 2, 3], "customer": ["Acme", "Bolt", "Cog"]})
        result, warnings_out = apply_merge(
            {"left": sales, "right": numeric_keys},
            {"left_on": ["customer_id"], "join_type": "keep all rows from the left table"},
        )
        assert result["customer"].notna().all()
        assert any("matched as text" in warning for warning in warnings_out)

    def test_a_repeated_key_multiplies_rows_and_says_so(self, sales):
        repeated = pd.DataFrame({"customer_id": ["1", "1"], "customer": ["Acme", "Acme Ltd"]})
        result, warnings_out = apply_merge(
            {"left": sales, "right": repeated},
            {"left_on": ["customer_id"], "join_type": "keep all rows from the left table"},
        )
        assert len(result) > len(sales)
        assert any("matched more than once" in warning for warning in warnings_out)

    def test_rows_that_matched_nothing_are_counted(self, sales):
        partial = pd.DataFrame({"customer_id": ["1"], "customer": ["Acme"]})
        _, warnings_out = apply_merge(
            {"left": sales, "right": partial},
            {"left_on": ["customer_id"], "join_type": "keep all rows from the left table"},
        )
        assert any("found no match" in warning for warning in warnings_out)

    def test_a_shared_column_name_gets_the_chosen_suffix(self, sales):
        other = pd.DataFrame({"customer_id": ["1"], "region": ["Elsewhere"]})
        result, _ = apply_merge(
            {"left": sales, "right": other},
            {
                "left_on": ["customer_id"],
                "join_type": "keep all rows from the left table",
                "suffix": "_other",
            },
        )
        assert "region_other" in result.columns

    def test_mismatched_key_counts_are_refused(self, sales, customers):
        with pytest.raises(InvalidStepParamsError, match="same number of matching columns"):
            apply_merge(
                {"left": sales, "right": customers},
                {"left_on": ["customer_id", "region"], "right_on": ["customer_id"]},
            )


class TestLookup:
    def test_it_brings_only_the_chosen_columns(self, sales, customers):
        result, _ = apply_lookup(
            {"source": sales, "lookup_table": customers},
            {"source_key": "customer_id", "bring_columns": ["customer"]},
        )
        assert "customer" in result.columns
        assert "tier" not in result.columns

    def test_it_never_multiplies_the_rows_of_the_table_it_adds_to(self, sales):
        repeated = pd.DataFrame(
            {"customer_id": ["1", "1", "2"], "customer": ["Acme", "Acme Ltd", "Bolt"]}
        )
        result, warnings_out = apply_lookup(
            {"source": sales, "lookup_table": repeated},
            {"source_key": "customer_id", "bring_columns": ["customer"]},
        )
        assert len(result) == len(sales)
        assert any("the first match for each was used" in warning.lower() for warning in warnings_out)

    def test_unmatched_rows_get_blanks_and_are_counted(self, sales):
        partial = pd.DataFrame({"customer_id": ["1"], "customer": ["Acme"]})
        result, warnings_out = apply_lookup(
            {"source": sales, "lookup_table": partial},
            {"source_key": "customer_id", "bring_columns": ["customer"]},
        )
        assert result["customer"].isna().sum() == 2
        assert any("no matching" in warning for warning in warnings_out)

    def test_the_key_may_be_called_something_else_in_the_other_table(self, sales):
        other = pd.DataFrame({"cust": ["1", "2", "3"], "customer": ["Acme", "Bolt", "Cog"]})
        result, _ = apply_lookup(
            {"source": sales, "lookup_table": other},
            {"source_key": "customer_id", "lookup_key": "cust", "bring_columns": ["customer"]},
        )
        assert result["customer"].tolist() == ["Acme", "Bolt", "Acme", "Cog"]

    def test_bringing_nothing_across_is_refused(self, sales, customers):
        with pytest.raises(InvalidStepParamsError, match="at least one column"):
            apply_lookup(
                {"source": sales, "lookup_table": customers},
                {"source_key": "customer_id", "bring_columns": []},
            )


class TestConcat:
    def test_two_tables_stack(self, sales):
        result, _ = apply_concat({"frames": [sales, sales]}, {"_frame_names": ["a", "b"]})
        assert len(result) == 8

    def test_a_column_only_one_table_has_is_kept_and_reported(self, sales):
        extra = sales.copy()
        extra["note"] = "x"
        result, warnings_out = apply_concat(
            {"frames": [sales, extra]}, {"_frame_names": ["a", "b"], "add_source_column": False}
        )
        assert "note" in result.columns
        assert any("aren't in every table" in warning for warning in warnings_out)

    def test_it_can_label_each_row_with_the_table_it_came_from(self, sales):
        result, _ = apply_concat(
            {"frames": [sales, sales]},
            {"_frame_names": ["january", "february"], "add_source_column": True},
        )
        assert set(result["source table"]) == {"january", "february"}

    def test_tables_with_nothing_in_common_are_refused(self, sales):
        unrelated = pd.DataFrame({"totally": ["different"]})
        with pytest.raises(InvalidStepParamsError, match="no column names in common"):
            apply_concat({"frames": [sales, unrelated]}, {"_frame_names": ["a", "b"]})

    def test_one_table_is_not_enough(self, sales):
        with pytest.raises(InvalidStepParamsError, match="at least two tables"):
            apply_concat({"frames": [sales]}, {})


# --------------------------------------------------------------------------------------
# Filtering and sorting
# --------------------------------------------------------------------------------------


class TestFilterRows:
    def test_text_matching_ignores_capitals(self, sales):
        result, _ = apply_filter_rows(
            {"source": sales}, {"column": "region", "comparison": "is", "value": "north"}
        )
        assert len(result) == 2

    def test_a_numeric_comparison_reads_text_amounts_as_numbers(self, sales):
        result, _ = apply_filter_rows(
            {"source": sales}, {"column": "amount", "comparison": "is greater than", "value": "250"}
        )
        assert result["amount"].tolist() == ["300", "400"]

    def test_is_one_of_takes_a_comma_separated_list(self, sales):
        result, _ = apply_filter_rows(
            {"source": sales},
            {"column": "region", "comparison": "is one of", "value": "North, East"},
        )
        assert len(result) == 3

    def test_is_blank_finds_cells_holding_only_spaces(self):
        frame = pd.DataFrame({"note": ["a", "   ", ""]})
        result, _ = apply_filter_rows({"source": frame}, {"column": "note", "comparison": "is blank"})
        assert len(result) == 2

    def test_the_matching_rows_can_be_thrown_away_instead(self, sales):
        result, _ = apply_filter_rows(
            {"source": sales},
            {"column": "region", "comparison": "is", "value": "North", "remove_matching": True},
        )
        assert len(result) == 2
        assert "North" not in result["region"].tolist()

    def test_contains_finds_part_of_a_value(self, sales):
        result, _ = apply_filter_rows(
            {"source": sales}, {"column": "region", "comparison": "contains", "value": "ort"}
        )
        assert len(result) == 2

    def test_matching_nothing_says_the_table_is_now_empty(self, sales):
        result, warnings_out = apply_filter_rows(
            {"source": sales}, {"column": "region", "comparison": "is", "value": "Atlantis"}
        )
        assert result.empty
        assert any("now empty" in warning for warning in warnings_out)

    def test_comparing_a_number_against_a_word_is_refused(self, sales):
        with pytest.raises(InvalidStepParamsError, match="isn't a number"):
            apply_filter_rows(
                {"source": sales},
                {"column": "amount", "comparison": "is greater than", "value": "lots"},
            )


class TestSortRows:
    def test_numeric_text_sorts_as_numbers_not_alphabetically(self):
        frame = pd.DataFrame({"amount": ["10", "9", "100"]})
        result, _ = apply_sort_rows({"source": frame}, {"columns": ["amount"], "ascending": True})
        assert result["amount"].tolist() == ["9", "10", "100"]

    def test_descending_reverses_it(self):
        frame = pd.DataFrame({"amount": ["10", "9", "100"]})
        result, _ = apply_sort_rows({"source": frame}, {"columns": ["amount"], "ascending": False})
        assert result["amount"].tolist() == ["100", "10", "9"]

    def test_text_sorts_without_regard_to_capitals(self):
        frame = pd.DataFrame({"name": ["banana", "Apple", "cherry"]})
        result, _ = apply_sort_rows({"source": frame}, {"columns": ["name"], "ascending": True})
        assert result["name"].tolist() == ["Apple", "banana", "cherry"]


class TestFillMissingAndDropDuplicates:
    def test_blanks_are_filled_with_a_typed_value(self):
        frame = pd.DataFrame({"region": ["North", None, "  "]})
        result, _ = apply_fill_missing(
            {"source": frame},
            {"columns": ["region"], "strategy": "a value I type", "value": "Unknown"},
        )
        assert result["region"].tolist() == ["North", "Unknown", "Unknown"]

    def test_blanks_can_take_the_value_above(self):
        frame = pd.DataFrame({"region": ["North", None, None]})
        result, _ = apply_fill_missing(
            {"source": frame}, {"columns": ["region"], "strategy": "the value above"}
        )
        assert result["region"].tolist() == ["North", "North", "North"]

    def test_duplicates_are_removed_and_counted(self, sales):
        result, warnings_out = apply_drop_duplicates(
            {"source": sales}, {"columns": ["customer_id"], "keep": "first"}
        )
        assert len(result) == 3
        assert any("duplicate row" in warning for warning in warnings_out)

    def test_with_no_columns_chosen_a_whole_row_must_match(self, sales):
        doubled = pd.concat([sales, sales], ignore_index=True)
        result, _ = apply_drop_duplicates({"source": doubled}, {"columns": [], "keep": "first"})
        assert len(result) == 4


# --------------------------------------------------------------------------------------
# Calculated columns
# --------------------------------------------------------------------------------------


class TestCalculatedColumns:
    def test_a_formula_becomes_a_new_column(self, sales):
        result, _ = apply_add_calculated_column(
            {"source": sales}, {"new_column": "commission", "expression": "amount * 0.1"}
        )
        assert result["commission"].tolist() == [10.0, 20.0, 30.0, 40.0]

    def test_it_can_be_rounded(self, sales):
        result, _ = apply_add_calculated_column(
            {"source": sales},
            {"new_column": "third", "expression": "amount / 3", "decimals": 2},
        )
        assert result["third"].iloc[0] == 33.33

    def test_a_name_already_in_use_is_given_a_suffix_rather_than_overwriting(self, sales):
        result, warnings_out = apply_add_calculated_column(
            {"source": sales}, {"new_column": "amount", "expression": "amount * 2"}
        )
        assert "amount_2" in result.columns
        assert any("already existed" in warning for warning in warnings_out)

    def test_a_formula_can_choose_between_two_numbers(self, sales):
        result, _ = apply_add_calculated_column(
            {"source": sales},
            {
                "new_column": "payable",
                "expression": "amount * 0.9 if amount > 200 else amount",
            },
        )
        assert result["payable"].tolist() == [100.0, 200.0, 270.0, 360.0]

    def test_a_chosen_answer_is_rounded_like_any_other(self, sales):
        result, _ = apply_add_calculated_column(
            {"source": sales},
            {
                "new_column": "share",
                "expression": "amount / 3 if amount > 200 else 0",
                "decimals": 2,
            },
        )
        assert result["share"].tolist() == [0.0, 0.0, 100.0, 133.33]

    def test_a_chosen_answer_still_gets_a_suffix_when_the_name_is_taken(self, sales):
        result, warnings_out = apply_add_calculated_column(
            {"source": sales},
            {"new_column": "amount", "expression": "1 if amount > 200 else 0"},
        )
        assert result["amount_2"].tolist() == [0.0, 0.0, 1.0, 1.0]
        assert any("already existed" in warning for warning in warnings_out)

    def test_a_formula_that_is_only_a_test_becomes_a_one_or_zero_flag(self, sales):
        result, _ = apply_add_calculated_column(
            {"source": sales}, {"new_column": "big", "expression": "amount > 200"}
        )
        assert result["big"].tolist() == [0.0, 0.0, 1.0, 1.0]

    def test_a_formula_with_an_if_but_no_else_is_refused_before_it_runs(self, sales):
        with pytest.raises(InvalidStepParamsError, match="'if' but no 'else'"):
            validate_add_calculated_column(
                {"source": list(sales.columns)},
                {"new_column": "payable", "expression": "amount * 0.9 if amount > 200"},
            )

    def test_an_if_else_column_picks_between_two_fixed_values(self, sales):
        result, _ = apply_add_conditional_column(
            {"source": sales},
            {
                "new_column": "priority",
                "column": "region",
                "comparison": "is",
                "value": "North",
                "result_if_true": "High",
                "result_if_false": "Low",
            },
        )
        assert result["priority"].tolist() == ["High", "Low", "High", "Low"]

    def test_an_if_else_answer_can_itself_be_a_formula(self, sales):
        result, _ = apply_add_conditional_column(
            {"source": sales},
            {
                "new_column": "discount",
                "column": "region",
                "comparison": "is",
                "value": "North",
                "result_if_true": "amount * 0.1",
                "result_if_false": "amount * 0.05",
            },
        )
        assert result["discount"].tolist() == [10.0, 10.0, 30.0, 20.0]

    def test_an_if_else_answer_that_is_just_a_column_keeps_its_text(self, sales):
        """A bare column name is how a conditional update carries an untouched column's
        text forward. Running it through the arithmetic formula evaluator would turn
        `region` into blanks, since 'North' and 'South' aren't numbers."""
        result, _ = apply_add_conditional_column(
            {"source": sales},
            {
                "new_column": "region_updated",
                "column": "amount",
                "comparison": "is less than",
                "value": "150",
                "result_if_true": "West",
                "result_if_false": "region",
            },
        )
        assert result["region_updated"].tolist() == ["West", "South", "North", "East"]


class TestBucketNumeric:
    def test_numbers_fall_into_the_named_bands(self, sales):
        result, _ = apply_bucket_numeric({"source": sales}, {"column": "amount", "edges": "150, 350"})
        assert result["amount band"].tolist() == [
            "up to 150",
            "up to 350",
            "up to 350",
            "over 350",
        ]

    def test_a_band_edge_belongs_to_the_band_below_it(self):
        frame = pd.DataFrame({"score": ["30", "31"]})
        result, _ = apply_bucket_numeric({"source": frame}, {"column": "score", "edges": "30"})
        assert result["score band"].tolist() == ["up to 30", "over 30"]

    def test_limits_that_arent_numbers_are_refused(self, sales):
        with pytest.raises(InvalidStepParamsError, match="isn't a number"):
            apply_bucket_numeric({"source": sales}, {"column": "amount", "edges": "low, high"})

    def test_no_limits_at_all_is_refused(self, sales):
        with pytest.raises(InvalidStepParamsError, match="range limits"):
            apply_bucket_numeric({"source": sales}, {"column": "amount", "edges": ""})


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------


class TestDates:
    def test_the_year_comes_out_as_a_number(self, sales):
        result, _ = apply_extract_date_part({"source": sales}, {"column": "sold_on", "part": "year"})
        assert result["sold_on year"].tolist() == [2025, 2025, 2026, 2026]

    def test_a_date_in_march_belongs_to_the_previous_financial_year(self, sales):
        result, _ = apply_extract_date_part(
            {"source": sales},
            {"column": "sold_on", "part": "fiscal year", "fiscal_year_starts": "April"},
        )
        # 01/04/2025 starts FY2025; 31/03/2026 is the last day of that same year.
        assert result["sold_on fiscal year"].tolist() == [2025, 2025, 2025, 2025]

    def test_the_financial_year_start_month_can_be_changed(self, sales):
        result, _ = apply_extract_date_part(
            {"source": sales},
            {"column": "sold_on", "part": "fiscal year", "fiscal_year_starts": "January"},
        )
        assert result["sold_on fiscal year"].tolist() == [2025, 2025, 2026, 2026]

    def test_the_month_name_comes_out_as_a_word(self, sales):
        result, _ = apply_extract_date_part(
            {"source": sales}, {"column": "sold_on", "part": "month name"}
        )
        assert result["sold_on month name"].iloc[0] == "April"

    def test_days_between_two_dates_counts_across_a_year_end(self):
        frame = pd.DataFrame({"start": ["31/12/2025"], "end": ["01/01/2026"]})
        result, _ = apply_date_difference(
            {"source": frame}, {"from_column": "start", "to_column": "end", "unit": "days"}
        )
        assert result["days between"].tolist() == [1]

    def test_months_are_counted_as_calendar_months(self):
        frame = pd.DataFrame({"start": ["31/01/2026"], "end": ["28/02/2026"]})
        result, _ = apply_date_difference(
            {"source": frame}, {"from_column": "start", "to_column": "end", "unit": "months"}
        )
        assert result["months between"].tolist() == [1]

    def test_a_backwards_pair_gives_a_negative_answer_and_is_reported(self):
        frame = pd.DataFrame({"start": ["10/01/2026"], "end": ["01/01/2026"]})
        result, warnings_out = apply_date_difference(
            {"source": frame}, {"from_column": "start", "to_column": "end", "unit": "days"}
        )
        assert result["days between"].tolist() == [-9]
        assert any("negative" in warning for warning in warnings_out)


# --------------------------------------------------------------------------------------
# Summarising
# --------------------------------------------------------------------------------------


class TestGroupByAggregate:
    def test_one_row_comes_out_per_group(self, sales):
        result, _ = apply_groupby_aggregate(
            {"source": sales},
            {"group_by": ["region"], "value_columns": ["amount"], "aggregation": "sum"},
        )
        assert len(result) == 3
        assert result.loc[result["region"] == "North", "sum of amount"].iloc[0] == 400.0

    def test_the_answer_column_says_what_was_done_to_it(self, sales):
        result, _ = apply_groupby_aggregate(
            {"source": sales},
            {"group_by": ["region"], "value_columns": ["amount"], "aggregation": "mean"},
        )
        assert "mean of amount" in result.columns

    def test_a_row_count_can_be_added(self, sales):
        result, _ = apply_groupby_aggregate(
            {"source": sales},
            {
                "group_by": ["region"],
                "value_columns": ["amount"],
                "aggregation": "sum",
                "include_row_count": True,
            },
        )
        assert result.loc[result["region"] == "North", "row count"].iloc[0] == 2

    def test_a_blank_grouping_value_becomes_its_own_group_rather_than_vanishing(self):
        frame = pd.DataFrame({"region": ["North", None], "amount": ["10", "20"]})
        result, _ = apply_groupby_aggregate(
            {"source": frame},
            {"group_by": ["region"], "value_columns": ["amount"], "aggregation": "sum"},
        )
        assert len(result) == 2

    def test_totalling_a_text_column_is_refused_with_a_useful_message(self, sales):
        with pytest.raises(InvalidStepParamsError, match="aren't numbers"):
            apply_groupby_aggregate(
                {"source": sales},
                {"group_by": ["customer_id"], "value_columns": ["region"], "aggregation": "sum"},
            )

    def test_counting_a_text_column_is_perfectly_fine(self, sales):
        result, _ = apply_groupby_aggregate(
            {"source": sales},
            {"group_by": ["customer_id"], "value_columns": ["region"], "aggregation": "count"},
        )
        assert result["count of region"].sum() == 4


class TestPivot:
    def test_values_become_columns(self, sales):
        result, _ = apply_pivot(
            {"source": sales},
            {
                "row_columns": ["region"],
                "column_column": "customer_id",
                "value_column": "amount",
                "aggregation": "sum",
            },
        )
        assert "1" in result.columns
        assert "region" in result.columns

    def test_a_column_with_too_many_values_is_refused_before_it_explodes(self):
        frame = pd.DataFrame(
            {"row": ["a"] * 300, "spread": [str(number) for number in range(300)], "value": ["1"] * 300}
        )
        with pytest.raises(InvalidStepParamsError, match="which would make"):
            apply_pivot(
                {"source": frame},
                {
                    "row_columns": ["row"],
                    "column_column": "spread",
                    "value_column": "value",
                    "aggregation": "sum",
                },
            )


class TestRankAndRunningTotal:
    def test_the_biggest_value_is_rank_one_by_default(self, sales):
        result, _ = apply_rank_within_group({"source": sales}, {"order_by": "amount"})
        assert result.loc[result["amount"] == "400", "rank"].iloc[0] == 1

    def test_ranking_can_start_again_for_each_group(self, sales):
        result, _ = apply_rank_within_group(
            {"source": sales}, {"order_by": "amount", "group_by": ["region"]}
        )
        assert sorted(result.loc[result["region"] == "North", "rank"].tolist()) == [1, 2]

    def test_tied_values_share_the_better_rank(self):
        frame = pd.DataFrame({"score": ["10", "10", "5"]})
        result, _ = apply_rank_within_group({"source": frame}, {"order_by": "score"})
        assert result["rank"].tolist() == [1, 1, 3]

    def test_a_running_total_accumulates_down_the_table(self, sales):
        result, _ = apply_running_total({"source": sales}, {"value_column": "amount"})
        assert result["running amount"].tolist() == [100.0, 300.0, 600.0, 1000.0]

    def test_a_running_total_can_restart_for_each_group(self, sales):
        result, _ = apply_running_total(
            {"source": sales}, {"value_column": "amount", "group_by": ["region"]}
        )
        assert result.loc[result["region"] == "North", "running amount"].tolist() == [100.0, 400.0]

    def test_ordering_the_running_total_leaves_the_rows_where_they_were(self, sales):
        result, _ = apply_running_total(
            {"source": sales}, {"value_column": "amount", "order_by": "sold_on"}
        )
        assert result["amount"].tolist() == sales["amount"].tolist()


# --------------------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------------------


class TestColumnOperations:
    def test_dropping_removes_only_what_was_chosen(self, sales):
        result, _ = apply_drop_columns({"source": sales}, {"columns": ["region", "sold_on"]})
        assert list(result.columns) == ["customer_id", "amount"]

    def test_renaming_changes_one_column(self, sales):
        result, _ = apply_rename_column({"source": sales}, {"column": "region", "new_name": "area"})
        assert "area" in result.columns
        assert "region" not in result.columns

    def test_renaming_onto_an_existing_column_is_refused(self, sales):
        with pytest.raises(InvalidStepParamsError, match="already has a column"):
            apply_rename_column({"source": sales}, {"column": "region", "new_name": "amount"})

    def test_splitting_makes_one_column_per_piece(self, sales):
        result, _ = apply_split_column({"source": sales}, {"column": "sold_on", "separator": "/"})
        assert {"sold_on_1", "sold_on_2", "sold_on_3"} <= set(result.columns)

    def test_a_single_piece_can_be_taken(self, sales):
        result, _ = apply_split_column(
            {"source": sales},
            {
                "column": "sold_on",
                "separator": "/",
                "mode": "one piece only",
                "piece_number": 3,
                "new_name": "year_text",
            },
        )
        assert result["year_text"].tolist() == ["2025", "2025", "2026", "2026"]

    def test_a_separator_that_isnt_there_changes_nothing_and_says_so(self, sales):
        result, warnings_out = apply_split_column(
            {"source": sales}, {"column": "region", "separator": "|"}
        )
        assert list(result.columns) == list(sales.columns)
        assert any("wasn't found" in warning for warning in warnings_out)

    def test_the_separator_is_taken_literally_not_as_a_pattern(self):
        frame = pd.DataFrame({"code": ["a.b"]})
        result, _ = apply_split_column({"source": frame}, {"column": "code", "separator": "."})
        assert result["code_1"].tolist() == ["a"]


# --------------------------------------------------------------------------------------
# Phase 30 - finance helpers
# --------------------------------------------------------------------------------------


class TestTodaysDate:
    def test_every_row_gets_the_same_date(self, sales):
        result, warnings_out = apply_add_today_date({"source": sales}, {"new_column": "Loaded_On"})
        expected = pd.Timestamp.today().normalize()
        assert result["Loaded_On"].tolist() == [expected] * len(sales)
        assert warnings_out == []

    def test_the_time_of_day_is_dropped_so_it_is_a_plain_date(self, sales):
        result, _ = apply_add_today_date({"source": sales}, {})
        stamped = result["today"].iloc[0]
        assert (stamped.hour, stamped.minute, stamped.second) == (0, 0, 0)

    def test_a_name_already_in_use_does_not_overwrite_it(self, sales):
        result, _ = apply_add_today_date({"source": sales}, {"new_column": "region"})
        assert result["region"].tolist() == sales["region"].tolist()
        assert "region_2" in result.columns

    def test_an_empty_table_gets_the_column_anyway(self):
        frame = pd.DataFrame({"amount": pd.Series([], dtype="object")})
        result, _ = apply_add_today_date({"source": frame}, {"new_column": "as_at"})
        assert list(result.columns) == ["amount", "as_at"]
        assert len(result) == 0


class TestShiftDate:
    def test_thirty_days_later_is_the_due_date(self):
        frame = pd.DataFrame({"invoice_date": ["01/04/2025"]})
        result, _ = apply_shift_date(
            {"source": frame}, {"column": "invoice_date", "days": 30, "new_column": "due_date"}
        )
        assert result["due_date"].tolist() == [pd.Timestamp("2025-05-01")]

    def test_a_minus_number_moves_the_date_earlier(self):
        frame = pd.DataFrame({"due_date": ["01/04/2025"]})
        result, _ = apply_shift_date(
            {"source": frame}, {"column": "due_date", "days": -7, "new_column": "remind_on"}
        )
        assert result["remind_on"].tolist() == [pd.Timestamp("2025-03-25")]

    def test_the_default_name_says_which_way_it_moved(self):
        frame = pd.DataFrame({"invoice_date": ["01/04/2025"]})
        result, _ = apply_shift_date({"source": frame}, {"column": "invoice_date", "days": -7})
        assert "invoice_date minus 7 days" in result.columns

    def test_a_value_that_isnt_a_date_becomes_blank_and_is_reported(self):
        frame = pd.DataFrame({"invoice_date": ["01/04/2025", "not a date"]})
        result, warnings_out = apply_shift_date(
            {"source": frame}, {"column": "invoice_date", "days": 30, "new_column": "due_date"}
        )
        assert pd.isna(result["due_date"].iloc[1])
        assert any("couldn't be read as a date" in warning for warning in warnings_out)

    def test_a_day_count_that_isnt_a_number_is_refused(self, sales):
        with pytest.raises(InvalidStepParamsError, match="whole number"):
            apply_shift_date({"source": sales}, {"column": "sold_on", "days": "a month"})

    def test_validate_catches_a_bad_day_count_before_the_step_is_added(self):
        with pytest.raises(InvalidStepParamsError, match="whole number"):
            validate_shift_date({"source": ["sold_on"]}, {"column": "sold_on", "days": "soon"})

    def test_validate_insists_on_a_column(self):
        with pytest.raises(InvalidStepParamsError, match="date column"):
            validate_shift_date({"source": ["sold_on"]}, {"days": 30})

    def test_a_day_count_beyond_a_century_is_refused_in_words_a_user_can_act_on(self, sales):
        with pytest.raises(InvalidStepParamsError, match="at most 36,500 days"):
            apply_shift_date({"source": sales}, {"column": "sold_on", "days": 99_999_999})


class TestMonthEdge:
    def test_the_start_of_the_month_is_the_first(self, sales):
        result, _ = apply_month_edge(
            {"source": sales}, {"column": "sold_on", "edge": "start of month", "new_column": "month"}
        )
        assert result["month"].tolist() == [
            pd.Timestamp("2025-04-01"),
            pd.Timestamp("2025-06-01"),
            pd.Timestamp("2026-01-01"),
            pd.Timestamp("2026-03-01"),
        ]

    def test_the_end_of_the_month_knows_how_long_each_month_is(self):
        frame = pd.DataFrame({"sold_on": ["05/02/2024", "05/02/2026", "10/04/2026"]})
        result, _ = apply_month_edge(
            {"source": frame}, {"column": "sold_on", "edge": "end of month", "new_column": "month_end"}
        )
        assert result["month_end"].tolist() == [
            pd.Timestamp("2024-02-29"),
            pd.Timestamp("2026-02-28"),
            pd.Timestamp("2026-04-30"),
        ]

    def test_a_date_already_on_the_last_day_stays_where_it_is(self):
        frame = pd.DataFrame({"sold_on": ["31/03/2026"]})
        result, _ = apply_month_edge(
            {"source": frame}, {"column": "sold_on", "edge": "end of month", "new_column": "month_end"}
        )
        assert result["month_end"].tolist() == [pd.Timestamp("2026-03-31")]

    def test_an_edge_the_step_doesnt_know_is_refused(self, sales):
        with pytest.raises(InvalidStepParamsError, match="isn't a choice"):
            apply_month_edge({"source": sales}, {"column": "sold_on", "edge": "middle of month"})

    def test_validate_catches_an_unknown_edge(self):
        with pytest.raises(InvalidStepParamsError, match="isn't a choice"):
            validate_month_edge({"source": ["sold_on"]}, {"column": "sold_on", "edge": "someday"})


class TestPercentageOfTotal:
    def test_each_row_gets_its_share_and_the_column_adds_to_a_hundred(self, sales):
        result, _ = apply_row_percentage_of_total({"source": sales}, {"column": "amount"})
        shares = result["amount % of total"]
        assert shares.tolist() == [10.0, 20.0, 30.0, 40.0]
        assert round(shares.sum(), 6) == 100.0

    def test_the_answer_is_rounded_to_the_places_asked_for(self):
        frame = pd.DataFrame({"amount": ["1", "2", "0"]})
        result, _ = apply_row_percentage_of_total(
            {"source": frame}, {"column": "amount", "decimals": 1}
        )
        assert result["amount % of total"].tolist() == [33.3, 66.7, 0.0]

    def test_a_column_adding_up_to_zero_gives_blanks_not_an_error(self):
        frame = pd.DataFrame({"amount": ["100", "-100"]})
        result, warnings_out = apply_row_percentage_of_total({"source": frame}, {"column": "amount"})
        assert result["amount % of total"].isna().all()
        assert any("adds up to zero" in warning for warning in warnings_out)

    def test_values_that_arent_numbers_become_blank_and_are_reported(self):
        frame = pd.DataFrame({"amount": ["100", "n/a", "300"]})
        result, warnings_out = apply_row_percentage_of_total({"source": frame}, {"column": "amount"})
        assert pd.isna(result["amount % of total"].iloc[1])
        assert any("couldn't be read as a number" in warning for warning in warnings_out)

    def test_validate_insists_on_a_column(self):
        with pytest.raises(InvalidStepParamsError, match="number column"):
            validate_row_percentage_of_total({"source": ["amount"]}, {})

    def test_validate_catches_decimal_places_that_arent_a_number(self):
        with pytest.raises(InvalidStepParamsError, match="whole number"):
            validate_row_percentage_of_total(
                {"source": ["amount"]}, {"column": "amount", "decimals": "a few"}
            )


class TestAbsoluteValue:
    def test_the_minus_sign_goes_in_place(self):
        frame = pd.DataFrame({"amount": ["-500", "300"]})
        result, _ = apply_absolute_value({"source": frame}, {"column": "amount"})
        assert result["amount"].tolist() == [500.0, 300.0]
        assert list(result.columns) == ["amount"]

    def test_an_accounting_negative_in_brackets_is_understood_too(self):
        frame = pd.DataFrame({"amount": ["(500)"]})
        result, _ = apply_absolute_value({"source": frame}, {"column": "amount"})
        assert result["amount"].tolist() == [500.0]

    def test_the_answer_can_go_in_a_new_column_instead(self):
        frame = pd.DataFrame({"amount": ["-500"]})
        result, _ = apply_absolute_value(
            {"source": frame},
            {"column": "amount", "mode": "add a new column", "new_column": "size"},
        )
        assert result["amount"].tolist() == ["-500"]
        assert result["size"].tolist() == [500.0]

    def test_a_value_that_isnt_a_number_becomes_blank_and_is_reported(self):
        frame = pd.DataFrame({"amount": ["-500", "credit"]})
        result, warnings_out = apply_absolute_value({"source": frame}, {"column": "amount"})
        assert pd.isna(result["amount"].iloc[1])
        assert any("couldn't be read as a number" in warning for warning in warnings_out)

    def test_validate_catches_a_mode_the_step_doesnt_know(self):
        with pytest.raises(InvalidStepParamsError, match="isn't a choice"):
            validate_absolute_value({"source": ["amount"]}, {"column": "amount", "mode": "sideways"})


class TestRowMinMax:
    def test_the_largest_of_two_columns_is_taken_row_by_row(self):
        frame = pd.DataFrame({"budget": ["100", "500"], "actual": ["150", "400"]})
        result, _ = apply_row_min_max(
            {"source": frame},
            {"columns": ["budget", "actual"], "which": "largest", "new_column": "worst_case"},
        )
        assert result["worst_case"].tolist() == [150.0, 500.0]

    def test_the_smallest_can_be_taken_instead(self):
        frame = pd.DataFrame({"budget": ["100", "500"], "actual": ["150", "400"]})
        result, _ = apply_row_min_max(
            {"source": frame},
            {"columns": ["budget", "actual"], "which": "smallest", "new_column": "best_case"},
        )
        assert result["best_case"].tolist() == [100.0, 400.0]

    def test_more_than_two_columns_can_be_compared(self):
        frame = pd.DataFrame({"q1": ["10"], "q2": ["40"], "q3": ["20"]})
        result, _ = apply_row_min_max(
            {"source": frame}, {"columns": ["q1", "q2", "q3"], "which": "largest"}
        )
        assert result["largest of the columns"].tolist() == [40.0]

    def test_a_row_with_a_number_in_only_one_column_still_answers(self):
        frame = pd.DataFrame({"budget": ["100", "x"], "actual": ["y", "400"]})
        result, _ = apply_row_min_max(
            {"source": frame}, {"columns": ["budget", "actual"], "which": "largest"}
        )
        assert result["largest of the columns"].tolist() == [100.0, 400.0]

    def test_a_row_with_no_numbers_at_all_comes_out_blank_and_is_reported(self):
        frame = pd.DataFrame({"budget": ["x"], "actual": ["y"]})
        result, warnings_out = apply_row_min_max(
            {"source": frame}, {"columns": ["budget", "actual"], "which": "largest"}
        )
        assert result["largest of the columns"].isna().all()
        assert any("no number in any of those columns" in warning for warning in warnings_out)

    def test_one_column_on_its_own_is_refused_because_there_is_nothing_to_compare(self):
        frame = pd.DataFrame({"budget": ["100"]})
        with pytest.raises(InvalidStepParamsError, match="at least two columns"):
            apply_row_min_max({"source": frame}, {"columns": ["budget"], "which": "largest"})

    def test_validate_catches_one_column_before_the_step_is_added(self):
        with pytest.raises(InvalidStepParamsError, match="at least two columns"):
            validate_row_min_max({"source": ["budget"]}, {"columns": ["budget"], "which": "largest"})

    def test_validate_catches_a_choice_the_step_doesnt_know(self):
        with pytest.raises(InvalidStepParamsError, match="isn't a choice"):
            validate_row_min_max(
                {"source": ["budget", "actual"]},
                {"columns": ["budget", "actual"], "which": "middling"},
            )


class TestExtractByPosition:
    def test_the_first_three_characters_of_a_code_come_out(self):
        frame = pd.DataFrame({"invoice_no": ["INV000123", "CRN000456"]})
        result, _ = apply_extract_by_position(
            {"source": frame},
            {"column": "invoice_no", "start": 1, "length": 3, "new_column": "doc_type"},
        )
        assert result["doc_type"].tolist() == ["INV", "CRN"]

    def test_a_run_from_the_middle_can_be_taken(self):
        frame = pd.DataFrame({"invoice_no": ["INV2526000123"]})
        result, _ = apply_extract_by_position(
            {"source": frame},
            {"column": "invoice_no", "start": 4, "length": 4, "new_column": "year_code"},
        )
        assert result["year_code"].tolist() == ["2526"]

    def test_the_default_name_says_which_characters_were_taken(self):
        frame = pd.DataFrame({"invoice_no": ["INV000123"]})
        result, _ = apply_extract_by_position(
            {"source": frame}, {"column": "invoice_no", "start": 1, "length": 3}
        )
        assert "invoice_no 1-3" in result.columns

    def test_a_length_running_past_the_end_takes_what_is_there(self):
        frame = pd.DataFrame({"code": ["AB"]})
        result, _ = apply_extract_by_position(
            {"source": frame}, {"column": "code", "start": 1, "length": 5, "new_column": "piece"}
        )
        assert result["piece"].tolist() == ["AB"]

    def test_a_value_shorter_than_the_start_comes_out_blank_and_is_reported(self):
        frame = pd.DataFrame({"code": ["INV000123", "AB"]})
        result, warnings_out = apply_extract_by_position(
            {"source": frame}, {"column": "code", "start": 4, "length": 3, "new_column": "piece"}
        )
        assert result["piece"].tolist()[0] == "000"
        assert pd.isna(result["piece"].iloc[1])
        assert any("shorter than 4 character" in warning for warning in warnings_out)

    def test_counting_from_zero_is_refused_rather_than_quietly_shifted(self):
        frame = pd.DataFrame({"code": ["INV000123"]})
        with pytest.raises(InvalidStepParamsError, match="starts at 1"):
            apply_extract_by_position({"source": frame}, {"column": "code", "start": 0, "length": 3})

    def test_validate_catches_a_position_that_isnt_a_number(self):
        with pytest.raises(InvalidStepParamsError, match="whole number"):
            validate_extract_by_position(
                {"source": ["code"]}, {"column": "code", "start": "first", "length": 3}
            )

    def test_validate_insists_on_a_column(self):
        with pytest.raises(InvalidStepParamsError, match="codes"):
            validate_extract_by_position({"source": ["code"]}, {"start": 1, "length": 3})
