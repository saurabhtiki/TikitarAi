"""Saved Tasks in SQLite (requirement 7.6).

Shaped like `test_checks_db.py`, and the ownership assertions matter for the same reason,
more so here: a Task is the longest thing a user authors in this app — the schema, the
links, the column meanings, every report item and every rule — so one readable by the wrong
account would be a real leak rather than an inconvenience.
"""

import pytest

from auth.db import create_user, init_db, seed_default_admin
from checks.model import CheckSet, add_check
from dashboard.model import Report
from engine.relationships import Relationship
from report_items.model import KIND_COLUMN, add_item
from tasks.db import delete_task, init_tasks_table, list_tasks, load_task, save_task
from tasks.exceptions import TaskStorageError
from tasks.model import Task, capture


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    create_user("second@example.com", "Second", "password123", "normal_user", path)
    init_tasks_table(path)
    return path


def _task(name="Monthly salary review") -> Task:
    items = []
    item = add_item(items, heading="Payroll by department")
    item.request = "Total pay per department"
    item.sql = "SELECT department, sum(basic) AS pay FROM salary GROUP BY department"
    step = add_item(items, KIND_COLUMN, heading="Add tax")
    step.statements = ["ALTER TABLE salary ADD COLUMN tax DOUBLE"]

    check_set = CheckSet(name=name)
    check = add_check(check_set, "Bonus cap")
    check.criteria_text = "Bonus must be at most 5% of basic."

    return capture(
        name,
        description="Run after payroll closes.",
        persona="You are a finance controller.",
        semantic_types_by_table={"salary": {"basic": "numeric"}},
        relationships=[Relationship("salary", "employee", "employee_master", "employee")],
        dictionary=[],
        statements=["ALTER TABLE salary ADD COLUMN tax DOUBLE"],
        report_items=items,
        checks=check_set,
        report=Report(title="Monthly payroll review"),
    )


class TestSaving:
    def test_a_saved_task_comes_back_whole(self, db_path):
        saved = save_task(1, _task(), db_path=db_path)
        assert saved.task_id is not None

        loaded = load_task(saved.task_id, 1, db_path=db_path)

        assert loaded.name == "Monthly salary review"
        assert loaded.description == "Run after payroll closes."
        assert loaded.persona == "You are a finance controller."
        assert loaded.schema.table_names() == ["salary"]
        assert loaded.calculated_columns == ["ALTER TABLE salary ADD COLUMN tax DOUBLE"]
        assert [item.heading for item in loaded.report_items] == ["Payroll by department", "Add tax"]
        assert [check.name for check in loaded.checks.checks] == ["Bonus cap"]
        assert loaded.report.title == "Monthly payroll review"

    def test_saving_under_a_name_already_used_updates_that_task(self, db_path):
        """Re-saving after an edit is the normal way to use this, not an exception to it — so
        the alternative is a unique-index error the user can do nothing useful about."""
        first = save_task(1, _task(), db_path=db_path)

        second = _task()
        second.persona = "You are a payroll auditor."
        save_task(1, second, db_path=db_path)

        assert len(list_tasks(1, db_path=db_path)) == 1
        assert load_task(first.task_id, 1, db_path=db_path).persona == "You are a payroll auditor."

    def test_the_same_name_under_two_accounts_is_two_tasks(self, db_path):
        save_task(1, _task(), db_path=db_path)
        save_task(2, _task(), db_path=db_path)

        assert len(list_tasks(1, db_path=db_path)) == 1
        assert len(list_tasks(2, db_path=db_path)) == 1

    def test_a_blank_name_is_refused_before_anything_is_written(self, db_path):
        with pytest.raises(TaskStorageError, match="name"):
            save_task(1, _task("   "), db_path=db_path)

        assert list_tasks(1, db_path=db_path) == []

    def test_the_returned_task_carries_the_id_it_now_has(self, db_path):
        task = _task()
        saved = save_task(1, task, db_path=db_path)

        assert saved is task
        assert task.task_id is not None


class TestListing:
    def test_the_picker_gets_the_name_and_description_without_the_recipe(self, db_path):
        """Loading a dozen full recipes to draw a dropdown is work nobody asked for."""
        save_task(1, _task(), db_path=db_path)

        rows = list_tasks(1, db_path=db_path)

        assert rows[0]["name"] == "Monthly salary review"
        assert rows[0]["description"] == "Run after payroll closes."
        assert "task_json" not in rows[0]

    def test_an_account_with_no_tasks_gets_an_empty_list(self, db_path):
        assert list_tasks(2, db_path=db_path) == []


class TestOwnership:
    def test_another_account_cannot_load_it(self, db_path):
        saved = save_task(1, _task(), db_path=db_path)

        with pytest.raises(TaskStorageError, match="No task"):
            load_task(saved.task_id, 2, db_path=db_path)

    def test_another_account_cannot_delete_it(self, db_path):
        saved = save_task(1, _task(), db_path=db_path)

        with pytest.raises(TaskStorageError, match="No task"):
            delete_task(saved.task_id, 2, db_path=db_path)

        assert len(list_tasks(1, db_path=db_path)) == 1

    def test_another_accounts_tasks_are_not_listed(self, db_path):
        save_task(1, _task("Mine"), db_path=db_path)

        assert [row["name"] for row in list_tasks(2, db_path=db_path)] == []


class TestDeleting:
    def test_a_deleted_task_is_gone(self, db_path):
        saved = save_task(1, _task(), db_path=db_path)

        delete_task(saved.task_id, 1, db_path=db_path)

        assert list_tasks(1, db_path=db_path) == []

    def test_deleting_one_that_is_not_there_says_so(self, db_path):
        with pytest.raises(TaskStorageError, match="No task"):
            delete_task(999, 1, db_path=db_path)


class TestInitialisation:
    def test_creating_the_table_twice_is_safe(self, tmp_path):
        """Called on every process start, so it has to be."""
        path = tmp_path / "twice.db"
        init_db(path)
        init_tasks_table(path)
        init_tasks_table(path)

        assert list_tasks(1, db_path=path) == []
