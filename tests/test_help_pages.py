"""Tests for utils/help_pages.py (phase 63)."""

from utils import help_pages


def test_help_file_found_by_page_path(tmp_path):
    (tmp_path / "meetings.html").write_text("<html></html>", encoding="utf-8")
    assert help_pages.help_file_for("meetings", tmp_path) == tmp_path / "meetings.html"


def test_empty_path_means_home(tmp_path):
    (tmp_path / "home.html").write_text("<html></html>", encoding="utf-8")
    assert help_pages.help_file_for("", tmp_path) == tmp_path / "home.html"


def test_page_without_file_gets_none(tmp_path):
    assert help_pages.help_file_for("settings", tmp_path) is None


def test_path_tricks_are_refused(tmp_path):
    (tmp_path / "secret.html").write_text("x", encoding="utf-8")
    assert help_pages.help_file_for("../secret", tmp_path) is None
    assert help_pages.help_file_for("a\\b", tmp_path) is None


def test_help_url_is_static_path(tmp_path):
    assert help_pages.help_url(tmp_path / "run task.html") == "app/static/help/run%20task.html"


def test_shipped_meetings_help_exists():
    assert help_pages.help_file_for("meetings") is not None
