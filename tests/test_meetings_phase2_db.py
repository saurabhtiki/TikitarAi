"""Grids, answers and evaluation fields in SQLite (requirement 6.7, Phase 2, spec 3a/3b).

Separate from `test_meetings_db.py` for the same reason `db.py` keeps its Phase 2 queries in
their own section: these tables are new, none of them needed a migration, and the invariants
worth protecting are their own.

The two that carry weight:

- **A stored response row is a filled row.** Completion is a `COUNT(*)`, so a blank row that
  got stored anyway would show up as progress that nobody made.
- **Editing an evaluation question keeps its answers.** The obvious implementation —
  delete-all then re-insert — cascades every extracted answer away when a creator fixes a
  typo, and nothing on screen would say so.
"""

import pytest

from auth.db import init_db, seed_default_admin
from meetings import db as meetings_db
from meetings.exceptions import MeetingStorageError
from meetings.model import (
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    EvaluationAnswer,
    EvaluationField,
    Meeting,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    meetings_db.init_meetings_tables(path)
    return path


@pytest.fixture
def meeting(db_path):
    return meetings_db.create_meeting(
        1,
        Meeting(
            subject="Vendor comparison",
            agenda=[
                AgendaItem(item="Delivery timeline"),
                AgendaItem(item="Outstanding bills", item_type=TABLE_ITEM),
            ],
        ),
        db_path=db_path,
    )


@pytest.fixture
def invitee(db_path, meeting):
    return meetings_db.add_invitee(
        meeting.meeting_id, 1, "Raj", "raj@vendor.com", "token-abc", "enc", db_path=db_path
    )


def _table(meeting) -> AgendaTable:
    return AgendaTable(
        meeting_id=meeting.meeting_id,
        item_ref="Outstanding bills",
        source_file="bills.csv",
        locked_columns=["Bill No"],
        editable_columns=["Expected date"],
        base_data=[{"Bill No": "B-1"}, {"Bill No": "B-2"}, {"Bill No": "B-3"}],
    )


class TestAgendaTables:
    def test_a_grid_is_saved_and_read_back_whole(self, db_path, meeting):
        meetings_db.save_agenda_table(meeting.meeting_id, 1, _table(meeting), db_path=db_path)
        stored = meetings_db.find_agenda_table(meeting.meeting_id, "Outstanding bills", db_path=db_path)

        assert stored.locked_columns == ["Bill No"]
        assert stored.editable_columns == ["Expected date"]
        assert stored.row_count() == 3
        assert stored.source_file == "bills.csv"

    def test_an_item_with_no_grid_yet_reads_as_none(self, db_path, meeting):
        assert meetings_db.find_agenda_table(meeting.meeting_id, "Outstanding bills", db_path=db_path) is None

    def test_re_uploading_replaces_rather_than_adding_a_second_grid(self, db_path, meeting):
        meetings_db.save_agenda_table(meeting.meeting_id, 1, _table(meeting), db_path=db_path)
        replacement = _table(meeting)
        replacement.base_data = [{"Bill No": "B-9"}]
        meetings_db.save_agenda_table(meeting.meeting_id, 1, replacement, db_path=db_path)

        stored = meetings_db.list_agenda_tables(meeting.meeting_id, db_path=db_path)
        assert len(stored) == 1
        assert stored[0].row_count() == 1

    def test_replacing_a_grid_clears_the_answers_to_the_old_one(self, db_path, meeting, invitee):
        table_id = meetings_db.save_agenda_table(meeting.meeting_id, 1, _table(meeting), db_path=db_path)
        meetings_db.save_table_responses(table_id, invitee, {0: {"Expected date": "2026-09-01"}}, db_path=db_path)

        meetings_db.save_agenda_table(meeting.meeting_id, 1, _table(meeting), db_path=db_path)

        # Those answers were given about a different sheet. Keeping them would re-attribute
        # one row's answer to whatever now sits in that position.
        assert meetings_db.load_table_responses(table_id, invitee, db_path=db_path) == {}

    def test_another_accounts_meeting_cannot_have_a_grid_attached(self, db_path, meeting):
        with pytest.raises(MeetingStorageError):
            meetings_db.save_agenda_table(meeting.meeting_id, 2, _table(meeting), db_path=db_path)

    def test_a_grid_with_no_agenda_item_is_refused(self, db_path, meeting):
        orphan = _table(meeting)
        orphan.item_ref = "   "

        with pytest.raises(MeetingStorageError, match="belong to an agenda item"):
            meetings_db.save_agenda_table(meeting.meeting_id, 1, orphan, db_path=db_path)

    def test_deleting_a_grid_takes_its_answers_with_it(self, db_path, meeting, invitee):
        table_id = meetings_db.save_agenda_table(meeting.meeting_id, 1, _table(meeting), db_path=db_path)
        meetings_db.save_table_responses(table_id, invitee, {0: {"Expected date": "x"}}, db_path=db_path)

        meetings_db.delete_agenda_table(meeting.meeting_id, 1, "Outstanding bills", db_path=db_path)

        assert meetings_db.list_agenda_tables(meeting.meeting_id, db_path=db_path) == []
        assert meetings_db.load_table_responses(table_id, invitee, db_path=db_path) == {}

    def test_another_account_cannot_delete_a_grid(self, db_path, meeting):
        meetings_db.save_agenda_table(meeting.meeting_id, 1, _table(meeting), db_path=db_path)

        with pytest.raises(MeetingStorageError):
            meetings_db.delete_agenda_table(meeting.meeting_id, 2, "Outstanding bills", db_path=db_path)

        assert len(meetings_db.list_agenda_tables(meeting.meeting_id, db_path=db_path)) == 1


class TestTableResponses:
    @pytest.fixture
    def table_id(self, db_path, meeting):
        return meetings_db.save_agenda_table(meeting.meeting_id, 1, _table(meeting), db_path=db_path)

    def test_answers_round_trip_by_row(self, db_path, table_id, invitee):
        meetings_db.save_table_responses(
            table_id, invitee, {0: {"Expected date": "2026-09-01"}, 2: {"Expected date": "2026-10-01"}},
            db_path=db_path,
        )

        assert meetings_db.load_table_responses(table_id, invitee, db_path=db_path) == {
            0: {"Expected date": "2026-09-01"},
            2: {"Expected date": "2026-10-01"},
        }

    def test_a_blank_row_is_never_stored(self, db_path, table_id, invitee):
        saved = meetings_db.save_table_responses(
            table_id, invitee, {0: {"Expected date": "   "}, 1: {"Expected date": ""}}, db_path=db_path
        )

        assert saved == 0
        assert meetings_db.load_table_responses(table_id, invitee, db_path=db_path) == {}

    def test_clearing_a_row_takes_it_back_out_of_the_count(self, db_path, table_id, invitee):
        meetings_db.save_table_responses(table_id, invitee, {0: {"Expected date": "x"}}, db_path=db_path)
        saved = meetings_db.save_table_responses(table_id, invitee, {0: {"Expected date": ""}}, db_path=db_path)

        assert saved == 0
        assert meetings_db.count_table_responses(meeting_id_of(db_path, table_id), db_path=db_path) == {}

    def test_saving_replaces_the_whole_set_rather_than_merging(self, db_path, table_id, invitee):
        meetings_db.save_table_responses(
            table_id, invitee, {0: {"Expected date": "x"}, 1: {"Expected date": "y"}}, db_path=db_path
        )
        meetings_db.save_table_responses(table_id, invitee, {1: {"Expected date": "y"}}, db_path=db_path)

        assert list(meetings_db.load_table_responses(table_id, invitee, db_path=db_path)) == [1]

    def test_one_invitees_answers_are_not_anothers(self, db_path, meeting, table_id, invitee):
        other = meetings_db.add_invitee(
            meeting.meeting_id, 1, "Sam", "sam@vendor.com", "token-two", "enc", db_path=db_path
        )
        meetings_db.save_table_responses(table_id, invitee, {0: {"Expected date": "mine"}}, db_path=db_path)

        assert meetings_db.load_table_responses(table_id, other, db_path=db_path) == {}

    def test_every_invitees_rows_come_back_together_for_the_matrix(self, db_path, meeting, table_id, invitee):
        other = meetings_db.add_invitee(
            meeting.meeting_id, 1, "Sam", "sam@vendor.com", "token-two", "enc", db_path=db_path
        )
        meetings_db.save_table_responses(table_id, invitee, {0: {"Expected date": "a"}}, db_path=db_path)
        meetings_db.save_table_responses(table_id, other, {0: {"Expected date": "b"}}, db_path=db_path)

        everyone = meetings_db.load_all_table_responses(table_id, db_path=db_path)
        assert everyone[invitee][0]["Expected date"] == "a"
        assert everyone[other][0]["Expected date"] == "b"

    def test_progress_reports_filled_against_total(self, db_path, meeting, table_id, invitee):
        meetings_db.save_table_responses(table_id, invitee, {0: {"Expected date": "x"}}, db_path=db_path)

        assert meetings_db.table_progress(meeting.meeting_id, invitee, db_path=db_path) == {
            "Outstanding bills": (1, 3)
        }

    def test_progress_for_an_untouched_grid_is_zero_of_its_rows(self, db_path, meeting, table_id, invitee):
        assert meetings_db.table_progress(meeting.meeting_id, invitee, db_path=db_path) == {
            "Outstanding bills": (0, 3)
        }


def meeting_id_of(db_path, table_id: int) -> int:
    """The meeting a grid belongs to — only needed to aim `count_table_responses`."""
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT meeting_id FROM meeting_agenda_tables WHERE table_id = ?;", (table_id,)
        ).fetchone()
    return row[0]


class TestEvaluationFields:
    def test_fields_are_saved_in_order(self, db_path, meeting):
        meetings_db.replace_evaluation_fields(
            meeting.meeting_id,
            1,
            [
                EvaluationField(question="Years of experience?", buckets=["Low", "High"]),
                EvaluationField(question="Employee strength?"),
            ],
            db_path=db_path,
        )

        stored = meetings_db.list_evaluation_fields(meeting.meeting_id, db_path=db_path)
        assert [field.question for field in stored] == ["Years of experience?", "Employee strength?"]
        assert stored[0].buckets == ["Low", "High"]
        assert stored[1].buckets == []

    def test_a_meeting_with_none_defined_has_the_feature_off(self, db_path, meeting):
        assert meetings_db.list_evaluation_fields(meeting.meeting_id, db_path=db_path) == []

    def test_a_blank_question_is_skipped(self, db_path, meeting):
        meetings_db.replace_evaluation_fields(
            meeting.meeting_id, 1, [EvaluationField(question="   ")], db_path=db_path
        )

        assert meetings_db.list_evaluation_fields(meeting.meeting_id, db_path=db_path) == []

    def test_editing_a_question_keeps_the_answers_already_extracted(self, db_path, meeting, invitee):
        meetings_db.replace_evaluation_fields(
            meeting.meeting_id, 1, [EvaluationField(question="Years of experience?")], db_path=db_path
        )
        field = meetings_db.list_evaluation_fields(meeting.meeting_id, db_path=db_path)[0]
        meetings_db.save_evaluation_answers(
            invitee, [EvaluationAnswer(field_id=field.field_id, raw_answer="8 years")], db_path=db_path
        )

        # The creator adds buckets to the question they already asked.
        field.buckets = ["Low", "High"]
        meetings_db.replace_evaluation_fields(meeting.meeting_id, 1, [field], db_path=db_path)

        answers = meetings_db.list_evaluation_answers(meeting.meeting_id, db_path=db_path)
        assert [answer.raw_answer for answer in answers] == ["8 years"]

    def test_removing_a_question_removes_its_answers(self, db_path, meeting, invitee):
        meetings_db.replace_evaluation_fields(
            meeting.meeting_id, 1, [EvaluationField(question="Years of experience?")], db_path=db_path
        )
        field = meetings_db.list_evaluation_fields(meeting.meeting_id, db_path=db_path)[0]
        meetings_db.save_evaluation_answers(
            invitee, [EvaluationAnswer(field_id=field.field_id, raw_answer="8 years")], db_path=db_path
        )

        meetings_db.replace_evaluation_fields(meeting.meeting_id, 1, [], db_path=db_path)

        assert meetings_db.list_evaluation_fields(meeting.meeting_id, db_path=db_path) == []
        assert meetings_db.list_evaluation_answers(meeting.meeting_id, db_path=db_path) == []

    def test_another_account_cannot_rewrite_the_questions(self, db_path, meeting):
        with pytest.raises(MeetingStorageError):
            meetings_db.replace_evaluation_fields(
                meeting.meeting_id, 2, [EvaluationField(question="Anything?")], db_path=db_path
            )


class TestEvaluationAnswers:
    @pytest.fixture
    def field_id(self, db_path, meeting):
        meetings_db.replace_evaluation_fields(
            meeting.meeting_id, 1, [EvaluationField(question="Years of experience?")], db_path=db_path
        )
        return meetings_db.list_evaluation_fields(meeting.meeting_id, db_path=db_path)[0].field_id

    def test_an_answer_round_trips_with_its_tag(self, db_path, meeting, invitee, field_id):
        meetings_db.save_evaluation_answers(
            invitee,
            [EvaluationAnswer(field_id=field_id, raw_answer="8 years", classified_tag="High")],
            db_path=db_path,
        )

        stored = meetings_db.list_evaluation_answers(meeting.meeting_id, db_path=db_path)[0]
        assert stored.raw_answer == "8 years"
        assert stored.classified_tag == "High"
        assert stored.invitee_id == invitee

    def test_re_extracting_overwrites_rather_than_stacking(self, db_path, meeting, invitee, field_id):
        meetings_db.save_evaluation_answers(
            invitee, [EvaluationAnswer(field_id=field_id, raw_answer="8 years")], db_path=db_path
        )
        meetings_db.save_evaluation_answers(
            invitee, [EvaluationAnswer(field_id=field_id, raw_answer="9 years")], db_path=db_path
        )

        answers = meetings_db.list_evaluation_answers(meeting.meeting_id, db_path=db_path)
        assert [answer.raw_answer for answer in answers] == ["9 years"]

    def test_an_answer_with_no_field_is_not_stored(self, db_path, meeting, invitee, field_id):
        meetings_db.save_evaluation_answers(
            invitee, [EvaluationAnswer(field_id=None, raw_answer="orphan")], db_path=db_path
        )

        assert meetings_db.list_evaluation_answers(meeting.meeting_id, db_path=db_path) == []

    def test_another_meetings_answers_are_not_listed(self, db_path, meeting, invitee, field_id):
        other = meetings_db.create_meeting(1, Meeting(subject="Something else"), db_path=db_path)
        meetings_db.save_evaluation_answers(
            invitee, [EvaluationAnswer(field_id=field_id, raw_answer="8 years")], db_path=db_path
        )

        assert meetings_db.list_evaluation_answers(other.meeting_id, db_path=db_path) == []
