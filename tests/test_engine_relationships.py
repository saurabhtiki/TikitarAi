"""Relationship suggestion, the two pre-checks, and foreign-key enforcement.

The enforcement tests are the important ones. Requirement 5.2 promises that once a
relationship is confirmed, "every query against those tables is guaranteed to join
cleanly — there are no orphaned or unmatched rows to account for downstream." That is
only true if the constraint is real, so the test that matters most inserts an orphan
*after* enforcement and asserts DuckDB refuses it.
"""

import duckdb
import pandas as pd
import pytest

from engine import columns as engine_columns
from engine import duckdb_session as ds
from engine import relationships as rel
from engine.exceptions import RelationshipError

SALES = pd.DataFrame(
    {
        "cust_id": ["c1", "c2", "c1", "c9"],
        "sku": ["s1", "s2", "s1", "s1"],
        "basic": [100.0, 50.0, 250.0, 75.0],
    }
)
CUSTOMER = pd.DataFrame({"id": ["c1", "c2", "c3"], "name": ["Ana", "Bo", "Cy"]})
STOCK = pd.DataFrame({"sku": ["s1", "s2"], "name": ["Widget", "Gadget"]})


@pytest.fixture
def connection():
    created = ds.open_connection()
    for name, frame in (("sales", SALES), ("customer", CUSTOMER), ("stock", STOCK)):
        ds.register_table(created, name, frame)
    yield created
    created.close()


def _named(candidates, child_column):
    return next(
        (candidate for candidate in candidates if candidate.relationship.child_column == child_column), None
    )


class TestSuggestion:
    def test_an_exact_column_name_match_is_suggested(self, connection):
        candidate = _named(rel.suggest_relationships(connection, ds.list_tables(connection)), "sku")
        assert candidate is not None
        assert candidate.relationship == rel.Relationship("sales", "sku", "stock", "sku")
        assert candidate.containment == 1.0

    def test_a_foreign_key_style_name_matches_a_bare_id(self, connection):
        """`sales.cust_id` -> `customer.id` — requirement 5.2's own shape."""
        candidate = _named(rel.suggest_relationships(connection, ds.list_tables(connection)), "cust_id")
        assert candidate is not None
        assert candidate.relationship == rel.Relationship("sales", "cust_id", "customer", "id")

    def test_an_abbreviated_name_matches_its_master_table(self, connection):
        ds.register_table(connection, "attendance", pd.DataFrame({"emp_id": ["e1", "e2"], "hours": [8, 7]}))
        ds.register_table(connection, "employee", pd.DataFrame({"id": ["e1", "e2", "e3"], "nm": ["X", "Y", "Z"]}))

        candidate = _named(rel.suggest_relationships(connection, ds.list_tables(connection)), "emp_id")
        assert candidate is not None
        assert candidate.relationship.parent_table == "employee"

    def test_the_unique_side_becomes_the_parent(self, connection):
        candidate = _named(rel.suggest_relationships(connection, ds.list_tables(connection)), "cust_id")
        assert candidate.relationship.child_table == "sales"
        assert candidate.relationship.parent_table == "customer"

    def test_two_bare_id_columns_are_not_paired_with_each_other(self, connection):
        """`customer.id` and `stock.id` both being called `id` means nothing."""
        ds.register_table(connection, "stock", pd.DataFrame({"id": ["s1"], "name": ["W"]}))
        pairs = {
            (candidate.relationship.child_table, candidate.relationship.parent_table)
            for candidate in rel.suggest_relationships(connection, ds.list_tables(connection))
        }
        assert ("customer", "stock") not in pairs
        assert ("stock", "customer") not in pairs

    def test_same_named_columns_with_no_shared_values_are_not_suggested(self, connection):
        """`customer.name` and `stock.name` match by name but share no values."""
        candidates = rel.suggest_relationships(connection, ds.list_tables(connection))
        assert not [
            candidate for candidate in candidates if candidate.relationship.child_column == "name"
        ]

    def test_candidates_are_ranked_by_confidence(self, connection):
        candidates = rel.suggest_relationships(connection, ds.list_tables(connection))
        assert [candidate.confidence for candidate in candidates] == sorted(
            (candidate.confidence for candidate in candidates), reverse=True
        )

    def test_the_summary_reads_as_plain_english(self, connection):
        candidate = _named(rel.suggest_relationships(connection, ds.list_tables(connection)), "cust_id")
        assert "67%" in candidate.summary()
        assert "sales.cust_id" in candidate.summary()


class TestPreChecks:
    def test_a_clean_relationship_passes(self, connection):
        check = rel.check_relationship(connection, rel.Relationship("sales", "sku", "stock", "sku"))
        assert check.ok

    def test_orphan_rows_block_the_relationship(self, connection):
        check = rel.check_relationship(connection, rel.Relationship("sales", "cust_id", "customer", "id"))
        assert not check.ok
        assert check.orphan_count == 1

    def test_orphan_rows_come_back_in_full(self, connection):
        """Requirement 5.2: every column's value, not a row number — that is what makes
        the row findable in the source file."""
        check = rel.check_relationship(connection, rel.Relationship("sales", "cust_id", "customer", "id"))
        assert list(check.orphan_rows.columns) == ["cust_id", "sku", "basic"]
        assert check.orphan_rows.iloc[0]["cust_id"] == "c9"
        assert check.orphan_rows.iloc[0]["basic"] == 75.0

    def test_a_null_key_is_not_an_orphan(self, connection):
        ds.register_table(
            connection, "sales", pd.DataFrame({"cust_id": ["c1", None], "sku": ["s1", "s1"], "basic": [1.0, 2.0]})
        )
        check = rel.check_relationship(connection, rel.Relationship("sales", "cust_id", "customer", "id"))
        assert check.ok

    def test_a_repeated_parent_key_blocks_the_relationship(self, connection):
        ds.register_table(connection, "customer", pd.DataFrame({"id": ["c1", "c1"], "name": ["Ana", "Ana2"]}))
        check = rel.check_relationship(connection, rel.Relationship("sales", "cust_id", "customer", "id"))
        assert not check.ok
        assert "repeats" in check.message
        assert len(check.duplicate_parent_keys) == 2

    def test_duplicate_parent_rows_come_back_in_full(self, connection):
        ds.register_table(connection, "customer", pd.DataFrame({"id": ["c1", "c1"], "name": ["Ana", "Ana2"]}))
        check = rel.check_relationship(connection, rel.Relationship("sales", "cust_id", "customer", "id"))
        assert list(check.duplicate_parent_keys.columns) == ["id", "name"]


class TestOrdering:
    def test_parents_come_before_children(self):
        ordered = rel.order_tables(
            ["sales", "customer"], [rel.Relationship("sales", "cust_id", "customer", "id")]
        )
        assert ordered.index("customer") < ordered.index("sales")

    def test_a_multi_level_chain_orders_correctly(self):
        chain = [
            rel.Relationship("po_details", "po_id", "po_header", "id"),
            rel.Relationship("po_header", "item_id", "inventory", "id"),
        ]
        ordered = rel.order_tables(["po_details", "po_header", "inventory"], chain)
        assert ordered.index("inventory") < ordered.index("po_header") < ordered.index("po_details")

    def test_a_cycle_is_refused_and_names_the_tables(self):
        with pytest.raises(RelationshipError, match="loop"):
            rel.order_tables(
                ["a", "b"], [rel.Relationship("a", "x", "b", "y"), rel.Relationship("b", "y", "a", "x")]
            )

    def test_ordering_is_deterministic(self):
        first = rel.order_tables(["c", "b", "a"], [])
        second = rel.order_tables(["c", "b", "a"], [])
        assert first == second


class TestEnforcement:
    def test_the_constraint_is_real(self, connection):
        """The promise in requirement 5.2: once confirmed, no unmatched rows can exist."""
        confirmed = [rel.Relationship("sales", "sku", "stock", "sku")]
        rel.enforce(connection, ds.list_tables(connection), confirmed)

        with pytest.raises(duckdb.Error):
            connection.execute("INSERT INTO sales VALUES ('c1', 'NOT_A_SKU', 1.0)")

    def test_rows_survive_the_rebuild(self, connection):
        rel.enforce(connection, ds.list_tables(connection), [rel.Relationship("sales", "sku", "stock", "sku")])
        assert ds.row_count(connection, "sales") == 4

    def test_a_matching_insert_is_still_accepted(self, connection):
        rel.enforce(connection, ds.list_tables(connection), [rel.Relationship("sales", "sku", "stock", "sku")])
        connection.execute("INSERT INTO sales VALUES ('c1', 's2', 5.0)")
        assert ds.row_count(connection, "sales") == 5

    def test_calculated_columns_survive_a_rebuild(self, connection):
        confirmed = [rel.Relationship("sales", "sku", "stock", "sku")]
        rel.enforce(connection, ds.list_tables(connection), confirmed)

        statements = engine_columns.add_calculated_column(connection, "sales", "tax", "basic * 0.10")
        statements += engine_columns.add_calculated_column(connection, "sales", "net", "basic - tax")

        rel.enforce(connection, ds.list_tables(connection), confirmed, replay_statements=statements)

        result = ds.preview(connection, "sales")
        assert "tax" in result.columns and "net" in result.columns
        assert result["net"].tolist() == [90.0, 45.0, 225.0, 67.5]

    def test_a_multi_level_chain_can_be_enforced(self, connection):
        ds.register_table(connection, "inventory", pd.DataFrame({"id": ["i1", "i2"], "nm": ["A", "B"]}))
        ds.register_table(connection, "po_header", pd.DataFrame({"id": ["p1"], "item_id": ["i1"]}))
        ds.register_table(connection, "po_details", pd.DataFrame({"po_id": ["p1"], "qty": [3]}))

        chain = [
            rel.Relationship("po_details", "po_id", "po_header", "id"),
            rel.Relationship("po_header", "item_id", "inventory", "id"),
        ]
        rel.enforce(connection, ["inventory", "po_header", "po_details"], chain)

        with pytest.raises(duckdb.Error):
            connection.execute("INSERT INTO po_details VALUES ('NOPE', 1)")

    def test_a_failing_rebuild_leaves_the_session_intact(self, connection):
        """Orphans exist, so the constraint can't be created. Nothing may be lost."""
        with pytest.raises(RelationshipError):
            rel.enforce(
                connection,
                ds.list_tables(connection),
                [rel.Relationship("sales", "cust_id", "customer", "id")],
            )

        assert ds.row_count(connection, "sales") == 4
        assert set(ds.list_tables(connection)) == {"sales", "customer", "stock"}

    def test_enforcing_twice_is_idempotent(self, connection):
        confirmed = [rel.Relationship("sales", "sku", "stock", "sku")]
        rel.enforce(connection, ds.list_tables(connection), confirmed)
        rel.enforce(connection, ds.list_tables(connection), confirmed)
        assert ds.row_count(connection, "sales") == 4


class TestEnforceable:
    """A relationship still isn't blocked from being *used* just because it can't be
    enforced — `enforceable()` is only the filter for what's safe to hand to
    `enforce()`, not a gate on what's recorded for the agent to join on."""

    def test_a_clean_relationship_passes_through(self, connection):
        clean = rel.Relationship("sales", "sku", "stock", "sku")
        assert rel.enforceable(connection, [clean]) == [clean]

    def test_an_orphaned_relationship_is_dropped(self, connection):
        dirty = rel.Relationship("sales", "cust_id", "customer", "id")
        assert rel.enforceable(connection, [dirty]) == []

    def test_a_mix_keeps_only_the_clean_ones(self, connection):
        clean = rel.Relationship("sales", "sku", "stock", "sku")
        dirty = rel.Relationship("sales", "cust_id", "customer", "id")
        assert rel.enforceable(connection, [clean, dirty]) == [clean]


class TestDiagram:
    def test_dot_contains_a_node_per_table_and_an_edge_per_link(self):
        dot = rel.to_dot(
            ["sales", "customer", "stock"],
            [
                rel.Relationship("sales", "cust_id", "customer", "id"),
                rel.Relationship("sales", "sku", "stock", "sku"),
            ],
        )
        assert dot.startswith("digraph")
        assert dot.count("->") == 2
        for table in ("sales", "customer", "stock"):
            assert f'"{table}"' in dot

    def test_dot_is_valid_with_no_relationships(self):
        assert "->" not in rel.to_dot(["sales"], [])
