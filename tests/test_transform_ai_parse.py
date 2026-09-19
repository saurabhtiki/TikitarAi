"""Turning a plain-English sentence into catalog steps (requirements section 5, path 2).

Fully monkeypatched - no model is ever called. What matters here is not that the parse is
clever but that it is *safe*: the catalog and the real schema are the only vocabulary, and
anything outside them is dropped with a sentence rather than admitted into the pipeline.
"""

import pandas as pd

from llm.client import LLMConnectionError
from transform import ai_parse
from transform.ai_parse import ParsedStep, ParsedSteps, ParsedValue
from transform.workspace import NamedFrame

PROFILE = {
    "profile_id": 1,
    "nickname": "Light",
    "default_model": "small-model",
    "provider_type": "local",
}


def _workspace() -> dict[str, NamedFrame]:
    sales = pd.DataFrame({"customer_id": [1, 1, 2], "amount": [100, 200, 300], "salary": [10, 20, 30]})
    customers = pd.DataFrame({"customer_id": [1, 2], "customer": ["Acme", "Bolt"]})
    return {
        "sales": NamedFrame(name="sales", frame=sales, origin="upload", source_label="sales.csv"),
        "customers": NamedFrame(
            name="customers", frame=customers, origin="upload", source_label="customers.csv"
        ),
    }


def _answer(monkeypatch, response: ParsedSteps, recorder: list | None = None):
    """Makes the Light Model return `response` without going near a network."""

    def fake_run(profile, prompt, schema, **kwargs):
        if recorder is not None:
            recorder.append(prompt)
        return response

    monkeypatch.setattr(ai_parse, "run_structured", fake_run)


def _proposed(
    operation: str,
    inputs: dict,
    params: dict,
    output_mode: str = "in_place",
    output_name: str = "",
) -> ParsedStep:
    """Builds a proposal the way the model has to send it: name/value pairs, all text.

    The dicts here are only for readability in the tests - a strict structured-output
    schema cannot describe a free-shaped object, which is why the real thing is a list.
    """
    return ParsedStep(
        operation=operation,
        inputs=[ParsedValue(name=name, value=value) for name, value in inputs.items()],
        params=[ParsedValue(name=name, value=value) for name, value in params.items()],
        output_mode=output_mode,
        output_name=output_name,
    )


def _calculated_column(column: str = "bonus", formula: str = "salary * 0.12") -> ParsedStep:
    return _proposed(
        "add_calculated_column",
        {"source": "sales"},
        {"new_column": column, "expression": formula},
    )


class TestTheCatalogGoesIntoThePrompt:
    def test_every_operation_is_offered(self):
        from transform.registry import OPERATION_REGISTRY

        catalog = ai_parse.describe_catalog_for_prompt()
        for operation in OPERATION_REGISTRY:
            assert operation in catalog

    def test_an_operations_parameters_are_named(self):
        catalog = ai_parse.describe_catalog_for_prompt()
        assert "left_on" in catalog and "right_on" in catalog

    def test_fixed_choices_are_listed_so_the_model_need_not_guess(self):
        assert "one of " in ai_parse.describe_catalog_for_prompt()


class TestTheSchemaGoesIntoThePrompt:
    def test_tables_and_columns_are_listed(self):
        rendered = ai_parse.describe_workspace_for_prompt(_workspace())
        assert "sales" in rendered and "customer_id" in rendered
        assert "customers" in rendered and "customer" in rendered

    def test_an_empty_workspace_says_so_rather_than_being_blank(self):
        assert ai_parse.describe_workspace_for_prompt({}) == "(no tables loaded)"

    def test_a_very_wide_table_is_trimmed_rather_than_crowding_out_the_catalog(self):
        wide = pd.DataFrame({f"column_{index}": [1] for index in range(200)})
        workspace = {"wide": NamedFrame(name="wide", frame=wide, origin="upload", source_label="w.csv")}
        rendered = ai_parse.describe_workspace_for_prompt(workspace)
        assert "more)" in rendered
        assert "column_199" not in rendered

    def test_the_instruction_the_user_typed_is_in_the_prompt(self, monkeypatch):
        prompts: list[str] = []
        _answer(monkeypatch, ParsedSteps(steps=[_calculated_column()]), prompts)
        ai_parse.parse_instruction(PROFILE, "add a bonus column", _workspace())
        assert "add a bonus column" in prompts[0]


class TestOneStep:
    def test_a_simple_instruction_becomes_one_valid_step(self, monkeypatch):
        _answer(monkeypatch, ParsedSteps(steps=[_calculated_column()]))
        steps, warnings_out, clarification = ai_parse.parse_instruction(
            PROFILE, "add bonus = salary * 0.12", _workspace()
        )
        assert warnings_out == [] and clarification is None
        assert len(steps) == 1
        assert steps[0]["operation"] == "add_calculated_column"
        assert steps[0]["params"]["new_column"] == "bonus"

    def test_an_in_place_step_writes_back_to_the_table_it_read(self, monkeypatch):
        _answer(monkeypatch, ParsedSteps(steps=[_calculated_column()]))
        steps, _, _ = ai_parse.parse_instruction(PROFILE, "add bonus", _workspace())
        assert steps[0]["output"] == {"mode": "in_place", "name": "sales"}

    def test_a_table_named_in_the_wrong_case_still_resolves(self, monkeypatch):
        """The model reads `sales` off the prompt and writes `Sales` back. Refusing a
        good step over that would be a bad trade."""
        parsed = _calculated_column()
        parsed.inputs = [ParsedValue(name="source", value="SALES")]
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "add bonus", _workspace())
        assert warnings_out == []
        assert steps[0]["inputs"]["source"] == "sales"

    def test_a_new_table_with_no_name_gets_the_registrys_suggestion(self, monkeypatch):
        parsed = _proposed(
            "groupby_aggregate",
            {"source": "sales"},
            {"group_by": "customer_id", "value_columns": "amount", "aggregation": "sum"},
            output_mode="new",
        )
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "total per customer", _workspace())
        assert warnings_out == []
        assert steps[0]["output"]["mode"] == "new"
        assert steps[0]["output"]["name"]


class TestSeveralSteps:
    def test_a_later_step_can_read_a_table_an_earlier_step_makes(self, monkeypatch):
        """The decomposition requirement (section 2.4): step 2 validates against step 1's
        output, which does not exist in the workspace the parse started from."""
        summarise = _proposed(
            "groupby_aggregate",
            {"source": "sales"},
            {"group_by": "customer_id", "value_columns": "amount", "aggregation": "sum"},
            output_mode="new",
            output_name="totals",
        )
        sort_it = _proposed(
            "sort_rows",
            {"source": "totals"},
            {"columns": "customer_id", "ascending": "false"},
        )
        _answer(monkeypatch, ParsedSteps(steps=[summarise, sort_it]))
        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "total per customer, biggest first", _workspace()
        )
        assert warnings_out == []
        assert [step["operation"] for step in steps] == ["groupby_aggregate", "sort_rows"]
        assert steps[1]["inputs"]["source"] == "totals"

    def test_the_stored_order_is_the_order_the_model_gave(self, monkeypatch):
        first = _calculated_column("bonus", "salary * 0.12")
        second = _calculated_column("total_pay", "salary + bonus")
        _answer(monkeypatch, ParsedSteps(steps=[first, second]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "two columns", _workspace())
        assert warnings_out == []
        assert [step["params"]["new_column"] for step in steps] == ["bonus", "total_pay"]


class TestNothingOutsideTheCatalogGetsIn:
    def test_an_invented_operation_is_dropped_with_a_warning(self, monkeypatch):
        invented = _proposed("train_a_model", {"source": "sales"}, {})
        _answer(monkeypatch, ParsedSteps(steps=[invented]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "do magic", _workspace())
        assert steps == []
        assert len(warnings_out) == 1 and "train_a_model" in warnings_out[0]

    def test_an_invented_table_is_dropped_with_a_warning(self, monkeypatch):
        parsed = _calculated_column()
        parsed.inputs = [ParsedValue(name="source", value="invoices")]
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "add bonus", _workspace())
        assert steps == []
        assert "invoices" in warnings_out[0]

    def test_an_invented_column_is_dropped_with_a_warning(self, monkeypatch):
        _answer(monkeypatch, ParsedSteps(steps=[_calculated_column("bonus", "wages * 2")]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "add bonus", _workspace())
        assert steps == []
        assert len(warnings_out) == 1

    def test_a_missing_required_parameter_is_dropped_rather_than_guessed(self, monkeypatch):
        half = _proposed("add_calculated_column", {"source": "sales"}, {"new_column": "bonus"})
        _answer(monkeypatch, ParsedSteps(steps=[half]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "add bonus", _workspace())
        assert steps == []
        assert len(warnings_out) == 1

    def test_one_bad_step_does_not_take_the_good_ones_with_it(self, monkeypatch):
        good = _calculated_column()
        bad = _proposed("not_a_step", {"source": "sales"}, {})
        _answer(monkeypatch, ParsedSteps(steps=[good, bad]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "two things", _workspace())
        assert len(steps) == 1 and len(warnings_out) == 1


class TestHonestFailure:
    def test_a_clarification_comes_back_with_no_steps(self, monkeypatch):
        """Requirements 4.3: a request the catalog can't express gets a message, not the
        nearest wrong step."""
        _answer(
            monkeypatch,
            ParsedSteps(steps=[], clarification="Forecasting isn't something this tool can do."),
        )
        steps, warnings_out, clarification = ai_parse.parse_instruction(
            PROFILE, "forecast next year", _workspace()
        )
        assert steps == [] and warnings_out == []
        assert clarification == "Forecasting isn't something this tool can do."

    def test_a_model_that_is_down_returns_a_warning_rather_than_raising(self, monkeypatch):
        def fake_run(profile, prompt, schema, **kwargs):
            raise LLMConnectionError("The model refused the connection.")

        monkeypatch.setattr(ai_parse, "run_structured", fake_run)
        steps, warnings_out, clarification = ai_parse.parse_instruction(
            PROFILE, "add bonus", _workspace()
        )
        assert steps == [] and clarification is None
        assert len(warnings_out) == 1 and "refused the connection" in warnings_out[0]

    def test_an_empty_instruction_never_reaches_the_model(self, monkeypatch):
        calls: list[str] = []
        _answer(monkeypatch, ParsedSteps(steps=[]), calls)
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "   ", _workspace())
        assert calls == []
        assert steps == [] and len(warnings_out) == 1

    def test_the_workspace_it_was_given_is_not_modified(self, monkeypatch):
        """Parsing runs the steps to check them. Doing that to the caller's workspace
        would change the page underneath the user before they had pressed anything."""
        workspace = _workspace()
        before = workspace["sales"].frame.copy()
        _answer(monkeypatch, ParsedSteps(steps=[_calculated_column()]))
        ai_parse.parse_instruction(PROFILE, "add bonus", workspace)
        assert list(workspace) == ["sales", "customers"]
        pd.testing.assert_frame_equal(workspace["sales"].frame, before)


class TestTheSchemaStaysStrictFriendly:
    """The bug that made every instruction fail, and the shape that fixed it.

    A cloud profile asks its provider for *strict* structured output, and a strict schema
    cannot describe an object whose keys are not declared up front. The first version
    asked for `params` as a free-shaped dict, so the provider returned it empty every
    time and every instruction came back "'New column name' is needed to add this step".
    """

    def test_the_schema_asks_for_no_free_shaped_object(self):
        import json

        schema = json.dumps(ParsedSteps.model_json_schema())
        assert '"additionalProperties": true' not in schema

    def test_every_value_is_plain_text_so_no_field_is_a_union(self):
        fields = ParsedValue.model_fields
        assert fields["name"].annotation is str
        assert fields["value"].annotation is str


class TestTheModelIsToldWhatTheColumnsAre:
    def test_each_column_carries_its_type(self):
        rendered = ai_parse.describe_workspace_for_prompt(_workspace())
        assert "amount (number)" in rendered
        assert "customer (text)" in rendered

    def test_a_column_written_in_the_wrong_case_finds_the_real_one(self, monkeypatch):
        """The user types "qty", the model copies the prompt's casing imperfectly. The
        column exists, so refusing the step over its capitals would be a bad trade -
        while anything less exact than case stays a guess and is still refused."""
        parsed = _proposed(
            "sort_rows", {"source": "sales"}, {"columns": "AMOUNT", "ascending": "true"}
        )
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "sort it", _workspace())
        assert warnings_out == []
        assert steps[0]["params"]["columns"] == ["amount"]


class TestTextValuesBecomeTheShapeEachParameterWants:
    def test_a_comma_separated_value_becomes_a_list(self, monkeypatch):
        parsed = _proposed(
            "sort_rows", {"source": "sales"}, {"columns": "customer_id, amount"}
        )
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "sort it", _workspace())
        assert warnings_out == []
        assert steps[0]["params"]["columns"] == ["customer_id", "amount"]

    def test_one_column_is_not_split_into_its_letters(self, monkeypatch):
        """`list("amount")` is `['a', 'm', ...]`. A list-valued parameter given one name
        has to become a one-item list, not six one-letter ones."""
        parsed = _proposed("sort_rows", {"source": "sales"}, {"columns": "amount"})
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, _, _ = ai_parse.parse_instruction(PROFILE, "sort it", _workspace())
        assert steps[0]["params"]["columns"] == ["amount"]

    def test_the_word_false_means_false(self, monkeypatch):
        """`bool("false")` is True. Reading a true/false parameter that way would sort
        every table the wrong way round while looking like it had worked."""
        parsed = _proposed(
            "sort_rows", {"source": "sales"}, {"columns": "amount", "ascending": "false"}
        )
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, _, _ = ai_parse.parse_instruction(PROFILE, "biggest first", _workspace())
        assert steps[0]["params"]["ascending"] is False

    def test_a_number_written_as_text_becomes_a_number(self, monkeypatch):
        parsed = _proposed(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "bonus", "expression": "salary * 0.12", "decimals": "2"},
        )
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "add bonus", _workspace())
        assert warnings_out == []
        assert steps[0]["params"]["decimals"] == 2

    def test_a_number_parameter_holding_words_is_dropped_not_crashed_on(self, monkeypatch):
        parsed = _proposed(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "bonus", "expression": "salary * 0.12", "decimals": "two"},
        )
        _answer(monkeypatch, ParsedSteps(steps=[parsed]))
        steps, warnings_out, _ = ai_parse.parse_instruction(PROFILE, "add bonus", _workspace())
        assert steps == []
        assert len(warnings_out) == 1


class TestUpdatingAnExistingColumnByCondition:
    """`add_conditional_column` only ever adds a new column - it never overwrites one
    that already exists. An "update X if Y" instruction on an existing column has to
    come back as three ordinary catalog steps: build the new values under a throwaway
    name, drop the old column, rename the throwaway one into its place."""

    def _workspace_with_remarks(self) -> dict[str, NamedFrame]:
        sales = pd.DataFrame(
            {"mrp": [40, 60, 30], "remarks": ["ok", "ok", "ok"]}
        )
        return {"sales": NamedFrame(name="sales", frame=sales, origin="upload")}

    def test_the_three_step_recipe_validates_end_to_end(self, monkeypatch):
        add_it = _proposed(
            "add_conditional_column",
            {"source": "sales"},
            {
                "new_column": "remarks_updated",
                "column": "mrp",
                "comparison": "is less than",
                "value": "50",
                "result_if_true": "xxx",
                "result_if_false": "remarks",
            },
        )
        drop_old = _proposed("drop_columns", {"source": "sales"}, {"columns": "remarks"})
        rename_it = _proposed(
            "rename_column", {"source": "sales"}, {"column": "remarks_updated", "new_name": "remarks"}
        )
        _answer(monkeypatch, ParsedSteps(steps=[add_it, drop_old, rename_it]))

        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "change remarks to xxx if mrp is under 50", self._workspace_with_remarks()
        )

        assert warnings_out == []
        assert [step["operation"] for step in steps] == [
            "add_conditional_column",
            "drop_columns",
            "rename_column",
        ]

    def test_carrying_the_old_value_forward_keeps_the_text_not_blanks(self, monkeypatch):
        """The point of naming `remarks` as the "otherwise" answer: rows that don't match
        keep what they already said, rather than losing it to a number-only formula."""
        add_it = _proposed(
            "add_conditional_column",
            {"source": "sales"},
            {
                "new_column": "remarks_updated",
                "column": "mrp",
                "comparison": "is less than",
                "value": "50",
                "result_if_true": "xxx",
                "result_if_false": "remarks",
            },
        )
        _answer(monkeypatch, ParsedSteps(steps=[add_it]))
        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "change remarks to xxx if mrp is under 50", self._workspace_with_remarks()
        )
        assert warnings_out == []

        from transform.pipeline import apply_steps_with_report

        result_workspace, report = apply_steps_with_report(self._workspace_with_remarks(), steps)
        assert all(entry.status == "applied" for entry in report)
        assert result_workspace["sales"].frame["remarks_updated"].tolist() == ["xxx", "ok", "xxx"]


class TestANumericConditionNeedsOnlyOneStep:
    """A branch whose two answers are both numbers now fits inside the formula box, so
    "flag amounts over 200" is one `add_calculated_column` rather than the three-step
    add/drop/rename recipe a text answer still needs."""

    def test_an_inline_if_else_formula_validates_and_runs(self, monkeypatch):
        flag_it = _calculated_column("high_value", "1 if amount > 200 else 0")
        _answer(monkeypatch, ParsedSteps(steps=[flag_it]))

        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "flag amounts over 200 as 1, otherwise 0", _workspace()
        )

        assert warnings_out == []
        assert [step["operation"] for step in steps] == ["add_calculated_column"]

        from transform.pipeline import apply_steps_with_report

        result_workspace, report = apply_steps_with_report(_workspace(), steps)
        assert all(entry.status == "applied" for entry in report)
        assert result_workspace["sales"].frame["high_value"].tolist() == [0.0, 0.0, 1.0]

    def test_the_prompt_tells_the_model_the_formula_can_branch(self):
        assert "else" in ai_parse._INSTRUCTIONS
        assert "add_calculated_column" in ai_parse._INSTRUCTIONS

    def test_the_catalog_advertises_the_new_syntax(self):
        catalog = ai_parse.describe_catalog_for_prompt()
        assert "if quantity > 100 else" in catalog

    def test_a_formula_that_cannot_be_read_is_dropped_with_a_warning(self, monkeypatch):
        broken = _calculated_column("high_value", "1 if amount > 200")
        _answer(monkeypatch, ParsedSteps(steps=[broken]))

        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "flag amounts over 200", _workspace()
        )

        assert steps == []
        assert any("'if' but no 'else'" in warning for warning in warnings_out)


class TestTheFinanceHelpersReachPlainEnglish:
    """Phase 30's seven steps need no prompt of their own: the catalog is rendered from the
    registry, so registering them is what makes them askable for."""

    def test_the_new_operations_are_offered_in_the_catalog(self):
        catalog = ai_parse.describe_catalog_for_prompt()
        for operation in [
            "add_today_date",
            "shift_date",
            "month_edge",
            "row_percentage_of_total",
            "absolute_value",
            "row_min_max",
            "extract_by_position",
        ]:
            assert operation in catalog

    def test_asking_for_todays_date_becomes_one_step_that_runs(self, monkeypatch):
        stamp = _proposed("add_today_date", {"source": "sales"}, {"new_column": "Loaded_On"})
        _answer(monkeypatch, ParsedSteps(steps=[stamp]))

        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "add a column with today's date", _workspace()
        )

        assert warnings_out == []
        assert [step["operation"] for step in steps] == ["add_today_date"]
        assert steps[0]["params"]["new_column"] == "Loaded_On"

    def test_asking_which_of_two_columns_is_higher_becomes_a_row_comparison(self, monkeypatch):
        compare = _proposed(
            "row_min_max",
            {"source": "sales"},
            {"columns": "amount, salary", "which": "largest", "new_column": "higher"},
        )
        _answer(monkeypatch, ParsedSteps(steps=[compare]))

        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "add a column with the higher of amount and salary", _workspace()
        )

        assert warnings_out == []
        assert [step["operation"] for step in steps] == ["row_min_max"]
        # The comma-separated text became a real list of the table's own column names.
        assert steps[0]["params"]["columns"] == ["amount", "salary"]

        from transform.pipeline import apply_steps_with_report

        after, report = apply_steps_with_report(_workspace(), steps)
        assert all(entry.status == "applied" for entry in report)
        assert after["sales"].frame["higher"].tolist() == [100.0, 200.0, 300.0]

    def test_a_day_shift_written_as_text_becomes_a_number(self, monkeypatch):
        shift = _proposed(
            "shift_date",
            {"source": "sales"},
            {"column": "customer_id", "days": "30", "new_column": "later"},
        )
        _answer(monkeypatch, ParsedSteps(steps=[shift]))

        steps, _, _ = ai_parse.parse_instruction(
            PROFILE, "add 30 days to customer_id", _workspace()
        )

        assert steps[0]["params"]["days"] == 30

    def test_a_single_column_row_comparison_is_dropped_rather_than_guessed_at(self, monkeypatch):
        compare = _proposed(
            "row_min_max", {"source": "sales"}, {"columns": "amount", "which": "largest"}
        )
        _answer(monkeypatch, ParsedSteps(steps=[compare]))

        steps, warnings_out, _ = ai_parse.parse_instruction(
            PROFILE, "what is the largest amount on each row", _workspace()
        )

        assert steps == []
        assert any("at least two columns" in warning for warning in warnings_out)
