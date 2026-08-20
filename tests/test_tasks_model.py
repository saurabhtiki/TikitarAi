"""What a Task records, and what it must never record (requirement 7.5).

No Streamlit and no database: `tasks/model.py` assembles five recipe formats and adds a
persona, and all of that is testable on plain objects.

The test that matters most is `TestNeverASnapshot`. A Task is run months later against a
different file, so a stored frame or figure would produce a "recipe" that reports last
month's numbers — and nothing would go wrong loudly enough to notice.
"""

import json

import pandas as pd
import plotly.express as px
import pytest

from analyst.charts import ChartChoices, ChartStyle
from checks.model import CheckSet, add_check
from checks.model import freeze_run as freeze_check_run
from dashboard.model import Report, Section, Subsection
from dashboard.session import PinnedItem
from engine.dictionary import ColumnEntry
from engine.relationships import Relationship
from report_items.model import KIND_COLUMN, ReportItem, add_item
from report_items.model import freeze_run as freeze_item_run
from tasks import model
from tasks.exceptions import TaskStorageError
from tasks.model import Task, capture, from_json, to_json

TYPES = {
    "salary": {"employee": "text", "basic": "numeric", "joining_date": "date"},
    "employee_master": {"employee": "text", "department": "text"},
}

LINKS = [Relationship("salary", "employee", "employee_master", "employee")]

DICTIONARY = [
    ColumnEntry(
        table="salary",
        column="basic",
        sql_type="DOUBLE",
        semantic_type="numeric",
        description="Monthly basic pay",
        synonyms=["base pay"],
    ),
    ColumnEntry(table="salary", column="employee", sql_type="VARCHAR", semantic_type="text"),
]

STATEMENTS = [
    "ALTER TABLE salary ADD COLUMN tax DOUBLE",
    "UPDATE salary SET tax = basic * 0.10",
]


def _items():
    items = []
    first = add_item(items, heading="Payroll by department")
    first.request = "Total pay per department"
    first.sql = "SELECT department, sum(basic) AS pay FROM salary GROUP BY department"
    first.comment = "HR is the largest."
    first.chart = ChartChoices(kind="Bar", x="department", measures=["pay"])
    first.chart_style = ChartStyle(title="Payroll")
    step = add_item(items, KIND_COLUMN, heading="Add tax")
    step.request = "Add tax = 10% of basic"
    step.statements = list(STATEMENTS)
    return items


def _check_set():
    check_set = CheckSet(name="Payroll checks", persona="Ignored — the Task's persona wins.")
    check = add_check(check_set, "Bonus cap")
    check.criteria_text = "Bonus must be at most 5% of basic."
    check.sql = "SELECT employee, bonus AS criteria_result, 'Yes' AS criteria_met FROM salary"
    return check_set


def _report():
    report = Report(title="Monthly payroll review")
    subsection = Subsection(name="Headline")
    subsection.items = [
        PinnedItem(
            heading="Payroll by department",
            comment="HR is the largest.",
            sql="SELECT 1",
            source_id="item:abc123",
            column_with_previous=True,
        )
    ]
    section = Section(name="Overview")
    section.subsections = [subsection]
    report.sections = [section]
    return report


@pytest.fixture
def task():
    return capture(
        "Monthly salary review",
        description="Run after payroll closes.",
        persona="You are a finance controller.",
        semantic_types_by_table=TYPES,
        relationships=LINKS,
        dictionary=DICTIONARY,
        statements=STATEMENTS,
        report_items=_items(),
        checks=_check_set(),
        report=_report(),
    )


class TestCapture:
    def test_it_takes_its_name_description_and_persona(self, task):
        assert task.name == "Monthly salary review"
        assert task.description == "Run after payroll closes."
        assert task.persona == "You are a finance controller."
        assert task.task_id is None

    def test_the_schema_signature_is_a_chat_type(self, task):
        """Stored in that format because it is the format `chat_types.matching` already knows
        how to measure next month's upload against."""
        assert sorted(task.schema.table_names()) == ["employee_master", "salary"]
        assert task.schema.table("salary").types_by_column()["joining_date"] == "date"
        assert task.schema.relationships == LINKS

    def test_only_described_columns_are_captured(self, task):
        """A dictionary is mostly blank rows, and a blank description restores to a blank
        description either way."""
        assert [saved.column for saved in task.schema.descriptions] == ["basic"]

    def test_the_calculated_columns_keep_their_order(self, task):
        """The order is the whole content of the field — requirement 8.2 replays it."""
        assert task.calculated_columns == STATEMENTS

    def test_the_lists_are_copied_rather_than_referenced(self, task):
        """`capture` runs on Save and the rerun that follows hands the same objects back to
        the view, so a Task holding live references would be rewritten under the user."""
        task.report_items.append(ReportItem(heading="Added later"))

        assert len(task.report_items) == 3

    def test_a_blank_name_is_kept_as_blank_rather_than_invented(self, task):
        """Refusing it is `tasks.db.save_task`'s job, where there is somewhere to report it."""
        assert capture("   ", semantic_types_by_table={}, relationships=[], dictionary=[],
                       statements=[], report_items=[], checks=CheckSet(), report=Report()).name == ""

    def test_an_unnamed_task_still_displays_as_something(self):
        assert Task().display_name() == model.UNTITLED_TASK


class TestRoundTrip:
    def test_every_part_comes_back(self, task):
        rebuilt = from_json(to_json(task), task_id=7, name=task.name, description=task.description)

        assert rebuilt.task_id == 7
        assert rebuilt.name == "Monthly salary review"
        assert rebuilt.description == "Run after payroll closes."
        assert rebuilt.persona == "You are a finance controller."
        assert sorted(rebuilt.schema.table_names()) == ["employee_master", "salary"]
        assert rebuilt.calculated_columns == STATEMENTS
        assert [item.heading for item in rebuilt.report_items] == ["Payroll by department", "Add tax"]
        assert [check.name for check in rebuilt.checks.checks] == ["Bonus cap"]
        assert rebuilt.report.title == "Monthly payroll review"

    def test_the_report_skeleton_keeps_its_arrangement(self, task):
        """Requirement 8.2 step 4 fills a loaded skeleton back in by `source_id`, so the
        arrangement has to survive with the ids intact or a re-run has nowhere to put its
        results."""
        rebuilt = from_json(to_json(task))

        subsection = rebuilt.report.sections[0].subsections[0]
        assert rebuilt.report.sections[0].name == "Overview"
        assert subsection.name == "Headline"
        assert subsection.items[0].source_id == "item:abc123"
        assert subsection.items[0].column_with_previous is True

    def test_a_report_items_chart_survives(self, task):
        rebuilt = from_json(to_json(task))

        assert rebuilt.report_items[0].chart.x == "department"
        assert rebuilt.report_items[0].chart_style.title == "Payroll"

    def test_a_column_steps_statements_survive_and_it_comes_back_unapplied(self, task):
        """A loaded recipe describes a change that has not been made to *this* session's
        tables. Reading it back as applied would leave every item below it querying a column
        nothing had added."""
        rebuilt = from_json(to_json(task))

        step = rebuilt.report_items[1]
        assert step.statements == STATEMENTS
        assert step.applied is False

    def test_an_empty_task_round_trips(self):
        rebuilt = from_json(to_json(Task()))

        assert rebuilt.report_items == []
        assert rebuilt.checks.checks == []
        assert rebuilt.report.sections == []

    def test_the_sub_formats_are_nested_as_objects_not_escaped_strings(self, task):
        """Readable is the point: a recipe someone has to debug a year from now should not be
        four JSON documents inside one."""
        payload = json.loads(to_json(task))

        assert isinstance(payload["checks"], dict)
        assert isinstance(payload["report_items"], dict)
        assert isinstance(payload["schema"], dict)
        assert isinstance(payload["report"], dict)


class TestNeverASnapshot:
    def test_no_report_item_rows_reach_the_json(self, task):
        task.report_items[0].saved_run = freeze_item_run(
            "SELECT 1", pd.DataFrame({"employee": ["Ana"], "pay": [1000]})
        )

        stored = to_json(task)

        assert "Ana" not in stored
        assert "GROUP BY department" in stored

    def test_no_criteria_rows_reach_the_json(self, task):
        task.checks.checks[0].saved_run = freeze_check_run(
            "SELECT 1",
            pd.DataFrame({"employee": ["Zed"], "criteria_result": [9.0], "criteria_met": ["No"]}),
        )

        assert "Zed" not in to_json(task)

    def test_no_pinned_frame_or_figure_reaches_the_json(self, task):
        item = task.report.sections[0].subsections[0].items[0]
        item.frame = pd.DataFrame({"secret_column": ["Ana"]})
        item.figure = px.bar(x=["a"], y=[1])

        stored = to_json(task)

        assert "secret_column" not in stored
        assert "plotly" not in stored.lower()

    def test_a_loaded_report_item_has_no_run_to_report(self, task):
        task.report_items[0].saved_run = freeze_item_run("SELECT 1", pd.DataFrame({"a": [1]}))

        rebuilt = from_json(to_json(task))

        assert rebuilt.report_items[0].saved_run is None
        assert rebuilt.report_items[0].sql.startswith("SELECT department")

    def test_a_loaded_report_has_empty_items_ready_to_be_filled(self, task):
        """A loaded skeleton is a report whose arrangement is right and whose contents are
        pending — a re-run pins into it by `source_id`."""
        rebuilt = from_json(to_json(task))

        item = rebuilt.report.sections[0].subsections[0].items[0]
        assert item.frame is None
        assert item.figure is None
        assert item.heading == "Payroll by department"


class TestRefusals:
    def test_unreadable_json_is_refused_with_a_reason(self):
        with pytest.raises(TaskStorageError, match="valid JSON"):
            from_json("{not json")

    def test_a_json_list_is_refused_rather_than_half_read(self):
        with pytest.raises(TaskStorageError, match="expected format"):
            from_json("[]")

    def test_a_newer_schema_version_is_refused(self):
        stored = json.dumps({"version": model.SCHEMA_VERSION + 1})

        with pytest.raises(TaskStorageError, match="newer version"):
            from_json(stored)

    def test_an_unreadable_embedded_part_is_reported_as_the_tasks_failure(self):
        """The user pressed Open on a task, not on a criteria set — so the message says the
        task couldn't be read, with the part's own wording attached."""
        stored = json.dumps({"version": 1, "checks": {"version": 999}})

        with pytest.raises(TaskStorageError, match="couldn't be read"):
            from_json(stored)

    def test_an_empty_string_reads_as_an_empty_task(self):
        assert from_json("").report_items == []


class TestRecipeFingerprint:
    """What "Unsaved changes" is measured with.

    The rule it exists for: opening a saved Task deliberately does *not* restore the session,
    so anything read from the live session would report a just-opened Task as edited before
    the user had touched it.
    """

    def test_the_same_recipe_fingerprints_the_same(self, task):
        assert model.recipe_fingerprint(task) == model.recipe_fingerprint(task)

    def test_the_schema_and_the_calculated_columns_are_not_part_of_it(self, task):
        loaded_elsewhere = capture(
            task.name,
            description=task.description,
            persona=task.persona,
            semantic_types_by_table={},
            relationships=[],
            dictionary=[],
            statements=[],
            report_items=task.report_items,
            checks=task.checks,
            report=task.report,
        )

        assert model.recipe_fingerprint(loaded_elsewhere) == model.recipe_fingerprint(task)

    def test_an_edited_report_item_changes_it(self, task):
        before = model.recipe_fingerprint(task)
        task.report_items[0].heading = "Payroll by cost centre"

        assert model.recipe_fingerprint(task) != before

    def test_a_new_persona_changes_it(self, task):
        before = model.recipe_fingerprint(task)
        task.persona = "You are an auditor."

        assert model.recipe_fingerprint(task) != before

    def test_a_rename_changes_it(self, task):
        before = model.recipe_fingerprint(task)
        task.name = "Quarterly review"

        assert model.recipe_fingerprint(task) != before


class TestSummaryLine:
    def test_it_counts_what_the_picker_needs_to_tell_two_tasks_apart(self, task):
        assert task.summary_line() == "2 table(s) · 2 report item(s) · 1 criteria"
