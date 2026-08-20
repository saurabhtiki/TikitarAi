"""A report item in plain language becomes SQL that runs and answers it (requirement 7.3).

The provider is stubbed the way `tests/test_checks_sql_builder.py` stubs it — by replacing
`run_structured`, the one seam every LLM call in this package goes through — so the whole
generate → guard → execute → validate path is exercised against a real in-memory DuckDB and
no network.
"""

import duckdb
import pytest

from report_items import sql_builder
from report_items.exceptions import ReportItemSqlError
from report_items.model import ReportItem
from report_items.sql_builder import (
    GeneratedSql,
    build_prompt,
    generate_and_run,
    run_item,
    validate_result,
)

SCHEMA = "Table salary: employee (VARCHAR), department (VARCHAR), basic (DOUBLE), bonus (DOUBLE)"

GOOD_SQL = "SELECT department, count(*) AS people FROM salary GROUP BY department"


@pytest.fixture
def connection():
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE salary (employee VARCHAR, department VARCHAR, basic DOUBLE, bonus DOUBLE)")
    connection.execute("INSERT INTO salary VALUES ('Ana', 'HR', 1000, 40), ('Bo', 'Ops', 1000, 120)")
    yield connection
    connection.close()


@pytest.fixture
def item():
    return ReportItem(heading="Headcount", request="How many people are in each department?")


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
    def test_the_persona_the_heading_the_request_and_the_schema_all_reach_the_model(self, item):
        prompt = build_prompt("You are a finance controller.", item, SCHEMA)

        assert "finance controller" in prompt
        assert "Headcount" in prompt
        assert "people are in each department" in prompt
        assert "Table salary" in prompt

    def test_hints_are_labelled_as_hints(self, item):
        item.hint_tables = ["salary"]
        item.hint_columns = ["salary.department"]

        prompt = build_prompt("", item, SCHEMA)

        assert "hint only" in prompt
        assert "salary.department" in prompt

    def test_a_refine_carries_the_previous_statement_forward(self, item):
        """A refine, not a retry — otherwise an item the user has already tuned comes back
        with different column names, which in a report means a heading that silently changed
        between one month and the next."""
        prompt = build_prompt("", item, SCHEMA, previous_sql=GOOD_SQL, previous_error="no such column")

        assert GOOD_SQL in prompt
        assert "as little of it as possible" in prompt
        assert "no such column" in prompt

    def test_an_empty_request_is_refused_before_the_provider_is_called(self, monkeypatch):
        prompts = _stub(monkeypatch, GeneratedSql(sql=GOOD_SQL))

        with pytest.raises(ReportItemSqlError, match="should show"):
            sql_builder.generate_sql({}, "", ReportItem(heading="Empty"), SCHEMA)

        assert prompts == []


class TestNoColumnContract:
    """The one deliberate difference from `checks/`: a report item answers whatever was
    asked, so demanding a verdict column would invent a question the user didn't ask."""

    def test_a_single_cell_result_is_accepted(self, connection):
        frame = run_item(connection, "SELECT sum(basic) AS total_pay FROM salary")

        assert list(frame.columns) == ["total_pay"]
        assert frame.loc[0, "total_pay"] == 2000

    def test_a_result_with_no_verdict_column_is_accepted(self, connection):
        frame = run_item(connection, GOOD_SQL)

        assert sorted(frame["department"]) == ["HR", "Ops"]

    def test_an_empty_result_is_not_an_error(self, connection):
        """"No invoice was overdue this month" is a legitimate answer, and refusing it would
        send the model chasing a problem that isn't there."""
        frame = run_item(connection, "SELECT employee FROM salary WHERE basic > 100000")

        assert frame.empty
        assert list(frame.columns) == ["employee"]

    def test_a_result_with_no_columns_at_all_is_refused(self):
        empty = duckdb.sql("SELECT 1").df().drop(columns=["1"])

        with pytest.raises(ReportItemSqlError, match="no columns"):
            validate_result(empty)


class TestRunning:
    def test_a_delete_is_refused_and_the_rows_survive(self, connection):
        """`engine.guards` blocks what could escape the session, but a DELETE is none of
        those and passes it — so a report item's read-only rule is checked explicitly."""
        with pytest.raises(ReportItemSqlError, match="only read data"):
            run_item(connection, "DELETE FROM salary")

        assert connection.execute("SELECT count(*) FROM salary").fetchone()[0] == 2

    def test_an_update_is_refused(self, connection):
        with pytest.raises(ReportItemSqlError, match="column step instead"):
            run_item(connection, "UPDATE salary SET basic = 0")

        assert connection.execute("SELECT min(basic) FROM salary").fetchone()[0] == 1000

    def test_a_write_hidden_behind_a_comment_is_still_refused(self, connection):
        with pytest.raises(ReportItemSqlError, match="only read data"):
            run_item(connection, "-- a report item\nDELETE FROM salary")

        assert connection.execute("SELECT count(*) FROM salary").fetchone()[0] == 2

    def test_a_with_clause_is_allowed(self, connection):
        frame = run_item(
            connection,
            "WITH counted AS (SELECT department, count(*) AS people FROM salary GROUP BY department) "
            "SELECT * FROM counted",
        )

        assert len(frame) == 2

    def test_a_leading_comment_before_a_select_is_allowed(self, connection):
        frame = run_item(connection, "/* headcount */ SELECT count(*) AS people FROM salary")

        assert frame.loc[0, "people"] == 2

    def test_a_drop_is_still_refused_by_the_engine_guard(self, connection):
        with pytest.raises(ReportItemSqlError):
            run_item(connection, "DROP TABLE salary")

    def test_a_broken_statement_reports_duckdbs_own_wording(self, connection):
        with pytest.raises(ReportItemSqlError, match="could not run"):
            run_item(connection, "SELECT no_such_column FROM salary")

    def test_running_nothing_says_to_generate_it_first(self, connection):
        with pytest.raises(ReportItemSqlError, match="generate it first"):
            run_item(connection, "   ")

    def test_markdown_fencing_is_stripped(self, monkeypatch, connection, item):
        fenced = f"```sql\n{GOOD_SQL}\n```"
        _stub(monkeypatch, GeneratedSql(sql=fenced))

        sql, frame = generate_and_run({}, "", item, SCHEMA, connection)

        assert sql == GOOD_SQL
        assert len(frame) == 2

    def test_a_trailing_semicolon_is_stripped(self, monkeypatch, connection, item):
        """Left on, `assert_safe_sql` sees two statements and blames the user's request."""
        _stub(monkeypatch, GeneratedSql(sql=f"{GOOD_SQL};"))

        sql, _ = generate_and_run({}, "", item, SCHEMA, connection)

        assert sql == GOOD_SQL


class TestRepair:
    def test_a_first_failure_is_repaired_automatically(self, monkeypatch, connection, item):
        prompts = _stub(
            monkeypatch,
            GeneratedSql(sql="SELECT no_such_column FROM salary"),
            GeneratedSql(sql=GOOD_SQL),
        )

        sql, frame = generate_and_run({}, "", item, SCHEMA, connection)

        assert sql == GOOD_SQL
        assert len(frame) == 2
        # The repair prompt carries the failed statement and the reason it failed.
        assert "no_such_column" in prompts[1]
        assert "could not run" in prompts[1]

    def test_a_second_failure_becomes_the_users_with_the_reason_attached(
        self, monkeypatch, connection, item
    ):
        _stub(
            monkeypatch,
            GeneratedSql(sql="SELECT no_such_column FROM salary"),
            GeneratedSql(sql="SELECT still_wrong FROM salary"),
        )

        with pytest.raises(ReportItemSqlError, match="could not run"):
            generate_and_run({}, "", item, SCHEMA, connection)

    def test_only_one_repair_is_attempted(self, monkeypatch, connection, item):
        prompts = _stub(
            monkeypatch,
            GeneratedSql(sql="SELECT bad_one FROM salary"),
            GeneratedSql(sql="SELECT bad_two FROM salary"),
        )

        with pytest.raises(ReportItemSqlError):
            generate_and_run({}, "", item, SCHEMA, connection)

        assert len(prompts) == 2

    def test_a_stored_error_is_fed_into_the_next_generation(self, monkeypatch, connection, item):
        item.sql = "SELECT no_such_column FROM salary"
        item.last_error = "That query could not run: no such column"
        prompts = _stub(monkeypatch, GeneratedSql(sql=GOOD_SQL))

        generate_and_run({}, "", item, SCHEMA, connection)

        assert "no such column" in prompts[0]
        assert "no_such_column" in prompts[0]


class TestNothingIsMutated:
    def test_the_item_is_left_untouched_for_the_caller_to_update(
        self, monkeypatch, connection, item
    ):
        item.sql = "SELECT 1"
        _stub(monkeypatch, GeneratedSql(sql=GOOD_SQL))

        generate_and_run({}, "", item, SCHEMA, connection)

        assert item.sql == "SELECT 1"
        assert item.saved_run is None
