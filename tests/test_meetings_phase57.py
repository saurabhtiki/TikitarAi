"""Phase 57: invitee files for the organiser, Rule vs Go to notes, clearer How to use and
reference documents, chat/summary downloads, and For each lists that everyone gets."""

import datetime
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access, loops, steps, storage, summary_agent, transcript
from meetings import db as meetings_db
from meetings.model import (
    ANSWER_DATE,
    ANSWER_NUMBER,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    SENDER_AI,
    SENDER_USER,
    STEP_ANSWERED,
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    ChatMessage,
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
TOKEN = "token-57"
INVOICES = "Outstanding invoices"
PAID = "Will it be paid on time?"
DATE = "Expected payment date"
PRICES = "Price list"
LIST_AGENDA = [
    AgendaItem(item=PAID, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO, loop=INVOICES),
    AgendaItem(item=DATE, item_type=QUESTION_ITEM, answer_type=ANSWER_DATE, loop=INVOICES),
]
INVOICE_ROWS = [
    {"Invoice No": "INV-101", "Amount": "50000"},
    {"Invoice No": "INV-102", "Amount": "20000"},
]


def _salary(rule: str, branch: str) -> AgendaItem:
    return AgendaItem(
        item="Expected salary", item_type=QUESTION_ITEM, answer_type=ANSWER_NUMBER, rule=rule, branch=branch
    )


def _setup(tmp_path, monkeypatch, agenda, label_column="", match_column="", attach=True):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)
    meeting = meetings_db.create_meeting(1, Meeting(subject="AR review", agenda=agenda))
    invitee_id = meetings_db.add_invitee(meeting.meeting_id, 1, "Sam", "s@x.com", TOKEN, access.encrypt_code("123456"))
    if attach and steps.loop_names(meeting):
        meetings_db.save_agenda_table(
            meeting.meeting_id,
            1,
            AgendaTable(
                item_ref=INVOICES,
                source_file="invoices.xlsx",
                locked_columns=["Invoice No", "Amount"],
                base_data=list(INVOICE_ROWS),
                label_column=label_column,
                match_column=match_column,
            ),
        )
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    return meeting, invitee_id, str(probe)


def _organiser_app(meeting_id):
    app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.session_state["meeting_open_id"] = meeting_id
    return app


def _invitee_app(meeting, invitee_id, probe):
    meetings_db.ensure_session(meeting.meeting_id, invitee_id)
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Hello!", "")
    app = AppTest.from_file(probe, default_timeout=60)
    app.session_state["probe_meeting_id"] = meeting.meeting_id
    app.session_state["probe_token"] = TOKEN
    app.session_state["invitee_verified"] = True
    app.session_state["invitee_meeting_id"] = meeting.meeting_id
    app.session_state["invitee_id"] = invitee_id
    return app


def _panel_labels(app) -> list[str]:
    # AppTest files an expander that has an icon under "status".
    return [block.label for block in app.get("status")]


def _captions(app) -> str:
    return " ".join(caption.value for caption in app.caption)


def _download_labels(app) -> list[str]:
    return [element.proto.label for element in app.get("download_button")]


def _answer(meeting_id, invitee_id, title, row, value):
    meetings_db.save_step_answer(
        meeting_id,
        invitee_id,
        StepAnswer(item_ref=steps.answer_key(title, row), value=value, status=STEP_ANSWERED, tries=1),
    )


# --------------------------------------------------------------------------------------
# 2. Rule vs Go to
# --------------------------------------------------------------------------------------


class TestRuleAndGoTo:
    def test_a_go_to_reaching_past_the_rule_gets_a_note(self):
        warnings = steps.agenda_warnings([_salary("between 10000 and 500000", "if > 100000 go to End")])
        assert len(warnings) == 1
        assert "also covers numbers the Rule refuses" in warnings[0]
        assert "10000 to 500000" in warnings[0]

    def test_a_go_to_inside_the_rule_gets_no_note(self):
        assert steps.agenda_warnings([_salary("between 10000 and 500000", "if between 100000 and 500000 go to End")]) == []

    def test_a_go_to_that_can_never_happen_says_so(self):
        warnings = steps.agenda_warnings([_salary("< 50000", "if > 60000 go to End")])
        assert "can never happen" in warnings[0]

    def test_no_rule_means_no_note(self):
        assert steps.agenda_warnings([_salary("", "if > 100000 go to End")]) == []

    def test_a_refused_number_says_too_high_or_too_low(self):
        item = _salary("between 10000 and 500000", "")
        today = datetime.date(2026, 9, 29)
        assert steps.check_answer(item, "700000", today).reason.endswith("700000 is too high.")
        assert steps.check_answer(item, "5000", today).reason.endswith("5000 is too low.")
        assert steps.check_answer(item, "200000", today).ok

    def test_the_organiser_sees_the_note_in_overview(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(tmp_path, monkeypatch, [_salary("between 10000 and 500000", "if > 100000 go to End")])
        app = _organiser_app(meeting.meeting_id).run()
        assert "also covers numbers the Rule refuses" in _captions(app)


# --------------------------------------------------------------------------------------
# 5. Lists: row name, everyone gets every row, side panel table
# --------------------------------------------------------------------------------------


class TestListHelpers:
    TABLE = AgendaTable(
        item_ref=INVOICES, locked_columns=["Amount", "Invoice No"], base_data=list(INVOICE_ROWS), label_column="Invoice No"
    )

    def _lists(self, table=None):
        table = table or self.TABLE
        return loops.invitee_lists({steps.loop_key(INVOICES): table}, {"name": "Sam", "email": "s@x.com"})

    def test_everyone_gets_every_row_without_a_match_column(self):
        assert self._lists().rows == {steps.loop_key(INVOICES): [0, 1]}

    def test_the_row_name_comes_first_and_labels_the_row(self):
        meeting = Meeting(agenda=list(LIST_AGENDA))
        step = steps.next_question(meeting, {}, self._lists().rows)
        assert self._lists().row_label(step) == "INV-101"
        assert self._lists().row_context(step).startswith("Invoice No: INV-101")
        text = steps.progress_text(meeting, step, {}, self._lists().rows, row_label="INV-101")
        assert text == f"{INVOICES} — INV-101: row 1 of 2, question 1 of 2"

    def test_no_row_name_column_means_no_label(self):
        table = AgendaTable(item_ref=INVOICES, locked_columns=["Invoice No"], base_data=list(INVOICE_ROWS))
        meeting = Meeting(agenda=list(LIST_AGENDA))
        step = steps.next_question(meeting, {}, self._lists(table).rows)
        assert self._lists(table).row_label(step) == ""

    def test_zero_row_invitees_are_found(self):
        table = AgendaTable(item_ref=INVOICES, locked_columns=["Invoice No"], base_data=list(INVOICE_ROWS), match_column="Invoice No")
        assert [person["name"] for person in loops.zero_row_invitees(table, [{"name": "Sam", "email": "s@x.com"}])] == ["Sam"]
        assert loops.zero_row_invitees(self.TABLE, [{"name": "Sam", "email": "s@x.com"}]) == []

    def test_side_panel_frame_marks_done_and_now(self):
        meeting = Meeting(agenda=list(LIST_AGENDA))
        answers = {
            steps.answer_key(PAID, 0): StepAnswer(item_ref=steps.answer_key(PAID, 0), value="Yes", status=STEP_ANSWERED),
            steps.answer_key(DATE, 0): StepAnswer(
                item_ref=steps.answer_key(DATE, 0), value="01-12-2099", status=STEP_ANSWERED
            ),
        }
        frame = loops.invitee_rows_frame(meeting, INVOICES, self._lists(), answers)
        assert frame[loops.STATUS_COLUMN].tolist() == [loops.DONE_MARK, loops.NOW_MARK]
        assert frame["Invoice No"].tolist() == ["INV-101", "INV-102"]


class TestListPages:
    def test_update_list_settings_keeps_answers(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch, list(LIST_AGENDA), match_column="Invoice No")
        _answer(meeting.meeting_id, invitee_id, PAID, 0, "Yes")
        meetings_db.update_list_settings(meeting.meeting_id, 1, INVOICES, "Invoice No", "")
        saved = meetings_db.find_agenda_table(meeting.meeting_id, INVOICES)
        assert (saved.label_column, saved.match_column) == ("Invoice No", "")
        assert steps.answer_key(PAID, 0) in meetings_db.load_step_answers(meeting.meeting_id, invitee_id)

    def test_organiser_is_warned_about_an_invitee_with_zero_rows(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(tmp_path, monkeypatch, list(LIST_AGENDA), match_column="Invoice No")
        app = _organiser_app(meeting.meeting_id).run()
        assert "Sam (s@x.com) gets 0 rows" in _captions(app)

    def test_save_list_settings_button_turns_the_filter_off(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(tmp_path, monkeypatch, list(LIST_AGENDA), match_column="Invoice No")
        app = _organiser_app(meeting.meeting_id).run()
        key = f"saved_{meeting.meeting_id}_{INVOICES}"
        app.toggle(key=f"meetings_list_own_{key}").set_value(False).run()
        app.selectbox(key=f"meetings_list_label_{key}").set_value("Invoice No").run()
        app.button(key=f"meetings_list_settings_{meeting.meeting_id}_{INVOICES}").click().run()
        saved = meetings_db.find_agenda_table(meeting.meeting_id, INVOICES)
        assert (saved.label_column, saved.match_column) == ("Invoice No", "")

    def test_invitee_sees_the_list_and_the_row_name(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, list(LIST_AGENDA), label_column="Invoice No")
        app = _invitee_app(meeting, invitee_id, probe).run()
        assert f"{INVOICES} (2 rows)" in _panel_labels(app)
        frame = next(element.value for element in app.dataframe if loops.STATUS_COLUMN in element.value.columns)
        assert frame[loops.STATUS_COLUMN].tolist() == [loops.NOW_MARK, ""]
        assert f"{INVOICES} — INV-101: row 1 of 2" in _captions(app)
        assert any(f"each row of **{INVOICES}**" in element.value for element in app.markdown)


# --------------------------------------------------------------------------------------
# 1, 3, 4. Files, How to use, reference documents, downloads
# --------------------------------------------------------------------------------------


class TestFilesAndInstructions:
    def test_organiser_can_download_an_invitee_file(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch, [AgendaItem(item="Delivery")])
        path = storage.save_upload(b"quote", "quote.pdf", meeting.meeting_id, invitee_id, root=tmp_path / "files")
        meetings_db.add_file(meeting.meeting_id, "quote.pdf", str(path), invitee_id)
        app = _organiser_app(meeting.meeting_id).run()
        assert any("Files (1)" in element.value for element in app.markdown)
        assert "quote.pdf" in _download_labels(app)

    def test_how_to_use_names_the_table_tab_and_its_columns(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, [AgendaItem(item=PRICES, item_type=TABLE_ITEM)])
        meetings_db.save_agenda_table(
            meeting.meeting_id,
            1,
            AgendaTable(
                item_ref=PRICES, locked_columns=["Item"], editable_columns=["Rate", "Delivery days"], base_data=[{"Item": "Pen"}]
            ),
        )
        app = _invitee_app(meeting, invitee_id, probe).run()
        text = " ".join(element.value for element in app.markdown)
        assert f"Open the tab **📋 {PRICES}** and fill in **Rate, Delivery days**" in text

    def test_invitee_sees_reference_documents(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, [AgendaItem(item="Delivery")])
        path = storage.save_upload(b"terms", "terms.pdf", meeting.meeting_id, None, root=tmp_path / "files")
        meetings_db.add_file(meeting.meeting_id, "terms.pdf", str(path))
        app = _invitee_app(meeting, invitee_id, probe).run()
        assert "Reference documents" in _panel_labels(app)
        assert "terms.pdf" in _download_labels(app)


class TestDownloads:
    MESSAGES = [
        ChatMessage(sender=SENDER_AI, text="What salary?", created_at="2026-09-29 10:15:00"),
        ChatMessage(sender=SENDER_USER, text="45k", created_at="2026-09-29 10:16:00"),
    ]

    def test_chat_text(self):
        text = transcript.chat_text("Interview", "Raj", self.MESSAGES)
        assert "[29-09-2026 10:15] AI: What salary?" in text
        assert "[29-09-2026 10:16] Raj: 45k" in text

    def test_summary_text(self):
        summary = summary_agent.MeetingSummary(
            agenda_items=[summary_agent.AgendaItemSummary(item="Salary", discussed=True, notes="- 45000")],
            other_extra="Asked about leave.",
        )
        text = transcript.summary_text("Interview", "Raj", summary)
        assert "Salary\n- 45000" in text and "Other / Extra\nAsked about leave." in text

    def test_all_chats_excel(self):
        frame = transcript.all_chats_frame([("Raj", self.MESSAGES), ("Sam", [])])
        assert frame.columns.tolist() == transcript.CHAT_COLUMNS
        assert frame["From"].tolist() == ["AI", "Raj"]
        assert pd.read_excel(__import__("io").BytesIO(transcript.to_excel_bytes(frame))).shape == (2, 4)

    def test_organiser_gets_download_buttons(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(tmp_path, monkeypatch, [AgendaItem(item="Delivery")])
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Hello!", "")
        summary = summary_agent.MeetingSummary(
            agenda_items=[summary_agent.AgendaItemSummary(item="Delivery", discussed=True, notes="- 5 days")]
        )
        meetings_db.close_session(meeting.meeting_id, invitee_id, summary_agent.to_json(summary))
        app = _organiser_app(meeting.meeting_id).run()
        labels = _download_labels(app)
        assert {"Download chat", "Download all chats (Excel)", "Download summary"} <= set(labels)
