from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_MGMT_PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "user_management.py")


def _make_app(tmp_path, monkeypatch, role="superuser"):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    at = AppTest.from_file(USER_MGMT_PAGE_PATH, default_timeout=10)
    at.session_state["user_id"] = 1
    at.session_state["email"] = "admin@admin.com"
    at.session_state["role"] = role
    at.run()
    return at


def test_non_superuser_sees_permission_error(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch, role="admin")

    assert not at.exception
    assert any("permission" in error.value for error in at.error)
    assert len(at.dataframe) == 0


def test_superuser_sees_user_list(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch, role="superuser")

    assert not at.exception
    assert len(at.dataframe) == 1


def test_row_selection_reveals_edit_and_delete_buttons(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch, role="superuser")

    at.session_state["um_users_table"] = {"selection": {"rows": [0], "columns": []}}
    at.run()

    assert not at.exception
    assert at.button(key="um_edit_button")
    assert at.button(key="um_delete_button")
