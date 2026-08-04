from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGIN_PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "login.py")


def _make_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    at = AppTest.from_file(LOGIN_PAGE_PATH, default_timeout=10)
    at.run()
    return at


def test_login_with_correct_credentials_authenticates(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)

    at.text_input(key="login_email").set_value("admin@admin.com")
    at.text_input(key="login_password").set_value("nimda")
    at.button(key="FormSubmitter:login_form-Log in").click().run()

    assert "user_id" in at.session_state
    assert at.session_state["role"] == "superuser"
    assert not at.error


def test_login_with_wrong_password_shows_error(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)

    at.text_input(key="login_email").set_value("admin@admin.com")
    at.text_input(key="login_password").set_value("wrong-password")
    at.button(key="FormSubmitter:login_form-Log in").click().run()

    assert "user_id" not in at.session_state
    assert any("Incorrect email or password" in error.value for error in at.error)
