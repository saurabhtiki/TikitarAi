"""Phase 59: the organiser's own How to use text, greeting the invitee by name, and
Type / Rule / Required for the columns an invitee fills in."""

import datetime
import sqlite3
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access, chat_agent, guide, steps, tables
from meetings import db as meetings_db
from meetings.chat_agent import ChatTurnOutput, opening_message, system_instructions
from meetings.model import (
    ANSWER_CHOICE,
    ANSWER_DATE,
    ANSWER_NUMBER,
    ANSWER_TEXT,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    SENDER_AI,
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    ColumnRule,
    Meeting,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATOR_PAGE = str(PROJECT_ROOT / "app_pages" / "meetings.py")
PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""
TOKEN = "token-59"
PRICES = "Price list"
TODAY = datetime.date(2026, 10, 1)
RULES = {
    "Expected date": ColumnRule(ANSWER_DATE, "future", required=True),
    "Expected price": ColumnRule(ANSWER_NUMBER, "> 0"),
    "Will deliver?": ColumnRule(ANSWER_YES_NO),
    "Status": ColumnRule(ANSWER_CHOICE, "Paid, Partly paid, Disputed"),
    "Remarks": ColumnRule(ANSWER_TEXT, "at most 10 characters"),
}


def _table(rules=None) -> AgendaTable:
    return AgendaTable(
        item_ref=PRICES,
        locked_columns=["Item"],
        editable_columns=list(RULES),
        base_data=[{"Item": "Pen"}, {"Item": "Ink"}, {"Item": "Pad"}],
        column_rules=RULES if rules is None else rules,
    )


def _setup(tmp_path, monkeypatch, agenda, table=None):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)
    meeting = meetings_db.create_meeting(1, Meeting(subject="Vendor", agenda=agenda))
    invitee_id = meetings_db.add_invitee(meeting.meeting_id, 1, "Ravi", "r@x.com", TOKEN, access.encrypt_code("123456"))
    if table is not None:
        meetings_db.save_agenda_table(meeting.meeting_id, 1, table)
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


def _button(app, label):
    return next(button for button in app.button if button.label == label)


class TestHowToUse:
    MEETING = Meeting(subject="Hiring", agenda=[AgendaItem(item="Salary", item_type=QUESTION_ITEM)])

    def test_default_steps_are_numbered(self):
        lines = guide.default_steps(self.MEETING, {}, {})
        text = guide.invitee_text(self.MEETING, lines)
        assert text.startswith("1. Type your reply")
        assert "Close chat" in text

    def test_own_text_replaces_the_steps(self):
        meeting = Meeting(subject="Hiring", how_to_use="Keep your payslips ready.")
        assert guide.invitee_text(meeting, ["Type your reply"]) == "Keep your payslips ready."

    def test_unchanged_default_is_stored_as_blank(self):
        lines = guide.default_steps(self.MEETING, {}, {})
        assert guide.text_to_store(guide.numbered(lines), lines) == ""
        assert guide.text_to_store("  My own steps ", lines) == "My own steps"

    def test_table_step_names_column_rules(self):
        meeting = Meeting(subject="Vendor", agenda=[AgendaItem(item=PRICES, item_type=TABLE_ITEM)])
        lines = guide.default_steps(meeting, {PRICES: _table()}, {})
        text = " ".join(lines)
        assert "Expected price (a number more than 0)" in text
        assert "Expected date (a date (dd-mm-yyyy) in the future, required)" in text

    def test_how_to_use_is_saved_and_loaded(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(tmp_path, monkeypatch, [])
        meetings_db.set_how_to_use(meeting.meeting_id, 1, "  Bring your ID.  ")
        assert meetings_db.load_meeting(meeting.meeting_id, 1).how_to_use == "Bring your ID."

    def test_organiser_saves_own_text_and_invitee_sees_it(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, [AgendaItem(item="Delivery")])
        app = _organiser_app(meeting.meeting_id).run()
        box = next(area for area in app.text_area if area.label.startswith("What the invitee sees"))
        assert box.value.startswith("1. Type your reply")
        box.set_value("Keep your last 3 payslips ready.").run()
        _button(app, "Save instructions").click().run()
        assert meetings_db.load_meeting(meeting.meeting_id, 1).how_to_use == "Keep your last 3 payslips ready."

        invitee = _invitee_app(meeting, invitee_id, probe).run()
        assert any("Keep your last 3 payslips ready." in element.value for element in invitee.markdown)

    def test_use_default_goes_back(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(tmp_path, monkeypatch, [AgendaItem(item="Delivery")])
        meetings_db.set_how_to_use(meeting.meeting_id, 1, "Own text")
        app = _organiser_app(meeting.meeting_id).run()
        _button(app, "Use default").click().run()
        assert meetings_db.load_meeting(meeting.meeting_id, 1).how_to_use == ""


class TestGreetingByName:
    def test_instructions_name_the_invitee(self):
        text = system_instructions(Meeting(subject="Vendor"), invitee_name="Ravi")
        assert "You are talking to Ravi" in text
        assert "Hi Ravi" in text

    def test_no_name_no_name_line(self):
        assert "You are talking to" not in system_instructions(Meeting(subject="Vendor"))

    def test_opening_message_carries_the_name(self, monkeypatch):
        seen = {}

        def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
            seen["instructions"], seen["prompt"] = instructions, prompt
            return ChatTurnOutput(reply="Hi Ravi")

        monkeypatch.setattr(chat_agent, "run_structured", fake_run_structured)
        opening_message(Meeting(subject="Vendor"), {}, invitee_name="Ravi")
        assert "You are talking to Ravi" in seen["instructions"]
        assert "by name" in seen["prompt"]

    def test_instructions_describe_table_columns(self):
        meeting = Meeting(subject="Vendor", agenda=[AgendaItem(item=PRICES, item_type=TABLE_ITEM)])
        text = system_instructions(meeting, sheets={PRICES: _table()})
        assert "Columns the invitee fills in: Expected date" in text
        assert "Expected price (a number more than 0)" in text


class TestColumnRules:
    def test_bad_rules_are_named(self):
        problems = tables.column_rule_problems(
            {"Status": ColumnRule(ANSWER_CHOICE, "Paid"), "Price": ColumnRule(ANSWER_NUMBER, "lots")}
        )
        assert problems[0].startswith("Status: a Choice column needs at least two options")
        assert problems[1].startswith("Price: 'lots' isn't a rule")
        assert tables.column_rule_problems(RULES) == []

    def test_good_rows_are_cleaned(self):
        rows = {0: {"Expected date": "05-10-2026", "Expected price": "45,000", "Will deliver?": "yes",
                    "Status": "paid", "Remarks": "ok"}}
        cleaned, problems = tables.check_rows(_table(), rows, TODAY)
        assert problems == []
        assert cleaned == {0: {"Expected date": "05-10-2026", "Expected price": "45000", "Will deliver?": "Yes",
                               "Status": "Paid", "Remarks": "ok"}}

    def test_bad_values_are_listed_by_row(self):
        rows = {
            0: {"Expected date": "05-10-2026", "Expected price": "0"},
            1: {"Expected date": "01-01-2020"},
            2: {"Expected date": "05-10-2026", "Status": "Maybe", "Remarks": "far too long text"},
        }
        _, problems = tables.check_rows(_table(), rows, TODAY)
        assert "Row 1, Expected price: '0' — needs to be a number more than 0 — 0 is too low." in problems
        assert any(problem.startswith("Row 2, Expected date: '01-01-2020'") for problem in problems)
        assert any(problem.startswith("Row 3, Status") for problem in problems)
        assert any(problem.startswith("Row 3, Remarks") for problem in problems)

    def test_required_only_for_started_rows(self):
        _, problems = tables.check_rows(_table(), {1: {"Expected price": "10"}}, TODAY)
        assert problems == ["Row 2, Expected date: this column is required."]
        assert tables.check_rows(_table(), {}, TODAY) == ({}, [])

    def test_no_rules_means_text(self):
        cleaned, problems = tables.check_rows(_table(rules={}), {0: {"Expected price": "lots"}}, TODAY)
        assert problems == [] and cleaned == {0: {"Expected price": "lots"}}

    def test_grid_frame_is_typed_and_reads_back(self):
        table = _table()
        frame = tables.display_frame(table, {0: {"Expected date": "05-10-2026", "Expected price": "45000"}})
        assert frame.loc[0, "Expected date"] == datetime.date(2026, 10, 5)
        assert frame.loc[0, "Expected price"] == 45000.0
        assert pd.isna(frame.loc[1, "Expected price"]) and frame.loc[1, "Expected date"] is None
        assert tables.responses_from_frame(table, frame) == {0: {"Expected date": "05-10-2026", "Expected price": "45000"}}

    def test_cell_text(self):
        assert tables.cell_text(float("nan")) == ""
        assert tables.cell_text(pd.Timestamp("2026-10-05")) == "05-10-2026"
        assert tables.cell_text(12.5) == "12.5"
        assert tables.cell_text(None) == ""

    def test_date_limits_and_max_length(self):
        future = AgendaItem(item="d", item_type=QUESTION_ITEM, answer_type=ANSWER_DATE, rule="future")
        within = AgendaItem(item="d", item_type=QUESTION_ITEM, answer_type=ANSWER_DATE, rule="within 30 days")
        assert steps.date_limits(future, TODAY) == (datetime.date(2026, 10, 2), None)
        assert steps.date_limits(within, TODAY) == (TODAY, datetime.date(2026, 10, 31))
        text = AgendaItem(item="t", item_type=QUESTION_ITEM, answer_type=ANSWER_TEXT, rule="at most 5 characters")
        assert steps.max_length(text) == 5

    def test_at_most_rule_works_for_a_question(self):
        item = AgendaItem(item="Why?", item_type=QUESTION_ITEM, answer_type=ANSWER_TEXT, rule="at most 5 characters")
        assert steps.rule_problem(item) == ""
        assert steps.check_answer(item, "short", TODAY).ok
        assert not steps.check_answer(item, "too long", TODAY).ok
        assert steps.describe_rule(item) == "a text answer of at most 5 characters"


class TestColumnRulesStorage:
    def test_rules_save_load_and_update(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(tmp_path, monkeypatch, [AgendaItem(item=PRICES, item_type=TABLE_ITEM)], _table())
        loaded = meetings_db.find_agenda_table(meeting.meeting_id, PRICES)
        assert loaded.column_rules == RULES
        meetings_db.update_column_rules(meeting.meeting_id, 1, PRICES, {"Remarks": ColumnRule(required=True)})
        assert meetings_db.find_agenda_table(meeting.meeting_id, PRICES).column_rules == {
            "Remarks": ColumnRule(required=True)
        }

    def test_old_database_gets_the_new_columns(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "old.db"
        with sqlite3.connect(path) as connection:
            connection.execute(meetings_db._CREATE_MEETINGS_TABLE)
            connection.execute(meetings_db._CREATE_AGENDA_TABLES_TABLE)
        meetings_db.init_meetings_tables(path)
        with sqlite3.connect(path) as connection:
            meeting_columns = {row[1] for row in connection.execute("PRAGMA table_info(meetings);")}
            table_columns = {row[1] for row in connection.execute("PRAGMA table_info(meeting_agenda_tables);")}
        assert "how_to_use" in meeting_columns
        assert "column_rules" in table_columns

    def test_organiser_sees_and_saves_column_rules(self, tmp_path, monkeypatch):
        meeting, _, _ = _setup(
            tmp_path, monkeypatch, [AgendaItem(item=PRICES, item_type=TABLE_ITEM)], _table(rules={})
        )
        app = _organiser_app(meeting.meeting_id).run()
        assert not app.exception
        assert any(element.value == "**Column rules**" for element in app.markdown)
        _button(app, "Save column rules").click().run()
        assert not app.exception
        assert meetings_db.find_agenda_table(meeting.meeting_id, PRICES).column_rules["Remarks"] == ColumnRule()

    def test_type_change_that_would_lose_answers_is_refused(self, tmp_path, monkeypatch):
        meeting, invitee_id, _ = _setup(
            tmp_path, monkeypatch, [AgendaItem(item=PRICES, item_type=TABLE_ITEM)], _table(rules={})
        )
        table = meetings_db.find_agenda_table(meeting.meeting_id, PRICES)
        meetings_db.save_table_responses(table.table_id, invitee_id, {0: {"Expected price": "approx 40k"}})
        all_responses = meetings_db.load_all_table_responses(table.table_id)
        assert tables.unreadable_answers(RULES, all_responses) == ["Expected price: 'approx 40k'"]
        assert tables.unreadable_answers({}, all_responses) == []
        # A changed Rule on a type the answer still fits is fine.
        assert tables.unreadable_answers({"Expected price": ColumnRule(ANSWER_TEXT, "at most 5 characters")}, all_responses) == []

    def test_invitee_grid_with_typed_columns_renders(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(
            tmp_path, monkeypatch, [AgendaItem(item=PRICES, item_type=TABLE_ITEM)], _table()
        )
        table = meetings_db.find_agenda_table(meeting.meeting_id, PRICES)
        meetings_db.save_table_responses(
            table.table_id, invitee_id, {0: {"Expected date": "05-10-2026", "Expected price": "45000", "Status": "Paid"}}
        )
        app = _invitee_app(meeting, invitee_id, probe).run()
        assert not app.exception
