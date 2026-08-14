"""Meetings, invitees, sessions and messages in SQLite (requirement 6.7, Phase 1).

Follows `chat_types/db.py` line for line — short-lived connection per call, every creator
read scoped to the owning `user_id` — with one structural difference that shapes the whole
module: **the invitee side has no user_id to scope by.** An invitee never logs in; their
token *is* their identity. So there are two families of function here:

- `..._for_creator` / anything taking `user_id`, scoped by `meetings.created_by`
- `..._by_token`, scoped by the token alone

The two are kept textually apart below rather than merged behind a flag, because the one
mistake that would matter in this module is a creator-side query that forgets its
`user_id`, or an invitee-side one that trusts a `meeting_id` from the URL without checking
the token actually belongs to it. `resolve_token` is the only door into the invitee side,
and it returns the meeting id rather than accepting one.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from meetings.exceptions import MeetingStorageError
from meetings.model import (
    AgendaItem,
    ChatMessage,
    ChatSession,
    Invitee,
    Meeting,
    agenda_from_json,
    agenda_to_json,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data") / "tikitarai.db"

_CREATE_USER_DEFAULTS_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_user_defaults (
    user_id         INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    default_persona TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_CONTACTS_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_contacts (
    email          TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    organization   TEXT,
    first_added_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    first_added_on TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_MEETINGS_TABLE = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject            TEXT NOT NULL,
    created_by         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    meeting_context    TEXT NOT NULL DEFAULT '',
    persona            TEXT NOT NULL DEFAULT '',
    context_sop        TEXT NOT NULL DEFAULT '',
    agenda_json        TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_MEETINGS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_meetings_created_by ON meetings (created_by);
"""

_CREATE_INVITEES_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_invitees (
    invitee_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id      INTEGER NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    token           TEXT NOT NULL UNIQUE,
    access_code_enc TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_INVITEES_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_invitees_token ON meeting_invitees (token);",
    "CREATE INDEX IF NOT EXISTS idx_meeting_invitees_meeting ON meeting_invitees (meeting_id);",
)

_CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_sessions (
    meeting_id                INTEGER NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    invitee_id                INTEGER NOT NULL REFERENCES meeting_invitees(invitee_id) ON DELETE CASCADE,
    closed                    INTEGER NOT NULL DEFAULT 0,
    closed_at                 TEXT,
    summary_json              TEXT,
    live_status_json          TEXT,
    live_status_at            TEXT,
    running_summary           TEXT NOT NULL DEFAULT '',
    folded_through_message_id INTEGER NOT NULL DEFAULT 0,
    last_active_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (meeting_id, invitee_id)
);
"""

_CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL,
    invitee_id INTEGER NOT NULL,
    sender     TEXT NOT NULL CHECK (sender IN ('user', 'ai')),
    text       TEXT NOT NULL,
    agenda_tag TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (meeting_id, invitee_id)
        REFERENCES meeting_sessions (meeting_id, invitee_id) ON DELETE CASCADE
);
"""

_CREATE_MESSAGES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_meeting_messages_session
ON meeting_messages (meeting_id, invitee_id, message_id);
"""

# `invitee_id IS NULL` marks a creator-uploaded reference document, shared read-only with
# everyone; a non-null one is that invitee's own private upload. One table rather than two,
# because "every file belonging to this meeting" is the query the storage folder mirrors.
_CREATE_FILES_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_files (
    file_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    invitee_id  INTEGER,
    filename    TEXT NOT NULL,
    filepath    TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_FILES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_meeting_files_meeting ON meeting_files (meeting_id, invitee_id);
"""


@contextmanager
def _get_connection(db_path: Path | str = DEFAULT_DB_PATH):
    """Opens a short-lived SQLite connection, closed on exit. See `auth.db.get_connection`
    for the rationale.

    Raises:
        MeetingStorageError: if the connection or any statement inside the `with` block fails.
    """
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
    except sqlite3.Error as error:
        logger.error("Failed to open SQLite connection at %s: %s", db_path, error)
        raise MeetingStorageError(f"Could not open the database at {db_path}.") from error

    try:
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("SQLite operation failed on %s: %s", db_path, error)
        raise MeetingStorageError("A database operation failed while accessing meetings.") from error
    finally:
        connection.close()


def init_meetings_tables(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Creates every meetings table if it doesn't exist. Safe to call every process start."""
    with _get_connection(db_path) as connection:
        connection.execute(_CREATE_USER_DEFAULTS_TABLE)
        connection.execute(_CREATE_CONTACTS_TABLE)
        connection.execute(_CREATE_MEETINGS_TABLE)
        connection.execute(_CREATE_MEETINGS_INDEX)
        connection.execute(_CREATE_INVITEES_TABLE)
        for statement in _CREATE_INVITEES_INDEXES:
            connection.execute(statement)
        connection.execute(_CREATE_SESSIONS_TABLE)
        connection.execute(_CREATE_MESSAGES_TABLE)
        connection.execute(_CREATE_MESSAGES_INDEX)
        connection.execute(_CREATE_FILES_TABLE)
        connection.execute(_CREATE_FILES_INDEX)


def _now() -> str:
    """The timestamp format the DEFAULT clauses write, for the columns Python sets itself.

    SQLite's `datetime('now')` is UTC, so this has to be too — mixing the two would make
    `last_active_at` jump backwards by the timezone offset on any row Python touched.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------------------
# Creator defaults and contacts
# --------------------------------------------------------------------------------------


def get_default_persona(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> str:
    """The persona this creator's new meetings start from, or "" if they've saved none."""
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT default_persona FROM meeting_user_defaults WHERE user_id = ?;", (user_id,)
        ).fetchone()
    return str(row["default_persona"]) if row is not None else ""


def set_default_persona(user_id: int, persona: str, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Saves the persona this creator's new meetings should start from."""
    with _get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO meeting_user_defaults (user_id, default_persona) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET default_persona = excluded.default_persona, "
            "updated_at = datetime('now');",
            (user_id, persona.strip()),
        )


def find_contact(email: str, db_path: Path | str = DEFAULT_DB_PATH) -> dict | None:
    """The saved contact for this email, or None. Feeds the creator form's name autofill."""
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM meeting_contacts WHERE email = ? COLLATE NOCASE;", (email.strip(),)
        ).fetchone()
    return dict(row) if row is not None else None


def remember_contact(
    email: str, name: str, user_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """Adds this email to the reusable contacts directory if it isn't already there.

    Deliberately does not update an existing contact's name. `first_added_by`/`first_added_on`
    say this records who a contact *was first seen as*; letting every meeting overwrite the
    name would mean one creator's typo silently renames a contact for everybody.
    """
    email = email.strip()
    name = name.strip()
    if not email or not name:
        return

    with _get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO meeting_contacts (email, name, first_added_by) VALUES (?, ?, ?) "
            "ON CONFLICT(email) DO NOTHING;",
            (email, name, user_id),
        )


# --------------------------------------------------------------------------------------
# Creator side — every query scoped by created_by
# --------------------------------------------------------------------------------------


def _row_to_meeting(row: sqlite3.Row) -> Meeting:
    return Meeting(
        meeting_id=row["meeting_id"],
        subject=row["subject"],
        created_by=row["created_by"],
        meeting_context=row["meeting_context"],
        persona=row["persona"],
        context_sop=row["context_sop"],
        agenda=agenda_from_json(row["agenda_json"]),
        created_at=row["created_at"],
    )


def create_meeting(
    user_id: int, meeting: Meeting, db_path: Path | str = DEFAULT_DB_PATH
) -> Meeting:
    """Inserts a meeting and returns it carrying the id it now has.

    Raises:
        MeetingStorageError: if the subject is blank, or on a database failure.
    """
    subject = (meeting.subject or "").strip()
    if not subject:
        raise MeetingStorageError("Give this meeting a subject before saving it.")

    with _get_connection(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO meetings (subject, created_by, meeting_context, persona, context_sop, agenda_json) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (
                subject,
                user_id,
                meeting.meeting_context.strip(),
                meeting.persona.strip(),
                meeting.context_sop.strip(),
                agenda_to_json(meeting.agenda),
            ),
        )
        meeting_id = cursor.lastrowid

    logger.info("Created meeting '%s' (%s) for user %s.", subject, meeting_id, user_id)
    meeting.meeting_id = meeting_id
    meeting.created_by = user_id
    meeting.subject = subject
    return meeting


def update_meeting(user_id: int, meeting: Meeting, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Rewrites an existing meeting's setup.

    The persona is included: spec 1 freezes the persona against later edits to the
    creator's *default*, not against the creator deliberately editing this meeting.

    Raises:
        MeetingStorageError: if it doesn't belong to user_id, or on a database failure.
    """
    subject = (meeting.subject or "").strip()
    if not subject:
        raise MeetingStorageError("Give this meeting a subject before saving it.")

    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting.meeting_id, user_id)
        connection.execute(
            "UPDATE meetings SET subject = ?, meeting_context = ?, persona = ?, context_sop = ?, "
            "agenda_json = ? WHERE meeting_id = ? AND created_by = ?;",
            (
                subject,
                meeting.meeting_context.strip(),
                meeting.persona.strip(),
                meeting.context_sop.strip(),
                agenda_to_json(meeting.agenda),
                meeting.meeting_id,
                user_id,
            ),
        )


def _get_owned_meeting(connection: sqlite3.Connection, meeting_id: int, user_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM meetings WHERE meeting_id = ? AND created_by = ?;", (meeting_id, user_id)
    ).fetchone()
    if row is None:
        raise MeetingStorageError(f"No meeting {meeting_id} found for this account.")
    return row


def list_meetings(user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Every meeting this user created, newest first, with its invitee counts.

    The counts are computed in SQL rather than by loading each meeting's invitees, because
    this feeds a list page that would otherwise run two queries per row.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT m.meeting_id, m.subject, m.created_at, "
            "  (SELECT COUNT(*) FROM meeting_invitees i WHERE i.meeting_id = m.meeting_id) AS invitee_count, "
            "  (SELECT COUNT(*) FROM meeting_sessions s WHERE s.meeting_id = m.meeting_id AND s.closed = 1) "
            "    AS closed_count "
            "FROM meetings m WHERE m.created_by = ? ORDER BY m.created_at DESC, m.meeting_id DESC;",
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def load_meeting(meeting_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> Meeting:
    """One meeting the given user created.

    Raises:
        MeetingStorageError: if it doesn't belong to user_id.
    """
    with _get_connection(db_path) as connection:
        row = _get_owned_meeting(connection, meeting_id, user_id)
    return _row_to_meeting(row)


def add_invitee(
    meeting_id: int,
    user_id: int,
    name: str,
    email: str,
    token: str,
    access_code_enc: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Adds one invitee to a meeting the user owns, and returns their new id.

    Raises:
        MeetingStorageError: if the meeting isn't theirs, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting_id, user_id)
        cursor = connection.execute(
            "INSERT INTO meeting_invitees (meeting_id, name, email, token, access_code_enc) "
            "VALUES (?, ?, ?, ?, ?);",
            (meeting_id, name.strip(), email.strip(), token, access_code_enc),
        )
        return cursor.lastrowid


def list_invitees(meeting_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    """Every invitee on a meeting the user owns, with their session state.

    A LEFT JOIN because an invitee who has never opened their link has no session row at
    all — and "hasn't joined yet" is exactly what the status table needs to show for them.
    """
    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting_id, user_id)
        rows = connection.execute(
            "SELECT i.invitee_id, i.meeting_id, i.name, i.email, i.token, i.access_code_enc, "
            "  s.closed, s.closed_at, s.summary_json, s.live_status_json, s.live_status_at, "
            "  s.last_active_at "
            "FROM meeting_invitees i "
            "LEFT JOIN meeting_sessions s "
            "  ON s.meeting_id = i.meeting_id AND s.invitee_id = i.invitee_id "
            "WHERE i.meeting_id = ? ORDER BY i.invitee_id;",
            (meeting_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_messages_for_creator(
    meeting_id: int, invitee_id: int, user_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> list[ChatMessage]:
    """One invitee's full transcript, read by the meeting's creator.

    Separate from `list_messages` purely so the ownership check can't be forgotten: the
    creator has a `user_id` to check and the invitee side does not, and a single function
    taking an optional `user_id` would make "no ownership check" the easy default.
    """
    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting_id, user_id)
        rows = _fetch_messages(connection, meeting_id, invitee_id)
    return [_row_to_message(row) for row in rows]


def save_live_status(
    meeting_id: int, invitee_id: int, user_id: int, summary_json: str, db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """Overwrites the on-demand status snapshot for one invitee's still-open chat.

    Overwrite rather than append is spec 3's rule: a live status is provisional by
    definition, and keeping every one ever generated would leave the creator picking
    between six snapshots with no way to tell which is current.
    """
    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting_id, user_id)
        connection.execute(
            "UPDATE meeting_sessions SET live_status_json = ?, live_status_at = ? "
            "WHERE meeting_id = ? AND invitee_id = ?;",
            (summary_json, _now(), meeting_id, invitee_id),
        )


# --------------------------------------------------------------------------------------
# Invitee side — the token is the identity, so nothing here takes a user_id
# --------------------------------------------------------------------------------------


def resolve_token(token: str, db_path: Path | str = DEFAULT_DB_PATH) -> dict | None:
    """The invitee row this token belongs to, or None.

    Returns the row — including its `meeting_id` — rather than taking a meeting id to check
    against, so the id in the URL can never be the thing that decides which meeting gets
    opened. The `m=` parameter is a convenience for readable links; this is the authority.
    """
    token = (token or "").strip()
    if not token:
        return None

    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM meeting_invitees WHERE token = ?;", (token,)
        ).fetchone()
    return dict(row) if row is not None else None


def load_meeting_for_invitee(meeting_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> Meeting:
    """The meeting an already-resolved token points at.

    Unscoped by design — the caller has already proved possession of a token belonging to
    this meeting, and an invitee has no user_id to scope by. Never call this with a
    meeting id that came from anywhere but `resolve_token`.

    Raises:
        MeetingStorageError: if the meeting no longer exists.
    """
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM meetings WHERE meeting_id = ?;", (meeting_id,)
        ).fetchone()
    if row is None:
        raise MeetingStorageError("This meeting is no longer available.")
    return _row_to_meeting(row)


def record_failed_attempt(
    invitee_id: int, locked_until: str | None, db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """Counts one wrong access code, and locks the invitee out if this was the last one."""
    with _get_connection(db_path) as connection:
        if locked_until is None:
            connection.execute(
                "UPDATE meeting_invitees SET failed_attempts = failed_attempts + 1 WHERE invitee_id = ?;",
                (invitee_id,),
            )
        else:
            # The counter resets alongside the lock so the next window starts clean —
            # otherwise a locked-out invitee would get one attempt per window forever after.
            connection.execute(
                "UPDATE meeting_invitees SET failed_attempts = 0, locked_until = ? WHERE invitee_id = ?;",
                (locked_until, invitee_id),
            )


def clear_failed_attempts(invitee_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Resets the lockout counters after a correct code.

    A legitimate invitee opening their link on a second device must not inherit the failed
    attempts from fumbling the code on the first.
    """
    with _get_connection(db_path) as connection:
        connection.execute(
            "UPDATE meeting_invitees SET failed_attempts = 0, locked_until = NULL WHERE invitee_id = ?;",
            (invitee_id,),
        )


def ensure_session(meeting_id: int, invitee_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> ChatSession:
    """The invitee's session, created on first access.

    Called on every verified page load, so it has to be idempotent — `INSERT OR IGNORE`
    rather than a read-then-write, which two tabs opened at once would both pass.
    """
    with _get_connection(db_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO meeting_sessions (meeting_id, invitee_id) VALUES (?, ?);",
            (meeting_id, invitee_id),
        )
        row = connection.execute(
            "SELECT * FROM meeting_sessions WHERE meeting_id = ? AND invitee_id = ?;",
            (meeting_id, invitee_id),
        ).fetchone()

    return ChatSession(
        meeting_id=row["meeting_id"],
        invitee_id=row["invitee_id"],
        closed=bool(row["closed"]),
        closed_at=row["closed_at"] or "",
        running_summary=row["running_summary"] or "",
        folded_through_message_id=row["folded_through_message_id"],
        last_active_at=row["last_active_at"] or "",
    )


def _row_to_message(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        message_id=row["message_id"],
        sender=row["sender"],
        text=row["text"],
        agenda_tag=row["agenda_tag"] or "",
        created_at=row["created_at"],
    )


def _fetch_messages(
    connection: sqlite3.Connection, meeting_id: int, invitee_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM meeting_messages WHERE meeting_id = ? AND invitee_id = ? ORDER BY message_id;",
        (meeting_id, invitee_id),
    ).fetchall()


def list_messages(
    meeting_id: int, invitee_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> list[ChatMessage]:
    """The full transcript for one session, oldest first.

    **Always the whole thing, never a window.** This is the source of truth the MoM is built
    from (spec 3), and the only trimming that happens anywhere is what
    `running_summary.recent_messages` hands the live chat agent.
    """
    with _get_connection(db_path) as connection:
        rows = _fetch_messages(connection, meeting_id, invitee_id)
    return [_row_to_message(row) for row in rows]


def add_message(
    meeting_id: int,
    invitee_id: int,
    sender: str,
    text: str,
    agenda_tag: str = "",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Saves one turn and returns its id, touching the session's last-active stamp.

    Both statements are in one transaction because a message whose session still claims the
    invitee was last active an hour ago would make the creator's status table lie.
    """
    with _get_connection(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO meeting_messages (meeting_id, invitee_id, sender, text, agenda_tag) "
            "VALUES (?, ?, ?, ?, ?);",
            (meeting_id, invitee_id, sender, text, agenda_tag or None),
        )
        connection.execute(
            "UPDATE meeting_sessions SET last_active_at = ? WHERE meeting_id = ? AND invitee_id = ?;",
            (_now(), meeting_id, invitee_id),
        )
        return cursor.lastrowid


def update_running_summary(
    meeting_id: int,
    invitee_id: int,
    running_summary: str,
    folded_through_message_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Records a completed fold.

    The summary and the cutoff move together in one statement: a cutoff advanced without
    its summary would silently drop those turns from the agent's context entirely, and a
    summary written without its cutoff would fold the same turns in again next time.
    """
    with _get_connection(db_path) as connection:
        connection.execute(
            "UPDATE meeting_sessions SET running_summary = ?, folded_through_message_id = ? "
            "WHERE meeting_id = ? AND invitee_id = ?;",
            (running_summary, folded_through_message_id, meeting_id, invitee_id),
        )


def close_session(
    meeting_id: int, invitee_id: int, summary_json: str, db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """Marks a session closed and stores its final MoM.

    `AND closed = 0` makes this a no-op on an already-closed session rather than a second
    write: spec 3 calls the final summary a locked, permanent record, so a double-submitted
    Close Chat must not replace one MoM with another.
    """
    with _get_connection(db_path) as connection:
        connection.execute(
            "UPDATE meeting_sessions SET closed = 1, closed_at = ?, summary_json = ? "
            "WHERE meeting_id = ? AND invitee_id = ? AND closed = 0;",
            (_now(), summary_json, meeting_id, invitee_id),
        )


def get_session_summary(
    meeting_id: int, invitee_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> str:
    """The stored final MoM for one session, or "" if it hasn't closed yet."""
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT summary_json FROM meeting_sessions WHERE meeting_id = ? AND invitee_id = ?;",
            (meeting_id, invitee_id),
        ).fetchone()
    return str(row["summary_json"] or "") if row is not None else ""


# --------------------------------------------------------------------------------------
# Files — reference documents and invitee uploads
# --------------------------------------------------------------------------------------


def add_file(
    meeting_id: int,
    filename: str,
    filepath: str,
    invitee_id: int | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Records a stored file. `invitee_id=None` means a shared reference document."""
    with _get_connection(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO meeting_files (meeting_id, invitee_id, filename, filepath) VALUES (?, ?, ?, ?);",
            (meeting_id, invitee_id, filename, filepath),
        )
        return cursor.lastrowid


def list_files(
    meeting_id: int, invitee_id: int | None = None, db_path: Path | str = DEFAULT_DB_PATH
) -> list[dict]:
    """Reference documents when `invitee_id` is None, otherwise that invitee's own uploads.

    An invitee's own uploads are never mixed into the shared list — spec 2 makes a
    per-invitee upload private to that invitee, so listing them together would expose one
    supplier's attachment to another.
    """
    with _get_connection(db_path) as connection:
        if invitee_id is None:
            rows = connection.execute(
                "SELECT * FROM meeting_files WHERE meeting_id = ? AND invitee_id IS NULL "
                "ORDER BY file_id;",
                (meeting_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM meeting_files WHERE meeting_id = ? AND invitee_id = ? ORDER BY file_id;",
                (meeting_id, invitee_id),
            ).fetchall()
    return [dict(row) for row in rows]
