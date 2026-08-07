import base64
import re

import pandas as pd
import pytest

from dashboard import html_export, images
from dashboard.css_presets import DEFAULT_PRESET, preset_css
from dashboard.html_export import build_html, frame_to_html
from dashboard.model import UNTITLED_REPORT, PinnedItem, Report, add_section, assign_item

FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-bytes"


@pytest.fixture(autouse=True)
def no_real_rasterizing(monkeypatch):
    """Rasterizing launches a headless browser. Every test here stubs it, so the suite
    stays offline and fast — the real call is exercised by hand, not in CI."""
    monkeypatch.setattr(images, "figure_to_png", lambda figure, **kwargs: FAKE_PNG)


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({"region": ["North", "South"], "sales": [120, 340]})


def _report_with(*items: PinnedItem, title: str = "Q3 review") -> Report:
    report = Report(title=title)
    section = add_section(report, "Sales")
    for item in items:
        report.pool.append(item)
        assign_item(report, item.item_id, section.subsections[0].node_id)
    return report


def _css() -> str:
    return preset_css(DEFAULT_PRESET)


# --------------------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------------------


def test_the_stylesheet_lands_in_exactly_one_style_block(frame):
    html = build_html(_report_with(PinnedItem(heading="Sales", frame=frame)), _css())
    assert html.count("<style>") == 1
    assert "border-collapse" in html


def test_a_chart_is_embedded_as_a_base64_image(frame):
    item = PinnedItem(heading="Sales by region", frame=frame, figure=object())
    html = build_html(_report_with(item), _css())
    encoded = base64.b64encode(FAKE_PNG).decode("ascii")
    assert f'src="data:image/png;base64,{encoded}"' in html


def test_nothing_is_loaded_from_the_network(frame):
    item = PinnedItem(heading="Sales", frame=frame, figure=object(), comment="Steady.")
    html = build_html(_report_with(item), _css())
    assert "http://" not in html
    assert "https://" not in html


def test_a_table_carries_every_row():
    frame = pd.DataFrame({"n": range(250)})
    html = build_html(_report_with(PinnedItem(heading="All rows", frame=frame)), _css())
    assert html.count("<tr>") == 251  # 250 data rows plus the header row


def test_headings_and_comments_are_escaped(frame):
    item = PinnedItem(heading="<script>alert(1)</script>", comment="5 < 6 & 7 > 2", frame=frame)
    html = build_html(_report_with(item), _css())
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "5 &lt; 6 &amp; 7 &gt; 2" in html


def test_cell_values_are_escaped():
    frame = pd.DataFrame({"note": ["<b>bold</b>"]})
    html = build_html(_report_with(PinnedItem(heading="Notes", frame=frame)), _css())
    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_sections_are_numbered_from_position(frame):
    report = Report(title="Two parts")
    first = add_section(report, "Sales")
    second = add_section(report, "Costs")
    for section in (first, second):
        item = PinnedItem(frame=frame)
        report.pool.append(item)
        assign_item(report, item.item_id, section.subsections[0].node_id)

    html = build_html(report, _css())
    assert "1. Sales" in html
    assert "2. Costs" in html
    assert html.index("1. Sales") < html.index("2. Costs")


def test_an_untitled_report_still_gets_a_title(frame):
    html = build_html(_report_with(PinnedItem(frame=frame), title="  "), _css())
    assert UNTITLED_REPORT in html


def test_an_empty_report_renders_rather_than_failing():
    html = build_html(Report(title="Nothing here"), _css())
    assert "no placed items" in html


# --------------------------------------------------------------------------------------
# A chart that couldn't be drawn
# --------------------------------------------------------------------------------------


def test_a_failed_chart_falls_back_to_a_notice_and_its_table(monkeypatch, frame):
    monkeypatch.setattr(images, "figure_to_png", lambda figure, **kwargs: None)
    item = PinnedItem(heading="Sales", frame=frame, figure=object())

    html = build_html(_report_with(item), _css())
    assert "couldn't be included as a picture" in html
    assert "<img" not in html
    assert "North" in html


def test_an_item_that_never_had_a_chart_says_nothing_about_one(frame):
    html = build_html(_report_with(PinnedItem(heading="Sales", frame=frame)), _css())
    assert "couldn't be included as a picture" not in html


# --------------------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------------------


def test_a_chart_is_rasterized_once_across_both_exports(monkeypatch, frame):
    calls = []

    def counting_rasterize(figure, **kwargs):
        calls.append(figure)
        return FAKE_PNG

    monkeypatch.setattr(images, "figure_to_png", counting_rasterize)
    report = _report_with(PinnedItem(heading="Sales", frame=frame, figure=object()))

    build_html(report, _css())
    build_html(report, _css())
    assert len(calls) == 1


# --------------------------------------------------------------------------------------
# frame_to_html
# --------------------------------------------------------------------------------------


def test_frame_to_html_omits_the_index(frame):
    markup = frame_to_html(frame)
    assert "<th>region</th>" in markup
    assert not re.search(r"<th>\s*0\s*</th>", markup)


def test_frame_to_html_blanks_missing_values():
    markup = frame_to_html(pd.DataFrame({"a": [1, None]}))
    assert "NaN" not in markup
