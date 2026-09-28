"""Branching between question steps (phase 51): reading Go to, and following it.

What matters is that the path is always forward, always ends, and never changes after the
fact — the organiser's table has to say the same thing tomorrow that it says today.
"""

import pytest

from meetings import steps
from meetings.model import (
    ANSWER_CHOICE,
    ANSWER_DATE,
    ANSWER_NUMBER,
    ANSWER_TEXT,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    AgendaItem,
    Meeting,
    StepAnswer,
    agenda_from_json,
    agenda_to_json,
)

SALARY = "Expected salary"
RELOCATE = "Willing to relocate?"
CITY = "Preferred city"
NOTICE = "Notice period"


def _question(title, answer_type, rule="", branch=""):
    return AgendaItem(item=title, item_type=QUESTION_ITEM, answer_type=answer_type, rule=rule, branch=branch)


def _meeting(salary_branch="if > 60000 go to End", relocate_branch=f"if No go to {NOTICE}"):
    return Meeting(
        agenda=[
            _question(SALARY, ANSWER_NUMBER, "> 0", salary_branch),
            _question(RELOCATE, ANSWER_YES_NO, branch=relocate_branch),
            AgendaItem(item="Chat about the team"),
            _question(CITY, ANSWER_CHOICE, "Pune, Delhi"),
            _question(NOTICE, ANSWER_NUMBER, "<= 90"),
        ]
    )


def _answered(title, value, day="2026-09-28 10:00:00"):
    return StepAnswer(item_ref=title, value=value, status=STEP_ANSWERED, tries=1, updated_at=day)


class TestReadingGoTo:
    def test_several_lines_are_read_with_titles_spelt_as_in_the_agenda(self):
        meeting = _meeting(salary_branch=f"if > 60000 go to end; if < 1000 go to notice PERIOD")
        branches = steps.parse_branches(meeting.agenda[0], meeting.agenda)
        assert branches == [
            steps.Branch("> 60000", steps.END_TARGET),
            steps.Branch("< 1000", NOTICE),
        ]

    @pytest.mark.parametrize(
        ("item", "expected"),
        [
            (_question(SALARY, ANSWER_NUMBER, branch="when big go to End"), "isn't a Go to I understand"),
            (_question(SALARY, ANSWER_NUMBER, branch="if lots go to End"), "isn't a rule I understand"),
            (_question(SALARY, ANSWER_NUMBER, branch="if > 5 go to Nowhere"), "isn't a later question"),
            (_question(SALARY, ANSWER_YES_NO, branch="if Maybe go to End"), "should be Yes or No"),
            (_question(SALARY, ANSWER_CHOICE, "A, B", "if C go to End"), "isn't one of this question's options"),
            (_question(SALARY, ANSWER_TEXT, branch="if hi go to End"), "can't branch"),
        ],
    )
    def test_mistakes_are_named(self, item, expected):
        problem = steps.branch_problem(item, [item])
        assert problem.startswith(f"{SALARY}: ")
        assert expected in problem

    def test_jumping_backwards_is_refused(self):
        meeting = _meeting(relocate_branch=f"if No go to {SALARY}")
        assert "isn't a later question" in " ".join(steps.agenda_problems(meeting.agenda))

    def test_a_good_agenda_has_no_problems(self):
        assert steps.agenda_problems(_meeting().agenda) == []

    def test_go_to_round_trips_through_json_and_older_agendas_are_unchanged(self):
        meeting = _meeting()
        assert agenda_from_json(agenda_to_json(meeting.agenda)) == meeting.agenda
        plain = [_question(CITY, ANSWER_CHOICE, "Pune, Delhi")]
        assert '"branch"' not in agenda_to_json(plain)


class TestFollowingIt:
    def test_no_jump_goes_on_in_order(self):
        meeting = _meeting()
        answers = {SALARY: _answered(SALARY, "45000")}
        assert steps.next_question(meeting, answers).item == RELOCATE
        assert steps.skipped_questions(meeting, answers) == []

    def test_a_jump_skips_the_questions_in_between(self):
        meeting = _meeting()
        answers = {SALARY: _answered(SALARY, "45000"), RELOCATE: _answered(RELOCATE, "No")}
        current = steps.next_question(meeting, answers)
        assert current.item == NOTICE
        assert steps.skipped_questions(meeting, answers) == [CITY]
        assert steps.progress_text(meeting, current, answers) == "Question 3 of 3"

    def test_end_skips_every_remaining_question(self):
        meeting = _meeting()
        answers = {SALARY: _answered(SALARY, "90000")}
        assert steps.next_question(meeting, answers) is None
        assert steps.skipped_questions(meeting, answers) == [RELOCATE, CITY, NOTICE]

    def test_the_first_matching_line_wins(self):
        meeting = _meeting(salary_branch=f"if > 10 go to {NOTICE}; if > 60000 go to End")
        answers = {SALARY: _answered(SALARY, "90000")}
        assert steps.next_question(meeting, answers).item == NOTICE

    def test_a_not_answered_question_does_not_branch(self):
        meeting = _meeting()
        answers = {SALARY: StepAnswer(item_ref=SALARY, status=STEP_NOT_ANSWERED, tries=3)}
        assert steps.next_question(meeting, answers).item == RELOCATE

    def test_a_choice_branches_on_any_listed_option(self):
        item = _question(CITY, ANSWER_CHOICE, "Pune, Delhi, Goa", f"if Pune, Goa go to {NOTICE}")
        agenda = [item, _question("Other", ANSWER_TEXT), _question(NOTICE, ANSWER_NUMBER)]
        meeting = Meeting(agenda=agenda)
        assert steps.next_question(meeting, {CITY: _answered(CITY, "Goa")}).item == NOTICE
        assert steps.next_question(meeting, {CITY: _answered(CITY, "Delhi")}).item == "Other"

    def test_a_date_condition_is_judged_on_the_day_it_was_answered(self):
        item = _question("Joining", ANSWER_DATE, "future", f"if within 30 days go to {NOTICE}")
        meeting = Meeting(agenda=[item, _question("Why so late?", ANSWER_TEXT), _question(NOTICE, ANSWER_NUMBER)])
        # Answered on 28-09-2026: 15-10-2026 was within 30 days then, whatever today is.
        answers = {"Joining": _answered("Joining", "15-10-2026", day="2026-09-28 09:00:00")}
        assert steps.next_question(meeting, answers).item == NOTICE

    def test_a_broken_go_to_in_a_stored_agenda_goes_on_in_order(self):
        meeting = _meeting(salary_branch="if lots go to End")
        assert steps.next_question(meeting, {SALARY: _answered(SALARY, "90000")}).item == RELOCATE
