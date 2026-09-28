"""For each lists (phase 52): questions repeated per row, and the answers written back.

What matters is that every row gets every question exactly once, that an answer lands on the
row it was about, and that the organiser's filled-in list says the same thing the chat did.
"""

from io import BytesIO

import pandas as pd
import pytest

from auth.db import init_db, seed_default_admin
from meetings import access, loops, steps
from meetings import db as meetings_db
from meetings.model import (
    ANSWER_DATE,
    ANSWER_TEXT,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    AgendaItem,
    AgendaTable,
    Meeting,
    StepAnswer,
    agenda_from_json,
    agenda_to_json,
)

INVOICES = "Outstanding invoices"
CONFIRM = "Customer confirmed?"
PAID = "Is it paid already?"
DATE = "Expected payment date"
REASON = "Reason for delay"
CLOSING = "Anything else?"


def _question(title, answer_type=ANSWER_TEXT, rule="", branch="", loop=""):
    return AgendaItem(item=title, item_type=QUESTION_ITEM, answer_type=answer_type, rule=rule, branch=branch, loop=loop)


def _meeting(paid_branch=f"if Yes go to Next row", confirm_branch=""):
    return Meeting(
        agenda=[
            _question(CONFIRM, ANSWER_YES_NO, branch=confirm_branch),
            _question(PAID, ANSWER_YES_NO, branch=paid_branch, loop=INVOICES),
            _question(DATE, ANSWER_DATE, "future", loop=INVOICES),
            _question(REASON, loop=INVOICES),
            _question(CLOSING),
        ]
    )


def _answered(key, value="x"):
    return StepAnswer(item_ref=key, value=value, status=STEP_ANSWERED, tries=1, updated_at="2026-09-28 10:00:00")


def _key(title, row):
    return steps.answer_key(title, row)


ROWS = {steps.loop_key(INVOICES): [0, 1]}


def _table(match_column=""):
    return AgendaTable(
        item_ref=INVOICES,
        locked_columns=["Bill No", "Customer", "Amount"],
        base_data=[
            {"Bill No": "1001", "Customer": "ABC Traders", "Amount": "5000"},
            {"Bill No": "1002", "Customer": "XYZ Ltd", "Amount": "700"},
            {"Bill No": "1003", "Customer": "abc  traders", "Amount": "90"},
        ],
        match_column=match_column,
    )


class TestSetupChecks:
    def test_for_each_round_trips_through_json_and_older_agendas_are_unchanged(self):
        meeting = _meeting()
        assert agenda_from_json(agenda_to_json(meeting.agenda)) == meeting.agenda
        assert '"loop"' not in agenda_to_json([_question(CLOSING)])

    def test_a_good_agenda_has_no_problems(self):
        assert steps.agenda_problems(_meeting().agenda) == []

    def test_list_names_are_tidied_to_the_first_spelling(self):
        agenda = [_question(PAID, loop=INVOICES), _question(DATE, loop="  outstanding   INVOICES ")]
        assert [item.loop for item in steps.tidy_loop_names(agenda)] == [INVOICES, INVOICES]
        assert steps.loop_names(Meeting(agenda=agenda)) == [INVOICES]

    def test_the_questions_of_one_list_must_sit_together(self):
        agenda = [_question(PAID, loop=INVOICES), _question(CLOSING), _question(REASON, loop=INVOICES)]
        assert "must sit together" in " ".join(steps.agenda_problems(agenda))

    def test_a_list_cannot_share_a_name_with_an_agenda_item(self):
        agenda = [_question(PAID, loop=INVOICES), AgendaItem(item=INVOICES)]
        assert "both a For each list and an agenda item title" in " ".join(steps.agenda_problems(agenda))

    @pytest.mark.parametrize(
        ("paid_branch", "confirm_branch", "expected"),
        [
            (f"if Yes go to {CLOSING}", "", "isn't a later question of the list"),
            ("", f"if No go to {DATE}", "in the middle of the For each list"),
            ("", "if No go to Next row", "Next row only works on a question with a For each"),
        ],
    )
    def test_go_to_mistakes_around_lists_are_named(self, paid_branch, confirm_branch, expected):
        meeting = _meeting(paid_branch=paid_branch, confirm_branch=confirm_branch)
        assert expected in " ".join(steps.agenda_problems(meeting.agenda))

    def test_go_to_can_jump_to_the_first_question_of_a_list_or_within_it(self):
        meeting = _meeting(paid_branch=f"if No go to {REASON}", confirm_branch=f"if Yes go to {PAID}")
        assert steps.agenda_problems(meeting.agenda) == []


class TestWalking:
    def test_every_row_gets_every_question_in_order(self):
        meeting = _meeting(paid_branch="")
        answers = {CONFIRM: _answered(CONFIRM, "Yes")}
        order = []
        for _ in range(7):
            step = steps.next_question(meeting, answers, ROWS)
            order.append((step.item, step.row))
            answers[step.key] = _answered(step.key, "No" if step.item == PAID else "01-12-2026")
        assert order == [
            (PAID, 0), (DATE, 0), (REASON, 0),
            (PAID, 1), (DATE, 1), (REASON, 1),
            (CLOSING, None),
        ]

    def test_next_row_skips_the_rest_of_that_row_only(self):
        meeting = _meeting()
        answers = {CONFIRM: _answered(CONFIRM, "Yes"), _key(PAID, 0): _answered(_key(PAID, 0), "Yes")}
        walk = steps.walk(meeting, answers, ROWS)
        assert (walk.current.item, walk.current.row) == (PAID, 1)
        assert [step.key for step in walk.skipped] == [_key(DATE, 0), _key(REASON, 0)]

    def test_end_inside_a_list_skips_every_remaining_row_and_question(self):
        meeting = _meeting(paid_branch="if Yes go to End")
        answers = {CONFIRM: _answered(CONFIRM, "Yes"), _key(PAID, 0): _answered(_key(PAID, 0), "Yes")}
        walk = steps.walk(meeting, answers, ROWS)
        assert walk.finished
        assert steps.skipped_questions(meeting, answers, ROWS) == [CLOSING]
        assert _key(REASON, 1) in {step.key for step in walk.skipped}

    def test_a_list_with_no_rows_is_skipped(self):
        meeting = _meeting()
        answers = {CONFIRM: _answered(CONFIRM, "Yes")}
        assert steps.next_question(meeting, answers, {}).item == CLOSING

    def test_jumping_to_a_list_starts_at_its_first_row(self):
        meeting = _meeting(confirm_branch=f"if Yes go to {PAID}")
        step = steps.next_question(meeting, {CONFIRM: _answered(CONFIRM, "Yes")}, ROWS)
        assert (step.item, step.row) == (PAID, 0)

    def test_answers_are_kept_per_row(self):
        meeting = _meeting(paid_branch="")
        answers = {CONFIRM: _answered(CONFIRM, "Yes"), _key(PAID, 0): _answered(_key(PAID, 0), "No")}
        step = steps.next_question(meeting, answers, ROWS)
        assert (step.item, step.row) == (DATE, 0)
        assert steps.record_attempt(step.question, None, steps.AnswerCheck(ok=True, value="v"), "v", key=step.key).item_ref == step.key

    def test_progress_says_which_row_and_which_question(self):
        meeting = _meeting(paid_branch="")
        answers = {CONFIRM: _answered(CONFIRM, "Yes"), _key(PAID, 0): _answered(_key(PAID, 0), "No")}
        step = steps.next_question(meeting, answers, ROWS)
        assert steps.progress_text(meeting, step, answers, ROWS) == f"{INVOICES}: row 1 of 2, question 2 of 3"

    def test_ordinary_questions_count_without_the_list(self):
        meeting = _meeting()
        answers = {CONFIRM: _answered(CONFIRM, "Yes")}
        for row in (0, 1):
            answers[_key(PAID, row)] = _answered(_key(PAID, row), "Yes")
        step = steps.next_question(meeting, answers, ROWS)
        assert steps.progress_text(meeting, step, answers, ROWS) == "Question 2 of 2"


class TestLists:
    def test_without_a_match_column_everyone_gets_every_row(self):
        assert loops.rows_for_invitee(_table(), {"name": "Raj", "email": "raj@x.com"}) == [0, 1, 2]

    def test_a_match_column_gives_each_invitee_their_rows_by_name_or_email(self):
        table = _table("Customer")
        assert loops.rows_for_invitee(table, {"name": "ABC Traders", "email": "a@x.com"}) == [0, 2]
        assert loops.rows_for_invitee(table, {"name": "Someone", "email": "xyz ltd"}) == [1]
        assert loops.rows_for_invitee(table, {"name": "Nobody", "email": "n@x.com"}) == []

    def test_the_row_is_described_for_the_bot(self):
        lists = loops.invitee_lists({steps.loop_key(INVOICES): _table()}, {"name": "Raj", "email": ""})
        step = steps.Step(_question(DATE, loop=INVOICES), row=1)
        assert lists.row_context(step) == "Bill No: 1002; Customer: XYZ Ltd; Amount: 700"
        assert lists.row_context(steps.Step(_question(CLOSING))) == ""

    def test_only_sheets_of_current_lists_are_used(self):
        stale = AgendaTable(item_ref="Old list")
        tables = loops.list_tables(_meeting(), [_table(), stale])
        assert list(tables) == [steps.loop_key(INVOICES)]

    def test_progress_and_results_show_answers_skips_and_blanks(self):
        meeting = _meeting()
        invitee = {"invitee_id": 7, "name": "ABC Traders", "email": "a@x.com"}
        tables = {steps.loop_key(INVOICES): _table("Customer")}
        answers = {
            CONFIRM: _answered(CONFIRM, "Yes"),
            _key(PAID, 0): _answered(_key(PAID, 0), "Yes"),
            _key(PAID, 2): _answered(_key(PAID, 2), "No"),
            _key(DATE, 2): StepAnswer(item_ref=_key(DATE, 2), status=STEP_NOT_ANSWERED, tries=3),
        }
        lists = loops.invitee_lists(tables, invitee)
        walk = steps.walk(meeting, answers, lists.rows)
        assert loops.progress_label(meeting, INVOICES, walk, answers, lists.rows[steps.loop_key(INVOICES)]) == (
            f"{INVOICES}: 1 of 2 row(s) done"
        )

        frame = loops.results_frame(meeting, INVOICES, tables, [invitee], {7: answers})
        assert list(frame.columns) == ["Invitee", "Bill No", "Customer", "Amount", PAID, DATE, REASON]
        assert frame.iloc[0][[PAID, DATE, REASON]].tolist() == ["Yes", steps.SKIPPED_CELL, steps.SKIPPED_CELL]
        assert frame.iloc[1][[PAID, DATE, REASON]].tolist() == ["No", "⚠ Not answered", ""]

        workbook = pd.read_excel(BytesIO(loops.to_excel_bytes(frame, INVOICES)), dtype=str, keep_default_na=False)
        assert workbook["Bill No"].tolist() == ["1001", "1003"]

    def test_a_list_jumped_over_says_skipped(self):
        meeting = _meeting(confirm_branch=f"if No go to {CLOSING}")
        answers = {CONFIRM: _answered(CONFIRM, "No")}
        walk = steps.walk(meeting, answers, ROWS)
        assert loops.progress_label(meeting, INVOICES, walk, answers, [0, 1]) == f"{INVOICES}: skipped"


class TestStorage:
    def _meeting(self, tmp_path):
        path = tmp_path / "m.db"
        init_db(path)
        seed_default_admin(path)
        meetings_db.init_meetings_tables(path)
        setup = _meeting()
        setup.subject = "AR review"
        meeting = meetings_db.create_meeting(1, setup, db_path=path)
        invitee_id = meetings_db.add_invitee(
            meeting.meeting_id, 1, "Raj", "raj@x.com", "tok", access.encrypt_code("1"), db_path=path
        )
        return path, meeting, invitee_id

    def test_the_match_column_is_stored(self, tmp_path):
        path, meeting, _ = self._meeting(tmp_path)
        meetings_db.save_agenda_table(meeting.meeting_id, 1, _table("Customer"), db_path=path)
        assert meetings_db.find_agenda_table(meeting.meeting_id, INVOICES, db_path=path).match_column == "Customer"

    def test_replacing_a_list_can_clear_only_its_row_answers(self, tmp_path):
        path, meeting, invitee_id = self._meeting(tmp_path)
        for key in (CONFIRM, _key(PAID, 0), _key(DATE, 1)):
            meetings_db.save_step_answer(meeting.meeting_id, invitee_id, _answered(key), db_path=path)

        meetings_db.delete_row_answers(
            meeting.meeting_id, 1, [steps.row_key_prefix(PAID), steps.row_key_prefix(DATE)], db_path=path
        )
        assert list(meetings_db.load_step_answers(meeting.meeting_id, invitee_id, db_path=path)) == [CONFIRM]

    def test_an_older_database_gains_the_match_column(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE meeting_agenda_tables (table_id INTEGER PRIMARY KEY, meeting_id INTEGER, "
                "item_ref TEXT, source_file TEXT, locked_columns TEXT, editable_columns TEXT, base_data TEXT, "
                "created_at TEXT)"
            )
        meetings_db.init_meetings_tables(path)
        with sqlite3.connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(meeting_agenda_tables)")}
        assert "match_column" in columns
