"""Phase 70: a step's Show document, and a Fill table the invitee must fill in first.

The organiser's example: a Price list table, then a Rates discussion with Fill table = Price
list. The chat waits until every row has a Rate, then the discussion starts.
"""

import pandas as pd

from app_pages import meetings as organiser_page
from meetings import chat_agent, flow, show_files, steps, tables
from meetings import db as meetings_db
from meetings.chat_agent import ChatTurnOutput, DiscussionReading
from meetings.model import (
    ANSWER_NUMBER,
    QUESTION_ITEM,
    SENDER_AI,
    SENDER_USER,
    STEP_ANSWERED,
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    ChatMessage,
    ColumnRule,
    Meeting,
    StepAnswer,
    agenda_from_json,
    agenda_to_json,
)
from tests.test_meetings_steps_pages import _invitee_app, _setup

PRICES = "Price list"
RATES = "Rates"
DELIVERY = "Delivery"


def _agenda() -> list[AgendaItem]:
    return [
        AgendaItem(item=PRICES, item_type=TABLE_ITEM),
        AgendaItem(item=RATES, fill_table=PRICES, show_file="Rate card", ai_note="Ask about big changes."),
        AgendaItem(item=DELIVERY, item_type=QUESTION_ITEM, answer_type=ANSWER_NUMBER),
    ]


def _sheet(required: bool = False, rates=("", "")) -> AgendaTable:
    return AgendaTable(
        item_ref=PRICES,
        locked_columns=["Item"],
        editable_columns=["Rate", "Remarks"],
        base_data=[{"Item": "Bolt", "Rate": rates[0]}, {"Item": "Nut", "Rate": rates[1]}],
        column_rules={"Rate": ColumnRule(answer_type=ANSWER_NUMBER, required=required)},
    )


class TestAgenda:
    def test_the_new_cells_save_and_load(self):
        loaded = agenda_from_json(agenda_to_json(_agenda()))
        assert loaded[1].fill_table == PRICES and loaded[1].show_file == "Rate card"

    def test_an_agenda_without_them_reads_back_the_same(self):
        plain = [AgendaItem(item="Chat")]
        assert "show_file" not in agenda_to_json(plain) and "fill_table" not in agenda_to_json(plain)

    def test_fill_table_must_name_a_table_row(self):
        agenda = [AgendaItem(item=RATES, fill_table="Nothing"), AgendaItem(item=PRICES, item_type=TABLE_ITEM)]
        assert "isn't a Table row of this agenda" in " ".join(steps.agenda_problems(agenda))

    def test_fill_table_is_refused_on_a_for_each_question(self):
        agenda = [
            AgendaItem(item=PRICES, item_type=TABLE_ITEM),
            AgendaItem(item="Paid", item_type=QUESTION_ITEM, loop="Invoices", fill_table=PRICES),
        ]
        assert "can't have a Fill table" in " ".join(steps.agenda_problems(agenda))

    def test_the_grid_spells_fill_table_like_the_table_row(self):
        frame = pd.DataFrame(
            [
                {**organiser_page._blank_agenda_row(), "Agenda item": PRICES, "Type": "Table"},
                {**organiser_page._blank_agenda_row(), "Agenda item": RATES, "Fill table": "price LIST"},
            ]
        )
        assert organiser_page._agenda_from_frame(frame)[1].fill_table == PRICES

    def test_a_missing_document_gets_a_note(self):
        notes = show_files.missing_documents(_agenda(), [{"file_id": 1, "filename": "Other"}])
        assert notes and "Rate card" in notes[0]
        assert show_files.missing_documents(_agenda(), [{"file_id": 1, "filename": "rate card"}]) == []


class TestFilled:
    def test_every_fill_in_column_counts_when_none_is_required(self):
        sheet = _sheet()
        assert not tables.is_filled(sheet, {0: {"Rate": "5"}, 1: {"Rate": "6"}})
        assert tables.is_filled(sheet, {0: {"Rate": "5", "Remarks": "ok"}, 1: {"Rate": "6", "Remarks": "ok"}})

    def test_only_required_columns_count_when_some_are(self):
        sheet = _sheet(required=True)
        assert not tables.is_filled(sheet, {0: {"Rate": "5"}})
        assert tables.is_filled(sheet, {0: {"Rate": "5"}, 1: {"Rate": "6"}})

    def test_a_value_in_the_uploaded_sheet_counts(self):
        assert tables.is_filled(_sheet(required=True, rates=("5", "")), {1: {"Rate": "6"}})

    def test_the_bot_sees_what_was_filled_in(self):
        text = tables.filled_text(_sheet(required=True), {0: {"Rate": "5"}})
        assert "Item,Rate,Remarks" in text and "Bolt,5," in text

    def test_tables_left_counts_the_open_steps_only(self):
        meeting = Meeting(agenda=_agenda())
        assert steps.tables_left(meeting, {}, {PRICES}) == [PRICES]
        assert steps.tables_left(meeting, {RATES: StepAnswer(status=STEP_ANSWERED)}, {PRICES}) == []
        assert steps.tables_left(meeting, {}, set()) == []


class TestGuidanceAndFlow:
    def test_fill_table_guidance_holds_the_step(self):
        text = chat_agent.fill_table_guidance(_agenda()[1], "Rate (a number)")
        assert f'first the invitee must fill in the "{PRICES}" tab' in text
        assert "do not move on" in text

    def test_the_flow_picture_names_the_table_and_the_document(self):
        dot = flow.flow_dot(_agenda())
        assert f"fill {PRICES} first" in dot and "shows Rate card" in dot


class TestInviteePage:
    def _started(self, tmp_path, monkeypatch, *, rates=("", ""), with_doc=False):
        meeting, invitee_id, probe = _setup(tmp_path, monkeypatch, agenda=_agenda())
        table_id = meetings_db.save_agenda_table(meeting.meeting_id, 1, _sheet(required=True, rates=rates))
        if with_doc:
            meetings_db.add_link(meeting.meeting_id, 1, "Rate card", "https://example.com/rates")
        meetings_db.ensure_session(meeting.meeting_id, invitee_id)
        meetings_db.add_message(meeting.meeting_id, invitee_id, SENDER_AI, "Please fill in the Price list.", RATES)
        return meeting, invitee_id, probe, table_id

    def _stub(self, monkeypatch) -> dict:
        import app_pages.meeting_invitee as page

        seen = {"read": 0}

        def _read(*args, **kwargs):
            seen["read"] += 1
            return DiscussionReading(covered=True)

        monkeypatch.setattr(page.chat_agent, "read_discussion", _read)

        def _reply(*args, **kwargs):
            seen.update(kwargs)
            return ChatTurnOutput(reply="Ok.", agenda_tag="")

        monkeypatch.setattr(page.chat_agent, "send_turn", _reply)
        return seen

    def test_an_unfilled_table_holds_the_step(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, _ = self._started(tmp_path, monkeypatch)
        seen = self._stub(monkeypatch)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        assert any("Fill in the 📋 Price list tab" in caption.value for caption in app.caption)
        assert app.button(key="invitee_close_chat").disabled
        app.chat_input(key="invitee_chat_input").set_value("Looks fine to me").run()
        assert not app.exception
        assert seen["read"] == 0  # no reply is read as the discussion while waiting
        assert seen["step_tag"] == RATES
        assert "first the invitee must fill in" in seen["step_guidance"]
        assert RATES not in meetings_db.load_step_answers(meeting.meeting_id, invitee_id)

    def test_saving_the_filled_table_starts_the_step(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, table_id = self._started(tmp_path, monkeypatch)
        meetings_db.save_table_responses(table_id, invitee_id, {0: {"Rate": "5"}, 1: {"Rate": "6"}})
        seen = self._stub(monkeypatch)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.button(key=f"invitee_table_save_{table_id}").click().run()
        assert not app.exception
        messages = meetings_db.list_messages(meeting.meeting_id, invitee_id)
        assert any(m.sender == SENDER_USER and m.text == f"I have filled in the {PRICES} table." for m in messages)
        assert "just filled in and saved" in seen["step_guidance"]
        assert f'Now discuss this agenda item: "{RATES}"' in seen["step_guidance"]
        assert "Bolt,5" in seen["step_guidance"]  # the bot sees what was filled in

    def test_a_filled_table_lets_the_step_go_on(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, _ = self._started(tmp_path, monkeypatch, rates=("5", "6"))
        seen = self._stub(monkeypatch)
        app = _invitee_app(probe, meeting.meeting_id, invitee_id).run()
        app.chat_input(key="invitee_chat_input").set_value("Rates are up because of steel").run()
        assert not app.exception
        assert seen["read"] == 1
        assert seen["step_tag"] == DELIVERY

    def test_the_show_document_appears_when_the_step_starts(self, tmp_path, monkeypatch):
        meeting, invitee_id, probe, _ = self._started(tmp_path, monkeypatch, with_doc=True)
        import app_pages.meeting_invitee as page

        files = meetings_db.list_files(meeting.meeting_id)
        turn = ChatTurnOutput(reply="Here is our rate card.", agenda_tag=RATES)
        assert page._shown_file_id(turn, files, meeting, []) == files[0]["file_id"]
        # Once shown for this step, it isn't shown again.
        shown = [ChatMessage(sender=SENDER_AI, agenda_tag=RATES, shown_file_id=files[0]["file_id"])]
        assert page._shown_file_id(turn, files, meeting, shown) is None


class TestOnlyItsStep:
    """The user's setup: About Us asks "Like to see video for company Intro?", and the video
    is the Company Video row's Show document — it must not play at About Us."""

    FILES = [{"file_id": 7, "filename": "Company Intro", "url": "https://youtu.be/abc"}]

    def _meeting(self) -> Meeting:
        return Meeting(
            agenda=[
                AgendaItem(item="About Us", item_type=QUESTION_ITEM, ai_note="Like to see video for company Intro?"),
                AgendaItem(item="Company Video", show_file="Company Intro"),
            ]
        )

    def test_the_note_naming_it_does_not_show_it(self):
        import app_pages.meeting_invitee as page

        turn = ChatTurnOutput(reply="Would you like to see it?", agenda_tag="About Us")
        assert page._shown_file_id(turn, self.FILES, self._meeting(), []) is None

    def test_the_bot_picking_it_does_not_show_it(self):
        import app_pages.meeting_invitee as page

        turn = ChatTurnOutput(reply="Here it is.", agenda_tag="About Us", show_file="Company Intro")
        assert page._shown_file_id(turn, self.FILES, self._meeting(), []) is None

    def test_its_own_step_shows_it(self):
        import app_pages.meeting_invitee as page

        turn = ChatTurnOutput(reply="Here is our video.", agenda_tag="Company Video")
        assert page._shown_file_id(turn, self.FILES, self._meeting(), []) == 7

    def test_the_bot_is_told_which_step_shows_it(self):
        text = show_files.files_block(self.FILES, self._meeting().agenda)
        assert 'the app shows it at "Company Video" only' in text
