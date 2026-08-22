import base64
import re

import pandas as pd
import pytest

from dashboard import html_export, images
from dashboard.css_presets import DEFAULT_PRESET, preset_css
from dashboard.html_export import build_html, frame_to_html
from dashboard.model import (
    UNTITLED_REPORT,
    PinnedItem,
    Report,
    add_section,
    assign_item,
    set_logo,
    set_logo_height,
    set_logo_position,
)

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


def test_a_multi_line_comment_keeps_its_line_breaks(frame):
    item = PinnedItem(heading="Sales", frame=frame, comment="a\nb\nc")
    html = build_html(_report_with(item), _css())
    assert "a\nb\nc" in html  # the newlines survive into the page
    assert ".comment { white-space: pre-wrap; }" in html  # and the browser honours them


def test_the_report_stylesheet_still_wins_over_the_line_break_rule(frame):
    html = build_html(_report_with(PinnedItem(heading="Sales", frame=frame)), _css())
    assert html.index("white-space: pre-wrap") < html.index(_css())


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
# Side-by-side rows
# --------------------------------------------------------------------------------------


def test_a_report_without_side_by_side_items_writes_no_row_wrappers(frame):
    """The markup a report always produced, unchanged — which is what keeps every preset
    and every hand-edited stylesheet styling it the way it already did."""
    html = build_html(_report_with(PinnedItem(heading="A", frame=frame), PinnedItem(heading="B")), _css())
    # The class name is in the stylesheet either way — what must be absent is any element
    # carrying it.
    assert 'class="item-row' not in html
    assert html.count('<div class="item">') == 2


def test_toggled_items_are_wrapped_in_one_row(frame):
    html = build_html(
        _report_with(
            PinnedItem(heading="A", frame=frame),
            PinnedItem(heading="B", column_with_previous=True),
            PinnedItem(heading="C", column_with_previous=True),
        ),
        _css(),
    )
    assert html.count('class="item-row cols-3"') == 1
    assert html.count('<div class="item">') == 3


def test_a_row_ends_where_the_toggle_does(frame):
    html = build_html(
        _report_with(
            PinnedItem(heading="A", frame=frame),
            PinnedItem(heading="B", column_with_previous=True),
            PinnedItem(heading="C"),
        ),
        _css(),
    )
    assert html.count('class="item-row cols-2"') == 1
    assert "cols-3" not in html


def test_the_stylesheet_can_lay_a_row_out(frame):
    """The flex rules live in the shared block, so they arrive with every preset."""
    html = build_html(_report_with(PinnedItem(heading="A", frame=frame)), _css())
    assert ".item-row" in html
    assert "display: flex" in html


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


# --------------------------------------------------------------------------------------
# Item numbering
# --------------------------------------------------------------------------------------


LOGO_BYTES = b"\x89PNG\r\n\x1a\npretend-this-is-a-logo"


def test_each_item_carries_its_section_subsection_point_number(frame):
    report = _report_with(
        PinnedItem(heading="First", frame=frame),
        PinnedItem(heading="Second", frame=frame),
    )

    html = build_html(report, _css())

    assert "<h4>1.1.1 First</h4>" in html
    assert "<h4>1.1.2 Second</h4>" in html


def test_side_by_side_items_are_numbered_across_the_row(frame):
    report = _report_with(
        PinnedItem(heading="Left", frame=frame),
        PinnedItem(heading="Right", frame=frame, column_with_previous=True),
    )

    html = build_html(report, _css())

    assert html.index("1.1.1 Left") < html.index("1.1.2 Right")
    assert "item-row" in html


# --------------------------------------------------------------------------------------
# The header logo
# --------------------------------------------------------------------------------------


def test_a_report_without_a_logo_writes_the_header_it_always_did(frame):
    """Untouched reports must produce the markup every preset was written against, so the
    wrapper only appears when there is a logo to wrap. Checked on the body, because the
    shared stylesheet always carries the rules for it."""
    body = build_html(_report_with(PinnedItem(heading="One", frame=frame)), _css()).split("<body>")[1]

    assert "report-header" not in body
    assert "<h1>Q3 review</h1>" in body


def test_a_logo_is_embedded_as_a_data_uri_so_the_file_still_works_offline(frame):
    report = _report_with(PinnedItem(heading="One", frame=frame))
    set_logo(report, LOGO_BYTES, "company.png")

    html = build_html(report, _css())

    assert 'class="report-logo"' in html
    assert f'src="data:image/png;base64,{base64.b64encode(LOGO_BYTES).decode("ascii")}"' in html
    assert "http" not in html.split("<style>")[0]


def test_the_logo_position_and_height_reach_the_page(frame):
    report = _report_with(PinnedItem(heading="One", frame=frame))
    set_logo(report, LOGO_BYTES, "company.png")
    set_logo_position(report, "above")
    set_logo_height(report, 120)

    html = build_html(report, _css())

    assert 'class="report-header above"' in html
    assert "height: 120px;" in html
