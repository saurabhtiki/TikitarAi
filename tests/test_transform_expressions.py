"""The calculated-column formula reader.

The security half of this file matters more than the arithmetic half: the expression box is
the one place a user's typing reaches something that evaluates, and the whole design rests
on the tokenizer refusing everything that isn't a number, a column, an operator or a bracket.
"""

import pandas as pd
import pytest

from transform.exceptions import InvalidStepParamsError
from transform.expressions import MAX_EXPRESSION_LENGTH, evaluate, referenced_columns, tokenize


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "basic": ["1,000", "2500", "(500)"],
            "da": ["10", "20", "30"],
            "net sales": ["5", "5", "5"],
            "mixed": ["x", "1", "2"],
        }
    )


class TestArithmetic:
    def test_a_column_times_a_number(self, frame):
        values, _ = evaluate(frame, "basic * 0.12")
        assert values.tolist() == [120.0, 300.0, -60.0]

    def test_multiplication_binds_tighter_than_addition(self, frame):
        values, _ = evaluate(frame, "1 + 2 * 3")
        assert values.tolist() == [7.0, 7.0, 7.0]

    def test_brackets_override_precedence(self, frame):
        values, _ = evaluate(frame, "(1 + 2) * 3")
        assert values.tolist() == [9.0, 9.0, 9.0]

    def test_subtraction_is_left_associative(self, frame):
        values, _ = evaluate(frame, "10 - 3 - 2")
        assert values.tolist() == [5.0, 5.0, 5.0]

    def test_a_column_name_with_spaces_is_written_in_square_brackets(self, frame):
        values, _ = evaluate(frame, "[net sales] * 2")
        assert values.tolist() == [10.0, 10.0, 10.0]

    def test_a_leading_minus_negates_rather_than_subtracts(self, frame):
        values, _ = evaluate(frame, "-da")
        assert values.tolist() == [-10.0, -20.0, -30.0]

    def test_a_minus_after_an_operator_negates(self, frame):
        values, _ = evaluate(frame, "da * -1")
        assert values.tolist() == [-10.0, -20.0, -30.0]

    def test_several_columns_combine(self, frame):
        values, _ = evaluate(frame, "(basic + da) * 0.1")
        assert values.tolist() == pytest.approx([101.0, 252.0, -47.0])

    def test_a_formula_of_only_numbers_fills_every_row(self, frame):
        values, _ = evaluate(frame, "100 / 4")
        assert values.tolist() == [25.0, 25.0, 25.0]


class TestRealWorldValues:
    def test_thousands_separators_are_understood(self, frame):
        values, _ = evaluate(frame, "basic")
        assert values.tolist()[0] == 1000.0

    def test_brackets_around_a_number_mean_it_is_negative(self, frame):
        values, _ = evaluate(frame, "basic")
        assert values.tolist()[2] == -500.0

    def test_dividing_by_zero_gives_a_blank_rather_than_an_error(self, frame):
        values, _ = evaluate(frame, "da / 0")
        assert values.isna().all()

    def test_text_that_isnt_a_number_is_blanked_and_reported(self, frame):
        values, warnings_out = evaluate(frame, "mixed * 1")
        assert values.isna().sum() == 1
        assert "couldn't be read as a number" in warnings_out[0]


class TestRejectsEverythingOffTheWhitelist:
    @pytest.mark.parametrize(
        "attack",
        [
            "__import__('os')",
            "basic.__class__",
            "import os",
            "open('secret.txt')",
            "exec('x=1')",
            "eval('2+2')",
            "lambda: 1",
            "basic; da",
            "[x for x in range(3)]",
            "basic ** 2",
            "5 & 3",
            "basic | da",
            "globals()",
        ],
    )
    def test_it_is_refused(self, frame, attack):
        with pytest.raises(InvalidStepParamsError):
            evaluate(frame, attack)

    def test_an_unknown_name_is_named_in_the_message(self, frame):
        with pytest.raises(InvalidStepParamsError, match="salary"):
            evaluate(frame, "salary * 2")

    def test_an_empty_formula_asks_for_one(self, frame):
        with pytest.raises(InvalidStepParamsError, match="Type a formula"):
            evaluate(frame, "   ")

    def test_an_enormous_formula_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="too long"):
            evaluate(frame, "da + " * MAX_EXPRESSION_LENGTH)


class TestIncompleteFormulas:
    def test_a_trailing_operator_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="needs a value on both sides"):
            evaluate(frame, "basic *")

    def test_an_unclosed_bracket_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match=r"'\(' with no matching"):
            evaluate(frame, "(1 + 2")

    def test_an_unopened_bracket_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match=r"'\)' with no matching"):
            evaluate(frame, "1 + 2)")

    def test_an_unclosed_column_bracket_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="never closed"):
            evaluate(frame, "[net sales * 2")

    def test_two_values_with_no_operator_between_them_are_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="isn't complete"):
            evaluate(frame, "basic da")


class TestReferencedColumns:
    def test_it_lists_the_columns_a_formula_reads_in_order(self, frame):
        assert referenced_columns("da + basic", list(frame.columns)) == ["da", "basic"]

    def test_a_column_named_twice_is_listed_once(self, frame):
        assert referenced_columns("da + da", list(frame.columns)) == ["da"]

    def test_it_does_not_need_the_data_only_the_column_names(self):
        assert referenced_columns("price * qty", ["price", "qty"]) == ["price", "qty"]


class TestTokenizerPrefersTheLongerColumnName:
    def test_a_column_whose_name_contains_another_is_matched_whole(self):
        frame = pd.DataFrame({"net": [1], "net sales": [10]})
        values, _ = evaluate(frame, "net sales * 2")
        assert values.tolist() == [20.0]

    def test_the_shorter_name_still_works_on_its_own(self):
        frame = pd.DataFrame({"net": [1], "net sales": [10]})
        values, _ = evaluate(frame, "net * 3")
        assert values.tolist() == [3.0]

    def test_tokenize_returns_column_tokens_for_known_names(self):
        tokens = tokenize("price * 2", ["price"])
        assert [token.kind for token in tokens] == ["column", "operator", "number"]


class TestComparisons:
    """A test on its own is a formula: 1 where it passes, 0 where it doesn't."""

    @pytest.mark.parametrize(
        ("formula", "expected"),
        [
            ("da > 15", [0.0, 1.0, 1.0]),
            ("da < 15", [1.0, 0.0, 0.0]),
            ("da >= 20", [0.0, 1.0, 1.0]),
            ("da <= 20", [1.0, 1.0, 0.0]),
            ("da == 20", [0.0, 1.0, 0.0]),
            ("da = 20", [0.0, 1.0, 0.0]),
            ("da != 20", [1.0, 0.0, 1.0]),
            ("da <> 20", [1.0, 0.0, 1.0]),
        ],
    )
    def test_each_comparison_gives_one_or_zero(self, frame, formula, expected):
        values, _ = evaluate(frame, formula)
        assert values.tolist() == expected

    def test_arithmetic_is_worked_out_before_the_comparison(self, frame):
        values, _ = evaluate(frame, "da * 2 > 30")
        assert values.tolist() == [0.0, 1.0, 1.0]

    def test_a_two_character_comparison_is_not_read_as_two_operators(self, frame):
        tokens = tokenize("da >= 20", list(frame.columns))
        assert [token.value for token in tokens] == ["da", ">=", 20.0]

    def test_a_column_that_is_not_a_number_never_passes_the_test(self, frame):
        values, _ = evaluate(frame, "mixed > 0")
        assert values.tolist() == [0.0, 1.0, 1.0]

    def test_two_comparisons_can_be_added_as_flags(self, frame):
        values, _ = evaluate(frame, "(da > 15) + (basic > 0)")
        assert values.tolist() == [1.0, 2.0, 1.0]

    def test_comparing_three_values_at_once_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="only two values at a time"):
            evaluate(frame, "10 < da < 25")


class TestIfElse:
    """A formula that picks between two numbers - the point of this phase."""

    def test_it_picks_one_of_two_numbers(self, frame):
        values, _ = evaluate(frame, "1 if da > 15 else 0")
        assert values.tolist() == [0.0, 1.0, 1.0]

    def test_each_branch_can_be_arithmetic(self, frame):
        values, _ = evaluate(frame, "basic * 0.9 if da > 15 else basic")
        assert values.tolist() == pytest.approx([1000.0, 2250.0, -450.0])

    def test_a_bracketed_branch_reads_the_same_way(self, frame):
        values, _ = evaluate(frame, "(basic * 0.9) if da > 15 else basic")
        assert values.tolist() == pytest.approx([1000.0, 2250.0, -450.0])

    def test_a_bracketed_choice_can_be_used_inside_a_bigger_formula(self, frame):
        values, _ = evaluate(frame, "(0 if da > 15 else 1) * basic")
        assert values.tolist() == [1000.0, 0.0, -0.0]

    def test_a_column_name_with_spaces_works_in_every_part(self, frame):
        values, _ = evaluate(frame, "[net sales] * 2 if [net sales] > 1 else [net sales]")
        assert values.tolist() == [10.0, 10.0, 10.0]

    def test_a_negative_answer_is_read_as_one_value(self, frame):
        values, _ = evaluate(frame, "0 if da > 15 else -1")
        assert values.tolist() == [-1.0, 0.0, 0.0]

    def test_choices_can_be_chained_and_are_read_right_to_left(self, frame):
        values, _ = evaluate(frame, "1 if da > 25 else 2 if da > 15 else 3")
        assert values.tolist() == [3.0, 2.0, 1.0]

    def test_a_test_that_cannot_be_read_as_a_number_takes_the_else_answer(self, frame):
        # 'mixed' is ["x", "1", "2"]; the first row is unreadable, so it falls to else.
        values, warnings_out = evaluate(frame, "1 if mixed > 0 else 0")
        assert values.tolist() == [0.0, 1.0, 1.0]
        assert any("couldn't be read as a number" in warning for warning in warnings_out)

    def test_a_choice_with_no_columns_still_fills_every_row(self, frame):
        values, _ = evaluate(frame, "7 if 2 > 1 else 9")
        assert values.tolist() == [7.0, 7.0, 7.0]

    def test_an_if_with_no_else_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="'if' but no 'else'"):
            evaluate(frame, "1 if da > 15")

    def test_an_else_with_no_if_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="'else' with no 'if'"):
            evaluate(frame, "1 else 0")

    def test_an_empty_part_is_refused(self, frame):
        with pytest.raises(InvalidStepParamsError, match="Part of the formula is empty"):
            evaluate(frame, "da if else 1")

    def test_a_column_whose_name_starts_with_if_is_still_a_column(self):
        frame = pd.DataFrame({"ifscode": [2, 4]})
        values, _ = evaluate(frame, "ifscode * 2")
        assert values.tolist() == [4.0, 8.0]

    def test_every_branch_counts_towards_the_columns_the_formula_reads(self, frame):
        assert referenced_columns(
            "basic if da > 1 else [net sales]", list(frame.columns)
        ) == ["basic", "da", "net sales"]
