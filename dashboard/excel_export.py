"""The multi-sheet workbook export (requirement 6.4).

One sheet per subsection, which is why `dashboard.model` insists items live in subsections
and nowhere else: the mapping from report structure to workbook structure is then a fact
rather than a judgement call. A leading Contents sheet lists the report title and every
section and subsection against the sheet it landed on, because a workbook's tab strip
loses the numbering as soon as there are more than a handful of them.

Within a sheet, each item is laid out the way the report reads: heading, then the chart,
then the table, then the comment. The tables carry **every row** — requirement 7.3 caps
what is shown on screen, never what is exported.

Excel's limits, the column-width calculation and the frozen-header treatment all come from
`cleaner.export`, which already had them for the Data Cleaner's workbook. Nothing about
"what Excel accepts" is re-derived here.
"""

import io
import logging

import pandas as pd

from cleaner.export import MAX_EXCEL_ROWS, check_limits, column_width
from cleaner.exceptions import ExportError
from cleaner.naming import sanitize_sheet_names
from dashboard.exceptions import ReportExportError
from dashboard.images import PNG_HEIGHT, PNG_SCALE, item_png
from dashboard.model import UNTITLED_REPORT, Report, walk
from dashboard.rich_text import TextRun, to_runs, sanitize_comment

logger = logging.getLogger(__name__)

CONTENTS_SHEET_NAME = "Contents"

# The chart PNG is rendered at 2x for print sharpness, so it is placed at half size to
# land on the sheet at its intended dimensions.
IMAGE_SCALE = 0.5

# How many blank rows a placed chart needs under it. Excel floats pictures above the grid
# rather than occupying cells, so without this the next table would be written underneath
# one. Derived from the size the image is rendered and placed at (a default row is 20px)
# rather than read back out of the PNG header — the two modules already agree on the
# dimensions, and parsing them would only add a way to be wrong.
_PIXELS_PER_ROW = 20
_IMAGE_ROWS = int(PNG_HEIGHT * PNG_SCALE * IMAGE_SCALE / _PIXELS_PER_ROW) + 1

# How wide the merged comment block is. Wide enough to read a paragraph without wrapping
# into a column of single words, narrow enough not to swamp a two-column table.
_COMMENT_COLUMNS = 6
_COMMENT_ROW_HEIGHT = 46

# A merged cell does not grow itself, so a comment with bullets on separate lines has to
# be given the height its lines need or Excel simply hides the ones past the first.
_COMMENT_LINE_HEIGHT = 15

_BLANK_ROWS_BETWEEN_ITEMS = 2


def sheet_names_for(report: Report) -> dict[str, str]:
    """Maps each subsection's node id to the worksheet name it will be written to.

    Names are the numbered subsection labels ("1.1 By region") put through
    `cleaner.naming.sanitize_sheet_names`, so Excel's 31-character limit, its forbidden
    characters and its case-insensitive uniqueness rule are all handled by the code that
    already handles them for the Data Cleaner. `Contents` goes through the same pass as
    the first entry, so a subsection actually called "Contents" is the one that gets
    renamed.
    """
    labels = [CONTENTS_SHEET_NAME]
    node_ids: list[str] = []

    for section in walk(report):
        for subsection in section.subsections:
            labels.append(f"{subsection.number} {subsection.name}")
            node_ids.append(subsection.number)

    sanitized = sanitize_sheet_names(labels)
    return dict(zip(node_ids, sanitized[1:]))


def _contents_frame(report: Report, sheet_names: dict[str, str]) -> pd.DataFrame:
    records = []
    for section in walk(report):
        records.append({"Section": f"{section.number}. {section.name}", "Subsection": "", "Sheet": "", "Items": ""})
        for subsection in section.subsections:
            records.append(
                {
                    "Section": "",
                    "Subsection": f"{subsection.number} {subsection.name}",
                    "Sheet": sheet_names.get(subsection.number, ""),
                    "Items": len(subsection.items),
                }
            )
    return pd.DataFrame.from_records(records, columns=["Section", "Subsection", "Sheet", "Items"])


def _widen(widths: dict[int, int], frame: pd.DataFrame) -> None:
    """Records the width each column of this frame needs, keeping the widest seen.

    Several tables share one sheet, so a column's width is the widest any of them needs —
    sizing to the last table written would clip the ones above it.
    """
    for position, column in enumerate(frame.columns):
        widths[position] = max(widths.get(position, 0), column_width(frame[column], column))


def _check_sheet_height(sheet_name: str, rows: int) -> None:
    """A sheet holds several stacked tables, so the per-frame check isn't enough on its own."""
    if rows > MAX_EXCEL_ROWS:
        raise ExportError(
            f"'{sheet_name}' needs {rows:,} rows, more than Excel's limit of {MAX_EXCEL_ROWS:,}. "
            "Split this subsection across two, or filter the tables in it."
        )


def _build_formats(workbook) -> dict:
    """The cell formats, created once for the whole workbook.

    Built here rather than per sheet because xlsxwriter interns formats by object, not by
    definition: creating an identical bold format on every sheet stores a separate one in
    the file for each.
    """
    formats = {
        "title": workbook.add_format({"bold": True, "font_size": 14}),
        "heading": workbook.add_format({"bold": True, "font_size": 11}),
        "comment": workbook.add_format({"text_wrap": True, "valign": "top", "italic": True}),
        "note": workbook.add_format({"italic": True, "font_color": "#767f88"}),
    }
    # One format per combination of the three run styles a comment can carry. Built up
    # front for the same reason as the rest: a rich string needs a real format object per
    # fragment, and creating them per item would store a duplicate in the file each time.
    # Italic is on throughout, because the plain comment block has always been italic —
    # the toolbar's italic is expressed by the words being bold or underlined around it.
    for bold in (False, True):
        for underline in (False, True):
            formats[_run_format_key(bold, underline)] = workbook.add_format(
                {
                    "italic": True,
                    "bold": bold,
                    "underline": 1 if underline else 0,
                    # Only a rich string's *font* comes from its fragments; wrapping and
                    # alignment come from the cell. These are here because a comment in one
                    # single style is written as a plain string with this format as the
                    # cell's own, and it has to wrap like every other comment.
                    "text_wrap": True,
                    "valign": "top",
                }
            )
    return formats


def _run_format_key(bold: bool, underline: bool) -> str:
    return f"comment_run_{int(bold)}{int(underline)}"


def _comment_arguments(runs: list[TextRun], formats: dict) -> list:
    """One comment's runs as the alternating format/string arguments a rich string takes.

    Runs that share the styling of the one before them are already merged by `to_runs`, so
    every fragment here is a real change of formatting.
    """
    arguments: list = []
    for run in runs:
        if not run.text:
            continue
        arguments.append(formats[_run_format_key(run.bold, run.underline)])
        arguments.append(run.text)
    return arguments


def _write_comment(worksheet, row: int, runs: list[TextRun], formats: dict) -> None:
    """Writes the comment across the merged block, keeping its bold/italic/underline.

    xlsxwriter cannot merge and write a rich string in one call, so the documented pairing
    is used: merge the block empty with the comment format, then write the rich string
    into its top-left cell with that same format passed along.

    A comment in one single style — every comment written before the toolbar existed, and
    also one that is bold from end to end — has a single fragment, which
    `write_rich_string` rejects. It is written as a plain string instead, carrying that
    one fragment's own format so "all bold" still arrives bold.
    """
    text = "".join(run.text for run in runs)
    lines = text.count("\n") + 1
    worksheet.set_row(row, max(_COMMENT_ROW_HEIGHT, lines * _COMMENT_LINE_HEIGHT))
    worksheet.merge_range(row, 0, row, _COMMENT_COLUMNS - 1, "", formats["comment"])

    arguments = _comment_arguments(runs, formats)
    if len(arguments) <= 2:
        single_format = arguments[0] if arguments else formats["comment"]
        worksheet.write(row, 0, text, single_format)
        return
    worksheet.write_rich_string(row, 0, *arguments, formats["comment"])


def _write_subsection(writer: pd.ExcelWriter, sheet_name: str, subsection, formats: dict) -> None:
    """Lays one subsection's items down a single worksheet, in report order."""
    workbook = writer.book

    # Created empty first so there is a worksheet to write the title to before any frame
    # has been placed — `to_excel` is what normally creates it.
    worksheet = workbook.add_worksheet(sheet_name)
    writer.sheets[sheet_name] = worksheet

    worksheet.write(0, 0, f"{subsection.number} {subsection.name}", formats["title"])
    cursor = 2
    widths: dict[int, int] = {}

    # Numbered through the model's own `numbered()`, the same call the HTML export makes,
    # so "2.1.3" names the same item in the workbook as it does in the page.
    for number, item in subsection.numbered():
        worksheet.write(cursor, 0, f"{number} {item.display_heading()}", formats["heading"])
        cursor += 2

        png = item_png(item)
        if png is not None:
            worksheet.insert_image(
                cursor,
                0,
                f"{item.item_id}.png",
                {"image_data": io.BytesIO(png), "x_scale": IMAGE_SCALE, "y_scale": IMAGE_SCALE},
            )
            cursor += _IMAGE_ROWS
        elif item.has_chart():
            worksheet.write(cursor, 0, "This chart couldn't be included as a picture.", formats["note"])
            cursor += 2

        if item.has_table():
            frame = item.frame
            check_limits(sheet_name, frame)
            _check_sheet_height(sheet_name, cursor + len(frame) + 1)
            frame.to_excel(writer, sheet_name=sheet_name, index=False, na_rep="", startrow=cursor)
            _widen(widths, frame)
            cursor += len(frame) + 2

        # Through the same sanitizer the HTML export uses, so the workbook and the page
        # are showing the same comment — one as formatted runs, the other as markup.
        comment_runs = to_runs(sanitize_comment(item.comment))
        if comment_runs:
            _write_comment(worksheet, cursor, comment_runs, formats)
            cursor += 2

        cursor += _BLANK_ROWS_BETWEEN_ITEMS
        _check_sheet_height(sheet_name, cursor)

    for position, width in widths.items():
        worksheet.set_column(position, position, width)


def build_report_workbook(report: Report) -> bytes:
    """Builds the whole report as one .xlsx in memory.

    Raises:
        ReportExportError: if the report has nothing placed in it, if a table exceeds one
            of Excel's hard limits, or if the workbook can't be written. Every limit is
            checked as its sheet is laid out and raises before the file is finalized, so a
            rejected export produces no partial download.
    """
    if report.is_empty():
        raise ReportExportError(
            "There's nothing to export yet — place at least one pinned item into a subsection first."
        )

    sheet_names = sheet_names_for(report)
    buffer = io.BytesIO()

    try:
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            formats = _build_formats(writer.book)

            contents = _contents_frame(report, sheet_names)
            contents.to_excel(writer, sheet_name=CONTENTS_SHEET_NAME, index=False, na_rep="", startrow=1)
            contents_sheet = writer.sheets[CONTENTS_SHEET_NAME]
            contents_sheet.write(0, 0, report.title or UNTITLED_REPORT, formats["title"])
            for position, column in enumerate(contents.columns):
                contents_sheet.set_column(position, position, column_width(contents[column], column))

            for section in walk(report):
                for subsection in section.subsections:
                    _write_subsection(writer, sheet_names[subsection.number], subsection, formats)
    except ExportError as error:
        # Already written for the person downloading it — naming the sheet and the limit —
        # so it is re-raised as this package's type rather than reworded.
        logger.info("Report workbook rejected before writing: %s", error)
        raise ReportExportError(str(error)) from error
    except (ValueError, OSError, KeyError, TypeError) as error:
        logger.exception("Failed to build the report workbook for '%s'.", report.title)
        raise ReportExportError("The report workbook couldn't be created. Please try again.") from error

    return buffer.getvalue()
