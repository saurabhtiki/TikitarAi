"""The report item list (requirement 7.3 step 3) — two kinds of item in one order.

No Streamlit and no provider: `report_items/model.py` is pure, which is the point of it
being separate from `report_items/session.py`.
"""

import json

import pandas as pd
import pytest

from analyst.charts import ChartChoices, ChartStyle
from report_items import model
from report_items.exceptions import ReportItemStorageError
from report_items.model import (
    KIND_COLUMN,
    KIND_REPORT,
    ReportItem,
    add_item,
    can_remove,
    find_item,
    freeze_run,
    remove_item,
    source_id_for,
)


class TestKinds:
    def test_a_new_item_is_a_report_item_by_default(self):
        items = []
        item = add_item(items)

        assert item.kind == KIND_REPORT
        assert not item.is_column_step()

    def test_a_column_step_is_never_saved_to_the_report(self):
        """The distinction the two kinds exist for: a column step's effect is the changed
        data, so it has nothing to pin even once it has run."""
        item = ReportItem(kind=KIND_COLUMN, heading="Add tax")
        item.saved_run = freeze_run("ALTER TABLE salary ADD COLUMN tax DOUBLE", pd.DataFrame())

        assert item.is_saved() is False

    def test_a_report_item_with_a_run_is_saved(self):
        item = ReportItem(kind=KIND_REPORT)
        item.saved_run = freeze_run("SELECT 1", pd.DataFrame({"a": [1]}))

        assert item.is_saved() is True

    def test_an_unknown_kind_is_read_as_a_report_item(self):
        items = []
        item = add_item(items, kind="nonsense")

        assert item.kind == KIND_REPORT

    def test_each_kind_has_its_own_placeholder_heading(self):
        assert ReportItem(kind=KIND_REPORT).display_heading() == model.UNTITLED_ITEM
        assert ReportItem(kind=KIND_COLUMN).display_heading() == model.UNTITLED_COLUMN_STEP


class TestOrdering:
    def test_items_are_appended_in_order(self):
        items = []
        first = add_item(items, heading="One")
        second = add_item(items, KIND_COLUMN, heading="Two")

        assert [item.item_id for item in items] == [first.item_id, second.item_id]

    def test_items_before_is_the_world_an_item_was_written_for(self):
        items = []
        first = add_item(items, heading="One")
        step = add_item(items, KIND_COLUMN, heading="Add tax")
        third = add_item(items, heading="Three")

        before = model.items_before(items, third.item_id)

        assert [item.item_id for item in before] == [first.item_id, step.item_id]

    def test_items_before_the_first_item_is_empty(self):
        items = []
        first = add_item(items)
        add_item(items)

        assert model.items_before(items, first.item_id) == []


class TestDeletionRule:
    """Only the last column step may go. Everything below one was written against the
    columns it added, so removing it in the middle would break them all — and the failure
    would surface next month, in a report, rather than here."""

    def test_a_report_item_can_always_be_removed(self):
        items = []
        first = add_item(items, heading="One")
        add_item(items, heading="Two")

        allowed, reason = can_remove(items, first.item_id)

        assert allowed is True
        assert reason == ""

    def test_the_last_column_step_can_be_removed(self):
        items = []
        add_item(items, heading="One")
        step = add_item(items, KIND_COLUMN, heading="Add tax")

        assert can_remove(items, step.item_id)[0] is True
        assert remove_item(items, step.item_id) is True
        assert len(items) == 1

    def test_a_column_step_with_anything_under_it_is_refused_with_a_reason(self):
        items = []
        step = add_item(items, KIND_COLUMN, heading="Add tax")
        add_item(items, heading="Tax by department")

        allowed, reason = can_remove(items, step.item_id)

        assert allowed is False
        assert "last column step" in reason
        assert remove_item(items, step.item_id) is False
        assert len(items) == 2

    def test_a_column_step_under_another_column_step_is_still_refused(self):
        items = []
        first_step = add_item(items, KIND_COLUMN, heading="Add tax")
        add_item(items, KIND_COLUMN, heading="Update tax")

        assert can_remove(items, first_step.item_id)[0] is False

    def test_removing_the_items_under_it_frees_the_step(self):
        items = []
        step = add_item(items, KIND_COLUMN, heading="Add tax")
        later = add_item(items, heading="Tax by department")

        assert remove_item(items, step.item_id) is False
        assert remove_item(items, later.item_id) is True
        assert remove_item(items, step.item_id) is True
        assert items == []

    def test_an_item_that_is_gone_is_refused_rather_than_crashing(self):
        allowed, reason = can_remove([], "nothing")

        assert allowed is False
        assert "no longer" in reason


class TestSelectors:
    def test_saved_items_are_the_report_items_with_a_run(self):
        items = []
        saved = add_item(items, heading="One")
        saved.saved_run = freeze_run("SELECT 1", pd.DataFrame({"a": [1]}))
        add_item(items, heading="Two")
        step = add_item(items, KIND_COLUMN)
        step.saved_run = freeze_run("ALTER …", pd.DataFrame())

        assert [item.item_id for item in model.saved_items(items)] == [saved.item_id]

    def test_pending_column_steps_are_the_ones_not_yet_applied(self):
        items = []
        applied = add_item(items, KIND_COLUMN, heading="Done")
        applied.applied = True
        waiting = add_item(items, KIND_COLUMN, heading="Not yet")
        add_item(items, heading="A report item")

        assert [item.item_id for item in model.pending_column_steps(items)] == [waiting.item_id]

    def test_a_source_id_is_prefixed_so_it_cannot_collide_with_a_criteria(self):
        item = ReportItem()

        assert source_id_for(item) == f"item:{item.item_id}"
        assert not source_id_for(item).startswith("check:")


class TestSavedRun:
    def test_the_frame_is_copied_rather_than_referenced(self):
        """Anything holding a live reference to a result quietly empties itself later —
        the rule `dashboard/session.py` documents at length."""
        frame = pd.DataFrame({"employee": ["Ana"]})
        run = freeze_run("SELECT 1", frame)

        frame.loc[0, "employee"] = "Changed"

        assert run.frame.loc[0, "employee"] == "Ana"
        assert run.row_count == 1

    def test_an_empty_run_counts_no_rows(self):
        assert model.SavedRun().row_count == 0


class TestSerialisation:
    def test_a_list_round_trips(self):
        items = []
        first = add_item(items, heading="Headcount")
        first.request = "How many people in each department?"
        first.hint_tables = ["employee_master"]
        first.hint_columns = ["department"]
        first.sql = "SELECT department, count(*) AS people FROM employee_master GROUP BY department"
        first.comment = "HR is the largest."
        step = add_item(items, KIND_COLUMN, heading="Add tax")
        step.request = "Add tax = 10% of basic"
        step.statements = ["ALTER TABLE salary ADD COLUMN tax DOUBLE", "UPDATE salary SET tax = basic * 0.10"]
        step.summary = "Added `tax` to `salary`."

        rebuilt = model.from_json(model.to_json(items))

        assert [item.heading for item in rebuilt] == ["Headcount", "Add tax"]
        assert rebuilt[0].kind == KIND_REPORT
        assert rebuilt[0].hint_columns == ["department"]
        assert rebuilt[0].comment == "HR is the largest."
        assert rebuilt[1].kind == KIND_COLUMN
        assert rebuilt[1].statements[1].startswith("UPDATE salary")
        assert rebuilt[1].summary == "Added `tax` to `salary`."

    def test_ids_survive_so_a_reloaded_item_keeps_the_report_item_it_owns(self):
        items = []
        item = add_item(items, heading="Headcount")

        rebuilt = model.from_json(model.to_json(items))

        assert rebuilt[0].item_id == item.item_id
        assert source_id_for(rebuilt[0]) == source_id_for(item)

    def test_rows_are_never_stored(self):
        """A recipe describes data that is not loaded yet — storing the rows would produce
        a Task that reports last month's numbers."""
        items = []
        item = add_item(items, heading="Headcount")
        item.saved_run = freeze_run("SELECT 1", pd.DataFrame({"employee": ["Ana"], "pay": [1000]}))

        stored = model.to_json(items)

        assert "Ana" not in stored
        assert "SELECT 1" in stored

    def test_a_reloaded_item_has_no_run(self):
        items = []
        item = add_item(items, heading="Headcount")
        item.sql = "SELECT 1"
        item.saved_run = freeze_run("SELECT 1", pd.DataFrame({"a": [1]}))

        rebuilt = model.from_json(model.to_json(items))

        # No run, because rebuilding one with an empty frame would put an item claiming
        # zero rows into the report. The SQL comes back so it can be re-run in one press.
        assert rebuilt[0].saved_run is None
        assert rebuilt[0].sql == "SELECT 1"

    def test_a_reloaded_column_step_is_not_applied(self):
        """A loaded recipe describes a change that has not been made to *this* session's
        tables. Reading it back as applied would leave every item below it querying a column
        nothing had added."""
        items = []
        step = add_item(items, KIND_COLUMN, heading="Add tax")
        step.applied = True

        rebuilt = model.from_json(model.to_json(items))

        assert rebuilt[0].applied is False

    def test_a_chart_spec_survives_and_a_style_without_one_does_not(self):
        items = []
        with_chart = add_item(items, heading="With a chart")
        with_chart.chart = ChartChoices(kind="Bar", x="department", measures=["people"])
        with_chart.chart_style = ChartStyle(title="Headcount")
        without = add_item(items, heading="No chart")
        without.chart_style = ChartStyle(title="Orphan")

        rebuilt = model.from_json(model.to_json(items))

        assert rebuilt[0].chart is not None
        assert rebuilt[0].chart.x == "department"
        assert rebuilt[0].chart_style.title == "Headcount"
        # A style with nothing to draw would be state with no visible cause.
        assert rebuilt[1].chart is None
        assert rebuilt[1].chart_style is None

    def test_an_empty_list_round_trips(self):
        assert model.from_json(model.to_json([])) == []

    def test_an_unknown_kind_is_kept_as_a_report_item_rather_than_dropped(self):
        stored = json.dumps({"version": 1, "items": [{"item_id": "abc", "kind": "nonsense", "heading": "Kept"}]})

        rebuilt = model.from_json(stored)

        assert len(rebuilt) == 1
        assert rebuilt[0].kind == KIND_REPORT
        assert rebuilt[0].heading == "Kept"

    def test_unreadable_json_is_refused_with_a_reason(self):
        with pytest.raises(ReportItemStorageError, match="valid JSON"):
            model.from_json("{not json")

    def test_a_json_list_is_refused_rather_than_half_read(self):
        with pytest.raises(ReportItemStorageError, match="expected format"):
            model.from_json("[]")

    def test_a_newer_schema_version_is_refused(self):
        stored = json.dumps({"version": model.SCHEMA_VERSION + 1, "items": []})

        with pytest.raises(ReportItemStorageError, match="newer version"):
            model.from_json(stored)

    def test_an_empty_string_reads_as_an_empty_list(self):
        assert model.from_json("") == []


class TestFind:
    def test_finding_an_item_that_is_not_there_returns_none(self):
        assert find_item([], "nothing") is None
