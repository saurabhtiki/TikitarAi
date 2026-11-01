"""Phase 66: Speak warns it replaces the box, voice is always English, the old note clears."""

import json
import shutil
import subprocess

import pytest
from streamlit.testing.v1 import AppTest

from app_pages import voice_controls
from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access
from meetings import db as meetings_db
from meetings.model import SENDER_AI, SENDER_USER, AgendaItem, Meeting

TOKEN = "token-66"
PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""


def _speak_data(app) -> dict:
    """The data of the 🎤 Speak button (the only component with a `turn`)."""
    found = [json.loads(component.proto.json or "{}") for component in app.get("bidi_component")]
    return next(data for data in found if "turn" in data)


class TestSpeakTurn:
    def test_speak_gets_the_message_count(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        init_db()
        seed_default_admin()
        init_llm_table()
        meetings_db.init_meetings_tables()
        profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
        set_default_model(profile["profile_id"], 1)
        meeting = meetings_db.create_meeting(1, Meeting(subject="Vendor", agenda=[AgendaItem(item="Delivery")]))
        invitee_id = meetings_db.add_invitee(
            meeting.meeting_id, 1, "Ravi", "r@x.com", TOKEN, access.encrypt_code("123456")
        )
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Hello Ravi!", "")
        probe = tmp_path / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        app = AppTest.from_file(str(probe), default_timeout=60)
        app.session_state["probe_meeting_id"] = meeting.meeting_id
        app.session_state["probe_token"] = TOKEN
        app.session_state["invitee_verified"] = True
        app.session_state["invitee_meeting_id"] = meeting.meeting_id
        app.session_state["invitee_id"] = invitee_id

        app.run()
        assert not app.exception
        assert _speak_data(app)["turn"] == 1

        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_USER, "Hi", "")
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "When can you deliver?", "")
        app.run()
        # The count changed, so the browser clears the old "Check the text…" note.
        assert _speak_data(app)["turn"] == 3

    def test_listening_note_warns_it_replaces_the_box(self):
        assert "replaces what's in the box" in voice_controls.LISTENING_NOTE
        assert "replace what is in the chat box" in voice_controls._SPEAK_HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the browser rule")
@pytest.mark.parametrize(
    ("browser", "expected"),
    [("en-IN", "en-IN"), ("en-GB", "en-GB"), ("fr-FR", "en-US"), ("hi-IN", "en-US"), ("", "en-US")],
)
def test_voice_is_always_english(browser, expected):
    script = (
        # defineProperty, not assignment: newer Node has its own read-only `navigator`.
        f"Object.defineProperty(globalThis, 'navigator', {{ value: {{ language: {json.dumps(browser)} }} }});"
        + voice_controls._ENGLISH_JS
        + "console.log(voiceLanguage())"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    assert result.stdout.strip() == expected


def test_both_voice_parts_use_the_english_rule():
    for source in (voice_controls._SPEAK_JS, voice_controls._LISTEN_JS):
        assert source.startswith(voice_controls._ENGLISH_JS)
        assert "lang = voiceLanguage()" in source
