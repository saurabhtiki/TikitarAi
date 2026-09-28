"""FAQ side questions on both pages (phase 53), driven with the model stubbed.

The invitee asks something the FAQ doesn't cover and it is logged; the organiser uploads the
FAQ, answers a logged question into it, and sees the count on the Status tab.
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access
from meetings import db as meetings_db
from meetings.chat_agent import ChatTurnOutput
from meetings.model import SENDER_AI, AgendaItem, Faq, FaqEntry, Meeting

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATOR_PAGE = str(PROJECT_ROOT / "app_pages" / "meetings.py")

PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""

TOKEN = "token-faq"
NOTICE = FaqEntry(question="What is the notice period?", answer="60 days.")
PARKING = "Is parking free?"


def _setup(tmp_path, monkeypatch, with_faq=True):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)

    meeting = meetings_db.create_meeting(1, Meeting(subject="Hiring", agenda=[AgendaItem(item="Salary")]))
    invitee_id = meetings_db.add_invitee(
        meeting.meeting_id, 1, "Raj", "raj@x.com", TOKEN, access.encrypt_code("123456")
    )
    if with_faq:
        meetings_db.save_faq(meeting.meeting_id, 1, Faq(source_file="faq.xlsx", entries=[NOTICE]))
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    return meeting, invitee_id, str(probe)


def _invitee_app(probe, meeting_id, invitee_id):
    app = AppTest.from_file(probe, default_timeout=60)
    app.session_state["probe_meeting_id"] = meeting_id
    app.session_state["probe_token"] = TOKEN
    app.session_state["invitee_verified"] = True
    app.session_state["invitee_meeting_id"] = meeting_id
    app.session_state["invitee_id"] = invitee_id
    return app


def _organiser_app(meeting_id):
    app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.session_state["meeting_open_id"] = meeting_id
    return app


def _ask_side_question(monkeypatch, probe, meeting_id, invitee_id) -> dict:
    """The invitee asks about parking; the model (stubbed) says the FAQ doesn't cover it."""
    from meetings import chat_agent

    seen = {}

    def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
        seen["instructions"] = instructions
        return ChatTurnOutput(reply="I'll check with the organiser.", agenda_tag="Salary", unanswered_question=PARKING)

    monkeypatch.setattr(chat_agent, "run_structured", fake_run_structured)
    meetings_db.ensure_session(meeting_id, invitee_id)
    meetings_db.add_message(meeting_id, invitee_id, SENDER_AI, "Welcome! Shall we talk about salary?", "Salary")
    app = _invitee_app(probe, meeting_id, invitee_id).run()
    app.chat_input(key="invitee_chat_input").set_value(PARKING).run()
    assert not app.exception
    return seen


class TestInvitee:
    def test_a_question_the_faq_cannot_answer_is_logged(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        seen = _ask_side_question(monkeypatch, probe, meeting.meeting_id, invitee_id)

        assert "Q: What is the notice period?" in seen["instructions"]
        misses = meetings_db.list_faq_misses(meeting.meeting_id, 1)
        assert [(miss.question, miss.agenda_tag) for miss in misses] == [(PARKING, "Salary")]

    def test_without_a_faq_nothing_is_logged(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, with_faq=False)
        seen = _ask_side_question(monkeypatch, probe, meeting.meeting_id, invitee_id)

        assert "FAQ" not in seen["instructions"]
        assert meetings_db.list_faq_misses(meeting.meeting_id, 1) == []


class TestOrganiser:
    def test_uploading_a_faq_saves_it(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(tmp_path, monkeypatch, with_faq=False)
        app = _organiser_app(meeting.meeting_id).run()
        assert "No FAQ yet" in " ".join(caption.value for caption in app.caption)

        app.file_uploader(key=f"meetings_faq_upload_{meeting.meeting_id}").set_value(
            ("faq.csv", b"Topic,Question,Answer\nHR,Is parking free?,Yes.\nHR,,\n", "text/csv")
        )
        app.run()
        assert app.selectbox(key=f"meetings_faq_question_col_{meeting.meeting_id}").value == "Question"
        assert app.selectbox(key=f"meetings_faq_answer_col_{meeting.meeting_id}").value == "Answer"
        app.button(key=f"meetings_faq_save_{meeting.meeting_id}").click().run()

        assert meetings_db.load_faq(meeting.meeting_id) == Faq(
            source_file="faq.csv", entries=[FaqEntry(question=PARKING, answer="Yes.")]
        )

    def test_an_answered_question_moves_into_the_faq(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch)
        meetings_db.add_faq_miss(meeting.meeting_id, invitee_id, PARKING, "Salary")
        meetings_db.add_faq_miss(meeting.meeting_id, invitee_id, "Is there a canteen?", "Salary")

        app = _organiser_app(meeting.meeting_id).run()
        app.session_state[f"meetings_faq_misses_{meeting.meeting_id}"] = {
            "edited_rows": {0: {"Answer": "Yes, for all staff."}},
            "added_rows": [],
            "deleted_rows": [],
        }
        app.button(key=f"meetings_faq_add_{meeting.meeting_id}").click().run()

        assert meetings_db.load_faq(meeting.meeting_id).entries == [
            NOTICE,
            FaqEntry(question=PARKING, answer="Yes, for all staff."),
        ]
        assert [miss.question for miss in meetings_db.list_faq_misses(meeting.meeting_id, 1)] == ["Is there a canteen?"]

    def test_the_status_tab_counts_unanswered_questions(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_faq_miss(meeting.meeting_id, invitee_id, PARKING, "Salary")

        app = _organiser_app(meeting.meeting_id).run()
        captions = " ".join(caption.value for caption in app.caption)
        assert "1 question(s) the bot couldn't answer — see Overview → FAQ" in captions
        assert any(element.label == "Download Excel" for element in app.get("download_button"))
