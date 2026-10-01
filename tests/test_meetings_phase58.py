"""Phase 58: no extra blank agenda row, optional invitees, own templates, strict side answers."""

import importlib.util
from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import db as meetings_db
from meetings.chat_agent import system_instructions
from meetings.model import QUESTION_ITEM, AgendaItem, EvaluationField, Faq, FaqEntry, Meeting
from meetings.templates import MeetingTemplate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "meetings.py")


def _page_module():
    spec = importlib.util.spec_from_file_location("meetings_page_under_test", PAGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
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
    app.button(key="meetings_new_button").click()
    app.run()
    assert not app.exception
    return app


def _template(name="Vendor follow-up", persona="You are the buyer."):
    return MeetingTemplate(
        name=name,
        description="",
        meeting_context="Chase the quote.",
        persona=persona,
        context_sop="Never agree a price.",
        agenda=[AgendaItem(item="Unit price", item_type=QUESTION_ITEM, answer_type="number", rule="> 0")],
        evaluation=[EvaluationField(question="Confidence", buckets=["High", "Low"])],
    )


class TestNoExtraBlankRow:
    def test_an_agenda_with_items_has_no_blank_row(self):
        page = _page_module()
        frame = page._agenda_to_frame([AgendaItem(item="Welcome")])
        assert list(frame["Agenda item"]) == ["Welcome"]

    def test_an_empty_agenda_still_has_one_row_to_type_into(self):
        page = _page_module()
        assert list(page._agenda_to_frame([])["Agenda item"]) == [""]
        assert list(page._evaluation_to_frame([])["Question"]) == [""]
        assert list(page._evaluation_to_frame([EvaluationField(question="Fit")])["Question"]) == ["Fit"]


class TestOptionalInvitees:
    def test_a_meeting_can_be_created_without_invitees(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.text_input(key="meetings_new_subject").set_value("PO 123")
        _keep_open(app)
        app.button(key="meetings_new_save").click()
        _keep_open(app)

        meetings = meetings_db.list_meetings(1)
        assert [row["subject"] for row in meetings] == ["PO 123"]
        assert meetings_db.list_invitees(meetings[0]["meeting_id"], 1) == []


class TestTemplateStorage:
    def test_save_list_update_delete(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_db()
        seed_default_admin()
        meetings_db.init_meetings_tables()

        assert meetings_db.save_template(1, _template()) is False
        saved = meetings_db.list_templates(1)
        assert [template.name for template in saved] == ["Vendor follow-up"]
        assert saved[0].agenda[0].rule == "> 0"
        assert saved[0].evaluation[0].buckets == ["High", "Low"]

        assert meetings_db.save_template(1, _template(name="vendor FOLLOW-up", persona="New")) is True
        again = meetings_db.list_templates(1)
        assert len(again) == 1 and again[0].persona == "New"

        assert meetings_db.list_templates(2) == [], "another user sees none of them"

        meetings_db.delete_template(1, "Vendor follow-up")
        assert meetings_db.list_templates(1) == []


class TestTemplatePage:
    def test_save_as_template_then_use_it(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.text_area(key="meetings_new_persona").set_value("You are the buyer.")
        app.text_input(key="meetings_new_tpl_name").set_value("My vendor call")
        _keep_open(app)
        app.button(key="meetings_new_tpl_save").click()
        _keep_open(app)
        assert [template.name for template in meetings_db.list_templates(1)] == ["My vendor call"]
        assert any("Template 'My vendor call' saved" in success.value for success in app.success)

        _keep_open(app)
        app.text_area(key="meetings_new_persona").set_value("")
        app.selectbox(key="meetings_new_template").set_value("My vendor call")
        _keep_open(app)
        app.button(key="meetings_new_template_apply").click()
        _keep_open(app)
        assert app.text_area(key="meetings_new_persona").value == "You are the buyer."

    def test_an_existing_name_says_it_will_update(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        meetings_db.save_template(1, _template())
        app.text_input(key="meetings_new_tpl_name").set_value("Vendor follow-up")
        _keep_open(app)
        assert app.button(key="meetings_new_tpl_save").label == "Update template"

    def test_a_ready_made_name_is_refused(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.text_input(key="meetings_new_tpl_name").set_value("hr interview")
        _keep_open(app)
        app.button(key="meetings_new_tpl_save").click()
        _keep_open(app)
        assert meetings_db.list_templates(1) == []
        assert any("ready-made template" in warning.value for warning in app.warning)


class TestStrictSideAnswers:
    def test_without_a_faq_general_knowledge_is_forbidden(self):
        instructions = system_instructions(Meeting(subject="Vendor"))
        assert "Never answer from your own general knowledge" in instructions
        assert "say you don't have that answer" in instructions
        assert "unanswered_question to" not in instructions, "no FAQ means nothing is logged"

    def test_with_a_faq_the_faq_rule_and_the_strict_rule_both_apply(self):
        faq = Faq(entries=[FaqEntry(question="Is parking free?", answer="Yes.")])
        instructions = system_instructions(Meeting(subject="Vendor"), faq=faq)
        assert "answer it only from this FAQ" in instructions
        assert "Never answer from your own general knowledge" in instructions
        assert "unanswered_question to their question" in instructions
