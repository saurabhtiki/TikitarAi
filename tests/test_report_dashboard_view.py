"""Phase 46: a report's saved dashboard, viewed and downloaded by anyone.

Three layers, each against real storage rather than fixtures shaped like it:

- `live_dashboard.saved_view` draws the dashboard over a real saved DuckDB file, with the
  saved links reaching a child table, and names what it can't draw;
- `tasks.db.load_dashboard_spec` hands the dashboard - and only the dashboard - to an
  account that doesn't own the report;
- the pages: the viewer, and the Dashboard buttons on Reports and Chat with reports.
"""

import json
from pathlib import Path

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from chat_types.db import init_chat_types_table
from checks.db import init_check_sets_table
from engine.relationships import Relationship
from live_dashboard import model as m
from live_dashboard import saved_view
from llm.db import init_llm_table
from reports.db import init_report_datasets_table, record_dataset
from reports.model import ReportSetup, StoredTable
from reports.store import open_store, write_tables
from tasks.db import init_tasks_table, load_dashboard_spec, save_task
from tasks.exceptions import TaskStorageError
from tasks.model import Task

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VIEWER_PAGE = str(PROJECT_ROOT / "app_pages" / "report_dashboard.py")
REPORTS_PAGE = str(PROJECT_ROOT / "app_pages" / "run_task.py")
CHAT_REPORTS_PAGE = str(PROJECT_ROOT / "app_pages" / "chat_with_reports.py")

LINKS = [Relationship("sales", "cust_id", "customer", "cust_id")]
REGION = "customer - region"


def _source() -> duckdb.DuckDBPyConnection:
    """Sales (the child) and Customer (its parent), as a run would have loaded them."""
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE customer AS SELECT * FROM (VALUES (1, 'North'), (2, 'South')) AS t(cust_id, region)"
    )
    connection.execute(
        "CREATE TABLE sales AS SELECT * FROM (VALUES (1, 1, 100.0), (2, 2, 250.0), (3, 1, 50.0)) "
        "AS t(sale_id, cust_id, amount)"
    )
    return connection


def _spec(*, column: str = "amount") -> m.DashboardSpec:
    spec = m.DashboardSpec(title="Sales pulse")
    spec.panels.extend([
        m.PanelSpec(visual_type=m.VISUAL_FILTER, sub_type=m.FILTER_DROPDOWN, source_table="sales",
                    source_columns=[REGION], title="Region"),
        m.PanelSpec(visual_type=m.VISUAL_CHART, sub_type=m.CHART_BAR, source_table="sales",
                    measure_column=column, group_by=REGION, title="Sales by region"),
    ])
    return spec


def _payload(html: str) -> dict:
    block = html.split('<script id="dashboard-payload" type="application/json">')[1]
    return json.loads(block.split("</script>")[0])


# --------------------------------------------------------------------------------------
# saved_view
# --------------------------------------------------------------------------------------


def test_the_dashboard_is_drawn_from_saved_data_through_the_saved_links():
    """The filter's column lives on Customer, and reaches Sales only through the link."""
    built = saved_view.build_saved_dashboard(_source(), _spec(), LINKS, ["sales", "customer"])

    assert built.allowed and not built.problems and not built.truncated
    assert built.download_html == built.preview_html
    sales = _payload(built.preview_html)["tables"]["sales"]
    assert REGION in sales["columns"]
    assert len(sales["rows"]) == 3
    assert built.row_count == 5


def test_a_visual_whose_column_is_gone_is_named_rather_than_dropped():
    built = saved_view.build_saved_dashboard(
        _source(), _spec(column="discount"), LINKS, ["sales", "customer"]
    )
    assert len(built.problems) == 1
    assert "Sales by region" in built.problems[0]
    assert built.preview_html  # the rest of the page is still drawn


def test_too_many_rows_is_refused_rather_than_built(monkeypatch):
    monkeypatch.setattr(saved_view.payload, "ROW_LIMIT", 2)
    built = saved_view.build_saved_dashboard(_source(), _spec(), LINKS, ["sales", "customer"])
    assert not built.allowed
    assert "too many" in built.size_message
    assert built.preview_html == "" and built.download_html == ""


def test_a_large_table_is_cut_for_the_preview_but_not_the_download(monkeypatch):
    monkeypatch.setattr(saved_view, "PREVIEW_ROW_CAP", 1)
    built = saved_view.build_saved_dashboard(_source(), _spec(), LINKS, ["sales", "customer"])
    assert built.truncated
    assert len(_payload(built.preview_html)["tables"]["sales"]["rows"]) == 1
    assert len(_payload(built.download_html)["tables"]["sales"]["rows"]) == 3


def test_no_saved_tables_is_a_sentence():
    with pytest.raises(saved_view.DashboardDataError):
        saved_view.build_saved_dashboard(_source(), _spec(), LINKS, [])


def test_the_file_is_named_after_the_dashboard_or_the_report():
    assert saved_view.file_name(m.DashboardSpec(title="Sales / April"), "x") == "sales___april.html"
    assert saved_view.file_name(m.DashboardSpec(), "Monthly pay") == "monthly_pay.html"


# --------------------------------------------------------------------------------------
# tasks.db.load_dashboard_spec
# --------------------------------------------------------------------------------------


def _seed(tmp_path, monkeypatch, *, spec: m.DashboardSpec | None = None, with_data=True) -> int:
    """A report owned by the admin (user 1), with data saved the way a run leaves it."""
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    init_chat_types_table()
    init_check_sets_table()
    init_tasks_table()
    init_report_datasets_table()

    task = save_task(1, Task(name="Monthly sales", dashboard_spec=spec or _spec()))
    if with_data:
        source = _source()
        store = open_store(task.task_id)
        write_tables(store, source, ["customer", "sales"])
        store.close()
        source.close()
        record_dataset(
            task.task_id,
            ReportSetup(tables=[StoredTable("customer", 2), StoredTable("sales", 3)], relationships=LINKS),
            "Ravi",
        )
    return task.task_id


def test_the_dashboard_reads_back_for_any_account(tmp_path, monkeypatch):
    task_id = _seed(tmp_path, monkeypatch, with_data=False)
    spec = load_dashboard_spec(task_id)
    assert spec.title == "Sales pulse"
    assert [panel.title for panel in spec.panels] == ["Region", "Sales by region"]


def test_a_missing_report_says_so(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, with_data=False)
    with pytest.raises(TaskStorageError, match="no longer exists"):
        load_dashboard_spec(999)


# --------------------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------------------


def _app(page: str, *, view: dict | None = None) -> AppTest:
    app = AppTest.from_file(page, default_timeout=120)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    if view is not None:
        app.session_state["rd_view"] = view
    app.run()
    return app


def _texts(app) -> str:
    parts = [element.value for element in [*app.caption, *app.info, *app.warning, *app.error]]
    return "\n".join(str(part) for part in parts)


def test_the_viewer_shows_the_refresh_line_and_a_download(tmp_path, monkeypatch):
    task_id = _seed(tmp_path, monkeypatch)
    app = _app(VIEWER_PAGE, view={"task_id": task_id, "name": "Monthly sales",
                                  "back_page": "app_pages/run_task.py"})

    assert not app.exception
    assert "Data last refreshed" in _texts(app)
    assert "by Ravi" in _texts(app)
    assert app.get("download_button")


def test_a_report_with_no_dashboard_says_where_to_build_one(tmp_path, monkeypatch):
    task_id = _seed(tmp_path, monkeypatch, spec=m.DashboardSpec())
    app = _app(VIEWER_PAGE, view={"task_id": task_id, "name": "Monthly sales"})

    assert not app.exception
    assert "no dashboard yet" in _texts(app)
    assert not app.get("download_button")


def test_the_viewer_opened_on_nothing_points_at_the_buttons(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, with_data=False)
    app = _app(VIEWER_PAGE)
    assert not app.exception
    assert "Pick a report first" in _texts(app)


def test_the_reports_list_offers_the_dashboard_once_there_is_data(tmp_path, monkeypatch):
    task_id = _seed(tmp_path, monkeypatch)
    app = _app(REPORTS_PAGE)
    assert not app.button(key=f"rt_dashboard_{task_id}").disabled


def test_the_reports_list_disables_the_dashboard_before_a_run(tmp_path, monkeypatch):
    task_id = _seed(tmp_path, monkeypatch, with_data=False)
    app = _app(REPORTS_PAGE)
    assert app.button(key=f"rt_dashboard_{task_id}").disabled


def test_chat_with_reports_opens_the_viewer_on_that_report(tmp_path, monkeypatch):
    """`AppTest` runs one page without the navigation registry, so the switch itself raises
    "Could not find page" - what matters is that the report was named before it."""
    task_id = _seed(tmp_path, monkeypatch)
    app = _app(CHAT_REPORTS_PAGE)

    app.button(key=f"cr_dashboard_{task_id}").click().run()

    assert app.session_state["rd_view"]["task_id"] == task_id
    assert app.session_state["rd_view"]["back_page"] == "app_pages/chat_with_reports.py"
