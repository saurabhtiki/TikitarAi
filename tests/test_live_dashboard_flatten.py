"""Fact detection and the join walk that turns a star schema into one wide table.

The two tests that matter most:

- **The hierarchy.** `Stock -> SubCategory -> Category` must come out as three hops, each
  joined to the previous one's alias rather than back to the fact table. This is the whole of
  the requirement's "how are hierarchical relationships managed" answer, and getting it wrong
  produces a join that silently returns nothing.
- **The orphan.** A LEFT JOIN keeps a transaction whose customer is missing from the master.
  An inner join here would show fewer sales than the system of record, which is the worst
  thing a dashboard can quietly do.
"""

import duckdb
import pytest

from engine.relationships import Relationship
from live_dashboard import flatten


TABLE_NAMES = ["Transactions", "Customer", "Stock", "SubCategory", "Category", "Calendar"]

RELATIONSHIPS = [
    Relationship("Transactions", "CustID", "Customer", "CustID"),
    Relationship("Transactions", "StockID", "Stock", "StockID"),
    Relationship("Stock", "SubID", "SubCategory", "SubID"),
    Relationship("SubCategory", "CatID", "Category", "CatID"),
]


@pytest.fixture
def connection():
    con = duckdb.connect()
    con.execute("CREATE TABLE Category(CatID INT, CatName VARCHAR)")
    con.execute("CREATE TABLE SubCategory(SubID INT, SubName VARCHAR, CatID INT)")
    con.execute("CREATE TABLE Stock(StockID INT, StockName VARCHAR, SubID INT)")
    con.execute("CREATE TABLE Customer(CustID INT, CustName VARCHAR, CreditPeriod INT)")
    con.execute("CREATE TABLE Transactions(TxnID INT, CustID INT, StockID INT, Amount DOUBLE)")
    con.execute("CREATE TABLE Calendar(TheDate DATE, MonthName VARCHAR)")

    con.execute("INSERT INTO Category VALUES (1,'Hardware')")
    con.execute("INSERT INTO SubCategory VALUES (10,'Fasteners',1)")
    con.execute("INSERT INTO Stock VALUES (100,'Widget A',10)")
    con.execute("INSERT INTO Customer VALUES (5,'ABC Traders',30)")
    # Transaction 2 points at a customer that does not exist.
    con.execute("INSERT INTO Transactions VALUES (1,5,100,5000.0),(2,999,100,3200.0)")
    con.execute("INSERT INTO Calendar VALUES ('2026-01-01','January')")
    try:
        yield con
    finally:
        con.close()


# ------------------------------------------------------------------ fact detection


def test_the_fact_table_is_the_one_that_refers_to_the_most_others(connection):
    assert flatten.detect_fact_table(RELATIONSHIPS, TABLE_NAMES, connection) == "Transactions"


def test_with_no_confirmed_links_the_largest_table_wins(connection):
    connection.execute("INSERT INTO Customer VALUES (6,'B',1),(7,'C',1),(8,'D',1)")
    chosen = flatten.detect_fact_table([], ["Transactions", "Customer"], connection)
    assert chosen == "Customer"


def test_detection_survives_having_no_tables_at_all():
    assert flatten.detect_fact_table(RELATIONSHIPS, []) == ""


# ------------------------------------------------------------------ the join walk


def test_the_walk_reaches_a_three_level_hierarchy(connection):
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    depths = {step.prefix: step.depth for step in plan.joins}
    assert depths == {"Customer": 1, "Stock": 1, "SubCategory": 2, "Category": 3}


def test_a_deeper_hop_joins_to_the_previous_hop_not_to_the_fact_table(connection):
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    by_prefix = {step.prefix: step for step in plan.joins}
    assert by_prefix["SubCategory"].parent_alias == by_prefix["Stock"].alias
    assert by_prefix["Category"].parent_alias == by_prefix["SubCategory"].alias


def test_a_table_the_walk_never_reaches_stays_a_side_table(connection):
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    assert plan.side_tables == ["Calendar"]


def test_the_walk_stops_at_the_depth_limit(connection):
    """A chain longer than the limit is truncated rather than walked forever."""
    long_chain = RELATIONSHIPS + [Relationship("Category", "CatID", "Calendar", "TheDate")]
    plan = flatten.join_plan("Transactions", long_chain, TABLE_NAMES)
    assert max(step.depth for step in plan.joins) <= flatten.MAX_JOIN_DEPTH


def test_a_cycle_in_the_links_does_not_hang_the_walk(connection):
    cyclic = RELATIONSHIPS + [Relationship("Category", "CatID", "Transactions", "TxnID")]
    plan = flatten.join_plan("Transactions", cyclic, TABLE_NAMES)
    assert [step.prefix for step in plan.joins].count("Transactions") == 0


def test_describe_names_what_was_joined(connection):
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    assert "Customer" in plan.describe() and "Transactions" in plan.describe()


def test_describe_is_honest_when_nothing_links_out(connection):
    plan = flatten.join_plan("Transactions", [], TABLE_NAMES)
    assert "no confirmed links" in plan.describe()


# ------------------------------------------------------------------ the query


def test_master_columns_arrive_prefixed_by_their_table(connection):
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    frame = flatten.flatten_main_table(connection, plan)
    assert "Customer - CustName" in frame.columns
    assert "Category - CatName" in frame.columns


def test_the_joined_on_key_is_not_carried_twice(connection):
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    frame = flatten.flatten_main_table(connection, plan)
    assert "Customer - CustID" not in frame.columns


def test_a_row_whose_master_is_missing_still_survives(connection):
    """The LEFT JOIN promise: a dashboard never shows fewer sales than the source."""
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    frame = flatten.flatten_main_table(connection, plan)
    assert len(frame) == 2
    orphan = frame[frame["TxnID"] == 2].iloc[0]
    assert orphan["Customer - CustName"] is None or str(orphan["Customer - CustName"]) == "nan"
    # It still reaches Category through Stock, which is a different path.
    assert orphan["Category - CatName"] == "Hardware"


def test_a_master_larger_than_the_fact_table_is_left_out(connection):
    """Joining it would fan the fact rows out and double every total on the page."""
    connection.execute("CREATE TABLE Big(BigID INT, Note VARCHAR)")
    connection.execute("INSERT INTO Big SELECT range, 'x' FROM range(50)")
    links = [Relationship("Transactions", "TxnID", "Big", "BigID")]

    plan = flatten.join_plan("Transactions", links, ["Transactions", "Big"])
    frame = flatten.flatten_main_table(connection, plan)

    assert "Big.Note" not in frame.columns
    assert len(frame) == 2


def test_an_identifier_containing_a_quote_is_handled(connection):
    connection.execute('CREATE TABLE Odd("we""ird" INT, Note VARCHAR)')
    connection.execute("INSERT INTO Odd VALUES (1,'ok')")
    links = [Relationship("Transactions", "TxnID", "Odd", 'we"ird')]

    plan = flatten.join_plan("Transactions", links, ["Transactions", "Odd"])
    frame = flatten.flatten_main_table(connection, plan)
    assert "Odd - Note" in frame.columns


def test_a_side_table_loads_whole(connection):
    frame = flatten.load_side_table(connection, "Calendar")
    assert list(frame.columns) == ["TheDate", "MonthName"]
    assert len(frame) == 1


# ------------------------------------------------------------------ names charts can read


def test_no_flattened_name_carries_vega_path_punctuation(connection):
    """A dot in a field name is a *path* to Vega-Lite, not part of the name.

    It reads `Customer.Name` as `datum["Customer"]["Name"]`, finds nothing, and draws every
    row as one empty category. Vega-Lite's backslash escape fixes the drawing but not the
    click - the selection expression it generates still reads the escaped spelling, which no
    row has - so the separator has to stay out of its grammar entirely.
    """
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    frame = flatten.flatten_main_table(connection, plan)
    for column in frame.columns:
        assert not set(column) & set(".[]"), column


def test_a_fact_column_of_the_users_own_containing_a_dot_is_renamed_too(connection):
    """The user's spreadsheet is not required to know about Vega-Lite's grammar."""
    connection.execute('ALTER TABLE Transactions ADD COLUMN "Price.Net" DOUBLE')
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    frame = flatten.flatten_main_table(connection, plan)

    assert "Price Net" in frame.columns
    assert "Price.Net" not in frame.columns


def test_a_rename_that_would_collide_keeps_both_columns(connection):
    """Losing a column silently is worse than an awkward name."""
    connection.execute('ALTER TABLE Transactions ADD COLUMN "Price.Net" DOUBLE')
    connection.execute('ALTER TABLE Transactions ADD COLUMN "Price Net" DOUBLE')
    plan = flatten.join_plan("Transactions", RELATIONSHIPS, TABLE_NAMES)
    frame = flatten.flatten_main_table(connection, plan)

    assert len([name for name in frame.columns if name.startswith("Price Net")]) == 2


def test_a_side_table_is_cleaned_the_same_way(connection):
    connection.execute('CREATE TABLE Budget("Plan.Amount" DOUBLE)')
    assert "Plan Amount" in flatten.load_side_table(connection, "Budget").columns
