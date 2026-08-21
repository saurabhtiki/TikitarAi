"""The SQL guardrails (requirements 5.5).

These matter more than most tests here: the calculated-column dialog accepts free text
from a user, and Stage 6's agent will send SQL written by a model. Everything the guard
is supposed to stop is asserted, and so is everything it must not — a guard that rejects
ordinary analytics would be quietly useless because people would route around it.
"""

import pytest

from engine.exceptions import UnsafeSqlError
from engine.guards import assert_safe_expression, assert_safe_sql, split_statements, strip_literals

SESSION_TABLES = {"sales", "customer"}


class TestStatementSplitting:
    def test_trailing_semicolon_is_one_statement(self):
        assert split_statements("SELECT 1;") == ["SELECT 1"]

    def test_two_statements_are_split(self):
        assert len(split_statements("SELECT 1; SELECT 2")) == 2

    def test_semicolon_inside_a_string_literal_does_not_split(self):
        assert split_statements("SELECT * FROM sales WHERE note = 'a;b'") == [
            "SELECT * FROM sales WHERE note = 'a;b'"
        ]

    def test_semicolon_inside_a_comment_does_not_split(self):
        assert split_statements("SELECT 1 -- and; then\n") == ["SELECT 1 -- and; then"]

    def test_strip_literals_preserves_length(self):
        sql = "SELECT 'abc' FROM sales"
        assert len(strip_literals(sql)) == len(sql)


class TestRejected:
    def test_multiple_statements(self):
        with pytest.raises(UnsafeSqlError, match="one SQL statement"):
            assert_safe_sql("SELECT 1; DROP TABLE sales", SESSION_TABLES)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_csv('/etc/passwd')",
            "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
            "SELECT * FROM read_json_auto('/tmp/x.json')",
            "SELECT * FROM glob('/**')",
        ],
    )
    def test_filesystem_functions(self, sql):
        with pytest.raises(UnsafeSqlError, match="filesystem"):
            assert_safe_sql(sql, SESSION_TABLES)

    @pytest.mark.parametrize(
        "sql",
        [
            "ATTACH 'other.db' AS other",
            "COPY sales TO '/tmp/out.csv'",
            "INSTALL httpfs",
            "LOAD httpfs",
            "EXPORT DATABASE '/tmp/dump'",
            "PRAGMA database_list",
        ],
    )
    def test_escape_statements(self, sql):
        with pytest.raises(UnsafeSqlError):
            assert_safe_sql(sql, SESSION_TABLES)

    def test_dropping_a_table_outside_the_session(self):
        with pytest.raises(UnsafeSqlError, match="users"):
            assert_safe_sql("DROP TABLE users", SESSION_TABLES)

    def test_altering_a_table_outside_the_session(self):
        with pytest.raises(UnsafeSqlError, match="users"):
            assert_safe_sql("ALTER TABLE users ADD COLUMN x INTEGER", SESSION_TABLES)

    def test_altering_a_quoted_table_outside_the_session(self):
        with pytest.raises(UnsafeSqlError, match="users"):
            assert_safe_sql('ALTER TABLE "users" ADD COLUMN x INTEGER', SESSION_TABLES)

    def test_a_quoted_name_with_a_space_is_still_checked(self):
        with pytest.raises(UnsafeSqlError, match="my table"):
            assert_safe_sql('DROP TABLE "my table"', SESSION_TABLES)

    def test_no_allowed_tables_means_no_table_may_be_mutated(self):
        with pytest.raises(UnsafeSqlError):
            assert_safe_sql("DROP TABLE sales")

    def test_empty_sql(self):
        with pytest.raises(UnsafeSqlError):
            assert_safe_sql("   ", SESSION_TABLES)


class TestAllowed:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM sales",
            "SELECT c.name, sum(s.amount) FROM sales s JOIN customer c ON s.cust_id = c.id GROUP BY 1",
            "SELECT * FROM sales WHERE note = 'drop table users'",
            "WITH totals AS (SELECT 1) SELECT * FROM totals",
            "SELECT count(*) FROM sales -- a comment",
        ],
    )
    def test_ordinary_analytics_passes(self, sql):
        assert assert_safe_sql(sql, SESSION_TABLES) == sql

    def test_mutating_a_session_table_is_allowed(self):
        sql = "ALTER TABLE sales ADD COLUMN tax DOUBLE"
        assert assert_safe_sql(sql, SESSION_TABLES) == sql

    def test_table_names_are_matched_case_insensitively(self):
        assert assert_safe_sql("DROP TABLE SALES", SESSION_TABLES)

    @pytest.mark.parametrize(
        "sql",
        [
            'ALTER TABLE "sales" ADD COLUMN "tax" DOUBLE',
            'ALTER TABLE "sales" DROP COLUMN "tax"',
            'CREATE OR REPLACE TABLE "sales" AS SELECT 1',
            'ALTER TABLE main."sales" ADD COLUMN tax DOUBLE',
        ],
    )
    def test_a_quoted_session_table_is_allowed(self, sql):
        """Every statement this app generates quotes its identifiers.

        Blanking the quotes left `ADD` looking like the table name, so replaying a saved
        calculated column failed with \"'ADD' isn't one of this session's tables\".
        """
        assert assert_safe_sql(sql, SESSION_TABLES) == sql


class TestExpressions:
    @pytest.mark.parametrize("expression", ["basic * 0.10", "basic - tax", "upper(name)", "coalesce(qty, 0)"])
    def test_ordinary_formulas_pass(self, expression):
        assert assert_safe_expression(expression) == expression

    def test_a_formula_that_smuggles_in_a_second_statement_is_refused(self):
        with pytest.raises(UnsafeSqlError, match="one SQL statement"):
            assert_safe_expression("1); DROP TABLE sales; --")

    def test_a_formula_reading_the_filesystem_is_refused(self):
        with pytest.raises(UnsafeSqlError, match="filesystem"):
            assert_safe_expression("(SELECT count(*) FROM read_csv('/etc/passwd'))")

    def test_empty_formula(self):
        with pytest.raises(UnsafeSqlError):
            assert_safe_expression("  ")
