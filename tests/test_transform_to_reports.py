"""Phase 47: Transform Data's finished table sent straight to a Report.

What matters is that a sent table is treated exactly as the same table downloaded and
uploaded again would be: loaded under the report's saved types, checked by the Reports
page's usual match, and refused **before** sending when a column the report needs is
missing - because on the Reports page a sent table has no file to remap.
"""

from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from auth.db import create_user, init_db, seed_default_admin
from chat_types import matching
from chat_types.model import ChatType, ExpectedColumn, ExpectedTable
from engine import loading
from engine import session as engine_session
from llm.db import init_llm_table
from runner import session as runner_session
from tasks.db import init_tasks_table, save_task
from tasks.model import Task
from transform import report_handoff
from transform import session
from transform.db import init_transform_pipelines_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRANSFORM_PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "transform_data.py")
SALES_CSV = b"customer_id,amount\n1,100\n1,200\n2,300\n"


def _schema(*columns: tuple[str, str], table: str = "sales") -> ChatType:
    return ChatType(
        name="Monthly sales",
        tables=[ExpectedTable(table, [ExpectedColumn(name, kind) for name, kind in columns])],
    )


SALES_SCHEMA = _schema(("customer_id", "id"), ("amount", "numeric"))


# --------------------------------------------------------------------------------------
# engine.loading.raw_from_frame
# --------------------------------------------------------------------------------------


def test_a_typed_table_goes_back_to_the_text_an_upload_would_hold():
    frame = pd.DataFrame({
        "when": pd.to_datetime(["2026-04-03", None]),
        "id": [7.0, None],
        "price": [1.5, 2.0],
        "name": ["Acme", None],
    })
    raw = loading.raw_from_frame(frame)

    assert raw["when"].tolist() == ["2026-04-03", None]
    assert raw["id"].tolist() == ["7", None]          # an id of 7 still joins to its master
    assert raw["price"].tolist() == ["1.5", "2.0"]
    assert raw["name"].tolist() == ["Acme", None]


def test_a_time_of_day_is_kept():
    raw = loading.raw_from_frame(pd.DataFrame({"at": pd.to_datetime(["2026-04-03 14:05:00"])}))
    assert raw["at"].tolist() == ["2026-04-03 14:05:00"]


# --------------------------------------------------------------------------------------
# transform.report_handoff
# --------------------------------------------------------------------------------------


def test_the_likely_file_is_chosen_by_name_or_by_being_the_only_one():
    assert report_handoff.default_target("Sales", ["customers", "sales"], 2) == "sales"
    assert report_handoff.default_target("merged_data", ["sales"], 1) == "sales"
    assert report_handoff.default_target("merged_data", ["sales", "customers"], 1) == report_handoff.NOT_SENT


def test_a_missing_column_stops_the_send_and_says_how_to_fix_it():
    frames = {"merged": pd.DataFrame({"customer_id": [1]})}
    problems = report_handoff.send_problems({"merged": "sales"}, frames, SALES_SCHEMA)
    assert len(problems) == 1
    assert "**amount**" in problems[0] and "Rename step" in problems[0]


def test_two_tables_as_one_file_is_refused():
    frames = {"a": pd.DataFrame({"customer_id": [1], "amount": [1]}),
              "b": pd.DataFrame({"customer_id": [1], "amount": [1]})}
    problems = report_handoff.send_problems({"a": "sales", "b": "sales"}, frames, SALES_SCHEMA)
    assert any("both sent as" in problem for problem in problems)


def test_nothing_chosen_is_refused_and_a_good_send_is_not():
    frame = pd.DataFrame({"customer_id": [1], "amount": [1], "extra": [0]})
    assert report_handoff.send_problems({"a": report_handoff.NOT_SENT}, {"a": frame}, SALES_SCHEMA)
    assert report_handoff.send_problems({"a": "sales"}, {"a": frame}, SALES_SCHEMA) == []


def test_files_nothing_is_sent_as_are_named():
    schema = ChatType(tables=[ExpectedTable("sales"), ExpectedTable("customers")])
    assert report_handoff.unsent_files({"a": "sales"}, schema) == ["customers"]


# --------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------


def _app(tmp_path, monkeypatch, schema: ChatType = SALES_SCHEMA, owner_id: int = 1) -> AppTest:
    """Transform Data with sales.csv loaded and ticked, and one saved report.

    `owner_id=2` saves the report under a second account, Anna, for phase 48.
    """
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    create_user("anna@example.com", "Anna", "Passw0rd!x", "normal_user")
    init_llm_table()
    init_transform_pipelines_table()
    init_tasks_table()
    save_task(owner_id, Task(name="Monthly sales", schema=schema))

    app = AppTest.from_file(TRANSFORM_PAGE_PATH, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.run()
    app.file_uploader(key=session.TF_UPLOADER_KEY).set_value(
        [("sales.csv", SALES_CSV, "application/octet-stream")]
    )
    app.run()
    app.button(key="tf_load_files").click().run()
    app.session_state[session.TF_EXPORT_KEY] = ["sales"]
    app.run()
    return app


def _open_dialog(app) -> AppTest:
    app.menu_button(key="tf_export_menu").click("Reports").run()
    return app


def test_reports_is_offered_and_opens_the_send_dialog(tmp_path, monkeypatch):
    app = _open_dialog(_app(tmp_path, monkeypatch))
    assert app.session_state[session.TF_DIALOG_KEY] == "send_report"
    assert app.selectbox(key="tf_form_send_task") is not None
    assert not app.button(key="tf_form_send_go").disabled


def test_sending_loads_the_table_as_the_reports_file_under_its_saved_types(tmp_path, monkeypatch):
    """`AppTest` has no page registry, so the final switch raises "Could not find page" -
    everything the Reports page reads is already in session state by then."""
    app = _open_dialog(_app(tmp_path, monkeypatch))
    app.button(key="tf_form_send_go").click().run()

    task = app.session_state[runner_session.RT_TASK_KEY]
    assert task.name == "Monthly sales"

    tables = {table.table_name: table for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
    assert list(tables) == ["sales"]
    assert tables["sales"].from_transform
    assert tables["sales"].semantic_types["customer_id"] == "id"   # the saved type, applied

    outcomes = app.session_state[engine_session.DE_LOAD_OUTCOMES_KEY]
    report = matching.check_upload(
        task.schema, outcomes, {name: table.semantic_types for name, table in tables.items()}
    )
    assert report.ok, report.problems()
    assert "Transform Data" in app.session_state[runner_session.RT_FLASH_KEY]


def test_someone_else_s_report_is_offered_with_its_owner_and_can_be_sent_to(tmp_path, monkeypatch):
    """Phase 48: anyone may run any report, so anyone may send a table to it."""
    app = _open_dialog(_app(tmp_path, monkeypatch, owner_id=2))
    assert app.selectbox(key="tf_form_send_task").options == ["Monthly sales · by Anna"]

    app.button(key="tf_form_send_go").click().run()

    assert app.session_state[runner_session.RT_TASK_KEY].name == "Monthly sales"


def test_a_missing_column_is_named_and_send_is_disabled(tmp_path, monkeypatch):
    schema = _schema(("customer_id", "id"), ("amount", "numeric"), ("region", "categorical"))
    app = _open_dialog(_app(tmp_path, monkeypatch, schema=schema))

    assert any("**region**" in error.value for error in app.error)
    assert app.button(key="tf_form_send_go").disabled


def test_cancel_sends_nothing(tmp_path, monkeypatch):
    app = _open_dialog(_app(tmp_path, monkeypatch))
    app.button(key="tf_form_send_cancel").click().run()

    assert session.TF_DIALOG_KEY not in app.session_state
    assert runner_session.RT_TASK_KEY not in app.session_state
