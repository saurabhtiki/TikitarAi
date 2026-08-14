"""Folding old turns out of the live context window (requirement 6.7, spec 3).

The provider is stubbed at `run_structured`, the one seam every LLM call in this package
goes through — same approach as `test_checks_sql_builder.py`.

What these assertions protect: a fold must never be able to cost the invitee a message. It
runs on the ordinary chat path, so every failure mode below has to end with the conversation
intact and the fold simply not having happened.
"""

import pytest

from auth.db import create_user, init_db, seed_default_admin
from meetings import running_summary
from meetings.db import (
    add_invitee,
    add_message,
    create_meeting,
    ensure_session,
    init_meetings_tables,
    list_messages,
    update_running_summary,
)
from meetings.model import SENDER_AI, SENDER_USER, ChatMessage, Meeting
from meetings.running_summary import (
    RECENT_MESSAGES,
    FoldedSummary,
    maybe_fold,
    pending_fold,
    recent_messages,
    render_turns,
)

OWNER = 1
PROFILE = {"profile_id": 1, "default_model": "test-model"}


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    init_meetings_tables(path)
    return path


@pytest.fixture
def session(db_path):
    meeting = create_meeting(OWNER, Meeting(subject="PO No 123"), db_path=db_path)
    invitee_id = add_invitee(meeting.meeting_id, OWNER, "Raj", "raj@x.com", "tok", "enc", db_path=db_path)
    ensure_session(meeting.meeting_id, invitee_id, db_path=db_path)
    return meeting.meeting_id, invitee_id


def _fill(db_path, meeting_id, invitee_id, turns):
    """`turns` complete exchanges — one invitee message and one AI reply each."""
    for index in range(turns):
        add_message(meeting_id, invitee_id, SENDER_USER, f"question {index}", db_path=db_path)
        add_message(meeting_id, invitee_id, SENDER_AI, f"answer {index}", db_path=db_path)


def _stub(monkeypatch, *, reply="Folded summary.", fails=False):
    prompts: list[str] = []

    def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
        prompts.append(prompt)
        if fails:
            from llm.client import LLMConnectionError

            raise LLMConnectionError("provider is down")
        return FoldedSummary(summary=reply)

    monkeypatch.setattr(running_summary, "run_structured", fake_run_structured)
    return prompts


class TestWindowing:
    def test_the_recent_window_is_the_tail(self):
        messages = [ChatMessage(message_id=index, text=str(index)) for index in range(50)]
        assert len(recent_messages(messages)) == RECENT_MESSAGES
        assert recent_messages(messages)[-1].text == "49"

    def test_a_short_conversation_has_nothing_to_fold(self):
        messages = [ChatMessage(message_id=index) for index in range(1, RECENT_MESSAGES + 1)]
        assert pending_fold(messages, 0) == []

    def test_only_what_has_aged_out_is_folded(self):
        # Ids start at 1, as SQLite's AUTOINCREMENT gives them — which is what makes the
        # default cutoff of 0 mean "nothing folded yet".
        messages = [ChatMessage(message_id=index) for index in range(1, RECENT_MESSAGES + 5)]
        assert [message.message_id for message in pending_fold(messages, 0)] == [1, 2, 3, 4]

    def test_what_a_previous_fold_covered_is_not_folded_again(self):
        messages = [ChatMessage(message_id=index) for index in range(1, RECENT_MESSAGES + 5)]
        assert [message.message_id for message in pending_fold(messages, 2)] == [3, 4]

    def test_turns_render_with_who_said_them(self):
        rendered = render_turns(
            [ChatMessage(sender=SENDER_USER, text="Hi"), ChatMessage(sender=SENDER_AI, text="Hello")]
        )
        assert rendered == "Invitee: Hi\nAI: Hello"


class TestFolding:
    def test_a_short_conversation_does_not_call_the_model_at_all(self, monkeypatch, db_path, session):
        meeting_id, invitee_id = session
        prompts = _stub(monkeypatch)
        _fill(db_path, meeting_id, invitee_id, turns=3)

        assert maybe_fold(PROFILE, meeting_id, invitee_id, db_path=db_path) is False
        assert prompts == []

    def test_a_long_conversation_folds_and_advances_the_cutoff(self, monkeypatch, db_path, session):
        meeting_id, invitee_id = session
        _stub(monkeypatch, reply="They agreed to 30 days.")
        _fill(db_path, meeting_id, invitee_id, turns=13)

        assert maybe_fold(PROFILE, meeting_id, invitee_id, db_path=db_path) is True

        stored = ensure_session(meeting_id, invitee_id, db_path=db_path)
        messages = list_messages(meeting_id, invitee_id, db_path=db_path)
        assert stored.running_summary == "They agreed to 30 days."
        # Exactly the messages that aged out, and no further.
        assert stored.folded_through_message_id == messages[-RECENT_MESSAGES - 1].message_id

    def test_the_existing_summary_is_given_to_the_model_to_build_on(self, monkeypatch, db_path, session):
        meeting_id, invitee_id = session
        prompts = _stub(monkeypatch)
        _fill(db_path, meeting_id, invitee_id, turns=13)
        update_running_summary(meeting_id, invitee_id, "Earlier: introductions.", 0, db_path=db_path)

        maybe_fold(PROFILE, meeting_id, invitee_id, db_path=db_path)

        assert "Earlier: introductions." in prompts[0]
        assert "question 0" in prompts[0]

    def test_folding_twice_over_does_not_refold_the_same_turns(self, monkeypatch, db_path, session):
        meeting_id, invitee_id = session
        prompts = _stub(monkeypatch)
        _fill(db_path, meeting_id, invitee_id, turns=13)

        assert maybe_fold(PROFILE, meeting_id, invitee_id, db_path=db_path) is True
        assert maybe_fold(PROFILE, meeting_id, invitee_id, db_path=db_path) is False
        assert len(prompts) == 1


class TestFailuresCostNothing:
    def test_a_provider_failure_leaves_every_message_intact(self, monkeypatch, db_path, session):
        meeting_id, invitee_id = session
        _stub(monkeypatch, fails=True)
        _fill(db_path, meeting_id, invitee_id, turns=13)

        assert maybe_fold(PROFILE, meeting_id, invitee_id, db_path=db_path) is False

        # The conversation is the thing that must survive; a stale summary is not a loss.
        assert len(list_messages(meeting_id, invitee_id, db_path=db_path)) == 26
        stored = ensure_session(meeting_id, invitee_id, db_path=db_path)
        assert stored.running_summary == ""
        assert stored.folded_through_message_id == 0

    def test_an_empty_summary_is_refused_rather_than_stored(self, monkeypatch, db_path, session):
        meeting_id, invitee_id = session
        _stub(monkeypatch, reply="   ")
        _fill(db_path, meeting_id, invitee_id, turns=13)
        update_running_summary(meeting_id, invitee_id, "Something real.", 0, db_path=db_path)

        assert maybe_fold(PROFILE, meeting_id, invitee_id, db_path=db_path) is False
        assert ensure_session(meeting_id, invitee_id, db_path=db_path).running_summary == "Something real."
