"""Meetings, invitees, sessions and messages in SQLite (requirement 6.7, Phase 1).

Two things are load-bearing here. The **ownership** assertions, as in `test_checks_db.py`:
a meeting carries a persona, an agenda and a whole conversation, and one readable by the
wrong account would be a leak. And the **token** assertions: the invitee side has no user_id
to scope by, so the token is the only thing standing between one invitee and another's chat.
"""

import pytest

from auth.db import create_user, init_db, seed_default_admin
from meetings.db import (
    add_invitee,
    add_message,
    close_session,
    create_meeting,
    ensure_session,
    find_contact,
    get_default_persona,
    get_session_summary,
    init_meetings_tables,
    list_invitees,
    list_meetings,
    list_messages,
    list_messages_for_creator,
    load_meeting,
    load_meeting_for_invitee,
    remember_contact,
    resolve_token,
    save_live_status,
    set_default_persona,
    update_running_summary,
)
from meetings.exceptions import MeetingStorageError
from meetings.model import SENDER_AI, SENDER_USER, AgendaItem, Meeting

OWNER = 1
OTHER_USER = 2


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    create_user("second@example.com", "Second", "password123", "normal_user", path)
    init_meetings_tables(path)
    return path


def _meeting() -> Meeting:
    return Meeting(
        subject="PO No 123",
        meeting_context="Resolve open issues on PO 123.",
        persona="You are the Purchase Manager.",
        context_sop="Never agree to a discount.",
        agenda=[AgendaItem(item="Delivery timeline", ai_note="SLA is 30 days.")],
    )


def _seed_meeting_with_invitee(db_path):
    meeting = create_meeting(OWNER, _meeting(), db_path=db_path)
    invitee_id = add_invitee(
        meeting.meeting_id, OWNER, "Raj", "raj@vendor.com", "token-abc", "encrypted", db_path=db_path
    )
    return meeting, invitee_id


class TestMeetings:
    def test_a_saved_meeting_comes_back_whole(self, db_path):
        saved = create_meeting(OWNER, _meeting(), db_path=db_path)
        loaded = load_meeting(saved.meeting_id, OWNER, db_path=db_path)

        assert loaded.subject == "PO No 123"
        assert loaded.persona == "You are the Purchase Manager."
        assert loaded.context_sop == "Never agree to a discount."
        assert loaded.agenda_titles() == ["Delivery timeline"]
        assert loaded.agenda[0].ai_note == "SLA is 30 days."

    def test_a_blank_subject_is_refused(self, db_path):
        with pytest.raises(MeetingStorageError):
            create_meeting(OWNER, Meeting(subject="   "), db_path=db_path)

    def test_another_account_cannot_open_it(self, db_path):
        saved = create_meeting(OWNER, _meeting(), db_path=db_path)
        with pytest.raises(MeetingStorageError):
            load_meeting(saved.meeting_id, OTHER_USER, db_path=db_path)

    def test_the_list_only_shows_your_own(self, db_path):
        create_meeting(OWNER, _meeting(), db_path=db_path)
        assert len(list_meetings(OWNER, db_path=db_path)) == 1
        assert list_meetings(OTHER_USER, db_path=db_path) == []

    def test_the_list_counts_invitees_and_who_has_finished(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        add_invitee(meeting.meeting_id, OWNER, "Sam", "sam@x.com", "token-two", "enc", db_path=db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        close_session(meeting.meeting_id, invitee_id, '{"agenda_items": []}', db_path=db_path)

        row = list_meetings(OWNER, db_path=db_path)[0]
        assert row["invitee_count"] == 2
        assert row["closed_count"] == 1


class TestInvitees:
    def test_a_token_resolves_to_its_own_invitee(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        resolved = resolve_token("token-abc", db_path=db_path)

        assert resolved["invitee_id"] == invitee_id
        # The meeting comes from the token, never from the URL — an id edited by hand must
        # not be able to open a different meeting.
        assert resolved["meeting_id"] == meeting.meeting_id

    def test_an_unknown_token_resolves_to_nothing(self, db_path):
        _seed_meeting_with_invitee(db_path)
        assert resolve_token("not-a-real-token", db_path=db_path) is None
        assert resolve_token("", db_path=db_path) is None

    def test_another_account_cannot_add_invitees_to_your_meeting(self, db_path):
        meeting = create_meeting(OWNER, _meeting(), db_path=db_path)
        with pytest.raises(MeetingStorageError):
            add_invitee(meeting.meeting_id, OTHER_USER, "Mallory", "m@x.com", "tok", "enc", db_path=db_path)

    def test_an_invitee_who_never_joined_still_lists_with_no_session(self, db_path):
        meeting, _ = _seed_meeting_with_invitee(db_path)
        row = list_invitees(meeting.meeting_id, OWNER, db_path=db_path)[0]

        assert row["name"] == "Raj"
        assert row["closed"] is None
        assert row["last_active_at"] is None


class TestMessages:
    def test_messages_come_back_in_order_with_their_tags(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Hello", "Opening", db_path=db_path)
        add_message(meeting.meeting_id, invitee_id, SENDER_USER, "Hi", db_path=db_path)

        messages = list_messages(meeting.meeting_id, invitee_id, db_path=db_path)
        assert [message.text for message in messages] == ["Hello", "Hi"]
        assert messages[0].agenda_tag == "Opening"
        assert messages[0].is_from_ai()

    def test_saving_a_message_marks_the_session_active(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        add_message(meeting.meeting_id, invitee_id, SENDER_USER, "Hi", db_path=db_path)

        row = list_invitees(meeting.meeting_id, OWNER, db_path=db_path)[0]
        assert row["last_active_at"] is not None

    def test_another_account_cannot_read_the_transcript(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        add_message(meeting.meeting_id, invitee_id, SENDER_USER, "Confidential", db_path=db_path)

        assert len(list_messages_for_creator(meeting.meeting_id, invitee_id, OWNER, db_path=db_path)) == 1
        with pytest.raises(MeetingStorageError):
            list_messages_for_creator(meeting.meeting_id, invitee_id, OTHER_USER, db_path=db_path)


class TestSessions:
    def test_ensuring_a_session_twice_does_not_duplicate_it(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        first = ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        add_message(meeting.meeting_id, invitee_id, SENDER_USER, "Hi", db_path=db_path)
        second = ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)

        assert first.closed is False
        assert second.closed is False
        assert len(list_messages(meeting.meeting_id, invitee_id, db_path=db_path)) == 1

    def test_closing_stores_the_summary_and_locks_it(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)

        close_session(meeting.meeting_id, invitee_id, '{"first": true}', db_path=db_path)
        # A double-submitted Close Chat must not replace a permanent record.
        close_session(meeting.meeting_id, invitee_id, '{"second": true}', db_path=db_path)

        assert get_session_summary(meeting.meeting_id, invitee_id, db_path=db_path) == '{"first": true}'
        assert ensure_session(meeting.meeting_id, invitee_id, db_path=db_path).closed is True

    def test_the_running_summary_and_its_cutoff_move_together(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        update_running_summary(meeting.meeting_id, invitee_id, "Discussed delivery.", 12, db_path=db_path)

        session = ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        assert session.running_summary == "Discussed delivery."
        assert session.folded_through_message_id == 12

    def test_a_live_status_is_overwritten_rather_than_accumulated(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)

        save_live_status(meeting.meeting_id, invitee_id, OWNER, '{"n": 1}', db_path=db_path)
        save_live_status(meeting.meeting_id, invitee_id, OWNER, '{"n": 2}', db_path=db_path)

        row = list_invitees(meeting.meeting_id, OWNER, db_path=db_path)[0]
        assert row["live_status_json"] == '{"n": 2}'

    def test_another_account_cannot_write_a_live_status(self, db_path):
        meeting, invitee_id = _seed_meeting_with_invitee(db_path)
        ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
        with pytest.raises(MeetingStorageError):
            save_live_status(meeting.meeting_id, invitee_id, OTHER_USER, '{"n": 1}', db_path=db_path)


class TestInviteeSideReads:
    def test_a_meeting_loads_for_an_invitee_without_a_user_id(self, db_path):
        meeting, _ = _seed_meeting_with_invitee(db_path)
        loaded = load_meeting_for_invitee(meeting.meeting_id, db_path=db_path)

        assert loaded.subject == "PO No 123"
        # The creator comes back too — the invitee's chat runs on their model profile.
        assert loaded.created_by == OWNER

    def test_a_missing_meeting_is_reported_rather_than_returned_empty(self, db_path):
        with pytest.raises(MeetingStorageError):
            load_meeting_for_invitee(9999, db_path=db_path)


class TestDefaultsAndContacts:
    def test_a_default_persona_round_trips_and_updates(self, db_path):
        assert get_default_persona(OWNER, db_path=db_path) == ""
        set_default_persona(OWNER, "You are the Purchase Manager.", db_path=db_path)
        set_default_persona(OWNER, "You are the Sales Manager.", db_path=db_path)

        assert get_default_persona(OWNER, db_path=db_path) == "You are the Sales Manager."

    def test_a_contact_is_remembered_once_and_not_renamed_afterwards(self, db_path):
        remember_contact("raj@vendor.com", "Raj", OWNER, db_path=db_path)
        # A second creator's typo must not rename a contact for everybody.
        remember_contact("raj@vendor.com", "Rajj", OTHER_USER, db_path=db_path)

        assert find_contact("raj@vendor.com", db_path=db_path)["name"] == "Raj"

    def test_an_incomplete_contact_is_not_stored(self, db_path):
        remember_contact("", "Nobody", OWNER, db_path=db_path)
        remember_contact("someone@x.com", "", OWNER, db_path=db_path)

        assert find_contact("someone@x.com", db_path=db_path) is None
