"""For each lists on both pages (phase 52), driven with the model stubbed.

The invitee is walked through an invoice list row by row; the organiser uploads the list,
follows progress, and gets the answers back as a table.
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access, steps
from meetings import db as meetings_db
from meetings.chat_agent import AnswerReading, ChatTurnOutput
from meetings.model import (
    ANSWER_DATE,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    SENDER_AI,
    STEP_ANSWERED,
    AgendaItem,
    AgendaTable,
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

TOKEN = "token-loop"
INVOICES = "Outstanding invoices"
PAID = "Is it paid already?"
DATE = "Expected payment date"


def _setup(tmp_path, monkeypatch, match_column="", attach=True):
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
            subject="AR review",
            agenda=[
                AgendaItem(item=PAID, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO, loop=INVOICES),
                AgendaItem(item=DATE, item_type=QUESTION_ITEM, answer_type=ANSWER_DATE, loop=INVOICES),
            ],
        ),
    )
    invitee_id = meetings_db.add_invitee(
        meeting.meeting_id, 1, "ABC Traders", "abc@x.com", TOKEN, access.encrypt_code("123456")
    )
    if attach:
        meetings_db.save_agenda_table(
            meeting.meeting_id,
            1,
            AgendaTable(
                item_ref=INVOICES,
                source_file="invoices.xlsx",
                locked_columns=["Bill No", "Customer"],
                base_data=[
                    {"Bill No": "1001", "Customer": "ABC Traders"},
                    {"Bill No": "1002", "Customer": "XYZ Ltd"},
                    {"Bill No": "1003", "Customer": "ABC Traders"},
                ],
                match_column=match_column,
            ),
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


def _organiser_app(meeting_id):
    app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.session_state["meeting_open_id"] = meeting_id
    return app


def _stub_model(monkeypatch, reading: AnswerReading) -> dict:
    """Stubs both model calls; returns what each was told."""
    import app_pages.meeting_invitee as page

    seen = {}

    def _read(*args, **kwargs):
        seen["read_row"] = kwargs.get("row_context", "")
        return reading

    def _reply(*args, **kwargs):
        seen.update(kwargs)
        return ChatTurnOutput(reply="Next.", agenda_tag="")

    monkeypatch.setattr(page.chat_agent, "read_answer", _read)
    monkeypatch.setattr(page.chat_agent, "send_turn", _reply)
    return seen


def _captions(app) -> str:
    return " ".join(caption.value for caption in app.caption)


def _answer(meeting_id, invitee_id, title, row, value):
    key = steps.answer_key(title, row)
    meetings_db.save_step_answer(
        meeting_id, invitee_id, StepAnswer(item_ref=key, value=value, status=STEP_ANSWERED, tries=1)
    )


class TestInvitee:
    def test_the_conversation_opens_on_the_first_row(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        import app_pages.meeting_invitee as page

        seen = {}

        def _open(*args, **kwargs):
            seen.update(kwargs)
            return ChatTurnOutput(reply="Welcome!", agenda_tag=kwargs["step_tag"])

        monkeypatch.setattr(page.chat_agent, "opening_message", _open)
        _invitee_app(probe, meeting.meeting_id, invitee_id).run()

        assert seen["step_tag"] == PAID
        assert f"{INVOICES}: row 1 of 3, question 1 of 2" in seen["step_guidance"]
        assert "Bill No: 1001" in seen["step_guidance"]

    def test_each_row_is_asked_in_turn_and_answers_land_on_their_row(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Invoice 1001 — paid?", PAID)
        _answer(meeting.meeting_id, invitee_id, PAID, 0, "No")
        app = _invitee_app(probe, meeting.meeting_id, invitee_id)
        seen = _stub_model(monkeypatch, AnswerReading(value="01-12-2099"))

        app.run()
        assert f"{INVOICES}: row 1 of 3, question 2 of 2" in _captions(app)
        app.chat_input(key="invitee_chat_input").set_value("first of December 2099").run()

        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id)
        assert saved[steps.answer_key(DATE, 0)].value == "01-12-2099"
        assert "Bill No: 1001" in seen["read_row"]
        assert seen["step_tag"] == PAID
        assert "Bill No: 1002" in seen["step_guidance"]
        assert f"{INVOICES}: row 2 of 3, question 1 of 2" in _captions(app)

    def test_a_match_column_asks_only_about_the_invitees_own_rows(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, match_column="Customer")
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Invoice 1001 — paid?", PAID)
        _answer(meeting.meeting_id, invitee_id, PAID, 0, "No")
        app = _invitee_app(probe, meeting.meeting_id, invitee_id)
        seen = _stub_model(monkeypatch, AnswerReading(value="01-12-2099"))

        app.run()
        app.chat_input(key="invitee_chat_input").set_value("1 Dec 2099").run()

        # XYZ Ltd's invoice 1002 is not theirs, so the next row is 1003.
        assert "Bill No: 1003" in seen["step_guidance"]
        assert f"{INVOICES}: row 2 of 2, question 1 of 2" in _captions(app)


class TestOrganiser:
    def test_list_answers_status_and_overview(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch, match_column="Customer")
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        _answer(meeting.meeting_id, invitee_id, PAID, 0, "No")
        _answer(meeting.meeting_id, invitee_id, DATE, 0, "01-12-2099")

        app = _organiser_app(meeting.meeting_id).run()

        frame = next(element.value for element in app.dataframe if DATE in element.value.columns)
        assert frame[["Invitee", "Bill No", PAID, DATE]].values.tolist() == [
            ["ABC Traders", "1001", "No", "01-12-2099"],
            ["ABC Traders", "1003", "", ""],
        ]
        assert f"{INVOICES}: 1 of 2 row(s) done" in _captions(app)
        assert any(f"for each row of: {INVOICES}" in element.value for element in app.markdown)
        assert not any(PAID in element.value.columns and DATE not in element.value.columns for element in app.dataframe)

    def test_uploading_the_list_saves_it_with_its_match_column(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch, attach=False)
        app = _organiser_app(meeting.meeting_id).run()
        assert "no list attached" in _captions(app).lower()

        upload_key = f"meetings_list_upload_{meeting.meeting_id}_{INVOICES}"
        app.file_uploader(key=upload_key).set_value(
            ("invoices.csv", b"Bill No,Customer\n2001,ABC Traders\n2002,XYZ Ltd\n", "text/csv")
        )
        app.run()
        app.selectbox(key=f"meetings_list_match_{meeting.meeting_id}_{INVOICES}").set_value("Customer").run()
        app.button(key=f"meetings_list_save_{meeting.meeting_id}_{INVOICES}").click().run()

        saved = meetings_db.find_agenda_table(meeting.meeting_id, INVOICES)
        assert (saved.match_column, saved.row_count(), saved.editable_columns) == ("Customer", 2, [])
