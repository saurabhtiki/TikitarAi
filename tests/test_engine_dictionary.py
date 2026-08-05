"""The column dictionary and the schema context handed to the agent (requirement 5.3)."""

import pandas as pd
import pytest

from engine import dictionary
from engine import duckdb_session as ds
from engine.dictionary import ColumnEntry
from engine.relationships import Relationship


@pytest.fixture
def connection():
    created = ds.open_connection()
    ds.register_table(created, "sales", pd.DataFrame({"qty": [5, 12, 3], "name": ["a", "b", "c"]}))
    ds.register_table(created, "stock", pd.DataFrame({"name": ["Widget", "Gadget", "Gizmo"]}))
    yield created
    created.close()


@pytest.fixture
def entries(connection):
    return dictionary.build_dictionary(connection, ["sales", "stock"], {"sales": {"qty": "numeric"}})


class TestSynonymParsing:
    def test_a_comma_separated_list_is_split_and_trimmed(self):
        assert dictionary.parse_synonyms("quantity, units , units sold") == [
            "quantity",
            "units",
            "units sold",
        ]

    def test_blanks_are_dropped(self):
        assert dictionary.parse_synonyms("a,, ,b") == ["a", "b"]

    def test_duplicates_are_removed_case_insensitively(self):
        assert dictionary.parse_synonyms("Units, units, UNITS") == ["Units"]

    def test_empty_input_gives_an_empty_list(self):
        assert dictionary.parse_synonyms(None) == []


class TestBuild:
    def test_one_entry_per_column_across_every_table(self, entries):
        assert len(entries) == 3
        assert {entry.qualified_name for entry in entries} == {"sales.qty", "sales.name", "stock.name"}

    def test_sql_and_semantic_types_are_both_recorded(self, entries):
        qty = next(entry for entry in entries if entry.column == "qty")
        assert qty.sql_type == "BIGINT"
        assert qty.semantic_type == "numeric"

    def test_same_named_columns_on_different_tables_stay_separate(self, entries):
        """Requirement 5.3's stated ambiguity — `Customer.Name` vs `Stock.Name`."""
        keys = [entry.key for entry in entries if entry.column == "name"]
        assert set(keys) == {("sales", "name"), ("stock", "name")}

    def test_typed_descriptions_survive_a_rebuild(self, connection, entries):
        entries[0].description = "Units sold"
        entries[0].synonyms = ["quantity"]

        rebuilt = dictionary.build_dictionary(connection, ["sales", "stock"], {}, existing=entries)

        carried = next(entry for entry in rebuilt if entry.key == entries[0].key)
        assert carried.description == "Units sold"
        assert carried.synonyms == ["quantity"]

    def test_a_new_column_arrives_blank(self, connection, entries):
        connection.execute("ALTER TABLE sales ADD COLUMN tax DOUBLE")
        rebuilt = dictionary.build_dictionary(connection, ["sales", "stock"], {}, existing=entries)
        assert next(entry for entry in rebuilt if entry.column == "tax").description == ""

    def test_a_removed_column_drops_out(self, connection, entries):
        connection.execute("ALTER TABLE sales DROP COLUMN name")
        rebuilt = dictionary.build_dictionary(connection, ["sales", "stock"], {}, existing=entries)
        assert ("sales", "name") not in {entry.key for entry in rebuilt}


class TestSampleValues:
    def test_distinct_non_blank_values_come_back(self, connection):
        assert sorted(dictionary.sample_values(connection, "sales", "qty")) == ["12", "3", "5"]

    def test_a_missing_column_degrades_to_no_samples(self, connection):
        assert dictionary.sample_values(connection, "sales", "nope") == []


class TestGridRoundTrip:
    def test_the_grid_has_a_row_per_entry(self, entries):
        grid = dictionary.to_grid(entries)
        assert list(grid.columns) == dictionary.GRID_COLUMNS
        assert len(grid) == len(entries)

    def test_edits_are_merged_back(self, entries):
        grid = dictionary.to_grid(entries)
        grid.loc[grid["column"] == "qty", "description"] = "Units sold per line"
        grid.loc[grid["column"] == "qty", "also_known_as"] = "quantity, units"

        merged = dictionary.merge_edits(entries, grid)

        qty = next(entry for entry in merged if entry.column == "qty")
        assert qty.description == "Units sold per line"
        assert qty.synonyms == ["quantity", "units"]

    def test_a_reordered_grid_still_matches_the_right_column(self):
        """Matched on (table, column), never row position — a sorted grid would
        otherwise attach one column's description to another."""
        entries = [
            ColumnEntry("sales", "qty", "BIGINT", "numeric"),
            ColumnEntry("sales", "amount", "DOUBLE", "numeric"),
        ]
        grid = dictionary.to_grid(entries).iloc[::-1].reset_index(drop=True)
        grid.loc[grid["column"] == "qty", "description"] = "Units"

        merged = dictionary.merge_edits(entries, grid)
        assert next(entry for entry in merged if entry.column == "qty").description == "Units"
        assert next(entry for entry in merged if entry.column == "amount").description == ""

    def test_display_only_columns_are_ignored_on_the_way_back(self, entries):
        grid = dictionary.to_grid(entries)
        grid.loc[0, "type"] = "tampered"
        merged = dictionary.merge_edits(entries, grid)
        assert merged[0].semantic_type == entries[0].semantic_type

    def test_an_empty_edit_frame_changes_nothing(self, entries):
        assert dictionary.merge_edits(entries, pd.DataFrame()) == entries


class TestSuggestions:
    class _Suggestion:
        def __init__(self, description, synonyms):
            self.description = description
            self.synonyms = synonyms

    def test_blank_descriptions_are_filled(self, entries):
        applied = dictionary.apply_suggestions(
            entries, {("sales", "qty"): self._Suggestion("Units sold", ["quantity"])}
        )
        assert next(entry for entry in applied if entry.column == "qty").description == "Units sold"

    def test_what_the_user_wrote_is_left_alone(self, entries):
        entries[0].description = "Mine"
        applied = dictionary.apply_suggestions(entries, {entries[0].key: self._Suggestion("Theirs", [])})
        assert applied[0].description == "Mine"

    def test_overwrite_replaces_it(self, entries):
        entries[0].description = "Mine"
        applied = dictionary.apply_suggestions(
            entries, {entries[0].key: self._Suggestion("Theirs", [])}, overwrite=True
        )
        assert applied[0].description == "Theirs"


class TestSchemaContext:
    def test_every_table_and_column_appears(self, entries):
        context = dictionary.schema_context(entries)
        for fragment in ("Table sales", "Table stock", "qty", "name"):
            assert fragment in context

    def test_descriptions_and_synonyms_appear(self, entries):
        entries[0].description = "Units sold per line"
        entries[0].synonyms = ["quantity", "units"]
        context = dictionary.schema_context(entries)
        assert "Units sold per line" in context
        assert "also called: quantity, units" in context

    def test_relationships_appear_as_joinable_pairs(self, entries):
        context = dictionary.schema_context(entries, [Relationship("sales", "sku", "stock", "sku")])
        assert "sales.sku = stock.sku" in context

    def test_it_says_something_useful_when_nothing_is_loaded(self):
        assert dictionary.schema_context([]) == "No tables are loaded."


class TestProgress:
    def test_described_and_total_are_counted(self, entries):
        entries[0].description = "Something"
        assert dictionary.describe_progress(entries) == (1, 3)

    def test_whitespace_is_not_a_description(self, entries):
        entries[0].description = "   "
        assert dictionary.describe_progress(entries) == (0, 3)
