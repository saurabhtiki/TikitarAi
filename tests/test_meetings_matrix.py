"""The cross-invitee comparison matrices (requirement 6.7, Phase 2, spec 8).

Pure frame-building, so all of it is testable without Streamlit or a database — which is why
`meetings/matrix.py` exists as its own module rather than as helpers on the page.

The distinction these tests protect hardest is **"not started" versus "not discussed"**. They
look alike in a grid and mean opposite things: one invitee hasn't answered yet, the other was
asked and didn't. Collapsing them would make a matrix that reads as an indictment of someone
who simply hasn't opened their link.
"""

import pytest

from meetings.matrix import (
    NOT_DISCUSSED,
    NOT_STARTED,
    consolidated_frame,
    evaluation_frame,
    invitee_labels,
    table_comparison_frame,
)
from meetings.model import (
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    EvaluationAnswer,
    EvaluationField,
    Meeting,
)
from meetings.summary_agent import AgendaItemSummary, MeetingSummary


@pytest.fixture
def invitees():
    return [
        {"invitee_id": 1, "name": "Raj", "email": "raj@a.com"},
        {"invitee_id": 2, "name": "Sam", "email": "sam@b.com"},
    ]


@pytest.fixture
def meeting():
    return Meeting(
        subject="Vendor comparison",
        agenda=[
            AgendaItem(item="Delivery timeline"),
            AgendaItem(item="Outstanding bills", item_type=TABLE_ITEM),
        ],
    )


class TestInviteeLabels:
    def test_a_name_is_the_column_heading(self, invitees):
        assert list(invitee_labels(invitees).values()) == ["Raj", "Sam"]

    def test_two_people_with_the_same_name_are_told_apart_by_email(self):
        labels = invitee_labels(
            [
                {"invitee_id": 1, "name": "Raj", "email": "raj@a.com"},
                {"invitee_id": 2, "name": "Raj", "email": "raj@b.com"},
            ]
        )

        assert labels[1] == "Raj (raj@a.com)"
        assert labels[2] == "Raj (raj@b.com)"

    def test_an_invitee_with_no_name_falls_back_to_their_email(self):
        labels = invitee_labels([{"invitee_id": 1, "name": "", "email": "raj@a.com"}])
        assert labels[1] == "raj@a.com"


class TestConsolidated:
    def test_rows_are_agenda_items_and_columns_are_invitees(self, meeting, invitees):
        frame = consolidated_frame(meeting, invitees, {})

        assert list(frame.columns) == ["Item", "Raj", "Sam"]
        assert frame["Item"].tolist() == ["Delivery timeline", "Outstanding bills"]

    def test_a_cell_shows_what_that_invitee_said(self, meeting, invitees):
        summaries = {
            1: MeetingSummary(
                agenda_items=[
                    AgendaItemSummary(item="Delivery timeline", discussed=True, notes="45 days quoted.")
                ]
            )
        }
        frame = consolidated_frame(meeting, invitees, summaries)

        assert frame.loc[0, "Raj"] == "45 days quoted."

    def test_an_invitee_who_has_generated_nothing_reads_as_not_started(self, meeting, invitees):
        frame = consolidated_frame(meeting, invitees, {})
        assert frame.loc[0, "Sam"] == NOT_STARTED

    def test_an_item_they_were_asked_about_and_skipped_reads_as_not_discussed(self, meeting, invitees):
        summaries = {
            2: MeetingSummary(
                agenda_items=[AgendaItemSummary(item="Delivery timeline", discussed=False, notes="")]
            )
        }
        frame = consolidated_frame(meeting, invitees, summaries)

        assert frame.loc[0, "Sam"] == NOT_DISCUSSED

    def test_a_table_items_cell_is_its_completion_line(self, meeting, invitees):
        summaries = {
            1: MeetingSummary(
                agenda_items=[
                    AgendaItemSummary(
                        item="Outstanding bills",
                        discussed=True,
                        notes="12 of 40 row(s) filled (30%)",
                        is_table=True,
                    )
                ]
            )
        }
        frame = consolidated_frame(meeting, invitees, summaries)

        assert frame.loc[1, "Raj"] == "12 of 40 row(s) filled (30%)"

    def test_a_meeting_with_no_agenda_produces_an_empty_frame_with_headings(self, invitees):
        frame = consolidated_frame(Meeting(subject="Empty"), invitees, {})

        assert list(frame.columns) == ["Item", "Raj", "Sam"]
        assert len(frame) == 0


class TestEvaluation:
    @pytest.fixture
    def fields(self):
        return [
            EvaluationField(field_id=1, question="Years of experience?", buckets=["Low", "High"]),
            EvaluationField(field_id=2, question="Employee strength?"),
        ]

    def test_rows_are_questions_and_cells_carry_both_halves(self, fields, invitees):
        answers = [
            EvaluationAnswer(field_id=1, invitee_id=1, raw_answer="8 years", classified_tag="High"),
            EvaluationAnswer(field_id=1, invitee_id=2, raw_answer="3 years", classified_tag="Low"),
        ]
        frame = evaluation_frame(fields, invitees, answers)

        assert frame["Field"].tolist() == ["Years of experience?", "Employee strength?"]
        # The raw answer stays beside the tag: the bucket is a model's judgement, and seeing
        # what it judged is what lets a creator notice when it is wrong.
        assert frame.loc[0, "Raj"] == "8 years (High)"
        assert frame.loc[0, "Sam"] == "3 years (Low)"

    def test_an_unextracted_cell_reads_as_not_started(self, fields, invitees):
        frame = evaluation_frame(fields, invitees, [])
        assert frame.loc[1, "Raj"] == NOT_STARTED

    def test_an_answer_with_no_bucket_shows_only_what_was_said(self, fields, invitees):
        answers = [EvaluationAnswer(field_id=2, invitee_id=1, raw_answer="150")]
        assert evaluation_frame(fields, invitees, answers).loc[1, "Raj"] == "150"


class TestTableComparison:
    @pytest.fixture
    def table(self):
        return AgendaTable(
            table_id=1,
            item_ref="Outstanding bills",
            locked_columns=["Bill No", "Amount"],
            editable_columns=["Expected date"],
            base_data=[{"Bill No": "B-1", "Amount": "100"}, {"Bill No": "B-2", "Amount": "250"}],
        )

    def test_rows_are_labelled_by_the_first_locked_column(self, table, invitees):
        frame = table_comparison_frame(table, "Expected date", invitees, {})
        assert frame["Item"].tolist() == ["B-1", "B-2"]

    def test_each_invitees_answer_lands_in_their_own_column(self, table, invitees):
        responses = {
            1: {0: {"Expected date": "2026-09-01"}},
            2: {0: {"Expected date": "2026-10-15"}},
        }
        frame = table_comparison_frame(table, "Expected date", invitees, responses)

        assert frame.loc[0, "Raj"] == "2026-09-01"
        assert frame.loc[0, "Sam"] == "2026-10-15"

    def test_an_unfilled_row_reads_as_not_started(self, table, invitees):
        frame = table_comparison_frame(table, "Expected date", invitees, {})
        assert frame.loc[1, "Raj"] == NOT_STARTED

    def test_a_grid_with_no_locked_column_falls_back_to_row_numbers(self, invitees):
        table = AgendaTable(
            table_id=1,
            editable_columns=["Value"],
            base_data=[{"Value": ""}, {"Value": ""}],
        )
        frame = table_comparison_frame(table, "Value", invitees, {})

        assert frame["Item"].tolist() == ["Row 1", "Row 2"]

    def test_a_blank_label_value_falls_back_to_a_row_number(self, table, invitees):
        table.base_data[1]["Bill No"] = ""
        frame = table_comparison_frame(table, "Expected date", invitees, {})

        assert frame["Item"].tolist() == ["B-1", "Row 2"]
