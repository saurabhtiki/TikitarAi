"""Turning a Plotly figure into PNG bytes for the exports.

Requirement 6.4 asks for a *fully self-contained* HTML file, which rules out Plotly's own
HTML output — that needs plotly.js, and inlining it puts several megabytes into every
download and still leaves the file useless in Excel. So charts are rasterized once and
embedded, as a base64 `<img>` in the HTML and as a real picture in the workbook.

The one rule this module exists to enforce: **an export never fails because a chart could
not be drawn.** Rasterizing needs `kaleido`, which shells out to a headless Chromium and
can fail for reasons that have nothing to do with the report — a locked-down machine, a
missing browser, a figure with a font it can't resolve. So `figure_to_png` returns None on
any failure and every caller treats None as "no image": the HTML writes the item's data
table plus a plain notice, the workbook writes the table without the picture. Losing a
picture is a much smaller failure than losing the report.

The second thing it does is re-lay-out the figure before rendering it. A chart drawn for
the app is drawn for a browser that resizes its margins to fit the axis labels and for a
theme that may be dark; a chart drawn for the export is drawn once, at a fixed size, onto
a white page. `_prepared_for_export` is the difference between those two, applied to a
copy so the chart on screen is left exactly as it was.
"""

import logging
from typing import Any

import plotly.graph_objects as go

logger = logging.getLogger(__name__)

# Wide enough for a legend and a dozen x labels without crowding, in the 16:9-ish
# proportion the on-screen charts already use.
PNG_WIDTH = 1000
PNG_HEIGHT = 560

# Retina-ish. The picture is embedded, so the file grows with this — 2 is the point where
# text stops looking soft in print without doubling the size of every export again.
PNG_SCALE = 2

# Room for the axis labels and titles that live outside the plot area. `analyst.charts`
# draws with `margin={"l": 10, "r": 10, "t": 55, "b": 10}`, which is right on screen —
# plotly.js grows those margins in the browser to fit whatever the axes need. Kaleido
# renders at a fixed size and does not, so a chart exported with them ran its y tick
# labels off the left edge and printed the x axis title straight through the category
# names. These are floors, not fixed values: `automargin` below still expands them when
# the labels are long.
EXPORT_MARGIN = {"l": 70, "r": 30, "t": 60, "b": 80}

# The export is a white page in both formats, so it is drawn on white rather than on the
# transparent background the app uses to follow its own theme. Without this, a chart drawn
# under a dark theme carries pale axis text into the export and lands invisible on it.
EXPORT_PAPER = "#ffffff"
EXPORT_FONT_COLOUR = "#1f2933"


def _prepared_for_export(figure: Any) -> Any:
    """A copy of the figure laid out for a fixed-size white page.

    A copy, not the figure itself: the same object is still on screen in the chat and in
    the Dashboard's preview, and export margins that suit a 1000px page look wrong in a
    Streamlit column. `go.Figure(figure)` deep-copies the spec, so nothing here touches
    what the user is looking at.

    Height is dropped along with it — the figure carries the height set by the chat's own
    slider, which would otherwise fight the `height=` passed to `to_image` and squeeze the
    labels back into the margins this function just made room for.
    """
    export_figure = go.Figure(figure)
    export_figure.update_layout(
        margin=EXPORT_MARGIN,
        height=None,
        width=None,
        paper_bgcolor=EXPORT_PAPER,
        plot_bgcolor=EXPORT_PAPER,
        font_color=EXPORT_FONT_COLOUR,
    )
    # What actually stops a long category name or a five-digit tick being clipped: it lets
    # plotly grow the margin above to fit, which is the behaviour the browser gives for
    # free and a fixed-size render does not.
    export_figure.update_xaxes(automargin=True)
    export_figure.update_yaxes(automargin=True)
    return export_figure


def figure_to_png(
    figure: Any, *, width: int = PNG_WIDTH, height: int = PNG_HEIGHT, scale: int = PNG_SCALE
) -> bytes | None:
    """Rasterizes a Plotly figure, or returns None if it can't be.

    Never raises. `except Exception` is deliberate and is the one place in this codebase
    it is right: the failure surface is a subprocess launching a browser, and the
    exception types that come back out of kaleido are neither documented nor stable
    across versions. Narrowing it here would mean an export that dies on an exception type
    nobody anticipated — the exact outcome this function exists to prevent. It covers the
    layout copy above for the same reason: a figure that can't be copied is a chart to skip,
    not a report to lose.
    """
    if figure is None:
        return None

    try:
        image = _prepared_for_export(figure).to_image(
            format="png", width=width, height=height, scale=scale
        )
    except Exception:
        logger.exception("Could not rasterize a chart for export; the export will carry its table instead.")
        return None

    if not image:
        logger.warning("Chart rasterization returned no bytes; the export will carry its table instead.")
        return None
    return bytes(image)


def item_png(item: Any) -> bytes | None:
    """The PNG for a pinned item, rasterized once and cached on the item itself.

    A report is normally downloaded as HTML *and* as Excel, and rasterizing is by far the
    slowest thing either export does — so the second download of the same report reuses
    the pictures the first one made. The cache is dropped whenever the item's chart is
    replaced, because `dashboard.session.pin` builds a fresh `PinnedItem` each time.
    """
    if item.figure is None:
        return None
    if item.png is None:
        item.png = figure_to_png(item.figure)
    return item.png
