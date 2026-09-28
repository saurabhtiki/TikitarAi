"""Templates, the flow picture and plain-English drafting (phase 54).

The provider is stubbed at `drafting_agent.run_structured`.
"""

import pytest

from llm.client import LLMConnectionError
from meetings import drafting_agent, flow, steps, templates
from meetings.drafting_agent import DraftAgenda, DraftRow
from meetings.exceptions import MeetingAgentError
from meetings.model import (
    ANSWER_NUMBER,
    ANSWER_TEXT,
    ANSWER_YES_NO,
    DISCUSSION_ITEM,
    QUESTION_ITEM,
    AgendaItem,
)

PROFILE = {"profile_id": 1, "default_model": "test-model"}


def _question(title, answer_type=ANSWER_NUMBER, rule="", branch="", loop=""):
    return AgendaItem(item=title, item_type=QUESTION_ITEM, answer_type=answer_type, rule=rule, branch=branch, loop=loop)


class TestTemplates:
    def test_there_are_five(self):
        assert [template.name for template in templates.TEMPLATES] == [
            "HR interview",
            "Vendor purchase",
            "Sales quotation",
            "Project review",
            "AR review",
        ]

    @pytest.mark.parametrize("template", templates.TEMPLATES, ids=lambda template: template.name)
    def test_every_template_passes_the_agenda_check(self, template):
        assert steps.agenda_problems(template.agenda) == []
        assert template.meeting_context and template.persona and template.description

    def test_the_ar_review_repeats_for_each_invoice(self):
        agenda = templates.TEMPLATE_BY_NAME["AR review"].agenda
        assert {item.loop for item in agenda if item.is_question()} == {"Outstanding invoices"}


class TestFlow:
    def test_no_questions_means_no_picture(self):
        assert flow.flow_dot([AgendaItem(item="Delivery")]) == ""

    def test_questions_are_joined_in_order_to_the_end(self):
        dot = flow.flow_dot([_question("Salary"), _question("Notice")])
        assert "start -> q0;" in dot
        assert "q0 -> q1;" in dot
        assert "q1 -> end;" in dot
        assert '"1. Salary\\na number"' in dot

    def test_a_go_to_is_a_labelled_arrow(self):
        dot = flow.flow_dot(
            [_question("Salary", branch="if > 60000 go to End"), _question("Notice"), _question("Joining")]
        )
        assert 'q0 -> end [label="if > 60000"' in dot

    def test_a_jump_to_a_later_question_points_at_it(self):
        dot = flow.flow_dot(
            [_question("Complete", ANSWER_YES_NO, branch="if Yes go to Next steps"), _question("Why"), _question("Next steps")]
        )
        assert 'q0 -> q2 [label="if Yes"' in dot

    def test_discussion_items_come_after_the_questions(self):
        dot = flow.flow_dot([_question("Salary"), AgendaItem(item="Experience")])
        assert '"Discussion: Experience"' in dot
        assert "end -> talk;" in dot

    def test_a_for_each_list_is_a_box_with_a_next_row_arrow(self):
        dot = flow.flow_dot(templates.TEMPLATE_BY_NAME["AR review"].agenda)
        assert 'label="For each row of Outstanding invoices"' in dot
        assert 'q2 -> q0 [label="next row"' in dot
        assert 'q0 -> q0 [label="if Yes: next row"' in dot

    def test_a_go_to_that_cannot_be_read_is_red(self):
        dot = flow.flow_dot([_question("Salary", branch="whenever"), _question("Notice")])
        assert "q0_problem" in dot
        assert 'label="?"' in dot

    def test_quotes_in_titles_are_escaped(self):
        dot = flow.flow_dot([_question('The "big" one')])
        assert '\\"big\\"' in dot


class TestDrafting:
    def _stub(self, monkeypatch, rows=None, error=None):
        seen = {}

        def fake_run_structured(profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None):
            seen.update(prompt=prompt, instructions=instructions)
            if error:
                raise error
            return DraftAgenda(rows=rows or [])

        monkeypatch.setattr(drafting_agent, "run_structured", fake_run_structured)
        return seen

    def test_the_prompt_carries_the_description_and_the_rule_words(self, monkeypatch):
        seen = self._stub(monkeypatch, rows=[DraftRow(title="Salary", answer_type="Number")])
        drafting_agent.draft_agenda(PROFILE, "Ask salary; if above 1 lakh end.")
        assert "Ask salary; if above 1 lakh end." in seen["prompt"]
        assert "between 20000 and 60000" in seen["instructions"]
        assert "go to End" in seen["instructions"]

    def test_rows_become_agenda_items(self, monkeypatch):
        self._stub(
            monkeypatch,
            rows=[
                DraftRow(title=" Expected  salary ", answer_type="Number", rule="> 0", go_to="if > 100000 go to End",
                         tries=9),
                DraftRow(title="Experience", item_type="discussion", answer_type="Number", rule="> 0", go_to="x"),
                DraftRow(title="", answer_type="Text"),
            ],
        )
        items = drafting_agent.draft_agenda(PROFILE, "Ask salary.")
        assert [item.item for item in items] == ["Expected salary", "Experience"]
        assert items[0].answer_type == ANSWER_NUMBER
        assert items[0].branch == "if > 100000 go to End"
        assert items[0].max_tries == 5
        assert (items[1].item_type, items[1].rule, items[1].branch) == (DISCUSSION_ITEM, "", "")

    def test_odd_words_fall_back(self, monkeypatch):
        self._stub(monkeypatch, rows=[DraftRow(title="Salary", item_type="poll", answer_type="Money")])
        item = drafting_agent.draft_agenda(PROFILE, "Ask salary.")[0]
        assert item.item_type == DISCUSSION_ITEM

        self._stub(monkeypatch, rows=[DraftRow(title="Salary", answer_type="Money"), DraftRow(title="OK", answer_type="yes_no")])
        items = drafting_agent.draft_agenda(PROFILE, "Ask salary.")
        assert [item.answer_type for item in items] == [ANSWER_TEXT, ANSWER_YES_NO]

    def test_an_empty_description_is_refused_without_a_call(self, monkeypatch):
        seen = self._stub(monkeypatch)
        with pytest.raises(MeetingAgentError, match="Describe the meeting"):
            drafting_agent.draft_agenda(PROFILE, "   ")
        assert seen == {}

    def test_a_provider_failure_becomes_a_clear_message(self, monkeypatch):
        self._stub(monkeypatch, error=LLMConnectionError("timeout"))
        with pytest.raises(MeetingAgentError, match="Couldn't draft the agenda"):
            drafting_agent.draft_agenda(PROFILE, "Ask salary.")

    def test_no_rows_back_is_an_error(self, monkeypatch):
        self._stub(monkeypatch, rows=[])
        with pytest.raises(MeetingAgentError, match="didn't return any agenda rows"):
            drafting_agent.draft_agenda(PROFILE, "Ask salary.")
