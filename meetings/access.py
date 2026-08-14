"""Getting an invitee into their own chat, and nobody else's (requirement 6.7, Phase 1).

There is no login here and there is not going to be one. Spec 2 is explicit: the **link is
the identity** and the **access code is proof of possession** — two factors, no password,
no account, because half the invitees are external suppliers who will never sign up for
anything.

That puts all the weight on the token. It is 24 random bytes from `secrets`, so guessing
one is not a threat worth modelling. The realistic attack is someone who *has* a link —
forwarded, or found in a mailbox — guessing the 6-digit code, and that is what the lockout
below exists to stop: 5 attempts, then 15 minutes, which turns a million-code space into
years rather than an afternoon. The lockout is per *invitee row*, not per IP or per
browser, because the row is the thing being attacked and it is the only identifier that
survives the attacker opening a new tab.

The code is **encrypted, not hashed** — deliberately, and it is the one place this module
departs from how `auth/` treats a password. A password is never shown to anyone again; an
access code has to be, because the creator's Share screen is how it reaches the invitee at
all while automated sending is undecided. A hash could not do that. The tradeoff is
accepted knowingly: the code is a second factor guarding a single meeting's chat, not a
credential to an account, and it is worth less than the ability to re-read it.
"""

import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llm.crypto import decrypt_api_key, encrypt_api_key
from llm.exceptions import LLMDatabaseError
from meetings.db import clear_failed_attempts, record_failed_attempt
from meetings.exceptions import MeetingAccessError

logger = logging.getLogger(__name__)

TOKEN_BYTES = 24
ACCESS_CODE_LENGTH = 6
MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def generate_token() -> str:
    """A new, unguessable invitee token. This is the whole of the invitee's identity."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def generate_access_code() -> str:
    """A new 6-digit access code.

    `secrets.choice` rather than `random` — this is the factor an attacker with the link
    has to guess, so a predictable generator would undo the lockout entirely.
    """
    return "".join(secrets.choice(string.digits) for _ in range(ACCESS_CODE_LENGTH))


def encrypt_code(code: str, *, key_path: Path | str | None = None) -> str:
    """Encrypts an access code for storage.

    Reuses the app's existing Fernet key rather than introducing a second one. The
    `llm.crypto` helpers are named for API keys but are generic string encryption; wrapping
    them here keeps that reuse visible at the call site instead of leaving `meetings/`
    looking like it depends on the LLM package for something LLM-related.

    Raises:
        MeetingAccessError: if the key can't be read or the code can't be encrypted.
    """
    try:
        if key_path is None:
            return encrypt_api_key(code)
        return encrypt_api_key(code, key_path=key_path)
    except LLMDatabaseError as error:
        logger.exception("Could not encrypt an access code.")
        raise MeetingAccessError("Could not secure the access code for this invitee.") from error


def decrypt_code(encrypted: str, *, key_path: Path | str | None = None) -> str:
    """Decrypts a stored access code, for the creator's Share screen.

    Raises:
        MeetingAccessError: if the stored code can't be read back.
    """
    try:
        if key_path is None:
            return decrypt_api_key(encrypted)
        return decrypt_api_key(encrypted, key_path=key_path)
    except LLMDatabaseError as error:
        logger.exception("Could not decrypt a stored access code.")
        raise MeetingAccessError("Could not read this invitee's access code.") from error


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        # A lockout stamp that can't be read is treated as no lockout rather than as a
        # permanent one: failing open here costs at most a few more guesses against a
        # 6-digit code, while failing closed would strand a legitimate invitee forever
        # with no way to appeal it.
        logger.warning("Ignoring an unreadable lockout timestamp: %r", value)
        return None


def lockout_remaining(invitee_row: dict, *, now: datetime | None = None) -> timedelta | None:
    """How much of this invitee's lockout is left, or None if they aren't locked out."""
    locked_until = _parse_timestamp(invitee_row.get("locked_until"))
    if locked_until is None:
        return None

    now = now or datetime.now(timezone.utc)
    remaining = locked_until - now
    return remaining if remaining.total_seconds() > 0 else None


def verify_code(
    invitee_row: dict,
    entered_code: str,
    *,
    now: datetime | None = None,
    db_path: Path | str | None = None,
    key_path: Path | str | None = None,
) -> tuple[bool, str]:
    """Checks an entered access code, recording the attempt.

    Returns `(True, "")` on success, or `(False, reason)` — a wrong code is the normal path
    through this screen, not an exception. `MeetingAccessError` is reserved for the check
    itself failing, which is a different thing and needs a different message.

    Raises:
        MeetingAccessError: if the stored code can't be decrypted.
    """
    now = now or datetime.now(timezone.utc)
    invitee_id = invitee_row["invitee_id"]

    remaining = lockout_remaining(invitee_row, now=now)
    if remaining is not None:
        minutes = max(1, int(remaining.total_seconds() // 60) + 1)
        # Checked before the code is even compared, so a locked-out attacker learns nothing
        # about whether the code they tried was right.
        return False, f"Too many incorrect attempts. Try again in {minutes} minute(s)."

    entered = (entered_code or "").strip()
    if not entered:
        return False, "Enter the access code from your invitation."

    stored = decrypt_code(str(invitee_row["access_code_enc"]), key_path=key_path)

    kwargs = {"db_path": db_path} if db_path is not None else {}

    if not secrets.compare_digest(entered, stored):
        attempts = int(invitee_row.get("failed_attempts") or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).strftime(_TIMESTAMP_FORMAT)
            record_failed_attempt(invitee_id, locked_until, **kwargs)
            logger.warning("Invitee %s locked out after %d failed attempts.", invitee_id, attempts)
            return False, f"Too many incorrect attempts. Try again in {LOCKOUT_MINUTES} minutes."

        record_failed_attempt(invitee_id, None, **kwargs)
        left = MAX_ATTEMPTS - attempts
        return False, f"That code isn't right. {left} attempt(s) left before a short lockout."

    clear_failed_attempts(invitee_id, **kwargs)
    return True, ""
