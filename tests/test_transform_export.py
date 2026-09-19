"""Building the downloadable workbook.

Asserted by reading the bytes back through `cleaner.loaders`, rather than by poking at
xlsxwriter internals — the same approach `test_cleaner_export.py` takes, and the one that
actually proves the file opens.
"""

import pandas as pd
import pytest

from cleaner.loaders import list_sheet_names, read_table
from cleaner.naming import CLEANING_LOG_SHEET_NAME
from transform.exceptions import TransformExportError
from transform.export import build_transform_workbook
from transform.pipeline import make_step
from transform.workspace import NamedFrame


@pytest.fixture
def workspace() -> dict[str, NamedFrame]:
    return {
        "sales": NamedFrame(
            name="sales", frame=pd.DataFrame({"customer": ["Acme"], "amount": ["10"]})
        ),
        "customers": NamedFrame(name="customers", frame=pd.DataFrame({"customer": ["Acme"]})),
        "customer_summary": NamedFrame(
            name="customer_summary", frame=pd.DataFrame({"customer": ["Acme"], "total": [10]})
        ),
    }


def sheet_names(workbook: bytes) -> list[str]:
    return list_sheet_names(workbook, "download.xlsx")


class TestWhatGoesIn:
    def test_each_chosen_table_becomes_a_sheet(self, workspace):
        workbook = build_transform_workbook(workspace, ["sales", "customer_summary"])
        assert "sales" in sheet_names(workbook)
        assert "customer_summary" in sheet_names(workbook)

    def test_a_table_that_was_not_chosen_is_left_out(self, workspace):
        workbook = build_transform_workbook(workspace, ["customer_summary"])
        assert "sales" not in sheet_names(workbook)

    def test_the_sheets_come_out_in_the_order_they_were_chosen(self, workspace):
        workbook = build_transform_workbook(workspace, ["customer_summary", "sales"])
        names = [name for name in sheet_names(workbook) if name != CLEANING_LOG_SHEET_NAME]
        assert names == ["customer_summary", "sales"]

    def test_the_data_survives_the_round_trip(self, workspace):
        workbook = build_transform_workbook(workspace, ["customer_summary"])
        restored = read_table(workbook, "download.xlsx", sheet_name="customer_summary")
        assert restored["customer"].tolist() == ["Acme"]
        assert restored["total"].tolist() == ["10"]

    def test_a_table_is_found_regardless_of_how_its_name_was_typed(self, workspace):
        workbook = build_transform_workbook(workspace, ["SALES"])
        assert "SALES" in sheet_names(workbook)


class TestTheStepsAreRecorded:
    def test_the_workbook_carries_a_sheet_listing_the_steps(self, workspace):
        step = make_step(
            "groupby_aggregate",
            {"source": "sales"},
            {"group_by": ["customer"], "value_columns": ["amount"], "aggregation": "sum"},
            "new",
            "customer_summary",
        )
        workbook = build_transform_workbook(workspace, ["customer_summary"], [step])
        log = read_table(workbook, "download.xlsx", sheet_name=CLEANING_LOG_SHEET_NAME)
        assert "Grouped by customer" in log.to_string()

    def test_a_pipeline_with_no_steps_says_so(self, workspace):
        workbook = build_transform_workbook(workspace, ["sales"], [])
        log = read_table(workbook, "download.xlsx", sheet_name=CLEANING_LOG_SHEET_NAME)
        assert "No steps were added" in log.to_string()


class TestRefusals:
    def test_choosing_nothing_is_refused(self, workspace):
        with pytest.raises(TransformExportError, match="at least one table"):
            build_transform_workbook(workspace, [])

    def test_a_table_that_has_gone_is_named_rather_than_silently_skipped(self, workspace):
        with pytest.raises(TransformExportError, match="vanished_table"):
            build_transform_workbook(workspace, ["sales", "vanished_table"])
