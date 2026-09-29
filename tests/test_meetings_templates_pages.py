"""Templates, plain-English drafting and the flow picture on the Meetings page (phase 54).

The model is stubbed at `drafting_agent.run_structured`.
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import db as meetings_db
from meetings.drafting_agent import DraftAgenda, DraftRow
from meetings.model import QUESTION_ITEM, AgendaItem, Meeting

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "meetings.py")


def _app(tmp_path, monkeypatch, with_model=True):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    if with_model:
        profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
        set_default_model(profile["profile_id"], 1)

    app = AppTest.from_file(PAGE_PATH, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    return app


def _open_dialog(app):
    app.run()
    app.button(key="meetings_new_button").click().run()
    assert not app.exception
    return app


def _keep_open(app):
    """AppTest reruns the whole page, which closes a dialog; clicking New meeting again with
    each step keeps it open, as a browser's dialog-only rerun would."""
    app.button(key="meetings_new_button").click()
    app.run()
    assert not app.exception
    return app


def _agenda_titles(app) -> list[str]:
    frame = app.session_state["meetings_agenda_rows"]
    return [title for title in frame["Agenda item"] if title]


class TestTemplates:
    def test_use_template_fills_the_boxes_and_the_grid(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.selectbox(key="meetings_new_template").set_value("HR interview")
        _keep_open(app)
        app.button(key="meetings_new_template_apply").click()
        _keep_open(app)
        assert not app.exception

        assert app.text_area(key="meetings_new_persona").value.startswith("You are the HR recruiter")
        assert "150000" in app.text_area(key="meetings_new_sop").value
        assert _agenda_titles(app)[:2] == ["Expected salary", "Notice period"]
        assert any("Filled in from 'HR interview'" in success.value for success in app.success)
        assert app.get("graphviz_chart"), "the flow picture shows under the grid"

    def test_blank_keeps_the_button_off(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        assert app.button(key="meetings_new_template_apply").disabled


class TestDrafting:
    def test_draft_with_ai_fills_the_grid(self, tmp_path, monkeypatch):
        from meetings import drafting_agent

        seen = {}

        def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
            seen["prompt"] = prompt
            return DraftAgenda(
                rows=[
                    DraftRow(title="Expected salary", answer_type="Number", rule="> 0", go_to="if > 100000 go to End"),
                    DraftRow(title="Notice period", answer_type="Number", rule="between 0 and 180"),
                ]
            )

        monkeypatch.setattr(drafting_agent, "run_structured", fake_run_structured)
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.text_area(key="meetings_new_describe").set_value("Ask salary; if above 1 lakh end.")
        app.button(key="meetings_new_draft").click()
        _keep_open(app)
        assert not app.exception

        assert "Ask salary; if above 1 lakh end." in seen["prompt"]
        assert _agenda_titles(app) == ["Expected salary", "Notice period"]
        assert any("Drafted 2 row(s)" in success.value for success in app.success)

    def test_a_draft_with_a_bad_rule_is_flagged(self, tmp_path, monkeypatch):
        from meetings import drafting_agent

        monkeypatch.setattr(
            drafting_agent,
            "run_structured",
            lambda *args, **kwargs: DraftAgenda(rows=[DraftRow(title="Salary", answer_type="Number", rule="lots")]),
        )
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.text_area(key="meetings_new_describe").set_value("Ask salary.")
        app.button(key="meetings_new_draft").click()
        _keep_open(app)

        assert any("Please fix these before creating" in warning.value for warning in app.warning)

    def test_without_a_model_it_says_so(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch, with_model=False))
        app.text_area(key="meetings_new_describe").set_value("Ask salary.")
        app.button(key="meetings_new_draft").click()
        _keep_open(app)

        assert any("Pick an AI model" in error.value for error in app.error)


class TestOverviewFlow:
    def test_a_meeting_with_questions_shows_the_flow_picture(self, tmp_path, monkeypatch):
        app = _app(tmp_path, monkeypatch)
        meeting = meetings_db.create_meeting(
            1,
            Meeting(
                subject="Hiring",
                agenda=[
                    AgendaItem(item="Salary", item_type=QUESTION_ITEM, answer_type="number", branch="if > 10 go to End"),
                    AgendaItem(item="Experience"),
                ],
            ),
        )
        app.session_state["meeting_open_id"] = meeting.meeting_id
        app.run()

        charts = app.get("graphviz_chart")
        assert charts
        assert "if > 10" in charts[0].proto.spec

    def test_a_meeting_without_questions_has_no_picture(self, tmp_path, monkeypatch):
        app = _app(tmp_path, monkeypatch)
        meeting = meetings_db.create_meeting(1, Meeting(subject="Chat", agenda=[AgendaItem(item="Delivery")]))
        app.session_state["meeting_open_id"] = meeting.meeting_id
        app.run()

        assert not app.get("graphviz_chart")
