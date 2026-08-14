"""What a meeting's agenda is, and what it refuses to become (requirement 6.7, Phase 1).

The tag-canonicalisation tests carry the most weight here. The MoM groups by exact tag and
the creator's coverage count reads the same field, so a tag that slips through uncorrected
becomes a silent third category in both.
"""

from meetings.model import (
    OPENING_TAG,
    OTHER_TAG,
    AgendaItem,
    Meeting,
    agenda_from_json,
    agenda_to_json,
    canonical_tag,
)


def _meeting() -> Meeting:
    return Meeting(
        subject="PO No 123",
        agenda=[
            AgendaItem(item="Delivery timeline", ai_note="Standard SLA is 30 days from PO date."),
            AgendaItem(item="Payment terms"),
        ],
    )


class TestAgendaSerialisation:
    def test_an_agenda_survives_a_round_trip(self):
        original = _meeting().agenda
        restored = agenda_from_json(agenda_to_json(original))

        assert [item.item for item in restored] == ["Delivery timeline", "Payment terms"]
        assert restored[0].ai_note == "Standard SLA is 30 days from PO date."

    def test_a_table_item_from_a_later_phase_is_dropped(self):
        # Phase 1 has no UI that could render a table item; carrying one through would put
        # a question on the agenda that the invitee has no way to answer.
        stored = (
            '{"version": 1, "items": ['
            '{"type": "discussion", "item": "Delivery timeline", "ai_note": ""},'
            '{"type": "table", "item": "Outstanding bills", "ai_note": ""}]}'
        )
        assert [item.item for item in agenda_from_json(stored)] == ["Delivery timeline"]

    def test_an_item_with_no_title_is_dropped(self):
        stored = '{"items": [{"type": "discussion", "item": "   ", "ai_note": "note"}]}'
        assert agenda_from_json(stored) == []

    def test_unreadable_json_becomes_an_empty_agenda_rather_than_an_error(self):
        # An exception here would take down the invitee's whole chat screen over a stored
        # format problem they can do nothing about.
        assert agenda_from_json("not json at all") == []
        assert agenda_from_json("") == []
        assert agenda_from_json("[1, 2, 3]") == []


class TestCanonicalTag:
    def test_an_exact_title_is_kept(self):
        assert canonical_tag("Delivery timeline", _meeting()) == "Delivery timeline"

    def test_case_and_whitespace_differences_still_match(self):
        assert canonical_tag("  delivery TIMELINE ", _meeting()) == "Delivery timeline"

    def test_a_near_miss_becomes_other_rather_than_a_new_category(self):
        assert canonical_tag("Delivery timelines", _meeting()) == OTHER_TAG

    def test_an_empty_tag_becomes_other(self):
        assert canonical_tag("", _meeting()) == OTHER_TAG

    def test_the_opening_tag_is_preserved(self):
        # The opening message names every agenda item without discussing any of them, so it
        # must not resolve to one.
        assert canonical_tag(OPENING_TAG, _meeting()) == OPENING_TAG


class TestMeeting:
    def test_a_blank_subject_still_displays_as_something(self):
        assert Meeting().display_subject() == "Untitled meeting"

    def test_agenda_titles_are_listed_in_order(self):
        assert _meeting().agenda_titles() == ["Delivery timeline", "Payment terms"]
