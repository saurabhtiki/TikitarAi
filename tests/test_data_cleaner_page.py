"""AppTest coverage for the Data Cleaner page.

Unlike Stages 2 and 3, `st.file_uploader` really is drivable through `AppTest` in the
installed Streamlit, so these tests exercise the page through actual uploads rather than
by injecting state. The dialog is driven through its session-state open flag, which is
why the page uses that idiom instead of `if st.button(...): _open_dialog()` — with the
button idiom the dialog closes on the next rerun and its widgets disappear.
"""

import io
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from cleaner import loaders, pipeline, session
from llm.db import init_llm_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_CLEANER_PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "data_cleaner.py")

SALARIES_CSV = b"emp_id,name,dept,amount\n007, Ana ,Sales,\"$1,200.50\"\n008, Bo ,Sales,(300)\n008, Bo ,Sales,(300)\n"
OTHER_CSV = b"code,city\nX1,Delhi\nX2,Mumbai\n"


def _workbook(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def _make_app(tmp_path, monkeypatch, role="normal_user"):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    app = AppTest.from_file(DATA_CLEANER_PAGE_PATH, default_timeout=30)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = role
    app.run()
    return app


def _upload(app, *files: tuple[str, bytes]):
    app.file_uploader(key=session.DC_UPLOADER_KEY).set_value(
        [(name, payload, "application/octet-stream") for name, payload in files]
    )
    app.run()
    return app


def _tables(app) -> list[session.TableState]:
    return list(app.session_state[session.DC_TABLES_KEY].values())


def _only_table(app) -> session.TableState:
    tables = _tables(app)
    assert len(tables) == 1
    return tables[0]


def _cleaned(table: session.TableState, payload: bytes) -> pd.DataFrame:
    """Derives the cleaned frame the way the page would, but without the cache.

    `session.cleaned_table` is backed by a `scope="session"` cache, which Streamlit only
    lets the app's own execution thread read — calling it from the test thread raises.
    Going through the pipeline directly exercises the same recipe.
    """
    return pipeline.apply_steps(loaders.read_table(payload, table.file_name, table.sheet_name), table.steps)


# --------------------------------------------------------------------------------------
# Access and empty state
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["normal_user", "admin", "superuser"])
def test_every_role_can_open_the_page(tmp_path, monkeypatch, role):
    app = _make_app(tmp_path, monkeypatch, role=role)

    assert not app.exception
    assert app.title[0].value == "Data Cleaner"


def test_the_page_is_calm_with_nothing_uploaded(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    assert not app.exception
    assert app.file_uploader(key=session.DC_UPLOADER_KEY)
    assert any("Upload a CSV or Excel file" in info.value for info in app.info)


# --------------------------------------------------------------------------------------
# Upload and table registration
# --------------------------------------------------------------------------------------


def test_uploading_a_csv_creates_a_table_and_renders_a_preview(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))

    assert not app.exception
    table = _only_table(app)
    assert table.file_name == "salaries.csv"
    assert table.output_sheet_name == "salaries"
    assert len(app.dataframe) >= 1


def test_detected_types_are_seeded_as_a_real_recorded_step(tmp_path, monkeypatch):
    """Typing is recorded rather than applied invisibly, so it replays in later stages
    and 'reset to raw' genuinely returns the file's literal contents."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table = _only_table(app)

    assert [step["action"] for step in table.steps] == ["set_column_types"]
    assert table.steps[0]["params"]["by_column"]["emp_id"]["target_type"] == "id"


def test_two_files_produce_two_tables(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV))

    assert not app.exception
    assert len(_tables(app)) == 2


def test_identically_named_files_get_distinct_tab_labels(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("data.csv", SALARIES_CSV), ("data.csv", OTHER_CSV))

    assert not app.exception
    labels = session.tab_labels(_tables(app))
    assert labels == ["data.csv", "data.csv (2)"]


def test_a_multi_sheet_workbook_offers_a_sheet_picker_and_one_table_per_sheet(tmp_path, monkeypatch):
    payload = _workbook({"Q1": pd.DataFrame({"a": [1]}), "Q2": pd.DataFrame({"b": [2]})})
    app = _upload(_make_app(tmp_path, monkeypatch), ("book.xlsx", payload))

    assert not app.exception
    file_id = _tables(app)[0].file_id
    assert app.multiselect(key=f"dc_sheets_{file_id}").value == ["Q1", "Q2"]
    assert sorted(table.sheet_name for table in _tables(app)) == ["Q1", "Q2"]


def test_deselecting_a_sheet_drops_its_table(tmp_path, monkeypatch):
    """Reconciliation has to run in both directions — otherwise the download silently
    keeps including tables the user believes they removed."""
    payload = _workbook({"Q1": pd.DataFrame({"a": [1]}), "Q2": pd.DataFrame({"b": [2]})})
    app = _upload(_make_app(tmp_path, monkeypatch), ("book.xlsx", payload))
    file_id = _tables(app)[0].file_id

    app.multiselect(key=f"dc_sheets_{file_id}").set_value(["Q1"]).run()

    assert not app.exception
    assert [table.sheet_name for table in _tables(app)] == ["Q1"]


def test_removing_every_file_clears_the_working_set(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    app.file_uploader(key=session.DC_UPLOADER_KEY).set_value([]).run()

    assert not app.exception
    assert _tables(app) == []


def _panel_rendered(app, table_id: str) -> bool:
    try:
        app.multiselect(key=f"dc_delete_columns_{table_id}")
    except KeyError:
        return False
    return True


def test_only_the_open_tab_is_rendered_and_switching_works(tmp_path, monkeypatch):
    """The `tab.open` gate is what stops every rerun re-deriving every loaded table's
    pipeline, so it is worth pinning down that it really gates and really switches."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV))
    first, second = (table.table_id for table in _tables(app))

    assert _panel_rendered(app, first)
    assert not _panel_rendered(app, second)

    app.session_state[session.DC_TABS_KEY] = "other.csv"
    app.run()

    assert not app.exception
    assert _panel_rendered(app, second)
    assert not _panel_rendered(app, first)


# --------------------------------------------------------------------------------------
# Applying, undoing and resetting steps
# --------------------------------------------------------------------------------------


def test_trimming_twice_leaves_a_single_log_entry(tmp_path, monkeypatch):
    """The REPLACE record policy, asserted through the real UI: a one-click idempotent
    action must not stack identical log lines."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    app.button(key=f"dc_apply_trim_{table_id}").click().run()
    app.button(key=f"dc_apply_trim_{table_id}").click().run()

    assert not app.exception
    trims = [step for step in _only_table(app).steps if step["action"] == "trim_whitespace"]
    assert len(trims) == 1


def test_trimming_actually_cleans_the_values(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table = _only_table(app)
    app.button(key=f"dc_apply_trim_{table.table_id}").click().run()

    cleaned = _cleaned(_only_table(app), SALARIES_CSV)

    assert list(cleaned["name"]) == ["Ana", "Bo", "Bo"]


def test_removing_duplicates_through_the_ui(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    app.button(key=f"dc_apply_dupes_{table_id}").click().run()
    cleaned = _cleaned(_only_table(app), SALARIES_CSV)

    assert not app.exception
    assert len(cleaned) == 2


def test_apply_to_all_tables_records_the_step_everywhere(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV))
    first = _tables(app)[0]

    app.checkbox(key=f"dc_trim_all_{first.table_id}").set_value(True).run()
    app.button(key=f"dc_apply_trim_{first.table_id}").click().run()

    assert not app.exception
    for table in _tables(app):
        assert any(step["action"] == "trim_whitespace" for step in table.steps)


def test_removing_one_log_entry_replays_the_rest(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    app.button(key=f"dc_apply_trim_{table_id}").click().run()
    assert len(_only_table(app).steps) == 2

    app.button(key=f"dc_remove_step_{table_id}_0").click().run()

    assert not app.exception
    remaining = _only_table(app).steps
    assert [step["action"] for step in remaining] == ["trim_whitespace"]


def test_reset_to_raw_dialog_clears_every_step(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    app.button(key=f"dc_apply_trim_{table_id}").click().run()

    app.button(key=f"dc_reset_{table_id}").click().run()
    assert app.session_state[session.DC_RESET_DIALOG_KEY] == table_id

    app.button(key="dc_confirm_reset_button").click().run()

    assert not app.exception
    assert _only_table(app).steps == []
    assert session.DC_RESET_DIALOG_KEY not in app.session_state


def test_cancelling_the_reset_dialog_keeps_the_steps(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    app.button(key=f"dc_apply_trim_{table_id}").click().run()

    app.button(key=f"dc_reset_{table_id}").click().run()
    app.button(key="dc_cancel_reset_button").click().run()

    assert not app.exception
    assert len(_only_table(app).steps) == 2


def test_an_invalid_rename_is_rejected_without_entering_the_recipe(tmp_path, monkeypatch):
    """Validation runs before a step is admitted, which is what keeps a stored recipe
    well-formed for Stage 7 to serialize."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    before = len(_only_table(app).steps)

    app.selectbox(key=f"dc_rename_source_{table_id}").set_value("name").run()
    app.text_input(key=f"dc_rename_target_{table_id}").set_value("dept").run()
    app.button(key=f"dc_apply_rename_{table_id}").click().run()

    assert not app.exception
    assert len(_only_table(app).steps) == before
    assert any("duplicate column name" in warning.value for warning in app.warning)


# --------------------------------------------------------------------------------------
# Output naming and download
# --------------------------------------------------------------------------------------


def test_the_output_sheet_name_is_sanitized(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    app.text_input(key=f"dc_output_sheet_name_{table_id}").set_value("Q1/Q2 results").run()

    assert not app.exception
    assert _only_table(app).output_sheet_name == "Q1_Q2 results"


def test_a_download_is_offered_with_one_sheet_per_table(tmp_path, monkeypatch):
    """AppTest's download_button exposes only its click state, not its bytes, so the
    workbook itself is covered in test_cleaner_export.py. What this asserts is that the
    page got far enough to build one and named the sheets from the uploaded files."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV))

    assert not app.exception
    assert app.download_button[0].label == "Download cleaned workbook"
    assert session.export_sheet_names(_tables(app)) == ["salaries", "other"]


def test_start_over_discards_every_table(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))

    app.button(key="dc_start_over_button").click().run()

    assert not app.exception
    assert app.session_state[session.DC_TABLES_KEY] == {}


# --------------------------------------------------------------------------------------
# Regression
# --------------------------------------------------------------------------------------


def test_an_action_button_below_its_panel_widgets_does_not_crash(tmp_path, monkeypatch):
    """Mirrors test_set_light_model_button_does_not_crash: a handler firing after its
    panel's widgets have already been instantiated must not raise
    StreamlitAPIException by writing to a widget's own session_state key."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    app.multiselect(key=f"dc_type_columns_{table_id}").set_value(["amount"]).run()
    app.selectbox(key=f"dc_type_target_{table_id}").set_value("numeric").run()
    app.button(key=f"dc_apply_types_{table_id}").click().run()

    assert not app.exception
    cleaned = _cleaned(_only_table(app), SALARIES_CSV)
    assert list(pd.to_numeric(cleaned["amount"]))[:2] == [1200.5, -300.0]
