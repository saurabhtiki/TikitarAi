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
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Wide enough for a legend and a dozen x labels without crowding, in the 16:9-ish
# proportion the on-screen charts already use.
PNG_WIDTH = 1000
PNG_HEIGHT = 560

# Retina-ish. The picture is embedded, so the file grows with this — 2 is the point where
# text stops looking soft in print without doubling the size of every export again.
PNG_SCALE = 2


def figure_to_png(
    figure: Any, *, width: int = PNG_WIDTH, height: int = PNG_HEIGHT, scale: int = PNG_SCALE
) -> bytes | None:
    """Rasterizes a Plotly figure, or returns None if it can't be.

    Never raises. `except Exception` is deliberate and is the one place in this codebase
    it is right: the failure surface is a subprocess launching a browser, and the
    exception types that come back out of kaleido are neither documented nor stable
    across versions. Narrowing it here would mean an export that dies on an exception type
    nobody anticipated — the exact outcome this function exists to prevent.
    """
    if figure is None:
        return None

    try:
        image = figure.to_image(format="png", width=width, height=height, scale=scale)
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
