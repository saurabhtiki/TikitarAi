"""Replaying a saved Task against this month's tables (requirement 8.2).

A real in-memory DuckDB and no network: the provider seam (`run_structured`) is replaced in
each of the four modules that call it, and the default replacement **fails the test if it is
called at all** — because "producing the numbers costs no provider call" is the load-bearing
claim of this whole stage, not an optimisation, and a stub that quietly answered would hide
the day it stopped being true.

`_run` therefore leaves the comment rewrite **off** unless a test is about it. Rewriting the
commentary is the one part of a run that is always a provider call by design (requirement 8.2
step 4 asks for the persona to be applied wherever commentary is generated), and it is a
choice the user makes on the page rather than something replaying a recipe requires.
"""

import duckdb
import pytest

from analyst import commentary
from checks import remarks as checks_remarks
from checks import sql_builder as checks_sql
from checks.model import Check, CheckSet, source_id_for as check_source_id_for
from dashboard.model import PinnedItem, Report, Section, Subsection, find_item_by_source
from report_items import sql_builder as items_sql
from report_items.model import KIND_COLUMN, ReportItem, source_id_for
from runner import replay
from runner.model import STATUS_FAILED, STATUS_FALLBACK, STATUS_OK, STATUS_SKIPPED
from tasks.model import Task

SCHEMA = "Table salary: employee (VARCHAR), department (VARCHAR), basic (DOUBLE), bonus (DOUBLE)"

HEADCOUNT_SQL = "SELECT department, count(*) AS people FROM salary GROUP BY department ORDER BY department"

BONUS_CHECK_SQL = (
    "SELECT employee, department, bonus AS criteria_result, "
    "CASE WHEN bonus <= basic * 0.05 THEN 'Yes' ELSE 'No' END AS criteria_met FROM salary "
    "ORDER BY employee"
)

PROFILE = {"profile_id": 1, "provider": "test", "model": "test"}


@pytest.fixture
def connection():
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE salary (employee VARCHAR, department VARCHAR, basic DOUBLE, bonus DOUBLE)")
    connection.execute(
        "INSERT INTO salary VALUES "
        "('Ana', 'HR', 1000, 40), ('Bo', 'Ops', 1000, 120), ('Cy', 'HR', 2000, 50)"
    )
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    """Every LLM seam, replaced by one that fails the test if anything reaches it.

    Individual tests that *want* a call install their own over the top of this.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("A replay made a provider call it should not have made.")

    for module in (items_sql, checks_sql, commentary, checks_remarks):
        monkeypatch.setattr(module, "run_structured", refuse)


def _report(*sources: str) -> Report:
    """A one-section report with a placed, empty item per source id — a skeleton, as
    `dashboard.skeleton.from_dict` hands one back."""
    subsection = Subsection(name="Findings")
    for source in sources:
        subsection.items.append(
            PinnedItem(question=source, heading=source, source_id=source, comment="last month's note")
        )
    section = Section(name="Payroll")
    section.subsections = [subsection]
    report = Report(title="Monthly payroll")
    report.sections = [section]
    return report


def _task(*, items=None, checks=None, report=None, persona="You are a payroll controller.") -> Task:
    return Task(
        name="Monthly payroll",
        persona=persona,
        report_items=list(items or []),
        checks=checks or CheckSet(),
        report=report or Report(),
    )


def _run(connection, task, **kwargs):
    kwargs.setdefault("loaded_tables", ["salary"])
    kwargs.setdefault("schema_context", SCHEMA)
    kwargs.setdefault("rewrite_comments", False)
    return replay.run_recipe(connection, task, **kwargs)


def _stub(monkeypatch, module, *replies):
    """Points one module's LLM seam at a queue of canned answers."""
    queue = list(replies)

    def fake(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
        return queue.pop(0)

    monkeypatch.setattr(module, "run_structured", fake)


class TestTheHappyPathCostsNothing:
    def test_producing_the_numbers_makes_no_provider_call(self, connection):
        """The claim this whole stage rests on. `no_provider` is what enforces it."""
        item = ReportItem(heading="Headcount", request="People per department", sql=HEADCOUNT_SQL)
        task = _task(items=[item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=PROFILE)

        assert [step.status for step in result.steps] == [STATUS_OK]

    def test_the_rows_land_in_the_slot_the_skeleton_held(self, connection):
        item = ReportItem(heading="Headcount", request="People per department", sql=HEADCOUNT_SQL)
        task = _task(items=[item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=PROFILE)

        placed = find_item_by_source(result.report, source_id_for(item))
        assert list(placed.frame["department"]) == ["HR", "Ops"]
        assert list(placed.frame["people"]) == [2, 1]
        assert placed.sql == HEADCOUNT_SQL

    def test_an_item_the_skeleton_never_placed_still_runs_and_is_simply_not_shown(self, connection):
        """The exports walk the section tree only, so there is nowhere to put it — but the
        run summary still records that it ran."""
        item = ReportItem(heading="Headcount", sql=HEADCOUNT_SQL)
        task = _task(items=[item], report=_report())

        result = _run(connection, task, profile=PROFILE)

        assert result.steps[0].status == STATUS_OK
        assert find_item_by_source(result.report, source_id_for(item)) is None


class TestTheRecipeIsNeverTouched:
    def test_running_twice_produces_one_report_rather_than_a_doubled_one(self, connection):
        item = ReportItem(heading="Headcount", sql=HEADCOUNT_SQL)
        task = _task(items=[item], report=_report(source_id_for(item)))

        first = _run(connection, task, profile=PROFILE)
        second = _run(connection, task, profile=PROFILE)

        assert len(second.report.sections[0].subsections[0].items) == 1
        assert first.report is not second.report
        assert list(second.report.sections[0].subsections[0].items[0].frame["people"]) == [2, 1]

    def test_the_task_handed_in_keeps_no_rows_from_the_run(self, connection):
        """The report the run fills is a deep copy — the Task in session state is a recipe
        before the run and a recipe after it."""
        item = ReportItem(heading="Headcount", sql=HEADCOUNT_SQL)
        skeleton = _report(source_id_for(item))
        task = _task(items=[item], report=skeleton)

        _run(connection, task, profile=PROFILE)

        assert skeleton.sections[0].subsections[0].items[0].frame is None


class TestOrderIsTheContentOfTheList:
    def test_an_item_below_a_column_step_sees_the_column_it_added(self, connection):
        step = ReportItem(
            kind=KIND_COLUMN,
            heading="Total cost",
            statements=["ALTER TABLE salary ADD COLUMN total DOUBLE", "UPDATE salary SET total = basic + bonus"],
        )
        item = ReportItem(heading="Cost", sql="SELECT sum(total) AS total_cost FROM salary")
        task = _task(items=[step, item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=PROFILE)

        assert [one.status for one in result.steps] == [STATUS_OK, STATUS_OK]
        placed = find_item_by_source(result.report, source_id_for(item))
        assert placed.frame["total_cost"].iloc[0] == pytest.approx(4210.0)

    def test_a_column_step_that_fails_is_reported_and_the_run_carries_on(self, connection):
        """The items that don't depend on it still land, which is why the run doesn't stop."""
        step = ReportItem(kind=KIND_COLUMN, heading="Bad step", statements=["ALTER TABLE salary ADD COLUMN"])
        item = ReportItem(heading="Headcount", sql=HEADCOUNT_SQL)
        task = _task(items=[step, item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=PROFILE)

        assert [one.status for one in result.steps] == [STATUS_FAILED, STATUS_OK]
        assert "couldn't be applied" in result.steps[0].detail

    def test_a_column_step_with_no_recorded_statements_is_skipped_not_failed(self, connection):
        task = _task(items=[ReportItem(kind=KIND_COLUMN, heading="Empty step")])

        result = _run(connection, task, profile=PROFILE)

        assert result.steps[0].status == STATUS_SKIPPED

    def test_statements_owned_by_no_column_step_are_applied_before_the_list(self, connection):
        """A Task can be saved in a session where a column arrived some other way. Dropping
        those would leave every item below missing a column."""
        item = ReportItem(heading="Cost", sql="SELECT sum(total) AS total_cost FROM salary")
        task = _task(items=[item], report=_report(source_id_for(item)))
        task.calculated_columns = [
            "ALTER TABLE salary ADD COLUMN total DOUBLE",
            "UPDATE salary SET total = basic + bonus",
        ]

        result = _run(connection, task, profile=PROFILE)

        assert result.steps[0].label == "Recorded calculated columns"
        assert [one.status for one in result.steps] == [STATUS_OK, STATUS_OK]

    def test_a_statement_a_column_step_already_owns_is_not_applied_twice(self, connection):
        """Otherwise the second application is an `ALTER TABLE ADD` of a column that exists."""
        statements = ["ALTER TABLE salary ADD COLUMN total DOUBLE"]
        step = ReportItem(kind=KIND_COLUMN, heading="Total", statements=statements)
        task = _task(items=[step])
        task.calculated_columns = list(statements)

        assert replay.unowned_statements(task) == []

        result = _run(connection, task, profile=PROFILE)
        assert [one.status for one in result.steps] == [STATUS_OK]


class TestTheFallback:
    def test_a_broken_statement_is_rewritten_and_recorded_as_a_fallback(self, connection, monkeypatch):
        item = ReportItem(heading="Headcount", request="People per department", sql="SELECT nope FROM salary")
        task = _task(items=[item], report=_report(source_id_for(item)))
        _stub(monkeypatch, items_sql, items_sql.GeneratedSql(sql=HEADCOUNT_SQL))

        result = _run(connection, task, profile=PROFILE)

        assert result.steps[0].status == STATUS_FALLBACK
        assert "the AI rewrote it" in result.steps[0].detail
        assert find_item_by_source(result.report, source_id_for(item)).frame is not None

    def test_the_failure_is_fed_back_so_the_rewrite_is_a_refine(self, connection, monkeypatch):
        """The model is sent the previous statement *and* what was wrong with it, so it
        changes the least it can rather than renaming every column in a saved report."""
        prompts = []

        def fake(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
            prompts.append(prompt)
            return items_sql.GeneratedSql(sql=HEADCOUNT_SQL)

        monkeypatch.setattr(items_sql, "run_structured", fake)
        item = ReportItem(heading="Headcount", request="People per department", sql="SELECT nope FROM salary")
        task = _task(items=[item])

        _run(connection, task, profile=PROFILE)

        assert "SELECT nope FROM salary" in prompts[0]
        assert "What was wrong with it" in prompts[0]

    def test_with_no_ai_connection_a_broken_statement_simply_fails(self, connection):
        item = ReportItem(heading="Headcount", sql="SELECT nope FROM salary")
        task = _task(items=[item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=None)

        assert result.steps[0].status == STATUS_FAILED
        assert "no AI connection" in result.steps[0].detail

    def test_a_rewrite_that_also_fails_is_a_failure_naming_both_attempts(self, connection, monkeypatch):
        item = ReportItem(heading="Headcount", request="People", sql="SELECT nope FROM salary")
        task = _task(items=[item])
        _stub(
            monkeypatch,
            items_sql,
            items_sql.GeneratedSql(sql="SELECT still_nope FROM salary"),
            items_sql.GeneratedSql(sql="SELECT hopeless FROM salary"),
        )

        result = _run(connection, task, profile=PROFILE)

        assert result.steps[0].status == STATUS_FAILED
        assert "didn't run" in result.steps[0].detail

    def test_an_item_saved_without_sql_is_skipped_rather_than_sent_to_a_model(self, connection):
        task = _task(items=[ReportItem(heading="Never generated", request="something")])

        result = _run(connection, task, profile=PROFILE)

        assert result.steps[0].status == STATUS_SKIPPED


class TestAFailedItemStaysInTheReport:
    def test_it_keeps_its_place_and_says_it_could_not_be_produced(self, connection):
        """A designed report with a point silently missing is worse than one that admits it."""
        item = ReportItem(heading="Headcount", sql="SELECT nope FROM salary")
        task = _task(items=[item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=None)

        placed = find_item_by_source(result.report, source_id_for(item))
        assert placed is not None
        assert placed.frame is None
        assert placed.comment.startswith("Not produced in this run")

    def test_last_months_comment_is_never_left_over_a_missing_result(self, connection):
        item = ReportItem(heading="Headcount", sql="SELECT nope FROM salary")
        task = _task(items=[item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=None)

        assert "last month's note" not in find_item_by_source(result.report, source_id_for(item)).comment


class TestCriteria:
    def _task_with_check(self, *, heading_suffix="", display_columns=None):
        check = Check(
            name="Bonus within policy",
            criteria_text="Bonus must be at most 5% of basic.",
            sql=BONUS_CHECK_SQL,
            display_columns=list(display_columns or []),
        )
        source = check_source_id_for(check.check_id)
        report = _report(source)
        report.sections[0].subsections[0].items[0].heading = f"{check.display_name()}{heading_suffix}"
        return _task(checks=CheckSet(checks=[check]), report=report), check, source

    def test_a_criteria_runs_from_its_saved_sql_and_lands_in_its_slot(self, connection):
        task, _check, source = self._task_with_check()

        result = _run(connection, task, profile=PROFILE)

        assert result.steps[0].status == STATUS_OK
        assert "1 of 3 record(s) breached" in result.steps[0].detail
        assert len(find_item_by_source(result.report, source).frame) == 3

    def test_an_item_pinned_as_breaches_only_gets_the_breaches_only(self, connection):
        """The mode is nowhere on the `Check` — it is recovered from the heading, which is the
        record of it that every Task already written carries."""
        task, _check, source = self._task_with_check(heading_suffix=" — breaches only")

        result = _run(connection, task, profile=PROFILE)

        frame = find_item_by_source(result.report, source).frame
        assert list(frame["criteria_met"]) == ["No"]
        assert list(frame["employee"]) == ["Bo"]

    def test_the_counts_still_read_the_whole_run_not_the_filtered_view(self, connection):
        """Otherwise saving the Failures view would produce remarks reading "0 breached"."""
        task, _check, _source = self._task_with_check(heading_suffix=" — breaches only")

        result = _run(connection, task, profile=PROFILE)

        assert "1 of 3 record(s) breached" in result.steps[0].detail

    def test_the_saved_column_selection_narrows_the_pinned_table(self, connection):
        task, _check, source = self._task_with_check(display_columns=["employee"])

        result = _run(connection, task, profile=PROFILE)

        assert list(find_item_by_source(result.report, source).frame.columns) == [
            "employee",
            "criteria_result",
            "criteria_met",
        ]

    def test_a_criteria_saved_before_the_column_picker_existed_shows_everything(self, connection):
        task, _check, source = self._task_with_check(display_columns=[])

        result = _run(connection, task, profile=PROFILE)

        assert "department" in find_item_by_source(result.report, source).frame.columns

    def test_the_overview_is_rebuilt_when_the_report_holds_one(self, connection):
        task, _check, _source = self._task_with_check()
        task.report.sections[0].subsections[0].items.append(
            PinnedItem(heading="Overview", source_id=replay.SUMMARY_SOURCE_ID)
        )

        result = _run(connection, task, profile=PROFILE)

        overview = find_item_by_source(result.report, replay.SUMMARY_SOURCE_ID)
        assert overview.frame is not None
        assert overview.figure is not None

    def test_no_overview_is_built_when_the_report_has_no_slot_for_one(self, connection):
        """Work with nowhere to put it. A Task whose author never pressed Save on the
        overview has no item for it."""
        task, _check, _source = self._task_with_check()

        result = _run(connection, task, profile=PROFILE)

        assert [step.kind for step in result.steps] == ["check"]

    def test_the_tasks_persona_is_what_the_criteria_run_under(self, connection):
        """One Task, one voice — not whatever the embedded criteria set was saved under."""
        task, _check, _source = self._task_with_check()
        task.checks.persona = "some other voice"

        _run(connection, task, profile=PROFILE)

        assert task.checks.persona == task.persona


class TestComments:
    def test_the_comment_is_redrafted_for_this_months_numbers(self, connection, monkeypatch):
        item = ReportItem(heading="Headcount", request="People per department", sql=HEADCOUNT_SQL)
        task = _task(items=[item], report=_report(source_id_for(item)))
        _stub(monkeypatch, commentary, commentary.Commentary(summary="Headcount rose in HR."))

        result = _run(connection, task, profile=PROFILE, rewrite_comments=True)

        assert find_item_by_source(result.report, source_id_for(item)).comment == "Headcount rose in HR."

    def test_turning_the_rewrite_off_keeps_the_wording_saved_with_the_task(self, connection):
        """`no_provider` is the assertion: nothing may be asked of a model."""
        item = ReportItem(heading="Headcount", sql=HEADCOUNT_SQL)
        task = _task(items=[item], report=_report(source_id_for(item)))

        result = _run(connection, task, profile=PROFILE, rewrite_comments=False)

        assert find_item_by_source(result.report, source_id_for(item)).comment == "last month's note"

    def test_a_comment_that_cannot_be_written_is_a_note_not_a_failure(self, connection, monkeypatch):
        """The table is the report item; the sentence under it is the part that can be missing."""
        item = ReportItem(heading="Headcount", sql=HEADCOUNT_SQL)
        task = _task(items=[item], report=_report(source_id_for(item)))
        monkeypatch.setattr(
            commentary, "write_commentary", lambda *args, **kwargs: ("", ["The model was unreachable."])
        )

        result = _run(connection, task, profile=PROFILE, rewrite_comments=True)

        assert result.steps[0].status == STATUS_OK
        assert result.steps[0].notes == ["The model was unreachable."]


class TestProgress:
    def test_every_stage_announces_itself(self, connection):
        """Requirement 8.2 step 7 — a screen that says nothing for minutes reads as one that
        has hung."""
        item = ReportItem(heading="Headcount", sql=HEADCOUNT_SQL)
        task = _task(items=[item])
        said = []

        _run(connection, task, profile=PROFILE, on_stage=said.append)

        assert any("Headcount" in message for message in said)
