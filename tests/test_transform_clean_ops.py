"""The nine clean-up operations added in phase 26.

Each one delegates to a `cleaner.steps` executor that already has its own tests, so these
do not re-test the cleaning itself. What they test is the part that is new and therefore
unproven: that the parameters this page's forms produce are translated into the ones the
cleaner expects, that the simplification each one makes (a set of columns and one setting,
rather than a per-column mapping) lands correctly, and that the wrapper keeps this package's
contract — a new frame out, the input untouched.

The `skip_rows` tests are the ones worth reading: it is the only operation here that changes
the *column names*, which is what makes it the step that has to run first.
"""

import pandas as pd
import pytest

from transform.exceptions import InvalidStepParamsError
from transform.ops_clean import (
    CASE_CHOICES,
    ROUNDING_CHOICES,
    apply_change_case,
    apply_find_replace,
    apply_fix_numeric_text,
    apply_remove_empty_rows,
    apply_remove_special_characters,
    apply_round_numbers,
    apply_skip_rows,
    apply_trim_whitespace,
    apply_unpivot,
    describe_change_case,
    describe_find_replace,
    describe_remove_empty_rows,
    describe_skip_rows,
    describe_unpivot,
    validate_change_case,
    validate_find_replace,
    validate_fix_numeric_text,
    validate_remove_special_characters,
    validate_round_numbers,
    validate_skip_rows,
    validate_unpivot,
)


@pytest.fixture
def messy() -> pd.DataFrame:
    """A table with something for each operation to find."""
    return pd.DataFrame(
        {
            "customer": ["  Acme   Ltd ", "bolt & co.", "Cog #7", "  "],
            "amount": ["1,250.00", "(500)", "300", ""],
            "region": ["North", "north", "SOUTH", ""],
        }
    )


@pytest.fixture
def unnamed_headers() -> pd.DataFrame:
    """What a file with a title and a blank line above its real headers looks like."""
    return pd.DataFrame(
        {
            "Sales report": ["Generated 01/04/2025", None, "invoice_no", "INV-1", "INV-2"],
            "Unnamed: 1": [None, None, "customer", "Acme", "Bolt"],
            "Unnamed: 2": [None, None, "amount", "100", "200"],
        }
    )


@pytest.fixture
def wide() -> pd.DataFrame:
    """Months spread across the top, which is what unpivot exists to undo."""
    return pd.DataFrame(
        {
            "customer": ["Acme", "Bolt"],
            "Jan": ["100", "150"],
            "Feb": ["200", "250"],
            "Mar": ["300", "350"],
        }
    )


# --------------------------------------------------------------------------------------
# The contract every executor keeps
# --------------------------------------------------------------------------------------

EXECUTOR_CASES = [
    ("skip_rows", apply_skip_rows, {"top": 1, "bottom": 0, "promote_header": False}),
    ("remove_empty_rows", apply_remove_empty_rows, {"columns": [], "blank_strings_count_as_empty": True}),
    (
        "fix_numeric_text",
        apply_fix_numeric_text,
        {"columns": ["amount"], "decimal_separator": ".", "parentheses_are_negative": True},
    ),
    (
        "round_numbers",
        apply_round_numbers,
        {"columns": ["amount"], "decimals": 0, "direction": "the nearest value"},
    ),
    ("trim_whitespace", apply_trim_whitespace, {"collapse_internal": True}),
    (
        "remove_special_characters",
        apply_remove_special_characters,
        {"keep_pattern": "A-Za-z0-9 ", "replacement": ""},
    ),
    ("change_case", apply_change_case, {"columns": ["region"], "case": "UPPERCASE"}),
    (
        "find_replace",
        apply_find_replace,
        {"columns": ["region"], "find": "North", "replace": "N", "regex": False, "case_sensitive": True},
    ),
    (
        "unpivot",
        apply_unpivot,
        {"id_columns": ["customer"], "value_columns": ["amount"], "variable_name": "Attribute", "value_name": "Value"},
    ),
]


@pytest.mark.parametrize(
    ("name", "executor", "params"), EXECUTOR_CASES, ids=[case[0] for case in EXECUTOR_CASES]
)
def test_an_executor_never_changes_the_table_it_was_given(messy, name, executor, params):
    before = messy.copy(deep=True)
    result, warnings_out = executor({"source": messy}, params)

    pd.testing.assert_frame_equal(messy, before)
    assert isinstance(result, pd.DataFrame)
    assert isinstance(warnings_out, list)
    assert result is not messy


@pytest.mark.parametrize(
    ("name", "executor", "params"), EXECUTOR_CASES, ids=[case[0] for case in EXECUTOR_CASES]
)
def test_an_executor_returns_a_frame_even_when_it_does_nothing(name, executor, params):
    """An empty table must come back as an empty table, not as an exception."""
    empty = pd.DataFrame({"customer": [], "amount": [], "region": []}, dtype="string")
    result, _ = executor({"source": empty}, params)

    assert isinstance(result, pd.DataFrame)
    assert result is not empty


# --------------------------------------------------------------------------------------
# skip_rows
# --------------------------------------------------------------------------------------


class TestSkipRows:
    def test_rows_come_off_the_top(self, messy):
        result, _ = apply_skip_rows({"source": messy}, {"top": 2, "promote_header": False})

        assert len(result) == 2
        assert result["region"].tolist() == ["SOUTH", ""]

    def test_rows_come_off_the_bottom(self, messy):
        result, _ = apply_skip_rows({"source": messy}, {"bottom": 1, "promote_header": False})

        assert len(result) == 3
        assert result["region"].tolist() == ["North", "north", "SOUTH"]

    def test_the_index_is_renumbered_from_zero(self, messy):
        result, _ = apply_skip_rows({"source": messy}, {"top": 2, "promote_header": False})

        assert result.index.tolist() == [0, 1]

    def test_promoting_a_header_renames_the_columns(self, unnamed_headers):
        """The whole point of the step: `Unnamed: 1` becomes `customer`."""
        result, _ = apply_skip_rows(
            {"source": unnamed_headers}, {"top": 2, "promote_header": True}
        )

        assert list(result.columns) == ["invoice_no", "customer", "amount"]
        assert result["customer"].tolist() == ["Acme", "Bolt"]

    def test_the_promoted_row_is_not_left_in_the_data(self, unnamed_headers):
        result, _ = apply_skip_rows(
            {"source": unnamed_headers}, {"top": 2, "promote_header": True}
        )

        assert len(result) == 2
        assert "invoice_no" not in result["invoice_no"].tolist()

    def test_a_blank_header_cell_gets_a_placeholder_name(self):
        frame = pd.DataFrame({"a": ["first", "1"], "b": [None, "2"]})
        result, _ = apply_skip_rows({"source": frame}, {"top": 0, "promote_header": True})

        assert list(result.columns) == ["first", "column_2"]

    def test_skipping_everything_warns_rather_than_failing(self, messy):
        result, warnings_out = apply_skip_rows(
            {"source": messy}, {"top": 10, "promote_header": False}
        )

        assert result.empty
        assert any("removes every one" in warning for warning in warnings_out)

    def test_a_step_that_would_do_nothing_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="would do nothing"):
            validate_skip_rows({}, {"top": 0, "bottom": 0, "promote_header": False})

    def test_a_negative_count_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="can't be negative"):
            validate_skip_rows({}, {"top": -1, "bottom": 0, "promote_header": True})

    def test_promoting_a_header_alone_is_allowed(self):
        assert validate_skip_rows({}, {"top": 0, "bottom": 0, "promote_header": True}) is None

    def test_the_description_says_what_happened(self):
        line = describe_skip_rows({"params": {"top": 3, "bottom": 1, "promote_header": True}})

        assert "3 row(s) from the top" in line
        assert "1 row(s) from the bottom" in line
        assert "column headers" in line


# --------------------------------------------------------------------------------------
# remove_empty_rows
# --------------------------------------------------------------------------------------


class TestRemoveEmptyRows:
    def test_a_row_of_spaces_counts_as_empty(self, messy):
        result, _ = apply_remove_empty_rows(
            {"source": messy}, {"columns": [], "blank_strings_count_as_empty": True}
        )

        assert len(result) == 3

    def test_only_the_chosen_columns_are_looked_at(self, messy):
        result, _ = apply_remove_empty_rows(
            {"source": messy}, {"columns": ["amount"], "blank_strings_count_as_empty": True}
        )

        assert result["amount"].tolist() == ["1,250.00", "(500)", "300"]

    def test_the_count_of_removed_rows_is_reported(self, messy):
        _, warnings_out = apply_remove_empty_rows(
            {"source": messy}, {"columns": [], "blank_strings_count_as_empty": True}
        )

        assert any("1 empty row(s)" in warning for warning in warnings_out)

    def test_nothing_to_remove_says_nothing(self, wide):
        _, warnings_out = apply_remove_empty_rows(
            {"source": wide}, {"columns": [], "blank_strings_count_as_empty": True}
        )

        assert warnings_out == []

    def test_the_description_names_the_columns(self):
        line = describe_remove_empty_rows({"params": {"columns": ["amount", "region"]}})

        assert "amount, region" in line


# --------------------------------------------------------------------------------------
# fix_numeric_text
# --------------------------------------------------------------------------------------


class TestFixNumericText:
    def test_thousands_separators_and_brackets_are_read(self, messy):
        result, _ = apply_fix_numeric_text(
            {"source": messy},
            {"columns": ["amount"], "decimal_separator": ".", "parentheses_are_negative": True},
        )

        assert result["amount"].tolist()[:3] == [1250.0, -500.0, 300.0]

    def test_the_column_is_stored_as_a_number_afterwards(self, messy):
        result, _ = apply_fix_numeric_text(
            {"source": messy},
            {"columns": ["amount"], "decimal_separator": ".", "parentheses_are_negative": True},
        )

        assert pd.api.types.is_numeric_dtype(result["amount"])

    def test_text_that_is_not_a_number_warns_with_examples(self, messy):
        _, warnings_out = apply_fix_numeric_text(
            {"source": messy},
            {"columns": ["region"], "decimal_separator": ".", "parentheses_are_negative": True},
        )

        assert any("North" in warning for warning in warnings_out)

    def test_no_columns_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="at least one column"):
            validate_fix_numeric_text({}, {"columns": [], "decimal_separator": "."})

    def test_an_odd_decimal_separator_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="must be"):
            validate_fix_numeric_text({}, {"columns": ["amount"], "decimal_separator": "-"})


# --------------------------------------------------------------------------------------
# round_numbers
# --------------------------------------------------------------------------------------


class TestRoundNumbers:
    @pytest.fixture
    def numbers(self) -> pd.DataFrame:
        return pd.DataFrame({"amount": [1.234, 5.678, -2.345]})

    def test_rounding_to_the_nearest_value(self, numbers):
        result, _ = apply_round_numbers(
            {"source": numbers},
            {"columns": ["amount"], "decimals": 2, "direction": "the nearest value"},
        )

        assert result["amount"].tolist() == [1.23, 5.68, -2.35]

    def test_rounding_up(self, numbers):
        result, _ = apply_round_numbers(
            {"source": numbers}, {"columns": ["amount"], "decimals": 1, "direction": "up"}
        )

        assert result["amount"].tolist()[0] == 1.3

    def test_rounding_down(self, numbers):
        result, _ = apply_round_numbers(
            {"source": numbers}, {"columns": ["amount"], "decimals": 1, "direction": "down"}
        )

        assert result["amount"].tolist()[1] == 5.6

    def test_a_column_still_stored_as_text_is_left_alone_and_says_so(self, messy):
        """The likely mistake on this page, because everything arrives as text."""
        result, warnings_out = apply_round_numbers(
            {"source": messy},
            {"columns": ["amount"], "decimals": 0, "direction": "the nearest value"},
        )

        assert result["amount"].tolist() == messy["amount"].tolist()
        assert any("isn't a numeric column" in warning for warning in warnings_out)

    def test_too_many_decimals_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="between 0 and"):
            validate_round_numbers(
                {}, {"columns": ["amount"], "decimals": 99, "direction": "up"}
            )

    def test_an_unknown_direction_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="round up, down"):
            validate_round_numbers(
                {}, {"columns": ["amount"], "decimals": 0, "direction": "sideways"}
            )

    def test_every_offered_direction_is_accepted(self):
        for direction in ROUNDING_CHOICES:
            assert (
                validate_round_numbers(
                    {}, {"columns": ["amount"], "decimals": 2, "direction": direction}
                )
                is None
            )


# --------------------------------------------------------------------------------------
# trim_whitespace
# --------------------------------------------------------------------------------------


class TestTrimWhitespace:
    def test_spaces_around_a_value_go(self, messy):
        result, _ = apply_trim_whitespace({"source": messy}, {"collapse_internal": False})

        assert result["customer"].tolist()[0] == "Acme   Ltd"

    def test_repeated_spaces_inside_can_be_squeezed(self, messy):
        result, _ = apply_trim_whitespace({"source": messy}, {"collapse_internal": True})

        assert result["customer"].tolist()[0] == "Acme Ltd"

    def test_it_applies_to_every_text_column(self, messy):
        result, _ = apply_trim_whitespace({"source": messy}, {"collapse_internal": True})

        assert result["region"].tolist() == ["North", "north", "SOUTH", ""]


# --------------------------------------------------------------------------------------
# remove_special_characters
# --------------------------------------------------------------------------------------


class TestRemoveSpecialCharacters:
    def test_unlisted_characters_are_deleted(self, messy):
        result, _ = apply_remove_special_characters(
            {"source": messy}, {"keep_pattern": "A-Za-z0-9 ", "replacement": ""}
        )

        assert result["customer"].tolist()[2] == "Cog 7"

    def test_they_can_be_replaced_instead(self, messy):
        result, _ = apply_remove_special_characters(
            {"source": messy}, {"keep_pattern": "A-Za-z0-9 ", "replacement": "-"}
        )

        assert result["customer"].tolist()[1] == "bolt - co-"

    def test_a_broken_pattern_is_refused_with_a_readable_message(self):
        with pytest.raises(InvalidStepParamsError, match="usable set of characters"):
            validate_remove_special_characters({}, {"keep_pattern": "z-a"})

    def test_the_default_pattern_is_accepted(self):
        assert validate_remove_special_characters({}, {"keep_pattern": ""}) is None


# --------------------------------------------------------------------------------------
# change_case
# --------------------------------------------------------------------------------------


class TestChangeCase:
    @pytest.mark.parametrize(
        ("choice", "expected"),
        [("UPPERCASE", "NORTH"), ("lowercase", "north"), ("Title Case", "North")],
    )
    def test_each_case_choice(self, messy, choice, expected):
        result, _ = apply_change_case({"source": messy}, {"columns": ["region"], "case": choice})

        assert result["region"].tolist()[0] == expected

    def test_every_chosen_column_gets_the_same_case(self, messy):
        """The simplification this page makes over Data Cleaner's per-column mapping."""
        result, _ = apply_change_case(
            {"source": messy}, {"columns": ["region", "customer"], "case": "UPPERCASE"}
        )

        assert result["region"].tolist()[1] == "NORTH"
        assert "ACME" in result["customer"].tolist()[0]

    def test_a_column_that_was_not_chosen_is_untouched(self, messy):
        result, _ = apply_change_case({"source": messy}, {"columns": ["region"], "case": "UPPERCASE"})

        assert result["customer"].tolist() == messy["customer"].tolist()

    def test_every_offered_choice_is_accepted(self):
        for choice in CASE_CHOICES:
            assert validate_change_case({}, {"columns": ["region"], "case": choice}) is None

    def test_no_columns_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="at least one column"):
            validate_change_case({}, {"columns": [], "case": "UPPERCASE"})

    def test_an_unknown_case_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="UPPERCASE"):
            validate_change_case({}, {"columns": ["region"], "case": "sPoNgEbOb"})

    def test_the_description_names_the_case(self):
        line = describe_change_case({"params": {"columns": ["region"], "case": "Title Case"}})

        assert "region" in line
        assert "Title Case" in line


# --------------------------------------------------------------------------------------
# find_replace
# --------------------------------------------------------------------------------------


class TestFindReplace:
    def test_plain_text_is_swapped(self, messy):
        result, _ = apply_find_replace(
            {"source": messy},
            {"columns": ["region"], "find": "North", "replace": "N", "regex": False, "case_sensitive": True},
        )

        assert result["region"].tolist()[0] == "N"

    def test_case_can_be_ignored(self, messy):
        result, _ = apply_find_replace(
            {"source": messy},
            {"columns": ["region"], "find": "north", "replace": "N", "regex": False, "case_sensitive": False},
        )

        assert result["region"].tolist()[:2] == ["N", "N"]

    def test_an_empty_replacement_deletes_the_text(self, messy):
        result, _ = apply_find_replace(
            {"source": messy},
            {"columns": ["region"], "find": "North", "replace": "", "regex": False, "case_sensitive": True},
        )

        assert result["region"].tolist()[0] == ""

    def test_only_the_chosen_columns_change(self, messy):
        result, _ = apply_find_replace(
            {"source": messy},
            {"columns": ["region"], "find": "o", "replace": "0", "regex": False, "case_sensitive": True},
        )

        assert result["customer"].tolist() == messy["customer"].tolist()

    def test_a_literal_search_is_not_read_as_a_pattern(self, messy):
        """'.' must match a full stop, not any character."""
        result, _ = apply_find_replace(
            {"source": messy},
            {"columns": ["customer"], "find": ".", "replace": "!", "regex": False, "case_sensitive": True},
        )

        assert result["customer"].tolist()[1] == "bolt & co!"

    def test_a_broken_pattern_is_refused_at_the_dialog(self):
        with pytest.raises(InvalidStepParamsError, match="isn't a valid pattern"):
            validate_find_replace({}, {"columns": ["region"], "find": "(unclosed", "regex": True})

    def test_a_broken_pattern_is_fine_as_plain_text(self):
        assert (
            validate_find_replace({}, {"columns": ["region"], "find": "(unclosed", "regex": False})
            is None
        )

    def test_nothing_to_find_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="text to look for"):
            validate_find_replace({}, {"columns": ["region"], "find": "", "regex": False})

    def test_the_description_says_what_was_swapped(self):
        line = describe_find_replace(
            {"params": {"columns": ["region"], "find": "North", "replace": "N"}}
        )

        assert "'North'" in line
        assert "'N'" in line


# --------------------------------------------------------------------------------------
# unpivot
# --------------------------------------------------------------------------------------


class TestUnpivot:
    def test_months_across_the_top_become_rows(self, wide):
        result, _ = apply_unpivot(
            {"source": wide},
            {
                "id_columns": ["customer"],
                "value_columns": ["Jan", "Feb", "Mar"],
                "variable_name": "Month",
                "value_name": "Amount",
            },
        )

        assert list(result.columns) == ["customer", "Month", "Amount"]
        assert len(result) == 6

    def test_the_kept_column_is_repeated_down_the_rows(self, wide):
        result, _ = apply_unpivot(
            {"source": wide},
            {
                "id_columns": ["customer"],
                "value_columns": ["Jan", "Feb", "Mar"],
                "variable_name": "Month",
                "value_name": "Amount",
            },
        )

        assert sorted(result["customer"].tolist()) == ["Acme"] * 3 + ["Bolt"] * 3

    def test_leaving_the_value_columns_empty_stacks_everything_else(self, wide):
        result, _ = apply_unpivot(
            {"source": wide},
            {
                "id_columns": ["customer"],
                "value_columns": [],
                "variable_name": "Month",
                "value_name": "Amount",
            },
        )

        assert sorted(result["Month"].unique().tolist()) == ["Feb", "Jan", "Mar"]

    def test_the_new_column_names_default_when_left_blank(self, wide):
        result, _ = apply_unpivot(
            {"source": wide},
            {"id_columns": ["customer"], "value_columns": [], "variable_name": "", "value_name": ""},
        )

        assert list(result.columns) == ["customer", "Attribute", "Value"]

    def test_no_kept_columns_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="at least one column to keep"):
            validate_unpivot({}, {"id_columns": [], "value_columns": ["Jan"]})

    def test_a_column_cannot_be_both_kept_and_stacked(self):
        with pytest.raises(InvalidStepParamsError, match="can't be both"):
            validate_unpivot({}, {"id_columns": ["Jan"], "value_columns": ["Jan", "Feb"]})

    def test_the_two_new_columns_need_different_names(self):
        with pytest.raises(InvalidStepParamsError, match="different names"):
            validate_unpivot(
                {},
                {
                    "id_columns": ["customer"],
                    "value_columns": [],
                    "variable_name": "Month",
                    "value_name": "Month",
                },
            )

    def test_a_new_name_cannot_clash_with_a_kept_column(self):
        with pytest.raises(InvalidStepParamsError, match="already a column you're keeping"):
            validate_unpivot(
                {},
                {
                    "id_columns": ["customer"],
                    "value_columns": [],
                    "variable_name": "customer",
                    "value_name": "Amount",
                },
            )

    def test_the_description_counts_the_stacked_columns(self):
        line = describe_unpivot(
            {
                "params": {
                    "id_columns": ["customer"],
                    "value_columns": ["Jan", "Feb", "Mar"],
                    "variable_name": "Month",
                }
            }
        )

        assert "3 column(s)" in line
        assert "Month" in line
