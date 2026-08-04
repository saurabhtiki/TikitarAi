import numpy as np
import pandas as pd
import pytest

from cleaner.export import MAX_EXCEL_CELL_CHARS, build_workbook
from cleaner.exceptions import ExportError
from cleaner.loaders import list_sheet_names, read_table
from cleaner.naming import CLEANING_LOG_SHEET_NAME


def test_every_table_becomes_a_sheet_in_order_plus_the_log():
    payload = build_workbook(
        [("Employees", pd.DataFrame({"a": [1]})), ("Salaries", pd.DataFrame({"b": [2]}))],
        {"Employees": ["Trimmed whitespace"], "Salaries": []},
    )

    assert list_sheet_names(payload, "out.xlsx") == ["Employees", "Salaries", CLEANING_LOG_SHEET_NAME]


def test_sheet_contents_round_trip():
    frame = pd.DataFrame({"city": ["Delhi", "Mumbai"], "amount": [10.5, 20.0]})
    payload = build_workbook([("Sales", frame)], {"Sales": []})

    restored = read_table(payload, "out.xlsx", "Sales")

    assert list(restored["city"]) == ["Delhi", "Mumbai"]
    # Read back through the all-text loader, so compare numerically rather than on
    # formatting — Excel stores 20.0 and hands it back as "20".
    assert list(pd.to_numeric(restored["amount"])) == [10.5, 20.0]


def test_missing_values_are_written_as_empty_cells():
    payload = build_workbook([("Sales", pd.DataFrame({"a": [1.0, np.nan], "b": ["x", "y"]}))], {"Sales": []})
    restored = read_table(payload, "out.xlsx", "Sales")

    assert pd.isna(restored.loc[1, "a"])
    assert restored.loc[1, "b"] == "y"


def test_the_log_sheet_records_each_step_in_order():
    payload = build_workbook(
        [("Sales", pd.DataFrame({"a": [1]}))],
        {"Sales": ["Skipped 2 row(s) from the top", "Trimmed whitespace from all text columns"]},
    )
    log = read_table(payload, "out.xlsx", CLEANING_LOG_SHEET_NAME)

    assert list(log["Step"]) == ["1", "2"]
    assert "Skipped 2 row(s) from the top" in list(log["Action"])


def test_a_table_with_no_steps_says_so_in_the_log():
    payload = build_workbook([("Sales", pd.DataFrame({"a": [1]}))], {"Sales": []})
    log = read_table(payload, "out.xlsx", CLEANING_LOG_SHEET_NAME)

    assert "No cleaning steps were applied." in list(log["Action"])


def test_exporting_nothing_raises():
    with pytest.raises(ExportError, match="nothing to export"):
        build_workbook([], {})


def test_an_oversized_cell_is_rejected_before_writing():
    """Checked up front so the user gets a specific message instead of xlsxwriter failing
    opaquely part-way through a large workbook."""
    frame = pd.DataFrame({"a": ["x" * (MAX_EXCEL_CELL_CHARS + 1)]})

    with pytest.raises(ExportError, match="per cell"):
        build_workbook([("Sales", frame)], {"Sales": []})


def test_an_empty_table_still_exports():
    payload = build_workbook([("Sales", pd.DataFrame({"a": pd.Series(dtype="string")}))], {"Sales": []})

    assert list_sheet_names(payload, "out.xlsx") == ["Sales", CLEANING_LOG_SHEET_NAME]
