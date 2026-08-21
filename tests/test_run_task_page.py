"""AppTest coverage for the Run a Task page (requirement 8).

The whole page, driven through real widgets, against a saved Task written by `tasks.db` — so
what is run here is the same JSON Task Builder writes, not a fixture shaped like it.

Four behaviours are worth more than the rest, because each would stay invisible until someone
had done a real month's work and then be expensive to discover:

- **the page is open to an ordinary user**, unlike Task Builder (requirement 8's first line);
- **the upload widgets are mounted on the picker too**, or going back to run a second task
  against the same files arrives with the files gone;
- **a mismatch is remapped rather than aborted** (requirement 8.1 step 5), including the
  renamed-file case the letter of the requirement doesn't name; and
- **the run fills the saved report and lands in its own report**, not the session Dashboard's.
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import create_user, init_db, seed_default_admin
from checks.db import init_check_sets_table
from checks.model import Check, CheckSet
from chat_types.db import init_chat_types_table
from chat_types.model import ChatType, ExpectedColumn, ExpectedTable
from dashboard.model import PinnedItem, Report, Section, Subsection
from engine import session as engine_session
from llm.db import create_profile, init_llm_table
from report_items.model import ReportItem, source_id_for
from tasks.db import init_tasks_table, save_task
from tasks.model import Task

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "run_task.py")

SALARY_CSV = b"employee,department,basic,bonus\nAna,HR,1000,40\nBo,HR,1000,120\nCy,Accounts,2000,90\n"

# The same columns under a different file name and one renamed column — the two mismatches
# requirement 8.1 step 5 is about, in one file.
RENAMED_CSV = b"emp,department,basic,bonus\nAna,HR,1000,40\nBo,HR,1000,120\nCy,Accounts,2000,90\n"

HEADCOUNT_SQL = "SELECT department, count(*) AS people FROM salary GROUP BY department ORDER BY department"

BONUS_SQL = (
    "SELECT employee, bonus AS criteria_result, "
    "CASE WHEN bonus <= basic * 0.05 THEN 'Yes' ELSE 'No' END AS criteria_met FROM salary"
)

TASK_NAME = "Monthly payroll"


def _schema() -> ChatType:
    """What the Task expects — the signature `chat_types.matching` measures against."""
    return ChatType(
        name=TASK_NAME,
        tables=[
            ExpectedTable(
                table_name="salary",
                columns=[
                    ExpectedColumn("employee", "text"),
                    ExpectedColumn("department", "categorical"),
                    ExpectedColumn("basic", "numeric"),
                    ExpectedColumn("bonus", "numeric"),
                ],
            )
        ],
    )


def _task(*, with_check=True, name=TASK_NAME) -> Task:
    item = ReportItem(heading="Headcount", request="People per department", sql=HEADCOUNT_SQL)
    check = Check(name="Bonus within policy", criteria_text="Bonus at most 5% of basic.", sql=BONUS_SQL)

    subsection = Subsection(name="Findings")
    subsection.items.append(
        PinnedItem(heading="Headcount", question="Headcount", source_id=source_id_for(item))
    )
    if with_check:
        subsection.items.append(
            PinnedItem(
                heading=check.display_name(),
                question=check.display_name(),
                source_id=f"check:{check.check_id}",
            )
        )
    section = Section(name="Payroll")
    section.subsections = [subsection]
    report = Report(title="Monthly payroll")
    report.sections = [section]

    return Task(
        name=name,
        description="Run after payroll closes.",
        persona="You are a payroll controller.",
        schema=_schema(),
        report_items=[item],
        checks=CheckSet(checks=[check] if with_check else []),
        report=report,
    )


def _app(tmp_path, monkeypatch, role="normal_user", task: Task | None = None):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    init_chat_types_table()
    init_check_sets_table()
    init_tasks_table()
    create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")

    saved = save_task(1, task if task is not None else _task())

    app = AppTest.from_file(PAGE_PATH, default_timeout=120)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = role
    app.run()
    return app, saved.task_id


def _open(app, task_id):
    app.button(key=f"rt_open_{task_id}").click().run()
    assert not app.exception
    return app


def _upload(app, files):
    app.file_uploader(key=engine_session.DE_UPLOADER_KEY).set_value(files)
    app.run()
    assert not app.exception
    return app


def _loaded(tmp_path, monkeypatch, files=None, task=None):
    """A run screen with this month's file uploaded and matching."""
    app, task_id = _app(tmp_path, monkeypatch, task=task)
    _open(app, task_id)
    _upload(app, files or [("salary.csv", SALARY_CSV, "text/csv")])
    return app, task_id


def _texts(app):
    return " ".join(
        element.value
        for group in (app.markdown, app.success, app.error, app.warning, app.info, app.caption)
        for element in group
        if isinstance(element.value, str)
    )


class TestWhoMayRunOne:
    def test_an_ordinary_user_gets_the_page(self, tmp_path, monkeypatch):
        """Requirement 8's first line. Task Builder is the admin half of this pair."""
        app, task_id = _app(tmp_path, monkeypatch, role="normal_user")

        assert not app.exception
        assert [button for button in app.button if button.key == f"rt_open_{task_id}"]

    def test_only_this_account_s_tasks_are_listed(self, tmp_path, monkeypatch):
        app, task_id = _app(tmp_path, monkeypatch)
        create_user("other@example.com", "Other", "Passw0rd!x", "normal_user")

        app.session_state["user_id"] = 2
        app.run()

        assert not [button for button in app.button if button.key == f"rt_open_{task_id}"]


class TestThePicker:
    def test_show_schema_lists_the_files_and_columns_to_have_ready(self, tmp_path, monkeypatch):
        """Requirement 8.1 step 2 — asked before anything is uploaded, which is the point."""
        app, task_id = _app(tmp_path, monkeypatch)

        app.button(key=f"rt_schema_{task_id}").click().run()

        assert "salary" in _texts(app)
        assert [frame for frame in app.dataframe]

    def test_the_search_box_keeps_only_the_tasks_that_match_every_word(self, tmp_path, monkeypatch):
        """An account can hold hundreds of Tasks, so the list is a search before it is a list."""
        app, task_id = _app(tmp_path, monkeypatch)
        other = save_task(1, _task(name="Quarterly stock count"))
        app.run()

        app.text_input(key="rt_search").set_value("stock count").run()

        assert not app.exception
        assert [button for button in app.button if button.key == f"rt_open_{other.task_id}"]
        assert not [button for button in app.button if button.key == f"rt_open_{task_id}"]

    def test_a_long_list_is_cut_short_until_show_more_is_pressed(self, tmp_path, monkeypatch):
        """Every row is three widgets Streamlit rebuilds on every rerun; the batch caps that."""
        app, _task_id = _app(tmp_path, monkeypatch)
        for number in range(30):
            save_task(1, _task(name=f"Task {number:02d}"))
        app.run()

        drawn = len([button for button in app.button if button.key.startswith("rt_open_")])
        assert drawn == 25

        app.button(key="rt_show_more").click().run()

        assert not app.exception
        assert len([button for button in app.button if button.key.startswith("rt_open_")]) == 31

    def test_the_upload_widgets_are_mounted_on_the_picker_too(self, tmp_path, monkeypatch):
        """A widget that stops being rendered stops reporting its value, and `sync_tables`
        would then drop every loaded table on the way back to pick a second task."""
        app, _task_id = _app(tmp_path, monkeypatch)

        assert app.file_uploader(key=engine_session.DE_UPLOADER_KEY) is not None

    def test_files_survive_going_back_to_the_picker(self, tmp_path, monkeypatch):
        app, _task_id = _loaded(tmp_path, monkeypatch)

        app.button(key="rt_switch_task").click().run()

        assert not app.exception
        assert engine_session.DE_TABLES_KEY in app.session_state
        assert len(app.session_state[engine_session.DE_TABLES_KEY]) == 1


class TestMatching:
    def test_a_matching_upload_says_so_and_offers_the_run(self, tmp_path, monkeypatch):
        app, _task_id = _loaded(tmp_path, monkeypatch)

        assert [button for button in app.button if button.key == "rt_run"]
        assert any("matched" in message.value for message in app.success)

    def test_a_renamed_file_is_a_missing_table_and_the_run_is_withheld(self, tmp_path, monkeypatch):
        app, task_id = _app(tmp_path, monkeypatch)
        _open(app, task_id)
        _upload(app, [("salary_august.csv", SALARY_CSV, "text/csv")])

        assert not [button for button in app.button if button.key == "rt_run"]
        assert "no uploaded file matches this table" in _texts(app)

    def test_the_saved_types_are_applied_as_the_file_is_read(self, tmp_path, monkeypatch):
        """The whole reason the recipe's types go in *with* the files: `department` is saved
        as a category, and detection alone would not have said so."""
        app, _task_id = _loaded(tmp_path, monkeypatch)

        table = next(iter(app.session_state[engine_session.DE_TABLES_KEY].values()))
        assert table.semantic_types["department"] == "categorical"


class TestRemapping:
    """Requirement 8.1 step 5: fix it by hand rather than abort the whole run."""

    def test_a_renamed_file_is_mapped_onto_the_task_s_table(self, tmp_path, monkeypatch):
        app, task_id = _app(tmp_path, monkeypatch)
        _open(app, task_id)
        _upload(app, [("salary_august.csv", SALARY_CSV, "text/csv")])

        picker = next(box for box in app.selectbox if box.key.startswith("rt_map_table_"))
        picker.set_value("salary").run()

        assert not app.exception
        assert any("matched" in message.value for message in app.success)
        assert [button for button in app.button if button.key == "rt_run"]

    def test_a_renamed_column_is_mapped_and_the_file_is_read_again_under_it(self, tmp_path, monkeypatch):
        app, task_id = _app(tmp_path, monkeypatch)
        _open(app, task_id)
        _upload(app, [("salary.csv", RENAMED_CSV, "text/csv")])

        assert "**employee** is missing" in _texts(app)

        picker = app.selectbox(key="rt_map_column_salary_employee")
        picker.set_value("emp").run()

        assert not app.exception
        table = next(iter(app.session_state[engine_session.DE_TABLES_KEY].values()))
        assert "employee" in table.semantic_types
        assert "emp" not in table.semantic_types

    def test_a_mapping_that_worked_is_still_visible_and_can_be_taken_back(self, tmp_path, monkeypatch):
        """A working remap takes its own dropdown off the screen — the column stops being
        missing — so without this there would be no way to see or undo it."""
        app, task_id = _app(tmp_path, monkeypatch)
        _open(app, task_id)
        _upload(app, [("salary.csv", RENAMED_CSV, "text/csv")])
        app.selectbox(key="rt_map_column_salary_employee").set_value("emp").run()

        assert "Mapping in effect" in _texts(app)

        app.button(key="rt_clear_mapping").click().run()

        assert not app.exception
        assert "**employee** is missing" in _texts(app)


class TestRunning:
    def test_the_run_fills_the_saved_report_and_summarises_itself(self, tmp_path, monkeypatch):
        app, _task_id = _loaded(tmp_path, monkeypatch)
        app.checkbox(key="rt_rewrite_comments").set_value(False).run()

        app.button(key="rt_run").click().run()

        assert not app.exception
        report = app.session_state["rt_report"]
        placed = report.sections[0].subsections[0].items[0]
        assert placed.frame is not None
        assert list(placed.frame["department"]) == ["Accounts", "HR"]
        assert "ran from the saved recipe" in _texts(app)

    def test_the_run_lands_in_its_own_report_not_the_session_dashboard(self, tmp_path, monkeypatch):
        """The whole reason `dashboard.session` has an active report key."""
        app, _task_id = _loaded(tmp_path, monkeypatch)
        app.checkbox(key="rt_rewrite_comments").set_value(False).run()

        app.button(key="rt_run").click().run()

        assert "rt_report" in app.session_state
        assert "db_report" not in app.session_state

    def test_the_criteria_lands_beside_the_report_item(self, tmp_path, monkeypatch):
        app, _task_id = _loaded(tmp_path, monkeypatch)
        app.checkbox(key="rt_rewrite_comments").set_value(False).run()

        app.button(key="rt_run").click().run()

        report = app.session_state["rt_report"]
        assert all(item.frame is not None for item in report.sections[0].subsections[0].items)

    def test_running_twice_leaves_one_report_rather_than_two(self, tmp_path, monkeypatch):
        app, _task_id = _loaded(tmp_path, monkeypatch)
        app.checkbox(key="rt_rewrite_comments").set_value(False).run()

        app.button(key="rt_run").click().run()
        app.button(key="rt_run").click().run()

        assert not app.exception
        assert len(app.session_state["rt_report"].sections[0].subsections[0].items) == 2

    def test_the_task_s_calculated_columns_are_left_for_a_later_rebuild_to_replay(
        self, tmp_path, monkeypatch
    ):
        """`relationships.enforce` replays this list on every rebuild, so it has to hold the
        Task's statements once the run has applied them — and exactly once."""
        task = _task()
        task.report_items.insert(
            0,
            ReportItem(
                kind="column",
                heading="Total",
                statements=["ALTER TABLE salary ADD COLUMN total DOUBLE"],
            ),
        )
        task.calculated_columns = ["ALTER TABLE salary ADD COLUMN total DOUBLE"]
        app, _task_id = _loaded(tmp_path, monkeypatch, task=task)
        app.checkbox(key="rt_rewrite_comments").set_value(False).run()

        app.button(key="rt_run").click().run()

        assert not app.exception
        assert app.session_state[engine_session.DE_STATEMENTS_KEY] == task.calculated_columns

    def test_the_report_is_previewed_and_downloadable_without_a_build_view(self, tmp_path, monkeypatch):
        """A run's arrangement came from the Task, and the next press of Run replaces it —
        so there is nothing here to file items into."""
        app, _task_id = _loaded(tmp_path, monkeypatch)
        app.checkbox(key="rt_rewrite_comments").set_value(False).run()
        app.button(key="rt_run").click().run()

        control = app.segmented_control(key="rt_output_view")
        assert control.options == ["Preview", "Download"]


class TestStartOver:
    def test_it_clears_the_run_but_not_the_saved_task(self, tmp_path, monkeypatch):
        app, task_id = _loaded(tmp_path, monkeypatch)
        app.checkbox(key="rt_rewrite_comments").set_value(False).run()
        app.button(key="rt_run").click().run()

        app.button(key="rt_start_over_button").click().run()

        assert not app.exception
        assert "rt_report" not in app.session_state
        # Back at the picker, with the task still there to run again.
        assert [button for button in app.button if button.key == f"rt_open_{task_id}"]
