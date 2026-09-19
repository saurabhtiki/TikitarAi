"""Saved transform pipelines: what capture records, and the JSON round trip.

Shaped like `test_cleaner_db.py` and `test_cleaner_template.py`, whose modules these are
adapted from. The ownership assertions matter for the same reason: a pipeline names a
company's actual files and the columns inside them, so one readable by the wrong account is
a leak rather than an inconvenience.
"""

import json

import pandas as pd
import pytest

from auth.db import create_user, init_db, seed_default_admin
from transform.db import (
    delete_pipeline,
    init_transform_pipelines_table,
    list_pipelines,
    load_pipeline,
    save_pipeline,
)
from transform.exceptions import PipelineStorageError
from transform.pipeline import make_step
from transform.template import (
    SAVED_PIPELINE_VERSION,
    PipelineTable,
    SavedPipeline,
    capture,
    from_json,
    normalise,
    source_key,
    to_json,
)
from transform.workspace import NamedFrame


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    seed_default_admin(path)
    create_user("second@example.com", "Second", "password123", "normal_user", path)
    init_transform_pipelines_table(path)
    return path


def _workspace() -> dict[str, NamedFrame]:
    return {
        "sales": NamedFrame(
            name="sales",
            frame=pd.DataFrame({"customer_id": [1], "amount": [10]}),
            origin="upload",
            source_label="sales.xlsx - Sheet1",
            upload_file_id="upload-abc",
        ),
        "customers": NamedFrame(
            name="customers",
            frame=pd.DataFrame({"customer_id": [1], "customer": ["Acme"]}),
            origin="upload",
            source_label="customers.csv",
            upload_file_id="upload-def",
        ),
        "joined": NamedFrame(
            name="joined",
            frame=pd.DataFrame({"customer_id": [1]}),
            origin="step",
            source_label="step 1: Join tables",
            created_by_step=0,
        ),
    }


def _steps() -> list:
    return [
        make_step(
            "merge",
            {"left": "sales", "right": "customers"},
            {"left_on": ["customer_id"], "how": "left"},
            "new",
            "joined",
        )
    ]


def _pipeline(name="Monthly sales") -> SavedPipeline:
    return capture(
        name,
        description="Joins sales to the customer master.",
        upload_workspace=_workspace(),
        steps=_steps(),
        outputs=["joined"],
    )


class TestCapture:
    def test_records_only_uploaded_tables(self):
        pipeline = _pipeline()
        assert pipeline.table_names() == ["sales", "customers"]

    def test_splits_the_source_label_back_into_file_and_sheet(self):
        pipeline = _pipeline()
        sales = pipeline.table("sales")
        assert (sales.file_name, sales.sheet_name) == ("sales.xlsx", "Sheet1")
        assert pipeline.table("customers").sheet_name is None

    def test_records_the_columns_each_file_had(self):
        assert _pipeline().table("sales").columns == ["customer_id", "amount"]

    def test_keeps_the_steps_in_order_and_the_chosen_outputs(self):
        pipeline = _pipeline()
        assert [step["operation"] for step in pipeline.steps] == ["merge"]
        assert pipeline.outputs == ["joined"]

    def test_copies_the_steps_so_later_edits_cannot_reach_back(self):
        steps = _steps()
        pipeline = capture(
            "x", upload_workspace=_workspace(), steps=steps, outputs=[]
        )
        steps[0]["params"]["left_on"] = ["something_else"]
        assert pipeline.steps[0]["params"]["left_on"] == ["customer_id"]

    def test_summary_line_counts_what_it_holds(self):
        assert _pipeline().summary_line() == "2 table(s) - 1 step(s) - 1 output(s)"

    def test_an_unnamed_pipeline_still_has_something_to_call_it(self):
        assert capture("  ", upload_workspace={}, steps=[], outputs=[]).display_name() == (
            "Untitled pipeline"
        )


class TestSourceKey:
    def test_drops_the_extension_so_a_csv_resaved_as_xlsx_still_matches(self):
        assert source_key("sales.csv", None) == source_key("sales.xlsx", None)

    def test_keeps_the_sheet_apart_from_the_file(self):
        assert source_key("book.xlsx", "Q1") == "book - Q1"

    def test_comparison_ignores_case_and_spacing(self):
        assert normalise("  Sales   Data ") == normalise("sales data")


class TestSerialisation:
    def test_round_trips(self):
        pipeline = _pipeline()
        restored = from_json(to_json(pipeline), name=pipeline.name)
        assert restored.table_names() == pipeline.table_names()
        assert restored.steps == pipeline.steps
        assert restored.outputs == pipeline.outputs
        assert restored.table("sales").columns == ["customer_id", "amount"]

    def test_a_step_this_version_does_not_know_is_carried_through_not_refused(self):
        """A pipeline saved by a newer app is still readable, so its owner can see it.

        The replay already reports a step it cannot run, and that is where someone is
        looking when they wonder why a pipeline stopped — a refused load would leave them
        with no way to see what the pipeline even contains.
        """
        payload = json.loads(to_json(_pipeline()))
        payload["steps"].append(
            {"operation": "teleport_columns", "inputs": {"source": "sales"}, "params": {},
             "output": {"mode": "new", "name": "beamed"}}
        )
        restored = from_json(json.dumps(payload))
        assert [step["operation"] for step in restored.steps] == ["merge", "teleport_columns"]

    def test_a_step_with_no_operation_is_dropped(self):
        payload = json.loads(to_json(_pipeline()))
        payload["steps"].append({"inputs": {}, "params": {}})
        assert len(from_json(json.dumps(payload)).steps) == 1

    def test_a_newer_version_is_refused_rather_than_misread(self):
        payload = json.loads(to_json(_pipeline()))
        payload["version"] = SAVED_PIPELINE_VERSION + 1
        with pytest.raises(PipelineStorageError, match="newer version"):
            from_json(json.dumps(payload))

    def test_text_that_is_not_json_is_refused(self):
        with pytest.raises(PipelineStorageError, match="valid JSON"):
            from_json("not json at all")

    def test_json_that_is_not_an_object_is_refused(self):
        with pytest.raises(PipelineStorageError, match="expected format"):
            from_json("[1, 2, 3]")

    def test_an_unnamed_table_is_dropped(self):
        payload = json.loads(to_json(_pipeline()))
        payload["tables"].append({"name": "   ", "file_name": "ghost.csv"})
        assert len(from_json(json.dumps(payload)).tables) == 2


class TestStorage:
    def test_save_then_load_round_trips(self, db_path):
        saved = save_pipeline(1, _pipeline(), db_path)
        assert saved.pipeline_id is not None

        loaded = load_pipeline(saved.pipeline_id, 1, db_path)
        assert loaded.name == "Monthly sales"
        assert loaded.description == "Joins sales to the customer master."
        assert loaded.table_names() == ["sales", "customers"]
        assert loaded.steps == saved.steps

    def test_saving_under_a_name_already_used_updates_that_one(self, db_path):
        """Re-saving after adding one more step is the normal way to use this."""
        first = save_pipeline(1, _pipeline(), db_path)
        again = capture(
            "monthly sales",  # different case on purpose
            upload_workspace=_workspace(),
            steps=_steps() * 2,
            outputs=["joined"],
        )
        second = save_pipeline(1, again, db_path)

        assert second.pipeline_id == first.pipeline_id
        assert len(list_pipelines(1, db_path)) == 1
        assert len(load_pipeline(first.pipeline_id, 1, db_path).steps) == 2

    def test_a_blank_name_is_refused(self, db_path):
        with pytest.raises(PipelineStorageError, match="name"):
            save_pipeline(1, _pipeline("   "), db_path)

    def test_listing_leaves_out_the_json(self, db_path):
        save_pipeline(1, _pipeline(), db_path)
        row = list_pipelines(1, db_path)[0]
        assert "pipeline_json" not in row
        assert row["name"] == "Monthly sales"

    def test_one_account_cannot_load_anothers(self, db_path):
        saved = save_pipeline(1, _pipeline(), db_path)
        with pytest.raises(PipelineStorageError, match="No transform pipeline"):
            load_pipeline(saved.pipeline_id, 2, db_path)

    def test_one_account_cannot_delete_anothers(self, db_path):
        saved = save_pipeline(1, _pipeline(), db_path)
        with pytest.raises(PipelineStorageError, match="No transform pipeline"):
            delete_pipeline(saved.pipeline_id, 2, db_path)
        assert len(list_pipelines(1, db_path)) == 1

    def test_two_accounts_may_each_have_a_pipeline_of_the_same_name(self, db_path):
        save_pipeline(1, _pipeline(), db_path)
        save_pipeline(2, _pipeline(), db_path)
        assert len(list_pipelines(1, db_path)) == 1
        assert len(list_pipelines(2, db_path)) == 1

    def test_delete_removes_it(self, db_path):
        saved = save_pipeline(1, _pipeline(), db_path)
        delete_pipeline(saved.pipeline_id, 1, db_path)
        assert list_pipelines(1, db_path) == []

    def test_creating_the_table_twice_is_safe(self, tmp_path):
        path = tmp_path / "twice.db"
        init_db(path)
        seed_default_admin(path)
        init_transform_pipelines_table(path)
        init_transform_pipelines_table(path)
        assert list_pipelines(1, path) == []


class TestPipelineTable:
    def test_is_matched_on_its_file_not_its_table_name(self):
        """Renaming the table on screen must not stop next month's file matching."""
        table = PipelineTable(name="renamed_by_user", file_name="sales.xlsx", sheet_name="Sheet1")
        assert table.key() == normalise("sales - Sheet1")

    def test_falls_back_to_its_name_when_no_file_was_recorded(self):
        assert PipelineTable(name="sales").key() == "sales"
