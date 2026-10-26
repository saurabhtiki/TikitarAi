from pathlib import Path

from streamlit.testing.v1 import AppTest

from utils import prebuilt_dashboards

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "prebuilt_reports.py")


def test_lists_html_files_by_name_sorted(tmp_path):
    (tmp_path / "sales_report.html").write_text("<p>s</p>", encoding="utf-8")
    (tmp_path / "CashFlowDashboard.htm").write_text("<p>c</p>", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    dashboards = prebuilt_dashboards.list_dashboards(tmp_path)

    assert list(dashboards) == ["Cash Flow Dashboard", "Sales Report"]
    assert dashboards["Sales Report"].name == "sales_report.html"


def test_missing_folder_gives_empty_list(tmp_path):
    assert prebuilt_dashboards.list_dashboards(tmp_path / "nope") == {}


def test_url_points_at_static_serving():
    url = prebuilt_dashboards.dashboard_url(Path("my report.html"))

    assert url == "app/static/reports/my%20report.html"


def _run_page(monkeypatch, folder):
    monkeypatch.setattr(prebuilt_dashboards, "DASHBOARDS_DIR", folder)
    at = AppTest.from_file(PAGE_PATH, default_timeout=10)
    at.run()
    return at


def test_page_shows_picker_and_open_link(tmp_path, monkeypatch):
    (tmp_path / "sales_report.html").write_text("<p>sales</p>", encoding="utf-8")

    at = _run_page(monkeypatch, tmp_path)

    assert not at.exception
    assert at.selectbox(key="prebuilt_report_choice").value == "Sales Report"
    link = at.get("link_button")[0]
    assert link.proto.url == "app/static/reports/sales_report.html"


def test_page_shows_note_when_folder_empty(tmp_path, monkeypatch):
    at = _run_page(monkeypatch, tmp_path)

    assert not at.exception
    assert "No reports yet" in at.info[0].value
