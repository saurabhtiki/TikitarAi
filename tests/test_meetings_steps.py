"""Question steps (phase 50): rules, answer checks, storage and the prompts around them.

The rule checks are the ones with teeth: the whole point of a question step is that the
meeting cannot move on with a wrong answer, and that decision is made here, not by a model.
"""

import datetime

import pytest

from meetings import chat_agent, steps
from meetings import db as meetings_db
from meetings.model import (
    ANSWER_CHOICE,
    ANSWER_DATE,
    ANSWER_NUMBER,
    ANSWER_TEXT,
    ANSWER_YES_NO,
    DISCUSSION_ITEM,
    QUESTION_ITEM,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    STEP_PENDING,
    AgendaItem,
    Meeting,
    StepAnswer,
    agenda_from_json,
    agenda_to_json,
)

TODAY = datetime.date(2026, 9, 28)
PROFILE = {"profile_id": 1, "default_model": "test-model"}


def _question(answer_type, rule="", title="Q", max_tries=3):
    return AgendaItem(item=title, item_type=QUESTION_ITEM, answer_type=answer_type, rule=rule, max_tries=max_tries)


class TestRules:
    @pytest.mark.parametrize(
        ("answer_type", "rule", "good", "bad"),
        [
            (ANSWER_NUMBER, "> 0", "5", "0"),
            (ANSWER_NUMBER, ">= 1000", "1000", "999"),
            (ANSWER_NUMBER, "< 90", "89", "90"),
            (ANSWER_NUMBER, "<= 90", "90", "91"),
            (ANSWER_NUMBER, "between 20000 and 60000", "45,000", "75000"),
            (ANSWER_NUMBER, "", "12.5", "about twelve"),
            (ANSWER_DATE, "future", "15-11-2026", "01-09-2026"),
            (ANSWER_DATE, "past", "01-09-2026", "15-11-2026"),
            (ANSWER_DATE, "after 01-10-2026", "02-10-2026", "01-10-2026"),
            (ANSWER_DATE, "before 31-12-2026", "30-12-2026", "31-12-2026"),
            (ANSWER_DATE, "within 30 days", "10-10-2026", "30-11-2026"),
            (ANSWER_DATE, "", "2026-11-15", "next month"),
            (ANSWER_TEXT, "at least 10 characters", "I lead a team of five", "Fine"),
            (ANSWER_CHOICE, "Morning, Evening, Night", "evening", "Afternoon"),
            (ANSWER_YES_NO, "", "yes", "maybe"),
        ],
    )
    def test_a_good_answer_passes_and_a_bad_one_does_not(self, answer_type, rule, good, bad):
        item = _question(answer_type, rule)
        assert steps.rule_problem(item) == ""
        assert steps.check_answer(item, good, TODAY).ok
        refused = steps.check_answer(item, bad, TODAY)
        assert not refused.ok
        assert refused.reason.startswith("The answer needs to be")

    def test_answers_are_stored_in_a_clean_form(self):
        assert steps.check_answer(_question(ANSWER_NUMBER), "45,000", TODAY).value == "45000"
        assert steps.check_answer(_question(ANSWER_DATE), "2026-11-15", TODAY).value == "15-11-2026"
        assert steps.check_answer(_question(ANSWER_YES_NO), "Y", TODAY).value == "Yes"
        assert steps.check_answer(_question(ANSWER_CHOICE, "Morning, Night"), "night", TODAY).value == "Night"

    def test_an_empty_answer_never_passes(self):
        assert not steps.check_answer(_question(ANSWER_TEXT), "  ", TODAY).ok

    @pytest.mark.parametrize(
        ("answer_type", "rule", "hint"),
        [
            (ANSWER_NUMBER, "more than 5k", "between 20000 and 60000"),
            (ANSWER_DATE, "soon", "within 30 days"),
            (ANSWER_DATE, "after tomorrow", "after 01-10-2026"),
            (ANSWER_TEXT, "long", "at least 20 characters"),
            (ANSWER_YES_NO, "yes only", "no rule"),
        ],
    )
    def test_an_unreadable_rule_is_refused_with_an_example(self, answer_type, rule, hint):
        problem = steps.rule_problem(_question(answer_type, rule, title="Expected salary"))
        assert problem.startswith("Expected salary:")
        assert hint in problem

    def test_a_choice_needs_its_options(self):
        assert "at least two options" in steps.rule_problem(_question(ANSWER_CHOICE, ""))

    def test_the_rule_reads_in_plain_words(self):
        assert steps.describe_rule(_question(ANSWER_NUMBER, "between 20000 and 60000")) == (
            "a number between 20000 and 60000"
        )
        assert steps.describe_rule(_question(ANSWER_DATE, "future")) == "a date (dd-mm-yyyy) in the future"
        assert steps.describe_rule(_question(ANSWER_CHOICE, "A, B")) == "one of: A, B"

    def test_a_discussion_item_has_no_rule_to_check(self):
        assert steps.rule_problem(AgendaItem(item="Anything", rule="nonsense")) == ""

    def test_repeated_titles_are_refused(self):
        agenda = [_question(ANSWER_TEXT, title="Salary"), AgendaItem(item="salary ")]
        assert any("twice" in problem for problem in steps.agenda_problems(agenda))


class TestAttempts:
    def test_a_passing_answer_is_answered(self):
        item = _question(ANSWER_NUMBER)
        state = steps.record_attempt(item, None, steps.AnswerCheck(ok=True, value="5"), "five")
        assert (state.status, state.value, state.tries) == (STEP_ANSWERED, "5", 1)

    def test_running_out_of_tries_marks_it_not_answered(self):
        item = _question(ANSWER_NUMBER, max_tries=2)
        refused = steps.AnswerCheck(ok=False, reason="no")
        first = steps.record_attempt(item, None, refused, "lots")
        assert first.status == STEP_PENDING
        second = steps.record_attempt(item, first, refused, "good money")
        assert (second.status, second.last_reply) == (STEP_NOT_ANSWERED, "good money")
        assert steps.display_answer(second) == '⚠ Not answered (said: "good money")'

    def test_the_next_question_skips_finished_ones(self):
        meeting = Meeting(
            agenda=[_question(ANSWER_TEXT, title="A"), AgendaItem(item="Chat"), _question(ANSWER_TEXT, title="B")]
        )
        assert steps.next_question(meeting, {}).item == "A"
        done = {"A": StepAnswer(item_ref="A", status=STEP_NOT_ANSWERED)}
        current = steps.next_question(meeting, done)
        assert current.item == "B"
        assert steps.progress_text(meeting, current, done) == "Question 2 of 2"
        assert steps.next_question(meeting, {**done, "B": StepAnswer(item_ref="B", status=STEP_ANSWERED)}) is None


class TestModel:
    def test_a_question_round_trips_through_json(self):
        item = _question(ANSWER_NUMBER, "> 0", title="Salary", max_tries=4)
        [restored] = agenda_from_json(agenda_to_json([item]))
        assert restored == item

    def test_an_older_agenda_still_loads_and_writes_the_same(self):
        older = '{"version": 1, "items": [{"type": "discussion", "item": "Delivery", "ai_note": ""}]}'
        [restored] = agenda_from_json(older)
        assert restored.item_type == DISCUSSION_ITEM
        assert "answer_type" not in agenda_to_json([restored])

    def test_tries_are_kept_in_range(self):
        [restored] = agenda_from_json(
            '{"items": [{"type": "question", "item": "Q", "answer_type": "odd", "max_tries": 99}]}'
        )
        assert (restored.answer_type, restored.max_tries) == (ANSWER_TEXT, 5)


class TestStorage:
    def test_answers_are_saved_and_read_back(self, tmp_path):
        db_path = tmp_path / "meetings.db"
        from auth.db import init_db, seed_default_admin

        init_db(db_path)
        seed_default_admin(db_path)
        meetings_db.init_meetings_tables(db_path)
        meeting = meetings_db.create_meeting(1, Meeting(subject="Interview"), db_path=db_path)
        invitee_id = meetings_db.add_invitee(meeting.meeting_id, 1, "Raj", "raj@x.com", "tok", "enc", db_path=db_path)

        meetings_db.save_step_answer(
            meeting.meeting_id, invitee_id, StepAnswer(item_ref="Salary", status=STEP_PENDING, tries=1), db_path
        )
        meetings_db.save_step_answer(
            meeting.meeting_id,
            invitee_id,
            StepAnswer(item_ref="Salary", value="45000", status=STEP_ANSWERED, tries=2),
            db_path,
        )

        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id, db_path)["Salary"]
        assert (saved.value, saved.status, saved.tries) == ("45000", STEP_ANSWERED, 2)
        everyone = meetings_db.list_all_step_answers(meeting.meeting_id, 1, db_path)
        assert everyone[invitee_id]["Salary"].value == "45000"

        with pytest.raises(meetings_db.MeetingStorageError):
            meetings_db.list_all_step_answers(meeting.meeting_id, 2, db_path)


class TestPrompts:
    def test_read_answer_sends_today_and_the_rule(self, monkeypatch):
        seen = {}

        def _fake(profile, prompt, schema, **kwargs):
            seen["prompt"] = prompt
            return chat_agent.AnswerReading(kind="QUESTION", value="")

        monkeypatch.setattr(chat_agent, "run_structured", _fake)
        reading = chat_agent.read_answer(
            PROFILE, _question(ANSWER_DATE, "future", title="Joining date"), "Is it remote?", TODAY
        )

        assert reading.kind == chat_agent.QUESTION_KIND
        assert "28-09-2026" in seen["prompt"]
        assert "a date (dd-mm-yyyy) in the future" in seen["prompt"]

    def test_step_guidance_reaches_the_instructions_and_questions_are_not_free_agenda(self):
        meeting = Meeting(agenda=[_question(ANSWER_NUMBER, title="Expected salary"), AgendaItem(item="Culture")])
        guidance = chat_agent.question_guidance(meeting.agenda[0], "Question 1 of 1")
        text = chat_agent.system_instructions(meeting, step_guidance=guidance)

        assert 'CURRENT STEP: Now ask this question (Question 1 of 1): "Expected salary"' in text
        free_agenda = text.split("Work through these agenda items")[1].split("\n\n")[0]
        assert "Culture" in free_agenda
        assert "Expected salary" not in free_agenda

    def test_a_steered_reply_is_tagged_with_its_question(self, monkeypatch):
        meeting = Meeting(agenda=[_question(ANSWER_NUMBER, title="Expected salary")])
        monkeypatch.setattr(
            chat_agent,
            "run_structured",
            lambda *a, **k: chat_agent.ChatTurnOutput(reply="And your notice period?", agenda_tag="Other/Extra"),
        )
        turn = chat_agent.send_turn(meeting, PROFILE, "", [], "45000", step_guidance="x", step_tag="Expected salary")
        assert turn.agenda_tag == "Expected salary"
