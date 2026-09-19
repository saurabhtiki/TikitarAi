"""AppTest coverage for the Transform Data page.

The add/edit dialog is driven through its session-state open flag rather than by clicking
the button, which is exactly why the page uses that idiom: with the button idiom the dialog
closes on the next rerun and its widgets vanish. See `transform.session.open_dialog`.
"""

import io
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_pages import saved_picker
from auth.db import init_db, seed_default_admin
from cleaner.loaders import list_sheet_names
from engine import session as engine_session
from llm import session as llm_session
from llm.db import init_llm_table
from transform import ai_parse
from transform import session
from transform.db import init_transform_pipelines_table, list_pipelines, load_pipeline
from transform.pipeline import make_step, step_headline
from transform.registry import get_operation

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRANSFORM_PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "transform_data.py")

SALES_CSV = b"customer_id,amount\n1,100\n1,200\n2,300\n"
CUSTOMERS_CSV = b"customer_id,customer\n1,Acme\n2,Bolt\n"

#: A file with a title row and a blank row above its real headers, so pandas reads the
#: title as the header and calls the rest `Unnamed: 1`, `Unnamed: 2`.
JUNK_CSV = b"Sales report,,\n,,\ninvoice_no,customer,amount\nINV-1,Acme,100\nINV-2,Bolt,200\n"


def _workbook(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def _make_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    init_transform_pipelines_table()
    app = AppTest.from_file(TRANSFORM_PAGE_PATH, default_timeout=30)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.run()
    return app


def _upload(app, *files: tuple[str, bytes]):
    """Puts files in the uploader. They are not read until Load is pressed."""
    app.file_uploader(key=session.TF_UPLOADER_KEY).set_value(
        [(name, payload, "application/octet-stream") for name, payload in files]
    )
    app.run()
    return app


def _load(app):
    """Presses the Load gate."""
    app.button(key="tf_load_files").click().run()
    return app


def _upload_and_load(app, *files: tuple[str, bytes]):
    _upload(app, *files)
    return _load(app)


def _state(app, key, fallback):
    """AppTest's session state has no `.get`, so absence is checked with `in`."""
    return app.session_state[key] if key in app.session_state else fallback


def _table_names(app) -> list[str]:
    return list(_state(app, session.TF_TABLE_NAMES_KEY, {}).values())


def _steps(app) -> list:
    return _state(app, session.TF_STEPS_KEY, [])


def _join_step() -> dict:
    return make_step(
        "merge",
        {"left": "sales", "right": "customers"},
        {"left_on": ["customer_id"], "join_type": "keep all rows from the left table"},
        "new",
        "merged_data",
    )


class TestTheEmptyPage:
    def test_it_opens_without_an_exception(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert not app.exception

    def test_it_says_what_to_do_first(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert any("Upload a CSV or Excel file" in info.value for info in app.info)

    def test_the_uploader_is_there(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert app.file_uploader(key=session.TF_UPLOADER_KEY) is not None


class TestTheLoadGate:
    def test_a_dropped_file_is_not_read_until_load_is_pressed(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert _table_names(app) == []
        assert app.button(key="tf_load_files") is not None

    def test_pressing_load_turns_the_file_into_a_table(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert _table_names(app) == ["sales"]
        assert not app.exception

    def test_the_gate_disappears_once_everything_is_loaded(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        with pytest.raises(KeyError):
            app.button(key="tf_load_files")


class TestNamingUploadedTables:
    def test_two_files_become_two_named_tables(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        assert sorted(_table_names(app)) == ["customers", "sales"]

    def test_two_files_with_the_same_name_are_told_apart(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("sales.csv", CUSTOMERS_CSV),
        )
        assert sorted(_table_names(app)) == ["sales", "sales_2"]

    def test_each_sheet_of_a_workbook_becomes_its_own_table(self, tmp_path, monkeypatch):
        book = _workbook(
            {"Jan": pd.DataFrame({"a": [1]}), "Feb": pd.DataFrame({"a": [2]})}
        )
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("months.xlsx", book))
        assert sorted(_table_names(app)) == ["months Feb", "months Jan"]

    def test_changing_what_is_in_the_uploader_drops_the_old_tables(self, tmp_path, monkeypatch):
        """Reconciliation runs in both directions, so a table whose file has gone goes too.

        AppTest mints a fresh upload id every time the uploader's value is set, so it
        cannot express "remove one of two files" the way a browser does — every file looks
        new. What it does show is the half that matters here: tables whose file is no
        longer in the uploader are dropped rather than left behind.
        """
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        assert sorted(_table_names(app)) == ["customers", "sales"]

        app.file_uploader(key=session.TF_UPLOADER_KEY).set_value(
            [("sales.csv", SALES_CSV, "application/octet-stream")]
        )
        app.run()
        assert _table_names(app) == []

        _load(app)
        assert _table_names(app) == ["sales"]


class TestTheStepList:
    def test_a_step_appears_in_the_list_with_its_number(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.run()

        assert not app.exception
        assert any("1. Joined sales with customers" in item.value for item in app.markdown)

    def test_the_step_creates_its_new_table(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.run()
        assert any(tab.label == "merged_data" for tab in app.tabs)

    def test_only_the_last_step_offers_edit_and_delete(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        second = make_step(
            "sort_rows", {"source": "merged_data"}, {"columns": ["amount"]}, "in_place", "merged_data"
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step(), second]
        app.run()

        assert app.button(key="tf_edit_1") is not None
        assert app.button(key="tf_delete_1") is not None
        with pytest.raises(KeyError):
            app.button(key="tf_edit_0")

    def test_an_earlier_step_says_why_it_cannot_be_changed(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        second = make_step(
            "sort_rows", {"source": "merged_data"}, {"columns": ["amount"]}, "in_place", "merged_data"
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step(), second]
        app.run()
        assert any("Only the last step can be changed" in item.value for item in app.caption)


class TestDeletingAStep:
    def test_the_confirmation_warns_that_a_table_goes_with_it(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.session_state[session.TF_DIALOG_KEY] = "delete"
        app.run()
        assert any("merged_data" in warning.value for warning in app.warning)

    def test_confirming_removes_the_step_and_its_table(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.session_state[session.TF_DIALOG_KEY] = "delete"
        app.run()
        app.button(key="tf_delete_confirm").click().run()

        assert _steps(app) == []
        assert not any(tab.label == "merged_data" for tab in app.tabs)

    def test_keeping_it_changes_nothing(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.session_state[session.TF_DIALOG_KEY] = "delete"
        app.run()
        app.button(key="tf_delete_cancel").click().run()

        assert len(_steps(app)) == 1


class TestTheAddStepDialog:
    def test_it_offers_the_operation_categories(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[session.TF_DIALOG_KEY] = "add"
        app.run()

        assert not app.exception
        assert "Combine tables" in app.selectbox(key="tf_pick_category").options

    def test_it_draws_the_chosen_operations_own_form(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[session.TF_DIALOG_KEY] = "add"
        app.session_state["tf_pick_category"] = "Filter & sort"
        app.run()

        assert app.selectbox(key="tf_form_comparison") is not None
        assert not app.exception

    def test_the_column_picker_offers_the_real_columns(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[session.TF_DIALOG_KEY] = "add"
        app.session_state["tf_pick_category"] = "Filter & sort"
        app.session_state["tf_form_input_source"] = "sales"
        app.run()

        assert app.selectbox(key="tf_form_column").options == ["customer_id", "amount"]

    def test_the_editing_dialog_locks_the_step_kind(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.session_state[session.TF_DIALOG_KEY] = "edit"
        app.run()

        assert not app.exception
        with pytest.raises(KeyError):
            app.selectbox(key="tf_pick_category")


class TestDownloading:
    def test_a_download_is_offered_for_the_last_steps_table(self, tmp_path, monkeypatch):
        """AppTest's download_button exposes only its click state, not its bytes, so what
        the workbook actually holds is covered in test_transform_export.py. What this
        asserts is that the page got far enough to build one, and pre-selected the table
        the last step produced."""
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.run()

        assert app.download_button(key="tf_download") is not None
        assert app.session_state[session.TF_EXPORT_KEY] == ["merged_data"]

    def test_only_the_first_500_rows_are_shown_on_screen(self, tmp_path, monkeypatch):
        """The download itself keeps every row - see test_transform_export.py."""
        big = b"a\n" + b"\n".join(str(number).encode() for number in range(600)) + b"\n"
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("big.csv", big))

        assert not app.exception
        assert any("Showing 500 of 600 rows" in item.value for item in app.caption)

    def test_starting_over_clears_everything(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.run()
        app.button(key="tf_start_over").click().run()

        assert _steps(app) == []
        assert _table_names(app) == []


class TestAFailingStepIsReportedOnScreen:
    def test_the_page_says_which_step_stopped_and_why(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        broken = make_step(
            "sort_rows", {"source": "nowhere"}, {"columns": ["amount"]}, "in_place", "nowhere"
        )
        app.session_state[session.TF_STEPS_KEY] = [broken]
        app.run()

        assert not app.exception
        assert any("couldn't run" in error.value for error in app.error)


class TestRenamingAnUploadedTable:
    def _name_box(self, app):
        return next(
            box for box in app.text_input if box.key.startswith("tf_name_")
        )

    def test_a_new_name_is_taken_up(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        self._name_box(app).set_value("this_month").run()

        assert _table_names(app) == ["this_month"]
        assert not app.exception

    def test_a_name_already_in_use_is_refused(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        self._name_box(app).set_value("customers").run()

        assert any("already exists" in error.value for error in app.error)
        assert sorted(_table_names(app)) == ["customers", "sales"]

    def test_renaming_repoints_the_steps_that_used_it(self, tmp_path, monkeypatch):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        app.session_state[session.TF_STEPS_KEY] = [_join_step()]
        app.run()

        self._name_box(app).set_value("this_month").run()

        assert _steps(app)[0]["inputs"]["left"] == "this_month"
        assert not app.exception
        assert any(tab.label == "merged_data" for tab in app.tabs)


class TestSwitchingOperationInTheDialog:
    def test_the_old_operations_boxes_do_not_carry_over(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[session.TF_DIALOG_KEY] = "add"
        app.session_state["tf_pick_category"] = "Filter & sort"
        app.session_state["tf_form_input_source"] = "sales"
        app.run()

        app.selectbox(key="tf_form_column").set_value("amount").run()
        assert app.session_state["tf_form_column"] == "amount"

        # Switching to another step in the same category, which has boxes of its own.
        app.selectbox(key="tf_pick_operation").set_value("Sort rows").run()

        assert not app.exception
        assert "tf_form_column" not in app.session_state

    def test_the_table_already_chosen_is_kept(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[session.TF_DIALOG_KEY] = "add"
        app.session_state["tf_pick_category"] = "Filter & sort"
        app.session_state["tf_form_input_source"] = "sales"
        app.run()

        app.selectbox(key="tf_pick_operation").set_value("Sort rows").run()

        assert app.session_state["tf_form_input_source"] == "sales"


class TestAddingAStepThroughTheDialog:
    """The commit path end to end: picker, form, Add step, run.

    Every other test in this file injects the step list directly, which leaves
    `_commit_step` and the form's read-back untested. This drives the real widgets.
    """

    def _open_filter_dialog(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[session.TF_DIALOG_KEY] = "add"
        app.session_state["tf_pick_category"] = "Filter & sort"
        app.session_state["tf_form_input_source"] = "sales"
        app.run()
        return app

    def test_a_filled_in_form_adds_a_step_that_runs(self, tmp_path, monkeypatch):
        app = self._open_filter_dialog(tmp_path, monkeypatch)

        app.selectbox(key="tf_form_column").set_value("customer_id").run()
        app.selectbox(key="tf_form_comparison").set_value("is").run()
        app.text_input(key="tf_form_value").set_value("1").run()
        app.button(key="tf_commit_step").click().run()

        assert not app.exception
        steps = _steps(app)
        assert len(steps) == 1
        assert steps[0]["operation"] == "filter_rows"
        assert steps[0]["params"]["value"] == "1"
        assert steps[0]["output"] == {"mode": "in_place", "name": "sales"}

    def test_the_dialog_closes_and_says_so(self, tmp_path, monkeypatch):
        app = self._open_filter_dialog(tmp_path, monkeypatch)

        app.selectbox(key="tf_form_column").set_value("customer_id").run()
        app.text_input(key="tf_form_value").set_value("1").run()
        app.button(key="tf_commit_step").click().run()

        assert session.TF_DIALOG_KEY not in app.session_state
        assert any("Step added" in success.value for success in app.success)

    def test_the_add_button_is_disabled_until_the_form_is_filled(self, tmp_path, monkeypatch):
        app = self._open_filter_dialog(tmp_path, monkeypatch)
        assert app.button(key="tf_commit_step").disabled

    def test_it_says_what_is_still_needed(self, tmp_path, monkeypatch):
        app = self._open_filter_dialog(tmp_path, monkeypatch)
        assert any("Still needed" in item.value for item in app.caption)

    def test_previewing_shows_the_answer_without_adding_the_step(self, tmp_path, monkeypatch):
        app = self._open_filter_dialog(tmp_path, monkeypatch)

        app.selectbox(key="tf_form_column").set_value("customer_id").run()
        app.text_input(key="tf_form_value").set_value("1").run()
        app.button(key="tf_preview_step").click().run()

        assert not app.exception
        assert _steps(app) == []
        assert any("2 row(s)" in item.value for item in app.caption)


class TestFixHeaders:
    """The Skip rows shortcut on an uploaded table's tab.

    Skip rows is an ordinary step; this button only opens the usual dialog with the
    operation and table already chosen. So what is worth testing is that the pre-selection
    actually reaches the widgets, and that the step it produces is a normal one.
    """

    def _upload_junk(self, tmp_path, monkeypatch):
        return _upload_and_load(_make_app(tmp_path, monkeypatch), ("junk.csv", JUNK_CSV))

    def test_an_uploaded_table_offers_the_button(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))

        assert app.button(key="tf_fix_headers_sales")

    def test_unnamed_columns_are_pointed_out(self, tmp_path, monkeypatch):
        app = self._upload_junk(tmp_path, monkeypatch)

        assert any("have no name" in item.value for item in app.caption)

    def test_a_tidy_file_is_not_nagged_about_headers(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))

        assert not any("have no name" in item.value for item in app.caption)

    def test_clicking_it_opens_the_dialog_on_skip_rows(self, tmp_path, monkeypatch):
        app = self._upload_junk(tmp_path, monkeypatch)

        app.button(key="tf_fix_headers_junk").click().run()

        assert _state(app, session.TF_DIALOG_KEY, None) == "add"
        assert _state(app, "tf_pick_category", None) == "Tidy up"
        assert _state(app, "tf_form_input_source", None) == "junk"

    def test_the_skip_rows_form_is_on_screen(self, tmp_path, monkeypatch):
        app = self._upload_junk(tmp_path, monkeypatch)

        app.button(key="tf_fix_headers_junk").click().run()

        assert not app.exception
        assert app.number_input(key="tf_form_top")
        assert app.toggle(key="tf_form_promote_header")

    def test_adding_the_step_gives_the_columns_their_real_names(self, tmp_path, monkeypatch):
        app = self._upload_junk(tmp_path, monkeypatch)
        app.button(key="tf_fix_headers_junk").click().run()

        app.number_input(key="tf_form_top").set_value(1).run()
        app.toggle(key="tf_form_promote_header").set_value(True).run()
        app.button(key="tf_commit_step").click().run()

        assert not app.exception
        steps = _steps(app)
        assert len(steps) == 1
        assert steps[0]["operation"] == "skip_rows"
        assert steps[0]["output"] == {"mode": "in_place", "name": "junk"}

    def test_the_step_it_makes_is_an_ordinary_one_that_can_be_deleted(self, tmp_path, monkeypatch):
        app = self._upload_junk(tmp_path, monkeypatch)
        app.button(key="tf_fix_headers_junk").click().run()
        app.number_input(key="tf_form_top").set_value(1).run()
        app.toggle(key="tf_form_promote_header").set_value(True).run()
        app.button(key="tf_commit_step").click().run()

        assert app.button(key="tf_delete_0")
        assert app.button(key="tf_edit_0")


# --------------------------------------------------------------------------------------
# Phase 27: saved pipelines, and the Chat with Data handoff
# --------------------------------------------------------------------------------------


def _save_pipeline_through_the_page(app, name="Monthly"):
    """Presses Save as pipeline and fills the dialog in, as a user would."""
    app.button(key="tf_pipeline_save_as").click().run()
    app.text_input(key="tf_pipeline_new_name").set_value(name).run()
    app.button(key="tf_pipeline_save_confirm").click().run()
    return app


def _joined_app(tmp_path, monkeypatch):
    """Two files loaded and joined — the smallest pipeline worth saving."""
    app = _upload_and_load(
        _make_app(tmp_path, monkeypatch),
        ("sales.csv", SALES_CSV),
        ("customers.csv", CUSTOMERS_CSV),
    )
    app.session_state[session.TF_STEPS_KEY] = [_join_step()]
    app.run()
    return app


def _select_saved(app, position=0):
    """Chooses the saved pipeline in the picker, as clicking it would."""
    pipeline_id = list_pipelines(1)[position]["pipeline_id"]
    app.selectbox(key=session.TF_PIPELINE_PICK_KEY).set_value(pipeline_id).run()
    return app


class TestThePipelineBar:
    def test_it_is_there_on_an_empty_page(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert app.selectbox(key=session.TF_PIPELINE_PICK_KEY) is not None
        assert not app.exception

    def test_save_is_greyed_out_until_there_is_something_to_save(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert app.button(key="tf_pipeline_save_as").disabled

    def test_save_wakes_up_once_a_file_is_loaded(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert not app.button(key="tf_pipeline_save_as").disabled

    def test_the_buttons_that_need_a_selection_are_greyed_out_without_one(
        self, tmp_path, monkeypatch
    ):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert app.button(key="tf_pipeline_schema").disabled
        assert app.button(key="tf_pipeline_update").disabled
        assert app.button(key="tf_pipeline_delete").disabled


class TestSavingAPipeline:
    def test_the_dialog_opens(self, tmp_path, monkeypatch):
        app = _joined_app(tmp_path, monkeypatch)
        app.button(key="tf_pipeline_save_as").click().run()
        assert app.text_input(key="tf_pipeline_new_name") is not None
        assert not app.exception

    def test_saving_writes_it_and_selects_it(self, tmp_path, monkeypatch):
        app = _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))

        assert not app.exception
        assert [row["name"] for row in list_pipelines(1)] == ["Monthly"]
        assert app.session_state[session.TF_PIPELINE_NAME_KEY] == "Monthly"

    def test_what_is_stored_is_the_tables_the_steps_and_the_chosen_output(
        self, tmp_path, monkeypatch
    ):
        _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))

        saved = load_pipeline(list_pipelines(1)[0]["pipeline_id"], 1)
        assert sorted(saved.table_names()) == ["customers", "sales"]
        assert [step["operation"] for step in saved.steps] == ["merge"]
        assert saved.outputs == ["merged_data"]

    def test_a_name_another_pipeline_already_has_is_refused(self, tmp_path, monkeypatch):
        app = _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))
        app.button(key="tf_pipeline_save_as").click().run()
        app.text_input(key="tf_pipeline_new_name").set_value("monthly").run()
        app.button(key="tf_pipeline_save_confirm").click().run()

        assert any("already have a pipeline" in error.value for error in app.error)
        assert len(list_pipelines(1)) == 1

    def test_saving_with_no_name_is_not_possible(self, tmp_path, monkeypatch):
        app = _joined_app(tmp_path, monkeypatch)
        app.button(key="tf_pipeline_save_as").click().run()
        assert app.button(key="tf_pipeline_save_confirm").disabled


class TestReusingAPipeline:
    """The promise of the page: upload next month's file, press one button."""

    def _saved_then_fresh(self, tmp_path, monkeypatch, sales=b"customer_id,amount\n1,999\n"):
        _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))
        fresh = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", sales),
            ("customers.csv", CUSTOMERS_CSV),
        )
        return _select_saved(fresh)

    def test_choosing_one_selects_it_without_running_it(self, tmp_path, monkeypatch):
        app = self._saved_then_fresh(tmp_path, monkeypatch)
        assert app.session_state[session.TF_PIPELINE_NAME_KEY] == "Monthly"
        assert _steps(app) == []
        assert not app.exception

    def test_a_matching_upload_says_so(self, tmp_path, monkeypatch):
        app = self._saved_then_fresh(tmp_path, monkeypatch)
        assert any("matched this pipeline exactly" in ok.value for ok in app.success)

    def test_running_it_installs_every_saved_step(self, tmp_path, monkeypatch):
        app = self._saved_then_fresh(tmp_path, monkeypatch)
        app.button(key="tf_pipeline_apply").click().run()

        assert not app.exception
        assert [step["operation"] for step in _steps(app)] == ["merge"]

    def test_running_it_builds_the_answer_table(self, tmp_path, monkeypatch):
        app = self._saved_then_fresh(tmp_path, monkeypatch)
        app.button(key="tf_pipeline_apply").click().run()

        assert not app.exception
        assert [step["output"]["name"] for step in _steps(app)] == ["merged_data"]
        assert any("Ran" in ok.value for ok in app.success)

    def test_it_points_the_download_at_the_saved_output(self, tmp_path, monkeypatch):
        app = self._saved_then_fresh(tmp_path, monkeypatch)
        app.button(key="tf_pipeline_apply").click().run()
        assert _state(app, session.TF_EXPORT_KEY, []) == ["merged_data"]

    def test_a_table_the_user_renamed_is_renamed_back_when_the_pipeline_runs(
        self, tmp_path, monkeypatch
    ):
        """The steps were written against a table the user had renamed to `s`.

        Next month the same file arrives and is auto-named `sales` again. Matching is on
        the *file*, so it is still found — and applying renames the table back to `s`,
        the name every saved step was written against. Renaming the table rather than
        rewriting the steps is the whole trick; see `session.apply_pipeline`.
        """
        app = _joined_app(tmp_path, monkeypatch)
        # Rename `sales` to `s`, and point the step at the new name — what the page's own
        # name box does. Written into session state directly because
        # `session.rename_uploaded_table` needs the running script's context.
        box = next(entry for entry in app.text_input if entry.value == "sales")
        box.set_value("s").run()
        assert "s" in _table_names(app)
        _save_pipeline_through_the_page(app)

        fresh = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        assert sorted(_table_names(fresh)) == ["customers", "sales"]

        _select_saved(fresh)
        fresh.button(key="tf_pipeline_apply").click().run()

        assert not fresh.exception
        assert sorted(_table_names(fresh)) == ["customers", "s"]
        assert _steps(fresh)[0]["inputs"]["left"] == "s"

    def test_a_missing_file_blocks_and_names_it(self, tmp_path, monkeypatch):
        _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))

        fresh = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        _select_saved(fresh)
        fresh.button(key="tf_pipeline_apply").click().run()

        assert _steps(fresh) == []
        assert any("customers" in error.value for error in fresh.error)

    def test_a_missing_column_blocks_and_names_both_sides(self, tmp_path, monkeypatch):
        """Section 6.4 of the requirements document, on screen."""
        _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))

        fresh = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            # `customer id` with a space, where the pipeline wants `customer_id`.
            ("sales.csv", b"customer id,amount\n1,100\n"),
            ("customers.csv", CUSTOMERS_CSV),
        )
        _select_saved(fresh)
        fresh.button(key="tf_pipeline_apply").click().run()

        assert _steps(fresh) == []
        shown = " ".join(error.value for error in fresh.error)
        assert "'customer_id'" in shown
        assert "'customer id'" in shown

    def test_an_extra_file_is_left_alone_not_refused(self, tmp_path, monkeypatch):
        _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))

        fresh = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
            ("budget.csv", b"target\n50\n"),
        )
        _select_saved(fresh)
        fresh.button(key="tf_pipeline_apply").click().run()

        assert not fresh.exception
        assert [step["operation"] for step in _steps(fresh)] == ["merge"]
        assert "budget" in _table_names(fresh)


class TestTheExpectedFilesDialog:
    def test_it_opens_and_can_be_closed(self, tmp_path, monkeypatch):
        app = _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))
        app.button(key="tf_pipeline_schema").click().run()

        assert not app.exception
        assert app.button(key="tf_pipeline_schema_close") is not None


class TestDeletingAPipeline:
    def test_it_asks_first(self, tmp_path, monkeypatch):
        app = _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))
        app.button(key="tf_pipeline_delete").click().run()

        assert app.button(key="tf_pipeline_delete_confirm") is not None
        assert len(list_pipelines(1)) == 1

    def test_confirming_removes_it_and_leaves_the_steps_alone(self, tmp_path, monkeypatch):
        """Deleting a saved recipe is not undoing the work on screen."""
        app = _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))
        app.button(key="tf_pipeline_delete").click().run()
        app.button(key="tf_pipeline_delete_confirm").click().run()

        assert not app.exception
        assert list_pipelines(1) == []
        assert [step["operation"] for step in _steps(app)] == ["merge"]


class TestUpdatingAPipeline:
    def test_it_overwrites_rather_than_adding_a_second(self, tmp_path, monkeypatch):
        app = _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))
        app.session_state[session.TF_STEPS_KEY] = [_join_step(), _join_step()]
        app.run()

        app.button(key="tf_pipeline_update").click().run()
        app.button(key="tf_pipeline_update_confirm").click().run()

        assert not app.exception
        rows = list_pipelines(1)
        assert len(rows) == 1
        assert len(load_pipeline(rows[0]["pipeline_id"], 1).steps) == 2


class TestExportToChatWithData:
    def test_the_button_is_there_beside_the_download(self, tmp_path, monkeypatch):
        """Both routes out of the page — a file, or another page — sit together.

        The greyed-out form of this button (nothing ticked) isn't asserted: it can't be
        reached from `AppTest`, because `seed_export_selection` refills the picker before
        the widget is drawn. In a browser it appears for the one run between a user
        clearing the box and the rerun that re-seeds it.
        """
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert app.menu_button(key="tf_export_menu") is not None
        assert app.download_button(key="tf_download") is not None

    def test_exporting_adopts_the_tables_before_switching_pages(self, tmp_path, monkeypatch):
        """The push side of the handoff, mirroring the Data Cleaner's own export test.

        `AppTest.from_file` runs one page in isolation, without the `st.navigation`
        registry `streamlit_app.py` builds at real runtime, so `st.switch_page(...)` has
        no page list to resolve against here and raises — a harness gap, not a bug. What
        is checked is that adoption completes and lands in session_state *before* that
        call, which is the part a real click depends on.
        """
        app = _joined_app(tmp_path, monkeypatch)
        app.session_state[session.TF_EXPORT_KEY] = ["merged_data"]
        app.run()

        app.menu_button(key="tf_export_menu").click("Chat with Data").run()

        assert len(app.exception) == 1
        assert "Could not find page" in app.exception[0].value

        tables = app.session_state[engine_session.DE_TABLES_KEY]
        assert tables
        assert all(table.from_transform for table in tables.values())

    def test_only_the_ticked_table_is_handed_over(self, tmp_path, monkeypatch):
        """A join's raw halves aren't useful to chat over — only the result."""
        app = _joined_app(tmp_path, monkeypatch)
        app.session_state[session.TF_EXPORT_KEY] = ["merged_data"]
        app.run()

        app.menu_button(key="tf_export_menu").click("Chat with Data").run()

        tables = app.session_state[engine_session.DE_TABLES_KEY]
        assert [table.table_name for table in tables.values()] == ["merged_data"]

    def test_re_exporting_replaces_rather_than_accumulates(self, tmp_path, monkeypatch):
        app = _joined_app(tmp_path, monkeypatch)
        app.session_state[session.TF_EXPORT_KEY] = ["merged_data"]
        app.run()

        app.menu_button(key="tf_export_menu").click("Chat with Data").run()
        app.menu_button(key="tf_export_menu").click("Chat with Data").run()

        assert len(app.session_state[engine_session.DE_TABLES_KEY]) == 1


class TestCalculatedColumns:
    """The formula box had no coverage at all, which is how it shipped unable to return
    its own value — the Add button stayed disabled saying "Formula" was still needed."""

    def _open_the_form(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        spec = get_operation("add_calculated_column")
        app.session_state["tf_pick_category"] = spec.category
        app.session_state["tf_pick_operation"] = spec
        # Written into session state, not `session.open_dialog()`, which needs the running
        # script's context — the same reason the rename test uses the page's own widget.
        app.session_state[session.TF_DIALOG_KEY] = "add"
        app.run()
        return app

    def test_typing_a_formula_lets_the_step_be_added(self, tmp_path, monkeypatch):
        app = self._open_the_form(tmp_path, monkeypatch)
        app.selectbox(key="tf_form_input_source").set_value("sales").run()
        app.text_input(key="tf_form_new_column").set_value("doubled").run()
        app.text_input(key="tf_form_expression").set_value("amount * 2").run()

        assert not app.button(key="tf_commit_step").disabled

        app.button(key="tf_commit_step").click().run()

        assert not app.exception
        steps = _steps(app)
        assert len(steps) == 1
        assert steps[0]["operation"] == "add_calculated_column"
        assert steps[0]["params"]["expression"] == "amount * 2"

    def test_the_new_column_actually_appears(self, tmp_path, monkeypatch):
        app = self._open_the_form(tmp_path, monkeypatch)
        app.selectbox(key="tf_form_input_source").set_value("sales").run()
        app.text_input(key="tf_form_new_column").set_value("doubled").run()
        app.text_input(key="tf_form_expression").set_value("amount * 2").run()
        app.button(key="tf_commit_step").click().run()

        assert not app.exception
        assert any("doubled" in list(frame.value.columns) for frame in app.dataframe)


class TestReplayDoesNotClobberAnExtraUpload:
    """`matching` promises an extra file is "left as it is". Replay must not contradict it."""

    def test_a_new_table_step_refuses_a_name_an_upload_already_holds(self):
        from transform.pipeline import apply_steps_with_report
        from transform.workspace import NamedFrame

        base = {
            "sales": NamedFrame(
                name="sales", frame=pd.DataFrame({"amount": [1]}), origin="upload"
            ),
            "summary": NamedFrame(
                name="summary", frame=pd.DataFrame({"mine": [99]}), origin="upload"
            ),
        }
        step = make_step(
            "filter_rows",
            {"source": "sales"},
            {"column": "amount", "comparison": "is greater than", "value": "0"},
            "new",
            "summary",
        )

        after, report = apply_steps_with_report(base, [step])

        assert report[-1].status == "failed"
        assert "already exists" in report[-1].message
        # The user's own table is untouched, exactly as they were told.
        assert list(after["summary"].frame.columns) == ["mine"]

    def test_an_in_place_step_still_writes_back_over_its_own_table(self):
        """The other half: in place *must* replace, or no step could change a table."""
        from transform.pipeline import apply_steps_with_report
        from transform.workspace import NamedFrame

        base = {
            "sales": NamedFrame(
                name="sales", frame=pd.DataFrame({"amount": [1, 5]}), origin="upload"
            )
        }
        step = make_step(
            "filter_rows",
            {"source": "sales"},
            {"column": "amount", "comparison": "is greater than", "value": "3"},
            "in_place",
            "sales",
        )

        after, report = apply_steps_with_report(base, [step])

        assert report[-1].status == "applied"
        assert len(after["sales"].frame) == 1


class TestApplyPipelineRollback:
    def test_a_table_it_cannot_rename_keeps_the_name_the_user_knows(
        self, tmp_path, monkeypatch
    ):
        """If the second rename pass fails, the table must not be left called `tfmove0`.

        Built by saving a pipeline whose table is called `s`, then uploading a second,
        unrelated file the user has named `s` — so the name the pipeline needs is held by
        a table it doesn't use.
        """
        app = _joined_app(tmp_path, monkeypatch)
        box = next(entry for entry in app.text_input if entry.value == "sales")
        box.set_value("s").run()
        _save_pipeline_through_the_page(app)

        fresh = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
            ("other.csv", b"whatever\n1\n"),
        )
        # `other` is renamed to `s`, squatting on the name the pipeline needs.
        squatter = next(entry for entry in fresh.text_input if entry.value == "other")
        squatter.set_value("s").run()

        _select_saved(fresh)
        fresh.button(key="tf_pipeline_apply").click().run()

        assert not fresh.exception
        names = _table_names(fresh)
        assert not any(name.startswith("tfmove") for name in names), names
        assert "sales" in names


class TestDeselectingAPipelineTakesItsStepsWithIt:
    """Reported from real use: run last month's pipeline, pick "New pipeline", upload a
    completely different set of files — and the page opened on "There's no table called
    'Stock with difference August 20'", a step from a pipeline no longer selected."""

    def _applied(self, tmp_path, monkeypatch):
        _save_pipeline_through_the_page(_joined_app(tmp_path, monkeypatch))
        fresh = _upload_and_load(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customers.csv", CUSTOMERS_CSV),
        )
        _select_saved(fresh)
        fresh.button(key="tf_pipeline_apply").click().run()
        return fresh

    def test_applying_marks_the_steps_as_the_pipelines_own(self, tmp_path, monkeypatch):
        app = self._applied(tmp_path, monkeypatch)
        assert _steps(app)
        assert app.session_state[session.TF_STEPS_SOURCE_KEY] is not None

    def test_going_back_to_new_pipeline_clears_them(self, tmp_path, monkeypatch):
        app = self._applied(tmp_path, monkeypatch)
        app.selectbox(key=session.TF_PIPELINE_PICK_KEY).set_value(
            saved_picker.NONE_OPTION
        ).run()

        assert _steps(app) == []
        assert session.TF_STEPS_SOURCE_KEY not in app.session_state
        assert not app.exception

    def test_it_says_so_rather_than_clearing_them_silently(self, tmp_path, monkeypatch):
        app = self._applied(tmp_path, monkeypatch)
        app.selectbox(key=session.TF_PIPELINE_PICK_KEY).set_value(
            saved_picker.NONE_OPTION
        ).run()
        assert any("Cleared the steps" in note.value for note in app.success)

    def test_the_uploaded_tables_stay(self, tmp_path, monkeypatch):
        """Clearing steps is not Start over — the files the user loaded are still loaded."""
        app = self._applied(tmp_path, monkeypatch)
        app.selectbox(key=session.TF_PIPELINE_PICK_KEY).set_value(
            saved_picker.NONE_OPTION
        ).run()
        assert sorted(_table_names(app)) == ["customers", "sales"]

    def test_steps_the_user_touched_are_never_thrown_away(self, tmp_path, monkeypatch):
        """The original rule still holds: deselecting is not undoing your own work."""
        app = self._applied(tmp_path, monkeypatch)
        # Any edit at all — here, undoing the last step — makes the list the user's.
        session_steps = list(_steps(app))
        app.session_state[session.TF_STEPS_KEY] = session_steps
        del app.session_state[session.TF_STEPS_SOURCE_KEY]
        app.run()

        app.selectbox(key=session.TF_PIPELINE_PICK_KEY).set_value(
            saved_picker.NONE_OPTION
        ).run()

        assert _steps(app) == session_steps
        assert not app.exception

    def test_adding_a_step_by_hand_releases_the_marker(self, tmp_path, monkeypatch):
        """`set_steps` is the single place that forgets the pipeline — check it fires."""
        app = self._applied(tmp_path, monkeypatch)
        box = next(entry for entry in app.text_input if entry.value == "sales")
        box.set_value("sales_2026").run()
        assert session.TF_STEPS_SOURCE_KEY not in app.session_state


class TestColumnDetails:
    # Asserted on the profile table itself rather than on the expander around it: AppTest
    # does not surface expanders nested inside `st.tabs`, and the table is the point anyway.
    PROFILE_COLUMNS = [
        "column", "column_type", "non_null", "missing", "missing_pct", "unique", "sample_values"
    ]

    def _profile(self, app):
        return next(
            frame.value
            for frame in app.dataframe
            if list(frame.value.columns) == self.PROFILE_COLUMNS
        )

    def test_every_table_gets_the_panel(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        profile = self._profile(app)
        assert sorted(profile["column"]) == ["amount", "customer_id"]
        assert not app.exception

    def test_it_reports_on_the_whole_table_not_the_preview(self, tmp_path, monkeypatch):
        rows = b"customer_id,amount\n" + b"".join(
            f"{index},{index}\n".encode() for index in range(session.PREVIEW_ROWS + 25)
        )
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", rows))
        stats = self._profile(app)
        filled = dict(zip(stats["column"], stats["non_null"]))
        assert filled["amount"] == session.PREVIEW_ROWS + 25


def _bonus_step(column: str = "bonus", expression: str = "amount * 0.12") -> dict:
    return make_step(
        "add_calculated_column",
        {"source": "sales"},
        {"new_column": column, "expression": expression},
        "in_place",
        "sales",
    )


class TestDescribeAStepDialog:
    """Plain English step entry (requirements section 5, path 2).

    The Light Model is monkeypatched at the page's own import sites, so no test here goes
    near a provider. What is being checked is the *gate*: nothing reaches the step list
    that the user has not seen described and pressed Add on.
    """

    LIGHT = {
        "profile_id": 9,
        "nickname": "Light",
        "default_model": "small-model",
        "provider_type": "local",
    }

    def _light(self, monkeypatch, profile):
        monkeypatch.setattr(llm_session, "light_profile", lambda user_id: profile)

    def _parse(self, monkeypatch, result, recorder=None):
        def fake_parse(light, instruction, workspace):
            if recorder is not None:
                recorder.append(instruction)
            return result

        monkeypatch.setattr(ai_parse, "parse_instruction", fake_parse)

    def _open(self, tmp_path, monkeypatch, *files):
        app = _upload_and_load(
            _make_app(tmp_path, monkeypatch), *(files or (("sales.csv", SALES_CSV),))
        )
        app.session_state[session.TF_DIALOG_KEY] = "ai_add"
        app.run()
        return app

    def _read(self, app, instruction):
        app.text_area(key=session.TF_AI_INSTRUCTION_KEY).set_value(instruction).run()
        app.button(key="tf_ai_parse").click().run()
        return app

    def test_the_button_sits_next_to_add_a_step(self, tmp_path, monkeypatch):
        app = _upload_and_load(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert app.button(key="tf_ai_add_step") is not None
        assert app.button(key="tf_add_step") is not None

    def test_it_opens_on_an_instruction_box(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        app = self._open(tmp_path, monkeypatch)
        assert not app.exception
        assert app.text_area(key=session.TF_AI_INSTRUCTION_KEY) is not None

    def test_with_no_light_model_it_explains_rather_than_offering_a_box(self, tmp_path, monkeypatch):
        self._light(monkeypatch, None)
        app = self._open(tmp_path, monkeypatch)
        assert not app.exception
        assert any("No Light Model is configured" in warning.value for warning in app.warning)
        with pytest.raises(KeyError):
            app.text_area(key=session.TF_AI_INSTRUCTION_KEY)

    def test_with_no_light_model_nothing_is_ever_sent(self, tmp_path, monkeypatch):
        asked = []
        self._light(monkeypatch, None)
        self._parse(monkeypatch, ([], [], None), asked)
        app = self._open(tmp_path, monkeypatch)
        app.button(key="tf_ai_close").click().run()
        assert asked == []

    def test_reading_an_instruction_shows_the_steps_but_adds_nothing_yet(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([_bonus_step()], [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "add bonus")

        assert not app.exception
        assert _steps(app) == []
        assert app.button(key="tf_ai_commit") is not None

    def test_the_summary_uses_the_same_words_the_step_list_uses(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([_bonus_step()], [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "add bonus")

        headline = step_headline(_bonus_step(), 0)
        assert any(headline in block.value for block in app.markdown)

    def test_what_the_user_typed_is_what_gets_sent(self, tmp_path, monkeypatch):
        asked = []
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([_bonus_step()], [], None), asked)
        self._read(self._open(tmp_path, monkeypatch), "add a bonus column")
        assert asked == ["add a bonus column"]

    def test_pressing_add_puts_the_steps_in_the_list(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([_bonus_step()], [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "add bonus")
        app.button(key="tf_ai_commit").click().run()

        assert not app.exception
        assert [step["operation"] for step in _steps(app)] == ["add_calculated_column"]
        assert session.TF_DIALOG_KEY not in app.session_state

    def test_several_steps_are_added_in_the_order_they_were_shown(self, tmp_path, monkeypatch):
        parsed = [_bonus_step(), _bonus_step("total_pay", "amount + bonus")]
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, (parsed, [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "two columns")
        app.button(key="tf_ai_commit").click().run()

        assert not app.exception
        assert [step["params"]["new_column"] for step in _steps(app)] == ["bonus", "total_pay"]

    def test_a_clarification_is_shown_and_nothing_is_added(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([], [], "Forecasting is not something this tool can do."))
        app = self._read(self._open(tmp_path, monkeypatch), "forecast next year")

        assert not app.exception
        assert any("Forecasting is not" in info.value for info in app.info)
        assert _steps(app) == []
        with pytest.raises(KeyError):
            app.button(key="tf_ai_commit")

    def test_a_dropped_step_is_reported_rather_than_hidden(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([], ["Skipped a step: train_a_model is not a step."], None))
        app = self._read(self._open(tmp_path, monkeypatch), "do magic")

        assert any("train_a_model" in warning.value for warning in app.warning)
        assert _steps(app) == []

    def test_preview_runs_the_steps_without_adding_them(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([_bonus_step()], [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "add bonus")
        app.button(key="tf_ai_preview").click().run()

        assert not app.exception
        assert _steps(app) == []
        assert any("bonus" in list(frame.value.columns) for frame in app.dataframe)

    def test_cancel_closes_the_dialog_and_forgets_the_parse(self, tmp_path, monkeypatch):
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([_bonus_step()], [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "add bonus")
        app.button(key="tf_ai_cancel").click().run()

        assert _steps(app) == []
        assert session.TF_DIALOG_KEY not in app.session_state
        assert session.TF_AI_STEPS_KEY not in app.session_state

    def test_a_step_that_no_longer_fits_is_refused_at_add_time(self, tmp_path, monkeypatch):
        """The parse validated against the workspace as it was. The check has to happen
        again at Add, or a parse gone stale could slip a broken step into the pipeline."""
        stale = make_step(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "bonus", "expression": "salary * 2"},
            "in_place",
            "sales",
        )
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([stale], [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "add bonus")
        app.button(key="tf_ai_commit").click().run()

        # `sales.csv` has no `salary` column, so the step cannot be admitted.
        assert not app.exception
        assert _steps(app) == []
        assert app.error

    def test_a_step_that_fails_on_the_data_is_not_left_in_the_list(self, tmp_path, monkeypatch):
        """Validation passes, the run doesn't. The step must not be appended first and
        then found to be broken - a pipeline is not a place to leave a step that cannot
        run, and the user would have no idea it was there."""
        breaks = make_step(
            "add_calculated_column",
            {"source": "sales"},
            {"new_column": "ratio", "expression": "amount / nonsense("},
            "in_place",
            "sales",
        )
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([breaks], [], None))
        app = self._read(self._open(tmp_path, monkeypatch), "add a ratio")
        app.button(key="tf_ai_commit").click().run()

        assert not app.exception
        assert _steps(app) == []
        assert app.error

    def test_the_steps_are_numbered_from_where_the_list_already_ends(self, tmp_path, monkeypatch):
        """A dialog that starts counting at one points at the wrong rows the moment the
        pipeline is not empty."""
        self._light(monkeypatch, self.LIGHT)
        self._parse(monkeypatch, ([_bonus_step()], [], None))
        app = self._open(tmp_path, monkeypatch)
        app.session_state[session.TF_STEPS_KEY] = [_bonus_step("first_one", "amount * 2")]
        app.run()
        app = self._read(app, "add bonus")

        headline = step_headline(_bonus_step(), 1)
        assert any(headline in block.value for block in app.markdown)
