"""Drafting a follow-up, and the files a user's own client opens (requirement 6.5).

The `.eml` and `.ics` assertions parse the output back rather than matching strings: a file
that looks right and doesn't open in a mail client is the failure that matters here, and
only a parser catches it.
"""

from datetime import datetime
from email import message_from_bytes
from email.policy import default as default_policy

import pandas as pd
import pytest

from checks import actions
from checks.actions import (
    DraftedAction,
    build_prompt,
    draft_action,
    drafts_frame_rows,
    file_name_for,
    parse_when,
    to_eml,
    to_ics,
)
from checks.model import ACTION_EMAIL, ACTION_MEETING, ActionDraft, Check, freeze_run
from llm.client import LLMConnectionError

RESULT = pd.DataFrame(
    {
        "employee": ["Ana", "Bo"],
        "criteria_result": [4.0, 12.0],
        "criteria_met": ["Yes", "No"],
    }
)


def _saved_check() -> Check:
    check = Check(name="Bonus cap", criteria_text="Bonus must be at most 5% of basic.")
    check.saved_run = freeze_run("SELECT 1", RESULT)
    return check


def _email_draft(**overrides) -> ActionDraft:
    fields = {
        "kind": ACTION_EMAIL,
        "recipients": {"to": ["hr@example.com"], "cc": ["controller@example.com"]},
        "subject": "Bonus policy breaches",
        "body": "Bo received 12% of basic, above the 5% cap.",
    }
    fields.update(overrides)
    return ActionDraft(**fields)


class TestPrompt:
    def test_only_the_breaching_rows_reach_the_model(self):
        prompt = build_prompt("", _saved_check(), _email_draft())
        assert "Bo" in prompt
        assert "Ana" not in prompt

    def test_the_audience_the_date_and_the_persona_all_reach_it(self):
        draft = _email_draft(when="2026-08-14 10:00")
        prompt = build_prompt("You are a finance controller.", _saved_check(), draft)
        assert "hr@example.com" in prompt
        assert "2026-08-14 10:00" in prompt
        assert "finance controller" in prompt

    def test_a_typed_title_is_given_to_the_model_to_write_to(self):
        assert "Bonus policy breaches" in build_prompt("", _saved_check(), _email_draft())


class TestDrafting:
    def test_a_draft_is_filled_in_place(self, monkeypatch):
        monkeypatch.setattr(
            actions,
            "run_structured",
            lambda *args, **kwargs: DraftedAction(subject="Written subject", body="Written body"),
        )
        draft = _email_draft(subject="", body="")
        returned, warnings = draft_action({}, "", _saved_check(), draft)

        assert returned is draft
        assert draft.subject == "Written subject"
        assert draft.body == "Written body"
        assert warnings == []

    def test_a_subject_the_user_typed_survives(self, monkeypatch):
        """Replacing it would silently undo a decision they made — and the prompt was told
        to write the body to match it."""
        monkeypatch.setattr(
            actions,
            "run_structured",
            lambda *args, **kwargs: DraftedAction(subject="Model's own", body="Body"),
        )
        draft = _email_draft(body="")
        draft_action({}, "", _saved_check(), draft)
        assert draft.subject == "Bonus policy breaches"

    def test_each_kind_gets_its_own_instructions(self, monkeypatch):
        seen = []

        def capture(profile, prompt, schema, *, instructions=None, text_field=None, key_path=None):
            seen.append(instructions)
            return DraftedAction(subject="s", body="b")

        monkeypatch.setattr(actions, "run_structured", capture)
        draft_action({}, "", _saved_check(), _email_draft(body=""))
        draft_action({}, "", _saved_check(), ActionDraft(kind=ACTION_MEETING))

        assert "email" in seen[0]
        assert "agenda" in seen[1]

    def test_a_provider_failure_costs_the_wording_and_nothing_else(self, monkeypatch):
        def boom(*args, **kwargs):
            raise LLMConnectionError("Connection error")

        monkeypatch.setattr(actions, "run_structured", boom)
        draft = _email_draft()
        _, warnings = draft_action({}, "", _saved_check(), draft)

        assert "Connection error" in warnings[0]
        assert draft.recipients == {"to": ["hr@example.com"], "cc": ["controller@example.com"]}

    def test_an_unsaved_criteria_is_refused_without_a_call(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("should not have called the provider")

        monkeypatch.setattr(actions, "run_structured", boom)
        _, warnings = draft_action({}, "", Check(name="Untested"), _email_draft())
        assert "Save this criteria" in warnings[0]

    def test_an_empty_body_is_reported_rather_than_stored(self, monkeypatch):
        monkeypatch.setattr(
            actions, "run_structured", lambda *args, **kwargs: DraftedAction(subject="s", body="  ")
        )
        draft = _email_draft(body="Original")
        _, warnings = draft_action({}, "", _saved_check(), draft)
        assert warnings == ["The model returned an empty draft."]
        assert draft.body == "Original"


class TestEml:
    def test_it_parses_back_as_a_real_message(self):
        parsed = message_from_bytes(to_eml(_email_draft()), policy=default_policy)
        assert parsed["Subject"] == "Bonus policy breaches"
        assert parsed["To"] == "hr@example.com"
        assert parsed["Cc"] == "controller@example.com"
        assert "above the 5% cap" in parsed.get_content()

    def test_it_carries_no_sender_or_date(self):
        """It is a draft the user sends from their own account — stamping either would make
        it look like a message that had already gone out from somewhere."""
        parsed = message_from_bytes(to_eml(_email_draft()), policy=default_policy)
        assert parsed["From"] is None
        assert parsed["Date"] is None

    def test_empty_recipient_lists_produce_no_header(self):
        parsed = message_from_bytes(to_eml(_email_draft(recipients={"to": ["a@b.com"], "cc": []})), policy=default_policy)
        assert parsed["Cc"] is None

    def test_a_draft_with_no_subject_still_produces_a_file(self):
        parsed = message_from_bytes(to_eml(_email_draft(subject="")), policy=default_policy)
        assert parsed["Subject"] == "(no subject)"


class TestIcs:
    def _lines(self, draft, **kwargs):
        return to_ics(draft, **kwargs).decode("utf-8").split("\r\n")

    def test_a_dated_meeting_carries_a_start_time(self):
        draft = ActionDraft(kind=ACTION_MEETING, subject="Bonus review", when="2026-08-14 10:00")
        lines = self._lines(draft, now=datetime(2026, 8, 7, 9, 0, 0))
        assert "BEGIN:VCALENDAR" in lines
        assert "DTSTART:20260814T100000" in lines
        assert "SUMMARY:Bonus review" in lines

    def test_an_unreadable_date_still_produces_an_openable_invite(self):
        """Better than an invite confidently scheduled on the wrong day."""
        draft = ActionDraft(kind=ACTION_MEETING, subject="Bonus review", when="next Tuesday-ish")
        lines = self._lines(draft)
        assert not any(line.startswith("DTSTART") for line in lines)
        assert "SUMMARY:Bonus review" in lines

    def test_attendees_are_listed(self):
        draft = ActionDraft(kind=ACTION_MEETING, recipients={"attendees": ["hr@example.com"]})
        assert any("hr@example.com" in line for line in self._lines(draft) if line.startswith("ATTENDEE"))

    def test_the_four_special_characters_are_escaped(self):
        """Unescaped, a comma or a newline in a description silently truncates the event in
        some clients rather than failing visibly."""
        draft = ActionDraft(kind=ACTION_MEETING, subject="A; B, C", body="line one\nline two\\end")
        text = to_ics(draft).decode("utf-8")
        assert "SUMMARY:A\\; B\\, C" in text
        assert "DESCRIPTION:line one\\nline two\\\\end" in text

    def test_lines_end_with_crlf(self):
        """RFC 5545 requires it, and some clients reject a bare-LF calendar."""
        assert to_ics(ActionDraft(kind=ACTION_MEETING)).endswith(b"END:VCALENDAR\r\n")


class TestParseWhen:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2026-08-14 10:00", datetime(2026, 8, 14, 10, 0)),
            ("2026-08-14T10:00", datetime(2026, 8, 14, 10, 0)),
            ("2026-08-14", datetime(2026, 8, 14, 0, 0)),
        ],
    )
    def test_the_supported_formats(self, text, expected):
        assert parse_when(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", "next Tuesday", "14/08/2026"])
    def test_anything_else_is_declined_rather_than_guessed(self, text):
        assert parse_when(text) is None


class TestReportRows:
    def test_only_confirmed_drafts_are_reported(self):
        check = _saved_check()
        check.actions.append(_email_draft())
        assert drafts_frame_rows(check) == []

        check.actions[0].confirmed = True
        rows = drafts_frame_rows(check)
        assert len(rows) == 1
        assert rows[0]["Action"] == "Email"
        assert "hr@example.com" in rows[0]["Who"]

    def test_the_filename_is_named_after_the_criteria_and_is_safe(self):
        check = Check(name="Bonus / cap: HR")
        assert file_name_for(check, _email_draft()) == "Bonus_cap_HR_email.eml"
        assert file_name_for(check, ActionDraft(kind=ACTION_MEETING)).endswith(".ics")
