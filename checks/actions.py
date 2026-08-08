"""Drafting the follow-up to a criteria's failures (requirement 6.5, addtion.md step 7).

**Nothing here sends anything.** The app has no SMTP configuration, no calendar
credentials and no task backend, and inventing outbound delivery to satisfy a button would
be the largest and least reversible part of this feature. So an action is drafted, reviewed,
edited, confirmed — which puts it in the report — and, if the user wants it to leave the
app, downloaded as a file their own mail or calendar client opens:

- **`.eml`** via the standard library's `email.message.EmailMessage`, which every mail
  client opens as a pre-filled draft.
- **`.ics`** as a minimal VCALENDAR, written by hand. A calendar library would be a
  dependency for forty lines of text, and the escaping rules that actually matter
  (backslash, comma, semicolon, newline) are the four below.

Everything that varies between an email, a meeting, a task and a note lives in **one**
`ActionKind` record per kind: its label, its drafting instructions, what it calls the people
and the date, and how it becomes a file. Adding a fifth kind is one entry rather than an
edit to four parallel dicts and a branch in the page — and the page never asks what kind a
draft is at all, because `to_file` answers the only question it had.

Nothing imports Streamlit, so the whole module is testable without `AppTest`.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from pydantic import BaseModel, Field

from checks.model import (
    ACTION_EMAIL,
    ACTION_MEETING,
    ACTION_OTHER,
    ACTION_TASK,
    ActionDraft,
    Check,
)
from checks.remarks import render_failures
from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

# What a drafted subject line is trimmed to. Mail clients truncate past roughly this, and a
# model asked for "a subject" will occasionally write a paragraph.
MAX_SUBJECT_CHARS = 120

_EMAIL_INSTRUCTIONS = (
    "You draft a short, professional internal email about records that failed a compliance "
    "check. State what the rule was, how many records breached it, and name them. "
    "Quote figures exactly as given — never round, rescale or recompute them. "
    "Four to eight lines. No greeting placeholders like [Name], no signature block."
)

_MEETING_INSTRUCTIONS = (
    "You draft the agenda for a short internal meeting about records that failed a "
    "compliance check. Three to five agenda points, one per line, starting with '- '. "
    "Name the specific records or groups to be discussed, quoting figures exactly as given. "
    "No preamble, no attendee list, no closing."
)

_TASK_INSTRUCTIONS = (
    "You draft the description of one work item to resolve records that failed a compliance "
    "check. State what has to be checked or corrected and for which records, quoting figures "
    "exactly as given. Three to six lines. No status, no priority, no assignee."
)

_OTHER_INSTRUCTIONS = (
    "You draft a short internal note about records that failed a compliance check. "
    "State the rule, the number of breaches and the records involved, quoting figures "
    "exactly as given. Three to six lines. No greeting, no signature."
)

@dataclass(frozen=True)
class ActionKind:
    """Everything that differs between one kind of follow-up and another.

    Attributes:
        label: what the UI and the report call it.
        instructions: the drafting call's system instructions.
        recipient_fields: what this kind calls its "who" — one field per input the page
            offers, and the keys of `ActionDraft.recipients`.
        when_label: what it calls its "when", or "" when it has none.
        extension: the file a confirmed draft downloads as. `.ics` means `to_ics` renders
            it; everything else is a mail draft.
    """

    label: str
    instructions: str
    recipient_fields: tuple[str, ...]
    when_label: str
    extension: str


KINDS: dict[str, ActionKind] = {
    ACTION_EMAIL: ActionKind("Email", _EMAIL_INSTRUCTIONS, ("to", "cc", "bcc"), "", "eml"),
    ACTION_MEETING: ActionKind("Meeting", _MEETING_INSTRUCTIONS, ("attendees",), "Date and time", "ics"),
    ACTION_TASK: ActionKind("Task", _TASK_INSTRUCTIONS, ("owner",), "Due date", "eml"),
    ACTION_OTHER: ActionKind("Other", _OTHER_INSTRUCTIONS, ("people",), "When", "eml"),
}

# The fallback for a draft whose kind is somehow unknown — a set saved by a later version,
# say. A note is the kind that assumes least about what the user meant.
FALLBACK_KIND = KINDS[ACTION_OTHER]


def kind_of(draft: ActionDraft) -> ActionKind:
    """The record describing this draft's kind."""
    return KINDS.get(draft.kind, FALLBACK_KIND)


class DraftedAction(BaseModel):
    """The structured shape every drafting call returns.

    One schema across all four kinds even though the instructions differ, because the two
    fields a follow-up needs — what it is called and what it says — are the same whether it
    becomes a subject line, a meeting title or a task name.
    """

    subject: str = Field(
        # Defaulted so a model that replies with the message itself rather than the schema
        # still yields a usable draft — `run_structured`'s `text_field` fills the body, and
        # the subject is a one-line box the user can type into.
        default="",
        description="A short title or subject line. One line, no prefix.",
    )
    body: str = Field(description="The email body, agenda points, or task description.")


def build_prompt(persona: str, check: Check, draft: ActionDraft) -> str:
    """Assembles the drafting prompt from the criteria's saved failures."""
    run = check.saved_run
    parts = [f"The rule that was checked:\n{check.criteria_text.strip()}"]

    if run is not None:
        parts.append(f"Outcome: {run.fail_count} of {run.row_count} record(s) breached the rule.")

    parts.append(f"The breaching records:\n{render_failures(check)}")

    audience = draft.display_recipients()
    if audience:
        parts.append(f"Who this is for:\n{audience}")
    if draft.when.strip():
        parts.append(f"When:\n{draft.when.strip()}")
    if draft.subject.strip():
        # The user's own title wins. Asked for rather than overwritten, so the body they get
        # back is about the thing they said it was about.
        parts.append(f"Use this title, and write the content to match it:\n{draft.subject.strip()}")
    if persona and persona.strip():
        parts.append(f"Write as this role, applying this context:\n{persona.strip()}")

    return "\n\n".join(parts)


def draft_action(
    profile: dict,
    persona: str,
    check: Check,
    draft: ActionDraft,
    *,
    key_path: Path | str | None = None,
) -> tuple[ActionDraft, list[str]]:
    """Fills a draft's subject and body from the criteria's failures.

    The draft is updated in place and returned, so the caller doesn't have to copy fields
    back across and can't half-apply the result.

    Returns:
        `(draft, warnings)`. Never raises: an undrafted action is one the user can still
        write themselves, and a provider hiccup must not cost them the recipients and dates
        they have already typed.
    """
    if check.saved_run is None:
        return draft, ["Save this criteria's results before drafting a follow-up."]

    kind = kind_of(draft)

    try:
        result = run_structured(
            profile,
            build_prompt(persona, check, draft),
            DraftedAction,
            instructions=kind.instructions,
            # The message text is the answer; a missing subject line is something the user
            # can supply in a second, an error message is not.
            text_field="body",
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Drafting a %s for '%s' failed: %s", draft.kind, check.display_name(), error)
        return draft, [f"Couldn't draft this {kind.label.lower()}: {error}"]

    subject = str(result.subject or "").strip()[:MAX_SUBJECT_CHARS]
    body = str(result.body or "").strip()
    if not body:
        return draft, ["The model returned an empty draft."]

    # The user's own subject is kept if they typed one — the prompt was told to write to it,
    # and replacing it would silently undo a decision they made.
    draft.subject = draft.subject.strip() or subject
    draft.body = body
    return draft, []


# --------------------------------------------------------------------------------------
# Files the user's own mail and calendar clients open
# --------------------------------------------------------------------------------------


def to_eml(draft: ActionDraft) -> bytes:
    """Renders an email draft as an `.eml` file.

    No `From` and no `Date`: this is a draft to be opened and sent by the user's own client
    under their own account, and stamping it with either would make it look like a message
    that had already been sent from somewhere.
    """
    message = EmailMessage()
    message["Subject"] = draft.subject or "(no subject)"
    for header, field in (("To", "to"), ("Cc", "cc"), ("Bcc", "bcc")):
        people = [person.strip() for person in draft.recipients.get(field, []) if person.strip()]
        if people:
            message[header] = ", ".join(people)
    message.set_content(draft.body or "")
    return message.as_bytes()


_ICS_ESCAPE = {"\\": "\\\\", ";": "\\;", ",": "\\,", "\n": "\\n"}
_ICS_PATTERN = re.compile("|".join(re.escape(character) for character in _ICS_ESCAPE))

# When a meeting has no parseable date. An invite with no start time is not openable, and
# defaulting to "now" would put a meeting in the past the moment the user reads it.
DEFAULT_MEETING_MINUTES = 30


def _ics_escape(text: str) -> str:
    """Escapes the four characters RFC 5545 gives meaning to inside a property value."""
    return _ICS_PATTERN.sub(lambda match: _ICS_ESCAPE[match.group(0)], str(text or ""))


def _ics_stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def parse_when(text: str) -> datetime | None:
    """Reads a meeting date-time out of what the user typed, or returns None.

    A short list of formats rather than a fuzzy parser: `when` is free text by design (it is
    drafted into prose, not scheduled against), and guessing wrongly at "next Tuesday" would
    put a real meeting on a real calendar on the wrong day.
    """
    candidate = str(text or "").strip()
    if not candidate:
        return None
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate, pattern)
        except ValueError:
            continue
    logger.info("Could not read '%s' as a date-time; the invite will carry no start time.", candidate)
    return None


def to_ics(draft: ActionDraft, *, now: datetime | None = None) -> bytes:
    """Renders a meeting draft as a minimal `.ics` file.

    Returns a VEVENT with a start time only when `draft.when` is a date this module can read
    without guessing — see `parse_when`. Otherwise the invite carries the title, the
    attendees and the agenda, and the user picks the time in their own calendar, which is
    better than an invite confidently scheduled on the wrong day.
    """
    stamp = _ics_stamp(now or datetime.now())
    start = parse_when(draft.when)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TikitarAi//Checks//EN",
        "BEGIN:VEVENT",
        f"UID:{draft.action_id}@tikitarai",
        f"DTSTAMP:{stamp}",
        f"SUMMARY:{_ics_escape(draft.subject or 'Exception review')}",
        f"DESCRIPTION:{_ics_escape(draft.body)}",
    ]

    if start is not None:
        lines.append(f"DTSTART:{_ics_stamp(start)}")
        lines.append(f"DURATION:PT{DEFAULT_MEETING_MINUTES}M")

    for person in draft.recipients.get("attendees", []):
        if person.strip():
            lines.append(f"ATTENDEE;CN={_ics_escape(person.strip())}:mailto:{person.strip()}")

    lines += ["END:VEVENT", "END:VCALENDAR"]
    # CRLF, because RFC 5545 requires it and some clients reject a bare-LF calendar.
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def file_name_for(check: Check, draft: ActionDraft) -> str:
    """A safe download filename for a draft, named after the criteria it came from."""
    stem = re.sub(r"[^0-9A-Za-z_-]+", "_", f"{check.display_name()}_{draft.kind}").strip("_")
    return f"{stem or 'action'}.{kind_of(draft).extension}"


def to_file(check: Check, draft: ActionDraft) -> tuple[bytes, str]:
    """A confirmed draft as `(bytes, filename)`, in whatever format its kind downloads as.

    The one place that asks what kind a draft is when turning it into a file — so the page
    offers a download without branching on kind, and a fifth kind is an entry in `KINDS`
    rather than another `if` somewhere in the UI.
    """
    renderer = to_ics if kind_of(draft).extension == "ics" else to_eml
    return renderer(draft), file_name_for(check, draft)


def drafts_frame_rows(check: Check) -> list[dict]:
    """A criteria's confirmed drafts as plain rows, for the report.

    Returned as dicts rather than a DataFrame so this module keeps no opinion about how the
    report renders them — the caller builds the frame.
    """
    return [
        {
            "Action": kind_of(action).label,
            "Who": action.display_recipients(),
            "When": action.when,
            "Subject": action.subject,
            "Detail": action.body,
        }
        for action in check.confirmed_actions()
    ]
