"""Saved chat types in SQLite (requirement 6.6).

The ownership assertions carry the same weight as `test_checks_db.py`'s: a chat type names
a user's files and columns, and one readable by the wrong account would be a leak rather
than an inconvenience.
"""

import sqlite3

import pytest

from auth.db import create_user, init_db, seed_default_admin
from chat_types.db import delete_type, init_chat_types_table, list_types, load_type, save_type
from chat_types.exceptions import ChatTypeStorageError
from chat_types.model import ChatType, capture
from checks.db import init_check_sets_table, list_sets, save_set
from checks.model import CheckSet, add_check
from engine.dictionary import ColumnEntry
from engine.relationships import Relationship


SEMANTIC_TYPES = {
    "employee": {"emp_id": "id", "joining_date": "date"},
    "salary": {"emp_id": "id", "bonus": "numeric"},
}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    create_user("second@example.com", "Second", "password123", "normal_user", path)
    init_chat_types_table(path)
    init_check_sets_table(path)
    return path


def _chat_type(name="Salary processing") -> ChatType:
    return capture(
        name,
        SEMANTIC_TYPES,
        [Relationship("salary", "emp_id", "employee", "emp_id")],
        [ColumnEntry("salary", "bonus", "DOUBLE", "numeric", description="Monthly bonus")],
    )


class TestSaving:
    def test_a_saved_chat_type_comes_back_whole(self, db_path):
        saved = save_type(1, _chat_type(), db_path=db_path)
        assert saved.chat_type_id is not None

        loaded = load_type(saved.chat_type_id, 1, db_path=db_path)
        assert loaded.name == "Salary processing"
        assert loaded.table_names() == ["employee", "salary"]
        assert loaded.table("employee").types_by_column()["joining_date"] == "date"
        assert loaded.relationships[0].child_table == "salary"
        assert loaded.descriptions[0].description == "Monthly bonus"

    def test_saving_the_same_name_updates_rather_than_duplicating(self, db_path):
        first = save_type(1, _chat_type(), db_path=db_path)
        again = save_type(1, _chat_type(), db_path=db_path)

        assert again.chat_type_id == first.chat_type_id
        assert len(list_types(1, db_path=db_path)) == 1

    def test_the_same_name_in_a_different_case_still_updates_the_one_setup(self, db_path):
        save_type(1, _chat_type("Salary processing"), db_path=db_path)
        save_type(1, _chat_type("SALARY PROCESSING"), db_path=db_path)
        assert len(list_types(1, db_path=db_path)) == 1

    def test_two_accounts_can_each_have_a_setup_of_the_same_name(self, db_path):
        save_type(1, _chat_type(), db_path=db_path)
        save_type(2, _chat_type(), db_path=db_path)
        assert len(list_types(1, db_path=db_path)) == 1
        assert len(list_types(2, db_path=db_path)) == 1

    def test_a_blank_name_is_refused_with_a_reason(self, db_path):
        with pytest.raises(ChatTypeStorageError, match="name"):
            save_type(1, _chat_type("   "), db_path=db_path)

    def test_an_updated_setup_replaces_the_stored_tables(self, db_path):
        saved = save_type(1, _chat_type(), db_path=db_path)
        saved.tables = []
        save_type(1, saved, db_path=db_path)
        assert load_type(saved.chat_type_id, 1, db_path=db_path).tables == []


class TestOwnership:
    def test_another_account_cannot_load_it(self, db_path):
        saved = save_type(1, _chat_type(), db_path=db_path)
        with pytest.raises(ChatTypeStorageError, match="No chat type"):
            load_type(saved.chat_type_id, 2, db_path=db_path)

    def test_another_account_cannot_delete_it(self, db_path):
        saved = save_type(1, _chat_type(), db_path=db_path)
        with pytest.raises(ChatTypeStorageError, match="No chat type"):
            delete_type(saved.chat_type_id, 2, db_path=db_path)
        assert len(list_types(1, db_path=db_path)) == 1

    def test_the_listing_shows_only_the_owners_setups(self, db_path):
        save_type(1, _chat_type("Mine"), db_path=db_path)
        save_type(2, _chat_type("Theirs"), db_path=db_path)
        assert [row["name"] for row in list_types(1, db_path=db_path)] == ["Mine"]


class TestDeleting:
    def test_a_deleted_setup_is_gone(self, db_path):
        saved = save_type(1, _chat_type(), db_path=db_path)
        delete_type(saved.chat_type_id, 1, db_path=db_path)
        assert list_types(1, db_path=db_path) == []

    def test_its_criteria_sets_survive_as_unscoped(self, db_path):
        # A tested set of rules costs far more to rebuild than the setup that owned it.
        saved = save_type(1, _chat_type(), db_path=db_path)
        check_set = CheckSet(name="Payroll checks")
        add_check(check_set, "Bonus within policy")
        save_set(1, check_set, saved.chat_type_id, db_path=db_path)

        assert delete_type(saved.chat_type_id, 1, db_path=db_path) == 1

        unscoped = list_sets(1, db_path=db_path)
        assert [row["name"] for row in unscoped] == ["Payroll checks"]
        assert unscoped[0]["chat_type_id"] is None

    def test_a_name_clash_renames_rather_than_failing_the_delete(self, db_path):
        # `check_sets` is unique on (user, scope, name), so un-scoping a set whose name is
        # already taken among the unscoped ones would violate that index — rolling the
        # whole delete back and leaving the user an error with no way forward.
        saved = save_type(1, _chat_type(), db_path=db_path)
        save_set(1, CheckSet(name="Payroll checks"), db_path=db_path)
        save_set(1, CheckSet(name="Payroll checks"), saved.chat_type_id, db_path=db_path)

        assert delete_type(saved.chat_type_id, 1, db_path=db_path) == 1

        names = sorted(row["name"] for row in list_sets(1, db_path=db_path))
        assert names == ["Payroll checks", "Payroll checks (Salary processing)"]

    def test_sets_that_dont_clash_keep_their_names(self, db_path):
        saved = save_type(1, _chat_type(), db_path=db_path)
        save_set(1, CheckSet(name="Payroll checks"), saved.chat_type_id, db_path=db_path)

        delete_type(saved.chat_type_id, 1, db_path=db_path)

        assert [row["name"] for row in list_sets(1, db_path=db_path)] == ["Payroll checks"]

    def test_another_accounts_sets_are_left_alone(self, db_path):
        mine = save_type(1, _chat_type("Mine"), db_path=db_path)
        theirs = save_type(2, _chat_type("Theirs"), db_path=db_path)
        save_set(2, CheckSet(name="Theirs"), theirs.chat_type_id, db_path=db_path)

        delete_type(mine.chat_type_id, 1, db_path=db_path)

        assert list_sets(2, theirs.chat_type_id, db_path=db_path)[0]["chat_type_id"] == theirs.chat_type_id

    def test_deleting_works_before_the_checks_view_has_ever_been_opened(self, tmp_path):
        # `check_sets` doesn't exist on a database that has never opened Checks, and a
        # missing table there must not block deleting a chat type.
        path = tmp_path / "fresh.db"
        init_db(path)
        seed_default_admin(path)
        init_chat_types_table(path)

        saved = save_type(1, _chat_type(), db_path=path)
        assert delete_type(saved.chat_type_id, 1, db_path=path) == 0


class TestReadingBrokenRows:
    def test_a_corrupted_payload_is_reported_not_raised_raw(self, db_path):
        saved = save_type(1, _chat_type(), db_path=db_path)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE chat_types SET config_json = ? WHERE chat_type_id = ?;",
                ("{not json", saved.chat_type_id),
            )

        with pytest.raises(ChatTypeStorageError, match="valid JSON"):
            load_type(saved.chat_type_id, 1, db_path=db_path)


class TestInitialisation:
    def test_calling_init_twice_is_harmless(self, db_path):
        init_chat_types_table(db_path)
        init_chat_types_table(db_path)
        assert list_types(1, db_path=db_path) == []
