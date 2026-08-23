"""Saved report datasets: the file, the record, and the schema the chat gets (Phase 13).

The claims worth testing here are the ones the feature rests on:

- data survives the session that produced it, which is the whole point;
- a refresh **replaces**, so nobody can be answered from last month's rows;
- the agent's context comes back intact — tables, column meanings and the links — because
  a rebuilt schema that quietly lost its joins would produce wrong answers, not errors;
- a deleted report takes its file with it.

No `AppTest` anywhere: `reports/store.py`, `reports/db.py` and `reports/model.py` are
Streamlit-free on purpose, so all of this runs against a real DuckDB and a real SQLite.
"""

import duckdb
import pytest

from auth.db import init_db, seed_default_admin
from engine.dictionary import ColumnEntry, schema_context
from engine.relationships import Relationship
from reports.db import (
    dataset_info,
    forget_dataset,
    init_report_datasets_table,
    list_reports_with_data,
    load_setup,
    record_dataset,
)
from reports.exceptions import ReportDataError
from reports.model import ReportSetup, StoredTable, from_json, to_json
from reports.store import (
    close_store,
    dataset_path,
    delete_dataset,
    has_dataset,
    open_store,
    stored_tables,
    write_tables,
)
from tasks.db import init_tasks_table, save_task
from tasks.model import Task


@pytest.fixture
def db_path(tmp_path):
    """A real database with a real saved Task, since `report_datasets` references one."""
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    init_tasks_table(path)
    init_report_datasets_table(path)
    return path


@pytest.fixture
def saved_task(db_path):
    return save_task(1, Task(name="Monthly sales"), db_path)


@pytest.fixture
def source():
    """A session-like in-memory DuckDB holding two linked tables."""
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE TABLE customer AS SELECT * FROM (VALUES (1, 'Acme'), (2, 'Bee')) AS t(cust_id, name)")
    connection.execute(
        "CREATE TABLE sales AS SELECT * FROM (VALUES (1, 1, 100.0), (2, 2, 250.0), (3, 1, 50.0)) "
        "AS t(sale_id, cust_id, amount)"
    )
    return connection


def _setup_for(tables: list[StoredTable]) -> ReportSetup:
    return ReportSetup(
        tables=tables,
        dictionary=[
            ColumnEntry("sales", "amount", "DOUBLE", "money", "What the sale was worth", ["value", "revenue"]),
            ColumnEntry("sales", "cust_id", "INTEGER", "identifier", "Who bought it"),
            ColumnEntry("customer", "cust_id", "INTEGER", "identifier", "The customer's id"),
        ],
        relationships=[Relationship("sales", "cust_id", "customer", "cust_id")],
    )


# --------------------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------------------


def test_data_survives_the_session_that_produced_it(tmp_path, source):
    """The whole feature: write from one connection, read back from a cold one."""
    store = open_store(7, tmp_path)
    write_tables(store, source, ["customer", "sales"])
    close_store(store)
    source.close()

    reopened = open_store(7, tmp_path)
    total = reopened.execute("SELECT sum(amount) FROM sales").fetchone()[0]
    close_store(reopened)

    assert total == 400.0


def test_write_records_each_table_and_its_row_count(tmp_path, source):
    store = open_store(7, tmp_path)
    written = write_tables(store, source, ["customer", "sales"])
    close_store(store)

    assert [(table.name, table.row_count) for table in written] == [("customer", 2), ("sales", 3)]


def test_a_refresh_replaces_rather_than_appends(tmp_path, source):
    """Nobody may be answered from a mixture of this month and last."""
    store = open_store(7, tmp_path)
    write_tables(store, source, ["sales"])

    source.execute("DELETE FROM sales")
    source.execute("INSERT INTO sales VALUES (9, 1, 5.0)")
    write_tables(store, source, ["sales"])

    rows = store.execute("SELECT sale_id, amount FROM sales").fetchall()
    close_store(store)

    assert rows == [(9, 5.0)]


def test_a_table_dropped_from_the_report_is_dropped_from_the_file(tmp_path, source):
    store = open_store(7, tmp_path)
    write_tables(store, source, ["customer", "sales"])
    write_tables(store, source, ["sales"])
    remaining = stored_tables(store)
    close_store(store)

    assert remaining == ["sales"]


def test_writing_an_unknown_table_reports_it(tmp_path, source):
    store = open_store(7, tmp_path)
    with pytest.raises(ReportDataError, match="couldn't be read"):
        write_tables(store, source, ["not_a_table"])
    close_store(store)


def test_delete_removes_the_file(tmp_path, source):
    store = open_store(7, tmp_path)
    write_tables(store, source, ["sales"])
    close_store(store)

    assert has_dataset(7, tmp_path) is True
    assert delete_dataset(7, tmp_path) is True
    assert has_dataset(7, tmp_path) is False
    # Deleting again is not an error — there is simply nothing to delete.
    assert delete_dataset(7, tmp_path) is False


def test_each_report_gets_its_own_file(tmp_path):
    assert dataset_path(7, tmp_path) != dataset_path(8, tmp_path)


# --------------------------------------------------------------------------------------
# The schema the chat is given
# --------------------------------------------------------------------------------------


def test_setup_survives_a_round_trip(db_path, saved_task):
    setup = _setup_for([StoredTable("sales", 3)])
    record_dataset(saved_task.task_id, setup, "Ravi", db_path)

    restored = load_setup(saved_task.task_id, db_path)

    assert restored.table_names() == ["sales"]
    assert restored.relationships == [Relationship("sales", "cust_id", "customer", "cust_id")]
    assert restored.dictionary[0].synonyms == ["value", "revenue"]


def test_the_restored_context_still_names_the_columns_and_the_joins(db_path, saved_task):
    """A rebuilt schema that lost its links would give wrong answers, not errors."""
    record_dataset(saved_task.task_id, _setup_for([StoredTable("sales", 3)]), "Ravi", db_path)

    context = schema_context(load_setup(saved_task.task_id, db_path).dictionary,
                             load_setup(saved_task.task_id, db_path).relationships)

    assert "Table sales:" in context
    assert "amount (DOUBLE, money): What the sale was worth" in context
    assert "also called: value, revenue" in context
    assert "sales.cust_id = customer.cust_id" in context


def test_a_setup_saved_by_a_newer_version_is_refused():
    payload = to_json(ReportSetup())
    newer = payload.replace('"version": 1', '"version": 99')
    with pytest.raises(ReportDataError, match="newer version"):
        from_json(newer)


def test_unreadable_setup_json_is_reported():
    with pytest.raises(ReportDataError, match="valid JSON"):
        from_json("{not json")


# --------------------------------------------------------------------------------------
# The record: shared, and stamped with who refreshed it
# --------------------------------------------------------------------------------------


def test_a_refresh_overwrites_the_record_rather_than_adding_one(db_path, saved_task):
    record_dataset(saved_task.task_id, _setup_for([StoredTable("sales", 3)]), "Ravi", db_path)
    record_dataset(saved_task.task_id, _setup_for([StoredTable("sales", 11)]), "Meera", db_path)

    listed = list_reports_with_data(db_path)

    assert len(listed) == 1
    assert listed[0]["refreshed_by"] == "Meera"
    assert listed[0]["row_count"] == 11


def test_every_report_with_data_is_listed_whoever_owns_it(db_path):
    """The point of the phase: a dataset is shared, so the listing is not scoped by user."""
    mine = save_task(1, Task(name="Mine"), db_path)
    from auth.db import create_user

    create_user("second@example.com", "Second", "password123", "normal_user", db_path)
    theirs = save_task(2, Task(name="Theirs"), db_path)

    record_dataset(mine.task_id, _setup_for([StoredTable("sales", 1)]), "Ravi", db_path)
    record_dataset(theirs.task_id, _setup_for([StoredTable("sales", 2)]), "Meera", db_path)

    names = {row["name"] for row in list_reports_with_data(db_path)}

    assert names == {"Mine", "Theirs"}


def test_a_report_with_no_data_is_not_listed_and_cannot_be_opened(db_path, saved_task):
    assert list_reports_with_data(db_path) == []
    assert dataset_info(saved_task.task_id, db_path) is None
    with pytest.raises(ReportDataError, match="no saved data"):
        load_setup(saved_task.task_id, db_path)


def test_forgetting_a_dataset_removes_its_record(db_path, saved_task):
    record_dataset(saved_task.task_id, _setup_for([StoredTable("sales", 3)]), "Ravi", db_path)
    forget_dataset(saved_task.task_id, db_path)

    assert dataset_info(saved_task.task_id, db_path) is None
