from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import create_user, init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, list_profiles

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "settings.py")


def _make_app(tmp_path, monkeypatch, role="normal_user"):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    at = AppTest.from_file(SETTINGS_PAGE_PATH, default_timeout=10)
    at.session_state["user_id"] = 1
    at.session_state["email"] = "admin@admin.com"
    at.session_state["role"] = role
    at.run()
    return at


def test_any_role_can_access_settings_page(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch, role="normal_user")

    assert not at.exception
    assert at.title[0].value == "Settings"


def test_profile_section_shows_by_default(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)

    assert not at.exception
    assert at.text_input(key="settings_profile_name")


def test_switching_to_password_section_shows_password_fields(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)

    at.segmented_control(key="settings_section").set_value("Password")
    at.run()

    assert not at.exception
    assert at.text_input(key="settings_current_password")
    assert at.text_input(key="settings_new_password")
    assert at.text_input(key="settings_confirm_password")


def test_llm_section_lists_only_own_profiles(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)
    db_path = Path("data") / "tikitarai.db"
    create_user("someone@example.com", "Someone", "password123", "normal_user", db_path)
    create_profile(1, "My profile", "local", "http://localhost:1234", None, "llama-3", db_path=db_path)
    create_profile(2, "Someone else's profile", "local", "http://localhost:5678", None, "phi-3", db_path=db_path)

    at.segmented_control(key="settings_section").set_value("LLM providers")
    at.run()

    assert not at.exception
    assert len(at.dataframe) == 1
    assert list(at.dataframe[0].value["nickname"]) == ["My profile"]


def test_set_light_model_button_does_not_crash(tmp_path, monkeypatch):
    """Regression test for a reported crash: clicking "Set as light model" raised
    StreamlitAPIException("...cannot be modified after the widget...is instantiated")
    because the old reset helper wrote directly to session_state["settings_llm_table"]
    from a handler that runs after that widget was already instantiated earlier in the
    same run. The fix queues the reset and applies it before the widget is (re)created
    on the next run."""
    at = _make_app(tmp_path, monkeypatch)
    db_path = Path("data") / "tikitarai.db"
    created = create_profile(1, "Local LM Studio", "local", "http://localhost:1234", None, "llama-3", db_path=db_path)

    at.segmented_control(key="settings_section").set_value("LLM providers")
    at.run()
    row_selection = {"selection": {"rows": [0], "columns": []}}
    at.session_state["settings_llm_table"] = row_selection
    at.run()

    button = at.button(key="settings_set_light_button")
    button.click()
    # A real browser keeps the row visually selected while the click's rerun happens;
    # AppTest doesn't auto-persist dataframe selection across a different widget's
    # rerun, so it's re-asserted here to faithfully simulate that.
    at.session_state["settings_llm_table"] = row_selection
    at.run()

    assert not at.exception
    profiles = list_profiles(1, db_path)
    assert profiles[0]["is_light_model"] == 1
