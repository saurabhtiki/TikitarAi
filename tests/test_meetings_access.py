"""Token and access-code verification (requirement 6.7, Phase 1, spec 2).

The lockout tests are the point of this file. The token is 24 random bytes and not worth
attacking; the realistic threat is someone holding a forwarded link guessing a six-digit
code, and the only thing standing in their way is the attempt counter below.
"""

from datetime import datetime, timedelta, timezone

import pytest

from auth.db import create_user, init_db, seed_default_admin
from meetings.access import (
    ACCESS_CODE_LENGTH,
    LOCKOUT_MINUTES,
    MAX_ATTEMPTS,
    decrypt_code,
    encrypt_code,
    generate_access_code,
    generate_token,
    lockout_remaining,
    verify_code,
)
from meetings.db import add_invitee, create_meeting, init_meetings_tables, resolve_token
from meetings.model import Meeting

OWNER = 1
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def key_path(tmp_path):
    return tmp_path / "encryption.key"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    init_meetings_tables(path)
    return path


@pytest.fixture
def invitee(db_path, key_path):
    meeting = create_meeting(OWNER, Meeting(subject="PO No 123"), db_path=db_path)
    add_invitee(
        meeting.meeting_id,
        OWNER,
        "Raj",
        "raj@vendor.com",
        "token-abc",
        encrypt_code("123456", key_path=key_path),
        db_path=db_path,
    )
    return resolve_token("token-abc", db_path=db_path)


def _reread(db_path):
    """The invitee row as it now stands, lockout columns included."""
    return resolve_token("token-abc", db_path=db_path)


class TestGeneration:
    def test_tokens_are_unguessable_and_unique(self):
        tokens = {generate_token() for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(token) > 20 for token in tokens)

    def test_an_access_code_is_the_expected_number_of_digits(self):
        code = generate_access_code()
        assert len(code) == ACCESS_CODE_LENGTH
        assert code.isdigit()


class TestEncryption:
    def test_a_code_can_be_read_back(self, key_path):
        # Encrypted rather than hashed on purpose: the creator's Share screen has to be able
        # to show the code again while automated sending is undecided.
        assert decrypt_code(encrypt_code("654321", key_path=key_path), key_path=key_path) == "654321"


class TestVerification:
    def test_the_right_code_unlocks(self, invitee, db_path, key_path):
        ok, reason = verify_code(invitee, "123456", now=NOW, db_path=db_path, key_path=key_path)
        assert ok is True
        assert reason == ""

    def test_surrounding_whitespace_is_forgiven(self, invitee, db_path, key_path):
        ok, _ = verify_code(invitee, "  123456 ", now=NOW, db_path=db_path, key_path=key_path)
        assert ok is True

    def test_a_wrong_code_counts_an_attempt_and_says_how_many_are_left(self, invitee, db_path, key_path):
        ok, reason = verify_code(invitee, "000000", now=NOW, db_path=db_path, key_path=key_path)

        assert ok is False
        assert f"{MAX_ATTEMPTS - 1} attempt" in reason
        assert _reread(db_path)["failed_attempts"] == 1

    def test_an_empty_code_is_refused_without_counting_an_attempt(self, invitee, db_path, key_path):
        ok, _ = verify_code(invitee, "   ", now=NOW, db_path=db_path, key_path=key_path)

        assert ok is False
        assert _reread(db_path)["failed_attempts"] == 0

    def test_the_last_wrong_attempt_locks_the_invitee_out(self, invitee, db_path, key_path):
        current = invitee
        for _ in range(MAX_ATTEMPTS):
            verify_code(current, "000000", now=NOW, db_path=db_path, key_path=key_path)
            current = resolve_token("token-abc", db_path=db_path)

        row = _reread(db_path)
        assert row["locked_until"] is not None
        # Reset alongside the lock, so the next window starts clean rather than giving one
        # attempt per window forever after.
        assert row["failed_attempts"] == 0

    def test_a_locked_out_invitee_is_refused_even_with_the_right_code(self, invitee, db_path, key_path):
        locked = dict(invitee)
        locked["locked_until"] = (NOW + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

        ok, reason = verify_code(locked, "123456", now=NOW, db_path=db_path, key_path=key_path)

        assert ok is False
        assert "Try again in" in reason

    def test_the_lockout_expires(self, invitee, db_path, key_path):
        expired = dict(invitee)
        expired["locked_until"] = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")

        ok, _ = verify_code(expired, "123456", now=NOW, db_path=db_path, key_path=key_path)
        assert ok is True

    def test_a_correct_code_clears_earlier_failures(self, invitee, db_path, key_path):
        # A legitimate invitee opening their link on a second device must not inherit the
        # failed attempts from fumbling the code on the first.
        verify_code(invitee, "000000", now=NOW, db_path=db_path, key_path=key_path)
        current = resolve_token("token-abc", db_path=db_path)
        verify_code(current, "123456", now=NOW, db_path=db_path, key_path=key_path)

        row = _reread(db_path)
        assert row["failed_attempts"] == 0
        assert row["locked_until"] is None


class TestLockoutRemaining:
    def test_no_lock_means_none(self, invitee):
        assert lockout_remaining(invitee, now=NOW) is None

    def test_a_future_lock_reports_what_is_left(self, invitee):
        locked = dict(invitee)
        locked["locked_until"] = (NOW + timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")

        remaining = lockout_remaining(locked, now=NOW)
        assert remaining is not None
        assert remaining.total_seconds() == pytest.approx(LOCKOUT_MINUTES * 60)

    def test_an_unreadable_stamp_fails_open_rather_than_stranding_someone(self, invitee):
        # Failing closed would leave a legitimate invitee locked out forever with no appeal.
        unreadable = dict(invitee)
        unreadable["locked_until"] = "whenever"
        assert lockout_remaining(unreadable, now=NOW) is None
