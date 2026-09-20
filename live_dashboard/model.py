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
    AGG_COUNT,
    AGG_SUM,
    CHART_AREA,
    CHART_BAR,
    CHART_BAR_HORIZONTAL,
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

# Chart sub-types. Phase 32's list; imported from `analyst.charts` so the exported dashboard
# and the app's own charts never drift into two vocabularies.
CHART_SUB_TYPES = (
    CHART_BAR,
    CHART_BAR_HORIZONTAL,
    CHART_LINE,
    CHART_AREA,
    CHART_PIE,
    CHART_SCATTER,
)

# Drilldown and Pivot are phase 33 - offering them now would be a control that refuses.
TABLE_FLAT = "flat"
TABLE_SUB_TYPES = (TABLE_FLAT,)

# The one sub-type a card can have. Named rather than left blank so `sub_types_for` can
# answer for every visual type without a special case.
CARD_SINGLE = "single"
CARD_SUB_TYPES = (CARD_SINGLE,)

THEME_LIGHT = "light"
THEME_DARK = "dark"
THEMES = (THEME_LIGHT, THEME_DARK)

FILTER_TOP = "top"
FILTER_LEFT = "left"
FILTER_POSITIONS = (FILTER_TOP, FILTER_LEFT)

# How a card's number is written. A whitelist rather than a format string - see
# `clean_properties` for why nothing the user types becomes code.
FORMAT_PLAIN = "plain"
FORMAT_THOUSANDS = "thousands"
FORMAT_CURRENCY = "currency"
FORMAT_PERCENT = "percent"
NUMBER_FORMATS = (FORMAT_PLAIN, FORMAT_THOUSANDS, FORMAT_CURRENCY, FORMAT_PERCENT)

UNTITLED_PANEL = "Untitled visual"
UNTITLED_DASHBOARD = "Untitled dashboard"

# A panel taller than this is almost certainly a typo, and a page of them is unreadable.
MIN_PANEL_HEIGHT = 120
MAX_PANEL_HEIGHT = 1200

MAX_CORNER_RADIUS = 2.0

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
        source_table: the table this panel reads. Normally the flattened main table; a
            filter or card over a separate side table names that instead.
        source_columns: column 4, the columns the user picked. Kept alongside the specific
            roles below rather than replaced by them, because a table panel has no roles at
            all - its columns *are* what it shows.
        measure_column / aggregation: column 5's "what to show" - the number, and what to do
            with it. A count needs no measure column, which is why the two are separate
            fields rather than one sentence to be parsed.
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

    def height(self) -> int:
        """The panel's drawn height, already clamped by `clean_properties`."""
        return int(self.properties.get("height", DEFAULT_CHART_HEIGHT))


@dataclass
class DashboardSpec:
    """A whole dashboard: its look, its data source, and its ordered panels.

    Attributes:
        main_table: which table's rows are embedded. Empty means "detect it", which
            `flatten.detect_fact_table` answers - left empty rather than filled in on first
            render, so detection keeps improving as the user confirms more relationships and
            only stops the moment they choose for themselves.
        theme: the mode the exported page *opens* in. The reader can still toggle.
        filter_position: top strip or left rail.
        logo_bytes / logo_mime: the picture for the header, validated on the way in. Bytes
            rather than a path, because the exported file has to carry it.
        ai_guidance: the user's own standing preferences for plain-English generation
            ("always show currency in INR", "prefer horizontal bars"). Stored with the
            dashboard rather than with the session, because it is the kind of thing a user
            writes once and expects to still apply next month. It is *added to* the
            generator's fixed rules and can never replace them - see
            `live_dashboard/ai_spec.py`.
    """

    title: str = ""
    subtitle: str = ""
    logo_bytes: bytes | None = None
    logo_mime: str = ""
    theme: str = THEME_LIGHT
    filter_position: str = FILTER_TOP
    main_table: str = ""
    palette: str = PALETTE_DEFAULT
    ai_guidance: str = ""
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
        elif name == "height":
            try:
                cleaned["height"] = max(MIN_PANEL_HEIGHT, min(int(float(text)), MAX_PANEL_HEIGHT))
            except ValueError:
                logger.info("Ignoring an unreadable height %r in a panel's properties.", text)
        else:
            logger.info("Ignoring an unknown panel property %r.", name)
    return cleaned


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
    if "height" in properties:
        parts.append(f"height:{properties['height']}")
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


def duplicate_panel(spec: DashboardSpec, panel_id: str) -> PanelSpec | None:
    """Copies a panel, placing the copy directly after the original.

    A new `panel_id` and a copied `properties` dict, so the two share no mutable state - the
    bug this would otherwise have is invisible until the user edits one and watches the
    other change.
    """
    panel = find_panel(spec, panel_id)
    if panel is None:
        return None

    copy = PanelSpec(
        visual_type=panel.visual_type,
        sub_type=panel.sub_type,
        source_table=panel.source_table,
        source_columns=list(panel.source_columns),
        measure_column=panel.measure_column,
        aggregation=panel.aggregation,
        group_by=panel.group_by,
        colour_by=panel.colour_by,
        sort=panel.sort,
        top_n=panel.top_n,
        title=f"{panel.display_title()} (copy)",
        properties=dict(panel.properties),
        row_number=panel.row_number,
    )
    spec.panels.insert(spec.panels.index(panel) + 1, copy)
    return copy


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


def panel_problems(panel: PanelSpec, available_columns: dict[str, list[str]]) -> str:
    """Why this panel can't be drawn, or an empty string when it can.

    `available_columns` maps each embedded table name to the columns it actually carries -
    the flattened main table plus any side tables. A column that is not there is the
    requirement's warning case: the panel names a combination the confirmed relationships do
    not reach, and the honest answer is to say so rather than to guess a join.

    Returns a sentence the user can act on, because that is the whole value of the warning.
    """
    if not panel.source_table:
        return "Pick a data source for this visual."

    columns = available_columns.get(panel.source_table)
    if columns is None:
        return (
            f"'{panel.source_table}' isn't part of this dashboard's data. Either pick the "
            "main table, or confirm a link to it in Setup, under Relationships."
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

    if panel.visual_type == VISUAL_CHART:
        if not panel.group_by:
            return "Pick what to break this chart down by."
        if missing(panel.group_by):
            return _unreachable(panel.group_by, panel.source_table)
        if missing(panel.colour_by):
            return _unreachable(panel.colour_by, panel.source_table)

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
                spec.filter_position if spec.filter_position in FILTER_POSITIONS else FILTER_TOP
            ),
            "main_table": spec.main_table,
            "palette": spec.palette,
            "ai_guidance": spec.ai_guidance,
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
    position = str(settings.get("filter_position") or FILTER_TOP)

    return DashboardSpec(
        title=str(settings.get("title") or ""),
        subtitle=str(settings.get("subtitle") or ""),
        logo_bytes=logo_bytes,
        logo_mime=str(settings.get("logo_mime") or ""),
        theme=theme if theme in THEMES else THEME_LIGHT,
        filter_position=position if position in FILTER_POSITIONS else FILTER_TOP,
        main_table=str(settings.get("main_table") or ""),
        palette=str(settings.get("palette") or PALETTE_DEFAULT),
        # Absent from anything saved before phase 33, which is why it reads as "" rather
        # than failing the load - an older dashboard simply has no preferences yet.
        ai_guidance=str(settings.get("ai_guidance") or ""),
        panels=[_panel_from_dict(raw) for raw in raw_panels if isinstance(raw, dict)],
    )
