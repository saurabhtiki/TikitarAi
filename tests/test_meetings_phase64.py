"""Phase 64: voice in the invitee chat — 🎤 Speak fills the chat box, 🔊 reads a reply aloud."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from app_pages.voice_controls import heard_text, spoken_text
from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access, guide
from meetings import db as meetings_db
from meetings.model import QUESTION_ITEM, SENDER_AI, SENDER_USER, AgendaItem, Meeting

TOKEN = "token-64"
PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""
SPEAK_PROBE = """
import streamlit as st
from app_pages.voice_controls import speak_button

speak_button("chat_box", key="speak")
st.chat_input("Type your reply", key="chat_box")
"""


class TestHelpers:
    def test_heard_text_is_tidied(self):
        assert heard_text("  I can join on Monday ") == "I can join on Monday"

    def test_blank_or_missing_heard_text_is_ignored(self):
        assert heard_text("   ") == ""
        assert heard_text(None) == ""

    def test_spoken_text_drops_markdown(self):
        assert spoken_text("**Question 2 of 5:** what is your `notice` period?") == (
            "Question 2 of 5:  what is your  notice  period?"
        )
        assert spoken_text("See [the policy](http://x.com)") == "See the policy"

    def test_spoken_text_keeps_underscores_and_comparisons(self):
        assert spoken_text("Invoice INV_102: amounts > 60000 need approval") == (
            "Invoice INV_102: amounts > 60000 need approval"
        )
        assert spoken_text("> Quoted __note__") == "Quoted  note"


class TestHowToUse:
    def test_default_steps_mention_voice(self):
        meeting = Meeting(subject="Hiring", agenda=[AgendaItem(item="Salary", item_type=QUESTION_ITEM)])
        text = guide.numbered(guide.default_steps(meeting, {}, {}))
        assert text.startswith("1. Type your reply")
        assert "🎤 Speak" in text and "🔊" in text and "Google" in text


FILL_PROBE = """
from types import SimpleNamespace
import streamlit as st
from app_pages.voice_controls import fill_chat_box

st.session_state["speak"] = SimpleNamespace(heard=st.session_state["probe_heard"])
fill_chat_box("speak", "chat_box")
st.chat_input("Type your reply", key="chat_box")
"""


class TestSpeakFillsChatBox:
    def _run(self, tmp_path, heard):
        probe = tmp_path / "fill_probe.py"
        probe.write_text(FILL_PROBE, encoding="utf-8")
        app = AppTest.from_file(str(probe), default_timeout=30)
        app.session_state["probe_heard"] = heard
        return app.run()

    def test_heard_text_goes_into_the_chat_box(self, tmp_path):
        app = self._run(tmp_path, "  I can join on Monday ")
        assert not app.exception
        # The box shows the text, ready for the invitee to check and press Enter.
        assert app.chat_input[0].proto.value == "I can join on Monday"

    def test_blank_heard_text_leaves_the_box_alone(self, tmp_path):
        app = self._run(tmp_path, "   ")
        assert not app.exception
        assert app.chat_input[0].proto.value == ""

    def test_speak_button_mounts_before_the_chat_box(self, tmp_path):
        probe = tmp_path / "speak_probe.py"
        probe.write_text(SPEAK_PROBE, encoding="utf-8")
        app = AppTest.from_file(str(probe), default_timeout=30).run()
        assert not app.exception
        assert len(app.get("bidi_component")) == 1


class TestInviteeChat:
    def _setup(self, tmp_path, monkeypatch):
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
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_USER, "Hi", "")
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "When can you deliver?", "")
        probe = tmp_path / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        app = AppTest.from_file(str(probe), default_timeout=60)
        app.session_state["probe_meeting_id"] = meeting.meeting_id
        app.session_state["probe_token"] = TOKEN
        app.session_state["invitee_verified"] = True
        app.session_state["invitee_meeting_id"] = meeting.meeting_id
        app.session_state["invitee_id"] = invitee_id
        return app

    def test_speak_and_one_listen_per_bot_reply(self, tmp_path, monkeypatch):
        app = self._setup(tmp_path, monkeypatch).run()
        assert not app.exception
        # 2 bot replies → 2 Listen buttons, plus 1 Speak button; the invitee's own message has none.
        assert len(app.get("bidi_component")) == 3
        assert len(app.chat_input) == 1


def test_help_page_mentions_voice():
    help_page = Path(__file__).resolve().parent.parent / "static" / "help" / "meetings.html"
    assert "🎤 Speak" in help_page.read_text(encoding="utf-8")
