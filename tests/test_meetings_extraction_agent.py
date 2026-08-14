"""Reading evaluation answers out of a conversation (requirement 6.7, Phase 2, spec 3b).

`TestFullHistoryIsTheSource` mirrors the class of the same name in
`test_meetings_summary_agent.py`, and for the same reason: an evaluation cell is a permanent,
comparable claim about an invitee — "8 yrs (High)" — and building one on a lossy rolling fold
is exactly the bug that guard exists to prevent.

The bucket tests are the other half. The comparison matrix groups on the tag string, so a
model that answers "high" where the creator defined "High", or invents "Very high" outright,
would quietly produce extra columns of one.
"""

import pytest

from meetings import extraction_agent
from meetings.exceptions import MeetingAgentError
from meetings.extraction_agent import (
    ExtractedAnswers,
    FieldAnswer,
    build_prompt,
    extract_answers,
)
from meetings.model import OPENING_TAG, ChatMessage, EvaluationField, Meeting

PROFILE = {"profile_id": 1, "default_model": "test-model"}


@pytest.fixture
def meeting():
    return Meeting(subject="Vendor comparison", meeting_context="Comparing four suppliers.")


@pytest.fixture
def fields():
    return [
        EvaluationField(field_id=1, question="Years of experience?", buckets=["Low", "Medium", "High"]),
        EvaluationField(field_id=2, question="Employee strength?"),
    ]


@pytest.fixture
def transcript():
    return [
        ChatMessage(message_id=1, sender="ai", text="Welcome.", agenda_tag=OPENING_TAG),
        ChatMessage(message_id=2, sender="user", text="We've been going 8 years, about 150 staff."),
    ]


def _stub(monkeypatch, result=None, fails=False):
    calls: list[dict] = []

    def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
        calls.append({"prompt": prompt, "instructions": instructions})
        if fails:
            from llm.client import LLMConnectionError

            raise LLMConnectionError("provider is down")
        return result or ExtractedAnswers(
            answers=[
                FieldAnswer(question="Years of experience?", raw_answer="8 years", classified_tag="High"),
                FieldAnswer(question="Employee strength?", raw_answer="150"),
            ]
        )

    monkeypatch.setattr(extraction_agent, "run_structured", fake_run_structured)
    return calls


class TestFullHistoryIsTheSource:
    def test_the_signature_cannot_be_handed_a_running_summary(self):
        import inspect

        parameters = inspect.signature(extract_answers).parameters
        assert "messages" in parameters
        assert not any("summary" in name for name in parameters)

    def test_every_message_reaches_the_prompt(self, meeting, fields, transcript):
        prompt = build_prompt(meeting, fields, transcript)

        assert "8 years, about 150 staff" in prompt
        assert "Welcome." in prompt

    def test_an_empty_conversation_says_so_rather_than_inventing_one(self, meeting, fields):
        assert "sent no messages" in build_prompt(meeting, fields, [])


class TestPrompt:
    def test_every_question_is_listed(self, meeting, fields, transcript):
        prompt = build_prompt(meeting, fields, transcript)

        assert "Years of experience?" in prompt
        assert "Employee strength?" in prompt

    def test_buckets_are_offered_only_where_they_were_defined(self, meeting, fields, transcript):
        prompt = build_prompt(meeting, fields, transcript)
        lines = {line.split(" [")[0]: line for line in prompt.splitlines() if line.startswith("- ")}

        assert "Low, Medium, High" in lines["- Years of experience?"]
        assert "[classify" not in lines["- Employee strength?"]


class TestExtracting:
    def test_answers_come_back_paired_to_their_fields(self, monkeypatch, meeting, fields, transcript):
        _stub(monkeypatch)
        answers = extract_answers(meeting, PROFILE, fields, transcript)

        assert [(answer.field_id, answer.raw_answer) for answer in answers] == [
            (1, "8 years"),
            (2, "150"),
        ]

    def test_a_meeting_with_no_questions_makes_no_provider_call(self, monkeypatch, meeting, transcript):
        # The common case. The toggle being off must not cost a request per close.
        calls = _stub(monkeypatch)
        assert extract_answers(meeting, PROFILE, [], transcript) == []
        assert calls == []

    def test_a_question_the_conversation_never_answered_still_gets_a_row(
        self, monkeypatch, meeting, fields, transcript
    ):
        # An empty cell is a real finding in a comparison. A missing row reads instead as a
        # question nobody was asked.
        _stub(
            monkeypatch,
            result=ExtractedAnswers(answers=[FieldAnswer(question="Years of experience?", raw_answer="8 years")]),
        )
        answers = extract_answers(meeting, PROFILE, fields, transcript)

        assert len(answers) == 2
        assert answers[1].raw_answer == ""

    def test_an_answer_to_a_question_nobody_asked_is_dropped(self, monkeypatch, meeting, fields, transcript):
        _stub(
            monkeypatch,
            result=ExtractedAnswers(
                answers=[FieldAnswer(question="Favourite colour?", raw_answer="Blue")]
            ),
        )
        answers = extract_answers(meeting, PROFILE, fields, transcript)

        assert [answer.field_id for answer in answers] == [1, 2]
        assert all(answer.raw_answer == "" for answer in answers)

    def test_questions_match_regardless_of_case_and_spacing(self, monkeypatch, meeting, fields, transcript):
        _stub(
            monkeypatch,
            result=ExtractedAnswers(
                answers=[FieldAnswer(question="  years of EXPERIENCE?  ", raw_answer="8 years")]
            ),
        )
        assert extract_answers(meeting, PROFILE, fields, transcript)[0].raw_answer == "8 years"

    def test_a_provider_failure_is_reported_rather_than_silently_blank(
        self, monkeypatch, meeting, fields, transcript
    ):
        _stub(monkeypatch, fails=True)
        with pytest.raises(MeetingAgentError):
            extract_answers(meeting, PROFILE, fields, transcript)


class TestBuckets:
    def test_a_tag_is_normalised_to_the_creators_spelling(self, monkeypatch, meeting, fields, transcript):
        # Two invitees whose answers came back as "high" and "High" have to end up in one
        # bucket, not two.
        _stub(
            monkeypatch,
            result=ExtractedAnswers(
                answers=[FieldAnswer(question="Years of experience?", raw_answer="8 years", classified_tag="high")]
            ),
        )
        assert extract_answers(meeting, PROFILE, fields, transcript)[0].classified_tag == "High"

    def test_a_bucket_the_creator_never_defined_is_dropped(self, monkeypatch, meeting, fields, transcript):
        _stub(
            monkeypatch,
            result=ExtractedAnswers(
                answers=[
                    FieldAnswer(question="Years of experience?", raw_answer="8 years", classified_tag="Very high")
                ]
            ),
        )
        answer = extract_answers(meeting, PROFILE, fields, transcript)[0]

        assert answer.classified_tag == ""
        # The raw answer survives, so the detail is not lost with the classification.
        assert answer.raw_answer == "8 years"

    def test_a_field_with_no_buckets_never_gets_a_tag(self, monkeypatch, meeting, fields, transcript):
        _stub(
            monkeypatch,
            result=ExtractedAnswers(
                answers=[FieldAnswer(question="Employee strength?", raw_answer="150", classified_tag="Large")]
            ),
        )
        answers = extract_answers(meeting, PROFILE, fields, transcript)

        assert answers[1].classified_tag == ""
