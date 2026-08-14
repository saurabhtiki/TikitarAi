"""The grid behind a table agenda item (requirement 6.7, Phase 2, spec 3a).

Two things here decide whether the feature is trustworthy rather than merely present.

**Values stay text.** A column of bill numbers that pandas reads as integers comes back as
`1001.0` the moment one row is blank, and the invitee is then reconciling against a reference
number that doesn't match their own records.

**A row's identity is its position.** Every completion count, every comparison column and
every stored answer is keyed on it, so a shift of one silently re-attributes an answer to a
different bill.
"""

from io import BytesIO

import pandas as pd
import pytest

from meetings.exceptions import MeetingStorageError
from meetings.model import AgendaTable
from meetings.tables import (
    MAX_ROWS,
    base_data_from_frame,
    completion,
    completion_label,
    display_frame,
    format_completion,
    read_source,
    responses_from_frame,
)


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def _table() -> AgendaTable:
    return AgendaTable(
        table_id=1,
        item_ref="Outstanding bills",
        locked_columns=["Bill No", "Amount"],
        editable_columns=["Expected date", "Remarks"],
        base_data=[
            {"Bill No": "B-1", "Amount": "100"},
            {"Bill No": "B-2", "Amount": "250"},
            {"Bill No": "B-3", "Amount": "75"},
        ],
    )


class TestReadSource:
    def test_a_csv_becomes_a_frame(self):
        frame = read_source(_csv("Bill No,Amount\nB-1,100\nB-2,250\n"), "bills.csv")

        assert list(frame.columns) == ["Bill No", "Amount"]
        assert len(frame) == 2

    def test_every_value_comes_back_as_text(self):
        # An integer column with one blank row is what turns "1001" into "1001.0".
        frame = read_source(_csv("Bill No,Amount\n1001,100\n1002,\n"), "bills.csv")

        assert frame["Bill No"].tolist() == ["1001", "1002"]
        assert frame["Amount"].tolist() == ["100", ""]

    def test_an_xlsx_is_read_too(self, tmp_path):
        buffer = BytesIO()
        pd.DataFrame({"Bill No": ["B-1"], "Amount": ["100"]}).to_excel(buffer, index=False)

        frame = read_source(buffer.getvalue(), "bills.xlsx")
        assert frame["Bill No"].tolist() == ["B-1"]

    def test_column_names_are_stripped(self):
        frame = read_source(_csv(" Bill No , Amount \nB-1,100\n"), "bills.csv")
        assert list(frame.columns) == ["Bill No", "Amount"]

    def test_an_unsupported_file_type_is_refused_by_name(self):
        with pytest.raises(MeetingStorageError, match="supported table file"):
            read_source(b"anything", "bills.pdf")

    def test_a_sheet_with_no_rows_is_refused(self):
        with pytest.raises(MeetingStorageError, match="no rows"):
            read_source(_csv("Bill No,Amount\n"), "bills.csv")

    def test_a_sheet_too_big_to_be_an_agenda_item_is_refused(self):
        rows = "\n".join(f"B-{index},100" for index in range(MAX_ROWS + 1))
        with pytest.raises(MeetingStorageError, match="more than"):
            read_source(_csv(f"Bill No,Amount\n{rows}\n"), "export.csv")

    def test_an_unreadable_file_raises_rather_than_returning_nothing(self):
        with pytest.raises(MeetingStorageError):
            read_source(b"\x00\x01not really a spreadsheet", "bills.xlsx")


class TestBaseData:
    def test_a_frame_becomes_row_dicts(self):
        frame = read_source(_csv("Bill No,Amount\nB-1,100\n"), "bills.csv")
        assert base_data_from_frame(frame) == [{"Bill No": "B-1", "Amount": "100"}]


class TestDisplayFrame:
    def test_locked_columns_come_first_and_hold_the_creators_values(self):
        frame = display_frame(_table(), {})

        assert list(frame.columns) == ["Bill No", "Amount", "Expected date", "Remarks"]
        assert frame["Bill No"].tolist() == ["B-1", "B-2", "B-3"]

    def test_saved_answers_come_back_on_their_own_rows(self):
        frame = display_frame(_table(), {1: {"Expected date": "2026-09-01"}})

        assert frame["Expected date"].tolist() == ["", "2026-09-01", ""]

    def test_the_locked_values_are_always_the_creators(self):
        # Even if a stored response somehow names a locked column, the grid shows the sheet.
        frame = display_frame(_table(), {0: {"Amount": "999", "Remarks": "ok"}})

        assert frame["Amount"].tolist() == ["100", "250", "75"]
        assert frame["Remarks"].tolist() == ["ok", "", ""]

    def test_an_empty_sheet_still_produces_the_right_columns(self):
        table = _table()
        table.base_data = []

        assert list(display_frame(table, {}).columns) == [
            "Bill No",
            "Amount",
            "Expected date",
            "Remarks",
        ]


class TestResponsesFromFrame:
    def test_only_editable_columns_are_read_back(self):
        table = _table()
        edited = display_frame(table, {})
        edited.loc[0, "Amount"] = "999"
        edited.loc[0, "Remarks"] = "Paid"

        assert responses_from_frame(table, edited) == {0: {"Remarks": "Paid"}}

    def test_a_row_with_nothing_filled_in_is_not_stored(self):
        table = _table()
        assert responses_from_frame(table, display_frame(table, {})) == {}

    def test_whitespace_only_does_not_count_as_an_answer(self):
        table = _table()
        edited = display_frame(table, {})
        edited.loc[1, "Remarks"] = "   "

        assert responses_from_frame(table, edited) == {}

    def test_rows_beyond_the_sheet_are_ignored(self):
        # The grid is fixed-size; extra rows mean something upstream went wrong, and
        # inventing answers to rows the creator never asked about is the worse failure.
        table = _table()
        edited = display_frame(table, {})
        edited.loc[len(edited)] = ["B-9", "1", "2026-01-01", "Extra"]

        assert set(responses_from_frame(table, edited)) <= {0, 1, 2}

    def test_a_grid_with_nothing_editable_reads_back_nothing(self):
        table = _table()
        table.editable_columns = []

        assert responses_from_frame(table, display_frame(table, {})) == {}


class TestCompletion:
    def test_progress_is_counted_in_rows(self):
        assert completion(_table(), 1) == (1, 3, 33)

    def test_a_full_grid_is_a_hundred_percent(self):
        assert completion(_table(), 3) == (3, 3, 100)

    def test_an_empty_grid_reads_as_zero_rather_than_complete(self):
        # A table with no rows is a setup that isn't finished. Calling it done would put a
        # tick against an item nobody could have answered.
        table = _table()
        table.base_data = []

        assert completion(table, 0) == (0, 0, 0)

    def test_a_count_larger_than_the_sheet_is_clamped(self):
        assert completion(_table(), 99) == (3, 3, 100)

    def test_the_label_is_the_same_wording_everywhere(self):
        # The status list, the MoM and the matrix must not quote three phrasings of one number.
        assert completion_label(_table(), 1) == format_completion(1, 3)
        assert "1 of 3 row(s) filled (33%)" == completion_label(_table(), 1)
