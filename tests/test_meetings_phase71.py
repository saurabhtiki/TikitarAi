"""Phase 71: Accept only if — a step doesn't end until the reply meets the organiser's words.

The organiser's example: Company Demo (Yes/No, Accept only if "Yes"), then Introduction (Text,
Accept only if "describes their company"). Saying "No" to either asks again, with no limit.
"""

import pandas as pd

from app_pages import meetings as organiser_page
from meetings import chat_agent, steps
from meetings import db as meetings_db
from meetings.chat_agent import AcceptReading, AnswerReading, ChatTurnOutput, DiscussionReading
from meetings.model import (
    ANSWER_TEXT,
    ANSWER_YES_NO,
    QUESTION_ITEM,
    SENDER_AI,
    STEP_ANSWERED,
    STEP_PENDING,
    AgendaItem,
    Meeting,
    StepAnswer,
    agenda_from_json,
    agenda_to_json,
)
from tests.test_meetings_steps_pages import _invitee_app, _setup

DEMO = "Company Demo"
INTRO = "Introduction"
PRICING = "Pricing"
COMPANY = "describes their company: what it does and where"
PROFILE = {"provider": "local"}


def _agenda() -> list[AgendaItem]:
    return [
        AgendaItem(item=DEMO, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO, accept_if="Yes", max_tries=2),
        AgendaItem(item=INTRO, item_type=QUESTION_ITEM, answer_type=ANSWER_TEXT, accept_if=COMPANY),
    ]


def _answered(value: str = "") -> StepAnswer:
    return StepAnswer(value=value, status=STEP_ANSWERED, tries=1)


class TestAgenda:
    def test_the_cell_saves_and_loads(self):
        loaded = agenda_from_json(agenda_to_json(_agenda()))
        assert loaded[0].accept_if == "Yes" and loaded[1].accept_if == COMPANY

    def test_an_agenda_without_it_reads_back_the_same(self):
        assert "accept_if" not in agenda_to_json([AgendaItem(item="Chat")])

    def test_the_grid_reads_it_for_steps_only(self):
        frame = pd.DataFrame(
            [
                {**organiser_page._blank_agenda_row(), "Agenda item": PRICING, "Accept only if": " a date "},
                {**organiser_page._blank_agenda_row(), "Agenda item": "Prices", "Type": "Table", "Accept only if": "x"},
            ]
        )
        items = organiser_page._agenda_from_frame(frame)
        assert items[0].accept_if == "a date" and items[1].accept_if == ""
        assert organiser_page._agenda_to_frame(items).loc[0, "Accept only if"] == "a date"


class TestSteps:
    def test_a_question_never_runs_out_of_tries(self):
        item = _agenda()[0]
        state = None
        for _ in range(item.max_tries + 3):
            state = steps.record_attempt(item, state, steps.AnswerCheck(ok=False, reason="Need Yes."), "No")
            assert state.status == STEP_PENDING

    def test_without_it_tries_still_run_out(self):
        item = AgendaItem(item=DEMO, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO, max_tries=1)
        state = steps.record_attempt(item, None, steps.AnswerCheck(ok=False), "No")
        assert state.is_finished() and state.status != STEP_ANSWERED

    def test_a_discussion_has_no_reply_limit(self):
        item = AgendaItem(item=PRICING, accept_if="agreed a demo date")
        state = None
        for _ in range(steps.DISCUSSION_REPLY_LIMIT + 2):
            state = steps.record_discussion_reply(item, state, covered=False, reply="not now")
            assert state.status == STEP_PENDING
        assert steps.record_discussion_reply(item, state, covered=True, reply="Tuesday").status == STEP_ANSWERED

    def test_close_chat_waits_for_such_a_discussion(self):
        meeting = Meeting(agenda=[AgendaItem(item=PRICING, accept_if="agreed a demo date")])
        assert steps.questions_left(meeting, {}) == 1
        assert steps.questions_left(Meeting(agenda=[AgendaItem(item=PRICING)]), {}) == 0

    def test_stuck_at_names_the_step_and_last_reply(self):
        meeting = Meeting(agenda=_agenda())
        assert steps.stuck_at(meeting, {}) == ""  # not asked yet
        tried = {DEMO: StepAnswer(item_ref=DEMO, status=STEP_PENDING, tries=4, last_reply="No")}
        assert steps.stuck_at(meeting, tried) == f'{DEMO} (said: "No")'
        assert steps.stuck_at(meeting, {DEMO: _answered("Yes")}) == ""

    def test_a_discussion_is_stuck_only_past_the_usual_limit(self):
        meeting = Meeting(agenda=[AgendaItem(item=PRICING, accept_if="agreed a demo date")])
        talking = {PRICING: StepAnswer(item_ref=PRICING, status=STEP_PENDING, tries=1, last_reply="ok")}
        assert steps.stuck_at(meeting, talking) == ""
        talking[PRICING].tries = steps.DISCUSSION_REPLY_LIMIT
        assert steps.stuck_at(meeting, talking) == f'{PRICING} (said: "ok")'


class TestChatAgent:
    def test_guidance_tells_the_bot_the_condition(self):
        assert COMPANY in chat_agent.question_guidance(_agenda()[1], "Question 2 of 2")
        assert "agreed a demo date" in chat_agent.discussion_guidance(AgendaItem(item=PRICING, accept_if="agreed a demo date"))

    def test_the_discussion_check_ignores_move_on(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            chat_agent, "run_structured", lambda profile, prompt, *a, **k: seen.update(prompt=prompt) or DiscussionReading()
        )
        chat_agent.read_discussion(PROFILE, AgendaItem(item=PRICING, accept_if="agreed a demo date"), [], "let's move on")
        assert "agreed a demo date" in seen["prompt"] and "does not count" in seen["prompt"]


class TestInviteePage:
    def _started(self, tmp_path, monkeypatch, answers, agenda=None):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, agenda=agenda or _agenda())
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        for title, value in answers.items():
            meetings_db.save_step_answer(
                meeting.meeting_id, invitee_id, StepAnswer(item_ref=title, value=value, status=STEP_ANSWERED, tries=1)
            )
        return meeting, invitee_id, probe

    def _stub(self, monkeypatch, value="", accepted=True, covered=False, missing="") -> dict:
        import app_pages.meeting_invitee as page

        seen = {"judged": 0}
        monkeypatch.setattr(page.chat_agent, "read_answer", lambda *a, **k: AnswerReading(value=value))

        def _judge(*args, **kwargs):
            seen["judged"] += 1
            return AcceptReading(accepted=accepted, missing="Please say what your company does.")

        monkeypatch.setattr(page.chat_agent, "read_accept", _judge)
        monkeypatch.setattr(page.chat_agent, "read_discussion", lambda *a, **k: DiscussionReading(covered=covered, missing=missing))

        def _reply(*args, **kwargs):
            seen.update(kwargs)
            return ChatTurnOutput(reply="Ok.", agenda_tag="")

        monkeypatch.setattr(page.chat_agent, "send_turn", _reply)
        return seen

    def test_a_refused_reply_asks_again_and_holds_close_chat(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = self._started(tmp_path, monkeypatch, {DEMO: "Yes"})
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Please introduce your company.", INTRO)
        # Three tries already used: without Accept only if, this reply would end it as Not answered.
        meetings_db.save_step_answer(
            meeting.meeting_id, invitee_id, StepAnswer(item_ref=INTRO, status=STEP_PENDING, tries=3, last_reply="no")
        )
        seen = self._stub(monkeypatch, value="No", accepted=False)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("No").run()
        assert not app.exception
        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id)[INTRO]
        assert saved.status == STEP_PENDING and saved.tries == 4
        assert seen["step_tag"] == INTRO and "Please say what your company does." in seen["step_guidance"]
        assert app.button(key="invitee_close_chat").disabled

    def test_an_accepted_reply_is_saved_and_moves_on(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = self._started(tmp_path, monkeypatch, {DEMO: "Yes"})
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Please introduce your company.", INTRO)
        seen = self._stub(monkeypatch, value="We make solar panels in Pune", accepted=True)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("We make solar panels in Pune").run()
        assert not app.exception
        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id)[INTRO]
        assert saved.status == STEP_ANSWERED and saved.value == "We make solar panels in Pune"
        assert seen["judged"] == 1
        assert not app.button(key="invitee_close_chat").disabled

    def test_a_reply_failing_the_rule_is_not_judged(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe = self._started(tmp_path, monkeypatch, {})
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Shall we go ahead?", DEMO)
        seen = self._stub(monkeypatch, value="")  # not a yes or no
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("hmm").run()
        assert not app.exception and seen["judged"] == 0

    def test_a_discussion_stays_past_the_reply_limit(self, tmp_path, monkeypatch):
        agenda = [AgendaItem(item=PRICING, accept_if="agreed a demo date")]
        meeting, invitee_id, probe = self._started(tmp_path, monkeypatch, {}, agenda=agenda)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "When can we demo?", PRICING)
        # One short of the limit: without Accept only if, this reply would end the discussion.
        meetings_db.save_step_answer(
            meeting.meeting_id,
            invitee_id,
            StepAnswer(item_ref=PRICING, status=STEP_PENDING, tries=steps.DISCUSSION_REPLY_LIMIT, last_reply="hmm"),
        )
        seen = self._stub(monkeypatch, covered=False, missing="A demo date.")
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("not sure").run()
        assert not app.exception
        assert "Still needed before moving on: A demo date." in seen["step_guidance"]
        assert any("1 step(s) left" in element.value for element in app.caption)
        saved = meetings_db.load_step_answers(meeting.meeting_id, invitee_id)[PRICING]
        assert saved.status == STEP_PENDING and saved.tries == steps.DISCUSSION_REPLY_LIMIT + 1
        assert seen["step_tag"] == PRICING
        assert app.button(key="invitee_close_chat").disabled
