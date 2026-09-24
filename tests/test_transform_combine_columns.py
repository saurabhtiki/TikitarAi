"""Phase 42: Combine columns - Split a column in reverse."""

import pandas as pd
import pytest

from transform import ai_parse
from transform.exceptions import InvalidStepParamsError
from transform.ops_columns import (
    apply_combine_columns,
    describe_combine_columns,
    validate_combine_columns,
)
from transform.registry import CATEGORY_COLUMN, OPERATION_REGISTRY


@pytest.fixture
def people() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Name": ["Ravi", "Asha", "Meena", None],
            "Surname": ["Kumar", None, "Iyer", None],
            "Code": ["A1", "B2", "C3", "D4"],
        },
        dtype="string",
    )


def _combine(frame: pd.DataFrame, **params) -> pd.DataFrame:
    result, _ = apply_combine_columns({"source": frame}, params)
    return result


class TestCombineColumns:
    def test_a_space_joins_name_and_surname(self, people):
        result = _combine(people, columns=["Name", "Surname"], joiner="space", new_column="Full Name")
        assert result["Full Name"].iloc[0] == "Ravi Kumar"

    def test_a_dash_joins_them_with_a_dash(self, people):
        result = _combine(people, columns=["Name", "Surname"], joiner="dash (-)", new_column="Full")
        assert result["Full"].iloc[0] == "Ravi-Kumar"

    def test_the_columns_are_joined_in_the_order_picked(self, people):
        result = _combine(people, columns=["Surname", "Name"], joiner="space", new_column="Full")
        assert result["Full"].iloc[0] == "Kumar Ravi"

    def test_a_blank_piece_leaves_no_dangling_separator(self, people):
        result = _combine(people, columns=["Name", "Surname"], joiner="dash (-)", new_column="Full")
        assert result["Full"].iloc[1] == "Asha"

    def test_a_row_with_every_piece_blank_stays_blank(self, people):
        result = _combine(people, columns=["Name", "Surname"], joiner="space", new_column="Full")
        assert pd.isna(result["Full"].iloc[3])

    def test_nothing_between_runs_them_together(self, people):
        result = _combine(people, columns=["Code", "Name"], joiner="nothing", new_column="Key")
        assert result["Key"].iloc[0] == "A1Ravi"

    def test_your_own_separator_is_used_exactly_spaces_included(self, people):
        result = _combine(
            people, columns=["Name", "Surname"], joiner="my own", custom_joiner=" | ", new_column="Full"
        )
        assert result["Full"].iloc[0] == "Ravi | Kumar"

    def test_the_new_column_is_named_after_the_columns_when_left_empty(self, people):
        result = _combine(people, columns=["Name", "Surname"], joiner="space")
        assert "Name Surname" in result.columns

    def test_a_name_clash_gets_a_suffix_rather_than_overwriting(self, people):
        result = _combine(people, columns=["Name", "Surname"], joiner="space", new_column="Name")
        assert result["Name"].iloc[0] == "Ravi"
        assert result["Name_2"].iloc[0] == "Ravi Kumar"

    def test_whole_numbers_and_dates_print_the_way_people_write_them(self):
        frame = pd.DataFrame(
            {"qty": [5.0, 2.5], "on": pd.to_datetime(["2025-04-03", "2025-12-31 10:30:00"], format="ISO8601")}
        )
        result = _combine(frame, columns=["qty", "on"], joiner="space", new_column="both")
        assert list(result["both"]) == ["5 03-04-2025", "2.5 31-12-2025 10:30:00"]

    def test_the_original_table_is_left_untouched(self, people):
        before = people.copy(deep=True)
        _combine(people, columns=["Name", "Surname"], joiner="space")
        pd.testing.assert_frame_equal(people, before)

    def test_one_column_is_refused(self, people):
        with pytest.raises(InvalidStepParamsError, match="at least two"):
            _combine(people, columns=["Name"], joiner="space")
        with pytest.raises(InvalidStepParamsError, match="at least two"):
            validate_combine_columns({"source": ["Name"]}, {"columns": ["Name"]})

    def test_my_own_with_nothing_typed_is_refused(self):
        with pytest.raises(InvalidStepParamsError, match="Type what should go between"):
            validate_combine_columns({}, {"columns": ["Name", "Surname"], "joiner": "my own"})

    def test_a_missing_column_names_what_the_table_has(self, people):
        with pytest.raises(InvalidStepParamsError, match="Combine columns needs column"):
            _combine(people, columns=["Name", "Middle"], joiner="space")

    def test_the_step_line_reads_in_plain_words(self):
        line = describe_combine_columns(
            {"params": {"columns": ["Name", "Surname"], "joiner": "space", "new_column": "Full Name"}}
        )
        assert line == "Combined Name, Surname with space between as Full Name"


class TestItIsInTheCatalog:
    def test_it_is_registered_under_columns(self):
        assert OPERATION_REGISTRY["combine_columns"].category == CATEGORY_COLUMN

    def test_describe_a_step_can_offer_it(self):
        assert "combine_columns" in ai_parse.describe_catalog_for_prompt()
