"""Measuring next month's upload against a saved pipeline.

The interesting half is `required_columns_by_table`. Section 6 of the requirements document
asks for "required columns present, not exact match", and the trap it hides is that a step
reading `sales` halfway down the pipeline may be reading a column an earlier step *created*
— demanding that of the uploaded file would refuse a perfectly good one.
"""

import pandas as pd
import pytest

from transform.matching import PipelineMatch, check_upload, required_columns_by_table
from transform.pipeline import make_step
from transform.template import capture
from transform.workspace import NamedFrame


def _upload(name: str, file_name: str, columns: list[str], sheet: str | None = None) -> NamedFrame:
    label = f"{file_name} - {sheet}" if sheet else file_name
    return NamedFrame(
        name=name,
        frame=pd.DataFrame({column: [] for column in columns}),
        origin="upload",
        source_label=label,
        upload_file_id=f"id-{name}",
    )


def _merge_step() -> dict:
    return make_step(
        "merge",
        {"left": "sales", "right": "customers"},
        {"left_on": ["customer_id"], "how": "left"},
        "new",
        "joined",
    )


def _filter_step(table: str, column: str, output: str = "") -> dict:
    return make_step(
        "filter_rows",
        {"source": table},
        {"column": column, "comparison": "is not empty"},
        "in_place",
        output or table,
    )


def _pipeline(steps, workspace=None):
    workspace = workspace or {
        "sales": _upload("sales", "sales.xlsx", ["customer_id", "amount"], "Sheet1"),
        "customers": _upload("customers", "customers.csv", ["customer_id", "customer"]),
    }
    return capture(
        "Monthly", upload_workspace=workspace, steps=steps, outputs=["joined"]
    )


def _uploaded(**tables: tuple[str, str | None, list[str]]) -> dict:
    return dict(tables)


#: What the two files held when the pipeline was saved. Passing this is the real usage:
#: it is the saved schema that tells a file's own column from one the steps create.
SAVED_SCHEMA = {
    "sales": ["customer_id", "amount"],
    "customers": ["customer_id", "customer"],
}


class TestRequiredColumns:
    def test_reports_each_role_against_the_table_that_role_names(self):
        required = required_columns_by_table([_merge_step()], SAVED_SCHEMA)
        assert required["sales"] == ["customer_id"]
        assert required["customers"] == ["customer_id"]

    def test_a_table_read_but_not_by_column_still_appears_with_nothing_required(self):
        """A `concat` reads whole tables. They are expected; they just constrain no column."""
        step = make_step(
            "concat", {"frames": ["jan", "feb"]}, {"add_source_column": False}, "new", "all_months"
        )
        required = required_columns_by_table(step and [step], {"jan": ["a"], "feb": ["a"]})
        assert required == {"jan": [], "feb": []}

    def test_duplicates_are_collapsed_in_first_seen_order(self):
        steps = [_filter_step("sales", "amount"), _filter_step("sales", "customer_id")]
        assert required_columns_by_table(steps, SAVED_SCHEMA)["sales"] == [
            "amount",
            "customer_id",
        ]

    def test_a_column_the_pipeline_creates_is_not_demanded_of_the_file(self):
        """The trap in the module docstring, asserted.

        Step 1 adds `bonus` to `sales`; step 2 filters on it. `bonus` must **not** be
        demanded of the uploaded file — the pipeline creates it. The saved schema is what
        says so: `bonus` wasn't in the file when the pipeline was saved.
        """
        add = make_step(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "bonus", "expression": "amount * 0.12"},
            "in_place",
            "sales",
        )
        required = required_columns_by_table(
            [add, _filter_step("sales", "bonus")], SAVED_SCHEMA
        )
        assert required["sales"] == ["amount"]
        assert "bonus" not in required["sales"]

    def test_a_formula_is_resolved_against_the_saved_schema(self):
        """`required_add_calculated_column` can't tokenize alone; here it can."""
        add = make_step(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "net", "expression": "amount - customer_id"},
            "in_place",
            "sales",
        )
        assert required_columns_by_table([add], SAVED_SCHEMA)["sales"] == [
            "amount",
            "customer_id",
        ]

    def test_a_formula_that_will_not_tokenize_contributes_nothing(self):
        """A formula naming a column the saved file never had can't be read for columns.

        It contributes nothing rather than blocking: the step itself refuses it at replay,
        with a message about the formula — far clearer than "a column is missing".
        """
        add = make_step(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "net", "expression": "mystery_column * 2"},
            "in_place",
            "sales",
        )
        assert required_columns_by_table([add], SAVED_SCHEMA)["sales"] == []

    def test_a_step_reading_a_table_an_earlier_step_built_is_not_a_file_requirement(self):
        steps = [_merge_step(), _filter_step("joined", "amount")]
        assert "joined" not in required_columns_by_table(steps, SAVED_SCHEMA)

    def test_without_a_schema_everything_the_steps_read_is_reported(self):
        """The unfiltered form, for describing a step list rather than validating a file."""
        steps = [_merge_step(), _filter_step("joined", "amount")]
        assert required_columns_by_table(steps)["joined"] == ["amount"]

    def test_an_operation_this_version_does_not_know_is_skipped_not_raised(self):
        """A pipeline from a newer app must still be describable — see `template.from_json`."""
        unknown = {
            "operation": "teleport_columns",
            "inputs": {"source": "sales"},
            "params": {},
            "output": {"mode": "in_place", "name": "sales"},
        }
        assert required_columns_by_table(
            [unknown, _filter_step("sales", "amount")], SAVED_SCHEMA
        ) == {"sales": ["amount"]}


class TestCheckUpload:
    def test_matches_a_file_by_name_whatever_the_table_is_called(self):
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline,
            _uploaded(
                sales_renamed=("sales.xlsx", "Sheet1", ["customer_id", "amount"]),
                anything=("customers.csv", None, ["customer_id", "customer"]),
            ),
        )
        assert match.ok
        assert match.matched == {"sales": "sales_renamed", "customers": "anything"}

    def test_matching_ignores_case_spacing_and_the_extension(self):
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline,
            _uploaded(
                a=("  SALES.csv ", "sheet1", ["customer_id", "amount"]),
                b=("Customers.XLSX", None, ["customer_id", "customer"]),
            ),
        )
        assert match.ok

    def test_a_missing_file_blocks_and_is_named(self):
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline, _uploaded(a=("sales.xlsx", "Sheet1", ["customer_id", "amount"]))
        )
        assert not match.ok
        assert match.missing == ["customers"]
        assert "customers" in " ".join(match.problems())

    def test_a_missing_required_column_blocks_and_names_both_sides(self):
        """Section 6.4's error, near enough word for word."""
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline,
            _uploaded(
                a=("sales.xlsx", "Sheet1", ["customer id", "amount"]),
                b=("customers.csv", None, ["customer_id", "customer"]),
            ),
        )
        assert not match.ok
        assert match.missing_columns == {"sales": ["customer_id"]}
        problem = " ".join(match.problems())
        assert "'customer_id'" in problem
        assert "'customer id'" in problem

    def test_extra_and_reordered_columns_are_allowed(self):
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline,
            _uploaded(
                a=("sales.xlsx", "Sheet1", ["amount", "note", "customer_id", "vat"]),
                b=("customers.csv", None, ["customer", "customer_id"]),
            ),
        )
        assert match.ok
        assert match.missing_columns == {}

    def test_an_extra_file_is_noted_never_discarded(self):
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline,
            _uploaded(
                a=("sales.xlsx", "Sheet1", ["customer_id", "amount"]),
                b=("customers.csv", None, ["customer_id", "customer"]),
                c=("budget.csv", None, ["target"]),
            ),
        )
        assert match.ok
        assert match.extra == ["c"]
        assert "budget" not in " ".join(match.problems())
        assert match.has_notes

    def test_the_same_file_uploaded_twice_keeps_the_first_and_lists_the_second(self):
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline,
            _uploaded(
                first=("sales.xlsx", "Sheet1", ["customer_id", "amount"]),
                second=("sales.xlsx", "Sheet1", ["customer_id", "amount"]),
                b=("customers.csv", None, ["customer_id", "customer"]),
            ),
        )
        assert match.matched["sales"] == "first"
        assert "second" in match.extra

    def test_summary_says_what_is_wrong(self):
        pipeline = _pipeline([_merge_step()])
        missing_file = check_upload(
            pipeline, _uploaded(a=("sales.xlsx", "Sheet1", ["customer_id"]))
        )
        assert "1 missing" in missing_file.summary()

        short_column = check_upload(
            pipeline,
            _uploaded(
                a=("sales.xlsx", "Sheet1", ["nope"]),
                b=("customers.csv", None, ["customer_id"]),
            ),
        )
        assert "required column(s) are missing" in short_column.summary()

    def test_an_exact_match_says_so(self):
        pipeline = _pipeline([_merge_step()])
        match = check_upload(
            pipeline,
            _uploaded(
                a=("sales.xlsx", "Sheet1", ["customer_id", "amount"]),
                b=("customers.csv", None, ["customer_id", "customer"]),
            ),
        )
        assert match.summary() == "2 expected file(s) matched this pipeline exactly."
        assert match.status_word() == "ready"

    def test_nothing_uploaded_is_a_report_not_an_exception(self):
        match = check_upload(_pipeline([_merge_step()]), {})
        assert not match.ok
        assert sorted(match.missing) == ["customers", "sales"]


class TestPipelineMatchDefaults:
    def test_an_empty_match_is_ready(self):
        assert PipelineMatch().ok
        assert PipelineMatch().notes() == []
