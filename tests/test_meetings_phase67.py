"""Phase 67: the organiser's photo as the bot's chat icon, and reference documents the bot
can show in the chat (picture, video, PDF, HTML) or only point to (Word, Excel...)."""

import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin, update_own_profile
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access, chat_agent, guide, show_files, storage
from meetings import db as meetings_db
from meetings.exceptions import MeetingStorageError
from meetings.model import SENDER_AI, Meeting

PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""
TOKEN = "token-67"
MB = 1024 * 1024


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------------------
# show_files — pure helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "kind"),
    [
        ("photo.PNG", show_files.IMAGE_KIND),
        ("pic.jpeg", show_files.IMAGE_KIND),
        ("demo.mp4", show_files.VIDEO_KIND),
        ("Brochure.PDF", show_files.PDF_KIND),
        ("page.htm", show_files.HTML_KIND),
        ("terms.docx", show_files.OTHER_KIND),
        ("prices.xlsx", show_files.OTHER_KIND),
        ("README", show_files.OTHER_KIND),
    ],
)
def test_file_kind(filename, kind):
    assert show_files.file_kind(filename) == kind


def test_upload_problems_only_limit_videos():
    assert show_files.upload_problems("demo.mp4", 60 * MB) == ["demo.mp4 is 60 MB — videos can be at most 50 MB."]
    assert show_files.upload_problems("brochure.pdf", 60 * MB) == []
    assert show_files.upload_problems("demo.mp4", 10 * MB) == []


def test_files_block_marks_and_describes():
    block = show_files.files_block(
        [
            {"filename": "demo.mp4", "description": "Product  demo video"},
            {"filename": "prices.xlsx", "description": ""},
        ]
    )
    assert block == "- demo.mp4 (can show) — Product demo video\n- prices.xlsx (refer only)"
    assert show_files.files_block([]) == ""


def test_pick_file_matches_loosely_and_refuses_invented_names():
    files = [{"file_id": 1, "filename": "Demo Video.mp4"}]
    assert show_files.pick_file("Demo Video.mp4", files)["file_id"] == 1
    assert show_files.pick_file("  demo   video.MP4 ", files)["file_id"] == 1
    assert show_files.pick_file("secret.mp4", files) is None
    assert show_files.pick_file("", files) is None


def test_file_for_step_finds_a_named_showable_file_once():
    files = [
        {"file_id": 1, "filename": "prices.xlsx"},
        {"file_id": 2, "filename": "Plan Layout.png"},
    ]
    note = "Show plan layout.PNG, then ask what they think"
    assert show_files.file_for_step(note, files, set())["file_id"] == 2
    assert show_files.file_for_step(note, files, {2}) is None  # already shown in this chat
    assert show_files.file_for_step("Discuss prices.xlsx", files, set()) is None  # refer only
    assert show_files.file_for_step("", files, set()) is None


def test_file_for_step_matches_whole_names_only():
    files = [{"file_id": 1, "filename": "plan.png"}, {"file_id": 2, "filename": "floorplan.png"}]
    assert show_files.file_for_step("Show floorplan.png, then discuss", files, set())["file_id"] == 2
    assert show_files.file_for_step("Show plan.png.", files, set())["file_id"] == 1


def test_prompt_says_to_show_a_step_file_with_a_short_line():
    meeting = Meeting(subject="Sales")
    text = chat_agent.system_instructions(meeting, ref_files=[{"filename": "demo.mp4", "description": "Demo"}])
    assert "note says to show" in text
    assert "Showing our product demo:" in text


# --------------------------------------------------------------------------------------
# Prompt and How to use
# --------------------------------------------------------------------------------------


def test_prompt_lists_files_only_when_there_are_some():
    meeting = Meeting(subject="Sales")
    with_files = chat_agent.system_instructions(meeting, ref_files=[{"filename": "demo.mp4", "description": "Demo"}])
    assert "- demo.mp4 (can show) — Demo" in with_files
    assert "show_file" in with_files
    assert "Reference documents the organiser shared" not in chat_agent.system_instructions(meeting)


def test_how_to_use_mentions_showing_only_for_showable_files():
    meeting = Meeting(subject="Sales")
    tip = "Ask to see a document, e.g. 'Show me the product demo' — it opens right in the chat."
    assert tip in guide.default_steps(meeting, {}, {}, [{"filename": "demo.mp4"}])
    assert tip not in guide.default_steps(meeting, {}, {}, [{"filename": "prices.xlsx"}])
    assert tip not in guide.default_steps(meeting, {}, {})


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)
    meeting = meetings_db.create_meeting(1, Meeting(subject="Sales demo"))
    invitee_id = meetings_db.add_invitee(meeting.meeting_id, 1, "Sam", "s@x.com", TOKEN, access.encrypt_code("123456"))
    meetings_db.ensure_session(meeting.meeting_id, invitee_id)
    return meeting, invitee_id


def _add_ref(tmp_path, meeting, filename, data=b"x") -> int:
    path = storage.save_upload(data, filename, meeting.meeting_id, None, root=tmp_path / "files")
    return meetings_db.add_file(meeting.meeting_id, filename, str(path))


def test_old_database_gains_the_new_columns(tmp_path):
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "CREATE TABLE meeting_files (file_id INTEGER PRIMARY KEY, meeting_id INTEGER, invitee_id INTEGER, "
            "filename TEXT, filepath TEXT, uploaded_at TEXT);"
            "CREATE TABLE meeting_messages (message_id INTEGER PRIMARY KEY, meeting_id INTEGER, invitee_id INTEGER, "
            "sender TEXT, text TEXT, agenda_tag TEXT, created_at TEXT);"
        )
    meetings_db.init_meetings_tables(db_path)
    with sqlite3.connect(db_path) as connection:
        file_columns = {row[1] for row in connection.execute("PRAGMA table_info(meeting_files);")}
        message_columns = {row[1] for row in connection.execute("PRAGMA table_info(meeting_messages);")}
    assert "description" in file_columns
    assert "shown_file_id" in message_columns


def test_shown_file_and_descriptions_round_trip(tmp_path, monkeypatch):
    meeting, invitee_id = _setup(tmp_path, monkeypatch)
    file_id = _add_ref(tmp_path, meeting, "demo.mp4")
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Here it is:", "", shown_file_id=file_id)
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Plain reply", "")
    shown = [message.shown_file_id for message in meetings_db.list_messages(meeting.meeting_id, invitee_id)]
    assert shown == [file_id, None]

    meetings_db.save_file_descriptions(meeting.meeting_id, 1, {file_id: "  Product   demo "})
    assert meetings_db.list_files(meeting.meeting_id)[0]["description"] == "Product demo"
    with pytest.raises(MeetingStorageError):
        meetings_db.save_file_descriptions(meeting.meeting_id, 999, {file_id: "Hacked"})
    assert meetings_db.list_files(meeting.meeting_id)[0]["description"] == "Product demo"


# --------------------------------------------------------------------------------------
# Invitee page
# --------------------------------------------------------------------------------------


def _invitee_app(tmp_path, meeting, invitee_id):
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    app = AppTest.from_file(str(probe), default_timeout=60)
    app.session_state["probe_meeting_id"] = meeting.meeting_id
    app.session_state["probe_token"] = TOKEN
    app.session_state["invitee_verified"] = True
    app.session_state["invitee_meeting_id"] = meeting.meeting_id
    app.session_state["invitee_id"] = invitee_id
    return app


def test_invitee_chat_shows_picture_and_organiser_photo(tmp_path, monkeypatch):
    meeting, invitee_id = _setup(tmp_path, monkeypatch)
    photo = tmp_path / "me.png"
    photo.write_bytes(_png())
    update_own_profile(1, "Admin", str(photo))
    file_id = _add_ref(tmp_path, meeting, "chart.png", _png())
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Here is the chart:", "", shown_file_id=file_id)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent))

    app = _invitee_app(tmp_path, meeting, invitee_id).run()

    assert not app.exception
    # A photo file becomes a media link instead of the default "assistant" icon.
    assert app.chat_message[0].avatar.startswith("/mock/media/")
    assert [image.captions for image in app.get("image")] == [["chart.png"]]
    assert not any("no longer available" in caption.value for caption in app.caption)


def test_deleted_shown_file_says_so(tmp_path, monkeypatch):
    meeting, invitee_id = _setup(tmp_path, monkeypatch)
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Here:", "", shown_file_id=12345)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent))

    app = _invitee_app(tmp_path, meeting, invitee_id).run()

    assert not app.exception
    assert any("This file is no longer available." in caption.value for caption in app.caption)
