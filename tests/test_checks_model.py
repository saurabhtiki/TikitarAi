"""The criteria-set shape and its JSON round trip (requirement 6.5).

No Streamlit, no database, no provider: `checks/model.py` is pure by design and this suite
is what holds it that way.
"""

import json

import pandas as pd
import pytest

from analyst.charts import (
    AGG_COUNT,
    AGG_SUM,
    CHART_COMBO,
    Aggregation,
    ChartChoices,
    ChartStyle,
)
from checks.exceptions import ChecksStorageError
from checks.model import (
    COLUMN_MET,
    COLUMN_RESULT,
    FILTER_ALL,
    FILTER_FAILURES,
    FILTER_PASSES,
    SCHEMA_VERSION,
    ActionDraft,
    CheckSet,
    add_check,
    filter_rows,
    find_action,
    find_check,
    freeze_run,
    from_json,
    remove_action,
    remove_check,
    to_json,
)

RESULT = pd.DataFrame(
    {
        "employee": ["Ana", "Bo", "Cy"],
        COLUMN_RESULT: [4.0, 12.0, 9.0],
        COLUMN_MET: ["Yes", "No", "no"],
    }
)


def _populated_set() -> CheckSet:
    check_set = CheckSet(
        name="Payroll",
        persona="You are a finance controller.",
        summary="- One rule, two breaches.",
    )
    check = add_check(check_set, "Bonus within policy")
    check.criteria_text = "Bonus must be at most 5% of basic."
    check.hint_tables = ["salary"]
    check.hint_columns = ["salary.bonus"]
    check.sql = "SELECT employee, bonus AS criteria_result, 'No' AS criteria_met FROM salary"
    check.remarks = "- Two employees breached the cap."
    check.saved_run = freeze_run(check.sql, RESULT)
    check.actions.append(ActionDraft(kind="email", recipients={"to": ["hr@example.com"]}, subject="Bonus review"))
    return check_set


class TestFiltering:
    def test_failures_and_passes_split_the_rows(self):
        assert list(filter_rows(RESULT, FILTER_PASSES)["employee"]) == ["Ana"]
        assert list(filter_rows(RESULT, FILTER_FAILURES)["employee"]) == ["Bo", "Cy"]

    def test_all_returns_every_row(self):
        assert len(filter_rows(RESULT, FILTER_ALL)) == 3

    def test_case_is_ignored_so_a_lowercase_verdict_still_counts(self):
        """'no' is the model's wording, not a third category — counting it as neither would
        be a silently wrong answer rather than a visible error."""
        assert "Cy" in list(filter_rows(RESULT, FILTER_FAILURES)["employee"])

    def test_a_frame_without_the_verdict_column_is_left_alone(self):
        frame = pd.DataFrame({"employee": ["Ana"]})
        assert filter_rows(frame, FILTER_FAILURES) is frame


class TestFreezeRun:
    def test_counts_are_precomputed(self):
        run = freeze_run("SELECT 1", RESULT)
        assert (run.pass_count, run.fail_count, run.row_count) == (1, 2, 3)

    def test_the_frame_is_a_copy_not_the_one_handed_in(self):
        """The pin lesson, applied a second time: a saved run that referenced a live frame
        would change under the user after the next test press."""
        source = RESULT.copy()
        run = freeze_run("SELECT 1", source)
        source.loc[0, "employee"] = "changed"
        assert run.frame.loc[0, "employee"] == "Ana"

    def test_failures_are_reachable_from_the_run(self):
        assert len(freeze_run("SELECT 1", RESULT).failures()) == 2


class TestSetOperations:
    def test_add_find_and_remove(self):
        check_set = CheckSet()
        check = add_check(check_set, "  Bonus cap  ")
        assert check.name == "Bonus cap"
        assert find_check(check_set, check.check_id) is check
        assert remove_check(check_set, check.check_id) is True
        assert remove_check(check_set, check.check_id) is False

    def test_an_unnamed_criteria_still_has_a_heading(self):
        assert add_check(CheckSet(), "").display_name() == "Untitled criteria"

    def test_saved_checks_are_only_the_ones_with_a_run(self):
        check_set = _populated_set()
        add_check(check_set, "Not tested yet")
        assert [check.display_name() for check in check_set.saved_checks()] == ["Bonus within policy"]

    def test_actions_can_be_found_and_removed(self):
        check = _populated_set().checks[0]
        action = check.actions[0]
        assert find_action(check, action.action_id) is action
        assert remove_action(check, action.action_id) is True
        assert remove_action(check, action.action_id) is False

    def test_only_confirmed_actions_are_reported(self):
        check = _populated_set().checks[0]
        assert check.confirmed_actions() == []
        check.actions[0].confirmed = True
        assert len(check.confirmed_actions()) == 1


class TestSerialisation:
    def test_a_set_round_trips(self):
        original = _populated_set()
        restored = from_json(to_json(original), set_id=7, name=original.name)

        assert restored.set_id == 7
        assert restored.persona == original.persona
        # The whole-set overview is the user's own writing once they have edited it, so it
        # is stored with the rules rather than rebuilt on load.
        assert restored.summary == original.summary
        assert len(restored.checks) == 1

        check = restored.checks[0]
        assert check.check_id == original.checks[0].check_id
        assert check.criteria_text == original.checks[0].criteria_text
        assert check.hint_columns == ["salary.bonus"]
        assert check.sql == original.checks[0].sql
        assert check.remarks == original.checks[0].remarks
        assert check.actions[0].recipients == {"to": ["hr@example.com"]}

    def test_the_rows_are_never_stored(self):
        """A stored frame would describe tables that are gone — the trap §6.1 step 5 names
        for conversations, applied to results."""
        payload = json.loads(to_json(_populated_set()))
        assert "Ana" not in json.dumps(payload)
        assert payload["checks"][0]["last_run"]["fail_count"] == 2

    def test_a_reloaded_criteria_has_no_saved_run(self):
        """So a set loaded before today's file is uploaded can't put a report item into the
        world claiming results it doesn't have."""
        restored = from_json(to_json(_populated_set()))
        assert restored.checks[0].saved_run is None
        assert restored.saved_checks() == []

    def test_the_version_is_recorded(self):
        assert json.loads(to_json(CheckSet()))["version"] == SCHEMA_VERSION

    def test_broken_json_is_refused_with_a_readable_message(self):
        with pytest.raises(ChecksStorageError, match="valid JSON"):
            from_json("{not json")

    def test_a_json_list_is_refused(self):
        with pytest.raises(ChecksStorageError, match="expected format"):
            from_json("[]")

    def test_a_set_saved_before_the_summary_existed_still_loads(self):
        """`summary` was added without a version bump, on the grounds that its absence reads
        back as an empty summary — which is exactly what such a set had."""
        restored = from_json('{"version": 1, "persona": "P", "checks": []}')
        assert restored.summary == ""
        assert restored.persona == "P"

    def test_a_newer_schema_is_refused_rather_than_half_read(self):
        with pytest.raises(ChecksStorageError, match="newer version"):
            from_json(json.dumps({"version": SCHEMA_VERSION + 1, "checks": []}))

    def test_a_set_with_no_checks_key_loads_empty(self):
        assert from_json(json.dumps({"version": 1, "persona": "x"})).checks == []


class TestTheStoredChart:
    """A criteria's chart is stored the way its SQL is: as the recipe, not the result. That
    is what makes re-running a saved set against next month's file redraw the same chart."""

    def _with_a_chart(self):
        check_set = CheckSet(name="Payroll")
        check = add_check(check_set, "Bonus cap")
        check.chart = ChartChoices(
            kind=CHART_COMBO,
            x="department",
            aggregate_by_x=True,
            aggregations=[Aggregation("basic", AGG_SUM), Aggregation("employee", AGG_COUNT)],
            line_measures=["Count of employee"],
            secondary_axis=True,
        )
        check.chart_style = ChartStyle(title="Breaches by department", show_values=True)
        return check_set

    def test_it_survives_the_round_trip(self):
        restored = from_json(to_json(self._with_a_chart()))
        original = self._with_a_chart().checks[0]
        assert restored.checks[0].chart == original.chart
        assert restored.checks[0].chart_style == original.chart_style

    def test_a_criteria_with_no_chart_stays_that_way(self):
        restored = from_json(to_json(_populated_set()))
        assert restored.checks[0].chart is None
        assert restored.checks[0].chart_style is None

    def test_a_set_saved_before_charts_existed_still_loads(self):
        """Added without a version bump, on the same grounds as `summary`: its absence reads
        back as no chart, which is what such a set had."""
        restored = from_json(
            json.dumps({"version": 1, "checks": [{"check_id": "a1", "name": "Rule"}]})
        )
        assert restored.checks[0].chart is None
        assert restored.checks[0].name == "Rule"

    def test_a_style_with_no_chart_to_wear_is_not_rebuilt(self):
        """Style with nothing to draw would be state with no visible cause."""
        restored = from_json(
            json.dumps({"version": 1, "checks": [{"check_id": "a1", "chart_style": {"title": "x"}}]})
        )
        assert restored.checks[0].chart_style is None
