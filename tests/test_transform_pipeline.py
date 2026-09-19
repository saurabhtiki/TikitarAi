"""Building a step list, validating it, and running it across the workspace."""

import json

import pandas as pd
import pytest

from transform.exceptions import (
    DuplicateFrameNameError,
    FrameNotFoundError,
    InvalidOperationError,
    InvalidStepParamsError,
)
from transform.pipeline import (
    append_step,
    apply_steps,
    apply_steps_with_report,
    describe_steps,
    frames_created_by,
    input_names,
    make_step,
    remove_last_step,
    replace_last_step,
    step_headline,
    suggested_output_name,
    to_json,
    validate_step,
)
from transform.registry import get_operation
from transform.workspace import NamedFrame, frame_names


@pytest.fixture
def workspace() -> dict[str, NamedFrame]:
    sales = pd.DataFrame(
        {
            "customer_id": ["1", "1", "2", "3"],
            "basic": ["100", "200", "300", "400"],
            "amount": ["10", "20", "30", "40"],
        }
    )
    customers = pd.DataFrame(
        {"customer_id": ["1", "2", "3"], "customer": ["Acme", "Bolt", "Cog"]}
    )
    return {
        "sales": NamedFrame(name="sales", frame=sales),
        "customers": NamedFrame(name="customers", frame=customers),
    }


def join_step() -> dict:
    return make_step(
        "merge",
        {"left": "sales", "right": "customers"},
        {"left_on": ["customer_id"], "join_type": "keep all rows from the left table"},
        "new",
        "merged_data",
    )


def bonus_step() -> dict:
    return make_step(
        "add_calculated_column",
        {"source": "merged_data"},
        {"new_column": "bonus", "expression": "basic * 0.12"},
        "in_place",
        "merged_data",
    )


def summary_step() -> dict:
    return make_step(
        "groupby_aggregate",
        {"source": "merged_data"},
        {"group_by": ["customer"], "value_columns": ["amount", "bonus"], "aggregation": "sum"},
        "new",
        "customer_summary",
    )


class TestMakeStep:
    def test_an_unknown_operation_is_refused(self):
        with pytest.raises(InvalidOperationError):
            make_step("teleport", {"source": "sales"}, {}, "in_place", "sales")

    def test_an_in_place_step_still_records_an_output_name(self):
        step = make_step("sort_rows", {"source": "sales"}, {"columns": ["amount"]}, "in_place", "sales")
        assert step["output"] == {"mode": "in_place", "name": "sales"}

    def test_the_output_name_is_held_to_excels_rules(self):
        step = make_step("merge", {"left": "a", "right": "b"}, {}, "new", "Q1: North/South")
        assert step["output"]["name"] == "Q1_ North_South"

    def test_it_copies_its_inputs_so_the_caller_cannot_change_it_afterwards(self):
        params = {"columns": ["amount"]}
        step = make_step("sort_rows", {"source": "sales"}, params, "in_place", "sales")
        params["columns"].append("basic")
        assert step["params"]["columns"] == ["amount"]


class TestValidateStep:
    def test_a_good_step_passes(self, workspace):
        validate_step(join_step(), workspace)

    def test_a_step_naming_a_table_that_isnt_there_is_refused(self, workspace):
        step = make_step("sort_rows", {"source": "nowhere"}, {"columns": ["amount"]}, "in_place", "nowhere")
        with pytest.raises(FrameNotFoundError, match="nowhere"):
            validate_step(step, workspace)

    def test_a_role_left_empty_is_refused_by_its_label(self, workspace):
        step = make_step("merge", {"left": "sales", "right": ""}, {"left_on": ["customer_id"]}, "new", "out")
        with pytest.raises(InvalidStepParamsError, match="Second table"):
            validate_step(step, workspace)

    def test_appending_needs_at_least_two_tables(self, workspace):
        step = make_step("concat", {"frames": ["sales"]}, {}, "new", "stacked")
        with pytest.raises(InvalidStepParamsError, match="at least two tables"):
            validate_step(step, workspace)

    def test_a_new_table_cannot_take_an_existing_name(self, workspace):
        step = make_step(
            "merge",
            {"left": "sales", "right": "customers"},
            {"left_on": ["customer_id"]},
            "new",
            "sales",
        )
        with pytest.raises(DuplicateFrameNameError, match="already exists"):
            validate_step(step, workspace)

    def test_the_duplicate_check_ignores_capitals(self, workspace):
        step = make_step(
            "merge",
            {"left": "sales", "right": "customers"},
            {"left_on": ["customer_id"]},
            "new",
            "SALES",
        )
        with pytest.raises(DuplicateFrameNameError):
            validate_step(step, workspace)

    def test_an_in_place_step_may_reuse_its_own_tables_name(self, workspace):
        step = make_step("sort_rows", {"source": "sales"}, {"columns": ["amount"]}, "in_place", "sales")
        validate_step(step, workspace)

    def test_the_operations_own_validator_still_runs(self, workspace):
        step = make_step("sort_rows", {"source": "sales"}, {"columns": []}, "in_place", "sales")
        with pytest.raises(InvalidStepParamsError, match="at least one column to sort"):
            validate_step(step, workspace)

    def test_a_formula_naming_a_missing_column_is_caught_before_the_step_is_added(self, workspace):
        step = make_step(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "x", "expression": "nosuchcolumn * 2"},
            "in_place",
            "sales",
        )
        with pytest.raises(InvalidStepParamsError, match="nosuchcolumn"):
            validate_step(step, workspace)


class TestStepListEditing:
    def test_appending_leaves_the_original_list_alone(self):
        original = [join_step()]
        extended = append_step(original, bonus_step())
        assert len(original) == 1
        assert len(extended) == 2

    def test_replacing_the_last_step_swaps_only_that_one(self):
        steps = [join_step(), bonus_step()]
        replaced = replace_last_step(steps, summary_step())
        assert replaced[0]["operation"] == "merge"
        assert replaced[1]["operation"] == "groupby_aggregate"

    def test_removing_the_last_step_shortens_the_list(self):
        steps = [join_step(), bonus_step()]
        assert len(remove_last_step(steps)) == 1

    def test_editing_an_empty_list_is_an_error(self):
        with pytest.raises(IndexError, match="no step to edit"):
            replace_last_step([], join_step())

    def test_deleting_from_an_empty_list_is_an_error(self):
        with pytest.raises(IndexError, match="no step to delete"):
            remove_last_step([])


class TestFramesCreatedBy:
    def test_a_step_that_makes_a_new_table_names_it(self):
        assert frames_created_by([join_step()], 0) == ["merged_data"]

    def test_an_in_place_step_creates_nothing(self):
        assert frames_created_by([join_step(), bonus_step()], 1) == []

    def test_an_index_outside_the_list_creates_nothing(self):
        assert frames_created_by([join_step()], 5) == []


class TestRunningThePipeline:
    def test_the_worked_example_from_the_requirements_produces_the_right_totals(self, workspace):
        steps = [join_step(), bonus_step(), summary_step()]
        final = apply_steps(workspace, steps)

        summary = final["customer_summary"].frame.set_index("customer")
        # Acme is the two rows for customer 1: amounts 10 + 20, bonus 12 + 24.
        assert summary.loc["Acme", "sum of amount"] == 30.0
        assert summary.loc["Acme", "sum of bonus"] == 36.0
        assert summary.loc["Cog", "sum of bonus"] == 48.0

    def test_every_table_along_the_way_stays_in_the_workspace(self, workspace):
        final = apply_steps(workspace, [join_step(), bonus_step(), summary_step()])
        assert frame_names(final) == ["sales", "customers", "merged_data", "customer_summary"]

    def test_running_the_steps_never_changes_the_uploaded_tables(self, workspace):
        before = workspace["sales"].frame.copy(deep=True)
        apply_steps(workspace, [join_step(), bonus_step(), summary_step()])
        pd.testing.assert_frame_equal(workspace["sales"].frame, before)

    def test_running_twice_gives_the_same_answer(self, workspace):
        steps = [join_step(), bonus_step(), summary_step()]
        first = apply_steps(workspace, steps)["customer_summary"].frame
        second = apply_steps(workspace, steps)["customer_summary"].frame
        pd.testing.assert_frame_equal(first, second)

    def test_a_derived_table_records_which_step_made_it(self, workspace):
        final = apply_steps(workspace, [join_step()])
        made = final["merged_data"]
        assert made.origin == "step"
        assert made.created_by_step == 0
        assert "step 1" in made.source_label

    def test_there_is_one_outcome_per_step_naming_its_output(self, workspace):
        _, report = apply_steps_with_report(workspace, [join_step(), bonus_step(), summary_step()])
        assert [outcome.output_name for outcome in report] == [
            "merged_data",
            "merged_data",
            "customer_summary",
        ]

    def test_an_outcome_records_how_the_row_count_changed(self, workspace):
        _, report = apply_steps_with_report(workspace, [join_step(), bonus_step(), summary_step()])
        assert report[2].rows_before == 4
        assert report[2].rows_after == 3

    def test_an_appending_step_is_given_the_names_of_the_tables_it_stacked(self, workspace):
        step = make_step(
            "concat", {"frames": ["sales", "sales"]}, {"add_source_column": True}, "new", "stacked"
        )
        final = apply_steps(workspace, [step])
        assert set(final["stacked"].frame["source table"]) == {"sales"}


class TestAFailingStepStopsTheRun:
    def test_the_failure_is_reported_rather_than_raised(self, workspace):
        broken = make_step(
            "groupby_aggregate",
            {"source": "sales"},
            {"group_by": ["customer_id"], "value_columns": ["basic"], "aggregation": "sum"},
            "new",
            "ok_summary",
        )
        missing_table = make_step(
            "sort_rows", {"source": "nowhere"}, {"columns": ["amount"]}, "in_place", "nowhere"
        )
        _, report = apply_steps_with_report(workspace, [broken, missing_table])

        assert report[0].ok
        assert report[1].status == "failed"
        assert not report[1].ok

    def test_nothing_after_the_failure_runs(self, workspace):
        missing_table = make_step(
            "sort_rows", {"source": "nowhere"}, {"columns": ["amount"]}, "in_place", "nowhere"
        )
        final, report = apply_steps_with_report(
            workspace, [missing_table, join_step(), summary_step()]
        )
        assert len(report) == 1
        assert "merged_data" not in final

    def test_the_tables_made_before_the_failure_are_kept(self, workspace):
        missing_table = make_step(
            "sort_rows", {"source": "nowhere"}, {"columns": ["amount"]}, "in_place", "nowhere"
        )
        final, _ = apply_steps_with_report(workspace, [join_step(), missing_table])
        assert "merged_data" in final

    def test_an_unregistered_operation_raises_rather_than_being_reported(self, workspace):
        with pytest.raises(InvalidOperationError):
            apply_steps_with_report(workspace, [{"operation": "teleport", "inputs": {}, "params": {}, "output": {"mode": "new", "name": "x"}}])


class TestDescriptions:
    def test_a_step_reads_as_a_sentence_naming_its_tables(self):
        assert step_headline(join_step(), 0) == (
            "1. Joined sales with customers on customer_id -> merged_data"
        )

    def test_an_in_place_step_does_not_repeat_its_table_name(self):
        assert step_headline(bonus_step(), 1) == "2. Added bonus = basic * 0.12"

    def test_every_step_gets_a_line(self):
        assert len(describe_steps([join_step(), bonus_step(), summary_step()])) == 3

    def test_a_step_from_a_newer_version_still_gets_a_line(self):
        unknown = {"operation": "teleport", "inputs": {}, "params": {}, "output": {"mode": "new", "name": "x"}}
        assert "Unknown step" in describe_steps([unknown])[0]


class TestHelpers:
    def test_input_names_flattens_every_role(self):
        assert input_names(join_step()) == ["sales", "customers"]

    def test_input_names_flattens_a_multi_table_role(self):
        step = make_step("concat", {"frames": ["a", "b", "c"]}, {}, "new", "stacked")
        assert input_names(step) == ["a", "b", "c"]

    def test_a_suggested_name_is_built_from_the_first_input(self, workspace):
        spec = get_operation("groupby_aggregate")
        assert suggested_output_name(spec, {"source": "sales"}, workspace) == "sales_summary"

    def test_a_suggested_name_avoids_one_that_is_taken(self, workspace):
        spec = get_operation("groupby_aggregate")
        workspace["sales_summary"] = NamedFrame(name="sales_summary", frame=pd.DataFrame())
        assert suggested_output_name(spec, {"source": "sales"}, workspace) == "sales_summary_2"


class TestSerialisation:
    def test_a_step_list_survives_a_json_round_trip_unchanged(self):
        steps = [join_step(), bonus_step(), summary_step()]
        assert json.loads(json.dumps(steps)) == steps

    def test_to_json_records_the_version_alongside_the_steps(self):
        payload = json.loads(to_json([join_step()]))
        assert payload["version"] == 1
        assert payload["steps"][0]["operation"] == "merge"
