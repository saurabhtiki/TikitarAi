"""Fact detection and the join walk that turns a star schema into one wide table.

The two tests that matter most:

- **The hierarchy.** `Stock -> SubCategory -> Category` must come out as three hops, each
  joined to the previous one's alias rather than back to the fact table. This is the whole of
  the requirement's "how are hierarchical relationships managed" answer, and getting it wrong
  produces a join that silently returns nothing.
- **The orphan.** A LEFT JOIN keeps a transaction whose customer is missing from the master.
  An inner join here would show fewer sales than the system of record, which is the worst
  thing a dashboard can quietly do.
- **The sibling** (phase 39). Salary and Attendance are both children of Employee Master
  and are not related to each other at all. Each must come out carrying Employee Master's
  columns under the *same* name and with its own row count untouched, because that one
  shared spelling is the whole of how a single filter reaches both.
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


def test_a_table_the_walk_never_reaches_is_simply_a_plan_with_no_joins(connection):
    """There is no "side table" any more: every table gets its own plan, and a table
    nothing links out of is that plan with an empty join list."""
    plan = flatten.join_plan("Calendar", RELATIONSHIPS, TABLE_NAMES)
    assert plan.joins == []
    frame = flatten.flatten_main_table(connection, plan)
    assert list(frame.columns) == ["TheDate", "MonthName"]
    assert len(frame) == 1


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


def test_an_unlinked_table_is_cleaned_the_same_way(connection):
    connection.execute('CREATE TABLE Budget("Plan.Amount" DOUBLE)')
    plan = flatten.join_plan("Budget", [], ["Budget"])
    assert "Plan Amount" in flatten.flatten_main_table(connection, plan).columns


# ------------------------------------------------- phase 39: every table, one filter


#: Employee Master with two children that are not related to each other. The shape phase 39
#: was written for, and the one the old single-table flattening could not serve.
SIBLINGS = [
    Relationship("Salary", "EmpID", "EmployeeMaster", "EmpID"),
    Relationship("Attendance", "EmpID", "EmployeeMaster", "EmpID"),
]

SIBLING_TABLES = ["EmployeeMaster", "Salary", "Attendance"]


@pytest.fixture
def people():
    con = duckdb.connect()
    con.execute("CREATE TABLE EmployeeMaster(EmpID INT, EmpName VARCHAR, Department VARCHAR)")
    con.execute("CREATE TABLE Salary(SalID INT, EmpID INT, Amount DOUBLE)")
    con.execute("CREATE TABLE Attendance(AttID INT, EmpID INT, Days INT)")
    con.execute("INSERT INTO EmployeeMaster VALUES (1,'Asha','HR'),(2,'Ravi','Ops')")
    con.execute("INSERT INTO Salary VALUES (1,1,50000.0),(2,2,60000.0)")
    con.execute("INSERT INTO Attendance VALUES (1,1,20),(2,2,25)")
    try:
        yield con
    finally:
        con.close()


def test_both_children_carry_the_parents_columns_under_the_same_name(people):
    """The bug phase 39 exists for: with Salary as the one flattened table, Attendance was
    embedded raw, had no Department column at all, and a Department filter could not reach
    it - or, worse, emptied it."""
    tables, _ = flatten.flatten_every_table(people, SIBLINGS, SIBLING_TABLES)

    for name in ("Salary", "Attendance"):
        assert "EmployeeMaster - Department" in tables[name].columns, name


def test_the_parent_itself_carries_the_same_prefixed_name(people):
    """Otherwise the one table the filter missed would be the table the column came from."""
    tables, _ = flatten.flatten_every_table(people, SIBLINGS, SIBLING_TABLES)
    columns = tables["EmployeeMaster"].columns
    assert "Department" in columns
    assert "EmployeeMaster - Department" in columns


def test_a_parents_own_key_is_not_repeated_under_a_prefixed_name(people):
    """`EmployeeMaster - EmpID` would be a second column of identical values with no way to
    tell which of the two to filter on."""
    tables, _ = flatten.flatten_every_table(people, SIBLINGS, SIBLING_TABLES)
    assert "EmployeeMaster - EmpID" not in tables["EmployeeMaster"].columns


def test_no_table_is_ever_joined_to_a_sibling(people):
    """Row counts are the test that matters: a sibling join would multiply them, and every
    total on the page with them."""
    tables, _ = flatten.flatten_every_table(people, SIBLINGS, SIBLING_TABLES)
    assert [len(tables[name]) for name in SIBLING_TABLES] == [2, 2, 2]
    assert "Attendance - Days" not in tables["Salary"].columns


def test_a_table_nothing_links_to_keeps_its_columns_to_itself(people):
    """A Budget at another grain gains nothing and loses nothing - and the runtime skips a
    filter it cannot answer rather than emptying it."""
    people.execute("CREATE TABLE Budget(BudgetID INT, Amount DOUBLE)")
    tables, _ = flatten.flatten_every_table(
        people, SIBLINGS, [*SIBLING_TABLES, "Budget"]
    )
    assert list(tables["Budget"].columns) == ["BudgetID", "Amount"]


def test_a_hierarchy_still_reaches_every_level_from_every_table(connection):
    """Each table walks its own parents, so the three-level hierarchy is still three hops -
    and Customer, which used to be a master only, is now flattened in its own right too."""
    tables, description = flatten.flatten_every_table(
        connection, RELATIONSHIPS, TABLE_NAMES
    )

    assert set(tables) == set(TABLE_NAMES)
    assert "Category - CatName" in tables["Transactions"].columns
    assert "Category - CatName" in tables["Stock"].columns
    assert len(tables["Stock"]) == 1
    assert "Transactions" in description and "Calendar" in description
