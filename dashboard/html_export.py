"""The self-contained HTML report (requirement 6.4).

One file, no network. The stylesheet is inlined in a single `<style>` block, charts are
base64 `data:` images, and tables are real `<table>` elements — so the download opens the
same on a machine that has never heard of this app, which is the whole point of the
requirement.

Escaping is Jinja2's, on by default. Exactly four values reach the page unescaped and all
are accounted for: the stylesheet, which `css_presets.validate_css` screens (and which is
why that screening exists); the table markup, which pandas generates itself with
`escape=True` so every cell and column name inside it is already escaped; and the comment,
which carries the toolbar's formatting and so goes through `rich_text.sanitize_comment` —
an allow-list of the handful of tags that toolbar can produce, with every attribute
dropped. The pasted-HTML block is the fourth, and is different in kind: it is not written
into the page at all but into an `<iframe sandbox="allow-scripts allow-same-origin"
srcdoc="...">`, escaped into that one attribute. It cannot navigate, submit or open a
plugin, and its stylesheet cannot touch the report around it — the frame is the boundary,
which is why `embed_html.sanitize_embed` no longer rewrites the markup for looks. Since
phase 19 it *can* run script and reach this app's own origin, deliberately, so a live embed
(Power BI and the like) can draw itself — see `embed_html`'s module docstring for why that
grant is scoped to internal reports. Every other string — the title, the headings — is user
text and is escaped by the template.

Since phase 22 one more value is worth calling out, though it is not unescaped: an External
Link block's address. It is escaped like every other string, but it goes into an `href` the
browser will *follow*, which escaping alone says nothing about — so it is put through
`model.link_problems` again here, and dropped if it would not be accepted today. That
allow-list of `http://` and `https://` is what keeps a `javascript:` line out of the page,
and it is applied on the way in (`model.set_item_link`), on the way out of a saved Task
(`skeleton._item_from_dict`) and here.

Sections and subsections are `<details>` elements as of phase 22, so the report opens as
its own table of contents. That is the browser's own folding, not this module's: the only
script in the exported page is the Expand all / Collapse all pair and the `beforeprint`
handler that opens every fold, because a section printed shut is a blank page.

Tables are cut to `pinned_tables.PREVIEW_ROWS`, with a line underneath saying how many rows
there were. A report is something a person reads, and a browser handed a hundred thousand
`<tr>` elements stops being readable long before it stops working. The **Excel** export is
not cut — requirements 6.4 and 7.5 both say "full data, no row limits" of the workbook, and
that is the file a person actually works in.

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

from dashboard.embed_html import embed_document, sanitize_embed
from dashboard.exceptions import ReportExportError
from dashboard.images import item_png
from dashboard.model import UNTITLED_REPORT, Report, link_problems, walk
from dashboard.pinned_tables import PREVIEW_ROWS
from dashboard.rich_text import sanitize_comment
from utils.dates import dates_as_text

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
    """One result set as a `<table>`. Cut to `PREVIEW_ROWS` — see `_table_note`.

    `escape=True` is what makes the `| safe` on this value in the template correct: pandas
    escapes cell values and column names itself, so nothing user-supplied survives as
    markup.

    The one edit to pandas' output is dropping the inline `text-align` it always writes
    onto the header row: an inline style beats a stylesheet, so leaving it there would let
    pandas override whichever preset the user chose. Dates are written dd-mm-yyyy.
    """
    shown = dates_as_text(frame.head(PREVIEW_ROWS))
    return _HEADER_ALIGN_PATTERN.sub("<tr>", shown.to_html(index=False, escape=True, border=0, na_rep=""))


def _table_note(frame: pd.DataFrame) -> str:
    """What is said under a table that has more rows than the report shows.

    Empty for a table that fits, so a short table carries no apology for a cut that never
    happened.
    """
    if len(frame) <= PREVIEW_ROWS:
        return ""
    return (
        f"Showing the first {PREVIEW_ROWS:,} of {len(frame):,} rows. "
        "The Excel download has all of them."
    )


def _render_item(number: str, item) -> dict:
    """One pinned item flattened into what the template needs.

    `number` is its "2.1.3" — derived from list position by `model.numbered_items`, the same
    way section and subsection numbers are, so it renumbers itself when the item moves.

    `chart_failed` distinguishes "this item never had a chart" from "this item had one and
    it couldn't be rasterized" — the second earns a line of explanation next to the table
    that replaced it, the first should say nothing at all. `chart_error` says *why*, so
    that line is something a reader can act on; it is escaped by the template like any
    other text, since it originates in an exception message.
    """
    png = item_png(item)
    return {
        "number": number,
        "heading": item.display_heading(),
        # Sanitized rather than escaped: it is rendered with `| safe` so the toolbar's
        # bold, italic, underline and lists survive into the page.
        "comment": sanitize_comment(item.comment),
        "image": base64.b64encode(png).decode("ascii") if png else "",
        "chart_failed": item.has_chart() and png is None,
        "chart_error": item.png_error,
        "table": frame_to_html(item.frame) if item.has_table() else "",
        "table_note": _table_note(item.frame) if item.has_table() else "",
        # A picture the user put on a block, as its own `data:` URI — kept apart from
        # `image` above, which is a rasterized chart, because one item may carry both.
        "picture": item.image_data_uri(),
        # Sanitized rather than escaped, and re-sanitized here rather than trusted from
        # the item: the same belt-and-braces the comment gets, since this is the value the
        # template renders with `| safe`.
        # The whole little document the frame shows, escaped into a `srcdoc` attribute by
        # the template. Re-sanitized here rather than trusted from the item, the same
        # belt-and-braces the comment gets.
        "embed": embed_document(sanitize_embed(item.embed_html)),
        "embed_height": item.embed_height,
        # Re-screened here rather than trusted from the item, the same belt-and-braces the
        # comment and the paste get: this is the only typed text the template writes into
        # an attribute the browser will follow, so a value that somehow reached the item
        # without passing `set_item_link` — out of an old saved Task, say — is dropped
        # rather than printed.
        "link_url": item.link_url.strip() if not link_problems(item.link_url) else "",
        "link_text": item.link_label(),
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
                        [_render_item(number, item) for number, item in row]
                        for row in subsection.numbered_rows()
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
            # Empty when there is no logo, which is what makes the template fall back to the
            # markup it always wrote. The bytes are this app's own — uploaded, size-checked
            # and mime-typed by `model.set_logo` — so the `data:` URI carries nothing the
            # user typed.
            logo=report.logo_data_uri(),
            logo_position=report.logo_position,
            logo_height=report.logo_height,
            generated_at=f"Generated {datetime.now():%d %b %Y, %H:%M}",
            sections=sections,
        )
    except (TemplateError, OSError) as error:
        logger.exception("Could not render the HTML report '%s'.", report.title)
        raise ReportExportError(
            "The HTML report couldn't be built. If you edited the stylesheet, switch back "
            "to a preset and try again."
        ) from error
