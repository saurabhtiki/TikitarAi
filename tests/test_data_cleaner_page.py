"""AppTest coverage for the Data Cleaner page.

Unlike Stages 2 and 3, `st.file_uploader` really is drivable through `AppTest` in the
installed Streamlit, so these tests exercise the page through actual uploads rather than
by injecting state. The dialog is driven through its session-state open flag, which is
why the page uses that idiom instead of `if st.button(...): _open_dialog()` — with the
button idiom the dialog closes on the next rerun and its widgets disappear.
"""

import io
import re
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_pages import saved_picker
from auth.db import init_db, seed_default_admin
from cleaner import loaders, pipeline, profiling, session
from cleaner.db import init_cleaning_templates_table, list_templates, load_template
from cleaner.steps import FILL_STRATEGIES, STEP_REGISTRY
from cleaner.template import to_json
from llm.db import init_llm_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_CLEANER_PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "data_cleaner.py")

SALARIES_CSV = b"emp_id,name,dept,amount\n007, Ana ,Sales,\"$1,200.50\"\n008, Bo ,Sales,(300)\n008, Bo ,Sales,(300)\n"
OTHER_CSV = b"code,city\nX1,Delhi\nX2,Mumbai\n"
# The merged-cell shape: a label written once, then left blank down the rows it covers.
REGIONS_CSV = b"region,city\nNorth,Delhi\n,Mumbai\n,Pune\nSouth,Goa\n"
# Cells that read as empty but aren't: one plain space, one non-breaking space.
SPACED_CSV = "name,city\nAna,Delhi\n ,Mumbai\n\xa0,Pune\nBo,Goa\n".encode()


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
    init_cleaning_templates_table()
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


# Streamlit lifts a leading Material directive out of an option's label into a separate
# icon field, so the option content AppTest reports is the face text without it.
_ICON_PREFIX = re.compile(r"^:material/[^:]+:\s*")


def _command_control(app, table_id: str, action: str):
    """Finds the command-bar segmented control offering `action`, or None.

    The page's grouping is deliberately not duplicated here — importing the page module
    would execute it outside a run context — so the group is found by asking each
    control's own `format_func` what `action` would look like and checking whether that
    face is on offer.
    """
    for control in app.segmented_control:
        key = control.key or ""
        if not (key.startswith("dc_cmd_") and key.endswith(table_id)):
            continue
        if _ICON_PREFIX.sub("", control.format_func(action)) in control.options:
            return control
    return None


def _open(app, table_id: str, action: str):
    """Picks a command from the command bar, which opens that action's dialog."""
    control = _command_control(app, table_id, action)
    assert control is not None, f"no command offered for {action}"
    control.set_value(action).run()
    return app


_WIDGET_GROUPS = ("checkbox", "multiselect", "selectbox", "text_input", "number_input", "radio")


def _widget_values(app) -> dict:
    values = {}
    for group in _WIDGET_GROUPS:
        for widget in getattr(app, group):
            if widget.key:
                try:
                    values[widget.key] = widget.value
                except KeyError:
                    pass
    return values


def _click_and_settle(app, key: str):
    """Clicks a button that closes a dialog, then lets AppTest catch up.

    Purely an AppTest quirk: when a dialog closes, Streamlit garbage-collects its
    widgets' session_state entries, but AppTest's element tree still lists them, so the
    next `.run()` raises on the orphans. Re-seeding each orphan with the value it had
    before the click lets the tree refresh. A browser never hits this — the frontend
    simply stops sending a closed dialog's widget states.
    """
    before = _widget_values(app)
    app.button(key=key).click().run()

    for _ in range(40):
        try:
            app.run()
            return app
        except KeyError as error:
            orphan = re.search(r'no key "([^"]+)"', str(error))
            if orphan is None or orphan.group(1) not in before:
                raise
            app.session_state[orphan.group(1)] = before[orphan.group(1)]
    raise AssertionError("AppTest's element tree never settled after the dialog closed.")


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
    # The page heading is an st.subheader, not an st.title.
    assert any("Data Cleaner" in heading.value for heading in app.subheader)


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
    return any(
        (control.key or "").startswith("dc_cmd_") and (control.key or "").endswith(table_id)
        for control in app.segmented_control
    )


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
# Command bar and dialogs
# --------------------------------------------------------------------------------------


def test_the_command_bar_offers_every_registered_action(tmp_path, monkeypatch):
    """Derived from STEP_REGISTRY rather than the page's own list, so adding a 13th
    cleaning action without giving it a command fails here."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    for action in [*STEP_REGISTRY, "reset"]:
        assert _command_control(app, table_id, action) is not None, f"no command for {action}"


def test_no_dialog_widgets_exist_until_a_command_is_picked(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    with pytest.raises(KeyError):
        app.checkbox(key=f"dc_trim_collapse_{table_id}")


def test_picking_a_command_opens_its_dialog_with_that_action_s_inputs(tmp_path, monkeypatch):
    """The dialog is rendered inside the tab fragment, alongside the button that opens
    it — at page top level a fragment-scoped rerun would never reach it."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "trim_whitespace")

    assert not app.exception
    assert app.session_state[session.DC_DIALOG_KEY] == {"table_id": table_id, "action": "trim_whitespace"}
    assert app.checkbox(key=f"dc_trim_collapse_{table_id}")
    assert app.button(key=f"dc_apply_trim_whitespace_{table_id}")


def test_cancelling_a_dialog_records_nothing(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    before = len(_only_table(app).steps)

    _open(app, table_id, "trim_whitespace")
    _click_and_settle(app, f"dc_cancel_trim_whitespace_{table_id}")

    assert not app.exception
    assert session.DC_DIALOG_KEY not in app.session_state
    assert len(_only_table(app).steps) == before


def test_reset_is_not_offered_while_a_table_has_no_steps(tmp_path, monkeypatch):
    """A freshly uploaded table with no detectable types has an empty recipe, so there is
    nothing to reset. Omitted rather than greyed out: a segmented control disables as a
    whole, so a disabled option would have to take the whole Rows group with it."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("plain.csv", b"note\nhello\n"))
    table_id = _only_table(app).table_id

    assert _only_table(app).steps == []
    assert _command_control(app, table_id, "reset") is None
    assert _command_control(app, table_id, "drop_duplicates") is not None


# --------------------------------------------------------------------------------------
# Applying, undoing and resetting steps
# --------------------------------------------------------------------------------------


def test_trimming_twice_leaves_a_single_log_entry(tmp_path, monkeypatch):
    """The REPLACE record policy, asserted through the real UI: a one-click idempotent
    action must not stack identical log lines."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "trim_whitespace")
    _click_and_settle(app, f"dc_apply_trim_whitespace_{table_id}")
    _open(app, table_id, "trim_whitespace")
    _click_and_settle(app, f"dc_apply_trim_whitespace_{table_id}")

    assert not app.exception
    trims = [step for step in _only_table(app).steps if step["action"] == "trim_whitespace"]
    assert len(trims) == 1


def test_trimming_actually_cleans_the_values(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table = _only_table(app)
    _open(app, table.table_id, "trim_whitespace")
    _click_and_settle(app, f"dc_apply_trim_whitespace_{table.table_id}")

    cleaned = _cleaned(_only_table(app), SALARIES_CSV)

    assert list(cleaned["name"]) == ["Ana", "Bo", "Bo"]


def test_removing_duplicates_through_the_ui(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "drop_duplicates")
    _click_and_settle(app, f"dc_apply_drop_duplicates_{table_id}")
    cleaned = _cleaned(_only_table(app), SALARIES_CSV)

    assert not app.exception
    assert len(cleaned) == 2


def test_copying_a_value_down_through_the_ui(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("regions.csv", REGIONS_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "fill_missing")
    app.multiselect(key=f"dc_fill_columns_{table_id}").set_value(["region"]).run()
    app.selectbox(key=f"dc_fill_strategy_{table_id}").set_value("previous").run()
    _click_and_settle(app, f"dc_apply_fill_missing_{table_id}")

    assert not app.exception
    cleaned = _cleaned(_only_table(app), REGIONS_CSV)
    assert list(cleaned["region"]) == ["North", "North", "North", "South"]


def test_filling_blanks_with_a_custom_value_through_the_ui(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("spaced.csv", SPACED_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "fill_missing")
    app.multiselect(key=f"dc_fill_columns_{table_id}").set_value(["name"]).run()
    app.selectbox(key=f"dc_fill_strategy_{table_id}").set_value("custom").run()
    app.text_input(key=f"dc_fill_value_{table_id}").set_value("Not given").run()
    _click_and_settle(app, f"dc_apply_fill_missing_{table_id}")

    assert not app.exception
    cleaned = _cleaned(_only_table(app), SPACED_CSV)
    assert list(cleaned["name"]) == ["Ana", "Not given", "Not given", "Bo"]


def test_a_cell_holding_only_spaces_is_reported_as_blank(tmp_path, monkeypatch):
    """The reported bug: a Name column whose empty-looking cells hold a space showed as
    fully filled in the column panel and in the Missing metric."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("spaced.csv", SPACED_CSV))
    table = _only_table(app)
    stats = profiling.column_stats(_cleaned(table, SPACED_CSV))

    assert not app.exception
    assert stats.set_index("column").loc["name", "missing"] == 2


def test_every_fill_strategy_has_a_label_in_its_dialog(tmp_path, monkeypatch):
    """The page keeps its own label map, so a strategy added to FILL_STRATEGIES without
    a matching label would raise KeyError inside the selectbox's format_func."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("regions.csv", REGIONS_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "fill_missing")
    labels = app.selectbox(key=f"dc_fill_strategy_{table_id}").options

    assert not app.exception
    assert len(labels) == len(FILL_STRATEGIES)
    assert all(label and label not in FILL_STRATEGIES for label in labels)


def test_apply_to_all_tables_records_the_step_everywhere(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV))
    first = _tables(app)[0]

    _open(app, first.table_id, "trim_whitespace")
    app.checkbox(key=f"dc_trim_all_{first.table_id}").set_value(True).run()
    _click_and_settle(app, f"dc_apply_trim_whitespace_{first.table_id}")

    assert not app.exception
    for table in _tables(app):
        assert any(step["action"] == "trim_whitespace" for step in table.steps)


def test_removing_one_log_entry_replays_the_rest(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    _open(app, table_id, "trim_whitespace")
    _click_and_settle(app, f"dc_apply_trim_whitespace_{table_id}")
    assert len(_only_table(app).steps) == 2

    app.button(key=f"dc_remove_step_{table_id}_0").click().run()

    assert not app.exception
    remaining = _only_table(app).steps
    assert [step["action"] for step in remaining] == ["trim_whitespace"]


def test_reset_to_raw_dialog_clears_every_step(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    _open(app, table_id, "trim_whitespace")
    _click_and_settle(app, f"dc_apply_trim_whitespace_{table_id}")

    _open(app, table_id, "reset")
    assert app.session_state[session.DC_DIALOG_KEY] == {"table_id": table_id, "action": "reset"}

    _click_and_settle(app, "dc_confirm_reset_button")

    assert not app.exception
    assert _only_table(app).steps == []
    assert session.DC_DIALOG_KEY not in app.session_state


def test_cancelling_the_reset_dialog_keeps_the_steps(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    _open(app, table_id, "trim_whitespace")
    _click_and_settle(app, f"dc_apply_trim_whitespace_{table_id}")

    _open(app, table_id, "reset")
    _click_and_settle(app, "dc_cancel_reset_button")

    assert not app.exception
    assert len(_only_table(app).steps) == 2


def test_an_invalid_rename_is_rejected_without_entering_the_recipe(tmp_path, monkeypatch):
    """Validation runs before a step is admitted, which is what keeps a stored recipe
    well-formed for Stage 7 to serialize."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    before = len(_only_table(app).steps)

    _open(app, table_id, "rename_columns")
    app.selectbox(key=f"dc_rename_source_{table_id}").set_value("name").run()
    app.text_input(key=f"dc_rename_target_{table_id}").set_value("dept").run()
    # A plain click, not _click_and_settle: a rejected step leaves the dialog open on
    # purpose so the user can correct the input, and the extra run that helper performs
    # would clear the transient warning being asserted below.
    app.button(key=f"dc_apply_rename_columns_{table_id}").click().run()

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
    assert any("Saved as worksheet 'Q1_Q2 results'." in caption.value for caption in app.caption)


def _download_caption(app) -> str:
    return next(caption.value for caption in app.caption if caption.value.startswith("One workbook"))


def test_renaming_the_output_sheet_reaches_the_download(tmp_path, monkeypatch):
    """The rename happens inside a tab fragment while the workbook is materialized at
    page level, so without an app-scoped rerun the download would silently keep building
    itself under the old sheet name."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id
    assert "salaries" in _download_caption(app)

    app.text_input(key=f"dc_output_sheet_name_{table_id}").set_value("Payroll").run()

    assert not app.exception
    assert session.export_sheet_names(_tables(app)) == ["Payroll"]
    assert "Payroll" in _download_caption(app)
    assert "salaries" not in _download_caption(app)


def test_a_sanitized_sheet_name_settles_instead_of_rerunning_forever(tmp_path, monkeypatch):
    """The stored name is the sanitized one, so the change check has to compare against
    the sanitized input — comparing the raw text would differ on every rerun and loop."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    app.text_input(key=f"dc_output_sheet_name_{table_id}").set_value("Q1/Q2").run()
    app.run()

    assert not app.exception
    assert _only_table(app).output_sheet_name == "Q1_Q2"


def test_a_download_is_offered_with_one_sheet_per_table(tmp_path, monkeypatch):
    """AppTest's download_button exposes only its click state, not its bytes, so the
    workbook itself is covered in test_cleaner_export.py. What this asserts is that the
    page got far enough to build one and named the sheets from the uploaded files."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV))

    assert not app.exception
    assert app.download_button[0].label == "Download cleaned workbook"
    assert session.export_sheet_names(_tables(app)) == ["salaries", "other"]


def test_exporting_adopts_the_tables_before_switching_pages(tmp_path, monkeypatch):
    """The export menu is the push side of the handoff `TestCleanerHandoff` in
    test_chat_with_data_page.py covers from the pull side. It must land the tables in
    the Data Engine itself — not just navigate — since the destination page's own
    "Use these tables" button is what the pull side already exercises.

    `AppTest.from_file` runs one page in isolation, without the `st.navigation` registry
    `streamlit_app.py` builds at real runtime, so `st.switch_page("app_pages/chat_with_
    data.py")` has no page list to resolve against here and raises — a harness gap, not
    a bug (same category as the other testing gaps `docs/plan.md` already calls out).
    What's checked is that adoption completes and lands in session_state *before* that
    call, which is the part a real click depends on.
    """
    from engine import session as engine_session

    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))

    app.menu_button(key="dc_export_menu").click("Chat with Data").run()

    assert len(app.exception) == 1
    assert "Could not find page" in app.exception[0].value
    assert "chat_with_data.py" in app.exception[0].value

    tables = app.session_state[engine_session.DE_TABLES_KEY]
    assert tables
    assert all(table.from_cleaner for table in tables.values())


def test_start_over_discards_every_table(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))

    app.button(key="dc_start_over_button").click().run()

    assert not app.exception
    assert app.session_state[session.DC_TABLES_KEY] == {}


def test_rounding_offers_only_numeric_columns_and_records_the_step(tmp_path, monkeypatch):
    """`dept` holds words, so rounding can't reach it and the picker shouldn't offer it.
    `amount` is numeric by the time the dialog opens — detected types are applied on
    upload — so "$1,200.50" is already 1200.5 here."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "round_numbers")
    assert app.multiselect(key=f"dc_round_columns_{table_id}").options == ["amount"]
    app.multiselect(key=f"dc_round_columns_{table_id}").set_value(["amount"]).run()
    app.number_input(key=f"dc_round_decimals_{table_id}").set_value(0).run()
    app.segmented_control(key=f"dc_round_direction_{table_id}").set_value("down").run()
    _click_and_settle(app, f"dc_apply_round_numbers_{table_id}")

    assert not app.exception
    cleaned = _cleaned(_only_table(app), SALARIES_CSV)
    assert list(cleaned["amount"])[:2] == [1200.0, -300.0]
    assert _only_table(app).steps[-1]["action"] == "round_numbers"


# --------------------------------------------------------------------------------------
# Regression
# --------------------------------------------------------------------------------------


def test_an_action_button_below_its_panel_widgets_does_not_crash(tmp_path, monkeypatch):
    """Mirrors test_set_light_model_button_does_not_crash: a handler firing after its
    panel's widgets have already been instantiated must not raise
    StreamlitAPIException by writing to a widget's own session_state key."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "set_column_types")
    app.multiselect(key=f"dc_type_columns_{table_id}").set_value(["amount"]).run()
    app.selectbox(key=f"dc_type_target_{table_id}").set_value("numeric").run()
    _click_and_settle(app, f"dc_apply_set_column_types_{table_id}")

    assert not app.exception
    cleaned = _cleaned(_only_table(app), SALARIES_CSV)
    assert list(pd.to_numeric(cleaned["amount"]))[:2] == [1200.5, -300.0]


def test_changing_a_column_type_shows_up_in_the_column_panel(tmp_path, monkeypatch):
    """The reported bug: retyping a column left the panel showing the old type, because
    it re-detected from the values instead of reading what the recipe set. `dept` holds
    plain words, so detection alone would keep calling it text however often it is set."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    table_id = _only_table(app).table_id

    def panel_type(column: str) -> str:
        table = _only_table(app)
        stats = profiling.column_stats(
            _cleaned(table, SALARIES_CSV), declared_types=pipeline.declared_column_types(table.steps)
        )
        return stats.set_index("column").loc[column, "column_type"]

    assert panel_type("dept") == "text"

    _open(app, table_id, "set_column_types")
    app.multiselect(key=f"dc_type_columns_{table_id}").set_value(["dept"]).run()
    app.selectbox(key=f"dc_type_target_{table_id}").set_value("categorical").run()
    _click_and_settle(app, f"dc_apply_set_column_types_{table_id}")

    assert not app.exception
    assert panel_type("dept") == "categorical"


# --------------------------------------------------------------------------------------
# Summaries: group & total, pivot, unpivot
# --------------------------------------------------------------------------------------

SALES_CSV = (
    b"region,month,amount,order_id\n"
    b"North,Jan,10,a\nSouth,Jan,20,b\nNorth,Feb,30,c\nSouth,Feb,40,d\nNorth,Jan,5,a\n"
)


def _sources(app) -> list[session.TableState]:
    return [table for table in _tables(app) if table.derived_from is None]


def _summaries(app) -> list[session.TableState]:
    return [table for table in _tables(app) if table.derived_from is not None]


def _only_summary(app) -> session.TableState:
    summaries = _summaries(app)
    assert len(summaries) == 1
    return summaries[0]


def _effective(app, table: session.TableState) -> list[dict]:
    """The recipe a table really runs, resolved the way `session.effective_steps` does.

    Re-derived here rather than called, for the same reason as `_cleaned`: the real
    function reads `st.session_state`, which only the app's own thread may touch.
    """
    if table.derived_from is None:
        return table.steps
    parent = app.session_state[session.DC_TABLES_KEY][table.derived_from]
    return parent.steps + [table.reshape]


def _select_tab(app, table: session.TableState):
    """Marks a table's tab as the open one, for the next run only.

    Only the open tab's fragment runs, so a summary's own widgets don't exist in the
    element tree until its tab is selected. It has to be re-set before *every* run:
    `st.tabs` is a block rather than a widget in AppTest's tree, so AppTest sends no
    selection back and Streamlit falls to the first tab again on the following run.
    """
    tables = _tables(app)
    app.session_state[session.DC_TABS_KEY] = session.tab_labels(tables)[
        [each.table_id for each in tables].index(table.table_id)
    ]
    return app


def _summary_frame(app, table: session.TableState, payload: bytes) -> pd.DataFrame:
    raw = loaders.read_table(payload, table.file_name, table.sheet_name)
    return pipeline.apply_steps(raw, _effective(app, table))


def _save_group_summary(app, table_id: str, group_by: list[str], columns: list[str], functions: list[str]):
    """Fills in the Group & total dialog and saves it.

    `functions` is applied to every column in `columns`; the dialog has a picker per
    column, so a per-column choice is made by driving those keys directly.
    """
    _open(app, table_id, "group_summarise")
    app.multiselect(key=f"dc_summarise_groupby_{table_id}").set_value(group_by).run()
    app.multiselect(key=f"dc_summarise_values_{table_id}").set_value(columns).run()
    for column in columns:
        app.multiselect(key=f"dc_agg_functions_group_summarise_{table_id}_{column}").set_value(functions).run()
    return _click_and_settle(app, f"dc_save_summary_group_summarise_{table_id}")


def test_saving_a_group_summary_adds_a_derived_table(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id

    _save_group_summary(app, table_id, ["region"], ["amount"], ["sum"])

    assert not app.exception
    summary = _only_summary(app)
    assert summary.derived_from == table_id
    assert summary.reshape["action"] == "group_summarise"
    assert summary.steps == [], "a summary recipe lives on its parent, not on itself"

    frame = _summary_frame(app, summary, SALES_CSV)
    assert list(frame.columns) == ["region", "sum_of_amount"]
    assert sorted(frame["sum_of_amount"]) == [45.0, 60.0]


def test_a_summary_gets_its_own_tab_and_worksheet(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    _save_group_summary(app, _only_table(app).table_id, ["region"], ["amount"], ["sum"])

    tables = _tables(app)
    assert len(tables) == 2
    assert tables[1].derived_from is not None, "a summary sits straight after the table it came from"
    assert len(set(session.tab_labels(tables))) == 2
    assert len(session.export_sheet_names(tables)) == 2


def test_a_summary_follows_its_source_when_that_is_cleaned_further(tmp_path, monkeypatch):
    """The linkage invariant. A summary stores only its reshape, so cleaning the source
    afterwards has to reach it — otherwise two tables that look related disagree."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id
    _save_group_summary(app, table_id, ["region"], ["amount"], ["sum"])

    before = _summary_frame(app, _only_summary(app), SALES_CSV)
    assert sorted(before["sum_of_amount"]) == [45.0, 60.0]

    _open(app, table_id, "drop_duplicates")
    app.multiselect(key=f"dc_dupe_columns_{table_id}").set_value(["order_id"]).run()
    _click_and_settle(app, f"dc_apply_drop_duplicates_{table_id}")

    assert not app.exception
    summary = _only_summary(app)
    assert "drop_duplicates" in [step["action"] for step in _effective(app, summary)]
    # The duplicate order_id row carried 5, so the North total has to drop with it.
    assert sorted(_summary_frame(app, summary, SALES_CSV)["sum_of_amount"]) == [40.0, 60.0]


def test_removing_the_source_file_removes_its_summaries(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("other.csv", OTHER_CSV))
    sales = next(table for table in _sources(app) if table.file_name == "sales.csv")
    _save_group_summary(app, sales.table_id, ["region"], ["amount"], ["sum"])
    assert len(_summaries(app)) == 1

    _upload(app, ("other.csv", OTHER_CSV))

    assert not app.exception
    assert _summaries(app) == []
    assert len(_sources(app)) == 1


def test_a_summary_survives_reruns_of_the_uploader(tmp_path, monkeypatch):
    """`sync_tables` rebuilds the working set from the uploader every rerun, so a summary
    that is not carried over explicitly would vanish on the next interaction."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    _save_group_summary(app, _only_table(app).table_id, ["region"], ["amount"], ["sum"])

    app.run()
    app.run()

    assert not app.exception
    assert len(_summaries(app)) == 1


def test_editing_a_summary_replaces_it_rather_than_adding_another(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    _save_group_summary(app, _only_table(app).table_id, ["region"], ["amount"], ["sum"])
    summary = _only_summary(app)

    _select_tab(app, summary).run()
    app.button(key=f"dc_edit_summary_{summary.table_id}").click()
    _select_tab(app, summary).run()
    app.multiselect(key=f"dc_summarise_groupby_{summary.table_id}").set_value(["month"])
    _select_tab(app, summary).run()
    _select_tab(app, summary)
    _click_and_settle(app, f"dc_save_summary_group_summarise_{summary.table_id}")

    assert not app.exception
    edited = _only_summary(app)
    assert edited.table_id == summary.table_id
    assert edited.reshape["params"]["group_by"] == ["month"]


def test_deleting_a_summary_leaves_its_source_alone(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id
    _save_group_summary(app, table_id, ["region"], ["amount"], ["sum"])
    summary = _only_summary(app)

    _select_tab(app, summary).run()
    app.button(key=f"dc_delete_summary_{summary.table_id}").click()
    _select_tab(app, summary).run()
    _select_tab(app, summary)
    _click_and_settle(app, f"dc_confirm_delete_summary_{summary.table_id}")

    assert not app.exception
    assert _summaries(app) == []
    assert [table.table_id for table in _sources(app)] == [table_id]


def test_saving_a_pivot_produces_a_column_per_distinct_value(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id

    # `region` across the top rather than `month`: month names parse as dates, so upload
    # type detection makes that column a date and its pivoted headings would be timestamps.
    _open(app, table_id, "pivot")
    app.multiselect(key=f"dc_pivot_index_{table_id}").set_value(["order_id"]).run()
    app.selectbox(key=f"dc_pivot_columns_{table_id}").set_value("region").run()
    app.multiselect(key=f"dc_pivot_values_{table_id}").set_value(["amount"]).run()
    app.multiselect(key=f"dc_agg_functions_pivot_{table_id}_amount").set_value(["sum"]).run()
    _click_and_settle(app, f"dc_save_summary_pivot_{table_id}")

    assert not app.exception
    frame = _summary_frame(app, _only_summary(app), SALES_CSV)
    assert list(frame.columns) == ["order_id", "North", "South"]
    assert frame.set_index("order_id").loc["a", "North"] == 15.0


def test_a_pivot_can_show_several_values_each_with_its_own_function(tmp_path, monkeypatch):
    """The point of the per-column pickers: a min and a max of the same column side by
    side, which the old single value + single function pair could not express."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "pivot")
    app.multiselect(key=f"dc_pivot_index_{table_id}").set_value(["order_id"]).run()
    app.selectbox(key=f"dc_pivot_columns_{table_id}").set_value("region").run()
    app.multiselect(key=f"dc_pivot_values_{table_id}").set_value(["amount"]).run()
    app.multiselect(key=f"dc_agg_functions_pivot_{table_id}_amount").set_value(["min", "max"]).run()
    _click_and_settle(app, f"dc_save_summary_pivot_{table_id}")

    assert not app.exception
    frame = _summary_frame(app, _only_summary(app), SALES_CSV)
    assert list(frame.columns) == [
        "order_id",
        "min_of_amount / North",
        "min_of_amount / South",
        "max_of_amount / North",
        "max_of_amount / South",
    ]


def test_renaming_a_summarys_columns_carries_into_the_saved_table(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "group_summarise")
    app.multiselect(key=f"dc_summarise_groupby_{table_id}").set_value(["region"]).run()
    app.multiselect(key=f"dc_summarise_values_{table_id}").set_value(["amount"]).run()
    app.multiselect(key=f"dc_agg_functions_group_summarise_{table_id}_amount").set_value(["sum"]).run()
    app.text_input(key=f"dc_rename_output_group_summarise_{table_id}_sum_of_amount").set_value("Total sales").run()
    _click_and_settle(app, f"dc_save_summary_group_summarise_{table_id}")

    assert not app.exception
    summary = _only_summary(app)
    assert summary.reshape["params"]["output_names"] == {"sum_of_amount": "Total sales"}
    assert list(_summary_frame(app, summary, SALES_CSV).columns) == ["region", "Total sales"]


def test_a_text_column_is_not_offered_the_numeric_only_functions(tmp_path, monkeypatch):
    """Sum, average and median need numbers, so offering them for a text column would only
    produce a warning and a missing column later."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "group_summarise")
    app.multiselect(key=f"dc_summarise_groupby_{table_id}").set_value(["region"]).run()
    app.multiselect(key=f"dc_summarise_values_{table_id}").set_value(["order_id"]).run()

    offered = app.multiselect(key=f"dc_agg_functions_group_summarise_{table_id}_order_id").options
    assert "sum" not in offered and "count" in offered


def test_saving_an_unpivot_stacks_the_chosen_columns(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id

    _open(app, table_id, "unpivot")
    app.multiselect(key=f"dc_unpivot_ids_{table_id}").set_value(["region", "month"]).run()
    app.multiselect(key=f"dc_unpivot_values_{table_id}").set_value(["amount"]).run()
    _click_and_settle(app, f"dc_save_summary_unpivot_{table_id}")

    assert not app.exception
    frame = _summary_frame(app, _only_summary(app), SALES_CSV)
    assert list(frame.columns) == ["region", "month", "Attribute", "Value"]
    assert len(frame) == 5


def test_a_reshape_is_never_recorded_as_a_cleaning_step(tmp_path, monkeypatch):
    """A reshape changes the grain of the table, so recording it would silently replace
    the table being cleaned. The command bar routes these to `_save_summary` only."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    table_id = _only_table(app).table_id
    _save_group_summary(app, table_id, ["region"], ["amount"], ["sum"])

    source = next(table for table in _sources(app) if table.table_id == table_id)
    assert [step["action"] for step in source.steps] == ["set_column_types"]


def test_the_download_carries_a_sheet_per_summary(tmp_path, monkeypatch):
    """`build_download` runs on every rerun, so a summary it couldn't derive would surface
    as an st.error here rather than as a bad file later."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    summary = _save_group_summary(app, _only_table(app).table_id, ["region"], ["amount"], ["sum"]) and _only_summary(
        app
    )

    assert not app.exception
    assert not app.error
    assert app.download_button(key="dc_download_button")
    caption = next(caption.value for caption in app.caption if "One workbook" in caption.value)
    assert "2 sheet(s)" in caption
    assert summary.output_sheet_name in caption


# --------------------------------------------------------------------------------------
# Cleaning templates
# --------------------------------------------------------------------------------------


def _template_ids() -> list[int]:
    return [row["template_id"] for row in list_templates(1)]


def _save_as_template(app, name: str, description: str = ""):
    """Drives the bar's Save as template button and its dialog, as a user would.

    The button only sets a flag — the bar draws above `st.file_uploader` and a run that
    ended there would drop the uploaded files — so the dialog appears on the same run.
    """
    app.button(key="dc_template_save_as").click().run()
    app.text_input(key="dc_template_new_name").set_value(name).run()
    if description:
        app.text_area(key="dc_template_new_description").set_value(description).run()
    return _click_and_settle(app, "dc_template_save_confirm")


def _pick_template(app, template_id):
    """Chooses a template in the bar. `None` means the leading new-template entry, which
    the picker carries as a sentinel because `st.selectbox` reserves `None` itself for
    "nothing is selected" — an option holding it could be offered but never chosen.

    Selecting no longer applies anything — `_apply_template` is the press that does."""
    value = saved_picker.NONE_OPTION if template_id is None else template_id
    app.selectbox(key=session.DC_TEMPLATE_PICK_KEY).set_value(value).run()
    return app


def _apply_template(app):
    """Presses Apply cleaning steps, the one thing that runs a template's recipes."""
    app.button(key="dc_template_apply").click().run()
    return app


def _use_template(app, template_id):
    """Choose, then apply — the whole motion, for tests that are about the outcome."""
    return _apply_template(_pick_template(app, template_id))


def _trim(app, table_id: str):
    _open(app, table_id, "trim_whitespace")
    return _click_and_settle(app, f"dc_apply_trim_whitespace_{table_id}")


def test_the_template_bar_offers_a_new_template_by_default(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    assert not app.exception
    assert app.selectbox(key=session.DC_TEMPLATE_PICK_KEY).value == saved_picker.NONE_OPTION
    # Disabled rather than absent, so the bar keeps its shape on "new template".
    assert app.button(key="dc_template_schema").disabled
    assert app.button(key="dc_template_update").disabled


def test_saving_a_template_stores_every_uploaded_file_and_its_steps(tmp_path, monkeypatch):
    app = _upload(
        _make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV)
    )
    salaries = next(table for table in _sources(app) if table.file_name == "salaries.csv")
    _trim(app, salaries.table_id)

    _save_as_template(app, "Receivables", "Monthly pack.")

    assert not app.exception
    rows = list_templates(1)
    assert len(rows) == 1
    stored = load_template(rows[0]["template_id"], 1)
    assert sorted(stored.table_names()) == ["other", "salaries"]
    assert "trim_whitespace" in [step["action"] for step in stored.table("salaries").steps]
    assert stored.description == "Monthly pack."


def test_a_saved_template_stores_no_upload_scoped_identity(tmp_path, monkeypatch):
    """`table_id` and `file_id` are Streamlit's per-upload UUIDs. One stored here would
    point at nothing in the session that opens this template next month."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    file_id = _only_table(app).file_id
    _save_as_template(app, "Receivables")

    text = to_json(load_template(list_templates(1)[0]["template_id"], 1))
    assert file_id not in text
    assert "table_id" not in text


def test_applying_a_template_puts_its_steps_on_a_fresh_upload(tmp_path, monkeypatch):
    """And choosing it does not — the press is the whole action, and it is visible."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _trim(app, _only_table(app).table_id)
    _save_as_template(app, "Receivables")
    template_id = list_templates(1)[0]["template_id"]

    # A second session, the way next month arrives: the same file, none of the steps.
    fresh = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    assert "trim_whitespace" not in [step["action"] for step in _only_table(fresh).steps]

    _pick_template(fresh, template_id)
    assert fresh.session_state[session.DC_TEMPLATE_ID_KEY] == template_id
    assert "trim_whitespace" not in [step["action"] for step in _only_table(fresh).steps]

    _apply_template(fresh)

    assert not fresh.exception
    assert "trim_whitespace" in [step["action"] for step in _only_table(fresh).steps]


def test_a_template_chosen_before_the_upload_still_cleans_it(tmp_path, monkeypatch):
    """The order users actually work in — choose the pack, then upload this month's files.
    Selecting used to be the only thing that applied, so nothing ran and the page sat there
    showing raw data under a template it claimed to be cleaning under."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _trim(app, _only_table(app).table_id)
    _save_as_template(app, "Receivables")
    template_id = list_templates(1)[0]["template_id"]

    fresh = _make_app(tmp_path, monkeypatch)
    _pick_template(fresh, template_id)
    _upload(fresh, ("salaries.csv", SALARIES_CSV))
    _apply_template(fresh)

    assert not fresh.exception
    assert "trim_whitespace" in [step["action"] for step in _only_table(fresh).steps]


def test_applying_a_template_rebuilds_its_summary_tables(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    _save_group_summary(app, _only_table(app).table_id, ["region"], ["amount"], ["sum"])
    saved_name = _only_summary(app).output_sheet_name
    _save_as_template(app, "Sales pack")
    template_id = list_templates(1)[0]["template_id"]

    fresh = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    assert not _summaries(fresh)

    _use_template(fresh, template_id)

    assert not fresh.exception
    rebuilt = _only_summary(fresh)
    assert rebuilt.output_sheet_name == saved_name
    assert rebuilt.reshape["action"] == "group_summarise"


def test_applying_a_template_twice_does_not_duplicate_its_summaries(tmp_path, monkeypatch):
    """The template states what the working set should contain, not what to add to it."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    _save_group_summary(app, _only_table(app).table_id, ["region"], ["amount"], ["sum"])
    _save_as_template(app, "Sales pack")
    template_id = list_templates(1)[0]["template_id"]

    fresh = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
    _use_template(fresh, template_id)
    _apply_template(fresh)

    assert not fresh.exception
    assert len(_summaries(fresh)) == 1


def test_an_uploaded_file_the_template_does_not_mention_is_left_alone(tmp_path, monkeypatch):
    """The one place this parts company with a chat type, which drops what it doesn't
    expect. A template is applied *to* files the user chose to upload."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _save_as_template(app, "Salaries only")
    template_id = list_templates(1)[0]["template_id"]

    fresh = _upload(
        _make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV)
    )
    _use_template(fresh, template_id)

    assert not fresh.exception
    assert sorted(table.file_name for table in _sources(fresh)) == ["other.csv", "salaries.csv"]


def test_a_missing_expected_file_refuses_the_apply(tmp_path, monkeypatch):
    """Half a working set is not what the template's name means, so nothing is applied and
    the missing file is named. The button stays there for once it has been uploaded."""
    app = _upload(
        _make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV), ("other.csv", OTHER_CSV)
    )
    salaries = next(table for table in _sources(app) if table.file_name == "salaries.csv")
    _trim(app, salaries.table_id)
    _save_as_template(app, "Receivables")
    template_id = list_templates(1)[0]["template_id"]

    fresh = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _use_template(fresh, template_id)

    assert not fresh.exception
    assert "trim_whitespace" not in [step["action"] for step in _only_table(fresh).steps]
    assert any("other" in error.value for error in fresh.error)


def test_deselecting_a_template_keeps_every_step_it_applied(tmp_path, monkeypatch):
    """Deselecting is not undoing. Reverting a page of cleaning because a dropdown changed
    would be the worst kind of surprise; Start over is what undoes."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _trim(app, _only_table(app).table_id)
    _save_as_template(app, "Receivables")
    template_id = list_templates(1)[0]["template_id"]

    fresh = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _use_template(fresh, template_id)
    _pick_template(fresh, None)

    assert not fresh.exception
    assert session.DC_TEMPLATE_ID_KEY not in fresh.session_state
    assert "trim_whitespace" in [step["action"] for step in _only_table(fresh).steps]


def test_updating_a_template_overwrites_it_rather_than_adding_one(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _save_as_template(app, "Receivables")
    template_id = list_templates(1)[0]["template_id"]

    _trim(app, _only_table(app).table_id)
    app.button(key="dc_template_update").click().run()
    _click_and_settle(app, "dc_template_update_confirm")

    assert not app.exception
    assert _template_ids() == [template_id]
    stored = load_template(template_id, 1)
    assert "trim_whitespace" in [step["action"] for step in stored.tables[0].steps]


def test_a_name_already_in_use_is_refused_rather_than_silently_overwriting(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _save_as_template(app, "Receivables")

    app.button(key="dc_template_save_as").click().run()
    app.text_input(key="dc_template_new_name").set_value("receivables").run()
    app.button(key="dc_template_save_confirm").click().run()

    assert any("already have a template" in error.value for error in app.error)
    assert len(list_templates(1)) == 1


def test_deleting_a_template_takes_it_off_the_picker_without_touching_the_data(
    tmp_path, monkeypatch
):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _save_as_template(app, "Receivables")
    _pick_template(app, list_templates(1)[0]["template_id"])

    app.button(key="dc_template_delete").click().run()
    _click_and_settle(app, "dc_template_delete_confirm")

    assert not app.exception
    assert list_templates(1) == []
    assert session.DC_TEMPLATE_ID_KEY not in app.session_state
    # The picker's own key held the id that has just stopped being an option, and the
    # deferred reset is the only thing standing between that and a crash on the next run.
    assert app.session_state[session.DC_TEMPLATE_PICK_KEY] == saved_picker.NONE_OPTION
    assert len(_sources(app)) == 1


def test_show_expected_files_lists_what_the_template_wants(tmp_path, monkeypatch):
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _save_as_template(app, "Receivables")
    _pick_template(app, list_templates(1)[0]["template_id"])

    app.button(key="dc_template_schema").click().run()

    assert not app.exception
    assert app.button(key="dc_template_schema_close")


def test_start_over_keeps_the_selected_template(tmp_path, monkeypatch):
    """Uploading next month's files against the same saved steps is the normal reason to
    clear the working set, not a reason to forget which template they belong to."""
    app = _upload(_make_app(tmp_path, monkeypatch), ("salaries.csv", SALARIES_CSV))
    _save_as_template(app, "Receivables")
    template_id = list_templates(1)[0]["template_id"]
    _pick_template(app, template_id)

    app.button(key="dc_start_over_button").click().run()
    app.run()

    assert not app.exception
    assert not _tables(app)
    assert app.session_state[session.DC_TEMPLATE_ID_KEY] == template_id
