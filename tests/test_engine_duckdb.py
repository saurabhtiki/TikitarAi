"""Connection handling, table registration, and the base/working table split."""

import pandas as pd
import pytest

from engine import duckdb_session as ds
from engine.exceptions import DataEngineError, TableLoadError, UnsafeSqlError


@pytest.fixture
def connection():
    created = ds.open_connection()
    yield created
    created.close()


@pytest.fixture
def sales():
    return pd.DataFrame({"cust_id": ["c1", "c2"], "amount": [10.0, 20.0]})


class TestTableNaming:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Jan Sales 2024.csv", "jan_sales_2024_csv"),
            ("  Employee Master  ", "employee_master"),
            ("2024 data", "data"),
            ("!!!", "table"),
            ("", "table"),
        ],
    )
    def test_slugify(self, label, expected):
        assert ds.slugify_table_name(label) == expected

    def test_collisions_get_a_suffix(self):
        assert ds.slugify_table_name("sales", {"sales"}) == "sales_2"
        assert ds.slugify_table_name("sales", {"sales", "sales_2"}) == "sales_3"

    def test_a_name_can_never_shadow_the_base_layer(self):
        assert not ds.slugify_table_name("base_sales").startswith("base_base")
        assert ds.slugify_table_name("base_sales") != "base_sales"

    def test_long_names_are_truncated(self):
        assert len(ds.slugify_table_name("x" * 200)) <= ds.MAX_TABLE_NAME_LENGTH


class TestRegistration:
    def test_both_layers_are_created(self, connection, sales):
        ds.register_table(connection, "sales", sales)
        assert ds.row_count(connection, "sales") == 2
        assert ds.row_count(connection, "base_sales") == 2

    def test_base_tables_are_hidden_from_the_table_list(self, connection, sales):
        ds.register_table(connection, "sales", sales)
        assert ds.list_tables(connection) == ["sales"]

    def test_describe_returns_columns_and_types(self, connection, sales):
        ds.register_table(connection, "sales", sales)
        assert ds.describe_table(connection, "sales") == [("cust_id", "VARCHAR"), ("amount", "DOUBLE")]

    def test_re_registering_replaces_rather_than_duplicates(self, connection, sales):
        ds.register_table(connection, "sales", sales)
        ds.register_table(connection, "sales", pd.DataFrame({"cust_id": ["c9"], "amount": [1.0]}))
        assert ds.row_count(connection, "sales") == 1
        assert ds.list_tables(connection) == ["sales"]

    def test_the_base_table_survives_changes_to_the_working_table(self, connection, sales):
        ds.register_table(connection, "sales", sales)
        connection.execute("DELETE FROM sales")
        assert ds.row_count(connection, "sales") == 0
        assert ds.row_count(connection, "base_sales") == 2

    def test_duplicate_column_names_are_refused_with_a_useful_message(self, connection):
        frame = pd.DataFrame([[1, 2]], columns=["amount", "amount"])
        with pytest.raises(TableLoadError, match="amount"):
            ds.register_table(connection, "sales", frame)

    def test_leading_zeros_survive_into_duckdb(self, connection):
        ds.register_table(connection, "staff", pd.DataFrame({"emp_id": ["007", "008"]}))
        assert ds.preview(connection, "staff")["emp_id"].tolist() == ["007", "008"]

    def test_describing_a_missing_table_raises(self, connection):
        with pytest.raises(DataEngineError):
            ds.describe_table(connection, "nope")


class TestExecution:
    def test_run_query_returns_a_dataframe(self, connection, sales):
        ds.register_table(connection, "sales", sales)
        assert ds.run_query(connection, "SELECT count(*) AS n FROM sales")["n"].tolist() == [2]

    def test_run_query_applies_the_guardrails(self, connection):
        with pytest.raises(UnsafeSqlError):
            ds.run_query(connection, "SELECT * FROM read_csv('/etc/passwd')")

    def test_a_duckdb_error_surfaces_duckdbs_own_message(self, connection, sales):
        ds.register_table(connection, "sales", sales)
        with pytest.raises(DataEngineError, match="(?i)not found|does not exist|referenced column"):
            ds.run_query(connection, "SELECT nope FROM sales")

    def test_identifiers_with_quotes_are_escaped(self):
        assert ds.quote_identifier('we"ird') == '"we""ird"'


class TestIsolation:
    def test_two_connections_do_not_share_tables(self, sales):
        first, second = ds.open_connection(), ds.open_connection()
        try:
            ds.register_table(first, "sales", sales)
            assert ds.list_tables(first) == ["sales"]
            assert ds.list_tables(second) == []
        finally:
            first.close()
            second.close()
