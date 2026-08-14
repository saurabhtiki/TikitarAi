"""What a meeting, an invitee, a session and a message are (requirement 6.7).

No Streamlit and no database here, for the same reason `chat_types/model.py` has neither:
`meetings/db.py` stores these and both pages hold them, and keeping the shape free of both
is what makes it testable without `AppTest`.

Phase 1 shipped discussion agenda items only, but wrote each item's `type` anyway. That is
what let Phase 2 add **table items** (spec 3a) without migrating a single `agenda_json`
already written: an agenda saved by Phase 1 reads back identically, and the new item type is
simply one the old rows never contained. Phase 2 also adds **evaluation fields** (spec 3b),
which are their own table rather than part of the agenda — they are questions woven into the
discussion, not items on it.
"""

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# The stored JSON carries its own version. Still 1: Phase 2 only ever *adds* keys that a
# Phase 1 agenda simply doesn't have, and every reader here defaults a missing one.
SCHEMA_VERSION = 1

DISCUSSION_ITEM = "discussion"

# An agenda item whose substance is a grid rather than a conversation (spec 3a). It gets its
# own tab and its own `meeting_agenda_tables` row; the chat never asks for its contents.
TABLE_ITEM = "table"

ITEM_TYPES = (DISCUSSION_ITEM, TABLE_ITEM)

# The tag an exchange gets when it doesn't belong to any agenda item. Spec 2.5: off-agenda
# points are captured in the MoM rather than forced into a defined item, so this is a real
# tag with a section of its own, not a null.
OTHER_TAG = "Other/Extra"

# The opening message belongs to no agenda item and must not count as one being discussed.
OPENING_TAG = "Opening"

SENDER_USER = "user"
SENDER_AI = "ai"


@dataclass
class AgendaItem:
    """One thing to be covered, with the specific knowledge the AI needs to cover it.

    `ai_note` is the creator's 1-2 lines of ground truth for this item ("Standard SLA is 30
    days from PO date") — it goes into the system prompt so the AI argues from the
    creator's facts rather than inventing plausible ones.

    A `TABLE_ITEM` carries no data of its own here: the grid lives in `AgendaTable`, keyed
    back to this item by title. The title is the join, not an id, because the agenda is
    stored as one JSON blob that gets rewritten whole on every edit — an id minted inside it
    would have nothing durable to be minted from.
    """

    item: str
    ai_note: str = ""
    item_type: str = DISCUSSION_ITEM

    def is_table(self) -> bool:
        return self.item_type == TABLE_ITEM


@dataclass
class Meeting:
    """A subject-based conversation, as its creator set it up.

    `persona` is a **snapshot** taken at creation from the creator's saved default, never a
    live read of it: spec 1 is explicit that editing the default must not rewrite the
    persona of meetings already sent out and possibly already answered.
    """

    meeting_id: int | None = None
    subject: str = ""
    created_by: int | None = None
    meeting_context: str = ""
    persona: str = ""
    context_sop: str = ""
    agenda: list[AgendaItem] = field(default_factory=list)
    created_at: str = ""

    def agenda_titles(self) -> list[str]:
        return [item.item for item in self.agenda]

    def discussion_items(self) -> list[AgendaItem]:
        """The items the chat works through. What `agenda_titles` used to mean, exactly."""
        return [item for item in self.agenda if not item.is_table()]

    def table_items(self) -> list[AgendaItem]:
        """The items that render as their own grid tab (spec 3a)."""
        return [item for item in self.agenda if item.is_table()]

    def display_subject(self) -> str:
        return self.subject.strip() or "Untitled meeting"


@dataclass
class Invitee:
    """One person invited to a meeting, and their private way in.

    `access_code` is the decrypted code, present only where the creator is allowed to see
    it (the Share screen). Rows read for the invitee side leave it empty — nothing on that
    side needs the code back, and carrying it around would be one more place to leak it.
    """

    invitee_id: int | None = None
    meeting_id: int | None = None
    name: str = ""
    email: str = ""
    token: str = ""
    access_code: str = ""


@dataclass
class ChatMessage:
    """One turn, as stored. `agenda_tag` is what the AI said this exchange was about."""

    message_id: int | None = None
    sender: str = SENDER_USER
    text: str = ""
    agenda_tag: str = ""
    created_at: str = ""

    def is_from_ai(self) -> bool:
        return self.sender == SENDER_AI


@dataclass
class AgendaTable:
    """The grid behind one table agenda item (spec 3a).

    `base_data` is the creator's uploaded sheet, frozen at upload: every invitee is answering
    the *same* rows, which is the entire premise of the cross-invitee comparison. It is
    stored as a list of row dicts rather than as the original file, because the file is a
    source and this is the record — re-parsing a spreadsheet at read time would let a
    changed pandas version quietly shift what an invitee was asked.

    `locked_columns` are reference-only (Bill No, Due Date); `editable_columns` are what the
    invitee fills. A column named in neither is treated as locked — the safe reading of a
    column nobody classified is that it is not an invitation to type.
    """

    table_id: int | None = None
    meeting_id: int | None = None
    item_ref: str = ""
    source_file: str = ""
    locked_columns: list[str] = field(default_factory=list)
    editable_columns: list[str] = field(default_factory=list)
    base_data: list[dict] = field(default_factory=list)

    def row_count(self) -> int:
        return len(self.base_data)

    def all_columns(self) -> list[str]:
        """Locked columns first, then editable — the order the invitee reads them in."""
        return [*self.locked_columns, *self.editable_columns]

    def signature(self) -> tuple[str, ...]:
        """What makes two tables comparable (spec 3a's "identical table template" rule)."""
        return tuple(self.all_columns())


@dataclass
class EvaluationField:
    """One short question asked across every invitee, for comparison (spec 3b).

    `buckets` is optional: with them the Extraction Agent also assigns a classification tag,
    without them only the raw answer is pulled. The raw answer is kept either way, so a
    bucket added later can be re-run without having lost the detail it classifies.
    """

    field_id: int | None = None
    meeting_id: int | None = None
    question: str = ""
    buckets: list[str] = field(default_factory=list)
    position: int = 0


@dataclass
class EvaluationAnswer:
    """What one invitee said about one evaluation field."""

    field_id: int | None = None
    invitee_id: int | None = None
    raw_answer: str = ""
    classified_tag: str = ""
    updated_at: str = ""

    def display(self) -> str:
        """Raw answer with its tag in brackets — the cell shape spec 3b's matrix shows."""
        raw = self.raw_answer.strip()
        tag = self.classified_tag.strip()
        if raw and tag:
            return f"{raw} ({tag})"
        return raw or tag


@dataclass
class ChatSession:
    """One invitee's progress through one meeting.

    `running_summary` is here because the Chat Agent needs it every turn. It is **never**
    an input to the MoM — see `summary_agent`, which takes a message list and has no way to
    reach this object at all.
    """

    meeting_id: int | None = None
    invitee_id: int | None = None
    closed: bool = False
    closed_at: str = ""
    running_summary: str = ""
    folded_through_message_id: int = 0
    last_active_at: str = ""


def canonical_tag(tag: str, meeting: Meeting) -> str:
    """The agenda title this tag names, or `OTHER_TAG`.

    The model is told to answer with one of the exact titles, but a model that returns
    "Delivery timelines" for an item called "Delivery timeline" would otherwise create a
    silent third category that the MoM groups separately and the coverage count
    double-counts. Matching is case-insensitive and whitespace-tolerant; anything that
    still doesn't match a real item becomes `OTHER_TAG`, which is a section that exists
    precisely to hold what didn't fit.
    """
    wanted = (tag or "").strip().lower()
    if not wanted:
        return OTHER_TAG
    if wanted == OPENING_TAG.lower():
        return OPENING_TAG
    for item in meeting.agenda:
        if item.item.strip().lower() == wanted:
            return item.item
    return OTHER_TAG


def agenda_to_json(agenda: list[AgendaItem]) -> str:
    """Serialises the agenda for the `meetings.agenda_json` column."""
    payload = {
        "version": SCHEMA_VERSION,
        "items": [
            {"type": item.item_type, "item": item.item, "ai_note": item.ai_note}
            for item in agenda
        ],
    }
    return json.dumps(payload, indent=2)


def agenda_from_json(text: str) -> list[AgendaItem]:
    """Rebuilds the agenda from stored JSON.

    An item whose `type` is neither known value is read as a discussion item rather than
    dropped. That reverses Phase 1's rule, and deliberately: dropping was right while there
    was no UI that could render anything else, but now that there are two real types, an
    unreadable third is far more likely to be a typo in a hand-edited row than a future
    feature — and a dropped item is one the invitee is never asked about, silently.

    Never raises. An agenda that can't be parsed at all comes back empty, which the pages
    already handle (a meeting with no agenda items is a legal, if unhelpful, meeting) —
    whereas an exception here would take down the invitee's whole chat screen over a
    stored-format problem they can do nothing about.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError):
        logger.exception("A stored agenda could not be parsed; treating it as empty.")
        return []

    if not isinstance(payload, dict):
        logger.warning("A stored agenda was %s, not an object.", type(payload).__name__)
        return []

    items = []
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item_type = str(raw.get("type") or DISCUSSION_ITEM)
        title = str(raw.get("item") or "").strip()
        if not title:
            continue
        if item_type not in ITEM_TYPES:
            logger.warning(
                "Agenda item '%s' has unknown type '%s'; reading it as a discussion item.",
                title,
                item_type,
            )
            item_type = DISCUSSION_ITEM
        items.append(
            AgendaItem(item=title, ai_note=str(raw.get("ai_note") or ""), item_type=item_type)
        )

    return items


def evaluation_buckets_to_text(buckets: list[str]) -> str:
    """The buckets as one comma-separated cell, for the creator's editor."""
    return ", ".join(bucket for bucket in buckets if bucket.strip())


def evaluation_buckets_from_text(text: str) -> list[str]:
    """Buckets typed as a comma-separated cell.

    Split on commas only — a bucket like "Medium / High" is one the creator wrote with a
    slash in it on purpose, and `llm.models.parse_model_names` splitting on newlines *and*
    commas is right for its input and wrong for a single-line grid cell.
    """
    return [part.strip() for part in str(text or "").split(",") if part.strip()]
