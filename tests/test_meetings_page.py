"""AppTest coverage for the creator's Meetings page (requirement 6.7, Phase 1, spec 1 & 8).

The Share screen assertions matter most here. While automated sending is undecided, that
screen is the *only* way an invitation reaches anybody — a link or code missing from it
means an invitee who can never join, with nothing on screen to say so.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access
from meetings import db as meetings_db
from meetings.model import SENDER_AI, SENDER_USER, AgendaItem, Meeting
from meetings.summary_agent import AgendaItemSummary, MeetingSummary

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "meetings.py")


def _app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)

    app = AppTest.from_file(PAGE_PATH, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    return app


def _seed_meeting(with_invitee=True):
    meeting = meetings_db.create_meeting(
        1,
        Meeting(
            subject="PO No 123",
            meeting_context="Resolve open issues.",
            persona="You are the Purchase Manager.",
            context_sop="Never agree to a discount.",
            agenda=[AgendaItem(item="Delivery timeline", ai_note="SLA is 30 days.")],
        ),
    )
    invitee_id = None
    if with_invitee:
        invitee_id = meetings_db.add_invitee(
            meeting.meeting_id, 1, "Raj", "raj@vendor.com", "token-abc", access.encrypt_code("123456")
        )
    return meeting, invitee_id


class TestList:
    def test_an_account_with_no_meetings_is_told_so(self, tmp_path, monkeypatch):
        app = _app(tmp_path, monkeypatch).run()
        assert any("haven't created any meetings" in info.value for info in app.info)

    def test_a_meeting_is_listed_with_its_progress(self, tmp_path, monkeypatch):
        app = _app(tmp_path, monkeypatch)
        meeting, invitee_id = _seed_meeting()
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        app.run()

        assert any("PO No 123" in markdown.value for markdown in app.markdown)
        assert any("0 of 1 invitee" in caption.value for caption in app.caption)

    def test_another_accounts_meeting_is_not_listed(self, tmp_path, monkeypatch):
        app = _app(tmp_path, monkeypatch)
        _seed_meeting()
        app.session_state["user_id"] = 2
        app.run()

        assert not any("PO No 123" in markdown.value for markdown in app.markdown)


class TestDetail:
    def _open(self, tmp_path, monkeypatch, **kwargs):
        app = _app(tmp_path, monkeypatch)
        meeting, invitee_id = _seed_meeting(**kwargs)
        app.session_state["meeting_open_id"] = meeting.meeting_id
        app.run()
        return app, meeting, invitee_id

    def test_the_setup_is_shown_back(self, tmp_path, monkeypatch):
        app, _, _ = self._open(tmp_path, monkeypatch)
        text = " ".join(element.value for element in app.markdown)

        assert "You are the Purchase Manager." in text or any(
            "You are the Purchase Manager." in element.value for element in app.markdown
        )
        assert any("Delivery timeline" in element.value for element in app.markdown)

    def test_an_invitee_who_has_not_started_shows_as_not_started(self, tmp_path, monkeypatch):
        app, _, _ = self._open(tmp_path, monkeypatch)
        assert any("Not started" in caption.value for caption in app.caption)

    def test_the_share_screen_gives_a_link_and_a_code_per_invitee(self, tmp_path, monkeypatch):
        # This screen is the only delivery mechanism there is right now.
        app, meeting, _ = self._open(tmp_path, monkeypatch)
        codes = [element.value for element in app.code]

        assert any(f"m={meeting.meeting_id}" in value and "t=token-abc" in value for value in codes)
        assert "123456" in codes

    def test_agenda_coverage_counts_what_was_actually_discussed(self, tmp_path, monkeypatch):
        app, meeting, invitee_id = self._open(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Welcome", "Opening")
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_USER, "45 days", "Delivery timeline")
        app.run()

        # The opening message names every item without discussing any, so it must not count.
        assert any("1 of 1 agenda item" in caption.value for caption in app.caption)

    def test_a_finished_invitees_summary_is_rendered(self, tmp_path, monkeypatch):
        app, meeting, invitee_id = self._open(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.close_session(
            meeting.meeting_id,
            invitee_id,
            MeetingSummary(
                agenda_items=[AgendaItemSummary(item="Delivery timeline", discussed=True, notes="45 days quoted.")],
                closing_message="Thanks.",
            ).model_dump_json(),
        )
        app.run()

        assert any("45 days quoted." in element.value for element in app.markdown)

    def test_generating_a_status_stores_a_snapshot(self, tmp_path, monkeypatch):
        app, meeting, invitee_id = self._open(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_USER, "45 days", "Delivery timeline")

        import app_pages.meetings as page

        monkeypatch.setattr(
            page.summary_agent,
            "generate_summary",
            lambda *a, **k: MeetingSummary(
                agenda_items=[AgendaItemSummary(item="Delivery timeline", discussed=True, notes="In progress.")]
            ),
        )
        app.run()
        app.button(key=f"meetings_status_{invitee_id}").click().run()

        stored = meetings_db.list_invitees(meeting.meeting_id, 1)[0]
        assert stored["live_status_json"] is not None
        assert "In progress." in stored["live_status_json"]
