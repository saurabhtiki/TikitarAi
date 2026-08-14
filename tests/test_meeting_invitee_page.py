"""AppTest coverage for the invitee's chat (requirement 6.7, Phase 1, spec 2).

Driven through a probe script that calls `render_invitee_page` directly, because that is how
`streamlit_app.py` renders it — the page is deliberately not registered as an `st.Page`, so
there is no route for `AppTest` to reach it through.

The two behaviours worth the most here are the ones that are invisible until something goes
wrong: **a wrong code cannot get in and eventually locks out**, and **a provider failure
mid-conversation leaves the invitee's own message saved**.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access
from meetings import db as meetings_db
from meetings.chat_agent import ChatTurnOutput
from meetings.model import SENDER_AI, SENDER_USER, AgendaItem, Meeting
from meetings.summary_agent import AgendaItemSummary, MeetingSummary

PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""

TOKEN = "token-abc"
CODE = "123456"


def _setup(tmp_path, monkeypatch):
    """A meeting with one invitee, and a model profile for its creator."""
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    # The invitee's chat runs on the *creator's* designated default — there is no session
    # picker on that side to fall back from.
    set_default_model(profile["profile_id"], 1)

    meeting = meetings_db.create_meeting(
        1,
        Meeting(
            subject="PO No 123",
            meeting_context="Resolve open issues.",
            persona="You are the Purchase Manager.",
            agenda=[AgendaItem(item="Delivery timeline", ai_note="SLA is 30 days.")],
        ),
    )
    invitee_id = meetings_db.add_invitee(
        meeting.meeting_id, 1, "Raj", "raj@vendor.com", TOKEN, access.encrypt_code(CODE)
    )

    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    return meeting, invitee_id, str(probe)


def _app(probe_path, meeting_id, token=TOKEN):
    app = AppTest.from_file(probe_path, default_timeout=60)
    app.session_state["probe_meeting_id"] = meeting_id
    app.session_state["probe_token"] = token
    return app


def _verified(app, meeting_id, invitee_id):
    """Skips the code gate the way a successful verification would."""
    app.session_state["invitee_verified"] = True
    app.session_state["invitee_meeting_id"] = meeting_id
    app.session_state["invitee_id"] = invitee_id
    return app


class TestLinkResolution:
    def test_an_unknown_token_is_refused(self, tmp_path, monkeypatch):
        meeting, _, probe = _setup(tmp_path, monkeypatch)
        app = _app(probe, meeting.meeting_id, token="not-a-real-token").run()

        assert any("isn't valid" in error.value for error in app.error)

    def test_a_valid_token_asks_for_the_access_code(self, tmp_path, monkeypatch):
        meeting, _, probe = _setup(tmp_path, monkeypatch)
        app = _app(probe, meeting.meeting_id).run()

        assert app.text_input(key="invitee_code_input") is not None
        # The chat must not be reachable before the second factor.
        assert not app.chat_input


class TestAccessCode:
    def test_the_wrong_code_does_not_unlock(self, tmp_path, monkeypatch):
        meeting, _, probe = _setup(tmp_path, monkeypatch)
        app = _app(probe, meeting.meeting_id).run()

        app.text_input(key="invitee_code_input").set_value("000000")
        app.button[0].click().run()

        assert any("isn't right" in error.value for error in app.error)
        assert "invitee_verified" not in app.session_state

    def test_the_right_code_unlocks_the_chat(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)

        import app_pages.meeting_invitee as page

        monkeypatch.setattr(
            page.chat_agent, "opening_message", lambda *a, **k: ChatTurnOutput(reply="Welcome.", agenda_tag="Opening")
        )

        app = _app(probe, meeting.meeting_id).run()
        app.text_input(key="invitee_code_input").set_value(CODE)
        app.button[0].click().run()

        assert app.session_state["invitee_verified"] is True
        assert app.session_state["invitee_id"] == invitee_id

    def test_repeated_wrong_codes_end_in_a_lockout(self, tmp_path, monkeypatch):
        meeting, _, probe = _setup(tmp_path, monkeypatch)
        app = _app(probe, meeting.meeting_id).run()

        for _ in range(access.MAX_ATTEMPTS):
            app.text_input(key="invitee_code_input").set_value("000000")
            app.button[0].click().run()

        assert any("Try again in" in error.value for error in app.error)


class TestConversation:
    def test_the_opening_message_is_generated_and_stored(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)

        import app_pages.meeting_invitee as page

        monkeypatch.setattr(
            page.chat_agent,
            "opening_message",
            lambda *a, **k: ChatTurnOutput(reply="Welcome, let's begin.", agenda_tag="Opening"),
        )

        _verified(_app(probe, meeting.meeting_id), meeting.meeting_id, invitee_id).run()

        stored = meetings_db.list_messages(meeting.meeting_id, invitee_id)
        assert [message.text for message in stored] == ["Welcome, let's begin."]
        assert stored[0].is_from_ai()

    def test_a_reply_saves_both_sides_of_the_turn(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Welcome.", "Opening")

        import app_pages.meeting_invitee as page

        monkeypatch.setattr(
            page.chat_agent,
            "send_turn",
            lambda *a, **k: ChatTurnOutput(reply="Noted.", agenda_tag="Delivery timeline"),
        )

        app = _verified(_app(probe, meeting.meeting_id), meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("45 days.").run()

        stored = meetings_db.list_messages(meeting.meeting_id, invitee_id)
        assert [message.text for message in stored] == ["Welcome.", "45 days.", "Noted."]
        assert stored[2].agenda_tag == "Delivery timeline"

    def test_a_provider_failure_still_leaves_the_invitees_message_saved(self, tmp_path, monkeypatch):
        # The one thing that must not happen: someone types a considered answer, the provider
        # times out, and their text is gone.
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Welcome.", "Opening")

        import app_pages.meeting_invitee as page
        from llm.client import LLMConnectionError

        def _fail(*args, **kwargs):
            raise LLMConnectionError("provider is down")

        monkeypatch.setattr(page.chat_agent, "send_turn", _fail)

        app = _verified(_app(probe, meeting.meeting_id), meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("45 days.").run()

        stored = meetings_db.list_messages(meeting.meeting_id, invitee_id)
        assert "45 days." in [message.text for message in stored]


class TestClosing:
    def test_closing_generates_a_summary_and_locks_the_chat(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Welcome.", "Opening")
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_USER, "45 days.", "Delivery timeline")

        import app_pages.meeting_invitee as page

        seen = {}

        def _summarise(meeting_arg, profile, messages, **kwargs):
            seen["messages"] = messages
            return MeetingSummary(
                agenda_items=[AgendaItemSummary(item="Delivery timeline", discussed=True, notes="45 days.")],
                closing_message="Thanks.",
            )

        monkeypatch.setattr(page.summary_agent, "generate_summary", _summarise)

        app = _verified(_app(probe, meeting.meeting_id), meeting.meeting_id, invitee_id).run()
        app.button(key="invitee_close_chat").click().run()

        session = meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        assert session.closed is True
        assert "Delivery timeline" in meetings_db.get_session_summary(meeting.meeting_id, invitee_id)
        # The permanent record is built from the whole transcript, never a rolling summary.
        assert [message.text for message in seen["messages"]] == ["Welcome.", "45 days."]

    def test_a_closed_chat_is_read_only(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Welcome.", "Opening")
        meetings_db.close_session(
            meeting.meeting_id,
            invitee_id,
            MeetingSummary(closing_message="Thanks.").model_dump_json(),
        )

        app = _verified(_app(probe, meeting.meeting_id), meeting.meeting_id, invitee_id).run()

        assert not app.chat_input
        assert any("complete" in message.value for message in app.success)
