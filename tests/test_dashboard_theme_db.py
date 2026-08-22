"""Saved report themes: one name per account, scoped to the owner."""

import sqlite3

import pytest

from dashboard.custom_style import default_settings
from dashboard.theme_db import (
    MAX_THEMES_PER_USER,
    ThemeStorageError,
    delete_theme,
    init_report_themes_table,
    list_themes,
    load_theme,
    save_theme,
)

OWNER = 1
STRANGER = 2


@pytest.fixture
def db_path(tmp_path):
    """A database with the users the themes hang off, and the themes table itself.

    The users table is created by hand rather than through `auth.db`: this suite is about
    theme storage, and the only thing it needs from `users` is a row for the foreign key to
    point at.
    """
    path = tmp_path / "themes.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY AUTOINCREMENT);")
    connection.executemany("INSERT INTO users (user_id) VALUES (?);", [(OWNER,), (STRANGER,)])
    connection.commit()
    connection.close()

    init_report_themes_table(path)
    return path


def test_creating_the_table_twice_is_harmless(db_path):
    init_report_themes_table(db_path)
    assert list_themes(OWNER, db_path) == []


def test_a_saved_theme_comes_back_as_the_settings_that_went_in(db_path):
    settings = default_settings()
    settings.font = "Serif"
    settings = settings.with_element("title", text_colour="#ff0000")

    theme_id = save_theme(OWNER, "Company blue", settings, db_path)
    loaded = load_theme(theme_id, OWNER, db_path)

    assert loaded.font == "Serif"
    assert loaded.element("title").text_colour == "#ff0000"


def test_saving_the_same_name_updates_rather_than_duplicating(db_path):
    first = save_theme(OWNER, "House style", default_settings(), db_path)

    changed = default_settings()
    changed.content_width = 1300
    second = save_theme(OWNER, "house style", changed, db_path)

    assert first == second
    assert len(list_themes(OWNER, db_path)) == 1
    assert load_theme(first, OWNER, db_path).content_width == 1300


def test_a_blank_name_is_refused(db_path):
    with pytest.raises(ThemeStorageError):
        save_theme(OWNER, "   ", default_settings(), db_path)


def test_one_account_cannot_read_or_delete_anothers_theme(db_path):
    theme_id = save_theme(OWNER, "Mine", default_settings(), db_path)

    assert list_themes(STRANGER, db_path) == []
    with pytest.raises(ThemeStorageError):
        load_theme(theme_id, STRANGER, db_path)
    with pytest.raises(ThemeStorageError):
        delete_theme(theme_id, STRANGER, db_path)


def test_a_deleted_theme_is_gone(db_path):
    theme_id = save_theme(OWNER, "Temporary", default_settings(), db_path)
    delete_theme(theme_id, OWNER, db_path)

    assert list_themes(OWNER, db_path) == []
    with pytest.raises(ThemeStorageError):
        load_theme(theme_id, OWNER, db_path)


def test_the_shelf_is_capped_but_re_saving_an_existing_name_never_is(db_path):
    for number in range(MAX_THEMES_PER_USER):
        save_theme(OWNER, f"Theme {number}", default_settings(), db_path)

    with pytest.raises(ThemeStorageError):
        save_theme(OWNER, "One too many", default_settings(), db_path)

    # Already on the shelf, so it is an update and the cap does not apply.
    save_theme(OWNER, "Theme 0", default_settings(), db_path)
