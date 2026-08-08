"""The self-contained HTML report (requirement 6.4).

One file, no network. The stylesheet is inlined in a single `<style>` block, charts are
base64 `data:` images, and tables are real `<table>` elements — so the download opens the
same on a machine that has never heard of this app, which is the whole point of the
requirement.

Escaping is Jinja2's, on by default. Exactly two values reach the page unescaped and both
are accounted for: the stylesheet, which `css_presets.validate_css` screens (and which is
why that screening exists), and the table markup, which pandas generates itself with
`escape=True` so every cell and column name inside it is already escaped. Every other
string — the title, headings, comments — is user text and is escaped by the template.

Tables carry **every row**. Requirement 7.3 is explicit that on-screen output may be
capped but HTML and Excel exports always contain the full result, so there is no row limit
here and no "showing first N" notice to write.

Items are handed to the template already grouped into rows by `model.group_into_rows`. A
row of one is written exactly as an item always was — no wrapper element — so a report
that never touches the side-by-side toggle produces the same markup it did before, and
every preset and hand-edited stylesheet keeps styling it the way it already did.
"""

import base64
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

from dashboard.exceptions import ReportExportError
from dashboard.images import item_png
from dashboard.model import UNTITLED_REPORT, Report, walk

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "report.html.j2"

_HEADER_ALIGN_PATTERN = re.compile(r'<tr style="text-align:[^"]*;?">')


def _environment() -> Environment:
    """The Jinja2 environment, with autoescaping on for HTML.

    Built per call rather than cached at import: an export happens once per button press,
    and a module-level environment holding a compiled template would keep serving a stale
    one after the template file changed during development.
    """
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def frame_to_html(frame: pd.DataFrame) -> str:
    """One result set as a `<table>`, every row included.

    `escape=True` is what makes the `| safe` on this value in the template correct: pandas
    escapes cell values and column names itself, so nothing user-supplied survives as
    markup.

    The one edit to pandas' output is dropping the inline `text-align` it always writes
    onto the header row: an inline style beats a stylesheet, so leaving it there would let
    pandas override whichever preset the user chose.
    """
    return _HEADER_ALIGN_PATTERN.sub("<tr>", frame.to_html(index=False, escape=True, border=0, na_rep=""))


def _render_item(item) -> dict:
    """One pinned item flattened into what the template needs.

    `chart_failed` distinguishes "this item never had a chart" from "this item had one and
    it couldn't be rasterized" — the second earns a line of explanation next to the table
    that replaced it, the first should say nothing at all.
    """
    png = item_png(item)
    return {
        "heading": item.display_heading(),
        "comment": (item.comment or "").strip(),
        "image": base64.b64encode(png).decode("ascii") if png else "",
        "chart_failed": item.has_chart() and png is None,
        "table": frame_to_html(item.frame) if item.has_table() else "",
    }


def build_html(report: Report, css: str) -> str:
    """Renders the whole report to one self-contained HTML document.

    Raises:
        ReportExportError: if the template can't be rendered. Nothing is written until the
            string is complete, so a failure here costs the download and nothing else.
    """
    sections = [
        {
            "number": section.number,
            "name": section.name,
            "subsections": [
                {
                    "number": subsection.number,
                    "name": subsection.name,
                    # Named `entries` because Jinja resolves `.items` on a dict to
                    # `dict.items` before it ever looks for the key. Grouped into rows
                    # here rather than in the template, so the flag that decides layout is
                    # read by `group_into_rows` in one place.
                    "entries": [
                        [_render_item(item) for item in row] for row in subsection.rows()
                    ],
                }
                for subsection in section.subsections
            ],
        }
        for section in walk(report)
    ]

    try:
        template = _environment().get_template(TEMPLATE_NAME)
        return template.render(
            title=(report.title or "").strip() or UNTITLED_REPORT,
            css=css,
            generated_at=f"Generated {datetime.now():%d %b %Y, %H:%M}",
            sections=sections,
        )
    except (TemplateError, OSError) as error:
        logger.exception("Could not render the HTML report '%s'.", report.title)
        raise ReportExportError(
            "The HTML report couldn't be built. If you edited the stylesheet, switch back "
            "to a preset and try again."
        ) from error
