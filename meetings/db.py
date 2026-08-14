"""Meetings, invitees, sessions and messages in SQLite (requirement 6.7).

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

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from meetings.exceptions import MeetingStorageError
from meetings.model import (
    AgendaItem,
    AgendaTable,
    ChatMessage,
    ChatSession,
    EvaluationAnswer,
    EvaluationField,
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

# Phase 2 (spec 3a/3b). All four are new tables rather than columns on existing ones, which
# is why none of them needs a `PRAGMA table_info` migration guard the way `check_sets` did:
# `CREATE TABLE IF NOT EXISTS` is the whole migration for a database written by Phase 1.
_CREATE_AGENDA_TABLES_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_agenda_tables (
    table_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id       INTEGER NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    item_ref         TEXT NOT NULL,
    source_file      TEXT NOT NULL DEFAULT '',
    locked_columns   TEXT NOT NULL DEFAULT '[]',
    editable_columns TEXT NOT NULL DEFAULT '[]',
    base_data        TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# One grid per agenda item title. UNIQUE rather than merely indexed so that re-uploading a
# sheet for the same item replaces it instead of leaving two tables both claiming the tab.
_CREATE_AGENDA_TABLES_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_meeting_agenda_tables_item
ON meeting_agenda_tables (meeting_id, item_ref);
"""

# A row exists here only when the invitee has actually filled something into it — see
# `save_table_responses`. That is what makes completion a plain COUNT rather than a scan of
# every stored JSON blob asking whether it counts as filled.
_CREATE_TABLE_RESPONSES_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_table_responses (
    table_id      INTEGER NOT NULL REFERENCES meeting_agenda_tables(table_id) ON DELETE CASCADE,
    invitee_id    INTEGER NOT NULL REFERENCES meeting_invitees(invitee_id) ON DELETE CASCADE,
    row_id        INTEGER NOT NULL,
    edited_values TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (table_id, invitee_id, row_id)
);
"""

_CREATE_EVALUATION_FIELDS_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_evaluation_fields (
    field_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id   INTEGER NOT NULL REFERENCES meetings(meeting_id) ON DELETE CASCADE,
    question     TEXT NOT NULL,
    buckets_json TEXT NOT NULL DEFAULT '[]',
    position     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_EVALUATION_FIELDS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_meeting_evaluation_fields_meeting
ON meeting_evaluation_fields (meeting_id, position);
"""

_CREATE_EVALUATION_RESULTS_TABLE = """
CREATE TABLE IF NOT EXISTS meeting_evaluation_results (
    field_id       INTEGER NOT NULL REFERENCES meeting_evaluation_fields(field_id) ON DELETE CASCADE,
    invitee_id     INTEGER NOT NULL REFERENCES meeting_invitees(invitee_id) ON DELETE CASCADE,
    raw_answer     TEXT NOT NULL DEFAULT '',
    classified_tag TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (field_id, invitee_id)
);
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
        connection.execute(_CREATE_AGENDA_TABLES_TABLE)
        connection.execute(_CREATE_AGENDA_TABLES_INDEX)
        connection.execute(_CREATE_TABLE_RESPONSES_TABLE)
        connection.execute(_CREATE_EVALUATION_FIELDS_TABLE)
        connection.execute(_CREATE_EVALUATION_FIELDS_INDEX)
        connection.execute(_CREATE_EVALUATION_RESULTS_TABLE)


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


# --------------------------------------------------------------------------------------
# Agenda tables and evaluation fields — the meeting's own setup (spec 3a, 3b)
# --------------------------------------------------------------------------------------
#
# These read functions take no `user_id` and no token, which breaks the two-family split
# above on purpose. They are part of the *meeting's* definition, like `agenda_json` — and
# both sides have already proved their right to the meeting before they get here: the
# creator through `load_meeting`, the invitee through `resolve_token`. Writing is a
# different matter, and every writer below is scoped to `created_by`.


def _row_to_agenda_table(row: sqlite3.Row) -> AgendaTable:
    return AgendaTable(
        table_id=row["table_id"],
        meeting_id=row["meeting_id"],
        item_ref=row["item_ref"],
        source_file=row["source_file"],
        locked_columns=_json_list(row["locked_columns"]),
        editable_columns=_json_list(row["editable_columns"]),
        base_data=_json_list(row["base_data"]),
    )


def _json_list(text: str) -> list:
    """A stored JSON array, or an empty list if it can't be read.

    Never raises, for the same reason `agenda_from_json` doesn't: a grid that won't parse
    should cost the invitee that one tab, not the chat screen it is a tab of.
    """
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        logger.exception("A stored meetings JSON list could not be read.")
        return []
    return value if isinstance(value, list) else []


def save_agenda_table(
    meeting_id: int, user_id: int, table: AgendaTable, db_path: Path | str = DEFAULT_DB_PATH
) -> int:
    """Attaches a grid to one table agenda item, replacing any grid already there.

    Replacing rather than versioning is deliberate: `base_data` is the question every
    invitee is answering, so two versions of it would mean two invitees answered different
    questions with nothing on screen saying so. Re-uploading before invitees start is the
    intended fix for a wrong sheet; re-uploading after they start is the creator changing
    the question, and their existing answers are cleared with it.

    Raises:
        MeetingStorageError: if the meeting isn't theirs, or on a database failure.
    """
    item_ref = (table.item_ref or "").strip()
    if not item_ref:
        raise MeetingStorageError("A table needs to belong to an agenda item.")

    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting_id, user_id)
        existing = connection.execute(
            "SELECT table_id FROM meeting_agenda_tables WHERE meeting_id = ? AND item_ref = ?;",
            (meeting_id, item_ref),
        ).fetchone()

        payload = (
            table.source_file,
            json.dumps(table.locked_columns),
            json.dumps(table.editable_columns),
            json.dumps(table.base_data),
        )
        if existing is None:
            cursor = connection.execute(
                "INSERT INTO meeting_agenda_tables "
                "(meeting_id, item_ref, source_file, locked_columns, editable_columns, base_data) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (meeting_id, item_ref, *payload),
            )
            return cursor.lastrowid

        table_id = existing["table_id"]
        connection.execute(
            "UPDATE meeting_agenda_tables SET source_file = ?, locked_columns = ?, "
            "editable_columns = ?, base_data = ? WHERE table_id = ?;",
            (*payload, table_id),
        )
        # The rows people filled in answered the old sheet. Keeping them against a new one
        # would silently re-attribute an answer to a different question.
        connection.execute("DELETE FROM meeting_table_responses WHERE table_id = ?;", (table_id,))
        return table_id


def list_agenda_tables(meeting_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> list[AgendaTable]:
    """Every grid attached to this meeting, keyed back to its agenda item by `item_ref`."""
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM meeting_agenda_tables WHERE meeting_id = ? ORDER BY table_id;",
            (meeting_id,),
        ).fetchall()
    return [_row_to_agenda_table(row) for row in rows]


def find_agenda_table(
    meeting_id: int, item_ref: str, db_path: Path | str = DEFAULT_DB_PATH
) -> AgendaTable | None:
    """The grid for one agenda item, or None if the creator hasn't uploaded one yet."""
    with _get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM meeting_agenda_tables WHERE meeting_id = ? AND item_ref = ?;",
            (meeting_id, (item_ref or "").strip()),
        ).fetchone()
    return _row_to_agenda_table(row) if row is not None else None


def delete_agenda_table(
    meeting_id: int, user_id: int, item_ref: str, db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """Removes a grid and every answer to it.

    Raises:
        MeetingStorageError: if the meeting isn't theirs, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting_id, user_id)
        connection.execute(
            "DELETE FROM meeting_agenda_tables WHERE meeting_id = ? AND item_ref = ?;",
            (meeting_id, (item_ref or "").strip()),
        )


def save_table_responses(
    table_id: int,
    invitee_id: int,
    rows: dict[int, dict],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Replaces one invitee's answers to one grid, and returns how many rows they've filled.

    A row whose values are all blank is **not stored**. That is the invariant the completion
    count rests on — "a stored response row is a filled row" — so it is enforced here rather
    than trusted from the caller, and clearing a row the invitee had filled correctly takes
    the row back out.

    Whole-set replace rather than per-row upsert because Save Progress is one press over one
    grid: a partial write would leave a row the invitee had just cleared still counted.
    """
    keep = {}
    for row_id, values in (rows or {}).items():
        cleaned = {
            str(column): value
            for column, value in (values or {}).items()
            if str(value or "").strip()
        }
        if cleaned:
            keep[int(row_id)] = cleaned

    with _get_connection(db_path) as connection:
        connection.execute(
            "DELETE FROM meeting_table_responses WHERE table_id = ? AND invitee_id = ?;",
            (table_id, invitee_id),
        )
        connection.executemany(
            "INSERT INTO meeting_table_responses (table_id, invitee_id, row_id, edited_values, updated_at) "
            "VALUES (?, ?, ?, ?, ?);",
            [
                (table_id, invitee_id, row_id, json.dumps(values), _now())
                for row_id, values in sorted(keep.items())
            ],
        )
    return len(keep)


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        logger.exception("A stored table response could not be read.")
        return {}
    return value if isinstance(value, dict) else {}


def load_table_responses(
    table_id: int, invitee_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[int, dict]:
    """One invitee's filled rows for one grid, keyed by row index."""
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT row_id, edited_values FROM meeting_table_responses "
            "WHERE table_id = ? AND invitee_id = ? ORDER BY row_id;",
            (table_id, invitee_id),
        ).fetchall()
    return {row["row_id"]: _json_object(row["edited_values"]) for row in rows}


def load_all_table_responses(
    table_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[int, dict[int, dict]]:
    """Every invitee's filled rows for one grid, as `{invitee_id: {row_id: values}}`.

    One query rather than one per invitee, because this feeds a comparison matrix that would
    otherwise open a connection per column.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT invitee_id, row_id, edited_values FROM meeting_table_responses "
            "WHERE table_id = ? ORDER BY invitee_id, row_id;",
            (table_id,),
        ).fetchall()

    responses: dict[int, dict[int, dict]] = {}
    for row in rows:
        responses.setdefault(row["invitee_id"], {})[row["row_id"]] = _json_object(row["edited_values"])
    return responses


def count_table_responses(meeting_id: int, db_path: Path | str = DEFAULT_DB_PATH) -> dict[tuple[int, int], int]:
    """`{(table_id, invitee_id): filled_row_count}` for every grid in this meeting.

    A COUNT rather than a length of loaded JSON — this feeds the creator's status list,
    which needs a completion figure per invitee per table and nothing else from the rows.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT r.table_id, r.invitee_id, COUNT(*) AS filled "
            "FROM meeting_table_responses r "
            "JOIN meeting_agenda_tables t ON t.table_id = r.table_id "
            "WHERE t.meeting_id = ? GROUP BY r.table_id, r.invitee_id;",
            (meeting_id,),
        ).fetchall()
    return {(row["table_id"], row["invitee_id"]): row["filled"] for row in rows}


def table_progress(
    meeting_id: int, invitee_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> dict[str, tuple[int, int]]:
    """`{agenda item title: (filled_rows, total_rows)}` for one invitee.

    The shape the MoM wants, in one query. Both pages need it — the invitee's Close Chat and
    the creator's Generate Status write the same figures into the same summary — so it lives
    here rather than being assembled twice from `list_agenda_tables` and
    `count_table_responses`.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT t.item_ref, t.base_data, "
            "  (SELECT COUNT(*) FROM meeting_table_responses r "
            "     WHERE r.table_id = t.table_id AND r.invitee_id = ?) AS filled "
            "FROM meeting_agenda_tables t WHERE t.meeting_id = ?;",
            (invitee_id, meeting_id),
        ).fetchall()
    return {row["item_ref"]: (row["filled"], len(_json_list(row["base_data"]))) for row in rows}


def _row_to_evaluation_field(row: sqlite3.Row) -> EvaluationField:
    return EvaluationField(
        field_id=row["field_id"],
        meeting_id=row["meeting_id"],
        question=row["question"],
        buckets=[str(bucket) for bucket in _json_list(row["buckets_json"])],
        position=row["position"],
    )


def list_evaluation_fields(
    meeting_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> list[EvaluationField]:
    """This meeting's evaluation questions, in the order the creator put them in.

    An empty list means the feature is simply off for this meeting — spec 1 makes evaluation
    fields an optional toggle, and "no questions defined" is how that toggle is stored.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM meeting_evaluation_fields WHERE meeting_id = ? ORDER BY position, field_id;",
            (meeting_id,),
        ).fetchall()
    return [_row_to_evaluation_field(row) for row in rows]


def replace_evaluation_fields(
    meeting_id: int,
    user_id: int,
    fields: list[EvaluationField],
    db_path: Path | str = DEFAULT_DB_PATH,
) -> None:
    """Saves this meeting's evaluation questions as the given list.

    Existing questions are **updated in place by `field_id`**, not deleted and re-inserted.
    Deleting would cascade `meeting_evaluation_results` away, so fixing a typo in a question
    would throw out every answer already extracted for it. Only a question the creator
    actually removed loses its answers — which is right, since the question it answered is
    gone.

    Raises:
        MeetingStorageError: if the meeting isn't theirs, or on a database failure.
    """
    with _get_connection(db_path) as connection:
        _get_owned_meeting(connection, meeting_id, user_id)

        existing = {
            row["field_id"]
            for row in connection.execute(
                "SELECT field_id FROM meeting_evaluation_fields WHERE meeting_id = ?;",
                (meeting_id,),
            ).fetchall()
        }

        kept = set()
        for position, field_spec in enumerate(fields):
            question = (field_spec.question or "").strip()
            if not question:
                continue
            buckets = json.dumps([bucket for bucket in field_spec.buckets if bucket.strip()])
            if field_spec.field_id in existing:
                connection.execute(
                    "UPDATE meeting_evaluation_fields SET question = ?, buckets_json = ?, position = ? "
                    "WHERE field_id = ? AND meeting_id = ?;",
                    (question, buckets, position, field_spec.field_id, meeting_id),
                )
                kept.add(field_spec.field_id)
            else:
                cursor = connection.execute(
                    "INSERT INTO meeting_evaluation_fields (meeting_id, question, buckets_json, position) "
                    "VALUES (?, ?, ?, ?);",
                    (meeting_id, question, buckets, position),
                )
                kept.add(cursor.lastrowid)

        for removed in existing - kept:
            connection.execute(
                "DELETE FROM meeting_evaluation_fields WHERE field_id = ? AND meeting_id = ?;",
                (removed, meeting_id),
            )


def save_evaluation_answers(
    invitee_id: int, answers: list[EvaluationAnswer], db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """Stores one invitee's extracted answers, overwriting any previous extraction.

    Overwrite rather than append for the same reason `save_live_status` overwrites: a
    re-extraction is a better reading of the same conversation, not a second opinion to be
    kept beside the first.
    """
    with _get_connection(db_path) as connection:
        connection.executemany(
            "INSERT INTO meeting_evaluation_results (field_id, invitee_id, raw_answer, classified_tag, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(field_id, invitee_id) DO UPDATE SET raw_answer = excluded.raw_answer, "
            "classified_tag = excluded.classified_tag, updated_at = excluded.updated_at;",
            [
                (answer.field_id, invitee_id, answer.raw_answer, answer.classified_tag, _now())
                for answer in answers
                if answer.field_id is not None
            ],
        )


def list_evaluation_answers(
    meeting_id: int, db_path: Path | str = DEFAULT_DB_PATH
) -> list[EvaluationAnswer]:
    """Every extracted answer in this meeting, across every invitee.

    Joined through the fields table so the meeting scope holds: the results table keys on
    `field_id`, and a query that filtered on anything else could not prove the rows belong
    to this meeting.
    """
    with _get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT r.* FROM meeting_evaluation_results r "
            "JOIN meeting_evaluation_fields f ON f.field_id = r.field_id "
            "WHERE f.meeting_id = ? ORDER BY f.position, f.field_id, r.invitee_id;",
            (meeting_id,),
        ).fetchall()
    return [
        EvaluationAnswer(
            field_id=row["field_id"],
            invitee_id=row["invitee_id"],
            raw_answer=row["raw_answer"] or "",
            classified_tag=row["classified_tag"] or "",
            updated_at=row["updated_at"] or "",
        )
        for row in rows
    ]
