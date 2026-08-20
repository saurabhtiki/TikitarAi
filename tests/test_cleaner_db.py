"""Saved cleaning templates in SQLite.

Shaped like `test_tasks_db.py`, whose `cleaner/db.py` is copied from. The ownership
assertions matter for the same reason: a template names a company's actual files and the
columns inside them, so one readable by the wrong account is a leak rather than an
inconvenience.
"""

import sqlite3

import pytest

from auth.db import create_user, init_db, seed_default_admin
from cleaner.db import (
    delete_template,
    init_cleaning_templates_table,
    list_templates,
    load_template,
    save_template,
)
from cleaner.exceptions import TemplateStorageError
from cleaner.pipeline import make_step
from cleaner.template import CleaningTemplate, TemplateSummary, TemplateTable, capture


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    create_user("second@example.com", "Second", "password123", "normal_user", path)
    init_cleaning_templates_table(path)
    return path


def _template(name="Receivables") -> CleaningTemplate:
    return capture(
        name,
        description="Monthly receivables pack.",
        tables=[
            TemplateTable(
                name="billwise_due",
                file_name="billwise_due.csv",
                output_sheet_name="Billwise due",
                steps=[make_step("trim_whitespace", {"columns": ["customer"]})],
                columns=["customer", "amount"],
            ),
            TemplateTable(name="sales", file_name="sales.csv", output_sheet_name="Sales"),
        ],
        summaries=[
            TemplateSummary(
                parent="billwise_due",
                name="Due by customer",
                reshape=make_step(
                    "group_summarise",
                    {"group_by": ["customer"], "aggregations": [{"column": "amount", "function": "sum"}]},
                ),
            )
        ],
    )


class TestSaving:
    def test_a_saved_template_comes_back_whole(self, db_path):
        saved = save_template(1, _template(), db_path=db_path)
        assert saved.template_id is not None

        restored = load_template(saved.template_id, 1, db_path=db_path)
        assert restored.name == "Receivables"
        assert restored.description == "Monthly receivables pack."
        assert restored.table_names() == ["billwise_due", "sales"]
        assert restored.tables[0].steps[0]["action"] == "trim_whitespace"
        assert restored.summaries[0].name == "Due by customer"

    def test_a_blank_name_is_refused(self, db_path):
        with pytest.raises(TemplateStorageError):
            save_template(1, _template("   "), db_path=db_path)

    def test_saving_under_a_taken_name_updates_that_row(self, db_path):
        first = save_template(1, _template(), db_path=db_path)

        edited = _template()
        edited.tables[1].steps = [make_step("remove_empty_rows", {})]
        second = save_template(1, edited, db_path=db_path)

        assert second.template_id == first.template_id
        assert len(list_templates(1, db_path=db_path)) == 1
        assert load_template(first.template_id, 1, db_path=db_path).tables[1].steps

    def test_the_same_name_under_two_accounts_is_two_templates(self, db_path):
        save_template(1, _template(), db_path=db_path)
        save_template(2, _template(), db_path=db_path)
        assert len(list_templates(1, db_path=db_path)) == 1
        assert len(list_templates(2, db_path=db_path)) == 1


class TestListing:
    def test_the_listing_leaves_the_json_out(self, db_path):
        save_template(1, _template(), db_path=db_path)
        row = list_templates(1, db_path=db_path)[0]
        assert set(row) == {
            "template_id",
            "user_id",
            "name",
            "description",
            "created_at",
            "updated_at",
        }

    def test_the_most_recently_saved_is_first(self, db_path):
        # The picker's whole ordering rests on this. `datetime('now')` has one-second
        # resolution, so three saves in a test land in the same second and tie — the
        # timestamps are set explicitly here to test the ORDER BY rather than the clock.
        older = save_template(1, _template("Payables"), db_path=db_path)
        newer = save_template(1, _template("Receivables"), db_path=db_path)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE cleaning_templates SET updated_at = ? WHERE template_id = ?;",
                ("2020-01-01 00:00:00", older.template_id),
            )
            connection.execute(
                "UPDATE cleaning_templates SET updated_at = ? WHERE template_id = ?;",
                ("2030-01-01 00:00:00", newer.template_id),
            )
        assert [row["name"] for row in list_templates(1, db_path=db_path)] == [
            "Receivables",
            "Payables",
        ]

    def test_an_account_with_nothing_saved_lists_nothing(self, db_path):
        assert list_templates(2, db_path=db_path) == []


class TestOwnership:
    def test_another_account_cannot_load_it(self, db_path):
        saved = save_template(1, _template(), db_path=db_path)
        with pytest.raises(TemplateStorageError):
            load_template(saved.template_id, 2, db_path=db_path)

    def test_another_account_cannot_delete_it(self, db_path):
        saved = save_template(1, _template(), db_path=db_path)
        with pytest.raises(TemplateStorageError):
            delete_template(saved.template_id, 2, db_path=db_path)
        assert list_templates(1, db_path=db_path)

    def test_another_account_cannot_overwrite_it_by_id(self, db_path):
        saved = save_template(1, _template(), db_path=db_path)
        theirs = _template("Something else")
        theirs.template_id = saved.template_id
        with pytest.raises(TemplateStorageError):
            save_template(2, theirs, db_path=db_path)


class TestDeleting:
    def test_a_deleted_template_is_gone(self, db_path):
        saved = save_template(1, _template(), db_path=db_path)
        delete_template(saved.template_id, 1, db_path=db_path)
        assert list_templates(1, db_path=db_path) == []

    def test_deleting_something_that_is_not_there_says_so(self, db_path):
        with pytest.raises(TemplateStorageError):
            delete_template(999, 1, db_path=db_path)


class TestInit:
    def test_creating_the_table_twice_is_safe(self, tmp_path):
        path = tmp_path / "twice.db"
        init_db(path)
        seed_default_admin(path)
        init_cleaning_templates_table(path)
        init_cleaning_templates_table(path)
        assert list_templates(1, db_path=path) == []
