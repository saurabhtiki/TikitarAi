"""Calculated, deleted and updated columns (requirement 5.4)."""

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


@pytest.fixture
def linked_connection():
    """Two tables joined by a confirmed relationship: salary (child) -> employee_master
    (parent). `employee_id` is unique on the parent side, which is what makes the join
    safe — every salary row matches exactly one employee_master row."""
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


LINK = rel.Relationship(
    child_table="salary", child_column="employee_id", parent_table="employee_master", parent_column="employee_id"
)


@pytest.fixture
def invoices():
    """The requirement 5.4 example: statuses to be marked overdue against a due date."""
    created = ds.open_connection()
    ds.register_table(
        created,
        "invoices",
        pd.DataFrame(
            {
                "invoice": ["i1", "i2", "i3"],
                "status": ["Open", "Open", "Open"],
                "days_late": [40, -5, 90],
            }
        ),
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


class TestCrossTableAddColumn:
    """A calculated column that reads across one confirmed relationship — child reading
    its parent, e.g. `salary.performance_bonus` computed from `employee_master.department`."""

    def test_a_child_column_can_read_its_parent(self, linked_connection):
        engine_columns.add_calculated_column(
            linked_connection,
            "salary",
            "bonus",
            "CASE WHEN employee_master.department = 'HR' THEN salary.basic_salary * 0.10 "
            "ELSE salary.basic_salary * 0.01 END",
            related_table="employee_master",
            relationships=[LINK],
        )
        result = ds.preview(linked_connection, "salary").sort_values("employee_id")
        assert result["bonus"].tolist() == [100.0, 20.0]

    def test_the_join_is_in_the_returned_statement_for_display_and_replay(self, linked_connection):
        statements = engine_columns.add_calculated_column(
            linked_connection,
            "salary",
            "bonus",
            "salary.basic_salary * 0.1",
            related_table="employee_master",
            relationships=[LINK],
        )
        assert "FROM" in statements[1] and "employee_master" in statements[1]
        assert "employee_id" in statements[1]

    def test_an_unlinked_table_is_refused(self, linked_connection):
        with pytest.raises(CalculatedColumnError, match="aren't linked"):
            engine_columns.add_calculated_column(
                linked_connection,
                "salary",
                "bonus",
                "employee_master.department",
                related_table="employee_master",
                relationships=[],
            )

    def test_the_reverse_direction_is_refused_as_needing_an_aggregate(self, linked_connection):
        """Adding to the parent from the child would need to combine several rows into
        one — a sum or count — which this doesn't attempt."""
        with pytest.raises(CalculatedColumnError, match="combine"):
            engine_columns.add_calculated_column(
                linked_connection,
                "employee_master",
                "total",
                "salary.basic_salary",
                related_table="salary",
                relationships=[LINK],
            )

    def test_nothing_is_altered_when_the_join_is_refused(self, linked_connection):
        with pytest.raises(CalculatedColumnError):
            engine_columns.add_calculated_column(
                linked_connection, "salary", "bonus", "1", related_table="employee_master", relationships=[]
            )
        assert "bonus" not in [column for column, _ in ds.describe_table(linked_connection, "salary")]

    def test_an_invented_alias_is_refused_by_duckdbs_own_binder(self, linked_connection):
        """The exact failure this feature exists to fix: a made-up alias like `em`
        instead of the real table name `employee_master`."""
        with pytest.raises(CalculatedColumnError, match="(?i)em"):
            engine_columns.add_calculated_column(
                linked_connection,
                "salary",
                "bonus",
                "em.department",
                related_table="employee_master",
                relationships=[LINK],
            )

    def test_an_unknown_related_table_is_refused(self, linked_connection):
        with pytest.raises(CalculatedColumnError, match="nope"):
            engine_columns.add_calculated_column(
                linked_connection, "salary", "bonus", "1", related_table="nope", relationships=[LINK]
            )

    def test_the_guardrails_still_apply_to_a_cross_table_expression(self, linked_connection):
        with pytest.raises(UnsafeSqlError):
            engine_columns.add_calculated_column(
                linked_connection,
                "salary",
                "x",
                "1); DROP TABLE salary; --",
                related_table="employee_master",
                relationships=[LINK],
            )

    def test_a_plain_add_is_unaffected_by_the_new_parameters(self, linked_connection):
        """The single-table path (`related_table=None`) is unchanged — no join is built
        just because two other tables happen to be in the session."""
        statements = engine_columns.add_calculated_column(linked_connection, "salary", "double_basic", "basic_salary * 2")
        assert "FROM" not in statements[1]


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


class TestAffectedRowCount:
    def test_no_condition_counts_every_row(self, invoices):
        assert engine_columns.affected_row_count(invoices, "invoices") == 3

    def test_a_condition_counts_only_matching_rows(self, invoices):
        assert engine_columns.affected_row_count(invoices, "invoices", "days_late > 30") == 2

    def test_a_blank_condition_counts_every_row(self, invoices):
        assert engine_columns.affected_row_count(invoices, "invoices", "   ") == 3

    def test_a_bad_condition_raises_with_duckdbs_message(self, invoices):
        with pytest.raises(CalculatedColumnError, match="(?i)days_lte"):
            engine_columns.affected_row_count(invoices, "invoices", "days_lte > 30")


class TestUpdateColumnValues:
    def test_a_conditional_update_changes_only_matching_rows(self, invoices):
        """Requirement 5.4's own example: mark status as overdue where the due date passed."""
        engine_columns.update_column_values(
            invoices, "invoices", "status", "'Over due'", "days_late > 30"
        )
        assert ds.preview(invoices, "invoices")["status"].tolist() == ["Over due", "Open", "Over due"]

    def test_no_condition_changes_every_row(self, invoices):
        engine_columns.update_column_values(invoices, "invoices", "status", "'Closed'")
        assert ds.preview(invoices, "invoices")["status"].tolist() == ["Closed"] * 3

    def test_an_expression_over_other_columns_works(self, invoices):
        engine_columns.update_column_values(invoices, "invoices", "days_late", "days_late * 2")
        assert ds.preview(invoices, "invoices")["days_late"].tolist() == [80, -10, 180]

    def test_the_executed_sql_is_returned_for_display_and_replay(self, invoices):
        assert engine_columns.update_column_values(
            invoices, "invoices", "status", "'Over due'", "days_late > 30"
        ) == ['UPDATE "invoices" SET "status" = (\'Over due\') WHERE (days_late > 30)']

    def test_the_column_name_is_matched_case_insensitively_but_written_as_stored(self, invoices):
        assert engine_columns.update_column_values(invoices, "invoices", "STATUS", "'x'") == [
            'UPDATE "invoices" SET "status" = (\'x\')'
        ]

    def test_an_unknown_column_is_refused(self, invoices):
        with pytest.raises(CalculatedColumnError, match="no column"):
            engine_columns.update_column_values(invoices, "invoices", "nope", "'x'")

    def test_an_empty_value_is_refused(self, invoices):
        with pytest.raises(CalculatedColumnError, match="(?i)new value"):
            engine_columns.update_column_values(invoices, "invoices", "status", "   ")

    def test_a_bad_value_expression_changes_nothing(self, invoices):
        with pytest.raises(CalculatedColumnError):
            engine_columns.update_column_values(invoices, "invoices", "status", "nosuchcolumn")
        assert ds.preview(invoices, "invoices")["status"].tolist() == ["Open"] * 3

    def test_a_bad_condition_changes_nothing(self, invoices):
        with pytest.raises(CalculatedColumnError):
            engine_columns.update_column_values(invoices, "invoices", "status", "'x'", "nosuchcolumn > 1")
        assert ds.preview(invoices, "invoices")["status"].tolist() == ["Open"] * 3

    def test_a_smuggled_statement_in_the_value_is_refused(self, invoices):
        with pytest.raises(UnsafeSqlError):
            engine_columns.update_column_values(
                invoices, "invoices", "status", "'x'); DROP TABLE invoices; --"
            )

    def test_a_smuggled_statement_in_the_condition_is_refused(self, invoices):
        with pytest.raises(UnsafeSqlError):
            engine_columns.update_column_values(
                invoices, "invoices", "status", "'x'", "1=1); DROP TABLE invoices; --"
            )

    def test_an_update_survives_a_relationship_rebuild(self, connection):
        """Requirement 5.2 rebuilds the tables to add a foreign key, and 5.4 says column
        changes persist for the session — so the recorded update has to replay in order."""
        ds.register_table(connection, "dept", pd.DataFrame({"id": ["a", "b"]}))
        recorded = engine_columns.add_calculated_column(connection, "salaries", "tax", "basic * 0.10")
        recorded += engine_columns.update_column_values(
            connection, "salaries", "tax", "0", "basic < 1500"
        )

        rel.enforce(
            connection,
            ["dept", "salaries"],
            [rel.Relationship("salaries", "emp", "dept", "id")],
            replay_statements=recorded,
        )

        assert ds.preview(connection, "salaries")["tax"].tolist() == [0.0, 200.0]


class TestDescribeStatements:
    def test_the_update_half_of_a_calculated_column_is_collapsed_away(self):
        lines = engine_columns.describe_statements(
            ['ALTER TABLE "s" ADD COLUMN "tax" DOUBLE', 'UPDATE "s" SET "tax" = (basic * 0.1)']
        )
        assert lines == ['ALTER TABLE "s" ADD COLUMN "tax" DOUBLE']

    def test_drops_are_kept(self):
        assert engine_columns.describe_statements(['ALTER TABLE "s" DROP COLUMN "tax"']) == [
            'ALTER TABLE "s" DROP COLUMN "tax"'
        ]

    def test_a_standalone_update_stays_visible(self):
        """It is a user action in its own right (requirement 5.4's update-values case).
        Hiding every UPDATE would make that whole action invisible in the log."""
        statement = 'UPDATE "s" SET "status" = (\'Over due\') WHERE (due_date < current_date)'
        assert engine_columns.describe_statements([statement]) == [statement]

    def test_an_update_on_a_column_added_earlier_but_not_immediately_stays_visible(self):
        lines = engine_columns.describe_statements(
            [
                'ALTER TABLE "s" ADD COLUMN "tax" DOUBLE',
                'UPDATE "s" SET "tax" = (basic * 0.1)',
                'ALTER TABLE "s" DROP COLUMN "name"',
                'UPDATE "s" SET "tax" = (0)',
            ]
        )
        assert lines == [
            'ALTER TABLE "s" ADD COLUMN "tax" DOUBLE',
            'ALTER TABLE "s" DROP COLUMN "name"',
            'UPDATE "s" SET "tax" = (0)',
        ]

    def test_an_update_on_a_different_column_than_the_one_just_added_stays_visible(self):
        lines = engine_columns.describe_statements(
            ['ALTER TABLE "s" ADD COLUMN "tax" DOUBLE', 'UPDATE "s" SET "basic" = (0)']
        )
        assert lines == ['ALTER TABLE "s" ADD COLUMN "tax" DOUBLE', 'UPDATE "s" SET "basic" = (0)']
