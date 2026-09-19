"""The named-table workspace: naming rules, uniqueness, and the no-mutation guarantee."""

import pandas as pd
import pytest

from transform.exceptions import DuplicateFrameNameError, FrameNotFoundError
from transform.workspace import (
    NamedFrame,
    columns_of,
    drop_frame,
    frame_names,
    get_dataframe,
    get_frame,
    has_frame,
    name_key,
    normalise_frame_name,
    put_frame,
    rename_frame,
    suggest_frame_name,
    uploads_only,
)


def make_frame(name: str, origin: str = "upload") -> NamedFrame:
    return NamedFrame(name=name, frame=pd.DataFrame({"a": [1, 2]}), origin=origin)


class TestNaming:
    def test_forbidden_excel_characters_become_underscores(self):
        assert normalise_frame_name("Q1: North/South") == "Q1_ North_South"

    def test_a_long_name_is_clamped_to_excels_limit(self):
        assert len(normalise_frame_name("x" * 60)) == 31

    def test_whitespace_collapses_and_is_stripped(self):
        assert normalise_frame_name("  sales   data  ") == "sales data"

    def test_an_unnameable_table_falls_back_rather_than_failing(self):
        assert normalise_frame_name("") == "Table"
        assert normalise_frame_name("   ") == "Table"

    def test_names_are_compared_without_regard_to_case(self):
        assert name_key("Sales") == name_key("sales")


class TestSuggestFrameName:
    def test_a_free_name_is_returned_unchanged(self):
        assert suggest_frame_name("sales", []) == "sales"

    def test_duplicates_gain_an_underscore_and_a_number(self):
        taken = ["sales"]
        second = suggest_frame_name("sales", taken)
        assert second == "sales_2"
        assert suggest_frame_name("sales", [*taken, second]) == "sales_3"

    def test_a_collision_is_detected_regardless_of_case(self):
        assert suggest_frame_name("Sales", ["sales"]) == "Sales_2"

    def test_a_suffixed_name_still_fits_excels_limit(self):
        base = "y" * 31
        assert len(suggest_frame_name(base, [base])) == 31


class TestPutAndGet:
    def test_putting_a_frame_does_not_change_the_original_workspace(self):
        original = {"sales": make_frame("sales")}
        updated = put_frame(original, make_frame("customers"))

        assert frame_names(original) == ["sales"]
        assert frame_names(updated) == ["sales", "customers"]

    def test_replacing_a_frame_keeps_its_position(self):
        workspace = {"a": make_frame("a"), "b": make_frame("b"), "c": make_frame("c")}
        replaced = put_frame(workspace, NamedFrame(name="b", frame=pd.DataFrame({"z": [9]})))

        assert frame_names(replaced) == ["a", "b", "c"]
        assert list(replaced["b"].frame.columns) == ["z"]

    def test_a_new_table_refuses_to_overwrite_an_existing_name(self):
        workspace = {"sales": make_frame("sales")}
        with pytest.raises(DuplicateFrameNameError, match="already exists"):
            put_frame(workspace, make_frame("sales"), replace=False)

    def test_get_frame_finds_a_table_regardless_of_case(self):
        workspace = {"Sales": make_frame("Sales")}
        assert get_frame(workspace, "sales").name == "Sales"

    def test_a_missing_table_names_what_is_available(self):
        workspace = {"sales": make_frame("sales")}
        with pytest.raises(FrameNotFoundError, match="sales"):
            get_dataframe(workspace, "nowhere")


class TestDropAndRename:
    def test_dropping_a_missing_table_is_silent(self):
        workspace = {"sales": make_frame("sales")}
        assert frame_names(drop_frame(workspace, "nothing")) == ["sales"]

    def test_renaming_keeps_the_tables_position(self):
        workspace = {"a": make_frame("a"), "b": make_frame("b"), "c": make_frame("c")}
        renamed = rename_frame(workspace, "b", "middle")
        assert frame_names(renamed) == ["a", "middle", "c"]

    def test_renaming_onto_a_taken_name_is_refused(self):
        workspace = {"a": make_frame("a"), "b": make_frame("b")}
        with pytest.raises(DuplicateFrameNameError):
            rename_frame(workspace, "a", "b")

    def test_a_table_can_be_renamed_to_a_different_spelling_of_itself(self):
        workspace = {"sales": make_frame("sales")}
        assert frame_names(rename_frame(workspace, "sales", "Sales")) == ["Sales"]


class TestHelpers:
    def test_columns_of_a_missing_table_is_empty_rather_than_an_error(self):
        assert columns_of({}, "nothing") == []

    def test_has_frame_is_case_insensitive(self):
        assert has_frame({"Sales": make_frame("Sales")}, "sales")

    def test_uploads_only_leaves_out_tables_that_steps_created(self):
        workspace = {
            "sales": make_frame("sales", origin="upload"),
            "summary": make_frame("summary", origin="step"),
        }
        assert frame_names(uploads_only(workspace)) == ["sales"]

    def test_shape_label_reads_as_a_sentence(self):
        single = NamedFrame(name="one", frame=pd.DataFrame({"a": [1]}))
        assert single.shape_label == "1 row x 1 column"
        many = NamedFrame(name="many", frame=pd.DataFrame({"a": [1, 2], "b": [3, 4]}))
        assert many.shape_label == "2 rows x 2 columns"
