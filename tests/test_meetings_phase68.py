"""Phase 68: reference documents that are links (YouTube, Drive, web pages) instead of uploads."""

import sqlite3
from pathlib import Path

import pytest

from meetings import chat_agent, guide, show_files
from meetings import db as meetings_db
from meetings.exceptions import MeetingStorageError
from meetings.model import SENDER_AI, Meeting
from tests.test_meetings_phase67 import _invitee_app, _setup

# --------------------------------------------------------------------------------------
# show_files — pure helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://www.youtube.com/watch?v=abc123", show_files.VIDEO_KIND),
        ("https://youtu.be/abc123", show_files.VIDEO_KIND),
        ("https://YouTube.com/watch?v=abc123", show_files.VIDEO_KIND),
        ("https://cdn.example.com/demo.MP4?sig=1", show_files.VIDEO_KIND),
        ("https://cdn.example.com/plan.png", show_files.IMAGE_KIND),
        ("https://drive.google.com/file/d/xyz/view", show_files.LINK_KIND),
        ("https://example.com/brochure.pdf", show_files.LINK_KIND),
        ("https://example.com/", show_files.LINK_KIND),
    ],
)
def test_link_kind(url, kind):
    assert show_files.link_kind(url) == kind


def test_record_kind_and_shows_cover_uploads_and_links():
    assert show_files.record_kind({"filename": "Product demo", "url": "https://youtu.be/a"}) == show_files.VIDEO_KIND
    assert show_files.record_kind({"filename": "prices.xlsx", "url": ""}) == show_files.OTHER_KIND
    assert show_files.shows({"filename": "Price list", "url": "https://drive.google.com/x"})
    assert not show_files.shows({"filename": "prices.xlsx"})


def test_link_problems():
    files = [{"file_id": 1, "filename": "Product demo"}]
    assert show_files.link_problems("Intro", "https://youtu.be/a", files) == []
    assert show_files.link_problems(" product  DEMO ", "https://youtu.be/a", files) == [
        "product DEMO is already in the list — use another name."
    ]
    assert show_files.link_problems("", "https://youtu.be/a", files) == ["Give the link a name, e.g. 'Product demo'."]
    for bad in ("ftp://x.com/a", "javascript:alert(1)", "https://", "youtu.be/a", ""):
        assert show_files.link_problems("Intro", bad, files) == [
            "The link must start with http:// or https://, e.g. https://youtu.be/abc123."
        ], bad
    assert "too long" in show_files.link_problems("Intro", "https://x.com/" + "a" * 2000, files)[0]


def test_prompt_and_how_to_use_treat_a_link_as_showable():
    link = {"file_id": 1, "filename": "Product demo", "url": "https://youtu.be/a", "description": "Demo video"}
    text = chat_agent.system_instructions(Meeting(subject="Sales"), ref_files=[link])
    assert "- Product demo (can show) — Demo video" in text
    tip = "Ask to see a document, e.g. 'Show me the product demo' — it opens right in the chat."
    assert tip in guide.default_steps(Meeting(subject="Sales"), {}, {}, [link])


def test_agenda_note_naming_a_link_shows_it_once():
    files = [{"file_id": 7, "filename": "Product demo", "url": "https://youtu.be/a"}]
    note = "Show Product demo, then ask what they think"
    assert show_files.file_for_step(note, files, set())["file_id"] == 7
    assert show_files.file_for_step(note, files, {7}) is None


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------


def test_old_database_gains_url(tmp_path):
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "CREATE TABLE meeting_files (file_id INTEGER PRIMARY KEY, meeting_id INTEGER, invitee_id INTEGER, "
            "filename TEXT, filepath TEXT, uploaded_at TEXT);"
        )
    meetings_db.init_meetings_tables(db_path)
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(meeting_files);")}
    assert "url" in columns


def test_add_link_round_trip_and_owner_only(tmp_path, monkeypatch):
    meeting, _ = _setup(tmp_path, monkeypatch)
    meetings_db.add_link(meeting.meeting_id, 1, "  Product   demo ", " https://youtu.be/a ")
    [record] = meetings_db.list_files(meeting.meeting_id)
    assert (record["filename"], record["url"], record["filepath"]) == ("Product demo", "https://youtu.be/a", "")
    with pytest.raises(MeetingStorageError):
        meetings_db.add_link(meeting.meeting_id, 999, "Hack", "https://evil.example")
    assert len(meetings_db.list_files(meeting.meeting_id)) == 1


# --------------------------------------------------------------------------------------
# Invitee page
# --------------------------------------------------------------------------------------


def test_invitee_chat_plays_youtube_and_opens_other_links(tmp_path, monkeypatch):
    meeting, invitee_id = _setup(tmp_path, monkeypatch)
    video_id = meetings_db.add_link(meeting.meeting_id, 1, "Product demo", "https://youtu.be/abc123")
    page_id = meetings_db.add_link(meeting.meeting_id, 1, "Price list", "https://drive.google.com/file/d/xyz/view")
    for text, file_id in (("Here is our demo:", video_id), ("Here:", page_id), ("Again:", page_id)):
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, text, "", shown_file_id=file_id)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent))

    app = _invitee_app(tmp_path, meeting, invitee_id).run()

    assert not app.exception
    assert [video.proto.url for video in app.get("video")] == ["https://youtu.be/abc123"]
    labels = [button.proto.label for button in app.get("link_button")]
    # Twice under the chat (same link shown twice), once in the Reference documents box each.
    assert labels.count("Open Price list") == 2
    assert {"Product demo", "Price list"} <= set(labels)


# --------------------------------------------------------------------------------------
# Organiser: Overview → Add a link
# --------------------------------------------------------------------------------------


def test_organiser_adds_a_link_and_a_bad_one_is_refused(tmp_path, monkeypatch):
    from tests.test_meetings_phase57 import _organiser_app

    meeting, _ = _setup(tmp_path, monkeypatch)
    app = _organiser_app(meeting.meeting_id).run()
    assert not app.exception

    app.text_input(key=f"meetings_reflink_name_{meeting.meeting_id}").input("Product demo")
    app.text_input(key=f"meetings_reflink_url_{meeting.meeting_id}").input("youtu.be/abc")
    app.button(key=f"meetings_reflink_add_{meeting.meeting_id}").click().run()
    assert any("must start with http://" in error.value for error in app.error)
    assert meetings_db.list_files(meeting.meeting_id) == []

    app.text_input(key=f"meetings_reflink_url_{meeting.meeting_id}").input("https://youtu.be/abc")
    app.button(key=f"meetings_reflink_add_{meeting.meeting_id}").click().run()
    assert not app.exception
    [record] = meetings_db.list_files(meeting.meeting_id)
    assert (record["filename"], record["url"]) == ("Product demo", "https://youtu.be/abc")
    assert "Product demo" in [button.proto.label for button in app.get("link_button")]
