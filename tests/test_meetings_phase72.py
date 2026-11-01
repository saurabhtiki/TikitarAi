"""Phase 72: remove a reference document (upload or link) from meeting setup."""

import pytest

from meetings import db as meetings_db
from meetings import storage
from meetings.exceptions import MeetingStorageError
from tests.test_meetings_phase67 import _add_ref, _setup


def test_removing_an_upload_deletes_the_row_and_the_file(tmp_path, monkeypatch):
    meeting, _ = _setup(tmp_path, monkeypatch)
    file_id = _add_ref(tmp_path, meeting, "CaCl2_Tech_Grade.pdf")
    [record] = meetings_db.list_files(meeting.meeting_id)

    filepath = meetings_db.delete_ref_file(meeting.meeting_id, 1, file_id)
    storage.delete_file(filepath)

    assert filepath == record["filepath"]
    assert meetings_db.list_files(meeting.meeting_id) == []
    assert not (tmp_path / "files" / str(meeting.meeting_id) / "ref_docs" / "CaCl2_Tech_Grade.pdf").exists()


def test_removing_a_link_returns_no_filepath(tmp_path, monkeypatch):
    meeting, _ = _setup(tmp_path, monkeypatch)
    file_id = meetings_db.add_link(meeting.meeting_id, 1, "Product demo", "https://youtu.be/a")
    assert meetings_db.delete_ref_file(meeting.meeting_id, 1, file_id) == ""
    assert meetings_db.list_files(meeting.meeting_id) == []


def test_a_file_already_missing_on_disk_still_removes(tmp_path, monkeypatch):
    meeting, _ = _setup(tmp_path, monkeypatch)
    file_id = meetings_db.add_file(meeting.meeting_id, "gone.pdf", str(tmp_path / "gone.pdf"))
    storage.delete_file(meetings_db.delete_ref_file(meeting.meeting_id, 1, file_id))
    assert meetings_db.list_files(meeting.meeting_id) == []


def test_only_the_owner_and_only_reference_documents(tmp_path, monkeypatch):
    meeting, invitee_id = _setup(tmp_path, monkeypatch)
    ref_id = _add_ref(tmp_path, meeting, "price.pdf")
    own_id = meetings_db.add_file(meeting.meeting_id, "scan.pdf", "scan.pdf", invitee_id=invitee_id)

    with pytest.raises(MeetingStorageError):
        meetings_db.delete_ref_file(meeting.meeting_id, 999, ref_id)
    with pytest.raises(MeetingStorageError):
        meetings_db.delete_ref_file(meeting.meeting_id, 1, own_id)

    assert len(meetings_db.list_files(meeting.meeting_id)) == 1
    assert len(meetings_db.list_files(meeting.meeting_id, invitee_id)) == 1


def test_organiser_removes_a_document_after_confirming(tmp_path, monkeypatch):
    from tests.test_meetings_phase57 import _organiser_app

    meeting, _ = _setup(tmp_path, monkeypatch)
    keep_id = meetings_db.add_link(meeting.meeting_id, 1, "Price list", "https://example.com/p")
    gone_id = _add_ref(tmp_path, meeting, "CaCl2_Tech_Grade.pdf")
    app = _organiser_app(meeting.meeting_id).run()
    assert not app.exception

    app.button(key=f"meetings_refdoc_remove_yes_{gone_id}").click().run()

    assert not app.exception
    assert [record["file_id"] for record in meetings_db.list_files(meeting.meeting_id)] == [keep_id]
    assert not (tmp_path / "files" / str(meeting.meeting_id) / "ref_docs" / "CaCl2_Tech_Grade.pdf").exists()
