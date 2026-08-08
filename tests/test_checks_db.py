"""Saved criteria sets in SQLite (requirement 6.5).

The ownership assertions are the point of this suite: `check_sets` is the first table in
the app that stores something a user authored at length, and a set readable by the wrong
account would be a real leak rather than an inconvenience.
"""

import sqlite3

import pytest

from auth.db import create_user, init_db, seed_default_admin
from checks.db import delete_set, init_check_sets_table, list_sets, load_set, save_set
from checks.exceptions import ChecksStorageError
from checks.model import CheckSet, add_check


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    create_user("second@example.com", "Second", "password123", "normal_user", path)
    init_check_sets_table(path)
    return path


def _set(name="Payroll checks") -> CheckSet:
    check_set = CheckSet(name=name, persona="You are a finance controller.")
    check = add_check(check_set, "Bonus within policy")
    check.criteria_text = "Bonus must be at most 5% of basic."
    check.sql = "SELECT 1"
    return check_set


class TestSaving:
    def test_a_saved_set_comes_back_with_its_criteria(self, db_path):
        saved = save_set(1, _set(), db_path=db_path)
        assert saved.set_id is not None

        loaded = load_set(saved.set_id, 1, db_path=db_path)
        assert loaded.name == "Payroll checks"
        assert loaded.persona == "You are a finance controller."
        assert [check.criteria_text for check in loaded.checks] == ["Bonus must be at most 5% of basic."]

    def test_saving_the_same_name_twice_updates_rather_than_duplicates(self, db_path):
        """Otherwise "Save" on a set the user has already saved silently accumulates
        near-identical sets they then have to tell apart in a dropdown."""
        save_set(1, _set(), db_path=db_path)

        second = _set()
        add_check(second, "PF mismatch")
        save_set(1, second, db_path=db_path)

        rows = list_sets(1, db_path=db_path)
        assert len(rows) == 1
        assert len(load_set(rows[0]["set_id"], 1, db_path=db_path).checks) == 2

    def test_a_blank_name_is_refused(self, db_path):
        with pytest.raises(ChecksStorageError, match="name"):
            save_set(1, CheckSet(name="   "), db_path=db_path)

    def test_the_name_is_trimmed_on_the_way_in(self, db_path):
        assert save_set(1, CheckSet(name="  Payroll  "), db_path=db_path).name == "Payroll"

    def test_two_users_can_each_have_a_set_of_the_same_name(self, db_path):
        """The unique index is per user — one account's naming must not constrain another's."""
        save_set(1, _set(), db_path=db_path)
        save_set(2, _set(), db_path=db_path)
        assert len(list_sets(1, db_path=db_path)) == 1
        assert len(list_sets(2, db_path=db_path)) == 1


class TestOwnership:
    def test_another_users_set_cannot_be_loaded(self, db_path):
        saved = save_set(1, _set(), db_path=db_path)
        with pytest.raises(ChecksStorageError, match="No criteria set"):
            load_set(saved.set_id, 2, db_path=db_path)

    def test_another_users_set_cannot_be_deleted(self, db_path):
        saved = save_set(1, _set(), db_path=db_path)
        with pytest.raises(ChecksStorageError, match="No criteria set"):
            delete_set(saved.set_id, 2, db_path=db_path)
        assert len(list_sets(1, db_path=db_path)) == 1

    def test_listing_shows_only_your_own(self, db_path):
        save_set(1, _set("Mine"), db_path=db_path)
        save_set(2, _set("Theirs"), db_path=db_path)
        assert [row["name"] for row in list_sets(1, db_path=db_path)] == ["Mine"]


class TestListingAndDeleting:
    def test_the_listing_carries_no_json(self, db_path):
        """It feeds a dropdown — loading a dozen full recipes to draw one is work nobody
        asked for."""
        save_set(1, _set(), db_path=db_path)
        assert "checks_json" not in list_sets(1, db_path=db_path)[0]

    def test_deleting_removes_it(self, db_path):
        saved = save_set(1, _set(), db_path=db_path)
        delete_set(saved.set_id, 1, db_path=db_path)
        assert list_sets(1, db_path=db_path) == []

    def test_init_is_safe_to_call_twice(self, db_path):
        init_check_sets_table(db_path)
        init_check_sets_table(db_path)
        assert list_sets(1, db_path=db_path) == []


class TestChatTypeScope:
    """Requirement 6.6: a set saved under a chat type is offered back under that chat type."""

    def test_a_set_saved_under_a_chat_type_is_listed_there(self, db_path):
        save_set(1, _set(), 5, db_path=db_path)
        assert [row["name"] for row in list_sets(1, 5, db_path=db_path)] == ["Payroll checks"]

    def test_it_is_not_listed_under_a_different_chat_type(self, db_path):
        save_set(1, _set(), 5, db_path=db_path)
        assert list_sets(1, 6, db_path=db_path) == []

    def test_it_is_not_listed_among_the_unscoped_sets(self, db_path):
        save_set(1, _set(), 5, db_path=db_path)
        assert list_sets(1, db_path=db_path) == []

    def test_every_scope_shows_them_all(self, db_path):
        # The page's escape hatch, so a set saved before chat types existed is never
        # stranded out of reach.
        save_set(1, _set("Scoped"), 5, db_path=db_path)
        save_set(1, _set("Unscoped"), db_path=db_path)
        assert len(list_sets(1, 5, every_scope=True, db_path=db_path)) == 2

    def test_the_same_name_can_exist_under_two_chat_types(self, db_path):
        first = save_set(1, _set(), 5, db_path=db_path)
        second = save_set(1, _set(), 6, db_path=db_path)
        assert first.set_id != second.set_id

    def test_saving_the_same_name_under_one_chat_type_still_updates(self, db_path):
        first = save_set(1, _set(), 5, db_path=db_path)
        again = save_set(1, _set(), 5, db_path=db_path)
        assert again.set_id == first.set_id

    def test_re_saving_a_loaded_set_adopts_it_into_the_active_chat_type(self, db_path):
        saved = save_set(1, _set(), db_path=db_path)
        loaded = load_set(saved.set_id, 1, db_path=db_path)

        save_set(1, loaded, 5, db_path=db_path)

        assert list_sets(1, db_path=db_path) == []
        assert list_sets(1, 5, db_path=db_path)[0]["set_id"] == saved.set_id

    def test_a_table_created_before_this_column_existed_is_migrated(self, tmp_path):
        # Stage 8 shipped `check_sets` without `chat_type_id`, and there are live rows in
        # `data/tikitarai.db`. The column has to arrive by migration, not only in CREATE.
        path = tmp_path / "legacy.db"
        init_db(path)
        seed_default_admin(path)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE check_sets ("
                "set_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                "name TEXT NOT NULL, checks_json TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
            )
            connection.execute(
                "CREATE UNIQUE INDEX idx_check_sets_user_name ON check_sets (user_id, name COLLATE NOCASE);"
            )
            connection.execute(
                "INSERT INTO check_sets (user_id, name, checks_json) VALUES (1, 'Old set', '{}');"
            )

        init_check_sets_table(path)

        assert [row["name"] for row in list_sets(1, db_path=path)] == ["Old set"]
        # The old index would refuse this — the same name under a different chat type.
        save_set(1, _set("Old set"), 5, db_path=path)
        assert len(list_sets(1, 5, every_scope=True, db_path=path)) == 2
