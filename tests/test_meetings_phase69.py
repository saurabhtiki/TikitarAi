"""Phase 69: discussions are steps too — taken in grid order, and Go to can jump to one.

The organiser's example: About Us (Yes/No, `if No go to Introduction`), then the Company
Introduction discussion that shows a video, then the Introduction question. Yes plays the
video; No skips straight to Introduction.
"""

from meetings import chat_agent, flow, steps
from meetings import db as meetings_db
from meetings.chat_agent import AnswerReading, ChatTurnOutput, DiscussionReading
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
)
from tests.test_meetings_steps_pages import _invitee_app, _setup

ABOUT = "About Us"
VIDEO = "Company Introduction"
INTRO = "Introduction"
PROFILE = {"provider": "local"}


def _agenda(branch: str = "if No go to Introduction") -> list[AgendaItem]:
    return [
        AgendaItem(item=ABOUT, item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO, branch=branch,
                   ai_note="Ask if they would like to see a short video about us."),
        AgendaItem(item=VIDEO, ai_note="Show Company Info, then ask what they think."),
        AgendaItem(item=INTRO, item_type=QUESTION_ITEM, answer_type=ANSWER_TEXT),
    ]


def _answered(value: str = "") -> StepAnswer:
    return StepAnswer(value=value, status=STEP_ANSWERED, tries=1)


class TestGoTo:
    def test_a_go_to_can_jump_to_a_later_discussion(self):
        agenda = [
            AgendaItem(item="Demo", item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO, branch="if yes go to company introduction"),
            AgendaItem(item="Pricing"),
            AgendaItem(item=VIDEO),
        ]
        assert steps.agenda_problems(agenda) == []
        assert steps.parse_branches(agenda[0], agenda)[0].target == VIDEO

    def test_a_go_to_an_earlier_discussion_is_refused(self):
        agenda = [AgendaItem(item=VIDEO), *_agenda(branch=f"if No go to {VIDEO}")[:1]]
        assert "isn't a later question or discussion" in " ".join(steps.agenda_problems(agenda))

    def test_a_discussion_between_for_each_questions_is_refused(self):
        agenda = [
            AgendaItem(item="Paid", item_type=QUESTION_ITEM, answer_type=ANSWER_YES_NO, loop="Invoices"),
            AgendaItem(item="Chat"),
            AgendaItem(item="When", item_type=QUESTION_ITEM, answer_type=ANSWER_TEXT, loop="Invoices"),
        ]
        assert "must sit together" in " ".join(steps.agenda_problems(agenda))


class TestWalk:
    def test_steps_run_in_grid_order(self):
        meeting = Meeting(agenda=_agenda())
        assert steps.next_question(meeting, {}).item == ABOUT
        after_yes = {ABOUT: _answered("Yes")}
        assert steps.next_question(meeting, after_yes).item == VIDEO
        after_video = {**after_yes, VIDEO: _answered()}
        assert steps.next_question(meeting, after_video).item == INTRO

    def test_no_skips_the_video_discussion(self):
        meeting = Meeting(agenda=_agenda())
        answers = {ABOUT: _answered("No")}
        assert steps.next_question(meeting, answers).item == INTRO
        walk = steps.walk(meeting, answers)
        assert [step.item for step in walk.skipped] == [VIDEO]
        # A skipped discussion is not a skipped question.
        assert steps.skipped_questions(meeting, answers) == []

    def test_close_chat_waits_for_questions_only(self):
        meeting = Meeting(agenda=_agenda())
        answers = {ABOUT: _answered("Yes")}
        # Video discussion open, Introduction still ahead: one question left, not two.
        assert steps.questions_left(meeting, answers) == 1

    def test_progress_counts_questions_only(self):
        meeting = Meeting(agenda=_agenda())
        answers = {ABOUT: _answered("Yes"), VIDEO: _answered()}
        current = steps.next_question(meeting, answers)
        assert steps.progress_text(meeting, current, answers) == "Question 2 of 2"


class TestDiscussionReplies:
    def test_covered_finishes_the_discussion(self):
        item = AgendaItem(item=VIDEO)
        state = steps.record_discussion_reply(item, None, covered=True, reply="Nice video")
        assert state.status == STEP_ANSWERED and state.tries == 1 and state.item_ref == VIDEO

    def test_it_moves_on_after_the_reply_limit(self):
        item = AgendaItem(item=VIDEO)
        state = None
        for _ in range(steps.DISCUSSION_REPLY_LIMIT - 1):
            state = steps.record_discussion_reply(item, state, covered=False, reply="hmm")
            assert state.status == STEP_PENDING
        state = steps.record_discussion_reply(item, state, covered=False, reply="hmm")
        assert state.status == STEP_ANSWERED


class TestChatAgent:
    def test_discussion_guidance_names_only_this_item(self):
        text = chat_agent.discussion_guidance(AgendaItem(item=VIDEO, ai_note="Show Company Info."))
        assert f'Now discuss this agenda item: "{VIDEO}"' in text
        assert "Show Company Info." in text
        assert "Do not start any other agenda item" in text

    def test_the_opening_drops_the_bots_own_document_pick(self, monkeypatch):
        monkeypatch.setattr(
            chat_agent, "run_structured",
            lambda *a, **k: ChatTurnOutput(reply="Hi!", agenda_tag=ABOUT, show_file="Company Info"),
        )
        opening = chat_agent.opening_message(Meeting(agenda=_agenda()), PROFILE, step_tag=ABOUT)
        assert opening.show_file == ""

    def test_the_bot_is_told_not_to_show_related_documents(self):
        files = [{"file_id": 1, "filename": "Company Info", "url": "https://youtu.be/a", "description": "About us"}]
        text = chat_agent.system_instructions(Meeting(agenda=_agenda()), ref_files=files)
        assert "Never show a document just because it is related to the topic" in text


class TestFlowPicture:
    def test_a_jump_to_a_discussion_is_drawn(self):
        dot = flow.flow_dot(_agenda())
        assert '"2. Company Introduction\\nDiscussion", style="rounded,dashed"' in dot
        assert 'q0 -> q2 [label="if No"' in dot
        assert "q0 -> q1;" in dot and "q1 -> q2;" in dot


class TestInviteePage:
    def _started(self, tmp_path, monkeypatch, answers):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, agenda=_agenda())
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        for title, value in answers.items():
            meetings_db.save_step_answer(
                meeting.meeting_id, invitee_id, StepAnswer(item_ref=title, value=value, status=STEP_ANSWERED, tries=1)
            )
        return meeting, invitee_id, probe, meetings_db

    def _stub(self, monkeypatch, reading=None, covered=False) -> dict:
        import app_pages.meeting_invitee as page

        seen = {}
        monkeypatch.setattr(page.chat_agent, "read_answer", lambda *a, **k: reading or AnswerReading(value="Yes"))
        monkeypatch.setattr(page.chat_agent, "read_discussion", lambda *a, **k: DiscussionReading(covered=covered))

        def _reply(*args, **kwargs):
            seen.update(kwargs)
            return ChatTurnOutput(reply="Ok.", agenda_tag="")

        monkeypatch.setattr(page.chat_agent, "send_turn", _reply)
        return seen

    def test_yes_moves_on_to_the_video_discussion(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, db = self._started(tmp_path, monkeypatch, {})
        db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Like to see a video about us?", ABOUT)
        seen = self._stub(monkeypatch, AnswerReading(value="Yes"))
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("yes please").run()
        assert not app.exception
        assert seen["step_tag"] == VIDEO
        assert f'Now discuss this agenda item: "{VIDEO}"' in seen["step_guidance"]

    def test_no_jumps_to_introduction(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, db = self._started(tmp_path, monkeypatch, {})
        db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Like to see a video about us?", ABOUT)
        seen = self._stub(monkeypatch, AnswerReading(value="No"))
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("no thanks").run()
        assert not app.exception
        assert seen["step_tag"] == INTRO

    def test_a_covered_discussion_moves_on_to_the_next_question(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, db = self._started(tmp_path, monkeypatch, {ABOUT: "Yes"})
        db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Here is our video. Thoughts?", VIDEO)
        seen = self._stub(monkeypatch, covered=True)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("Looks great").run()
        assert not app.exception
        assert db.load_step_answers(meeting.meeting_id, invitee_id)[VIDEO].status == STEP_ANSWERED
        assert seen["step_tag"] == INTRO

    def test_a_discussion_still_going_stays_on_it(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, db = self._started(tmp_path, monkeypatch, {ABOUT: "Yes"})
        db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Here is our video. Thoughts?", VIDEO)
        seen = self._stub(monkeypatch, covered=False)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("Where is your office?").run()
        assert not app.exception
        assert db.load_step_answers(meeting.meeting_id, invitee_id)[VIDEO].status == STEP_PENDING
        assert seen["step_tag"] == VIDEO
