"""What a meeting's agenda is, and what it refuses to become (requirement 6.7).

The tag-canonicalisation tests carry the most weight here. The MoM groups by exact tag and
the creator's coverage count reads the same field, so a tag that slips through uncorrected
becomes a silent third category in both.
"""

from meetings.model import (
    DISCUSSION_ITEM,
    OPENING_TAG,
    OTHER_TAG,
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    EvaluationAnswer,
    EvaluationField,
    Meeting,
    agenda_from_json,
    agenda_to_json,
    canonical_tag,
    evaluation_buckets_from_text,
    evaluation_buckets_to_text,
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

    def test_an_agenda_written_by_phase_one_reads_back_unchanged(self):
        # The reason Phase 1 wrote `type` on every item: Phase 2 adds a second type without
        # touching a single stored agenda.
        stored = (
            '{"version": 1, "items": ['
            '{"type": "discussion", "item": "Delivery timeline", "ai_note": "SLA is 30 days."}]}'
        )
        restored = agenda_from_json(stored)

        assert [item.item for item in restored] == ["Delivery timeline"]
        assert restored[0].item_type == DISCUSSION_ITEM
        assert restored[0].is_table() is False

    def test_a_table_item_keeps_its_type(self):
        stored = (
            '{"version": 1, "items": ['
            '{"type": "discussion", "item": "Delivery timeline", "ai_note": ""},'
            '{"type": "table", "item": "Outstanding bills", "ai_note": "Confirm each date."}]}'
        )
        restored = agenda_from_json(stored)

        assert [item.item for item in restored] == ["Delivery timeline", "Outstanding bills"]
        assert restored[1].is_table() is True

    def test_an_item_type_nobody_recognises_is_read_as_a_discussion_rather_than_dropped(self):
        # Reverses Phase 1's rule deliberately: with two real types, a third is far more
        # likely to be a typo than a future feature, and dropping it would silently remove
        # something the invitee is never then asked about.
        stored = '{"items": [{"type": "disccusion", "item": "Payment terms", "ai_note": ""}]}'
        restored = agenda_from_json(stored)

        assert [item.item for item in restored] == ["Payment terms"]
        assert restored[0].item_type == DISCUSSION_ITEM

    def test_a_table_item_survives_a_round_trip(self):
        original = [
            AgendaItem(item="Delivery timeline"),
            AgendaItem(item="Outstanding bills", item_type=TABLE_ITEM),
        ]
        restored = agenda_from_json(agenda_to_json(original))

        assert [item.item_type for item in restored] == [DISCUSSION_ITEM, TABLE_ITEM]

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

    def test_discussion_and_table_items_are_told_apart(self):
        meeting = Meeting(
            agenda=[
                AgendaItem(item="Delivery timeline"),
                AgendaItem(item="Outstanding bills", item_type=TABLE_ITEM),
                AgendaItem(item="Payment terms"),
            ]
        )

        assert [item.item for item in meeting.discussion_items()] == ["Delivery timeline", "Payment terms"]
        assert [item.item for item in meeting.table_items()] == ["Outstanding bills"]
        # `agenda_titles` stays the whole agenda — it feeds the tagging instruction, and an
        # exchange about a grid is legitimately tagged with the grid's title.
        assert len(meeting.agenda_titles()) == 3


class TestAgendaTable:
    def _table(self) -> AgendaTable:
        return AgendaTable(
            item_ref="Outstanding bills",
            locked_columns=["Bill No", "Amount"],
            editable_columns=["Expected date"],
            base_data=[{"Bill No": "B-1", "Amount": "100"}],
        )

    def test_columns_read_locked_first_then_editable(self):
        # The order the invitee reads them in: what they are being asked about, then where
        # they answer.
        assert self._table().all_columns() == ["Bill No", "Amount", "Expected date"]

    def test_two_tables_with_the_same_columns_share_a_signature(self):
        assert self._table().signature() == self._table().signature()

    def test_a_different_column_set_is_a_different_signature(self):
        other = self._table()
        other.editable_columns = ["Remarks"]
        assert other.signature() != self._table().signature()

    def test_the_row_count_is_the_uploaded_sheets(self):
        assert self._table().row_count() == 1


class TestEvaluationFields:
    def test_buckets_round_trip_through_the_editors_cell(self):
        assert evaluation_buckets_from_text("Low, Medium, High") == ["Low", "Medium", "High"]
        assert evaluation_buckets_to_text(["Low", "Medium", "High"]) == "Low, Medium, High"

    def test_blank_buckets_mean_no_classification(self):
        assert evaluation_buckets_from_text("") == []
        assert evaluation_buckets_from_text("  ,  ,") == []

    def test_a_bucket_containing_a_slash_is_not_split_on_it(self):
        # Only commas separate. A creator who wrote "Medium / High" meant one bucket.
        assert evaluation_buckets_from_text("Low, Medium / High") == ["Low", "Medium / High"]

    def test_an_answer_shows_its_raw_text_and_its_tag(self):
        answer = EvaluationAnswer(raw_answer="8 years", classified_tag="High")
        assert answer.display() == "8 years (High)"

    def test_an_answer_with_no_bucket_shows_only_what_was_said(self):
        assert EvaluationAnswer(raw_answer="8 years").display() == "8 years"

    def test_an_unanswered_field_displays_as_nothing(self):
        assert EvaluationAnswer().display() == ""

    def test_a_field_defaults_to_no_buckets(self):
        assert EvaluationField(question="Years of experience?").buckets == []
