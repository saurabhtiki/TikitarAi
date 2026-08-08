"""The chat-type shape and its JSON round trip (requirement 6.6).

No Streamlit, no database, no provider: `chat_types/model.py` is pure by design and this
suite is what holds it that way.
"""

import json

import pytest

from chat_types.exceptions import ChatTypeStorageError
from chat_types.model import (
    SCHEMA_VERSION,
    ChatType,
    ExpectedColumn,
    ExpectedTable,
    SavedDescription,
    capture,
    column_count,
    from_json,
    to_json,
)
from engine.dictionary import ColumnEntry
from engine.relationships import Relationship


SEMANTIC_TYPES = {
    "employee": {"emp_id": "id", "name": "text", "joining_date": "date"},
    "salary": {"emp_id": "id", "basic": "numeric", "bonus": "numeric"},
}

LINK = Relationship("salary", "emp_id", "employee", "emp_id")


def _dictionary() -> list[ColumnEntry]:
    return [
        ColumnEntry("employee", "emp_id", "VARCHAR", "id", description="Employee number"),
        ColumnEntry("employee", "name", "VARCHAR", "text"),
        ColumnEntry("salary", "bonus", "DOUBLE", "numeric", description="Monthly bonus", synonyms=["incentive"]),
    ]


def _captured(name="Salary processing") -> ChatType:
    return capture(name, SEMANTIC_TYPES, [LINK], _dictionary())


class TestCapture:
    def test_it_captures_every_table_and_column(self):
        chat_type = _captured()
        assert chat_type.table_names() == ["employee", "salary"]
        assert column_count(chat_type) == 6

    def test_it_captures_each_columns_semantic_type(self):
        employee = _captured().table("employee")
        assert employee.types_by_column()["joining_date"] == "date"

    def test_it_keeps_only_the_columns_the_user_actually_described(self):
        # A dictionary is mostly blank rows; storing them would triple the payload to say
        # nothing at all.
        described = {(saved.table, saved.column) for saved in _captured().descriptions}
        assert described == {("employee", "emp_id"), ("salary", "bonus")}

    def test_a_column_with_only_synonyms_is_still_captured(self):
        dictionary = [ColumnEntry("salary", "basic", "DOUBLE", "numeric", synonyms=["base pay"])]
        chat_type = capture("Payroll", SEMANTIC_TYPES, [], dictionary)
        assert [saved.column for saved in chat_type.descriptions] == ["basic"]

    def test_a_new_chat_type_has_no_id_until_it_is_saved(self):
        assert _captured().chat_type_id is None

    def test_the_name_is_stripped(self):
        assert capture("  Salary processing  ", {}, [], []).name == "Salary processing"


class TestLookup:
    def test_a_table_is_found_whatever_case_the_file_arrived_in(self):
        # The name comes from a filename, so `SALARY MASTER.xlsx` is the same table as
        # `Salary Master.xlsx`.
        assert _captured().table("EMPLOYEE").table_name == "employee"

    def test_an_unknown_table_is_none_rather_than_an_error(self):
        assert _captured().table("payroll") is None

    def test_an_unnamed_chat_type_still_has_something_to_display(self):
        assert ChatType().display_name() == "Untitled chat type"


class TestRestoredDictionary:
    def test_descriptions_and_synonyms_come_back(self):
        entries = {entry.column: entry for entry in _captured().restored_dictionary()}
        assert entries["emp_id"].description == "Employee number"
        assert entries["bonus"].synonyms == ["incentive"]

    def test_the_stored_types_are_left_blank(self):
        # Deliberate: these entries are only ever passed to `build_dictionary(existing=…)`,
        # which reads the types off the tables as actually loaded. A restored type here
        # could claim a column is a date while the loaded column is text.
        assert all(entry.semantic_type == "" and entry.sql_type == "" for entry in _captured().restored_dictionary())


class TestRoundTrip:
    def test_everything_survives_a_save_and_load(self):
        loaded = from_json(to_json(_captured()), chat_type_id=7, name="Salary processing")

        assert loaded.chat_type_id == 7
        assert loaded.name == "Salary processing"
        assert loaded.table_names() == ["employee", "salary"]
        assert loaded.table("salary").types_by_column() == SEMANTIC_TYPES["salary"]
        assert loaded.relationships == [LINK]
        assert loaded.descriptions[1].synonyms == ["incentive"]

    def test_the_payload_carries_its_schema_version(self):
        assert json.loads(to_json(_captured()))["version"] == SCHEMA_VERSION

    def test_no_rows_or_statements_are_stored(self):
        # A chat type is a recipe. Calculated-column statements belong to the Task recipe
        # of requirement 7.5, not here.
        payload = json.loads(to_json(_captured()))
        assert set(payload) == {"version", "tables", "relationships", "descriptions"}

    def test_an_empty_chat_type_round_trips(self):
        loaded = from_json(to_json(ChatType(name="Empty")))
        assert loaded.tables == [] and loaded.relationships == []


class TestReadingBrokenJson:
    def test_text_that_isnt_json_is_reported_not_raised_raw(self):
        with pytest.raises(ChatTypeStorageError, match="valid JSON"):
            from_json("{not json")

    def test_json_that_isnt_an_object_is_refused(self):
        with pytest.raises(ChatTypeStorageError, match="expected format"):
            from_json("[1, 2, 3]")

    def test_a_newer_schema_version_is_refused_with_a_reason(self):
        payload = json.dumps({"version": SCHEMA_VERSION + 1, "tables": []})
        with pytest.raises(ChatTypeStorageError, match="newer version"):
            from_json(payload)

    def test_a_half_written_relationship_is_dropped_rather_than_loaded(self):
        # `relationships.enforce` would fail on a link joining a table to nothing, and the
        # user's other links are worth more than that one.
        payload = json.dumps(
            {
                "version": SCHEMA_VERSION,
                "tables": [{"table_name": "salary", "columns": []}],
                "relationships": [
                    {"child_table": "salary", "child_column": "", "parent_table": "employee", "parent_column": "emp_id"},
                    {"child_table": "salary", "child_column": "emp_id", "parent_table": "employee", "parent_column": "emp_id"},
                ],
            }
        )
        assert from_json(payload).relationships == [LINK]

    def test_a_table_with_no_name_is_dropped(self):
        payload = json.dumps({"version": SCHEMA_VERSION, "tables": [{"columns": []}]})
        assert from_json(payload).tables == []

    def test_missing_keys_read_back_as_empty_rather_than_failing(self):
        loaded = from_json(json.dumps({"version": SCHEMA_VERSION}))
        assert loaded.tables == [] and loaded.descriptions == []


class TestExpectedTable:
    def test_it_lists_its_column_names_in_order(self):
        table = ExpectedTable(
            "salary", [ExpectedColumn("emp_id", "id"), ExpectedColumn("basic", "numeric")]
        )
        assert table.column_names == ["emp_id", "basic"]

    def test_a_saved_description_defaults_to_no_synonyms(self):
        assert SavedDescription("salary", "basic").synonyms == []
