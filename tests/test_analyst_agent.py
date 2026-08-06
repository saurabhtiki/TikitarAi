"""The analyst agent and the answering pipeline (requirements 5.4, 5.5, 6.2).

No network: the two seams every LLM call goes through — `llm.client.build_model` and
`llm.client.run_structured` — are monkeypatched, the same way
`tests/test_llm_suggestions.py` does it. The stub agent below runs *real* SQL through the
*real* guarded toolkit, so what these tests exercise is the wiring, not a mock of it.
"""

import pandas as pd
import pytest

from analyst import agent as analyst_agent
from analyst import column_intent, commentary, pipeline, routing
from analyst.exceptions import AgentRunError, AnalystError
from engine import duckdb_session as ds
from engine.relationships import Relationship
from llm.client import LLMConnectionError

PROFILE = {"profile_id": 1, "nickname": "Stub", "default_model": "stub-model", "provider_type": "local"}


@pytest.fixture
def connection():
    created = ds.open_connection()
    ds.register_table(
        created,
        "salaries",
        pd.DataFrame(
            {
                "emp": ["a", "b", "c"],
                "department": ["Sales", "Ops", "Sales"],
                "basic": [1000.0, 2000.0, 3000.0],
            }
        ),
    )
    yield created
    created.close()


@pytest.fixture
def linked_connection():
    """A second, cross-table fixture: salary (child) linked to employee_master (parent)
    via a confirmed relationship, the shape requirement 5.4's cross-table add needs."""
    created = ds.open_connection()
    ds.register_table(
        created,
        "employee_master",
        pd.DataFrame({"employee_id": ["e1", "e2"], "department": ["HR", "Sales"]}),
    )
    ds.register_table(
        created,
        "salary",
        pd.DataFrame({"employee_id": ["e1", "e2"], "basic_salary": [1000.0, 2000.0]}),
    )
    yield created
    created.close()


LINK = Relationship(
    child_table="salary", child_column="employee_id", parent_table="employee_master", parent_column="employee_id"
)


class StubResponse:
    def __init__(self, content: str):
        self.content = content


class StubAgent:
    """Stands in for `agno.agent.Agent`, driving the real toolkit it was handed.

    `queries` is what the "model" decides to run; `reply` is its prose. Everything after
    the tool call — the guards, the dataframe capture — is the real code path.
    """

    queries: list[str] = []
    reply: str = "Sales leads on total basic pay."
    raises: Exception | None = None

    def __init__(self, **kwargs):
        self.toolkit = kwargs["tools"][0]
        self.kwargs = kwargs

    def run(self, question: str):
        if StubAgent.raises is not None:
            raise StubAgent.raises
        for query in StubAgent.queries:
            self.toolkit.run_query(query)
        return StubResponse(StubAgent.reply)


@pytest.fixture(autouse=True)
def stub_model(monkeypatch):
    """Replaces the model builder and the Agent class for every test in this module."""
    StubAgent.queries = [
        "SELECT department, sum(basic) AS total FROM salaries GROUP BY department ORDER BY total DESC"
    ]
    StubAgent.reply = "Sales leads on total basic pay."
    StubAgent.raises = None
    monkeypatch.setattr(analyst_agent, "build_model", lambda profile, **kwargs: object())
    monkeypatch.setattr(analyst_agent, "Agent", StubAgent)


@pytest.fixture
def stub_commentary(monkeypatch):
    """Requirement 6.2's separate narrow call, stubbed to a fixed sentence."""
    monkeypatch.setattr(
        commentary,
        "run_structured",
        lambda *args, **kwargs: commentary.Commentary(summary="Sales is 4000 of the 6000 total."),
    )


class TestAnswerQuestion:
    def test_the_sql_and_the_rows_both_land_on_the_result(self, connection):
        result = analyst_agent.answer_question(PROFILE, connection, "schema", "total basic by department")
        assert result.sql.startswith("SELECT department")
        assert result.frame["total"].tolist() == [4000.0, 2000.0]
        assert result.narrative == "Sales leads on total basic pay."

    def test_the_schema_context_reaches_the_agent(self, connection):
        """Requirement 5.5: the schema context — names, relationships and descriptions —
        goes with every query."""
        analyst_agent.answer_question(PROFILE, connection, "Tables and columns: …", "anything")
        # The stub keeps its constructor kwargs; this is the only way to see what was sent.
        assert StubAgent.queries  # sanity: the stub did run
        built, _ = analyst_agent.build_analyst(PROFILE, connection, "Tables and columns: …")
        assert built.kwargs["additional_context"] == "Tables and columns: …"

    def test_the_tool_call_limit_is_set(self, connection):
        built, _ = analyst_agent.build_analyst(PROFILE, connection, "schema")
        assert built.kwargs["tool_call_limit"] == analyst_agent.MAX_TOOL_CALLS

    def test_the_last_successful_query_is_the_answer(self, connection):
        StubAgent.queries = [
            "SELECT nosuchcolumn FROM salaries",
            "SELECT emp FROM salaries ORDER BY emp",
        ]
        result = analyst_agent.answer_question(PROFILE, connection, "schema", "list employees")
        assert result.frame["emp"].tolist() == ["a", "b", "c"]

    def test_a_run_where_every_query_failed_raises_with_the_sql_error(self, connection):
        StubAgent.queries = ["SELECT nosuchcolumn FROM salaries"]
        with pytest.raises(AgentRunError, match="(?i)nosuchcolumn"):
            analyst_agent.answer_question(PROFILE, connection, "schema", "something")

    def test_a_provider_failure_becomes_a_readable_error_not_a_traceback(self, connection):
        StubAgent.raises = RuntimeError("Connection error.\nstack\nnoise")
        with pytest.raises(AgentRunError, match="Connection error"):
            analyst_agent.answer_question(PROFILE, connection, "schema", "something")

    def test_an_answer_with_no_query_is_returned_but_flagged(self, connection):
        StubAgent.queries = []
        StubAgent.reply = "There's no 'revenue' column in your data."
        result = analyst_agent.answer_question(PROFILE, connection, "schema", "total revenue")
        assert result.frame is None
        assert result.warnings

    def test_no_query_and_no_reply_raises(self, connection):
        StubAgent.queries = []
        StubAgent.reply = ""
        with pytest.raises(AgentRunError):
            analyst_agent.answer_question(PROFILE, connection, "schema", "something")

    def test_an_empty_question_raises(self, connection):
        with pytest.raises(AgentRunError, match="Ask a question"):
            analyst_agent.answer_question(PROFILE, connection, "schema", "   ")

    def test_a_model_that_cannot_be_built_raises(self, connection, monkeypatch):
        def refuse(profile, **kwargs):
            raise LLMConnectionError("This connection has no model name set.")

        monkeypatch.setattr(analyst_agent, "build_model", refuse)
        with pytest.raises(AgentRunError, match="no model name"):
            analyst_agent.answer_question(PROFILE, connection, "schema", "something")


class TestPipelineQuestions:
    def test_a_chart_question_produces_a_chart_from_the_same_frame(self, connection):
        answer = pipeline.answer(PROFILE, connection, "schema", "chart total basic by department")
        assert routing.OUTPUT_CHART in answer.outputs
        assert answer.figure is not None
        assert answer.frame["total"].tolist() == [4000.0, 2000.0]

    def test_a_commentary_question_calls_the_separate_narrow_call(self, connection, stub_commentary):
        answer = pipeline.answer(PROFILE, connection, "schema", "explain total basic by department")
        assert answer.text == "Sales is 4000 of the 6000 total."
        assert answer.outputs == {routing.OUTPUT_COMMENTARY}

    def test_a_default_question_gives_a_table_and_commentary(self, connection, stub_commentary):
        answer = pipeline.answer(PROFILE, connection, "schema", "total basic by department")
        assert answer.outputs == {routing.OUTPUT_DATAFRAME, routing.OUTPUT_COMMENTARY}
        assert answer.frame is not None
        assert answer.text

    def test_a_failed_commentary_call_still_leaves_the_table(self, connection, monkeypatch):
        """Commentary is one of three outputs and must not be able to cost the other two."""
        def refuse(*args, **kwargs):
            raise LLMConnectionError("Rate limited.")

        monkeypatch.setattr(commentary, "run_structured", refuse)
        answer = pipeline.answer(PROFILE, connection, "schema", "explain total basic by department")
        assert answer.frame is not None
        assert not answer.is_error
        assert any("Rate limited" in warning for warning in answer.warnings)

    def test_an_unchartable_result_falls_back_to_the_table(self, connection):
        StubAgent.queries = ["SELECT count(*) AS n FROM salaries"]
        answer = pipeline.answer(PROFILE, connection, "schema", "chart the headcount")
        assert answer.figure is None
        assert routing.OUTPUT_DATAFRAME in answer.outputs
        assert answer.warnings

    def test_a_failure_becomes_an_error_answer_not_an_exception(self, connection):
        StubAgent.raises = RuntimeError("Connection error.")
        answer = pipeline.answer(PROFILE, connection, "schema", "total basic by department")
        assert answer.is_error
        assert "Connection error" in answer.text

    def test_an_empty_question_is_an_error_answer(self, connection):
        assert pipeline.answer(PROFILE, connection, "schema", "  ").is_error

    def test_the_assistant_always_has_something_to_say(self, connection, stub_commentary):
        """An assistant turn with no text at all reads as a broken chat."""
        answer = pipeline.answer(PROFILE, connection, "schema", "chart total basic by department")
        assert answer.text


class TestPipelineColumnChanges:
    """Requirement 5.4's conversational door onto the same engine functions."""

    def stub_action(self, monkeypatch, **fields):
        action = column_intent.ColumnAction(**fields)
        monkeypatch.setattr(column_intent, "run_structured", lambda *args, **kwargs: action)

    def test_add_runs_the_engines_own_ddl_and_shows_it(self, connection, monkeypatch):
        self.stub_action(
            monkeypatch, action="add", table="salaries", column="tax", expression="basic * 0.10"
        )
        answer = pipeline.answer(PROFILE, connection, "schema", "Add tax = 10% of basic")
        assert not answer.is_error
        assert ds.preview(connection, "salaries")["tax"].tolist() == [100.0, 200.0, 300.0]
        assert "ALTER TABLE" in answer.sql
        assert len(answer.statements) == 2

    def test_delete_removes_the_column(self, connection, monkeypatch):
        self.stub_action(monkeypatch, action="delete", table="salaries", column="department")
        answer = pipeline.answer(PROFILE, connection, "schema", "Delete department")
        assert not answer.is_error
        assert "department" not in [name for name, _ in ds.describe_table(connection, "salaries")]

    def test_update_changes_only_matching_rows_and_reports_how_many(self, connection, monkeypatch):
        self.stub_action(
            monkeypatch,
            action="update",
            table="salaries",
            column="department",
            expression="'Senior'",
            condition="basic > 1500",
        )
        answer = pipeline.answer(
            PROFILE, connection, "schema", "Mark department as Senior if basic is over 1500"
        )
        assert ds.preview(connection, "salaries")["department"].tolist() == ["Sales", "Senior", "Senior"]
        assert "2 row(s)" in answer.text

    def test_a_bad_formula_is_reported_and_changes_nothing(self, connection, monkeypatch):
        self.stub_action(monkeypatch, action="add", table="salaries", column="tax", expression="bsic * 0.1")
        answer = pipeline.answer(PROFILE, connection, "schema", "Add tax = 10% of bsic")
        assert answer.is_error
        assert "tax" not in [name for name, _ in ds.describe_table(connection, "salaries")]

    def test_an_unclear_request_explains_itself_rather_than_guessing(self, connection, monkeypatch):
        self.stub_action(monkeypatch, action="none", explanation="You didn't say which table.")
        answer = pipeline.answer(PROFILE, connection, "schema", "Add a column")
        assert answer.is_error
        assert "which table" in answer.text

    def test_a_model_written_statement_cannot_bypass_the_guards(self, connection, monkeypatch):
        """The model fills in parameters; it never writes SQL. A smuggled statement in a
        parameter still meets `assert_safe_expression` on the way to DuckDB."""
        self.stub_action(
            monkeypatch,
            action="add",
            table="salaries",
            column="x",
            expression="1); DROP TABLE salaries; --",
        )
        answer = pipeline.answer(PROFILE, connection, "schema", "Add x = 1")
        assert answer.is_error
        assert "salaries" in ds.list_tables(connection)

    def test_a_parse_failure_becomes_an_error_answer(self, connection, monkeypatch):
        def refuse(*args, **kwargs):
            raise LLMConnectionError("Unreachable host.")

        monkeypatch.setattr(column_intent, "run_structured", refuse)
        answer = pipeline.answer(PROFILE, connection, "schema", "Delete tax")
        assert answer.is_error
        assert "Unreachable host" in answer.text

    def test_the_routing_hint_reaches_the_parser(self, connection, monkeypatch):
        captured = {}

        def capture(profile, prompt, schema, **kwargs):
            captured["prompt"] = prompt
            return column_intent.ColumnAction(action="delete", table="salaries", column="department")

        monkeypatch.setattr(column_intent, "run_structured", capture)
        pipeline.answer(PROFILE, connection, "schema", "Delete department")
        assert "'delete' request" in captured["prompt"]


class TestPipelineCrossTableAdd:
    """The case that motivated this: a formula that needs a column living on a table the
    model reached across a confirmed relationship to get to."""

    def stub_action(self, monkeypatch, **fields):
        action = column_intent.ColumnAction(action="add", **fields)
        monkeypatch.setattr(column_intent, "run_structured", lambda *args, **kwargs: action)

    def test_a_formula_can_read_the_parent_table(self, linked_connection, monkeypatch):
        self.stub_action(
            monkeypatch,
            table="salary",
            column="bonus",
            expression=(
                "CASE WHEN employee_master.department = 'HR' THEN salary.basic_salary * 0.10 "
                "ELSE salary.basic_salary * 0.01 END"
            ),
            related_table="employee_master",
        )
        answer = pipeline.answer(
            PROFILE, linked_connection, "schema", "Add bonus = 10% if HR else 1%", relationships=[LINK]
        )
        assert not answer.is_error
        result = ds.preview(linked_connection, "salary").sort_values("employee_id")
        assert result["bonus"].tolist() == [100.0, 20.0]

    def test_without_the_relationship_the_request_is_refused(self, linked_connection, monkeypatch):
        """No `relationships` passed — same as an unconfirmed link — refuses cleanly
        rather than the model's join guess reaching DuckDB."""
        self.stub_action(
            monkeypatch,
            table="salary",
            column="bonus",
            expression="employee_master.department",
            related_table="employee_master",
        )
        answer = pipeline.answer(PROFILE, linked_connection, "schema", "Add a bonus column from department")
        assert answer.is_error
        assert "aren't linked" in answer.text
        assert "bonus" not in [name for name, _ in ds.describe_table(linked_connection, "salary")]


class TestColumnIntentDirectly:
    def test_an_unknown_action_is_refused(self, connection):
        action = column_intent.ColumnAction(action="none", table="salaries", column="tax")
        with pytest.raises(AnalystError):
            column_intent.apply_action(connection, action)

    def test_a_missing_table_is_refused_before_any_engine_call(self, connection):
        action = column_intent.ColumnAction(action="add", table="", column="tax", expression="1")
        with pytest.raises(AnalystError, match="(?i)which table"):
            column_intent.apply_action(connection, action)

    def test_a_cross_table_add_reaches_the_join(self, linked_connection):
        action = column_intent.ColumnAction(
            action="add",
            table="salary",
            column="bonus",
            expression="employee_master.department",
            related_table="employee_master",
        )
        column_intent.apply_action(linked_connection, action, [LINK])
        assert "bonus" in [name for name, _ in ds.describe_table(linked_connection, "salary")]

    def test_the_reverse_direction_is_refused_as_needing_an_aggregate(self, linked_connection):
        action = column_intent.ColumnAction(
            action="add",
            table="employee_master",
            column="total",
            expression="salary.basic_salary",
            related_table="salary",
        )
        with pytest.raises(AnalystError, match="combine"):
            column_intent.apply_action(linked_connection, action, [LINK])
