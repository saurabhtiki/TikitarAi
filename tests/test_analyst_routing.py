"""Output-type and column-action routing (requirements 5.4, 6.2)."""

import pandas as pd
import pytest

from analyst import routing

SINGLE_VALUE = pd.DataFrame({"total": [42]})
MANY_ROWS = pd.DataFrame({"department": ["Sales", "Ops"], "total": [100, 200]})


class TestClassifyOutput:
    """Requirement 6.2's table, row by row."""

    @pytest.mark.parametrize(
        "question",
        ["Show a chart of sales", "Plot revenue by month", "Graph this", "Visualise the split", "Visualize it"],
    )
    def test_chart_keywords_ask_for_a_chart(self, question):
        assert routing.classify_output(question, MANY_ROWS) == {routing.OUTPUT_CHART}

    @pytest.mark.parametrize(
        "question",
        ["Give me a table of sales", "Return a dataframe", "List the employees", "Show me the data"],
    )
    def test_dataframe_keywords_ask_for_a_dataframe(self, question):
        assert routing.classify_output(question, MANY_ROWS) == {routing.OUTPUT_DATAFRAME}

    @pytest.mark.parametrize(
        "question",
        ["Why did sales drop", "Explain the variance", "Summary of payroll", "Summarise this", "Any insights"],
    )
    def test_commentary_keywords_ask_for_commentary(self, question):
        assert routing.classify_output(question, MANY_ROWS) == {routing.OUTPUT_COMMENTARY}

    @pytest.mark.parametrize("question", ["Give me all of it", "Show everything", "Full breakdown please"])
    def test_all_keywords_ask_for_all_three(self, question):
        assert routing.classify_output(question, MANY_ROWS) == {
            routing.OUTPUT_CHART,
            routing.OUTPUT_DATAFRAME,
            routing.OUTPUT_COMMENTARY,
        }

    def test_several_keywords_are_additive(self):
        assert routing.classify_output("Give me a table and a chart", MANY_ROWS) == {
            routing.OUTPUT_CHART,
            routing.OUTPUT_DATAFRAME,
        }

    def test_no_keyword_with_many_rows_defaults_to_dataframe_and_commentary(self):
        assert routing.classify_output("Total salary by department", MANY_ROWS) == {
            routing.OUTPUT_DATAFRAME,
            routing.OUTPUT_COMMENTARY,
        }

    def test_no_keyword_with_a_single_value_defaults_to_commentary(self):
        assert routing.classify_output("Total headcount", SINGLE_VALUE) == {routing.OUTPUT_COMMENTARY}

    def test_no_rows_at_all_defaults_to_commentary(self):
        assert routing.classify_output("Total headcount", None) == {routing.OUTPUT_COMMENTARY}
        assert routing.classify_output("Total headcount", MANY_ROWS.head(0)) == {routing.OUTPUT_COMMENTARY}

    @pytest.mark.parametrize("question", ["Are people listening to us", "Who chartered the boat"])
    def test_keywords_inside_longer_words_do_not_trigger(self, question):
        """'listen' is not 'list' and 'chartered' is not 'chart' — word boundaries matter,
        or half the questions a user asks would silently pick the wrong output."""
        assert routing.classify_output(question, MANY_ROWS) == {
            routing.OUTPUT_DATAFRAME,
            routing.OUTPUT_COMMENTARY,
        }


class TestIsSingleValue:
    def test_one_cell_is_a_single_value(self):
        assert routing.is_single_value(SINGLE_VALUE)

    def test_more_than_one_row_is_not(self):
        assert not routing.is_single_value(MANY_ROWS)

    def test_none_is_not(self):
        assert not routing.is_single_value(None)


class TestLooksLikeColumnAction:
    @pytest.mark.parametrize(
        "message",
        ["Add tax = 10% of basic", "Add a column called tax", "Calculate a net_salary column"],
    )
    def test_requirement_54s_add_phrasings(self, message):
        assert routing.looks_like_column_action(message) == routing.ACTION_ADD

    @pytest.mark.parametrize("message", ["Delete tax", "Remove the tax column", "Drop net_salary"])
    def test_requirement_54s_delete_phrasings(self, message):
        assert routing.looks_like_column_action(message) == routing.ACTION_DELETE

    @pytest.mark.parametrize(
        "message",
        [
            "Mark status as Over due if Due date is less than Today",
            "Set region to 'North' where state is MH",
            "Flag every invoice over 90 days as high risk",
        ],
    )
    def test_requirement_54s_update_phrasings(self, message):
        assert routing.looks_like_column_action(message) == routing.ACTION_UPDATE

    @pytest.mark.parametrize(
        "message",
        [
            "Which customers should we mark as overdue?",
            "What is the total salary",
            "Show me the top 10 by revenue",
            "How many invoices are overdue",
        ],
    )
    def test_a_question_is_never_a_column_change(self, message):
        """'Which customers should we mark as overdue?' asks to see rows, not to write
        them. Routing it to the column pipeline would silently edit the user's data."""
        assert routing.looks_like_column_action(message) is None

    def test_add_needs_a_column_or_an_assignment(self):
        """'Add up the totals' is arithmetic, not a schema change."""
        assert routing.looks_like_column_action("Add up the totals by region") is None

    def test_an_empty_message_is_not_an_action(self):
        assert routing.looks_like_column_action("   ") is None
