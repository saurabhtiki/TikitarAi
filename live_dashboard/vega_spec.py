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
    CHART_COMBO,
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
from live_dashboard.model import (
    AGG_DISTINCT,
    AGG_FIRST,
    AGG_LAST,
    AGG_MEDIAN,
    AGG_PERCENT_OF_TOTAL,
    AGG_Q1,
    AGG_Q3,
    AGG_RUNNING_TOTAL,
    AGG_STDEV,
    CHART_BAR_GROUPED,
    DASHBOARD_AGGREGATIONS,
    CHART_BAR_STACKED,
    CHART_BOXPLOT,
    CHART_DONUT,
    CHART_HEATMAP,
    AXIS_LEFT,
    AXIS_RIGHT,
    CHART_HISTOGRAM,
    CHART_COMBO,
    LABELLABLE_CHARTS,
    LEGEND_NONE,
    NAMED_COLOURS,
    PanelSpec,
    THEME_DARK,
    TRANSFORM_AGGREGATIONS,
)

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
    AGG_MEDIAN: "median",
    AGG_DISTINCT: "distinct",
    AGG_STDEV: "stdev",
    AGG_Q1: "q1",
    AGG_Q3: "q3",
}

#: Where an axis title needs different grammar from the picker's label. "Spread of Amount"
#: reads; "Standard deviation of Amount" is a mouthful on an axis. Anything not named here
#: uses `DASHBOARD_AGGREGATIONS`' own label, so a new aggregation gets a usable title without
#: being added to a second map.
_AXIS_WORDS: dict[str, str] = {
    AGG_MINIMUM: "Smallest",
    AGG_MAXIMUM: "Largest",
    AGG_STDEV: "Spread of",
    AGG_Q1: "Lower quarter of",
    AGG_Q3: "Upper quarter of",
    AGG_DISTINCT: "Unique",
    AGG_MEDIAN: "Median",
}

#: The column a transform aggregation leaves its answer in. Leading underscore so it cannot
#: collide with a real column: `flatten` prefixes its own names with the table they came from.
TRANSFORM_FIELD = "_measure"

#: The colours a heatmap runs through. A sequential scheme rather than the chart palette,
#: because a heatmap's colour is a *quantity* - the palette's categorical colours would put
#: an unrelated hue halfway up the scale and make a middling value look like a different kind
#: of thing.
HEATMAP_SCHEME = "blues"

#: How big the hole in a donut is, as a share of its radius.
DONUT_INNER_RADIUS = 60

#: Which Vega-Lite mark draws each of our chart kinds. A horizontal bar is the same mark as a
#: bar with its channels swapped, which `_encode_chart` does rather than this map.
_VEGA_MARKS: dict[str, str] = {
    CHART_BAR: "bar",
    CHART_BAR_HORIZONTAL: "bar",
    CHART_BAR_STACKED: "bar",
    CHART_BAR_GROUPED: "bar",
    CHART_LINE: "line",
    CHART_AREA: "area",
    CHART_PIE: "arc",
    CHART_DONUT: "arc",
    CHART_SCATTER: "point",
    CHART_HISTOGRAM: "bar",
    CHART_HEATMAP: "rect",
    CHART_BOXPLOT: "boxplot",
    CHART_COMBO: "bar",
}

#: Charts drawn from the rows themselves rather than from totals. Aggregating a scatter or a
#: box plot first would collapse the very spread they exist to show.
_RAW_ROW_KINDS = frozenset({CHART_SCATTER, CHART_BOXPLOT})

#: The two shapes drawn as a circle, where the category is the legend rather than an axis.
_ROUND_KINDS = frozenset({CHART_PIE, CHART_DONUT})

#: Marks that set their own colour - either from the data (a pie slice, a heatmap cell) or in
#: their encoding. Painting a single mark colour over these would flatten the very thing the
#: colour is carrying.
_SELF_COLOURING_MARKS = frozenset({"arc", "rect", "boxplot"})

#: Charts with no category to click, so no cross-filter to offer. A scatter plots two numbers
#: and a histogram's bars are bins rather than values anyone filters by. Private: callers ask
#: `selection_field` rather than re-deriving the answer from this.
_UNCLICKABLE_KINDS = frozenset({CHART_SCATTER, CHART_HISTOGRAM})

#: Charts whose category axis is a real ordering rather than a set of labels. Sorting these
#: biggest-first would scramble time, so `SORT_AUTOMATIC` leaves them alone.
_ORDERED_KINDS = frozenset({CHART_LINE, CHART_AREA, CHART_SCATTER, CHART_COMBO})

#: The shapes `labels:yes` can print a number on (phase 38), from the one list in `model` the
#: AI catalog and the help text read too. Left out on purpose: a heatmap already prints its
#: number as colour, a box plot's number is five numbers, a scatter of a thousand labelled
#: points is a black rectangle, a histogram's bars are bins rather than values, and a combo
#: chart has two numbers with no one place to put them.
_LABELLABLE_KINDS = frozenset(LABELLABLE_CHARTS)

#: How the number on a label is written. Short on purpose: `12,345` fits beside a bar and
#: `12,345.6789` does not.
LABEL_NUMBER_FORMAT = ",.0f"

#: How far the pie's percentages sit outside the slices, and how much of the panel the pie
#: itself takes. Outside rather than inside because the label is painted in its slice's own
#: colour - readable against the page, invisible against the slice.
PIE_LABEL_GAP = 16
PIE_RADIUS_PADDING = 34

#: Where a pie's percentage comes from: its share of every slice on the chart.
SHARE_FIELD = "_share"
SHARE_TOTAL_FIELD = "_share_total"
SHARE_VALUE_FIELD = "_share_value"

#: Where a chart's several numbers end up once they have been folded into one column: the
#: name of the measure, and its value. Folding is what turns "three numbers per category"
#: into rows Vega-Lite can colour and offset - one bar per measure, side by side.
MEASURE_NAME_FIELD = "_measure_name"
MEASURE_VALUE_FIELD = "_measure_value"

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


def measure_label(measure: dict) -> str:
    """One extra measure's name: "Average of Salary", "Count".

    This is not decoration - the name becomes the column the aggregate writes into and
    the entry in the legend, so the same measure has to name itself the same way every
    time or the fold reads a column that is not there.
    """
    aggregation = str(measure.get("aggregation") or "")
    if aggregation == AGG_COUNT:
        return "Count"
    word = _AXIS_WORDS.get(aggregation) or DASHBOARD_AGGREGATIONS.get(aggregation, "Sum")
    return f"{word} of {measure.get('column', '')}"


def measure_title(panel: PanelSpec) -> str:
    """What to call the measure axis, e.g. "Sum of Amount" or "Count".

    Derived rather than stored so it keeps up with the panel: change the aggregation and the
    axis relabels itself, which is the behaviour `analyst.charts` already has.
    """
    if panel.aggregation == AGG_COUNT:
        return "Count"
    if panel.aggregation == AGG_PERCENT_OF_TOTAL:
        return f"% of total {panel.measure_column}"
    if panel.aggregation == AGG_RUNNING_TOTAL:
        return f"Running total of {panel.measure_column}"
    word = _AXIS_WORDS.get(panel.aggregation) or DASHBOARD_AGGREGATIONS.get(
        panel.aggregation, "Sum"
    )
    return f"{word} of {panel.measure_column}"


def wants_labels(panel: PanelSpec) -> bool:
    """Whether this chart prints its numbers on itself.

    Asked in three places - the transforms, the encoding and the layers - so it is one
    function rather than three readings of the same property that could drift apart. A shape
    with nowhere sensible to put a number says no however the property is set, and
    `panel_problems` is where the user is told that.
    """
    if panel.draws_several_measures():
        # Three numbers per category is already three marks to read; a number written over
        # each of them is a category's worth of text in the width of one bar.
        return False
    return bool(panel.properties.get("labels")) and panel.sub_type in _LABELLABLE_KINDS


def _share_value_field(panel: PanelSpec) -> str:
    """Which column a pie's slices are measured from once `_share_transforms` has run."""
    if panel.aggregation in TRANSFORM_AGGREGATIONS:
        # `_transform_aggregation` has already written one number per slice into its own
        # column, and totalling it again would square it.
        return TRANSFORM_FIELD
    return SHARE_VALUE_FIELD


def _share_transforms(panel: PanelSpec) -> list[dict]:
    """A pie's percentages: each slice's total, the whole, and the share between them.

    The same grand-total pattern `_transform_aggregation` uses for "percentage of total",
    and it has to happen here too because a label is a *number in the data*, not a way of
    drawing one - Vega-Lite can print a field, and there is no field for "my share of this
    chart" until one is calculated.

    The slice totals are computed in the transform rather than in the encoding because the
    text layer and the arc layer must read the same one number. Two layers each doing their
    own aggregate is how a label ends up beside the wrong slice.
    """
    value = _share_value_field(panel)
    steps: list[dict] = []
    if value == SHARE_VALUE_FIELD:
        if panel.aggregation == AGG_COUNT:
            total = {"op": "count", "as": SHARE_VALUE_FIELD}
        else:
            total = {"op": vega_aggregate(panel.aggregation),
                     "field": panel.measure_column, "as": SHARE_VALUE_FIELD}
        steps.append({"aggregate": [total], "groupby": [panel.group_by]})

    return steps + [
        {"joinaggregate": [{"op": "sum", "field": value, "as": SHARE_TOTAL_FIELD}]},
        {"calculate": (f"datum['{SHARE_TOTAL_FIELD}'] ? datum['{value}'] / "
                       f"datum['{SHARE_TOTAL_FIELD}'] : 0"),
         "as": SHARE_FIELD},
    ]


def _chart_transforms(panel: PanelSpec) -> list[dict]:
    """Every transform this chart needs, decided in one place rather than per shape.

    Asked once by `build_vega_spec` instead of at each of `_encode_chart`'s return sites,
    because getting it wrong produces a wrong chart with no exception - and a new shape
    should not have to answer the question again to be drawn correctly.
    """
    if panel.sub_type in _RAW_ROW_KINDS or panel.sub_type == CHART_HISTOGRAM:
        return []  # drawn from the rows themselves, so there is nothing to total first
    if panel.sub_type in _ROUND_KINDS:
        # No axis to rank along, so no Top N. Percentage labels need the slices totalled
        # into a column of their own first - see `_share_transforms`.
        base = _transform_aggregation(panel)
        return base + _share_transforms(panel) if wants_labels(panel) else base
    return _transform_aggregation(panel) or _top_n_transform(panel)


def _measure_field(panel: PanelSpec) -> dict:
    """The quantitative channel: the number, and what is done to it.

    A count deliberately carries no `field` - Vega-Lite's `{"aggregate": "count"}` counts
    rows, and naming a column there would instead count that column's non-null values, which
    is a different and usually wrong answer.
    """
    encoding: dict = {"type": "quantitative", "title": measure_title(panel)}
    if panel.aggregation in TRANSFORM_AGGREGATIONS:
        # `_transform_aggregation` has already written the answer into one column, so this
        # channel reads it plainly - aggregating an aggregate would total it twice.
        encoding["field"] = TRANSFORM_FIELD
        return encoding
    if panel.aggregation == AGG_COUNT:
        encoding["aggregate"] = "count"
    elif panel.sub_type in _RAW_ROW_KINDS:
        # A box plot's mark does its own quartile maths over the rows it is given, so an
        # aggregate here would hand it one number per category to draw a box around.
        encoding["field"] = panel.measure_column
        encoding["title"] = panel.measure_column
    else:
        encoding["field"] = panel.measure_column
        encoding["aggregate"] = vega_aggregate(panel.aggregation)
    return encoding


def _transform_aggregation(panel: PanelSpec) -> list[dict]:
    """The transforms behind the four totals Vega-Lite has no aggregate operation for.

    Each ends with one row per group carrying `TRANSFORM_FIELD`, so `_measure_field` can read
    it as an ordinary column. Empty for every other aggregation.

    "Group" is the breakdown *and the colour column*, which is the correction phase 36 made
    here. An `aggregate` replaces its input with the group keys and the totals it computed,
    so a colour column left out of `groupby` simply stops existing: the chart still drew, in
    one colour, with a legend reading "null" - a wrong answer that looked like a right one.
    Grouping by both keeps a stack a stack, and each segment is totalled in its own right.
    """
    if panel.aggregation not in TRANSFORM_AGGREGATIONS:
        return []

    measure = panel.measure_column
    group_by = panel.group_by
    groups = [group_by] + ([panel.colour_by] if panel.colour_by else [])

    if panel.aggregation in {AGG_FIRST, AGG_LAST}:
        operation = "first_value" if panel.aggregation == AGG_FIRST else "last_value"
        return [
            # The whole partition, not the rows so far: `first_value` over a growing frame
            # would give every row its own answer.
            {"window": [{"op": operation, "field": measure, "as": TRANSFORM_FIELD}],
             "groupby": groups,
             "frame": [None, None]},
            {"aggregate": [{"op": "max", "field": TRANSFORM_FIELD, "as": TRANSFORM_FIELD}],
             "groupby": groups},
        ]

    totals = [
        {"aggregate": [{"op": "sum", "field": measure, "as": TRANSFORM_FIELD}],
         "groupby": groups},
    ]

    if panel.aggregation == AGG_RUNNING_TOTAL:
        # Into a new column and then copied back, rather than summed onto itself: a window
        # reading the column it is writing is the kind of thing that works until it doesn't.
        #
        # Partitioned by colour where there is one: a running total across the colours would
        # add one line's rise onto the next, which is not what either line claims to show.
        running = {"window": [{"op": "sum", "field": TRANSFORM_FIELD, "as": "_running"}],
                   "sort": [{"field": group_by, "order": "ascending"}],
                   "frame": [None, 0]}
        if panel.colour_by:
            running["groupby"] = [panel.colour_by]
        return totals + [
            running,
            {"calculate": "datum._running", "as": TRANSFORM_FIELD},
        ]

    # Percentage of total: the grand total joined back onto every row, then the division.
    # Guarded against a zero total, which is a real dataset rather than a rare one - a month
    # with nothing in it would otherwise make every bar `null` and the chart come out empty.
    return totals + [
        {"joinaggregate": [{"op": "sum", "field": TRANSFORM_FIELD, "as": "_grand_total"}]},
        {"calculate":
            f"datum._grand_total ? datum.{TRANSFORM_FIELD} / datum._grand_total * 100 : 0",
         "as": TRANSFORM_FIELD},
    ]


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
    if panel.sub_type in {CHART_LINE, CHART_AREA, CHART_COMBO} and panel.group_by in date_columns:
        return "temporal"
    return "nominal"


def _top_n_transform(panel: PanelSpec) -> list[dict]:
    """The transforms that keep only the biggest N categories, and nothing else.

    Done as a transform rather than by trimming the data, because the data is shared: the
    same rows feed every panel, and one panel's Top 10 must not take the other panels' rows
    away with it.

    `joinaggregate` rather than `aggregate`, which is the whole of this function's history:
    `aggregate` *replaces* its input with the group keys and the totals it computed, so the
    measure column - and any colour column - stopped existing before the encoding could read
    it. The chart then drew a titled, empty axis and no bars at all. `joinaggregate` adds the
    category's total to each row and leaves every column where it was, so the encoding does
    its own totalling afterwards exactly as it does on a chart with no Top N.

    `dense_rank` rather than `rank` for the same reason the rows are kept: the rank runs over
    rows, not categories, and only a dense rank gives every row of one category the same
    number. Two categories with the same total therefore share a place and both are kept, so
    a "top 5" over a tie can show six bars - the honest answer to an actual tie, and better
    than dropping one of two equals on row order.
    """
    if not panel.top_n:
        return []

    if panel.aggregation in TRANSFORM_AGGREGATIONS:
        # The transform has already collapsed the rows to one per category, and ranking them
        # again would total the totals. Left alone rather than half-applied, because a Top 10
        # that quietly changed what "% of total" meant would be worse than no Top 10.
        logger.info(
            "Top %s is not applied to a %s chart - the totals it ranks are already computed.",
            panel.top_n, panel.aggregation,
        )
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
        {"joinaggregate": [measure_expression], "groupby": [panel.group_by]},
        {"window": [{"op": "dense_rank", "as": "_rank"}],
         "sort": [{"field": "_rank_measure", "order": "descending"}]},
        {"filter": f"datum._rank <= {int(panel.top_n)}"},
    ]


def _legend_option(panel: PanelSpec) -> dict:
    """What `legend:` asks of the colour channel, as the keyword Vega-Lite takes.

    `{"legend": None}` is Vega-Lite's way of saying "no legend at all", which is why this
    returns the keyword rather than a position: `legend:none` has to remove the block, not
    place it somewhere. An empty dict when nothing was asked, so the caller can splat it.
    """
    position = str(panel.properties.get("legend", ""))
    if not position:
        return {}
    if position == LEGEND_NONE:
        return {"legend": None}
    return {"legend": {"orient": position}}


def _titled(channel: dict, panel: PanelSpec) -> dict:
    """One axis channel with its title removed if `axis_titles:no` asked for that.

    `None` rather than `""`: an empty string still reserves the room the title would have
    taken, so the chart shrinks without the label going away.
    """
    if panel.properties.get("axis_titles") is False:
        channel = dict(channel)
        channel["title"] = None
    return channel


def _single_colour(panel: PanelSpec, palette: list[str]) -> str:
    """The one colour a single-series chart is painted.

    The palette's first colour unless `colour:` named one. A chart broken down *by* colour
    ignores this - there the palette is carrying the breakdown, and one flat colour would
    erase what the legend is explaining.
    """
    named = NAMED_COLOURS.get(str(panel.properties.get("colour", "")))
    return named or palette[0]


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
            **_legend_option(panel),
        }
    }


def _encode_chart(
    panel: PanelSpec, palette: list[str], date_columns: frozenset[str]
) -> dict:
    """The encoding block for one chart, by sub-type.

    Encodings only. What has to be totalled first is `_chart_transforms`' question, asked
    once by `build_vega_spec` - so a new shape added here cannot get it wrong by forgetting
    to answer it on the way out.
    """
    measure = _measure_field(panel)
    category = {
        "field": panel.group_by,
        "type": _category_type(panel, date_columns),
        "title": panel.group_by,
    }

    if panel.sub_type in _ROUND_KINDS:
        # A pie has no axes: the measure is the slice angle and the category *is* the legend,
        # so the colour channel is the breakdown rather than an optional extra. A donut is
        # the same encoding with a hole, which `build_vega_spec` puts in the mark.
        if wants_labels(panel):
            # The slices are already totalled by `_share_transforms`, so the angle reads that
            # column plainly. Aggregating here as well would total the totals.
            measure = {"field": _share_value_field(panel), "type": "quantitative",
                       "title": measure_title(panel)}
        return (
            {
                "theta": measure,
                "color": {
                    "field": panel.group_by,
                    "type": "nominal",
                    "title": panel.group_by,
                    "scale": {"range": list(palette)},
                    **_legend_option(panel),
                },
            }
        )

    if panel.sub_type == CHART_HISTOGRAM:
        # The one chart with no breakdown: the bars *are* the breakdown. Vega-Lite chooses
        # the bin width from the data, which is what makes this useful without a setting for
        # it - the user asked to see the spread, not to pick a bucket size.
        return (
            {
                "x": _titled({"field": panel.measure_column, "type": "quantitative",
                              "bin": True, "title": panel.measure_column}, panel),
                "y": _titled({"aggregate": "count", "type": "quantitative",
                              "title": "How many rows"}, panel),
                "color": {"value": _single_colour(panel, palette)},
            }
        )

    if panel.sub_type == CHART_HEATMAP:
        # Two categories and a number: the colour carries the measure, so there is no
        # measure axis and `_colour_encoding`'s legend channel is used as the second axis.
        measure["scale"] = {"scheme": HEATMAP_SCHEME}
        return (
            {
                "x": _titled({"field": panel.group_by, "type": "nominal",
                              "title": panel.group_by}, panel),
                "y": _titled({"field": panel.colour_by, "type": "nominal",
                              "title": panel.colour_by}, panel),
                "color": {**measure, **_legend_option(panel)},
            }
        )

    if panel.sub_type == CHART_BOXPLOT:
        # Raw rows: the mark works out the median, the quartiles and the outliers itself,
        # which is the whole point of showing one.
        return (
            {
                "x": _titled({"field": panel.group_by, "type": "nominal",
                              "title": panel.group_by,
                              "axis": {"labelAngle": LABEL_ANGLE_EXPRESSION}}, panel),
                "y": _titled(measure, panel),
                "color": {"value": _single_colour(panel, palette)},
            }
        )

    if panel.sub_type == CHART_SCATTER:
        # Raw points, not totals: a scatter exists to show the relationship between two
        # numbers, and aggregating it first would collapse the very spread being looked at.
        encoding = {
            "x": _titled({"field": panel.group_by, "type": "quantitative",
                          "title": panel.group_by}, panel),
            "y": _titled({"field": panel.measure_column, "type": "quantitative",
                          "title": panel.measure_column}, panel),
        }
        encoding.update(_colour_encoding(panel, palette))
        return encoding

    if panel.sub_type == CHART_BAR_HORIZONTAL:
        # The categories already run flat down the side, so there is no angle to decide.
        category["sort"] = _category_sort(panel, "x")
        encoding = {"y": _titled(category, panel), "x": _titled(measure, panel)}
    else:
        category["sort"] = _category_sort(panel, "y")
        category["axis"] = {"labelAngle": LABEL_ANGLE_EXPRESSION}
        encoding = {"x": _titled(category, panel), "y": _titled(measure, panel)}

    encoding.update(_colour_encoding(panel, palette))

    if panel.sub_type == CHART_BAR_GROUPED and panel.colour_by:
        # The single line between stacked and grouped: `xOffset` gives each colour its own
        # slot inside the category instead of a segment on top of the one below.
        encoding["xOffset"] = {"field": panel.colour_by, "type": "nominal"}

    return encoding


def _label_layers(panel: PanelSpec, spec: dict) -> list[dict]:
    """The chart, plus a second layer that writes its numbers on it.

    Layers rather than a label option, because Vega-Lite has no "show the values" switch:
    text is a mark like any other, and the way to put it over bars is to draw it over them.

    Both layers share the spec's one top-level `encoding`, so the text sits exactly where the
    mark it describes is - the category, the stacking and the colours are decided once. Only
    what is *written* and where it is nudged to differs, which is this function's whole job.
    """
    if panel.sub_type in _ROUND_KINDS:
        # Outside the slices, in the slice's own colour, inherited from the top-level colour
        # channel - a percentage painted inside its own slice is the one place it cannot be
        # read, and a pie is asked "what share is that?" rather than "how many?".
        outer = max(40, (panel.height() - CHART_CHROME_HEIGHT) / 2 - PIE_RADIUS_PADDING)
        arc = dict(spec["mark"])
        arc["outerRadius"] = outer
        return [
            {"mark": arc},
            {"mark": {"type": "text", "radius": outer + PIE_LABEL_GAP, "fontSize": 11},
             "encoding": {"text": {"field": SHARE_FIELD, "type": "quantitative",
                                   "format": ".0%"}}},
        ]

    if panel.sub_type == CHART_BAR_HORIZONTAL:
        # The bars run rightwards, so the number goes past the end of each one.
        nudge = {"align": "left", "baseline": "middle", "dx": 4}
    else:
        nudge = {"baseline": "bottom", "dy": -4}

    number = dict(_measure_field(panel))
    number.pop("title", None)
    number["format"] = LABEL_NUMBER_FORMAT

    return [
        {"mark": spec["mark"]},
        {"mark": {"type": "text", "fontSize": 11, **nudge}, "encoding": {"text": number}},
    ]


def _measure_names(measures: list[dict]) -> list[str]:
    """Each measure's name, with a repeat made unique.

    Two measures can honestly want the same name - the same column totalled the same way
    on both axes - and an `aggregate` writing twice into one column silently keeps one of
    them. A numbered suffix is ugly and visible, which is the right trade against a
    measure that quietly disappears.
    """
    names: list[str] = []
    for measure in measures:
        name = measure_label(measure)
        if name in names:
            name = f"{name} ({names.count(name) + 1})"
        names.append(name)
    return names


def _aggregate_every_measure(panel: PanelSpec) -> list[dict]:
    """One `aggregate` that totals every number this chart draws, in one pass.

    One transform rather than one per layer, because the layers have to agree about their
    categories: two aggregates over the same rows can disagree about which categories
    exist at all once a filter has emptied one, and the line would then be drawn against
    the wrong bars.
    """
    measures = panel.measures_on(AXIS_LEFT) + panel.measures_on(AXIS_RIGHT)
    names = _measure_names(measures)

    operations = []
    for measure, name in zip(measures, names):
        if measure["aggregation"] == AGG_COUNT:
            # `count` counts rows and takes no field - naming one would instead count
            # that column's non-null values, a different and usually wrong answer.
            operations.append({"op": "count", "as": name})
        else:
            operations.append({"op": vega_aggregate(measure["aggregation"]),
                               "field": measure["column"], "as": name})

    return [{"aggregate": operations, "groupby": [panel.group_by]}]


def _measure_layer(panel: PanelSpec, names: list[str], mark: dict, colour_scale: dict,
                   date_columns: frozenset[str], *, axis_title: str) -> dict:
    """One axis' worth of measures, as a layer of its own.

    `fold` is the step that makes this possible: the aggregate leaves one row per category
    with a column per measure, and folding turns those columns back into rows - one per
    measure - which is what lets a single encoding colour them apart and sit them side by
    side. Without it, three measures would need three hand-written encodings.
    """
    category = {
        "field": panel.group_by,
        "type": _category_type(panel, date_columns),
        "title": panel.group_by,
    }
    value = {"field": MEASURE_VALUE_FIELD, "type": "quantitative", "title": axis_title}
    colour = {
        "field": MEASURE_NAME_FIELD,
        "type": "nominal",
        "title": "",
        # The identical scale in every layer, which is what makes the chart draw ONE legend
        # naming every measure on it. Vega-Lite shares a colour scale across a layered chart
        # and merges the legends only when the layers agree about it: describe the scale two
        # ways and two legends are drawn, silence one with `legend: null` and the merged
        # legend disappears altogether, taking the names of the bars with it.
        "scale": dict(colour_scale),
    }
    colour.update(_legend_option(panel))

    if panel.sub_type == CHART_BAR_HORIZONTAL:
        category["sort"] = _category_sort(panel, "x")
        encoding = {"y": _titled(category, panel), "x": _titled(value, panel),
                    "color": colour}
        offset = "yOffset"
    else:
        category["sort"] = _category_sort(panel, "y")
        category["axis"] = {"labelAngle": LABEL_ANGLE_EXPRESSION}
        encoding = {"x": _titled(category, panel), "y": _titled(value, panel),
                    "color": colour}
        offset = "xOffset"

    if mark["type"] == "bar" and len(names) > 1:
        # Side by side rather than stacked: these are the same quantity measured
        # differently, and stacking a minimum on top of a maximum adds up to nothing.
        encoding[offset] = {"field": MEASURE_NAME_FIELD, "type": "nominal",
                            "sort": list(names)}

    return {
        "transform": [{"fold": list(names),
                       "as": [MEASURE_NAME_FIELD, MEASURE_VALUE_FIELD]}],
        "mark": mark,
        "encoding": encoding,
    }


def _several_measure_layers(panel: PanelSpec, mark: str, palette: list[str],
                            date_columns: frozenset[str]) -> list[dict]:
    """The whole chart when it draws more than one number.

    Left-axis measures share one layer and one scale, so their heights can be compared -
    which is the only reason to draw the minimum, the average and the maximum together.
    Right-axis measures get a layer of their own, always a line, because a number on the
    right axis is there precisely because it is in different units from the bars.
    """
    left = panel.measures_on(AXIS_LEFT)
    right = panel.measures_on(AXIS_RIGHT)
    names = _measure_names(left + right)
    left_names, right_names = names[:len(left)], names[len(left):]

    # One scale over every measure on the chart, built once here rather than per layer.
    colour_scale = {
        "domain": list(names),
        "range": [palette[index % len(palette)] for index in range(len(names))],
    }

    # A combo chart's left side is bars by definition; any other shape keeps its own.
    left_mark = "bar" if panel.sub_type == CHART_COMBO else mark
    layers = [_measure_layer(
        panel, left_names,
        {"type": left_mark, "tooltip": True},
        colour_scale, date_columns,
        axis_title=left_names[0] if len(left_names) == 1 else "",
    )]

    if right_names:
        layers.append(_measure_layer(
            panel, right_names,
            {"type": "line", "tooltip": True, "point": True},
            colour_scale, date_columns,
            axis_title=right_names[0] if len(right_names) == 1 else "",
        ))

    return layers


def selection_field(panel: PanelSpec) -> str:
    """The column a click on this chart filters by, or "" when clicking it means nothing.

    One answer, asked by both the spec builder and the exporter. They used to decide it
    separately with slightly different rules, which held only because every non-histogram
    chart happens to have a breakdown - so the runtime could have been told to filter on a
    field the spec never offered.
    """
    if panel.sub_type in _UNCLICKABLE_KINDS:
        return ""
    return panel.group_by


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

    encoding = _encode_chart(panel, palette, date_columns)
    transforms = _chart_transforms(panel)

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
        inner = DONUT_INNER_RADIUS if panel.sub_type == CHART_DONUT else 0
        spec["mark"] = {"type": "arc", "tooltip": True, "innerRadius": inner}

    if panel.draws_several_measures():
        # A layered spec has no top-level mark or encoding of its own: each layer carries
        # its own. The measure axis stays independent only when something is actually
        # drawn against the right-hand side - splitting the scale for numbers that share a
        # unit would let a taller bar sit lower than a shorter one.
        spec.pop("mark")
        spec.pop("encoding")
        spec["layer"] = _several_measure_layers(panel, mark, palette, date_columns)
        transforms = transforms + _aggregate_every_measure(panel)
        if panel.measures_on(AXIS_RIGHT):
            measure_channel = "x" if panel.sub_type == CHART_BAR_HORIZONTAL else "y"
            spec["resolve"] = {"scale": {measure_channel: "independent"}}

    if transforms:
        spec["transform"] = transforms

    if not panel.colour_by and "mark" in spec and mark not in _SELF_COLOURING_MARKS:
        # One series, so there is nothing to vary - paint it the colour `colour:` named, or
        # the palette's first, rather than leaving Vega-Lite's default blue to disagree with
        # the rest of the app.
        spec["mark"]["color"] = _single_colour(panel, palette)

    if wants_labels(panel) and "mark" in spec:
        # Done last, so the mark being layered is the finished one - its colour, its hole and
        # its tooltip already decided. `encoding` stays at the top level, shared by both
        # layers, which is what keeps each number over the mark it belongs to.
        spec["layer"] = _label_layers(panel, spec)
        spec.pop("mark")

    # What makes the chart clickable. The runtime listens for this parameter and turns it
    # into the cross-filter; a chart with no category to pick has nothing to select.
    select_field = selection_field(panel)
    if select_field:
        selection = {
            "name": SELECTION_NAME,
            "select": {"type": "point", "fields": [select_field], "toggle": False},
        }
        if "layer" in spec:
            # Vega-Lite only takes a selection inside a unit spec, so a layered chart puts
            # it on the layer the reader actually clicks: the bars.
            spec["layer"][0]["params"] = [selection]
        else:
            spec["params"] = [selection]

    return spec
