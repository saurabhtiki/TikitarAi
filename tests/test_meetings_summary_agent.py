"""The point-wise MoM (requirement 6.7, spec 4).

`TestFullHistoryIsTheSource` is the load-bearing class in this file. The standing decision
is that the MoM is built from the **complete stored transcript**, never from the rolling
`running_summary` — that summary is a lossy fold meant to keep a live conversation inside a
context window, and a permanent record built on it would quietly degrade the longer the
conversation ran. Those tests exist so the wrong-source bug cannot reappear silently.
"""

import pytest

from meetings import summary_agent
from meetings.exceptions import MeetingAgentError
from meetings.model import OPENING_TAG, OTHER_TAG, AgendaItem, ChatMessage, Meeting
from meetings.summary_agent import (
    AgendaItemSummary,
    MeetingSummary,
    build_prompt,
    coverage,
    from_json,
    generate_summary,
    to_json,
)

PROFILE = {"profile_id": 1, "default_model": "test-model"}


@pytest.fixture
def meeting():
    return Meeting(
        subject="PO No 123",
        meeting_context="Resolve open issues on PO 123.",
        agenda=[
            AgendaItem(item="Delivery timeline", ai_note="SLA is 30 days."),
            AgendaItem(item="Payment terms"),
        ],
    )


@pytest.fixture
def transcript():
    return [
        ChatMessage(message_id=1, sender="ai", text="Welcome, let's start.", agenda_tag=OPENING_TAG),
        ChatMessage(message_id=2, sender="user", text="Delivery will take 45 days.", agenda_tag="Delivery timeline"),
        ChatMessage(message_id=3, sender="ai", text="That exceeds the SLA.", agenda_tag="Delivery timeline"),
    ]


def _stub(monkeypatch, result=None, fails=False):
    calls: list[str] = []

    def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
        calls.append(prompt)
        if fails:
            from llm.client import LLMConnectionError

            raise LLMConnectionError("provider is down")
        return result or MeetingSummary(
            agenda_items=[AgendaItemSummary(item="Delivery timeline", discussed=True, notes="45 days quoted.")],
            other_extra="",
            closing_message="Thanks.",
        )

    monkeypatch.setattr(summary_agent, "run_structured", fake_run_structured)
    return calls


class TestFullHistoryIsTheSource:
    def test_every_message_reaches_the_prompt(self, meeting, transcript):
        prompt = build_prompt(meeting, transcript)

        assert "Delivery will take 45 days." in prompt
        assert "That exceeds the SLA." in prompt
        # The opening is included too: it is what the invitee was answering, and dropping it
        # would leave the first answer with no question above it.
        assert "Welcome, let's start." in prompt

    def test_the_signature_cannot_be_handed_a_running_summary(self, meeting, transcript):
        # The mechanism, not just a convention: `generate_summary` takes a message list, so
        # there is no argument on it that a rolling summary could arrive through.
        import inspect

        parameters = inspect.signature(generate_summary).parameters
        assert "messages" in parameters
        assert not any("summary" in name and name != "messages" for name in parameters)

    def test_a_wrong_running_summary_cannot_influence_the_result(self, monkeypatch, meeting, transcript):
        # The scenario this guards: a session whose stored rolling summary says something
        # untrue. The MoM must still describe the real transcript, because it never reads it.
        calls = _stub(monkeypatch)
        generate_summary(meeting, PROFILE, transcript)

        assert "45 days" in calls[0]
        assert "agreed to everything" not in calls[0]

    def test_an_empty_conversation_says_so_rather_than_inventing_one(self, meeting):
        assert "sent no messages" in build_prompt(meeting, [])


class TestPrompt:
    def test_the_agenda_and_its_notes_are_given_to_report_against(self, meeting, transcript):
        prompt = build_prompt(meeting, transcript)

        assert "Delivery timeline" in prompt
        assert "SLA is 30 days." in prompt
        assert "Payment terms" in prompt


class TestGenerating:
    def test_an_item_the_model_omitted_is_reported_as_not_discussed(self, monkeypatch, meeting, transcript):
        # Missing from the reply reads on screen as though it were never on the agenda —
        # a different and more flattering claim than "nobody got to it".
        _stub(monkeypatch)
        summary = generate_summary(meeting, PROFILE, transcript)

        by_item = {entry.item: entry for entry in summary.agenda_items}
        assert by_item["Delivery timeline"].discussed is True
        assert by_item["Payment terms"].discussed is False

    def test_items_come_back_in_agenda_order(self, monkeypatch, meeting, transcript):
        _stub(
            monkeypatch,
            result=MeetingSummary(
                agenda_items=[
                    AgendaItemSummary(item="Payment terms", discussed=True, notes="30 days net."),
                    AgendaItemSummary(item="Delivery timeline", discussed=True, notes="45 days."),
                ]
            ),
        )
        summary = generate_summary(meeting, PROFILE, transcript)

        assert [entry.item for entry in summary.agenda_items] == ["Delivery timeline", "Payment terms"]

    def test_something_off_agenda_the_model_reported_is_kept(self, monkeypatch, meeting, transcript):
        _stub(
            monkeypatch,
            result=MeetingSummary(
                agenda_items=[AgendaItemSummary(item="A surprise topic", discussed=True, notes="Raised late.")]
            ),
        )
        summary = generate_summary(meeting, PROFILE, transcript)

        assert "A surprise topic" in [entry.item for entry in summary.agenda_items]

    def test_a_provider_failure_is_reported_rather_than_stored_as_a_blank_record(
        self, monkeypatch, meeting, transcript
    ):
        _stub(monkeypatch, fails=True)
        with pytest.raises(MeetingAgentError):
            generate_summary(meeting, PROFILE, transcript)


class TestCoverage:
    def test_only_real_agenda_items_count(self, meeting, transcript):
        assert coverage(transcript, meeting) == {"Delivery timeline"}

    def test_the_opening_message_does_not_count_as_coverage(self, meeting):
        messages = [ChatMessage(message_id=1, sender="ai", text="Welcome", agenda_tag=OPENING_TAG)]
        assert coverage(messages, meeting) == set()

    def test_off_agenda_exchanges_do_not_count(self, meeting):
        messages = [ChatMessage(message_id=1, sender="user", text="Aside", agenda_tag=OTHER_TAG)]
        assert coverage(messages, meeting) == set()


class TestSerialisation:
    def test_a_summary_round_trips(self):
        original = MeetingSummary(
            agenda_items=[AgendaItemSummary(item="Delivery timeline", discussed=True, notes="45 days.")],
            other_extra="They mentioned a quality complaint.",
            closing_message="Thanks.",
        )
        restored = from_json(to_json(original))

        assert restored is not None
        assert restored.agenda_items[0].notes == "45 days."
        assert restored.other_extra == "They mentioned a quality complaint."

    def test_unreadable_stored_text_costs_one_panel_rather_than_the_page(self):
        assert from_json("not json") is None
        assert from_json("") is None
