"""Phase 56: the meeting's AI model, the FAQ helpers, My meetings search, the invitee side
panel and "finish the questions before closing"."""

from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, delete_profile, init_llm_table, set_default_model, set_light_model
from meetings import access, faq, listing, llm_choice, steps
from meetings import db as meetings_db
from meetings.chat_agent import AnswerReading, ChatTurnOutput
from meetings.exceptions import MeetingStorageError
from meetings.model import (
    ANSWER_NUMBER,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    SENDER_AI,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    AgendaItem,
    Faq,
    FaqEntry,
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
TOKEN = "token-56"
SALARY = "Expected salary"
RELOCATE = "Willing to relocate?"
QUESTIONS = [
    AgendaItem(item=SALARY, item_type=QUESTION_ITEM, answer_type=ANSWER_NUMBER, rule="between 20000 and 60000"),
    AgendaItem(item=RELOCATE, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO),
]


def _setup(tmp_path, monkeypatch, agenda=None, profile_id_choice=None):
    """Two models (default "Local", light "Mini") and one meeting with one invitee."""
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    local = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(local["profile_id"], 1)
    mini = create_profile(1, "Mini", "local", "http://localhost:1234", None, "tiny")
    set_light_model(mini["profile_id"], 1)
    chosen = {"local": local["profile_id"], "mini": mini["profile_id"]}.get(profile_id_choice)

    meeting = meetings_db.create_meeting(
        1, Meeting(subject="Interview", agenda=agenda if agenda is not None else list(QUESTIONS), profile_id=chosen)
    )
    invitee_id = meetings_db.add_invitee(meeting.meeting_id, 1, "Raj", "raj@x.com", TOKEN, access.encrypt_code("123456"))
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    return meeting, invitee_id, str(probe), local, mini


def _organiser_app(meeting_id=None):
    app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    if meeting_id is not None:
        app.session_state["meeting_open_id"] = meeting_id
    return app


def _invitee_app(probe, meeting_id, invitee_id):
    app = AppTest.from_file(probe, default_timeout=60)
    app.session_state["probe_meeting_id"] = meeting_id
    app.session_state["probe_token"] = TOKEN
    app.session_state["invitee_verified"] = True
    app.session_state["invitee_meeting_id"] = meeting_id
    app.session_state["invitee_id"] = invitee_id
    return app


def _started(meeting, invitee_id, probe):
    meetings_db.ensure_session(meeting.meeting_id, invitee_id)
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "What salary do you expect?", SALARY)
    return _invitee_app(probe, meeting.meeting_id, invitee_id)


def _captions(app) -> str:
    return " ".join(caption.value for caption in app.caption)


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


class TestModelChoice:
    PROFILES = [
        {"profile_id": 1, "nickname": "Local", "default_model": "llama-3", "is_default_model": 1, "is_light_model": 0},
        {"profile_id": 2, "nickname": "Mini", "default_model": "tiny", "is_default_model": 0, "is_light_model": 1},
    ]

    def test_labels_mark_light_and_default(self):
        assert llm_choice.profile_label(self.PROFILES[0]) == "Local (llama-3) (default)"
        assert llm_choice.profile_label(self.PROFILES[1]) == "Mini (tiny) (light)"

    def test_the_chosen_model_is_used_even_if_light(self):
        assert llm_choice.resolve(self.PROFILES, 2) == llm_choice.ModelChoice(self.PROFILES[1], fell_back=False)

    def test_no_choice_means_the_default_without_a_warning(self):
        assert llm_choice.resolve(self.PROFILES, None) == llm_choice.ModelChoice(self.PROFILES[0], fell_back=False)

    def test_a_deleted_choice_falls_back_to_the_default_with_a_warning(self):
        assert llm_choice.resolve(self.PROFILES, 99) == llm_choice.ModelChoice(self.PROFILES[0], fell_back=True)


class TestListing:
    ROWS = [
        {"subject": "PO No 123", "invitee_count": 2, "closed_count": 1, "created_at": "2026-09-01 10:00:00"},
        {"subject": "Hiring Raj", "invitee_count": 1, "closed_count": 1, "created_at": "2026-09-02 10:00:00"},
        {"subject": "Draft", "invitee_count": 0, "closed_count": 0, "created_at": "2026-09-03 10:00:00"},
    ]

    def test_search_matches_part_of_the_subject_in_any_case(self):
        assert [row["subject"] for row in listing.filter_meetings(self.ROWS, "po no")] == ["PO No 123"]

    @pytest.mark.parametrize(
        "status, expected",
        [
            (listing.STATUS_ALL, ["PO No 123", "Hiring Raj", "Draft"]),
            (listing.STATUS_WAITING, ["PO No 123"]),
            (listing.STATUS_FINISHED, ["Hiring Raj"]),
            (listing.STATUS_NO_INVITEES, ["Draft"]),
        ],
    )
    def test_status_filter(self, status, expected):
        assert [row["subject"] for row in listing.filter_meetings(self.ROWS, "", status)] == expected

    def test_created_date_is_dd_mm_yyyy(self):
        assert listing.created_on("2026-09-01 10:00:00") == "01-09-2026"


class TestQuestionsLeft:
    MEETING = Meeting(agenda=list(QUESTIONS))

    def test_counts_every_open_question(self):
        assert steps.questions_left(self.MEETING, {}) == 2

    def test_answered_and_not_answered_both_count_as_done(self):
        answers = {
            SALARY: StepAnswer(item_ref=SALARY, value="45000", status=STEP_ANSWERED),
            RELOCATE: StepAnswer(item_ref=RELOCATE, status=STEP_NOT_ANSWERED, tries=3),
        }
        assert steps.questions_left(self.MEETING, answers) == 0

    def test_questions_skipped_by_go_to_do_not_count(self):
        meeting = Meeting(
            agenda=[
                AgendaItem(item=SALARY, item_type=QUESTION_ITEM, answer_type=ANSWER_NUMBER, branch="if > 60000 go to End"),
                AgendaItem(item=RELOCATE, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO),
            ]
        )
        answers = {SALARY: StepAnswer(item_ref=SALARY, value="90000", status=STEP_ANSWERED)}
        assert steps.questions_left(meeting, answers) == 0

    def test_a_meeting_without_questions_has_none_left(self):
        assert steps.questions_left(Meeting(agenda=[AgendaItem(item="Salary")]), {}) == 0


class TestFaqFrames:
    def test_the_table_shows_the_saved_faq(self):
        frame = faq.frame_from_faq(Faq(entries=[FaqEntry("Q1", "A1")]))
        assert frame.to_dict("records") == [{"Question": "Q1", "Answer": "A1"}]
        assert list(faq.frame_from_faq(None).columns) == ["Question", "Answer"]

    def test_an_upload_is_cut_to_question_and_answer(self):
        upload = pd.DataFrame({"Topic": ["HR"], "Question": ["Q1"], "Answer": ["A1"]})
        assert faq.frame_from_upload(upload).to_dict("records") == [{"Question": "Q1", "Answer": "A1"}]

    def test_a_one_column_upload_is_refused(self):
        with pytest.raises(MeetingStorageError):
            faq.frame_from_upload(pd.DataFrame({"Question": ["Q1"]}))

    def test_blank_rows_from_the_table_are_dropped(self):
        frame = pd.DataFrame({"Question": ["Q1", None], "Answer": ["A1", None]})
        assert faq.entries_from_frame(frame, "Question", "Answer") == [FaqEntry("Q1", "A1")]


# --------------------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------------------


class TestOrganiserModel:
    def test_a_new_meeting_saves_the_picked_model(self, tmp_path, monkeypatch):
        meeting, _, _, _, mini = _setup(tmp_path, monkeypatch, profile_id_choice="mini")
        assert meetings_db.load_meeting(meeting.meeting_id, 1).profile_id == mini["profile_id"]

    def test_overview_changes_the_model(self, tmp_path, monkeypatch):
        meeting, _, _, _, mini = _setup(tmp_path, monkeypatch)
        app = _organiser_app(meeting.meeting_id).run()
        app.selectbox(key=f"meetings_model_{meeting.meeting_id}").set_value(mini["profile_id"])
        app.button(key=f"meetings_model_save_{meeting.meeting_id}").click().run()

        assert not app.exception
        assert meetings_db.load_meeting(meeting.meeting_id, 1).profile_id == mini["profile_id"]
        assert "AI model saved." in " ".join(element.value for element in app.success)

    def test_a_deleted_model_shows_a_red_warning(self, tmp_path, monkeypatch):
        meeting, _, _, _, mini = _setup(tmp_path, monkeypatch, profile_id_choice="mini")
        delete_profile(mini["profile_id"], 1)
        app = _organiser_app(meeting.meeting_id).run()

        assert "The chosen AI model was deleted in Settings — using the default (Local (llama-3) (default))." in _captions(app)
        assert llm_choice.meeting_profile(meetings_db.load_meeting(meeting.meeting_id, 1)).profile["nickname"] == "Local"


class TestMyMeetingsSearch:
    def test_search_narrows_the_list(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        meetings_db.create_meeting(1, Meeting(subject="PO No 123"))
        app = _organiser_app().run()
        app.text_input(key="meetings_search").set_value("po no").run()

        assert [markdown.value for markdown in app.markdown if markdown.value.startswith("**")] == ["**PO No 123**"]
        assert "Showing 1 of 2 meeting(s)." in _captions(app)

    def test_no_match_says_so(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        app = _organiser_app().run()
        app.selectbox(key="meetings_status_filter").set_value(listing.STATUS_FINISHED).run()
        assert "No meeting matches this search." in _captions(app)


class TestInviteePage:
    def test_side_panel_has_how_to_agenda_and_attach(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, _, _ = _setup(tmp_path, monkeypatch)
        app = _started(meeting, invitee_id, probe).run()

        assert not app.exception
        # AppTest files an expander that has an icon under "status".
        labels = [block.label for block in app.get("status")]
        assert labels[:3] == ["How to use this meeting", "Agenda", "Attach a file"]
        assert app.file_uploader(key="invitee_upload") is not None

    def test_close_waits_for_the_questions(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, _, _ = _setup(tmp_path, monkeypatch)
        app = _started(meeting, invitee_id, probe).run()
        assert app.button(key="invitee_close_chat").disabled
        assert "2 question(s) left — finish them to close." in _captions(app)

        for title, value in ((SALARY, "45000"), (RELOCATE, "Yes")):
            meetings_db.save_step_answer(
                meeting.meeting_id, invitee_id, StepAnswer(item_ref=title, value=value, status=STEP_ANSWERED)
            )
        app.run()
        assert not app.button(key="invitee_close_chat").disabled

    def test_a_meeting_without_questions_can_close_at_once(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, _, _ = _setup(tmp_path, monkeypatch, agenda=[AgendaItem(item="Salary")])
        app = _started(meeting, invitee_id, probe).run()
        assert not app.button(key="invitee_close_chat").disabled

    def test_the_chat_uses_the_meeting_model_and_notes_it(self, tmp_path, monkeypatch):
        import app_pages.meeting_invitee as page

        meeting, invitee_id, probe, _, _ = _setup(tmp_path, monkeypatch, profile_id_choice="mini")
        used = []
        monkeypatch.setattr(page.chat_agent, "read_answer", lambda profile, *a, **k: AnswerReading(value="45000"))

        def _reply(meeting_arg, profile, *args, **kwargs):
            used.append(profile["nickname"])
            return ChatTurnOutput(reply="Next.", agenda_tag="")

        monkeypatch.setattr(page.chat_agent, "send_turn", _reply)
        app = _started(meeting, invitee_id, probe).run()
        app.chat_input(key="invitee_chat_input").set_value("45k").run()

        assert not app.exception
        assert used == ["Mini"]
        assert meetings_db.list_invitees(meeting.meeting_id, 1)[0]["model_used"] == "Mini (tiny) (light)"

        organiser = _organiser_app(meeting.meeting_id).run()
        assert "model: Mini (tiny) (light)" in _captions(organiser)
