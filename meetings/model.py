"""What a meeting, an invitee, a session and a message are (requirement 6.7, Phase 1).

No Streamlit and no database here, for the same reason `chat_types/model.py` has neither:
`meetings/db.py` stores these and both pages hold them, and keeping the shape free of both
is what makes it testable without `AppTest`.

Phase 1 is **discussion agenda items only**. Table items (spec 3a), evaluation fields
(3b) and the comparison matrices are later phases. The one concession made to them here is
that `AgendaItem` stores its `type` even though Phase 1 only ever writes `"discussion"` —
that is what lets a later phase add table items by adding a table, rather than by migrating
every `agenda_json` already written. `agenda_from_json` drops anything that isn't a
discussion item, so Phase 1 code never has to understand one.
"""

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# The stored JSON carries its own version, so an agenda saved today can still be read back
# once table items widen the shape.
SCHEMA_VERSION = 1

DISCUSSION_ITEM = "discussion"

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
    """One thing to be discussed, with the specific knowledge the AI needs to discuss it.

    `ai_note` is the creator's 1-2 lines of ground truth for this item ("Standard SLA is 30
    days from PO date") — it goes into the system prompt so the AI argues from the
    creator's facts rather than inventing plausible ones.
    """

    item: str
    ai_note: str = ""
    item_type: str = DISCUSSION_ITEM


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
    """Rebuilds the agenda from stored JSON, keeping only discussion items.

    Never raises. An agenda that can't be parsed comes back empty, which the pages already
    handle (a meeting with no agenda items is a legal, if unhelpful, meeting) — whereas an
    exception here would take down the invitee's whole chat screen over a stored-format
    problem they can do nothing about.
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
        if item_type != DISCUSSION_ITEM:
            # A later phase's table item, read by Phase 1 code. Dropping it is right:
            # Phase 1 has no UI that could render one, and carrying it through would put
            # an item on the agenda list that the invitee is asked about but cannot answer.
            logger.info("Skipping a '%s' agenda item — this phase handles discussion items only.", item_type)
            continue
        items.append(AgendaItem(item=title, ai_note=str(raw.get("ai_note") or "")))

    return items
