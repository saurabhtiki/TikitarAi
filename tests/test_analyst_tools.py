"""The agent's guarded DuckDB toolkit (requirement 5.5).

These are the tests that matter most in this stage: they are what stands between a model's
SQL and the filesystem. Upstream `DuckDbTools.run_query` passes its string straight to
DuckDB, so every case here would succeed against the unsubclassed toolkit.
"""

import pandas as pd
import pytest

from analyst.tools import READ_ONLY_TOOLS, MODEL_PREVIEW_ROWS, SessionDuckDbTools, render_for_model
from engine import duckdb_session as ds


@pytest.fixture
def toolkit():
    connection = ds.open_connection()
    ds.register_table(
        connection,
        "salaries",
        pd.DataFrame({"emp": ["a", "b", "c"], "basic": [1000.0, 2000.0, 3000.0]}),
    )
    yield SessionDuckDbTools(connection)
    connection.close()


class TestGuardrails:
    def test_reading_the_filesystem_is_refused(self, toolkit):
        result = toolkit.run_query("SELECT * FROM read_csv('C:/Windows/win.ini')")
        assert "could not run" in result
        assert toolkit.executions == []

    @pytest.mark.parametrize(
        "sql",
        [
            "ATTACH 'other.db' AS other",
            "INSTALL httpfs",
            "COPY salaries TO 'out.csv'",
            "PRAGMA database_list",
        ],
    )
    def test_escaping_the_session_is_refused(self, toolkit, sql):
        assert "could not run" in toolkit.run_query(sql)

    def test_dropping_a_table_outside_the_session_is_refused(self, toolkit):
        assert "could not run" in toolkit.run_query("DROP TABLE users")

    def test_dropping_even_a_session_table_is_refused(self, toolkit):
        """The agent's tool is read-only. Column changes go through `engine/columns.py`,
        which probes and wraps in a transaction — not through model-written DDL."""
        assert "could not run" in toolkit.run_query("DROP TABLE salaries")
        assert "salaries" in ds.list_tables(toolkit.connection)

    def test_multiple_statements_are_refused_rather_than_silently_truncated(self, toolkit):
        """Upstream splits on ';' and runs only the first fragment, so the second would be
        discarded without anyone being told. Refusing is the honest behaviour."""
        assert "could not run" in toolkit.run_query("SELECT 1; DROP TABLE salaries")


class TestOrdinaryQueries:
    def test_an_analytics_query_runs(self, toolkit):
        assert "3000.0" in toolkit.run_query("SELECT emp, basic FROM salaries ORDER BY basic DESC")

    def test_the_dataframe_is_captured_not_just_the_text(self, toolkit):
        """Requirement 5.5 step 3 needs the result set as a dataframe. The model's prose
        rendering of a result is not a result."""
        toolkit.run_query("SELECT emp, basic FROM salaries ORDER BY emp")
        sql, frame = toolkit.last_result
        assert sql.startswith("SELECT")
        assert isinstance(frame, pd.DataFrame)
        assert frame["basic"].tolist() == [1000.0, 2000.0, 3000.0]

    def test_every_query_is_recorded_in_order(self, toolkit):
        toolkit.run_query("SELECT count(*) AS n FROM salaries")
        toolkit.run_query("SELECT emp FROM salaries")
        assert len(toolkit.executions) == 2
        assert toolkit.last_result[0] == "SELECT emp FROM salaries"

    def test_backticks_are_stripped_before_execution(self, toolkit):
        assert "could not run" not in toolkit.run_query("SELECT `emp` FROM salaries")

    def test_no_result_yet_means_no_last_result(self, toolkit):
        assert toolkit.last_result is None


class TestSelfCorrection:
    def test_bad_sql_comes_back_as_text_not_an_exception(self, toolkit):
        """Raising would end the run on the first typo. Returning the error is how the
        agent learns the column doesn't exist and tries again."""
        result = toolkit.run_query("SELECT nosuchcolumn FROM salaries")
        assert "could not run" in result
        assert toolkit.failures

    def test_a_query_that_succeeds_after_a_failure_still_gives_a_result(self, toolkit):
        toolkit.run_query("SELECT nosuchcolumn FROM salaries")
        toolkit.run_query("SELECT emp FROM salaries")
        assert toolkit.last_result is not None
        assert len(toolkit.failures) == 1


class TestToolSurface:
    def test_only_read_only_tools_are_exposed(self, toolkit):
        """The eleven omitted tools include `load_local_csv_to_table`,
        `export_table_to_path` and the S3 loaders — all of which cross the session
        boundary by design."""
        assert set(toolkit.functions) == set(READ_ONLY_TOOLS)


class TestModelRendering:
    def test_a_small_result_is_rendered_whole(self):
        frame = pd.DataFrame({"a": [1, 2]})
        text = render_for_model(frame)
        assert "2 row(s) in total" in text
        assert "first" not in text

    def test_a_large_result_is_capped_and_says_so(self):
        frame = pd.DataFrame({"a": range(500)})
        text = render_for_model(frame)
        assert f"first {MODEL_PREVIEW_ROWS} shown" in text
        assert "499" not in text

    def test_an_empty_result_says_so_plainly(self):
        assert "no rows" in render_for_model(pd.DataFrame({"a": []}))

    def test_a_very_wide_result_is_capped_by_characters_too(self):
        """30 wide rows can still be enormous, and a result the model can't read past is
        worse than one it knows was truncated."""
        frame = pd.DataFrame({f"col{n}": ["x" * 50] * 30 for n in range(20)})
        assert len(render_for_model(frame)) < 5000
