"""Phase 65: the bot's newest reply is read aloud by itself, with a switch to turn it off."""

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access, guide
from meetings import db as meetings_db
from meetings.model import QUESTION_ITEM, SENDER_AI, SENDER_USER, AgendaItem, Meeting

TOKEN = "token-65"
PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""


def _listen_data(app) -> list[dict]:
    """The data of every 🔊 button on the page, in order (the Speak button has no `text`)."""
    found = [json.loads(component.proto.json or "{}") for component in app.get("bidi_component")]
    return [data for data in found if "text" in data]


def _setup(tmp_path, monkeypatch, conversation):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)
    meeting = meetings_db.create_meeting(1, Meeting(subject="Vendor", agenda=[AgendaItem(item="Delivery")]))
    invitee_id = meetings_db.add_invitee(meeting.meeting_id, 1, "Ravi", "r@x.com", TOKEN, access.encrypt_code("123456"))
    meetings_db.ensure_session(meeting.meeting_id, invitee_id)
    for sender, text in conversation:
        meetings_db.add_message(meeting.meeting_id, invitee_id, sender, text, "")
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    app = AppTest.from_file(str(probe), default_timeout=60)
    app.session_state["probe_meeting_id"] = meeting.meeting_id
    app.session_state["probe_token"] = TOKEN
    app.session_state["invitee_verified"] = True
    app.session_state["invitee_meeting_id"] = meeting.meeting_id
    app.session_state["invitee_id"] = invitee_id
    return app, meeting.meeting_id, invitee_id


BOT_LAST = [(SENDER_AI, "Hello Ravi!"), (SENDER_USER, "Hi"), (SENDER_AI, "When can you deliver?")]


class TestAutoRead:
    def test_switch_is_on_by_default(self, tmp_path, monkeypatch):
        app, _, _ = _setup(tmp_path, monkeypatch, BOT_LAST)
        app.run()
        assert not app.exception
        switch = app.toggle(key="invitee_auto_read")
        assert switch.value is True
        assert switch.help

    def test_only_the_newest_bot_reply_is_read(self, tmp_path, monkeypatch):
        app, _, _ = _setup(tmp_path, monkeypatch, BOT_LAST)
        app.run()
        assert [data["auto"] for data in _listen_data(app)] == [False, True]

    def test_the_first_greeting_is_read(self, tmp_path, monkeypatch):
        app, _, _ = _setup(tmp_path, monkeypatch, [(SENDER_AI, "Hello Ravi!")])
        app.run()
        assert [data["auto"] for data in _listen_data(app)] == [True]

    def test_nothing_is_read_when_the_invitee_spoke_last(self, tmp_path, monkeypatch):
        app, _, _ = _setup(tmp_path, monkeypatch, [(SENDER_AI, "Hello Ravi!"), (SENDER_USER, "Hi")])
        app.run()
        assert [data["auto"] for data in _listen_data(app)] == [False]

    def test_switch_off_reads_nothing(self, tmp_path, monkeypatch):
        app, _, _ = _setup(tmp_path, monkeypatch, BOT_LAST)
        app.run()
        app.toggle(key="invitee_auto_read").set_value(False).run()
        assert not app.exception
        assert [data["auto"] for data in _listen_data(app)] == [False, False]

    def test_each_reply_carries_its_own_id(self, tmp_path, monkeypatch):
        app, _, _ = _setup(tmp_path, monkeypatch, BOT_LAST)
        app.run()
        ids = [data["id"] for data in _listen_data(app)]
        assert len(set(ids)) == 2 and all(ids)

    def test_closed_chat_reads_nothing(self, tmp_path, monkeypatch):
        app, meeting_id, invitee_id = _setup(tmp_path, monkeypatch, BOT_LAST)
        meetings_db.close_session(meeting_id, invitee_id, "Summary")
        app.run()
        assert not app.exception
        assert [data["auto"] for data in _listen_data(app)] == [False, False]
        assert not app.get("toggle")


class TestWording:
    def test_how_to_use_mentions_the_switch(self):
        meeting = Meeting(subject="Hiring", agenda=[AgendaItem(item="Salary", item_type=QUESTION_ITEM)])
        text = guide.numbered(guide.default_steps(meeting, {}, {}))
        assert "Read replies aloud" in text

    def test_help_page_mentions_the_switch(self):
        help_page = Path(__file__).resolve().parent.parent / "static" / "help" / "meetings.html"
        assert "Read replies aloud" in help_page.read_text(encoding="utf-8")
