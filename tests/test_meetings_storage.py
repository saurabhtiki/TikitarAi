"""Where a meeting's files land on disk (requirement 6.7, Phase 1, spec 7).

The sanitisation tests matter more than they look: an uploaded filename is attacker-supplied
text that gets joined onto a path, and an invitee folder is meant to be private to one
person.
"""

import pytest

from meetings.exceptions import MeetingStorageError
from meetings.storage import (
    invitee_folder,
    read_file,
    ref_docs_folder,
    sanitize_filename,
    save_upload,
)


class TestSanitisation:
    def test_an_ordinary_name_is_left_alone(self):
        assert sanitize_filename("invoice_scan.pdf") == "invoice_scan.pdf"

    def test_a_traversal_attempt_cannot_escape_the_folder(self):
        # The directory part is dropped entirely rather than escaped: an upload's name is
        # never meant to carry a location.
        assert "/" not in sanitize_filename("../../etc/passwd")
        assert "\\" not in sanitize_filename(r"..\..\windows\system32")
        assert not sanitize_filename("../../etc/passwd").startswith(".")

    def test_awkward_characters_become_underscores(self):
        assert sanitize_filename("my report (final);rm -rf.xlsx") == "my_report__final__rm_-rf.xlsx"

    def test_a_name_that_sanitises_to_nothing_still_gets_one(self):
        assert sanitize_filename("") == "upload"
        assert sanitize_filename("...") == "upload"


class TestPaths:
    def test_reference_documents_and_invitee_uploads_are_kept_apart(self, tmp_path):
        # Spec 2 makes an invitee's upload private to them, so these must never be the same
        # folder.
        shared = ref_docs_folder(7, root=tmp_path)
        private = invitee_folder(7, 3, root=tmp_path)

        assert shared != private
        assert shared.parent == private.parent

    def test_two_invitees_never_share_a_folder(self, tmp_path):
        assert invitee_folder(7, 3, root=tmp_path) != invitee_folder(7, 4, root=tmp_path)


class TestSaving:
    def test_a_file_is_written_where_it_says_it_is(self, tmp_path):
        path = save_upload(b"hello", "notes.txt", 7, root=tmp_path)

        assert path.read_bytes() == b"hello"
        assert path.parent == ref_docs_folder(7, root=tmp_path)

    def test_an_invitee_upload_goes_to_their_own_folder(self, tmp_path):
        path = save_upload(b"private", "scan.pdf", 7, invitee_id=3, root=tmp_path)
        assert path.parent == invitee_folder(7, 3, root=tmp_path)

    def test_a_repeated_name_is_suffixed_rather_than_overwritten(self, tmp_path):
        # Two invitees both uploading `scan.pdf` is ordinary; losing the first silently is
        # not an acceptable way to handle it.
        first = save_upload(b"one", "scan.pdf", 7, root=tmp_path)
        second = save_upload(b"two", "scan.pdf", 7, root=tmp_path)

        assert first != second
        assert first.read_bytes() == b"one"
        assert second.read_bytes() == b"two"


class TestReading:
    def test_a_stored_file_reads_back(self, tmp_path):
        path = save_upload(b"payload", "notes.txt", 7, root=tmp_path)
        assert read_file(path) == b"payload"

    def test_a_file_removed_from_disk_reports_rather_than_crashes(self, tmp_path):
        with pytest.raises(MeetingStorageError):
            read_file(tmp_path / "never_existed.txt")
