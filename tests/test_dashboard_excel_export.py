"""Asserted against the pure builder rather than through the page.

`tests/test_data_cleaner_page.py` already records that `AppTest` cannot read the bytes
behind a download button, so a page-level test could only check that the button exists —
which `test_dashboard_page.py` does. What the workbook actually contains is checked here.
"""

import base64
import io

import pandas as pd
import pytest
from openpyxl import load_workbook

from dashboard import images
from dashboard.exceptions import ReportExportError
from dashboard.excel_export import CONTENTS_SHEET_NAME, build_report_workbook, sheet_names_for
from dashboard.model import (
    KIND_EMBED,
    KIND_IMAGE,
    KIND_TEXT,
    PinnedItem,
    Report,
    add_section,
    add_subsection,
    assign_item,
)

# A real 1×1 PNG rather than a few token bytes: xlsxwriter reads the header to size the
# picture, so a fake would fail inside the writer for reasons that say nothing about this
# code.
FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def no_real_rasterizing(monkeypatch):
    """Rasterizing launches a headless browser; the workbook only needs *some* bytes."""
    monkeypatch.setattr(images, "figure_to_png", lambda figure, **kwargs: (FAKE_PNG, ""))


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({"region": ["North", "South"], "sales": [120, 340]})


def _one_item_report(frame: pd.DataFrame, **item_fields) -> Report:
    report = Report(title="Q3 review")
    section = add_section(report, "Sales")
    item = PinnedItem(heading="Sales by region", frame=frame, **item_fields)
    report.pool.append(item)
    assign_item(report, item.item_id, section.subsections[0].node_id)
    return report


def _sheets(workbook_bytes: bytes) -> list[str]:
    return load_workbook(io.BytesIO(workbook_bytes)).sheetnames


# --------------------------------------------------------------------------------------
# Sheet naming
# --------------------------------------------------------------------------------------


def test_one_sheet_per_subsection_plus_contents(frame):
    report = Report(title="Two parts")
    section = add_section(report, "Sales")
    add_subsection(section, "By product")
    for subsection in section.subsections:
        item = PinnedItem(frame=frame)
        report.pool.append(item)
        assign_item(report, item.item_id, subsection.node_id)

    assert _sheets(build_report_workbook(report)) == [CONTENTS_SHEET_NAME, "1.1 General", "1.2 By product"]


def test_long_subsection_names_are_truncated_for_excel(frame):
    report = _one_item_report(frame)
    report.sections[0].subsections[0].name = "A" * 60

    name = _sheets(build_report_workbook(report))[1]
    assert len(name) <= 31


def test_forbidden_characters_are_replaced(frame):
    report = _one_item_report(frame)
    report.sections[0].subsections[0].name = "North/South: [2024]"

    assert "/" not in _sheets(build_report_workbook(report))[1]


def test_duplicate_subsection_names_are_still_distinct_sheets(frame):
    report = Report(title="Duplicates")
    first = add_section(report, "Sales")
    second = add_section(report, "Costs")
    for section in (first, second):
        section.subsections[0].name = "Summary"
        item = PinnedItem(frame=frame)
        report.pool.append(item)
        assign_item(report, item.item_id, section.subsections[0].node_id)

    sheets = _sheets(build_report_workbook(report))
    assert len(set(sheets)) == len(sheets)


def test_a_subsection_called_contents_does_not_displace_the_contents_sheet(frame):
    report = _one_item_report(frame)
    report.sections[0].subsections[0].name = ""

    names = sheet_names_for(report)
    assert CONTENTS_SHEET_NAME not in names.values()


def test_empty_subsections_get_no_sheet(frame):
    report = _one_item_report(frame)
    add_subsection(report.sections[0], "Nothing here")

    assert _sheets(build_report_workbook(report)) == [CONTENTS_SHEET_NAME, "1.1 General"]


# --------------------------------------------------------------------------------------
# Contents
# --------------------------------------------------------------------------------------


def test_the_contents_sheet_lists_the_title_and_every_subsection(frame):
    report = _one_item_report(frame)
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))[CONTENTS_SHEET_NAME]
    text = "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None)

    assert "Q3 review" in text
    assert "1. Sales" in text
    assert "1.1 General" in text


# --------------------------------------------------------------------------------------
# Sheet contents
# --------------------------------------------------------------------------------------


def test_a_sheet_carries_the_heading_the_comment_and_every_row(frame):
    report = _one_item_report(frame, comment="Sales held up in the North.")
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]
    text = "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None)

    assert "Sales by region" in text
    assert "Sales held up in the North." in text
    assert "North" in text and "South" in text
    assert "120" in text and "340" in text


def test_a_formatted_comment_reaches_the_workbook_as_words_not_tags(frame):
    """A merged cell cannot hold a list, so bullets arrive as markers on their own lines —
    and none of the markup itself may show up as text."""
    report = _one_item_report(
        frame, comment="<p><b>Up</b> again</p><ul><li>North</li><li>South</li></ul>"
    )
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]
    text = "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None)

    assert "Up again" in text
    assert "• North\n• South" in text
    assert "<b>" not in text and "<li>" not in text


def test_a_comment_that_is_bold_throughout_arrives_bold(frame):
    """One style end to end is a single fragment, which a rich string cannot hold — the
    comment is written plainly instead, and must not lose its formatting on the way."""
    report = _one_item_report(frame, comment="<p><b>Everything bold</b></p>")
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]
    written = next(
        cell for row in sheet.iter_rows() for cell in row if cell.value == "Everything bold"
    )

    assert written.font.bold


def test_an_item_heading_carries_the_same_number_the_html_report_gives_it(frame):
    report = _one_item_report(frame)
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]
    text = "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None)

    assert "1.1.1 Sales by region" in text


def test_a_full_table_is_written_with_no_row_limit():
    """Deliberately longer than the cut the HTML report makes — requirements 6.4 and 7.5
    both say "full data, no row limits" of the workbook, and it is the file a person
    actually works in. See `test_dashboard_html_export.py` for the other half of this."""
    frame = pd.DataFrame({"n": range(600)})
    report = _one_item_report(frame)
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]

    values = {cell.value for row in sheet.iter_rows() for cell in row}
    assert 0 in values and 599 in values


def test_a_chart_is_embedded_as_a_picture(frame):
    report = _one_item_report(frame, figure=object())
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]

    assert len(sheet._images) == 1


def test_a_chart_that_could_not_be_drawn_leaves_a_note_instead(monkeypatch, frame):
    monkeypatch.setattr(images, "figure_to_png", lambda figure, **kwargs: (None, "no browser found"))
    report = _one_item_report(frame, figure=object())
    sheet = load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]
    text = "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None)

    assert "couldn't be included as a picture" in text
    assert "no browser found" in text
    assert "North" in text


# --------------------------------------------------------------------------------------
# Blocks the user writes
# --------------------------------------------------------------------------------------


def _block_sheet(**item_fields):
    """One block, alone in a report, as the worksheet it lands on."""
    report = Report(title="Blocks")
    section = add_section(report, "Extras")
    item = PinnedItem(heading="Pasted", **item_fields)
    report.pool.append(item)
    assign_item(report, item.item_id, section.subsections[0].node_id)
    return load_workbook(io.BytesIO(build_report_workbook(report)))["1.1 General"]


def _text_of(sheet) -> str:
    return "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value is not None)


def test_a_blocks_picture_is_placed_in_the_sheet():
    sheet = _block_sheet(kind=KIND_IMAGE, image=FAKE_PNG, image_mime="image/png")

    assert len(sheet._images) == 1


def test_a_picture_that_is_not_really_one_leaves_a_note_instead_of_failing():
    """`set_item_image` checks the extension and the size, not the contents — so the file
    reaching xlsxwriter may be anything, and it must cost the picture and not the
    download."""
    sheet = _block_sheet(kind=KIND_IMAGE, image=b"this is not a picture", image_mime="image/png")

    assert "picture couldn't be included" in _text_of(sheet)


def test_pasted_html_is_written_as_real_cells():
    """A workbook cannot hold markup, and someone who pasted an Excel pivot in wants the
    grid back — sortable, totalable, in cells."""
    sheet = _block_sheet(
        kind=KIND_EMBED,
        embed_html="<table><tr><td>Region</td><td>Revenue</td></tr><tr><td>North</td><td>1240</td></tr></table>",
    )

    values = [[cell.value for cell in row] for row in sheet.iter_rows()]
    assert ["Region", "Revenue"] in [row[:2] for row in values]
    assert ["North", "1240"] in [row[:2] for row in values]


def test_pasted_html_with_no_table_in_it_says_so_rather_than_writing_tags():
    sheet = _block_sheet(kind=KIND_EMBED, embed_html="<p>Just a paragraph.</p>")

    text = _text_of(sheet)
    assert "no table in it" in text
    assert "<p>" not in text


def test_a_block_still_carries_its_heading_and_comment():
    sheet = _block_sheet(kind=KIND_TEXT, comment="<p>Revenue held up.</p>")

    text = _text_of(sheet)
    assert "1.1.1 Pasted" in text
    assert "Revenue held up." in text


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_an_empty_report_is_refused_with_a_readable_message():
    with pytest.raises(ReportExportError, match="place at least one"):
        build_report_workbook(Report(title="Nothing"))


def test_a_report_with_only_empty_sections_is_refused():
    report = Report(title="Skeleton")
    add_section(report, "Sales")
    with pytest.raises(ReportExportError):
        build_report_workbook(report)


def test_a_cell_over_excels_length_limit_is_refused_before_writing():
    frame = pd.DataFrame({"note": ["x" * 40_000]})
    with pytest.raises(ReportExportError, match="characters"):
        build_report_workbook(_one_item_report(frame))


def test_a_pasted_cell_starting_with_equals_stays_text():
    """`write` reads a leading `=` as a formula, so a pasted `=SUM(A1:A9)` became a live —
    and wrong — calculation in the user's workbook. These cells came out of someone else's
    web page as text and must land as text."""
    sheet = _block_sheet(
        kind=KIND_EMBED,
        embed_html="<table><tr><td>=SUM(A1:A9)</td></tr></table>",
    )

    cell = next(cell for row in sheet.iter_rows() for cell in row if cell.value == "=SUM(A1:A9)")
    assert cell.data_type == "s"


def test_a_pasted_cell_holding_a_url_does_not_become_a_hyperlink():
    sheet = _block_sheet(
        kind=KIND_EMBED,
        embed_html="<table><tr><td>http://example.com/a</td></tr></table>",
    )

    cell = next(cell for row in sheet.iter_rows() for cell in row if cell.value == "http://example.com/a")
    assert cell.data_type == "s"
    assert cell.hyperlink is None


def test_a_pasted_table_widens_its_columns():
    """It shares a sheet with real tables that were widened, and without this its own
    columns stayed at Excel's default and clipped every value in them."""
    sheet = _block_sheet(
        kind=KIND_EMBED,
        embed_html="<table><tr><td>A rather long pasted value indeed</td></tr></table>",
    )

    assert sheet.column_dimensions["A"].width > 8


def test_a_webp_picture_is_converted_rather_than_dropped():
    """The app accepts WEBP for a block picture and the HTML export embeds one happily, but
    xlsxwriter cannot hold it — so it used to be silently absent from every Excel download
    behind a note that said only "couldn't be included"."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buffer, format="WEBP")

    sheet = _block_sheet(kind=KIND_IMAGE, image=buffer.getvalue(), image_mime="image/webp")

    assert len(sheet._images) == 1


def test_a_picture_no_converter_can_read_still_explains_itself():
    sheet = _block_sheet(kind=KIND_IMAGE, image=b"not a picture at all", image_mime="image/webp")

    assert not sheet._images
    assert "WEBP" in _text_of(sheet)
