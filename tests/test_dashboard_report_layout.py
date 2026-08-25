"""The folded report and the boxes around it (phase 22).

Two things this suite is really holding:

* **Nothing is folded away from a printer.** A `<details>` printed shut is a blank page, so
  the export has to carry both the script that opens every fold before printing and the
  print rules that undo the rest of the new look.
* **A preset still wins.** Every rule added in phase 22 is written *before*
  `{{ css | safe }}`, which is what lets a user who picked Corporate — or hand-edited their
  own stylesheet years ago — keep the report looking the way they set it.
"""

import pandas as pd
import pytest

from dashboard import images
from dashboard.css_presets import PRESETS, DEFAULT_PRESET, preset_css
from dashboard.html_export import build_html
from dashboard.model import PinnedItem, Report, add_section, add_subsection, assign_item

FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-bytes"


@pytest.fixture(autouse=True)
def no_real_rasterizing(monkeypatch):
    monkeypatch.setattr(images, "figure_to_png", lambda figure, **kwargs: FAKE_PNG)


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({"region": ["North", "South"], "sales": [120, 340]})


def _two_section_report(frame) -> Report:
    report = Report(title="Q3 review")
    for name in ("Sales", "Stock"):
        section = add_section(report, name)
        add_subsection(section, "Detail")
        for subsection in section.subsections:
            item = PinnedItem(heading=f"{name} — {subsection.name}", frame=frame)
            report.pool.append(item)
            assign_item(report, item.item_id, subsection.node_id)
    return report


def _css() -> str:
    return preset_css(DEFAULT_PRESET)


# --------------------------------------------------------------------------------------
# Folding
# --------------------------------------------------------------------------------------


def test_every_section_and_subsection_is_a_fold(frame):
    html = build_html(_two_section_report(frame), _css())
    assert html.count('<details class="fold section">') == 2
    assert html.count('<details class="fold subsection">') == 4


def test_nothing_starts_open(frame):
    """The point of the phase: the report opens as its own table of contents."""
    html = build_html(_two_section_report(frame), _css())
    assert "<details class=\"fold section\" open>" not in html
    assert " open>" not in html


def test_the_headings_are_still_the_headings_a_preset_styles(frame):
    """An `<h2>` moved inside a `<summary>` is still an `<h2>`. If it ever stopped being
    one, every preset in the app would quietly lose its section styling."""
    html = build_html(_two_section_report(frame), _css())
    assert "<summary><h2>" in html
    assert "<summary><h3>" in html
    assert "1. Sales" in html
    assert "1.1 General" in html


def test_the_expand_and_collapse_pair_is_offered(frame):
    html = build_html(_two_section_report(frame), _css())
    assert 'data-fold="open"' in html
    assert 'data-fold="close"' in html


def test_an_empty_report_offers_nothing_to_expand():
    html = build_html(Report(title="Empty"), _css())
    # The body only: the rules for the controls and the script that drives them are
    # constants in the page either way — what must be absent is the pair itself.
    assert "fold-controls" not in html.split("<body>", 1)[1].split("<script>", 1)[0]
    assert "This report has no placed items yet." in html


def test_folds_are_opened_before_the_page_is_printed(frame):
    """Without this a printed report is a page of headings. It is the one thing the
    folding cannot do for itself, and the only reason the export carries any script."""
    html = build_html(_two_section_report(frame), _css())
    assert "beforeprint" in html
    assert "fold.open = true" in html


def test_the_report_still_works_with_no_script_at_all(frame):
    """`<details>` folds itself. Only the Expand all pair needs script, so a file opened
    with scripting off is still a report someone can read."""
    html = build_html(_two_section_report(frame), _css())
    body = html.split("<body>", 1)[1].split("<script>", 1)[0]
    assert "<details" in body
    assert "onclick" not in body


# --------------------------------------------------------------------------------------
# The look
# --------------------------------------------------------------------------------------


def test_items_and_subsections_are_boxed(frame):
    html = build_html(_two_section_report(frame), _css())
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".subsection { border:" in style
    assert ".item { border:" in style
    assert ".item:hover" in style


def test_a_long_table_gets_a_pinned_header_it_can_scroll_under(frame):
    html = build_html(_two_section_report(frame), _css())
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "position: sticky" in style
    # Sticky needs a scrollport with a height, or it is a rule that never fires.
    assert "max-height" in style


def test_print_undoes_everything_that_would_cost_a_page(frame):
    html = build_html(_two_section_report(frame), _css())
    print_rules = html.split("@media print {", 1)[1].split("}", 1)[0] + html.split("@media print {", 1)[1].split("\n}", 1)[0]
    assert ".fold-controls { display: none; }" in print_rules
    assert "max-height: none" in print_rules


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_every_preset_is_still_written_after_the_new_rules(preset, frame):
    """The ordering rule the whole phase rests on: a preset's own `.item` and `table` rules
    come last, so they override anything added here."""
    html = build_html(_two_section_report(frame), preset_css(preset))
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert style.index(".item:hover") < style.index("border-collapse")


def test_nothing_new_reaches_the_page_unescaped(frame):
    """The script is a constant and the fold classes are constants. No user text may end up
    in either — the report's own title going into a `<script>` would be the one way this
    phase could have opened a hole."""
    report = _two_section_report(frame)
    report.title = "</script><img src=x onerror=alert(1)>"
    html = build_html(report, _css())
    assert "<img src=x onerror" not in html
