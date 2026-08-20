"""A rule in plain language becomes SQL that runs and answers it (requirement 6.5).

The provider is stubbed the way `tests/test_analyst_agent.py` stubs a model — by replacing
`run_structured`, the one seam every LLM call in this package goes through — so the whole
generate → guard → execute → validate path is exercised against a real in-memory DuckDB and
no network.
"""

import duckdb
import pytest

from checks import sql_builder
from checks.exceptions import CheckSqlError
from checks.model import Check
from checks.sql_builder import GeneratedSql, build_prompt, generate_and_run, run_check, validate_result

SCHEMA = "Table salary: employee (VARCHAR), department (VARCHAR), basic (DOUBLE), bonus (DOUBLE)"

GOOD_SQL = (
    "SELECT employee, department, bonus / basic * 100 AS criteria_result, "
    "CASE WHEN bonus / basic * 100 <= 5 THEN 'Yes' ELSE 'No' END AS criteria_met FROM salary"
)


@pytest.fixture
def connection():
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE salary (employee VARCHAR, department VARCHAR, basic DOUBLE, bonus DOUBLE)")
    connection.execute("INSERT INTO salary VALUES ('Ana', 'HR', 1000, 40), ('Bo', 'HR', 1000, 120)")
    yield connection
    connection.close()


@pytest.fixture
def check():
    return Check(name="Bonus cap", criteria_text="Bonus must be at most 5% of basic.")


def _stub(monkeypatch, *replies):
    """Replaces the one LLM seam with a queue of canned answers, recording every prompt."""
    prompts: list[str] = []
    queue = list(replies)

    def fake_run_structured(
        profile, prompt, output_schema, *, instructions=None, text_field=None, key_path=None
    ):
        prompts.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr(sql_builder, "run_structured", fake_run_structured)
    return prompts


class TestPrompt:
    def test_the_persona_the_rule_and_the_schema_all_reach_the_model(self, check):
        prompt = build_prompt("You are a finance controller.", check, SCHEMA)
        assert "finance controller" in prompt
        assert "at most 5% of basic" in prompt
        assert "Table salary" in prompt

    def test_hints_are_labelled_as_hints(self, check):
        check.hint_tables = ["salary"]
        check.hint_columns = ["salary.bonus"]
        prompt = build_prompt("", check, SCHEMA)
        assert "hint only" in prompt
        assert "salary.bonus" in prompt

    def test_a_refine_carries_the_previous_statement_forward(self, check):
        """Step 4 asks for a refine, not a retry — the model is told to change the least
        it can rather than rewrite a query the user has already tuned."""
        prompt = build_prompt("", check, SCHEMA, previous_sql=GOOD_SQL, previous_error="missing criteria_met")
        assert GOOD_SQL in prompt
        assert "as little of it as possible" in prompt
        assert "missing criteria_met" in prompt


class TestValidation:
    def test_a_result_missing_the_verdict_column_is_refused(self):
        frame = duckdb.sql("SELECT 'Ana' AS employee, 1 AS criteria_result").df()
        with pytest.raises(CheckSqlError, match="criteria_met"):
            validate_result(frame)

    def test_a_result_missing_the_value_column_is_refused(self):
        frame = duckdb.sql("SELECT 'Ana' AS employee, 'Yes' AS criteria_met").df()
        with pytest.raises(CheckSqlError, match="criteria_result"):
            validate_result(frame)

    def test_a_boolean_verdict_is_refused(self):
        """A boolean reads fine to a model and breaks every filter, count and draft
        downstream — so it is caught here rather than becoming a silently empty failure list."""
        frame = duckdb.sql("SELECT 'Ana' AS employee, 1 AS criteria_result, true AS criteria_met").df()
        with pytest.raises(CheckSqlError, match="only 'Yes' or 'No'"):
            validate_result(frame)

    def test_a_result_nobody_can_be_held_to_is_refused(self):
        """The addition to addtion.md's contract: a failure with no identifying column
        cannot be actioned, so it is not a usable answer."""
        frame = duckdb.sql("SELECT 1 AS criteria_result, 'No' AS criteria_met").df()
        with pytest.raises(CheckSqlError, match="identify"):
            validate_result(frame)

    def test_lowercase_verdicts_are_accepted(self):
        frame = duckdb.sql("SELECT 'Ana' AS employee, 1 AS criteria_result, 'no' AS criteria_met").df()
        validate_result(frame)

    def test_no_rows_is_a_legitimate_answer(self):
        """"No employee is in scope this month" is an answer, and refusing it would send
        the model chasing a problem that isn't there."""
        frame = duckdb.sql("SELECT 'x' AS employee, 1 AS criteria_result, 'No' AS criteria_met WHERE false").df()
        validate_result(frame)


class TestRunCheck:
    def test_a_good_statement_returns_its_rows(self, connection):
        frame = run_check(connection, GOOD_SQL)
        assert len(frame) == 2
        assert set(frame["criteria_met"]) == {"Yes", "No"}

    def test_a_destructive_statement_is_refused_by_the_guard(self, connection):
        with pytest.raises(CheckSqlError, match="could not run"):
            run_check(connection, "DROP TABLE salary")
        assert connection.execute("SELECT count(*) FROM salary").fetchone()[0] == 2

    def test_reading_the_filesystem_is_refused(self, connection):
        with pytest.raises(CheckSqlError, match="could not run"):
            run_check(connection, "SELECT * FROM read_csv('secrets.csv')")

    def test_a_query_against_a_missing_column_reports_duckdbs_own_reason(self, connection):
        with pytest.raises(CheckSqlError, match="could not run"):
            run_check(connection, "SELECT nope AS criteria_result FROM salary")

    def test_empty_sql_says_so(self, connection):
        with pytest.raises(CheckSqlError, match="generate it first"):
            run_check(connection, "   ")


class TestGenerateAndRun:
    def test_a_good_first_attempt_needs_only_one_call(self, monkeypatch, connection, check):
        prompts = _stub(monkeypatch, GeneratedSql(sql=GOOD_SQL))
        sql, frame, _ = generate_and_run({}, "", check, SCHEMA, connection)
        assert sql == GOOD_SQL
        assert len(frame) == 2
        assert len(prompts) == 1

    def test_a_missing_alias_is_repaired_automatically(self, monkeypatch, connection, check):
        """One retry, because models fix their own missing alias reliably and making the
        user drive it turns a two-second correction into a manual refine cycle."""
        broken = "SELECT employee, bonus AS criteria_result FROM salary"
        prompts = _stub(monkeypatch, GeneratedSql(sql=broken), GeneratedSql(sql=GOOD_SQL))

        sql, frame, _ = generate_and_run({}, "", check, SCHEMA, connection)

        assert sql == GOOD_SQL
        assert len(prompts) == 2
        assert "criteria_met" in prompts[1]
        assert broken in prompts[1]

    def test_a_second_failure_reaches_the_user_carrying_the_reason(self, monkeypatch, connection, check):
        broken = "SELECT employee, bonus AS criteria_result FROM salary"
        _stub(monkeypatch, GeneratedSql(sql=broken), GeneratedSql(sql=broken))
        with pytest.raises(CheckSqlError, match="criteria_met"):
            generate_and_run({}, "", check, SCHEMA, connection)

    def test_markdown_fencing_is_stripped(self, monkeypatch, connection, check):
        """Models fence SQL unbidden, and a fenced statement would be reported to the user
        as if their rule were at fault."""
        _stub(monkeypatch, GeneratedSql(sql=f"```sql\n{GOOD_SQL};\n```"))
        sql, _, _ = generate_and_run({}, "", check, SCHEMA, connection)
        assert sql == GOOD_SQL

    def test_a_bare_statement_reply_is_asked_to_be_recovered(self, monkeypatch, connection, check):
        """Reasoning models answer "write me one SELECT" with the SELECT and no envelope.

        The recovery itself is `llm.client`'s; what matters here is that this call opts into
        it, because the statement it produces still has to clear the guard and the contract.
        """
        captured = {}

        def fake_run_structured(profile, prompt, output_schema, **kwargs):
            captured.update(kwargs)
            return GeneratedSql(sql=GOOD_SQL)

        monkeypatch.setattr(sql_builder, "run_structured", fake_run_structured)
        generate_and_run({}, "", check, SCHEMA, connection)
        assert captured["text_field"] == "sql"

    def test_an_empty_rule_is_refused_before_any_call(self, monkeypatch, connection):
        prompts = _stub(monkeypatch, GeneratedSql(sql=GOOD_SQL))
        with pytest.raises(CheckSqlError, match="plain language"):
            generate_and_run({}, "", Check(name="Empty"), SCHEMA, connection)
        assert prompts == []
