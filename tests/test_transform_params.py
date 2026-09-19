"""The pure decision-making behind the generic form renderer."""

import pytest

from transform.exceptions import InvalidStepParamsError
from transform.params import (
    ParamKind,
    ParamSpec,
    collect_params,
    default_value,
    is_visible,
    missing_required,
    option_columns,
    visible_params,
)

COLUMN_PICKER = ParamSpec(
    name="column", label="Column", kind=ParamKind.COLUMN, help="Which column."
)
RIGHT_PICKER = ParamSpec(
    name="other", label="Other", kind=ParamKind.COLUMN, from_role="right", help="Which column."
)
MODE = ParamSpec(
    name="mode",
    label="Mode",
    kind=ParamKind.CHOICE,
    choices=("all", "one"),
    default="all",
    help="Which mode.",
)
PIECE = ParamSpec(
    name="piece",
    label="Piece",
    kind=ParamKind.INTEGER,
    depends_on="mode",
    depends_value="one",
    help="Which piece.",
)


class TestOptionColumns:
    def test_it_reads_the_role_the_parameter_names(self):
        columns_by_role = {"source": ["a", "b"], "right": ["x", "y"]}
        assert option_columns(COLUMN_PICKER, columns_by_role) == ["a", "b"]
        assert option_columns(RIGHT_PICKER, columns_by_role) == ["x", "y"]

    def test_a_role_with_no_table_chosen_yet_offers_nothing(self):
        assert option_columns(COLUMN_PICKER, {}) == []

    def test_a_parameter_that_isnt_column_backed_offers_nothing(self):
        text = ParamSpec(name="t", label="T", kind=ParamKind.TEXT, help="Type something.")
        assert option_columns(text, {"source": ["a"]}) == []


class TestDefaultValue:
    def test_a_declared_default_is_used(self):
        assert default_value(MODE) == "all"

    def test_a_previously_saved_value_beats_the_default(self):
        assert default_value(MODE, {"mode": "one"}) == "one"

    def test_a_list_parameter_starts_as_a_fresh_empty_list(self):
        spec = ParamSpec(name="c", label="C", kind=ParamKind.COLUMNS, help="Which columns.")
        first = default_value(spec)
        first.append("a")
        assert default_value(spec) == []

    def test_a_toggle_starts_off(self):
        spec = ParamSpec(name="b", label="B", kind=ParamKind.BOOLEAN, help="On or off.")
        assert default_value(spec) is False

    def test_a_choice_with_no_declared_default_starts_at_its_first_option(self):
        spec = ParamSpec(
            name="c", label="C", kind=ParamKind.CHOICE, choices=("x", "y"), help="Pick."
        )
        assert default_value(spec) == "x"


class TestVisibility:
    def test_a_parameter_with_no_dependency_is_always_shown(self):
        assert is_visible(COLUMN_PICKER, {})

    def test_a_dependent_parameter_is_hidden_until_its_trigger_matches(self):
        assert not is_visible(PIECE, {"mode": "all"})
        assert is_visible(PIECE, {"mode": "one"})

    def test_visible_params_keeps_the_declared_order(self):
        shown = visible_params([MODE, PIECE, COLUMN_PICKER], {"mode": "one"})
        assert [spec.name for spec in shown] == ["mode", "piece", "column"]


class TestCollectParams:
    def test_a_number_typed_as_text_is_stored_as_a_number(self):
        spec = ParamSpec(name="n", label="N", kind=ParamKind.INTEGER, help="How many.")
        assert collect_params([spec], {"n": "12"}) == {"n": 12}

    def test_a_decimal_is_stored_as_a_float(self):
        spec = ParamSpec(name="n", label="N", kind=ParamKind.NUMBER, help="How much.")
        assert collect_params([spec], {"n": "1.5"}) == {"n": 1.5}

    def test_something_that_isnt_a_number_is_refused_by_label(self):
        spec = ParamSpec(name="n", label="Decimals", kind=ParamKind.INTEGER, help="How many.")
        with pytest.raises(InvalidStepParamsError, match="Decimals"):
            collect_params([spec], {"n": "lots"})

    def test_an_empty_required_box_is_refused_by_label(self):
        with pytest.raises(InvalidStepParamsError, match="'Column' is needed"):
            collect_params([COLUMN_PICKER], {"column": ""})

    def test_an_empty_optional_box_is_simply_left_out(self):
        spec = ParamSpec(
            name="suffix", label="Suffix", kind=ParamKind.TEXT, required=False, help="Optional."
        )
        assert collect_params([spec], {"suffix": None}) == {}

    def test_a_hidden_box_is_not_collected_even_when_it_holds_something_stale(self):
        collected = collect_params([MODE, PIECE], {"mode": "all", "piece": 3})
        assert collected == {"mode": "all"}

    def test_a_hidden_box_is_collected_once_it_becomes_visible(self):
        collected = collect_params([MODE, PIECE], {"mode": "one", "piece": 3})
        assert collected == {"mode": "one", "piece": 3}

    def test_an_empty_required_list_is_refused(self):
        spec = ParamSpec(name="c", label="Columns", kind=ParamKind.COLUMNS, help="Which columns.")
        with pytest.raises(InvalidStepParamsError, match="Columns"):
            collect_params([spec], {"c": []})


class TestMissingRequired:
    def test_it_names_the_empty_boxes_without_raising(self):
        assert missing_required([COLUMN_PICKER], {}) == ["Column"]

    def test_a_filled_form_reports_nothing_missing(self):
        assert missing_required([COLUMN_PICKER], {"column": "a"}) == []

    def test_a_hidden_box_is_never_reported_as_missing(self):
        assert missing_required([MODE, PIECE], {"mode": "all"}) == []
