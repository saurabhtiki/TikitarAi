"""Phase 61: templates carry their own FAQ — built-in, New meeting table, Save as template."""

import sqlite3
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import db as meetings_db
from meetings import session
from meetings.model import AgendaItem, Faq, FaqEntry, Meeting
from meetings.templates import TEMPLATES, MeetingTemplate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "meetings.py")


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()


def _app(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
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


class TestBuiltInTemplates:
    def test_every_template_has_a_filled_faq(self):
        for template in TEMPLATES:
            assert template.faq, f"{template.name} has no FAQ"
            assert all(entry.question.strip() and entry.answer.strip() for entry in template.faq)


class TestTemplateStorage:
    def test_a_saved_template_keeps_its_faq(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        template = MeetingTemplate(
            name="Mine", description="", meeting_context="", persona="", context_sop="",
            agenda=[AgendaItem(item="Welcome")],
            faq=[FaqEntry(question="Is parking free?", answer="Yes.")],
        )
        meetings_db.save_template(1, template)
        saved = meetings_db.list_templates(1)[0]
        assert saved.faq == template.faq
        assert "1 FAQ row(s)" in saved.description

    def test_an_old_templates_table_gains_the_faq_column(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        db_path = Path(meetings_db.DEFAULT_DB_PATH)
        with sqlite3.connect(db_path) as connection:
            connection.execute("DROP TABLE meeting_templates;")
            connection.execute(
                "CREATE TABLE meeting_templates (template_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
                "name TEXT NOT NULL COLLATE NOCASE, meeting_context TEXT NOT NULL DEFAULT '', "
                "persona TEXT NOT NULL DEFAULT '', context_sop TEXT NOT NULL DEFAULT '', "
                "agenda_json TEXT NOT NULL DEFAULT '{}', evaluation_json TEXT NOT NULL DEFAULT '[]', "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE (user_id, name));"
            )
            connection.execute("INSERT INTO meeting_templates (user_id, name) VALUES (1, 'Old');")
        meetings_db.init_meetings_tables()
        assert meetings_db.list_templates(1)[0].faq == []


class TestNewMeetingFaq:
    def test_use_template_fills_the_faq_and_create_saves_it(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.selectbox(key="meetings_new_template").set_value("HR interview")
        _keep_open(app)
        app.button(key="meetings_new_template_apply").click()
        _keep_open(app)
        hr = next(template for template in TEMPLATES if template.name == "HR interview")
        shown = app.session_state["meetings_new_faq_rows"]
        assert list(shown["Question"]) == [entry.question for entry in hr.faq]

        app.text_input(key="meetings_new_subject").set_value("Interview Asha")
        _keep_open(app)
        app.button(key="meetings_new_save").click()
        _keep_open(app)

        meeting_id = meetings_db.list_meetings(1)[0]["meeting_id"]
        saved = meetings_db.load_faq(meeting_id)
        assert saved.entries == hr.faq
        assert saved.source_file == "Template: HR interview"

    def test_an_empty_faq_table_means_no_faq(self, tmp_path, monkeypatch):
        app = _open_dialog(_app(tmp_path, monkeypatch))
        app.text_input(key="meetings_new_subject").set_value("PO 123")
        _keep_open(app)
        app.button(key="meetings_new_save").click()
        _keep_open(app)
        meeting_id = meetings_db.list_meetings(1)[0]["meeting_id"]
        assert meetings_db.load_faq(meeting_id) is None

    def test_new_faq_entries_skips_blank_rows(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("meetings_page_61", PAGE_PATH)
        page = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(page)
        blank = pd.DataFrame([{"Question": None, "Answer": None}])
        assert page._new_faq_entries(blank) == []
        filled = pd.DataFrame([{"Question": "Parking?", "Answer": "Free."}, {"Question": None, "Answer": None}])
        assert page._new_faq_entries(filled) == [FaqEntry(question="Parking?", answer="Free.")]


class TestOverviewSaveAsTemplate:
    def test_the_meetings_faq_goes_into_the_template(self, tmp_path, monkeypatch):
        app = _app(tmp_path, monkeypatch)
        saved = meetings_db.create_meeting(1, Meeting(subject="Vendor", agenda=[AgendaItem(item="Price")]))
        entries = [FaqEntry(question="GST?", answer="Quote before tax.")]
        meetings_db.save_faq(saved.meeting_id, 1, Faq(source_file="x.xlsx", entries=entries))
        app.session_state[session.MEETING_OPEN_ID_KEY] = saved.meeting_id
        app.run()
        assert not app.exception

        key = f"meetings_tpl_{saved.meeting_id}"
        app.text_input(key=f"{key}_name").set_value("Vendor call")
        app.run()
        app.button(key=f"{key}_save").click()
        app.run()
        assert not app.exception
        assert meetings_db.list_templates(1)[0].faq == entries
