"""Both pages, driven end to end over a table item (requirement 6.7, Phase 2, spec 3a/3b).

Kept apart from the Phase 1 page tests because what these check is a different question: not
"does the screen draw" but "does a row an invitee typed survive the round trip and turn up in
the creator's matrix".

The invitee page is driven through a probe script, as in `test_meeting_invitee_page.py` — it
is deliberately not registered as an `st.Page`, so `AppTest` has no route to it.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model
from meetings import access
from meetings import db as meetings_db
from meetings.extraction_agent import ExtractedAnswers, FieldAnswer
from meetings.model import (
    SENDER_AI,
    SENDER_USER,
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    EvaluationAnswer,
    EvaluationField,
    Meeting,
)
from meetings.summary_agent import AgendaItemSummary, MeetingSummary

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATOR_PAGE = str(PROJECT_ROOT / "app_pages" / "meetings.py")

PROBE = """
import streamlit as st
from app_pages.meeting_invitee import render_invitee_page

render_invitee_page(st.session_state["probe_meeting_id"], st.session_state["probe_token"])
"""

TOKEN = "token-abc"
CODE = "123456"


def _setup(tmp_path, monkeypatch):
    """A meeting with a discussion item, a table item, one evaluation field and one invitee."""
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    meetings_db.init_meetings_tables()
    profile = create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")
    set_default_model(profile["profile_id"], 1)

    meeting = meetings_db.create_meeting(
        1,
        Meeting(
            subject="Vendor comparison",
            persona="You are the Purchase Manager.",
            agenda=[
                AgendaItem(item="Delivery timeline", ai_note="SLA is 30 days."),
                AgendaItem(item="Outstanding bills", ai_note="Confirm each date.", item_type=TABLE_ITEM),
            ],
        ),
    )
    invitee_id = meetings_db.add_invitee(
        meeting.meeting_id, 1, "Raj", "raj@vendor.com", TOKEN, access.encrypt_code(CODE)
    )
    meetings_db.replace_evaluation_fields(
        meeting.meeting_id,
        1,
        [EvaluationField(question="Years of experience?", buckets=["Low", "High"])],
    )
    return meeting, invitee_id


def _attach_table(meeting, rows=3):
    return meetings_db.save_agenda_table(
        meeting.meeting_id,
        1,
        AgendaTable(
            meeting_id=meeting.meeting_id,
            item_ref="Outstanding bills",
            source_file="bills.csv",
            locked_columns=["Bill No"],
            editable_columns=["Expected date"],
            base_data=[{"Bill No": f"B-{index + 1}"} for index in range(rows)],
        ),
    )


def _creator_app(tmp_path, monkeypatch, meeting):
    app = AppTest.from_file(CREATOR_PAGE, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.session_state["meeting_open_id"] = meeting.meeting_id
    return app


def _invitee_app(tmp_path, meeting, invitee_id):
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")

    app = AppTest.from_file(str(probe), default_timeout=60)
    app.session_state["probe_meeting_id"] = meeting.meeting_id
    app.session_state["probe_token"] = TOKEN
    app.session_state["invitee_verified"] = True
    app.session_state["invitee_meeting_id"] = meeting.meeting_id
    app.session_state["invitee_id"] = invitee_id
    return app


def _started(meeting, invitee_id):
    """A conversation already under way, so the page renders the chat rather than opening one."""
    meetings_db.ensure_session(meeting.meeting_id, invitee_id)
    meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Welcome.", "Opening")


class TestInviteeTableTab:
    def test_a_meeting_with_no_table_item_still_has_no_tabs(self, tmp_path, monkeypatch):
        # The plain chat must be untouched by this phase — including `st.chat_input` staying
        # at page level, where Streamlit pins it to the bottom.
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        meeting.agenda = [AgendaItem(item="Delivery timeline")]
        meetings_db.update_meeting(1, meeting)
        _started(meeting, invitee_id)

        app = _invitee_app(tmp_path, meeting, invitee_id).run()

        assert not app.tabs
        assert app.chat_input(key="invitee_chat_input") is not None

    def test_a_table_item_gets_its_own_tab_beside_the_discussion(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        _attach_table(meeting)
        _started(meeting, invitee_id)

        app = _invitee_app(tmp_path, meeting, invitee_id).run()

        labels = [tab.label for tab in app.tabs]
        assert len(labels) == 2
        assert "General Discussion" in labels[0]
        assert "Outstanding bills" in labels[1]

    def test_the_grid_shows_the_creators_rows(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        _attach_table(meeting)
        _started(meeting, invitee_id)

        app = _invitee_app(tmp_path, meeting, invitee_id).run()

        assert any("0 of 3 row(s) filled" in caption.value for caption in app.caption)

    def test_an_item_whose_data_is_not_attached_yet_says_so(self, tmp_path, monkeypatch):
        # An invitee opening their link early must find an explanation, not an empty grid.
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        _started(meeting, invitee_id)

        app = _invitee_app(tmp_path, meeting, invitee_id).run()

        assert any("isn't ready yet" in info.value for info in app.info)

    def test_saved_rows_are_stored_and_counted(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        table_id = _attach_table(meeting)
        _started(meeting, invitee_id)

        app = _invitee_app(tmp_path, meeting, invitee_id).run()
        # `AppTest` can't type into a data editor, so the edit is made where the widget's
        # value is read from — what this checks is the save path, not the grid rendering.
        app.session_state[f"invitee_table_{table_id}"] = {
            "edited_rows": {0: {"Expected date": "2026-09-01"}},
            "added_rows": [],
            "deleted_rows": [],
        }
        app.button(key=f"invitee_table_save_{table_id}").click().run()

        assert meetings_db.load_table_responses(table_id, invitee_id) == {
            0: {"Expected date": "2026-09-01"}
        }

    def test_a_closed_chat_leaves_the_grid_read_only(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        table_id = _attach_table(meeting)
        _started(meeting, invitee_id)
        meetings_db.close_session(
            meeting.meeting_id, invitee_id, MeetingSummary(closing_message="Thanks.").model_dump_json()
        )

        app = _invitee_app(tmp_path, meeting, invitee_id).run()

        assert f"invitee_table_save_{table_id}" not in [button.key for button in app.button]
        assert not app.chat_input


class TestClosingWithTablesAndEvaluations:
    def test_the_summary_reports_the_grid_as_a_row_count(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        table_id = _attach_table(meeting)
        _started(meeting, invitee_id)
        meetings_db.save_table_responses(table_id, invitee_id, {0: {"Expected date": "2026-09-01"}})

        import app_pages.meeting_invitee as page

        seen = {}

        def _summarise(meeting_arg, profile, messages, *, table_progress=None, **kwargs):
            seen["progress"] = table_progress
            return MeetingSummary(closing_message="Thanks.")

        monkeypatch.setattr(page.summary_agent, "generate_summary", _summarise)
        monkeypatch.setattr(page.extraction_agent, "extract_answers", lambda *a, **k: [])

        app = _invitee_app(tmp_path, meeting, invitee_id).run()
        app.button(key="invitee_close_chat").click().run()

        assert seen["progress"] == {"Outstanding bills": (1, 3)}

    def test_closing_extracts_the_evaluation_answers(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        _started(meeting, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_USER, "We've been going 8 years.")

        import app_pages.meeting_invitee as page

        field = meetings_db.list_evaluation_fields(meeting.meeting_id)[0]
        monkeypatch.setattr(
            page.summary_agent, "generate_summary", lambda *a, **k: MeetingSummary(closing_message="Thanks.")
        )
        monkeypatch.setattr(
            page.extraction_agent,
            "extract_answers",
            lambda *a, **k: [
                EvaluationAnswer(field_id=field.field_id, raw_answer="8 years", classified_tag="High")
            ],
        )

        app = _invitee_app(tmp_path, meeting, invitee_id).run()
        app.button(key="invitee_close_chat").click().run()

        stored = meetings_db.list_evaluation_answers(meeting.meeting_id)
        assert [(answer.raw_answer, answer.classified_tag) for answer in stored] == [("8 years", "High")]

    def test_a_failed_extraction_does_not_cost_the_summary(self, tmp_path, monkeypatch):
        # The MoM is the record the invitee is owed. The evaluation answers are a
        # convenience for the creator, who can re-extract them from their own page.
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        _started(meeting, invitee_id)

        import app_pages.meeting_invitee as page
        from meetings.exceptions import MeetingAgentError

        def _fail(*args, **kwargs):
            raise MeetingAgentError("provider is down")

        monkeypatch.setattr(
            page.summary_agent, "generate_summary", lambda *a, **k: MeetingSummary(closing_message="Thanks.")
        )
        monkeypatch.setattr(page.extraction_agent, "extract_answers", _fail)

        app = _invitee_app(tmp_path, meeting, invitee_id).run()
        app.button(key="invitee_close_chat").click().run()

        assert meetings_db.ensure_session(meeting.meeting_id, invitee_id).closed is True
        assert "Thanks." in meetings_db.get_session_summary(meeting.meeting_id, invitee_id)


class TestCreatorTableSetup:
    def test_a_table_item_offers_somewhere_to_attach_its_data(self, tmp_path, monkeypatch):
        meeting, _ = _setup(tmp_path, monkeypatch)
        app = _creator_app(tmp_path, monkeypatch, meeting).run()

        assert app.file_uploader(key=f"meetings_table_upload_{meeting.meeting_id}_Outstanding bills") is not None

    def test_an_attached_table_reports_its_columns_back(self, tmp_path, monkeypatch):
        meeting, _ = _setup(tmp_path, monkeypatch)
        _attach_table(meeting)

        app = _creator_app(tmp_path, monkeypatch, meeting).run()
        captions = " ".join(caption.value for caption in app.caption)

        assert "bills.csv" in captions
        assert "editable: Expected date" in captions

    def test_removing_a_table_takes_it_and_its_answers_away(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        table_id = _attach_table(meeting)
        meetings_db.save_table_responses(table_id, invitee_id, {0: {"Expected date": "x"}})

        app = _creator_app(tmp_path, monkeypatch, meeting).run()
        app.button(key=f"meetings_table_remove_{meeting.meeting_id}_Outstanding bills").click().run()

        assert meetings_db.list_agenda_tables(meeting.meeting_id) == []

    def test_the_evaluation_questions_are_shown_back(self, tmp_path, monkeypatch):
        meeting, _ = _setup(tmp_path, monkeypatch)
        app = _creator_app(tmp_path, monkeypatch, meeting).run()

        assert any("Years of experience?" in element.value for element in app.markdown)


class TestCreatorComparisons:
    def test_the_consolidated_matrix_reads_a_closed_invitees_mom(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.close_session(
            meeting.meeting_id,
            invitee_id,
            MeetingSummary(
                agenda_items=[
                    AgendaItemSummary(item="Delivery timeline", discussed=True, notes="45 days quoted.")
                ]
            ).model_dump_json(),
        )

        app = _creator_app(tmp_path, monkeypatch, meeting).run()
        rendered = [frame.value for frame in app.dataframe]

        assert any("45 days quoted." in frame.to_string() for frame in rendered)

    def test_the_evaluation_matrix_reads_stored_answers(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        field = meetings_db.list_evaluation_fields(meeting.meeting_id)[0]
        meetings_db.save_evaluation_answers(
            invitee_id,
            [EvaluationAnswer(field_id=field.field_id, raw_answer="8 years", classified_tag="High")],
        )

        app = _creator_app(tmp_path, monkeypatch, meeting).run()
        rendered = [frame.value for frame in app.dataframe]

        assert any("8 years (High)" in frame.to_string() for frame in rendered)

    def test_the_table_comparison_stacks_what_invitees_filled_in(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        table_id = _attach_table(meeting)
        meetings_db.save_table_responses(table_id, invitee_id, {0: {"Expected date": "2026-09-01"}})

        app = _creator_app(tmp_path, monkeypatch, meeting).run()
        rendered = [frame.value for frame in app.dataframe]

        assert any("2026-09-01" in frame.to_string() for frame in rendered)

    def test_generating_a_status_also_extracts_the_answers(self, tmp_path, monkeypatch):
        meeting, invitee_id = _setup(tmp_path, monkeypatch)
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_USER, "8 years in the trade.")

        import app_pages.meetings as page

        field = meetings_db.list_evaluation_fields(meeting.meeting_id)[0]
        monkeypatch.setattr(
            page.summary_agent, "generate_summary", lambda *a, **k: MeetingSummary(closing_message="Ongoing.")
        )
        monkeypatch.setattr(
            page.extraction_agent,
            "extract_answers",
            lambda *a, **k: [EvaluationAnswer(field_id=field.field_id, raw_answer="8 years")],
        )

        app = _creator_app(tmp_path, monkeypatch, meeting).run()
        app.button(key=f"meetings_status_{invitee_id}").click().run()

        assert [answer.raw_answer for answer in meetings_db.list_evaluation_answers(meeting.meeting_id)] == [
            "8 years"
        ]
