"""Calculated and deleted columns (requirement 5.4)."""

import pandas as pd
import pytest

from engine import columns as engine_columns
from engine import duckdb_session as ds
from engine import relationships as rel
from engine.exceptions import CalculatedColumnError, UnsafeSqlError


@pytest.fixture
def connection():
    created = ds.open_connection()
    ds.register_table(
        created, "salaries", pd.DataFrame({"emp": ["a", "b"], "basic": [1000.0, 2000.0], "name": ["Ana", "Bo"]})
    )
    yield created
    created.close()


class TestExpressionProbe:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [("basic * 0.10", "DOUBLE"), ("upper(name)", "VARCHAR"), ("basic > 1500", "BOOLEAN")],
    )
    def test_the_type_comes_from_duckdb_not_a_guess(self, connection, expression, expected):
        assert engine_columns.expression_type(connection, "salaries", expression) == expected

    def test_a_typo_raises_carrying_duckdbs_own_message(self, connection):
        with pytest.raises(CalculatedColumnError, match="(?i)bsic"):
            engine_columns.expression_type(connection, "salaries", "bsic * 0.10")

    def test_probing_does_not_alter_the_table(self, connection):
        engine_columns.expression_type(connection, "salaries", "basic * 2")
        assert [column for column, _ in ds.describe_table(connection, "salaries")] == ["emp", "basic", "name"]

    def test_an_unknown_table_raises(self, connection):
        with pytest.raises(CalculatedColumnError, match="nope"):
            engine_columns.expression_type(connection, "nope", "1")


class TestAddColumn:
    def test_the_column_is_added_and_computed(self, connection):
        engine_columns.add_calculated_column(connection, "salaries", "tax", "basic * 0.10")
        assert ds.preview(connection, "salaries")["tax"].tolist() == [100.0, 200.0]

    def test_the_executed_sql_is_returned_for_display_and_replay(self, connection):
        statements = engine_columns.add_calculated_column(connection, "salaries", "tax", "basic * 0.10")
        assert len(statements) == 2
        assert statements[0].startswith("ALTER TABLE")
        assert statements[1].startswith("UPDATE")

    def test_a_later_column_can_reference_an_earlier_one(self, connection):
        """Requirement 5.4's chaining case: each statement runs against the same table,
        so `tax` is an ordinary column by the time `net` needs it."""
        engine_columns.add_calculated_column(connection, "salaries", "tax", "basic * 0.10")
        engine_columns.add_calculated_column(connection, "salaries", "net", "basic - tax")
        assert ds.preview(connection, "salaries")["net"].tolist() == [900.0, 1800.0]

    def test_a_duplicate_name_is_refused(self, connection):
        with pytest.raises(CalculatedColumnError, match="already has a column"):
            engine_columns.add_calculated_column(connection, "salaries", "basic", "1")

    def test_a_duplicate_name_is_refused_case_insensitively(self, connection):
        with pytest.raises(CalculatedColumnError):
            engine_columns.add_calculated_column(connection, "salaries", "BASIC", "1")

    def test_an_empty_name_is_refused(self, connection):
        with pytest.raises(CalculatedColumnError, match="name"):
            engine_columns.add_calculated_column(connection, "salaries", "  ", "1")

    def test_a_bad_expression_alters_nothing(self, connection):
        with pytest.raises(CalculatedColumnError):
            engine_columns.add_calculated_column(connection, "salaries", "tax", "bsic * 0.10")
        assert "tax" not in [column for column, _ in ds.describe_table(connection, "salaries")]

    def test_the_guardrails_apply(self, connection):
        with pytest.raises(UnsafeSqlError):
            engine_columns.add_calculated_column(connection, "salaries", "x", "1); DROP TABLE salaries; --")


class TestDropColumn:
    def test_the_column_is_removed(self, connection):
        engine_columns.drop_column(connection, "salaries", "name")
        assert [column for column, _ in ds.describe_table(connection, "salaries")] == ["emp", "basic"]

    def test_the_executed_sql_is_returned(self, connection):
        assert engine_columns.drop_column(connection, "salaries", "name") == [
            'ALTER TABLE "salaries" DROP COLUMN "name"'
        ]

    def test_dropping_a_missing_column_raises(self, connection):
        with pytest.raises(CalculatedColumnError, match="no column"):
            engine_columns.drop_column(connection, "salaries", "nope")

    def test_a_calculated_column_can_be_removed_again(self, connection):
        engine_columns.add_calculated_column(connection, "salaries", "tax", "basic * 0.10")
        engine_columns.drop_column(connection, "salaries", "tax")
        assert "tax" not in [column for column, _ in ds.describe_table(connection, "salaries")]

    def test_dropping_a_foreign_key_column_is_refused(self, connection):
        """DuckDB protects the constraint, and that refusal is worth surfacing: dropping
        the column would silently discard a confirmed relationship."""
        ds.register_table(connection, "dept", pd.DataFrame({"id": ["a", "b"]}))
        rel.enforce(connection, ["dept", "salaries"], [rel.Relationship("salaries", "emp", "dept", "id")])

        with pytest.raises(CalculatedColumnError, match="(?i)foreign key"):
            engine_columns.drop_column(connection, "salaries", "emp")


class TestDescribeStatements:
    def test_the_update_half_is_collapsed_away(self):
        lines = engine_columns.describe_statements(
            ['ALTER TABLE "s" ADD COLUMN "tax" DOUBLE', 'UPDATE "s" SET "tax" = (basic * 0.1)']
        )
        assert lines == ['ALTER TABLE "s" ADD COLUMN "tax" DOUBLE']

    def test_drops_are_kept(self):
        assert engine_columns.describe_statements(['ALTER TABLE "s" DROP COLUMN "tax"']) == [
            'ALTER TABLE "s" DROP COLUMN "tax"'
        ]
