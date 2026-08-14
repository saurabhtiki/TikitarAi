"""The persona an invitee talks to (requirement 6.7, spec 4).

The provider is stubbed at `run_structured`, as in `test_checks_sql_builder.py`.

The tag-coercion assertions are the ones with teeth: the MoM groups by exact tag and the
creator's coverage count reads the same field, so a tag taken on trust from the model would
show up wrong in two places at once.
"""

import pytest

from meetings import chat_agent
from meetings.chat_agent import (
    ChatTurnOutput,
    build_history_block,
    opening_message,
    send_turn,
    system_instructions,
)
from meetings.model import OPENING_TAG, OTHER_TAG, AgendaItem, ChatMessage, Meeting

PROFILE = {"profile_id": 1, "default_model": "test-model"}


@pytest.fixture
def meeting():
    return Meeting(
        subject="PO No 123",
        meeting_context="Resolve open issues on PO 123 with ABC Traders.",
        persona="You are the Purchase Manager.",
        context_sop="Never agree to a discount above 5%.",
        agenda=[
            AgendaItem(item="Delivery timeline", ai_note="Standard SLA is 30 days from PO date."),
            AgendaItem(item="Payment terms"),
        ],
    )


def _stub(monkeypatch, reply="Understood.", tag="Delivery timeline"):
    calls: list[dict] = []

    def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
        calls.append({"prompt": prompt, "instructions": instructions})
        return ChatTurnOutput(reply=reply, agenda_tag=tag)

    monkeypatch.setattr(chat_agent, "run_structured", fake_run_structured)
    return calls


class TestSystemInstructions:
    def test_the_persona_context_and_sop_all_reach_the_model(self, meeting):
        instructions = system_instructions(meeting)

        assert "You are the Purchase Manager." in instructions
        assert "Resolve open issues on PO 123" in instructions
        assert "Never agree to a discount above 5%." in instructions

    def test_every_agenda_item_and_its_note_is_listed(self, meeting):
        instructions = system_instructions(meeting)

        assert "Delivery timeline" in instructions
        assert "Standard SLA is 30 days from PO date." in instructions
        assert "Payment terms" in instructions

    def test_the_allowed_tags_are_spelled_out(self, meeting):
        instructions = system_instructions(meeting)

        assert '"Delivery timeline"' in instructions
        assert OTHER_TAG in instructions

    def test_a_meeting_with_no_persona_still_gets_one(self):
        assert "facilitator" in system_instructions(Meeting(subject="Anything"))

    def test_a_meeting_with_no_agenda_says_so_rather_than_listing_nothing(self):
        assert "no fixed agenda" in system_instructions(Meeting(subject="Anything"))


class TestHistoryBlock:
    def test_the_summary_and_the_recent_turns_both_appear(self):
        block = build_history_block(
            "Earlier they agreed to 30 days.",
            [ChatMessage(sender="user", text="What about payment?")],
        )

        assert "Earlier they agreed to 30 days." in block
        assert "Invitee: What about payment?" in block

    def test_a_fresh_conversation_produces_no_block_at_all(self):
        assert build_history_block("", []) == ""


class TestOpeningMessage:
    def test_it_runs_without_any_history(self, monkeypatch, meeting):
        calls = _stub(monkeypatch, reply="Hello, I'm the Purchase Manager.")
        result = opening_message(meeting, PROFILE)

        assert result.reply == "Hello, I'm the Purchase Manager."
        assert "Invitee:" not in calls[0]["prompt"]

    def test_it_is_tagged_as_the_opening_not_as_an_agenda_item(self, monkeypatch, meeting):
        # Listing the agenda is not discussing it — tagging this as item one would show every
        # invitee as having covered it before they arrived.
        _stub(monkeypatch, tag="Delivery timeline")
        assert opening_message(meeting, PROFILE).agenda_tag == OPENING_TAG

    def test_the_agenda_still_reaches_the_model_so_it_can_list_it(self, monkeypatch, meeting):
        calls = _stub(monkeypatch)
        opening_message(meeting, PROFILE)

        assert "Delivery timeline" in calls[0]["instructions"]


class TestSendTurn:
    def test_the_invitee_message_and_history_both_reach_the_model(self, monkeypatch, meeting):
        calls = _stub(monkeypatch)
        send_turn(
            meeting,
            PROFILE,
            "Earlier: introductions.",
            [ChatMessage(sender="ai", text="When can you deliver?")],
            "Within three weeks.",
        )

        prompt = calls[0]["prompt"]
        assert "Earlier: introductions." in prompt
        assert "AI: When can you deliver?" in prompt
        assert "Invitee: Within three weeks." in prompt

    def test_an_exact_tag_is_kept(self, monkeypatch, meeting):
        _stub(monkeypatch, tag="Payment terms")
        assert send_turn(meeting, PROFILE, "", [], "Hi").agenda_tag == "Payment terms"

    def test_a_near_miss_tag_is_coerced_rather_than_trusted(self, monkeypatch, meeting):
        _stub(monkeypatch, tag="Delivery timelines")
        assert send_turn(meeting, PROFILE, "", [], "Hi").agenda_tag == OTHER_TAG

    def test_an_invented_tag_becomes_other(self, monkeypatch, meeting):
        _stub(monkeypatch, tag="Something nobody asked about")
        assert send_turn(meeting, PROFILE, "", [], "Hi").agenda_tag == OTHER_TAG

    def test_a_missing_tag_becomes_other(self, monkeypatch, meeting):
        _stub(monkeypatch, tag="")
        assert send_turn(meeting, PROFILE, "", [], "Hi").agenda_tag == OTHER_TAG
