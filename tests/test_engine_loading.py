"""Preparing uploads for DuckDB, reusing the Data Cleaner's readers and type detection."""

import io

import pandas as pd
import pytest

from engine import duckdb_session as ds
from engine import loading
from engine.exceptions import TableLoadError

SALARIES_CSV = b"emp_id,name,amount,joined\n007,Ana,1200.50,2024-01-03\n008,Bo,300,2024-02-11\n"


def _workbook(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


class TestPrepareTable:
    def test_a_csv_is_read_and_typed(self):
        frame, types = loading.prepare_table(SALARIES_CSV, "salaries.csv")
        assert list(frame.columns) == ["emp_id", "name", "amount", "joined"]
        assert types["amount"] == "numeric"
        assert types["joined"] == "date"

    def test_an_id_column_keeps_its_leading_zeros(self):
        """The whole reason files are read as text first: once pandas parses `007` as
        the integer 7, nothing downstream can restore it."""
        frame, types = loading.prepare_table(SALARIES_CSV, "salaries.csv")
        assert types["emp_id"] == "id"
        assert frame["emp_id"].tolist() == ["007", "008"]

    def test_leading_zeros_survive_the_whole_way_into_duckdb(self):
        frame, _ = loading.prepare_table(SALARIES_CSV, "salaries.csv")
        connection = ds.open_connection()
        try:
            ds.register_table(connection, "salaries", frame)
            assert ds.preview(connection, "salaries")["emp_id"].tolist() == ["007", "008"]
        finally:
            connection.close()

    def test_a_specific_excel_sheet_can_be_selected(self):
        workbook = _workbook(
            {"Jan": pd.DataFrame({"v": [1]}), "Feb": pd.DataFrame({"v": [2], "extra": ["x"]})}
        )
        frame, _ = loading.prepare_table(workbook, "book.xlsx", "Feb")
        assert list(frame.columns) == ["v", "extra"]

    def test_an_unreadable_file_raises_a_useful_error(self):
        with pytest.raises(TableLoadError):
            loading.prepare_table(b"not a workbook", "book.xlsx")

    def test_an_unsupported_type_raises(self):
        with pytest.raises(TableLoadError):
            loading.prepare_table(b"x", "old.xls")

    def test_an_all_text_table_still_returns_types(self):
        frame, types = loading.prepare_table(b"a,b\nx,y\n", "t.csv")
        assert types == {"a": "text", "b": "text"}
        assert len(frame) == 1


class TestPrepareCleanedFrame:
    def test_types_are_read_back_rather_than_re_detected(self):
        """The handoff path: the Data Cleaner already typed this, and re-detecting could
        disagree with the cleaning log the user just read."""
        frame = pd.DataFrame({"emp_id": ["007", "008"], "amount": [1.0, 2.0]})
        prepared, types = loading.prepare_cleaned_frame(frame, {"emp_id": "id"})
        assert types["emp_id"] == "id"
        assert types["amount"] == "numeric"
        assert prepared["emp_id"].tolist() == ["007", "008"]

    def test_a_declared_type_wins_over_detection(self):
        """pandas stores text, categorical and id identically, so without this a column
        of plain digits set to `id` would keep reporting as something else."""
        frame = pd.DataFrame({"code": ["1", "2", "3"]})
        _, types = loading.prepare_cleaned_frame(frame, {"code": "id"})
        assert types["code"] == "id"


class TestRenameColumns:
    """Requirement 8.1 step 5's manual remap, applied to the raw text before any typing."""

    def test_a_column_is_renamed_before_anything_has_been_typed(self):
        raw = pd.DataFrame({"emp_id": ["007"], "amount": ["1.5"]})
        renamed = loading.rename_columns(raw, {"emp_id": "employee_id"})
        assert list(renamed.columns) == ["employee_id", "amount"]

    def test_the_renamed_column_then_takes_the_declared_type(self):
        """The whole reason the rename happens here: renaming after the load would leave
        the column typed by detection under a name the recipe declares a type for."""
        raw = pd.DataFrame({"emp_id": ["007", "008"], "amount": ["1.5", "2.5"]})
        renamed = loading.rename_columns(raw, {"emp_id": "employee_id"})
        frame, types, outcome = loading.prepare_declared_table(
            renamed, {"employee_id": "id", "amount": "numeric"}
        )
        assert outcome.accepted
        assert types["employee_id"] == "id"
        assert frame["employee_id"].tolist() == ["007", "008"]

    def test_renaming_nothing_hands_the_frame_straight_back(self):
        raw = pd.DataFrame({"a": ["1"]})
        assert loading.rename_columns(raw, None) is raw
        assert loading.rename_columns(raw, {}) is raw

    def test_a_rename_naming_a_column_the_file_does_not_have_is_ignored(self):
        """The match report names the still-missing column far more usefully than a
        KeyError from inside pandas would."""
        raw = pd.DataFrame({"a": ["1"]})
        assert list(loading.rename_columns(raw, {"nope": "b"}).columns) == ["a"]

    def test_a_rename_onto_a_column_that_already_exists_is_ignored(self):
        """It would silently produce two columns of the same name, which every later
        lookup reads as one."""
        raw = pd.DataFrame({"a": ["1"], "b": ["2"]})
        assert list(loading.rename_columns(raw, {"a": "b"}).columns) == ["a", "b"]
