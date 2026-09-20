"""One panel into one Vega-Lite spec (phase 32).

This is the module that makes the exported dashboard interactive rather than a picture.
**Nothing here computes a total.** A spec *declares* that a measure is summed by a category
and the browser does the arithmetic on every click - which is the whole reason cross-filtering
costs so little: handing a view a smaller set of rows is enough, and the chart re-totals
itself.

It is also plain Python returning a plain dict, which is deliberate and is the single biggest
practical win of the Vega-Lite choice: every chart type is unit-tested in pytest with no
browser, no screenshot and no provider.

Two rules hold the security story together, and both live here rather than in the JavaScript:

1. **No Vega expression is ever built from user input.** Filtering works by pushing *rows*
   into a view (see `assets/runtime.js`), never by composing a `datum.x == '...'` string. A
   cell value containing a quote is therefore data, and can never become logic.
2. **Every field name is checked against the real columns** before it reaches a spec, by
   `model.panel_problems`. A spec is only built for a panel that passed.

The vocabulary - chart kinds, aggregations, sorts, palettes - is imported from
`analyst.charts` rather than redeclared, so the dashboard the user downloads uses the same
words and the same colours as the charts they see inside the app.
"""

import logging

from analyst.charts import (
    AGG_AVERAGE,
    AGG_COUNT,
    AGG_MAXIMUM,
    AGG_MINIMUM,
    AGG_SUM,
    CHART_AREA,
    CHART_BAR,
    CHART_BAR_HORIZONTAL,
    CHART_LINE,
    CHART_PIE,
    CHART_SCATTER,
    CHART_PALETTES,
    PALETTE_DEFAULT,
    SORT_AUTOMATIC,
    SORT_LABEL,
    SORT_LARGEST,
    SORT_ORIGINAL,
    SORT_SMALLEST,
)
from live_dashboard.model import PanelSpec, THEME_DARK

logger = logging.getLogger(__name__)

#: The name every spec reads its rows from. One constant because the runtime pushes filtered
#: rows by this name - if the two ever disagreed, every chart would silently show everything.
DATA_NAME = "source"

#: Room a panel's own title and padding take, so a 450px panel draws a 450px box rather
#: than a 450px chart inside a taller one.
CHART_CHROME_HEIGHT = 64

#: The selection parameter a click sets. Also shared with the runtime, for the same reason.
SELECTION_NAME = "picked"

#: How much room one category label needs before it can be written straight across.
LABEL_ROOM_PIXELS = 70

#: Whether to lay the category labels flat or turn them on their side, decided in the
#: browser rather than here.
#:
#: Vega-Lite picks this at compile time and defaults to 270 - sideways - because at that
#: point it knows neither how wide the panel will be nor how many categories there are. On a
#: full-width panel showing three categories that reads badly for no reason. `width` and
#: `domain('x')` are both live in the browser, so the choice is deferred to where the answer
#: is actually known: flat when each label has room, sideways when it does not.
LABEL_ANGLE_EXPRESSION = {
    "expr": f"width / max(1, length(domain('x'))) >= {LABEL_ROOM_PIXELS} ? 0 : 270"
}

#: Our aggregation words to Vega-Lite's. Ours come from `analyst.charts` and read as English;
#: Vega-Lite's are its own vocabulary, and the two genuinely differ ("average" vs "mean").
_VEGA_AGGREGATES: dict[str, str] = {
    AGG_SUM: "sum",
    AGG_COUNT: "count",
    AGG_AVERAGE: "mean",
    AGG_MINIMUM: "min",
    AGG_MAXIMUM: "max",
}

#: Which Vega-Lite mark draws each of our chart kinds. A horizontal bar is the same mark as a
#: bar with its channels swapped, which `_encode_chart` does rather than this map.
_VEGA_MARKS: dict[str, str] = {
    CHART_BAR: "bar",
    CHART_BAR_HORIZONTAL: "bar",
    CHART_LINE: "line",
    CHART_AREA: "area",
    CHART_PIE: "arc",
    CHART_SCATTER: "point",
}

#: Charts whose category axis is a real ordering rather than a set of labels. Sorting these
#: biggest-first would scramble time, so `SORT_AUTOMATIC` leaves them alone.
_ORDERED_KINDS = frozenset({CHART_LINE, CHART_AREA, CHART_SCATTER})

# Theme colours for the chart interior. The page around it is styled by CSS custom
# properties; these are the values Vega-Lite needs told explicitly, since it paints its own
# background and axes.
_DARK_CONFIG = {
    "background": "transparent",
    "axis": {"labelColor": "#cbd5e1", "titleColor": "#e2e8f0", "gridColor": "#334155",
             "domainColor": "#475569", "tickColor": "#475569"},
    "legend": {"labelColor": "#cbd5e1", "titleColor": "#e2e8f0"},
    "title": {"color": "#e2e8f0"},
    "view": {"stroke": "transparent"},
}

_LIGHT_CONFIG = {
    "background": "transparent",
    "axis": {"labelColor": "#475569", "titleColor": "#1e293b", "gridColor": "#e2e8f0",
             "domainColor": "#cbd5e1", "tickColor": "#cbd5e1"},
    "legend": {"labelColor": "#475569", "titleColor": "#1e293b"},
    "title": {"color": "#1e293b"},
    "view": {"stroke": "transparent"},
}


def vega_aggregate(aggregation: str) -> str:
    """Our aggregation word as Vega-Lite's. Unknown words fall back to a sum with a log line,
    rather than emitting an operation Vega-Lite would reject and leaving a blank panel."""
    operation = _VEGA_AGGREGATES.get(aggregation)
    if operation is None:
        logger.warning("Unknown aggregation '%s' in a panel - totalling it instead.", aggregation)
        return "sum"
    return operation


def measure_title(panel: PanelSpec) -> str:
    """What to call the measure axis, e.g. "Sum of Amount" or "Count".

    Derived rather than stored so it keeps up with the panel: change the aggregation and the
    axis relabels itself, which is the behaviour `analyst.charts` already has.
    """
    if panel.aggregation == AGG_COUNT:
        return "Count"
    word = {AGG_SUM: "Sum", AGG_AVERAGE: "Average", AGG_MINIMUM: "Smallest",
            AGG_MAXIMUM: "Largest"}.get(panel.aggregation, "Sum")
    return f"{word} of {panel.measure_column}"


def _measure_field(panel: PanelSpec) -> dict:
    """The quantitative channel: the number, and what is done to it.

    A count deliberately carries no `field` - Vega-Lite's `{"aggregate": "count"}` counts
    rows, and naming a column there would instead count that column's non-null values, which
    is a different and usually wrong answer.
    """
    encoding: dict = {"type": "quantitative", "title": measure_title(panel)}
    if panel.aggregation == AGG_COUNT:
        encoding["aggregate"] = "count"
    else:
        encoding["field"] = panel.measure_column
        encoding["aggregate"] = vega_aggregate(panel.aggregation)
    return encoding


def _category_sort(panel: PanelSpec, measure_channel: str) -> object:
    """How the categories are ordered, as Vega-Lite's `sort` value.

    `"-y"` means "by the y channel, descending" - so the same intent produces `"-y"` on a
    vertical bar and `"-x"` on a horizontal one, which is why the channel is passed in rather
    than assumed.

    `SORT_AUTOMATIC` follows `analyst.charts`' own judgement: time and scatter axes keep their
    natural order, everything else goes biggest-first.
    """
    if panel.sort == SORT_LARGEST:
        return f"-{measure_channel}"
    if panel.sort == SORT_SMALLEST:
        return measure_channel
    if panel.sort == SORT_LABEL:
        return "ascending"
    if panel.sort == SORT_ORIGINAL:
        return None
    if panel.sort == SORT_AUTOMATIC:
        return None if panel.sub_type in _ORDERED_KINDS else f"-{measure_channel}"
    logger.info("Unknown sort '%s' in a panel - leaving the order alone.", panel.sort)
    return None


def _category_type(panel: PanelSpec, date_columns: frozenset[str]) -> str:
    """Whether the breakdown axis is a time axis or a set of labels.

    Lines and areas over a date column want `temporal` so Vega-Lite spaces the points by
    actual elapsed time rather than evenly; `payload` writes dates as ISO strings precisely so
    this works without any parsing hint.
    """
    if panel.sub_type in {CHART_LINE, CHART_AREA} and panel.group_by in date_columns:
        return "temporal"
    return "nominal"


def _top_n_transform(panel: PanelSpec) -> list[dict]:
    """The window-plus-filter pair that keeps only the biggest N categories.

    Done as a transform rather than by trimming the data, because the data is shared: the
    same rows feed every panel, and one panel's Top 10 must not take the other panels' rows
    away with it.
    """
    if not panel.top_n:
        return []

    if panel.aggregation == AGG_COUNT:
        measure_expression = {"op": "count", "as": "_rank_measure"}
    else:
        measure_expression = {
            "op": vega_aggregate(panel.aggregation),
            "field": panel.measure_column,
            "as": "_rank_measure",
        }

    return [
        {"aggregate": [measure_expression], "groupby": [panel.group_by]},
        {"window": [{"op": "rank", "as": "_rank"}],
         "sort": [{"field": "_rank_measure", "order": "descending"}]},
        {"filter": f"datum._rank <= {int(panel.top_n)}"},
    ]


def _colour_encoding(panel: PanelSpec, palette: list[str]) -> dict:
    """The legend channel, and the colours it uses.

    When there is no colour column the palette still has to be applied, or every bar draws in
    Vega-Lite's default blue and the exported dashboard stops matching the app's own charts.
    That case sets a single mark colour instead of a scale, since there is nothing to vary.
    """
    if not panel.colour_by:
        return {}
    return {
        "color": {
            "field": panel.colour_by,
            "type": "nominal",
            "title": panel.colour_by,
            "scale": {"range": list(palette)},
        }
    }


def _encode_chart(
    panel: PanelSpec, palette: list[str], date_columns: frozenset[str]
) -> tuple[dict, list[dict]]:
    """The encoding block and any transforms for one chart, by sub-type."""
    measure = _measure_field(panel)
    category = {
        "field": panel.group_by,
        "type": _category_type(panel, date_columns),
        "title": panel.group_by,
    }

    if panel.sub_type == CHART_PIE:
        # A pie has no axes: the measure is the slice angle and the category *is* the legend,
        # so the colour channel is the breakdown rather than an optional extra.
        return (
            {
                "theta": measure,
                "color": {
                    "field": panel.group_by,
                    "type": "nominal",
                    "title": panel.group_by,
                    "scale": {"range": list(palette)},
                },
            },
            [],
        )

    if panel.sub_type == CHART_SCATTER:
        # Raw points, not totals: a scatter exists to show the relationship between two
        # numbers, and aggregating it first would collapse the very spread being looked at.
        encoding = {
            "x": {"field": panel.group_by, "type": "quantitative", "title": panel.group_by},
            "y": {"field": panel.measure_column, "type": "quantitative",
                  "title": panel.measure_column},
        }
        encoding.update(_colour_encoding(panel, palette))
        return encoding, []

    if panel.sub_type == CHART_BAR_HORIZONTAL:
        # The categories already run flat down the side, so there is no angle to decide.
        category["sort"] = _category_sort(panel, "x")
        encoding = {"y": category, "x": measure}
    else:
        category["sort"] = _category_sort(panel, "y")
        category["axis"] = {"labelAngle": LABEL_ANGLE_EXPRESSION}
        encoding = {"x": category, "y": measure}

    encoding.update(_colour_encoding(panel, palette))
    return encoding, _top_n_transform(panel)


def build_vega_spec(
    panel: PanelSpec,
    palette_name: str = PALETTE_DEFAULT,
    theme: str = "",
    date_columns: frozenset[str] = frozenset(),
) -> dict:
    """One chart panel as a Vega-Lite v5 spec.

    The returned dict is embedded in the page's payload and handed to `vegaEmbed` by the
    runtime. It carries no data of its own - `{"name": DATA_NAME}` is a named dataset the
    runtime fills and refills as filters change.

    Args:
        panel: the panel to draw. Must already have passed `model.panel_problems`, which is
            what guarantees every field name here is a real column.
        palette_name: a key of `analyst.charts.CHART_PALETTES`.
        theme: `model.THEME_DARK` for the dark configuration, anything else for light.
        date_columns: which columns hold dates, so a line or area over one is spaced by
            elapsed time rather than evenly. Supplied by the caller because a panel
            describes intent, not the types of the data underneath it.

    Returns:
        A plain dict, ready for `json.dumps`. Never raises: an unknown sub-type falls back to
        a bar with a log line, because a panel drawn plainly is better than a page with a
        hole in it.
    """
    palette = CHART_PALETTES.get(palette_name) or CHART_PALETTES[PALETTE_DEFAULT]

    mark = _VEGA_MARKS.get(panel.sub_type)
    if mark is None:
        logger.warning("Unknown chart type '%s' in a panel - drawing it as a bar.", panel.sub_type)
        mark = "bar"

    encoding, transforms = _encode_chart(panel, palette, date_columns)

    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"name": DATA_NAME},
        "mark": {"type": mark, "tooltip": True},
        "encoding": encoding,
        "width": "container",
        "height": panel.height() - CHART_CHROME_HEIGHT,
        "autosize": {"type": "fit", "contains": "padding"},
        "config": dict(_DARK_CONFIG if theme == THEME_DARK else _LIGHT_CONFIG),
    }

    if mark == "arc":
        spec["mark"] = {"type": "arc", "tooltip": True, "innerRadius": 0}

    if transforms:
        spec["transform"] = transforms

    if not panel.colour_by and mark != "arc":
        # One series, so there is nothing to vary - paint it the palette's first colour
        # rather than leaving Vega-Lite's default blue to disagree with the rest of the app.
        spec["mark"]["color"] = palette[0]

    # What makes the chart clickable. The runtime listens for this parameter and turns it
    # into the cross-filter; a chart with no category to pick has nothing to select.
    if panel.sub_type != CHART_SCATTER:
        select_field = panel.group_by
        if select_field:
            spec["params"] = [
                {"name": SELECTION_NAME,
                 "select": {"type": "point", "fields": [select_field], "toggle": False}}
            ]

    return spec
