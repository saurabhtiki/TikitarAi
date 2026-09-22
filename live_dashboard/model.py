"""What a dashboard is, and every pure operation on one (phase 32).

No Streamlit and no DuckDB here: `live_dashboard/session.py` holds these objects in session
state and `tasks/model.py` embeds their JSON in a Task, but neither concern reaches this
module - which is what makes the whole shape testable without `AppTest`. Deliberately shaped
like `report_items/model.py`, because it is the same kind of thing: a recipe that outlives
the session it was written in.

**A dashboard is an ordered list of panels**, and a panel is one of four things:

- A **filter** draws a widget and narrows every other panel.
- A **card** shows one number.
- A **chart** shows a picture of grouped, totalled rows.
- A **table** shows the rows themselves.

One dataclass rather than four, for the reason `report_items.model.ReportItem` gives: they
are positions in one sequence and every operation on the list treats them alike. `visual_type`
says which, and the fields a kind ignores stay at their defaults.

**Nothing here is drawn and nothing here is aggregated.** A panel says *what* to show; the
exported page's Vega-Lite spec says how, and the browser does the totalling. That separation
is what lets one spec drive both the Streamlit preview and the downloaded file.

The requirement's ninth spec-table column, "Depends on Filter", is deliberately **not** a
field. It is computed by `depends_on_filters` from the panels around it, so it cannot go
stale when a filter is added or deleted.
"""

import base64
import json
import logging
import uuid
from dataclasses import dataclass, field

from analyst.charts import (
    AGGREGATION_LABELS,
    AGG_COUNT,
    AGG_SUM,
    CHART_AREA,
    CHART_BAR,
    CHART_BAR_HORIZONTAL,
    CHART_COMBO,
    CHART_LABELS,
    CHART_LINE,
    CHART_PIE,
    CHART_SCATTER,
    DEFAULT_CHART_HEIGHT,
    PALETTE_DEFAULT,
    SORT_LARGEST,
)
from live_dashboard.exceptions import DashboardStorageError

logger = logging.getLogger(__name__)

# The four kinds of panel. See the module docstring.
VISUAL_FILTER = "filter"
VISUAL_CARD = "card"
VISUAL_CHART = "chart"
VISUAL_TABLE = "table"
VISUAL_TYPES = (VISUAL_FILTER, VISUAL_CARD, VISUAL_CHART, VISUAL_TABLE)

VISUAL_LABELS: dict[str, str] = {
    VISUAL_FILTER: "Filter",
    VISUAL_CARD: "Card",
    VISUAL_CHART: "Chart",
    VISUAL_TABLE: "Table",
}

# Filter sub-types: one widget per column type, as the requirement's Filters section asks.
FILTER_DROPDOWN = "dropdown"
FILTER_MULTISELECT = "multiselect"
FILTER_RANGE = "range"
FILTER_DATE_RANGE = "date_range"
FILTER_SUB_TYPES = (FILTER_DROPDOWN, FILTER_MULTISELECT, FILTER_RANGE, FILTER_DATE_RANGE)

FILTER_LABELS: dict[str, str] = {
    FILTER_DROPDOWN: "Dropdown (pick one)",
    FILTER_MULTISELECT: "Multiselect (pick several)",
    FILTER_RANGE: "Range slider (numbers)",
    FILTER_DATE_RANGE: "Date range",
}

# Chart shapes the dashboard can draw but the app's own charts have no name for. Declared
# here rather than pushed back into `analyst.charts`, because that module draws with Plotly
# for one reader on screen and these exist only in the exported Vega-Lite page.
CHART_BAR_STACKED = "bar_stacked"
CHART_BAR_GROUPED = "bar_grouped"
CHART_DONUT = "donut"
CHART_HISTOGRAM = "histogram"
CHART_HEATMAP = "heatmap"
CHART_BOXPLOT = "boxplot"

# Chart sub-types. The shared ones are imported from `analyst.charts` so the exported
# dashboard and the app's own charts never drift into two vocabularies; the rest are the
# constants above. Ordered as the picker should list them: bars together, then the
# time-shaped ones, then the distribution and comparison shapes.
CHART_SUB_TYPES = (
    CHART_BAR,
    CHART_BAR_HORIZONTAL,
    CHART_BAR_STACKED,
    CHART_BAR_GROUPED,
    CHART_LINE,
    CHART_AREA,
    CHART_COMBO,
    CHART_PIE,
    CHART_DONUT,
    CHART_SCATTER,
    CHART_HISTOGRAM,
    CHART_HEATMAP,
    CHART_BOXPLOT,
)

# What each shape is called on screen. The app's labels plus this module's own, in one dict
# so the picker, the spec table and the AI's catalog all read from the same place.
DASHBOARD_CHART_LABELS: dict[str, str] = {
    **CHART_LABELS,
    CHART_BAR_STACKED: "Bar — stacked",
    CHART_BAR_GROUPED: "Bar — grouped",
    CHART_DONUT: "Donut",
    CHART_HISTOGRAM: "Histogram (distribution)",
    CHART_HEATMAP: "Heatmap",
    CHART_BOXPLOT: "Box plot (spread)",
}

# Charts that need a second breakdown or measure on top of the usual one, so
# `panel_problems` can ask for it rather than letting a half-filled panel draw blank.
CHARTS_NEEDING_COLOUR = frozenset({CHART_BAR_STACKED, CHART_BAR_GROUPED, CHART_HEATMAP})
CHARTS_NEEDING_SECOND_MEASURE = frozenset({CHART_COMBO})

#: Which side of the chart an extra number is measured against.
#:
#: The right axis exists for one reason: two numbers in different units. Average salary
#: in rupees and headcount in people share a chart happily and an axis not at all - on one
#: scale the headcount is a flat line along the bottom. Numbers in the SAME unit (minimum,
#: average and maximum salary) belong on the left together, where their heights can be
#: compared, which is the whole point of drawing them side by side.
AXIS_LEFT = "left"
AXIS_RIGHT = "right"
MEASURE_AXES = (AXIS_LEFT, AXIS_RIGHT)

#: How many numbers one chart may draw, the first included. Four is already a busy chart;
#: past that the bars are too thin to compare, which is the only reason to draw them.
MAX_MEASURES_PER_CHART = 4

#: The shapes that can draw more than one number. A pie has one whole to divide, a
#: histogram one spread to show, a heatmap one number per cell, and a box plot's single
#: number is already five.
MULTI_MEASURE_CHARTS = (
    CHART_BAR, CHART_BAR_HORIZONTAL, CHART_LINE, CHART_AREA, CHART_COMBO,
)

# A histogram bins one column by itself: asking what to break it down by has no answer.
CHARTS_WITHOUT_GROUP_BY = frozenset({CHART_HISTOGRAM})

# Drilldown and Pivot are a later phase - they are new widgets in the exported page rather
# than new chart specs, so offering them now would be a control that refuses.
TABLE_FLAT = "flat"
TABLE_SUB_TYPES = (TABLE_FLAT,)

# The one sub-type a card can have. Named rather than left blank so `sub_types_for` can
# answer for every visual type without a special case.
CARD_SINGLE = "single"
CARD_SUB_TYPES = (CARD_SINGLE,)

# Ways of totalling a number that the app's own charts have no name for. `analyst.charts`'s
# five are shared with Chat with Data and report items and must keep meaning exactly what
# they mean there; the dashboard needs more, because the browser can compute more.
AGG_MEDIAN = "median"
AGG_DISTINCT = "distinct"
AGG_STDEV = "stdev"
AGG_Q1 = "q1"
AGG_Q3 = "q3"
AGG_FIRST = "first"
AGG_LAST = "last"
AGG_PERCENT_OF_TOTAL = "percent_of_total"
AGG_RUNNING_TOTAL = "running_total"

# Everything a dashboard visual may do with a number. Starts with the app's five, so the two
# vocabularies stay identical where they overlap and a panel built in Chat with Data reads
# back here unchanged.
DASHBOARD_AGGREGATIONS: dict[str, str] = {
    **AGGREGATION_LABELS,
    AGG_MEDIAN: "Median (middle value)",
    AGG_DISTINCT: "Count unique",
    AGG_STDEV: "Standard deviation",
    AGG_Q1: "Lower quarter (25%)",
    AGG_Q3: "Upper quarter (75%)",
    AGG_FIRST: "First value",
    AGG_LAST: "Last value",
    AGG_PERCENT_OF_TOTAL: "Percentage of total",
    AGG_RUNNING_TOTAL: "Running total",
}

# Counting rows and counting different values are meaningful for text and dates as well as
# numbers - "how many customers" is the usual question, and customers are text. Everything
# else here reads the cell as a number, on a card as well as in a chart, so asking for it
# over a name would quietly answer zero.
TEXT_FRIENDLY_AGGREGATIONS = frozenset({AGG_COUNT, AGG_DISTINCT})

# Totals Vega-Lite has no aggregate operation for, so the exported chart builds them out of
# transforms instead (see `vega_spec._transform_aggregation`).
#
# The last two are not aggregations at all: they are a total compared against, or added to,
# the totals beside them, and each only makes sense in some places - see `panel_problems`,
# which is where the refusals are worded. The first two are here for a duller reason:
# Vega-Lite's aggregate vocabulary genuinely has no first or last, only its `window`
# transform does.
TRANSFORM_AGGREGATIONS = frozenset(
    {AGG_FIRST, AGG_LAST, AGG_PERCENT_OF_TOTAL, AGG_RUNNING_TOTAL}
)

THEME_LIGHT = "light"
THEME_DARK = "dark"
THEMES = (THEME_LIGHT, THEME_DARK)

FILTER_TOP = "top"
FILTER_LEFT = "left"
FILTER_POSITIONS = (FILTER_TOP, FILTER_LEFT)

#: Where filters go when nobody has said otherwise (phase 37). Left rather than top: a top
#: strip pushes the first row of visuals below the fold as soon as there are three filters,
#: and a reader looks for the controls down the side the way every report tool trains them
#: to. A dashboard saved before this carries its own choice and keeps it.
DEFAULT_FILTER_POSITION = FILTER_LEFT

# How a card's number is written. A whitelist rather than a format string - see
# `clean_properties` for why nothing the user types becomes code.
FORMAT_PLAIN = "plain"
FORMAT_THOUSANDS = "thousands"
FORMAT_CURRENCY = "currency"
FORMAT_PERCENT = "percent"
NUMBER_FORMATS = (FORMAT_PLAIN, FORMAT_THOUSANDS, FORMAT_CURRENCY, FORMAT_PERCENT)

# Which currency `format:currency` writes. A short printable whitelist for the same reason
# every other property is one: the code goes straight into the reader's browser, and a list
# of nine ISO codes cannot carry anything but a currency. The reader's own locale decides the
# grouping, so `INR` gives the lakh-crore grouping to someone in India.
CURRENCY_CODES = ("INR", "USD", "GBP", "EUR", "AED", "JPY", "AUD", "CAD", "SGD")
DEFAULT_CURRENCY = "INR"

UNTITLED_PANEL = "Untitled visual"
UNTITLED_DASHBOARD = "Untitled dashboard"

# A panel taller than this is almost certainly a typo, and a page of them is unreadable.
MIN_PANEL_HEIGHT = 120
MAX_PANEL_HEIGHT = 1200

MAX_CORNER_RADIUS = 2.0

# ---------------------------------------------------------------------- look (phase 38)
#
# Seven settings the user can ask for in words ("show data labels", "make it tall", "put the
# legend at the bottom"). Every one is a named choice from a short list, for the reason the
# whole of `clean_properties` exists: these values are written into a page a reader opens, so
# a free-form value would be a way into the exported file. A word outside the list is dropped
# with a log line, and `panel_problems` is what tells the user it went.

# Numbers printed on the bars, points or slices. On a pie or a donut it is the share of the
# whole rather than the raw number, which is the question a pie is asked.
LABELS_ON = "yes"
LABELS_OFF = "no"
YES_NO = (LABELS_ON, LABELS_OFF)

#: Where the legend goes, and "none" to take it away.
LEGEND_RIGHT = "right"
LEGEND_BOTTOM = "bottom"
LEGEND_TOP = "top"
LEGEND_NONE = "none"
LEGEND_POSITIONS = (LEGEND_RIGHT, LEGEND_BOTTOM, LEGEND_TOP, LEGEND_NONE)

#: One colour for a chart with one series. Named rather than a hex code the user types: a
#: name is a value we choose, a hex code is a value they do, and only one of those can be
#: checked. A chart broken down by a colour column ignores this - the palette is carrying
#: the breakdown there, and overpainting it would erase the thing the legend explains.
NAMED_COLOURS: dict[str, str] = {
    "blue": "#2563eb",
    "green": "#16a34a",
    "red": "#dc2626",
    "orange": "#ea580c",
    "purple": "#7c3aed",
    "grey": "#64748b",
}

#: How tall a chart is drawn, as three words rather than a pixel count. `height:N` still
#: works and still wins, so a dashboard saved before this opens exactly as it did.
SIZE_SHORT = "short"
SIZE_MEDIUM = "medium"
SIZE_TALL = "tall"
PANEL_SIZES: dict[str, int] = {SIZE_SHORT: 220, SIZE_MEDIUM: 320, SIZE_TALL: 480}

#: How wide a panel sits in its row. "full" is a row to itself even where another visual
#: shares the row number; "half" is the ordinary side-by-side. Applied as a CSS class on the
#: panel rather than an inline width, so the stylesheet keeps deciding the actual number.
WIDTH_HALF = "half"
WIDTH_FULL = "full"
PANEL_WIDTHS = (WIDTH_HALF, WIDTH_FULL)

#: How big a card's number is printed. Cards ignored size entirely before phase 38.
CARD_SMALL = "small"
CARD_MEDIUM = "medium"
CARD_LARGE = "large"
CARD_SIZES = (CARD_SMALL, CARD_MEDIUM, CARD_LARGE)

#: The chart shapes `labels:yes` can print a number on. Declared here rather than in
#: `vega_spec` because it is a fact about the vocabulary - the AI catalog and the "what can I
#: ask" help both read it, and neither should have to import the drawing code to find out.
LABELLABLE_CHARTS = (
    CHART_BAR, CHART_BAR_HORIZONTAL, CHART_BAR_STACKED, CHART_BAR_GROUPED,
    CHART_LINE, CHART_AREA, CHART_PIE, CHART_DONUT,
)

# The JSON `to_json` writes carries its own version, so a dashboard saved today can be read
# back after the shape changes rather than failing to load with no explanation.
SCHEMA_VERSION = 1


def new_id() -> str:
    """A short stable id. Used in widget keys, so it must survive reruns unchanged.

    Its own copy rather than an import of `report_items.model.new_id`, on the grounds that
    file already gives: a recipe package should not depend on a sibling for a uuid.
    """
    return uuid.uuid4().hex[:12]


def sub_types_for(visual_type: str) -> tuple[str, ...]:
    """The sub-types a visual type allows - what makes column 3 depend on column 2."""
    return {
        VISUAL_FILTER: FILTER_SUB_TYPES,
        VISUAL_CARD: CARD_SUB_TYPES,
        VISUAL_CHART: CHART_SUB_TYPES,
        VISUAL_TABLE: TABLE_SUB_TYPES,
    }.get(visual_type, CHART_SUB_TYPES)


def default_sub_type(visual_type: str) -> str:
    return sub_types_for(visual_type)[0]


@dataclass
class PanelSpec:
    """One row of the requirement's spec table - one visual, or one filter.

    Attributes:
        panel_id: stable across reruns; forms this panel's widget keys and its element id in
            the exported page, which is why it is generated once and never derived from the
            title.
        visual_type / sub_type: columns 2 and 3. `sub_type` is only meaningful against its
            own `visual_type`; `sub_types_for` is the pairing.
        source_table: the table this panel reads. Every table is flattened since phase 39,
            so this is whichever of them carries the columns below - including a parent's
            columns, which each child carries under a `Parent - Column` name.
        source_columns: column 4, the columns the user picked. Kept alongside the specific
            roles below rather than replaced by them, because a table panel has no roles at
            all - its columns *are* what it shows.
        measure_column / aggregation: column 5's "what to show" - the number, and what to do
            with it. A count needs no measure column, which is why the two are separate
            fields rather than one sentence to be parsed.
        extra_measures: the other numbers this chart draws beside `measure_column`, each
            with its own aggregation and its own side of the chart -
            `{"column", "aggregation", "axis"}`, already whitelisted by
            `clean_measures`. This was a single `measure_column_2` until phase 38, which
            could only ever draw a combo chart's line; a list is what lets one chart show
            the minimum, the average and the maximum of the same column. A dashboard
            saved with the old field is migrated on the way in by `_panel_from_dict`.
        group_by: what the measure is broken down by - the category axis, or the slices.
        colour_by: the optional second breakdown that becomes a legend.
        sort / top_n: how categories are ordered and how many survive. `top_n` of 0 means
            "no cap", so an untouched panel shows everything it found.
        title: column 6.
        properties: column 7, already parsed and whitelisted by `clean_properties`. A dict
            rather than the raw string, so nothing the user typed can reach the page as CSS.
        row_number: column 8. Panels sharing a number sit side by side; filters ignore it,
            since `DashboardSpec.filter_position` decides where they go.
    """

    panel_id: str = field(default_factory=new_id)
    visual_type: str = VISUAL_CHART
    sub_type: str = CHART_BAR
    source_table: str = ""
    source_columns: list[str] = field(default_factory=list)
    measure_column: str = ""
    extra_measures: list[dict] = field(default_factory=list)
    aggregation: str = AGG_SUM
    group_by: str = ""
    colour_by: str = ""
    sort: str = SORT_LARGEST
    top_n: int = 0
    title: str = ""
    properties: dict = field(default_factory=dict)
    row_number: int = 1

    def display_title(self) -> str:
        """What to label this panel. Never empty."""
        return (self.title or "").strip() or UNTITLED_PANEL

    def is_filter(self) -> bool:
        return self.visual_type == VISUAL_FILTER

    def filter_column(self) -> str:
        """The column a filter narrows on.

        A filter has no measure and no group-by - its one column is whichever the user
        picked, so `source_columns` is read here rather than duplicated into a field that
        could disagree with column 4.
        """
        if not self.is_filter():
            return ""
        return self.source_columns[0] if self.source_columns else ""

    def measures_on(self, axis: str) -> list[dict]:
        """Every number drawn against one side of the chart, the panel's own included.

        One answer, asked by the spec builder, the exporter and the wording of the round,
        because deciding it three times is how a legend ends up naming a line that was
        drawn against the other axis.
        """
        mine: list[dict] = []
        if axis == AXIS_LEFT:
            # The panel's own measure is always the left axis. There is nothing to be
            # "second" to otherwise.
            mine.append({"column": self.measure_column, "aggregation": self.aggregation,
                         "axis": AXIS_LEFT})
        return mine + [dict(one) for one in self.extra_measures
                       if one.get("axis") == axis]

    def draws_several_measures(self) -> bool:
        """Whether this chart draws more than the one number every chart draws."""
        return bool(self.extra_measures)

    def height(self) -> int:
        """The panel's drawn height, already clamped by `clean_properties`.

        An exact `height:N` wins over `size:tall`. The two mean the same thing and a
        dashboard saved before phase 38 carries the number, so the number is the one that
        must keep working - `size` is the word the AI is taught to use from now on.
        """
        if "height" in self.properties:
            return int(self.properties["height"])
        return PANEL_SIZES.get(str(self.properties.get("size", "")), DEFAULT_CHART_HEIGHT)


@dataclass
class DashboardSpec:
    """A whole dashboard: its look, its data source, and its ordered panels.

    Attributes:
        main_table: **kept only so a dashboard saved before phase 39 still opens.** It is
            never read: every table is embedded now, each carrying the columns of the
            parents it can reach, so there is no one table a dashboard is built from and
            each visual names its own `source_table`.
        theme: the mode the exported page *opens* in. The reader can still toggle.
        filter_position: top strip or left rail. Left by default - see
            `DEFAULT_FILTER_POSITION`.
        logo_bytes / logo_mime: the picture for the header, validated on the way in. Bytes
            rather than a path, because the exported file has to carry it.
    """

    title: str = ""
    subtitle: str = ""
    logo_bytes: bytes | None = None
    logo_mime: str = ""
    theme: str = THEME_LIGHT
    filter_position: str = DEFAULT_FILTER_POSITION
    main_table: str = ""
    palette: str = PALETTE_DEFAULT
    panels: list[PanelSpec] = field(default_factory=list)

    def display_title(self) -> str:
        return (self.title or "").strip() or UNTITLED_DASHBOARD

    def filters(self) -> list[PanelSpec]:
        return [panel for panel in self.panels if panel.is_filter()]

    def visuals(self) -> list[PanelSpec]:
        """Everything that is not a filter - the panels laid out in rows."""
        return [panel for panel in self.panels if not panel.is_filter()]

    def logo_data_uri(self) -> str:
        """The logo as a `data:` URI, or empty when there is none.

        Empty rather than None so the template can fall back without a conditional, the same
        shape `dashboard.model.Report.logo_data_uri` uses.
        """
        if not self.logo_bytes or not self.logo_mime:
            return ""
        encoded = base64.b64encode(self.logo_bytes).decode("ascii")
        return f"data:{self.logo_mime};base64,{encoded}"


# --------------------------------------------------------------------------------------
# Properties (column 7)
# --------------------------------------------------------------------------------------


def clean_properties(raw: str | dict | None) -> dict:
    """Parses the free-text Properties cell into a whitelist of settings.

    The requirement lets the user type things like `border:yes, radius:0.5, format:currency`.
    That text is never written into the page - only the handful of values recognised here
    survive, each coerced to its own type and clamped, and anything else is dropped with a
    log line. It is the reason the exported dashboard has no CSS-injection surface at all:
    there is no path from this cell to a stylesheet.

    Accepts a dict unchanged (re-cleaned), so a value read back from JSON goes through the
    same gate as one the user just typed.
    """
    if isinstance(raw, dict):
        pairs = [(str(key), str(value)) for key, value in raw.items()]
    else:
        pairs = []
        for chunk in str(raw or "").split(","):
            if ":" not in chunk:
                continue
            key, _, value = chunk.partition(":")
            pairs.append((key, value))

    cleaned: dict = {}
    for key, value in pairs:
        name = key.strip().lower()
        text = value.strip()
        if name == "border":
            cleaned["border"] = text.lower() in {"yes", "true", "on", "1"}
        elif name == "radius":
            try:
                cleaned["radius"] = max(0.0, min(float(text), MAX_CORNER_RADIUS))
            except ValueError:
                logger.info("Ignoring an unreadable radius %r in a panel's properties.", text)
        elif name in {"format", "number_format"}:
            if text.lower() in NUMBER_FORMATS:
                cleaned["number_format"] = text.lower()
            else:
                logger.info("Ignoring an unknown number format %r in a panel's properties.", text)
        elif name == "currency":
            code = text.upper()
            if code in CURRENCY_CODES:
                cleaned["currency"] = code
            else:
                logger.info("Ignoring an unknown currency %r in a panel's properties.", text)
        elif name == "height":
            try:
                cleaned["height"] = max(MIN_PANEL_HEIGHT, min(int(float(text)), MAX_PANEL_HEIGHT))
            except ValueError:
                logger.info("Ignoring an unreadable height %r in a panel's properties.", text)
        elif name in {"labels", "data_labels", "axis_titles"}:
            key_name = "labels" if name in {"labels", "data_labels"} else "axis_titles"
            word = text.lower()
            if word in {"yes", "true", "on", "1"}:
                cleaned[key_name] = True
            elif word in {"no", "false", "off", "0"}:
                cleaned[key_name] = False
            else:
                logger.info("Ignoring an unreadable %s %r in a panel's properties.", key_name, text)
        elif name == "legend":
            if text.lower() in LEGEND_POSITIONS:
                cleaned["legend"] = text.lower()
            else:
                logger.info("Ignoring an unknown legend position %r.", text)
        elif name in {"colour", "color"}:
            if text.lower() in NAMED_COLOURS:
                cleaned["colour"] = text.lower()
            else:
                logger.info("Ignoring an unknown colour %r in a panel's properties.", text)
        elif name == "size":
            if text.lower() in PANEL_SIZES:
                cleaned["size"] = text.lower()
            else:
                logger.info("Ignoring an unknown size %r in a panel's properties.", text)
        elif name == "width":
            if text.lower() in PANEL_WIDTHS:
                cleaned["width"] = text.lower()
            else:
                logger.info("Ignoring an unknown width %r in a panel's properties.", text)
        elif name == "card_size":
            if text.lower() in CARD_SIZES:
                cleaned["card_size"] = text.lower()
            else:
                logger.info("Ignoring an unknown card size %r in a panel's properties.", text)
        else:
            logger.info("Ignoring an unknown panel property %r.", name)
    return cleaned


def _extra_measures_from_dict(raw: dict) -> list[dict]:
    """The extra measures a saved panel carries, including one saved before phase 38.

    A dashboard saved earlier has `measure_column_2` and no list. It becomes one extra
    measure on the right axis with the panel's own aggregation, which is exactly what the
    combo chart drew at the time - so the file opens showing what it always showed.
    """
    measures = clean_measures(raw.get("extra_measures"))
    if measures:
        return measures

    older = str(raw.get("measure_column_2") or "").strip()
    if not older:
        return []
    return clean_measures([{
        "column": older,
        "aggregation": str(raw.get("aggregation") or AGG_SUM),
        "axis": AXIS_RIGHT,
    }])


def clean_measures(raw) -> list[dict]:
    """The extra measures, whitelisted the way `clean_properties` whitelists settings.

    Each entry keeps only three things, and each of the three is checked: a column name
    (checked against the real data later, by `panel_problems`), an aggregation that is in
    the catalog, and a side of the chart that is one of two words. Anything else is
    dropped with a log line.

    The four transform aggregations are refused here rather than drawn wrongly. Each one
    is a total computed *against the other totals on the chart* - a percentage of what is
    on screen, a total running along it - and a second such column on the same chart is
    two answers to a question with one.
    """
    if not isinstance(raw, (list, tuple)):
        return []

    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            logger.info("Ignoring an extra measure that isn't a set of fields: %r.", entry)
            continue

        column = str(entry.get("column") or "").strip()
        aggregation = str(entry.get("aggregation") or "").strip().lower()
        axis = str(entry.get("axis") or AXIS_LEFT).strip().lower()

        if aggregation not in DASHBOARD_AGGREGATIONS:
            logger.info("Ignoring an extra measure with an unknown total %r.", aggregation)
            continue
        if aggregation in TRANSFORM_AGGREGATIONS:
            logger.info("Ignoring an extra measure totalled as %r - it is measured "
                        "against the chart it is on.", aggregation)
            continue
        if not column and aggregation != AGG_COUNT:
            logger.info("Ignoring an extra measure with no column to total.")
            continue
        if axis not in MEASURE_AXES:
            logger.info("Ignoring an extra measure on an unknown axis %r.", axis)
            continue

        cleaned.append({"column": column, "aggregation": aggregation, "axis": axis})

    # The panel's own measure is the first of the four, so the extras stop one short.
    if len(cleaned) > MAX_MEASURES_PER_CHART - 1:
        logger.info("Keeping only the first %d extra measures of %d.",
                    MAX_MEASURES_PER_CHART - 1, len(cleaned))
        cleaned = cleaned[:MAX_MEASURES_PER_CHART - 1]
    return cleaned


def measures_text(measures: list[dict]) -> str:
    """The extra measures as the one line the AI writes them on, for editing and for the
    listing every round is shown. `"Salary:minimum:left; Headcount:count:right"`."""
    return "; ".join(
        f"{one.get('column', '')}:{one.get('aggregation', '')}:{one.get('axis', '')}"
        for one in measures
    )


def property_problems(panel: "PanelSpec") -> str:
    """A sentence about a setting this visual understood but cannot use - or "".

    Separate from `panel_problems` on purpose. That one decides whether a visual can be
    *drawn*, and refusing to draw a perfectly good heatmap because labels were asked for
    would be a bad trade. This one is a note beside a visual that was drawn anyway: the user
    asked for something and did not get it, and saying so is the difference between a
    setting that quietly does nothing and one that has a reason.
    """
    if panel.visual_type != VISUAL_CHART:
        if panel.properties.get("labels"):
            return "Only charts can print their numbers on themselves."
        return ""

    if panel.properties.get("labels") and panel.extra_measures:
        return ("This chart draws several numbers, so data labels were left off - there is "
                "no room to write one over each.")

    if panel.properties.get("labels") and panel.sub_type not in LABELLABLE_CHARTS:
        shape = DASHBOARD_CHART_LABELS.get(panel.sub_type, panel.sub_type)
        return (f"A {shape} has nowhere to print its numbers, so data labels were left off. "
                "A bar or a pie can show them.")

    if panel.properties.get("colour") and panel.colour_by:
        return ("This chart is already split into colours by "
                f"'{panel.colour_by}', so one colour was not applied.")

    return ""


def properties_text(properties: dict) -> str:
    """The whitelist back as the text the user typed, for editing.

    Round-trips `clean_properties`, so opening a saved panel in the edit dialog shows the
    settings that actually survived rather than the string that was typed at them.
    """
    parts = []
    if "border" in properties:
        parts.append(f"border:{'yes' if properties['border'] else 'no'}")
    if "radius" in properties:
        parts.append(f"radius:{properties['radius']:g}")
    if "number_format" in properties:
        parts.append(f"format:{properties['number_format']}")
    if "currency" in properties:
        parts.append(f"currency:{properties['currency']}")
    if "height" in properties:
        parts.append(f"height:{properties['height']}")
    if "labels" in properties:
        parts.append(f"labels:{'yes' if properties['labels'] else 'no'}")
    if "axis_titles" in properties:
        parts.append(f"axis_titles:{'yes' if properties['axis_titles'] else 'no'}")
    for name in ("legend", "colour", "size", "width", "card_size"):
        if name in properties:
            parts.append(f"{name}:{properties[name]}")
    return ", ".join(parts)


# --------------------------------------------------------------------------------------
# Operations on the list
# --------------------------------------------------------------------------------------


def add_panel(spec: DashboardSpec, visual_type: str = VISUAL_CHART, title: str = "") -> PanelSpec:
    """Appends a panel of the given kind and returns it.

    A new visual lands on its own row rather than joining the last one: side-by-side is a
    deliberate choice the user makes by editing the row number, not something that happens
    to them as they add panels.
    """
    if visual_type not in VISUAL_TYPES:
        logger.warning("Unknown visual type '%s' - treating it as a chart.", visual_type)
        visual_type = VISUAL_CHART

    row_number = 1 + max((panel.row_number for panel in spec.visuals()), default=0)
    panel = PanelSpec(
        visual_type=visual_type,
        sub_type=default_sub_type(visual_type),
        title=(title or "").strip(),
        row_number=row_number,
    )
    spec.panels.append(panel)
    return panel


def find_panel(spec: DashboardSpec, panel_id: str) -> PanelSpec | None:
    return next((panel for panel in spec.panels if panel.panel_id == panel_id), None)


def remove_panel(spec: DashboardSpec, panel_id: str) -> bool:
    """Drops a panel. Returns whether it went.

    Unlike `report_items.model.can_remove`, any panel may go at any time: panels do not
    change the data the ones below them read, so nothing downstream can break.
    """
    panel = find_panel(spec, panel_id)
    if panel is None:
        logger.info("Refused to remove panel %s: it is no longer in the list.", panel_id)
        return False
    spec.panels.remove(panel)
    return True


def move_panel(spec: DashboardSpec, panel_id: str, offset: int) -> bool:
    """Shifts a panel up or down the list. Returns whether it moved.

    Order within a row follows list order, which is what the requirement's column 8 says - so
    moving a panel is how the user reorders visuals sitting side by side.
    """
    panel = find_panel(spec, panel_id)
    if panel is None:
        return False

    position = spec.panels.index(panel)
    target = position + offset
    if not 0 <= target < len(spec.panels):
        return False

    spec.panels.pop(position)
    spec.panels.insert(target, panel)
    return True


def group_into_rows(panels: list[PanelSpec]) -> list[list[PanelSpec]]:
    """Visual panels grouped by `row_number`, in list order within each row.

    Filters are excluded - they live in their own strip or rail, not in the grid. Rows come
    back sorted by number so the spec table reads like the page it describes, which is the
    requirement's "group rows visually by Row/Position".
    """
    rows: dict[int, list[PanelSpec]] = {}
    for panel in panels:
        if panel.is_filter():
            continue
        rows.setdefault(panel.row_number, []).append(panel)
    return [rows[number] for number in sorted(rows)]


def depends_on_filters(spec: DashboardSpec, panel: PanelSpec) -> list[PanelSpec]:
    """Which filters narrow this panel - the requirement's read-only ninth column.

    Derived rather than stored, so it cannot disagree with the list it describes.

    A filter narrows a panel when they read the same table. That is the whole rule, and it is
    correct *because* of how the data is assembled: `flatten` pre-joins each master's
    attributes onto the fact rows, so a filter on `Customer - Name` and a chart of
    `Transactions.Amount` are both columns of one flattened table. A panel over a separate
    side table (Calendar, Budget) is only narrowed by filters over that same table.

    A filter never narrows itself, and - following the requirement - never narrows another
    filter: a filter's choices stay the full list, so the reader cannot paint themselves into
    a corner where no options remain.
    """
    if panel.is_filter():
        return []
    return [
        other
        for other in spec.filters()
        if other.panel_id != panel.panel_id
        and other.filter_column()
        and other.source_table == panel.source_table
    ]


def panel_problems(
    panel: PanelSpec,
    available_columns: dict[str, list[str]],
    date_columns: frozenset[str] | None = None,
) -> str:
    """Why this panel can't be drawn, or an empty string when it can.

    `available_columns` maps each embedded table name to the columns it actually carries,
    its parents' columns included. A column that is not there is the
    requirement's warning case: the panel names a combination the confirmed relationships do
    not reach, and the honest answer is to say so rather than to guess a join.

    `date_columns` is which of those columns hold dates, from `payload.date_columns`. Only a
    running total needs it - it has to run *along* something - and `None` means "not known
    here", which lets the check be skipped rather than guessed at.

    Returns a sentence the user can act on, because that is the whole value of the warning.
    """
    if not panel.source_table:
        return "Pick a data source for this visual."

    columns = available_columns.get(panel.source_table)
    if columns is None:
        return (
            f"'{panel.source_table}' isn't one of the tables loaded. Load it in Setup, or "
            "ask for this visual over a table you do have."
        )

    def missing(column: str) -> bool:
        return bool(column) and column not in columns

    if panel.is_filter():
        if not panel.filter_column():
            return "Pick the column this filter should narrow on."
        if missing(panel.filter_column()):
            return _unreachable(panel.filter_column(), panel.source_table)
        return ""

    if panel.visual_type == VISUAL_TABLE:
        if not panel.source_columns:
            return "Pick at least one column for this table to show."
        for column in panel.source_columns:
            if missing(column):
                return _unreachable(column, panel.source_table)
        return ""

    # Cards and charts both compute a number; a count is the one that needs no column.
    if panel.aggregation != AGG_COUNT and not panel.measure_column:
        return "Pick the number this visual should total, or switch it to a count."
    if missing(panel.measure_column):
        return _unreachable(panel.measure_column, panel.source_table)

    if panel.visual_type == VISUAL_CARD and panel.aggregation == AGG_PERCENT_OF_TOTAL:
        # There is nothing on a card for the percentage to be *of*. On a chart the other
        # bars are the total; alone, the honest answer is always 100%.
        return (
            "A percentage of total needs something to compare against. Use it on a chart "
            "that is broken down, or switch this card to a sum."
        )

    if panel.visual_type != VISUAL_CHART and panel.aggregation == AGG_RUNNING_TOTAL:
        # Beside its sibling above rather than as an `elif` forty lines below, so the two
        # "wrong place for this total" refusals read as the pair they are.
        return "A running total needs a date to run along, so it only works on a chart."

    if panel.visual_type == VISUAL_CHART:
        if panel.sub_type == CHART_HISTOGRAM and not panel.measure_column:
            # A count with no column is a legal card but an empty histogram: the bins are
            # made *from* a number, so there is nothing to bin without one.
            return "Pick the number whose spread this histogram should show."

        needs_group_by = panel.sub_type not in CHARTS_WITHOUT_GROUP_BY
        if needs_group_by and not panel.group_by:
            return "Pick what to break this chart down by."
        if missing(panel.group_by):
            return _unreachable(panel.group_by, panel.source_table)
        if missing(panel.colour_by):
            return _unreachable(panel.colour_by, panel.source_table)

        if panel.sub_type in CHARTS_NEEDING_COLOUR and not panel.colour_by:
            return (
                f"A {DASHBOARD_CHART_LABELS.get(panel.sub_type, panel.sub_type).lower()} "
                "needs a second breakdown. Pick a column to colour it by."
            )

        if (panel.sub_type in CHARTS_NEEDING_SECOND_MEASURE
                and not panel.measures_on(AXIS_RIGHT)):
            return ("A combo chart draws a line over its bars. Add a second number on "
                    "the right axis.")

        if panel.extra_measures:
            if panel.sub_type not in MULTI_MEASURE_CHARTS:
                shape = DASHBOARD_CHART_LABELS.get(panel.sub_type, panel.sub_type).lower()
                return (f"A {shape} draws one number, so it can't show several at once. "
                        "A bar or a line chart can.")
            if panel.aggregation in TRANSFORM_AGGREGATIONS:
                total = DASHBOARD_AGGREGATIONS.get(panel.aggregation, panel.aggregation)
                return (f"'{total}' is measured against the rest of this chart, so no "
                        "other number can share it.")
            for one in panel.extra_measures:
                column = str(one.get("column") or "")
                if one.get("aggregation") != AGG_COUNT and missing(column):
                    return _unreachable(column, panel.source_table)

        # A running total adds each category to the ones before it, so the order has to mean
        # something. Over unordered labels the climbing line is an accident of sorting.
        if (panel.aggregation == AGG_RUNNING_TOTAL and date_columns is not None
                and panel.group_by not in date_columns):
            return (
                "A running total needs a date to run along. Break this chart down by a "
                "date column, or switch it to a sum."
            )

    return ""


def _unreachable(column: str, table: str) -> str:
    return (
        f"'{column}' isn't reachable from {table}. Confirm the link it needs in Setup, under "
        "Relationships, then pick it again."
    )


# --------------------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------------------


def _panel_to_dict(panel: PanelSpec) -> dict:
    return {
        "panel_id": panel.panel_id,
        "visual_type": panel.visual_type,
        "sub_type": panel.sub_type,
        "source_table": panel.source_table,
        "source_columns": list(panel.source_columns),
        "measure_column": panel.measure_column,
        "extra_measures": [dict(one) for one in panel.extra_measures],
        "aggregation": panel.aggregation,
        "group_by": panel.group_by,
        "colour_by": panel.colour_by,
        "sort": panel.sort,
        "top_n": panel.top_n,
        "title": panel.title,
        "properties": dict(panel.properties),
        "row_number": panel.row_number,
    }


def _panel_from_dict(raw: dict) -> PanelSpec:
    """The reverse.

    An unknown visual type or sub-type is **repaired with a warning rather than dropped**:
    losing a panel silently is worse than mis-typing one, the same call
    `report_items.model._item_from_dict` makes. The properties go back through
    `clean_properties`, so a payload hand-edited to carry something else gains nothing.
    """
    visual_type = str(raw.get("visual_type") or VISUAL_CHART)
    if visual_type not in VISUAL_TYPES:
        logger.warning(
            "Panel %s has unknown visual type '%s' - reading it as a chart.",
            raw.get("panel_id"),
            visual_type,
        )
        visual_type = VISUAL_CHART

    sub_type = str(raw.get("sub_type") or "")
    if sub_type not in sub_types_for(visual_type):
        logger.warning(
            "Panel %s has unknown sub-type '%s' for a %s - using '%s'.",
            raw.get("panel_id"),
            sub_type,
            visual_type,
            default_sub_type(visual_type),
        )
        sub_type = default_sub_type(visual_type)

    try:
        top_n = max(0, int(raw.get("top_n") or 0))
    except (TypeError, ValueError):
        logger.info("Panel %s had an unreadable top_n - showing everything.", raw.get("panel_id"))
        top_n = 0

    try:
        row_number = max(1, int(raw.get("row_number") or 1))
    except (TypeError, ValueError):
        logger.info("Panel %s had an unreadable row number - putting it on row 1.", raw.get("panel_id"))
        row_number = 1

    return PanelSpec(
        panel_id=str(raw.get("panel_id") or new_id()),
        visual_type=visual_type,
        sub_type=sub_type,
        source_table=str(raw.get("source_table") or ""),
        source_columns=[str(name) for name in raw.get("source_columns") or []],
        measure_column=str(raw.get("measure_column") or ""),
        extra_measures=_extra_measures_from_dict(raw),
        aggregation=str(raw.get("aggregation") or AGG_SUM),
        group_by=str(raw.get("group_by") or ""),
        colour_by=str(raw.get("colour_by") or ""),
        sort=str(raw.get("sort") or SORT_LARGEST),
        top_n=top_n,
        title=str(raw.get("title") or ""),
        properties=clean_properties(raw.get("properties")),
        row_number=row_number,
    )


def to_json(spec: DashboardSpec) -> str:
    """Serialises a dashboard for storage.

    Shaped as a recipe rather than a snapshot: the panels and the look are written, the
    *rows* never are. Storing the data would describe a table that is no longer loaded - the
    same promise `report_items.model.to_json` makes.

    The logo is the one binary value, base64 encoded so the payload stays plain JSON and can
    nest inside a Task unchanged.
    """
    payload = {
        "version": SCHEMA_VERSION,
        "dashboard": {
            "title": spec.title,
            "subtitle": spec.subtitle,
            "logo": base64.b64encode(spec.logo_bytes).decode("ascii") if spec.logo_bytes else "",
            "logo_mime": spec.logo_mime,
            "theme": spec.theme if spec.theme in THEMES else THEME_LIGHT,
            "filter_position": (
                spec.filter_position if spec.filter_position in FILTER_POSITIONS
                else DEFAULT_FILTER_POSITION
            ),
            "main_table": spec.main_table,
            "palette": spec.palette,
        },
        "panels": [_panel_to_dict(panel) for panel in spec.panels],
    }
    return json.dumps(payload, indent=2)


def from_json(text: str) -> DashboardSpec:
    """Rebuilds a dashboard from stored JSON.

    Raises:
        DashboardStorageError: if the text isn't the JSON object this module writes. The
            dashboard in front of the user is never replaced by a partial load - the caller
            only swaps it in once this returns.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("A stored dashboard could not be parsed.")
        raise DashboardStorageError(
            "This saved dashboard couldn't be read - its stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning("A stored dashboard was %s, not an object.", type(payload).__name__)
        raise DashboardStorageError(
            "This saved dashboard couldn't be read - it isn't in the expected format."
        )

    version = payload.get("version")
    if version is not None and version > SCHEMA_VERSION:
        logger.warning("A dashboard was saved by a newer version (%s).", version)
        raise DashboardStorageError(
            "This dashboard was saved by a newer version of the app and can't be opened here."
        )

    settings = payload.get("dashboard")
    if not isinstance(settings, dict):
        settings = {}

    raw_panels = payload.get("panels")
    if not isinstance(raw_panels, list):
        raw_panels = []

    try:
        logo_bytes = base64.b64decode(settings.get("logo") or "") or None
    except (TypeError, ValueError):
        # A corrupt logo costs the picture, not the dashboard.
        logger.exception("A stored dashboard's logo couldn't be decoded - dropping it.")
        logo_bytes = None

    theme = str(settings.get("theme") or THEME_LIGHT)
    position = str(settings.get("filter_position") or DEFAULT_FILTER_POSITION)

    return DashboardSpec(
        title=str(settings.get("title") or ""),
        subtitle=str(settings.get("subtitle") or ""),
        logo_bytes=logo_bytes,
        logo_mime=str(settings.get("logo_mime") or ""),
        theme=theme if theme in THEMES else THEME_LIGHT,
        filter_position=position if position in FILTER_POSITIONS else DEFAULT_FILTER_POSITION,
        main_table=str(settings.get("main_table") or ""),
        palette=str(settings.get("palette") or PALETTE_DEFAULT),
        # Anything saved between phases 33 and 35 carries an "ai_guidance" setting. It is
        # read past rather than restored: phase 36 took the box away, and silently applying
        # a preference with nowhere on the page to see or change it is worse than losing it.
        panels=[_panel_from_dict(raw) for raw in raw_panels if isinstance(raw, dict)],
    )
