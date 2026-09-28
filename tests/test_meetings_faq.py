"""FAQ side questions (phase 53): reading the upload, the prompt, and storage.

The provider is stubbed at `run_structured`, as in `test_meetings_chat_agent.py`.
"""

import pandas as pd
import pytest

from auth.db import init_db, seed_default_admin
from meetings import access, chat_agent, faq
from meetings import db as meetings_db
from meetings.chat_agent import ChatTurnOutput, opening_message, send_turn, system_instructions
from meetings.exceptions import MeetingStorageError
from meetings.model import Faq, FaqEntry, FaqMiss, Meeting

PROFILE = {"profile_id": 1, "default_model": "test-model"}
NOTICE = FaqEntry(question="What is the notice period?", answer="60 days for managers, 30 for others.")
CREDIT = FaqEntry(question="Who approves credit notes?", answer="The regional finance manager.")


class TestReadingTheUpload:
    def test_columns_named_question_and_answer_are_picked(self):
        assert faq.guess_columns(["No", "Customer question", "Our answer"]) == ("Customer question", "Our answer")

    def test_without_those_names_the_first_two_columns_are_used(self):
        assert faq.guess_columns(["Q", "A", "Owner"]) == ("Q", "A")

    def test_blank_rows_are_dropped(self):
        frame = pd.DataFrame(
            {"Question": ["What is the notice period?", "", "Who approves?"], "Answer": ["60 days.", "x", ""]}
        )
        entries = faq.entries_from_frame(frame, "Question", "Answer")
        assert entries == [FaqEntry(question="What is the notice period?", answer="60 days.")]

    def test_the_same_column_twice_is_refused(self):
        frame = pd.DataFrame({"Question": ["a"], "Answer": ["b"]})
        with pytest.raises(MeetingStorageError, match="two different columns"):
            faq.entries_from_frame(frame, "Question", "Question")

    def test_a_file_with_no_complete_rows_is_refused(self):
        frame = pd.DataFrame({"Question": ["a"], "Answer": [""]})
        with pytest.raises(MeetingStorageError, match="no rows"):
            faq.entries_from_frame(frame, "Question", "Answer")

    def test_too_many_questions_are_refused(self):
        count = faq.MAX_FAQ_ENTRIES + 1
        frame = pd.DataFrame({"Question": [f"Q{index}" for index in range(count)], "Answer": ["A"] * count})
        with pytest.raises(MeetingStorageError, match="more than"):
            faq.entries_from_frame(frame, "Question", "Answer")


class TestAddingAnswers:
    def test_a_new_answer_is_appended_and_a_known_question_is_updated(self):
        current = Faq(source_file="faq.xlsx", entries=[NOTICE])
        updated = faq.add_answers(
            current,
            [
                FaqEntry(question="what is the notice period", answer="90 days now."),
                CREDIT,
            ],
        )
        assert updated.source_file == "faq.xlsx"
        assert [entry.answer for entry in updated.entries] == ["90 days now.", CREDIT.answer]

    def test_answers_can_start_a_faq(self):
        assert faq.add_answers(None, [CREDIT]).entries == [CREDIT]

    def test_the_unanswered_table_shows_dates_as_dd_mm_yyyy(self):
        frame = faq.misses_frame(
            [FaqMiss(invitee_name="Raj", question="Is parking free?", agenda_tag="Salary", created_at="2026-09-28 10:00:00")]
        )
        assert frame.iloc[0].to_dict() == {
            "Invitee": "Raj",
            "Question": "Is parking free?",
            "Asked during": "Salary",
            "Asked on": "28-09-2026",
            "Answer": "",
        }


class TestPrompt:
    def test_the_faq_and_its_rule_are_in_the_instructions(self):
        instructions = system_instructions(Meeting(subject="Hiring"), faq=Faq(entries=[NOTICE, CREDIT]))
        assert "Q: What is the notice period?\nA: 60 days for managers, 30 for others." in instructions
        assert "answer it only from this FAQ" in instructions
        assert "unanswered_question" in instructions

    def test_without_a_faq_nothing_is_added(self):
        assert "FAQ" not in system_instructions(Meeting(subject="Hiring"))

    def _stub(self, monkeypatch, missed: str):
        seen = {}

        def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
            seen["instructions"] = instructions
            return ChatTurnOutput(reply="I'll check.", agenda_tag="", unanswered_question=missed)

        monkeypatch.setattr(chat_agent, "run_structured", fake_run_structured)
        return seen

    def test_send_turn_passes_the_faq_and_keeps_the_unanswered_question(self, monkeypatch):
        seen = self._stub(monkeypatch, "  Is   parking free? ")
        turn = send_turn(Meeting(subject="Hiring"), PROFILE, "", [], "Is parking free?", faq=Faq(entries=[NOTICE]))
        assert "What is the notice period?" in seen["instructions"]
        assert turn.unanswered_question == "Is parking free?"

    def test_without_a_faq_an_unanswered_question_is_dropped(self, monkeypatch):
        self._stub(monkeypatch, "Is parking free?")
        assert send_turn(Meeting(subject="Hiring"), PROFILE, "", [], "Is parking free?").unanswered_question == ""

    def test_the_opening_message_never_logs_a_question(self, monkeypatch):
        self._stub(monkeypatch, "Hello?")
        assert opening_message(Meeting(subject="Hiring"), PROFILE, faq=Faq(entries=[NOTICE])).unanswered_question == ""


class TestStorage:
    def _meeting(self, tmp_path):
        path = tmp_path / "m.db"
        init_db(path)
        seed_default_admin(path)
        meetings_db.init_meetings_tables(path)
        meeting = meetings_db.create_meeting(1, Meeting(subject="Hiring"), db_path=path)
        invitee_id = meetings_db.add_invitee(
            meeting.meeting_id, 1, "Raj", "raj@x.com", "tok", access.encrypt_code("1"), db_path=path
        )
        return path, meeting.meeting_id, invitee_id

    def test_the_faq_is_saved_replaced_and_removed(self, tmp_path):
        path, meeting_id, _ = self._meeting(tmp_path)
        assert meetings_db.load_faq(meeting_id, db_path=path) is None

        meetings_db.save_faq(meeting_id, 1, Faq(source_file="faq.xlsx", entries=[NOTICE]), db_path=path)
        meetings_db.save_faq(meeting_id, 1, Faq(source_file="faq2.csv", entries=[NOTICE, CREDIT]), db_path=path)
        assert meetings_db.load_faq(meeting_id, db_path=path) == Faq(source_file="faq2.csv", entries=[NOTICE, CREDIT])

        meetings_db.delete_faq(meeting_id, 1, db_path=path)
        assert meetings_db.load_faq(meeting_id, db_path=path) is None

    def test_only_the_owner_can_save_a_faq(self, tmp_path):
        path, meeting_id, _ = self._meeting(tmp_path)
        with pytest.raises(MeetingStorageError):
            meetings_db.save_faq(meeting_id, 99, Faq(entries=[NOTICE]), db_path=path)

    def test_a_question_is_logged_once_per_invitee_with_their_name(self, tmp_path):
        path, meeting_id, invitee_id = self._meeting(tmp_path)
        assert meetings_db.add_faq_miss(meeting_id, invitee_id, "Is parking free?", "Salary", db_path=path)
        assert not meetings_db.add_faq_miss(meeting_id, invitee_id, "is parking  free", "Salary", db_path=path)
        assert not meetings_db.add_faq_miss(meeting_id, invitee_id, "   ", db_path=path)

        misses = meetings_db.list_faq_misses(meeting_id, 1, db_path=path)
        assert [(miss.invitee_name, miss.question, miss.agenda_tag) for miss in misses] == [
            ("Raj", "Is parking free?", "Salary")
        ]

        meetings_db.delete_faq_misses(meeting_id, 1, [misses[0].miss_id], db_path=path)
        assert meetings_db.list_faq_misses(meeting_id, 1, db_path=path) == []

    def test_an_older_database_gains_the_faq_tables(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE meeting_agenda_tables (table_id INTEGER PRIMARY KEY, meeting_id INTEGER, "
                "item_ref TEXT, source_file TEXT, locked_columns TEXT, editable_columns TEXT, base_data TEXT, "
                "created_at TEXT)"
            )
        meetings_db.init_meetings_tables(path)
        with sqlite3.connect(path) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"meeting_faqs", "meeting_faq_misses"} <= names
