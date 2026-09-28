"""Adding an invitee after creation and the FAQ template download (phase 55)."""

from io import BytesIO
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import init_llm_table
from meetings import access, faq, loops, tables
from meetings import db as meetings_db
from meetings.model import AgendaItem, Meeting

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATOR_PAGE = str(PROJECT_ROOT / "app_pages" / "meetings.py")


def _setup(tmp_path, monkeypatch, with_invitee=True) -> int:
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    meeting = meetings_db.create_meeting(1, Meeting(subject="Hiring", agenda=[AgendaItem(item="Salary")]))
    if with_invitee:
        meetings_db.add_invitee(meeting.meeting_id, 1, "Raj", "raj@x.com", "token-raj", access.encrypt_code("123456"))
    return meeting.meeting_id


def _organiser_app(meeting_id):
    app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.session_state["meeting_open_id"] = meeting_id
    return app


def _add(app, meeting_id, name, email):
    app.text_input(key=f"meetings_add_invitee_name_{meeting_id}").set_value(name)
    app.text_input(key=f"meetings_add_invitee_email_{meeting_id}").set_value(email)
    app.button(key=f"meetings_add_invitee_submit_{meeting_id}").click().run()
    assert not app.exception
    return app


class TestAddInvitee:
    def test_a_new_invitee_gets_a_working_code(self, tmp_path, monkeypatch):
        meeting_id = _setup(tmp_path, monkeypatch)
        app = _add(_organiser_app(meeting_id).run(), meeting_id, "Priya", "priya@x.com")

        invitees = meetings_db.list_invitees(meeting_id, 1)
        assert [(row["name"], row["email"]) for row in invitees] == [("Raj", "raj@x.com"), ("Priya", "priya@x.com")]
        assert len(access.decrypt_code(invitees[1]["access_code_enc"])) > 0
        assert meetings_db.find_contact("priya@x.com")["name"] == "Priya"
        assert "Added Priya" in " ".join(element.value for element in app.success)

    def test_a_duplicate_email_is_refused(self, tmp_path, monkeypatch):
        meeting_id = _setup(tmp_path, monkeypatch)
        app = _add(_organiser_app(meeting_id).run(), meeting_id, "Raj again", "RAJ@x.com")

        assert len(meetings_db.list_invitees(meeting_id, 1)) == 1
        assert "already invited" in " ".join(element.value for element in app.warning)

    def test_a_bad_email_is_refused(self, tmp_path, monkeypatch):
        meeting_id = _setup(tmp_path, monkeypatch)
        app = _add(_organiser_app(meeting_id).run(), meeting_id, "Nobody", "not-an-email")

        assert len(meetings_db.list_invitees(meeting_id, 1)) == 1
        assert "valid email" in " ".join(element.value for element in app.warning)

    def test_a_meeting_without_invitees_can_gain_one(self, tmp_path, monkeypatch):
        meeting_id = _setup(tmp_path, monkeypatch, with_invitee=False)
        _add(_organiser_app(meeting_id).run(), meeting_id, "", "sam@x.com")

        assert [(row["name"], row["email"]) for row in meetings_db.list_invitees(meeting_id, 1)] == [
            ("sam@x.com", "sam@x.com")
        ]


class TestFaqTemplate:
    def test_the_template_reads_back_as_a_faq(self):
        payload = loops.to_excel_bytes(faq.template_frame(), "FAQ")
        frame = tables.read_source(payload, "FAQ template.xlsx")

        assert list(frame.columns) == [faq.QUESTION_COLUMN, faq.ANSWER_COLUMN]
        assert faq.guess_columns(list(frame.columns)) == (faq.QUESTION_COLUMN, faq.ANSWER_COLUMN)
        assert len(faq.entries_from_frame(frame, faq.QUESTION_COLUMN, faq.ANSWER_COLUMN)) == 2
        assert pd.read_excel(BytesIO(payload)).shape == (2, 2)

    def test_the_faq_box_offers_the_template(self, tmp_path, monkeypatch):
        meeting_id = _setup(tmp_path, monkeypatch)
        app = _organiser_app(meeting_id).run()

        assert not app.exception
        assert any(element.label == "Download template" for element in app.get("download_button"))
