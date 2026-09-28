"""Question steps on both pages (phase 50), driven with the model stubbed.

The invitee page goes through a probe script, as in `test_meeting_invitee_page.py`. What is
checked is the promise the feature makes: a wrong answer does not move the meeting on, a
right one does, and running out of tries moves on without losing what was said.
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access
from meetings import db as meetings_db
from meetings.chat_agent import QUESTION_KIND, AnswerReading, ChatTurnOutput
from meetings.model import (
    ANSWER_NUMBER,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    SENDER_AI,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    STEP_PENDING,
    AgendaItem,
    Meeting,
    StepAnswer,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATOR_PAGE = str(PROJECT_ROOT / "app_pages" / "meetings.py")

PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""

TOKEN = "token-abc"
SALARY = "Expected salary"
RELOCATE = "Willing to relocate?"


def _setup(tmp_path, monkeypatch, agenda=None):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)

    meeting = meetings_db.create_meeting(
        1,
        Meeting(
            subject="Interview",
            persona="You are the HR Manager.",
            agenda=agenda
            or [
                AgendaItem(
                    item=SALARY,
                    item_type=QUESTION_ITEM,
                    answer_type=ANSWER_NUMBER,
                    rule="between 20000 and 60000",
                    max_tries=2,
                ),
                AgendaItem(item=RELOCATE, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO),
            ],
        ),
    )
    invitee_id = meetings_db.add_invitee(
        meeting.meeting_id, 1, "Raj", "raj@x.com", TOKEN, access.encrypt_code("123456")
    )
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


def _stub_model(monkeypatch, reading: AnswerReading) -> dict:
    """Stubs both model calls; returns what the reply call was told."""
    import app_pages.meeting_invitee as page

    seen = {}
    monkeypatch.setattr(page.chat_agent, "read_answer", lambda *a, **k: reading)

    def _reply(*args, **kwargs):
        seen.update(kwargs)
        return ChatTurnOutput(reply="Next.", agenda_tag="")

    monkeypatch.setattr(page.chat_agent, "send_turn", _reply)
    return seen


def _started(tmp_path, monkeypatch):
    meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
    meetings_db.ensure_session(meeting.meeting_id, invitee_id)
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "What salary do you expect?", SALARY)
    return meeting, invitee_id, _invitee_app(probe, meeting.meeting_id, invitee_id)


def _captions(app) -> str:
    return " ".join(caption.value for caption in app.caption)


class TestInvitee:
    def test_the_conversation_opens_on_the_first_question(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        import app_pages.meeting_invitee as page

        seen = {}

        def _open(*args, **kwargs):
            seen.update(kwargs)
            return ChatTurnOutput(reply="Welcome! What salary do you expect?", agenda_tag=kwargs["step_tag"])

        monkeypatch.setattr(page.chat_agent, "opening_message", _open)
        _invitee_app(probe, meeting.meeting_id, invitee_id).run()

        assert seen["step_tag"] == SALARY
        assert "Question 1 of 2" in seen["step_guidance"]
        assert meetings_db.list_messages(meeting.meeting_id, invitee_id)[0].agenda_tag == SALARY

    def test_the_progress_line_shows_which_question(self, tmp_path, monkeypatch):
        _, _, app = _started(tmp_path, monkeypatch)
        app.run()
        assert "Question 1 of 2" in _captions(app)

    def test_a_good_answer_moves_to_the_next_question(self, tmp_path, monkeypatch):
        meeting, invitee_id, app = _started(tmp_path, monkeypatch)
        seen = _stub_model(monkeypatch, AnswerReading(value="45000"))

        app.run()
        app.chat_input(key="invitee_chat_input").set_value("45k per month").run()

        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id)[SALARY]
        assert (saved.status, saved.value) == (STEP_ANSWERED, "45000")
        assert seen["step_tag"] == RELOCATE
        assert '"45000" was accepted' in seen["step_guidance"]
        assert "Question 2 of 2" in _captions(app)

    def test_a_wrong_answer_asks_again_and_uses_a_try(self, tmp_path, monkeypatch):
        meeting, invitee_id, app = _started(tmp_path, monkeypatch)
        seen = _stub_model(monkeypatch, AnswerReading(value="90000"))

        app.run()
        app.chat_input(key="invitee_chat_input").set_value("90k").run()

        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id)[SALARY]
        assert (saved.status, saved.tries) == (STEP_PENDING, 1)
        assert seen["step_tag"] == SALARY
        assert "between 20000 and 60000" in seen["step_guidance"]
        assert "Tries left: 1" in seen["step_guidance"]
        assert "Question 1 of 2" in _captions(app)

    def test_running_out_of_tries_moves_on_and_keeps_what_was_said(self, tmp_path, monkeypatch):
        meeting, invitee_id, app = _started(tmp_path, monkeypatch)
        meetings_db.save_step_answer(
            meeting.meeting_id, invitee_id, StepAnswer(item_ref=SALARY, status=STEP_PENDING, tries=1)
        )
        seen = _stub_model(monkeypatch, AnswerReading(value=""))

        app.run()
        app.chat_input(key="invitee_chat_input").set_value("good money").run()

        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id)[SALARY]
        assert (saved.status, saved.last_reply) == (STEP_NOT_ANSWERED, "good money")
        assert seen["step_tag"] == RELOCATE
        assert "Question 2 of 2" in _captions(app)

    def test_asking_something_instead_uses_no_try(self, tmp_path, monkeypatch):
        meeting, invitee_id, app = _started(tmp_path, monkeypatch)
        seen = _stub_model(monkeypatch, AnswerReading(kind=QUESTION_KIND))

        app.run()
        app.chat_input(key="invitee_chat_input").set_value("Is this role remote?").run()

        assert SALARY not in meetings_db.load_step_answers(meeting.meeting_id, invitee_id)
        assert "asked something instead" in seen["step_guidance"]

    def test_after_the_last_question_the_invitee_is_told_to_close(self, tmp_path, monkeypatch):
        meeting, invitee_id, app = _started(tmp_path, monkeypatch)
        meetings_db.save_step_answer(
            meeting.meeting_id, invitee_id, StepAnswer(item_ref=SALARY, value="45000", status=STEP_ANSWERED, tries=1)
        )
        seen = _stub_model(monkeypatch, AnswerReading(value="Yes"))

        app.run()
        app.chat_input(key="invitee_chat_input").set_value("yes").run()

        assert "Close chat" in seen["step_guidance"]
        assert "Question" not in _captions(app)


class TestOrganiser:
    def _open(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.save_step_answer(
            meeting.meeting_id, invitee_id, StepAnswer(item_ref=SALARY, value="45000", status=STEP_ANSWERED, tries=1)
        )
        meetings_db.save_step_answer(
            meeting.meeting_id,
            invitee_id,
            StepAnswer(item_ref=RELOCATE, status=STEP_NOT_ANSWERED, tries=3, last_reply="depends"),
        )
        app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
        app.session_state["user_id"] = 1
        app.session_state["email"] = "admin@admin.com"
        app.session_state["role"] = "normal_user"
        app.session_state["meeting_open_id"] = meeting.meeting_id
        return app.run()

    def test_the_answers_table_has_a_column_per_question(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)
        frames = [element.value for element in app.dataframe]
        answers = next(frame for frame in frames if SALARY in frame.columns)

        assert answers.iloc[0].to_dict() == {
            "Invitee": "Raj",
            SALARY: "45000",
            RELOCATE: '⚠ Not answered (said: "depends")',
        }

    def test_status_and_overview_describe_the_questions(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        assert "1 of 2 question(s) answered" in _captions(app)
        assert any(
            "question · a number between 20000 and 60000, 2 tries" in element.value for element in app.markdown
        )


class TestBranching:
    """Phase 51: a Go to on the salary question jumps over the relocation question."""

    SHIFT = "Preferred shift"

    def _agenda(self):
        return [
            AgendaItem(
                item=SALARY,
                item_type=QUESTION_ITEM,
                answer_type=ANSWER_NUMBER,
                rule="> 0",
                branch=f"if > 60000 go to {self.SHIFT}",
            ),
            AgendaItem(item=RELOCATE, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO),
            AgendaItem(item=self.SHIFT, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO),
        ]

    def test_an_answer_that_jumps_skips_the_question_in_between(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, self._agenda())
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "What salary?", SALARY)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id)
        seen = _stub_model(monkeypatch, AnswerReading(value="90000"))

        app.run()
        app.chat_input(key="invitee_chat_input").set_value("90k").run()

        assert seen["step_tag"] == self.SHIFT
        assert "Question 2 of 2" in seen["step_guidance"]
        assert "Question 2 of 2" in _captions(app)

    def test_the_organiser_sees_the_skipped_question(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch, self._agenda())
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.save_step_answer(
            meeting.meeting_id, invitee_id, StepAnswer(item_ref=SALARY, value="90000", status=STEP_ANSWERED, tries=1)
        )
        app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
        app.session_state["user_id"] = 1
        app.session_state["email"] = "admin@admin.com"
        app.session_state["role"] = "normal_user"
        app.session_state["meeting_open_id"] = meeting.meeting_id
        app.run()

        answers = next(element.value for element in app.dataframe if SALARY in element.value.columns)
        assert answers.iloc[0][RELOCATE] == "— skipped"
        assert "1 of 2 question(s) answered" in _captions(app)
        assert any(f"go to: if > 60000 go to {self.SHIFT}" in element.value for element in app.markdown)
