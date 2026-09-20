"""Plain English in, a dashboard spec out (phase 33).

The user types *"total sales card, sales by customer as a bar chart and a customer filter"*
and gets back the same rows the Add visual dialog would have built, one per visual. This is
step 1 of `docs/html_dashboard.md`'s user flow, and it is the only place in the dashboard
feature where a model is called at all.

Modelled on `transform/ai_parse.py`, which solved this problem once already. The same three
rules hold, for the same reasons:

1. **The catalog is the only vocabulary.** Visual types, sub-types, aggregations and sort
   orders are rendered into the prompt from the constants in `live_dashboard/model.py`. A
   visual type added in a later phase becomes available to plain English the moment it joins
   those tuples, and one the model invents is dropped here rather than reaching the page.
2. **Only real tables and columns.** The embedded tables' columns and their types go into
   the prompt, and every proposed panel is put through `model.panel_problems` against those
   same columns before it is offered. Names are matched on case and spacing only - a merely
   *similar* name is a guess, and a chart quietly totalling the wrong column is precisely
   the failure that rule exists to prevent.
3. **Nothing is guessed when nothing fits.** A request the catalog cannot express comes back
   as an empty panel list and one plain sentence in `clarification`.

**AI is used here only.** Once the rows exist they are ordinary rows: previewing, redrawing
and downloading never call a model, which is the same promise a saved Transform pipeline
makes.

The user's own guidance (the requirement's "Extra guidance for AI" box) is appended to the
*prompt*, never to the instructions. That is deliberate: user-typed text stays in the user's
turn, where it reads as a preference, rather than sitting among the hard rules where it would
read as one. It can only ever add a preference on top - the catalog and column checks below
run afterwards either way.

Like `llm.suggestions`, `propose_dashboard` never raises: a model that is down or talking
nonsense is a degraded result the user works around with the Add visual dialog, not a broken
screen.
"""

import logging

import pandas as pd
from pydantic import BaseModel, Field

from analyst.charts import (
    AGG_AVERAGE,
    AGG_COUNT,
    AGG_MAXIMUM,
    AGG_MINIMUM,
    AGG_SUM,
    CHART_BAR,
    CHART_BAR_HORIZONTAL,
    CHART_PIE,
    CHART_SCATTER,
    SORT_LABELS,
    SORT_LARGEST,
)
from live_dashboard import payload
from live_dashboard.model import (
    AGG_DISTINCT,
    AGG_MEDIAN,
    AGG_PERCENT_OF_TOTAL,
    AGG_RUNNING_TOTAL,
    AGG_STDEV,
    CHART_BOXPLOT,
    TEXT_FRIENDLY_AGGREGATIONS,
    CHART_HISTOGRAM,
    DASHBOARD_AGGREGATIONS,
    DASHBOARD_CHART_LABELS,
    DashboardSpec,
    FILTER_LABELS,
    FILTER_POSITIONS,
    FILTER_TOP,
    PanelSpec,
    VISUAL_CARD,
    VISUAL_CHART,
    VISUAL_LABELS,
    VISUAL_TABLE,
    VISUAL_TYPES,
    clean_properties,
    default_sub_type,
    panel_problems,
    sub_types_for,
)
from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

#: How many of a table's columns go into the prompt. A flattened table with three hundred
#: columns would otherwise crowd out the catalog itself.
PROMPT_COLUMN_LIMIT = 80

#: The most visuals one instruction may produce. A page of twenty is not a dashboard, it is
#: a scroll, and the user can always ask again for more.
MAX_PANELS = 10

#: How many charts sit side by side when the row numbers have to be worked out here.
CHARTS_PER_ROW = 2

#: Spellings of an aggregation that are obviously one of ours. Narrow on purpose: this maps
#: a synonym onto a catalog entry, it never invents a new kind of maths.
_AGGREGATION_SYNONYMS: dict[str, str] = {
    "total": AGG_SUM,
    "sum": AGG_SUM,
    "avg": AGG_AVERAGE,
    "mean": AGG_AVERAGE,
    "average": AGG_AVERAGE,
    "count": AGG_COUNT,
    "rows": AGG_COUNT,
    "min": AGG_MINIMUM,
    "minimum": AGG_MINIMUM,
    "lowest": AGG_MINIMUM,
    "max": AGG_MAXIMUM,
    "maximum": AGG_MAXIMUM,
    "highest": AGG_MAXIMUM,
    "middle": AGG_MEDIAN,
    "med": AGG_MEDIAN,
    "unique": AGG_DISTINCT,
    "countunique": AGG_DISTINCT,
    "distinctcount": AGG_DISTINCT,
    "uniquecount": AGG_DISTINCT,
    "nunique": AGG_DISTINCT,
    "standarddeviation": AGG_STDEV,
    "std": AGG_STDEV,
    "spread": AGG_STDEV,
    "share": AGG_PERCENT_OF_TOTAL,
    "shareoftotal": AGG_PERCENT_OF_TOTAL,
    "percentoftotal": AGG_PERCENT_OF_TOTAL,
    "%oftotal": AGG_PERCENT_OF_TOTAL,
    "percentage": AGG_PERCENT_OF_TOTAL,
    "proportion": AGG_PERCENT_OF_TOTAL,
    "cumulative": AGG_RUNNING_TOTAL,
    "cumulativetotal": AGG_RUNNING_TOTAL,
    "running": AGG_RUNNING_TOTAL,
    "runningsum": AGG_RUNNING_TOTAL,
    "runningtotal": AGG_RUNNING_TOTAL,
    "ytd": AGG_RUNNING_TOTAL,
}

#: A chart shape we can't draw, mapped onto the nearest one we can.
#:
#: The fence around shapes is real and this is where it stops being silent. A shape needs
#: drawing code *inside the exported file*; a shape with none is a blank box discovered after
#: the file has been emailed. But dropping the visual throws away a good idea, so the nearest
#: honest shape is drawn instead and the swap is always reported - never quietly applied.
#:
#: A shape with no sensible stand-in (a map) is deliberately absent: it still refuses, and
#: says why. A gauge is absent for the same reason - flattened to a card it loses the dial,
#: which is the only thing anyone asks a gauge for.
_SHAPE_FALLBACKS: dict[str, str] = {
    "treemap": CHART_PIE,
    "sunburst": CHART_PIE,
    "funnel": CHART_BAR_HORIZONTAL,
    "waterfall": CHART_BAR,
    "radar": CHART_BAR,
    "bubble": CHART_SCATTER,
    "violin": CHART_BOXPLOT,
    "density": CHART_HISTOGRAM,
}

_INSTRUCTIONS = """\
You turn one plain-English request into rows of a dashboard spec. Each row is one visual.

Hard rules:
- Use ONLY the visual types, sub-types, aggregations and sort orders from the catalog you
  are given. Never invent one.
- Use ONLY table and column names from the schema you are given, spelled exactly as shown.
  The user may abbreviate ("sales"); you must write the real column ("Amount"). Never
  invent a column and never guess at one that is merely similar.
- Every field is written as TEXT, numbers included. row_number is "1", "2", "3"; top_n is
  the digits alone, or "" for no cap.
- A filter names its column in `columns` and nothing else - no measure, no group_by.
- A card and a chart both need an aggregation and, unless the aggregation is "count", a
  `measure_column` that is a NUMBER column in the schema.
- A chart also needs `group_by`: what the number is broken down by. "Sales by customer"
  means measure_column=the amount column, group_by=the customer column. Never the other
  way round.
- A table names the columns it shows in `columns`.
- Some sub-types need one more field, and are dropped without it:
  bar_stacked, bar_grouped and heatmap need `colour_by` (the second breakdown);
  combo needs `measure_column_2` (the number drawn as a line over the bars);
  histogram needs `measure_column` and NO `group_by` - it shows one number's spread.
- "percent_of_total" only works on a chart, never a card. "running_total" only works on a
  chart broken down by a DATE column.
- row_number lays the page out: visuals sharing a number sit side by side. Put cards
  together on row 1, then charts two to a row, then tables on rows of their own. Filters
  ignore row_number - say where they go with filter_position ("top" or "left").
- Give every visual a short, plain title a reader would understand.
- Propose at most 10 visuals. Choose the ones that answer the request best.
- If the request cannot be done with this catalog and these columns, return an EMPTY panels
  list and say why in one plain sentence in `clarification`. Never return a visual that is
  merely close - a wrong chart is worse than an honest no.\
"""


class ProposedPanel(BaseModel):
    """One visual as the model proposed it, before anything has been checked.

    Every field is text, numbers included, for the reason `transform.ai_parse.ParsedValue`
    records: a provider's strict structured-output mode - which every cloud profile asks for
    - handles unions badly, and a field that is sometimes a number and sometimes blank is a
    union. `_whole_number` turns the text back into an integer here, where a bad value
    becomes a warning rather than a crash.

    Unlike Transform's name/value pairs, these fields are fixed and few, so an ordinary flat
    schema describes them. The awkward shape there existed because twenty-nine operations'
    worth of parameter keys cannot be declared up front; nothing like that applies here.
    """

    visual_type: str = Field(..., description="filter, card, chart or table")
    sub_type: str = Field(default="", description="A sub-type allowed for that visual type")
    source_table: str = Field(default="", description="The table this visual reads")
    columns: str = Field(
        default="", description="Comma separated; the filter's column, or a table's columns"
    )
    measure_column: str = Field(default="", description="The number column to aggregate")
    measure_column_2: str = Field(
        default="", description="Only for a combo chart: the second number, drawn as a line"
    )
    aggregation: str = Field(default="", description="An aggregation from the catalog")
    group_by: str = Field(default="", description="The column the number is broken down by")
    colour_by: str = Field(default="", description="Optional second breakdown, for a legend")
    sort: str = Field(default="", description="A sort order from the catalog")
    top_n: str = Field(default="", description="How many categories to keep, digits only")
    title: str = Field(default="", description="The heading shown on the visual")
    row_number: str = Field(default="", description="Which row it sits on, digits only")
    properties: str = Field(
        default="", description="Optional, e.g. 'format:currency, height:420'"
    )


class ProposedDashboard(BaseModel):
    """What one generation returns: the whole page, or an honest reason there is none."""

    title: str = Field(default="", description="A title for the dashboard")
    subtitle: str = Field(default="", description="One short line under the title")
    filter_position: str = Field(default="", description="top or left")
    panels: list[ProposedPanel] = Field(default_factory=list)
    clarification: str = Field(
        default="", description="Why the request can't be built, if it can't"
    )


# --------------------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------------------


def describe_catalog_for_prompt() -> str:
    """Every visual type and the sub-types it allows, as prompt text.

    Rendered from the model's own tuples rather than written out by hand, so a sub-type
    added in a later phase reaches plain English without this module being touched.
    """
    sub_labels: dict[str, str] = {}
    sub_labels.update(FILTER_LABELS)
    sub_labels.update(DASHBOARD_CHART_LABELS)

    lines: list[str] = []
    for visual_type in VISUAL_TYPES:
        lines.append(f"- {visual_type} ({VISUAL_LABELS.get(visual_type, visual_type)})")
        described = [
            f"{name} ({sub_labels[name]})" if name in sub_labels else name
            for name in sub_types_for(visual_type)
        ]
        lines.append("    sub-types: " + ", ".join(described))

    lines.append("Aggregations: " + ", ".join(
        f"{key} ({label})" for key, label in DASHBOARD_AGGREGATIONS.items()
    ))
    lines.append("Sort orders: " + ", ".join(
        f"{key} ({label})" for key, label in SORT_LABELS.items()
    ))
    return "\n".join(lines)


def _type_word(dtype) -> str:
    """`number`, `date`, `true/false` or `text` for one column's dtype.

    The type beside the name is what lets the model tell an amount from an invoice number
    that merely looks like one - and it is what `_measure_is_a_number` checks against
    afterwards.
    """
    kind = getattr(dtype, "kind", "O")
    if kind in "iuf":
        return "number"
    if kind == "b":
        return "true/false"
    if kind == "M":
        return "date"
    return "text"


def describe_tables_for_prompt(tables: dict[str, pd.DataFrame]) -> str:
    """The tables the dashboard can draw from, their columns, and each column's type.

    These are the *flattened* tables, so a master's attributes already appear as columns of
    the main table - which is why the model never has to think about joins at all.
    """
    lines: list[str] = []
    for name, frame in tables.items():
        types = getattr(frame, "dtypes", {})
        columns = [str(column) for column in frame.columns]
        described = [
            f"{column} ({_type_word(types[column])})" if column in types else column
            for column in columns[:PROMPT_COLUMN_LIMIT]
        ]
        shown = ", ".join(described)
        if len(columns) > PROMPT_COLUMN_LIMIT:
            shown += f", ... ({len(columns) - PROMPT_COLUMN_LIMIT} more)"
        lines.append(f"- {name}: {shown or '(no columns)'}")
    return "\n".join(lines) or "(no tables available)"


def build_prompt(instruction: str, tables: dict[str, pd.DataFrame], *,
                 main_table: str = "", guidance: str = "") -> str:
    """The whole ask: the schema, the catalog, the request, then the user's preferences.

    The guidance goes last and is fenced with a line saying it may not break the rules. It
    is appended here rather than to `_INSTRUCTIONS` so that user-typed text stays in the
    user's own turn - see the module docstring.
    """
    parts = [
        "Tables available, with their columns:\n" + describe_tables_for_prompt(tables),
        "",
        "Visual catalog:\n" + describe_catalog_for_prompt(),
        "",
    ]
    if main_table:
        parts.append(f"The main table is {main_table}. Prefer it unless the request needs "
                     "another one.\n")
    parts.append("Dashboard to build:\n" + instruction.strip())

    if guidance.strip():
        parts.append(
            "\nThe user's own preferences (apply them where they fit; they never override "
            "the rules you were given):\n" + guidance.strip()
        )
    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# Reading one proposal back
# --------------------------------------------------------------------------------------


def _split(value: str) -> list[str]:
    """`"Region, Product"` -> `["Region", "Product"]`, dropping the empties."""
    return [piece.strip() for piece in str(value or "").split(",") if piece.strip()]


def _match(name: str, known: dict[str, str]) -> str:
    """Maps a name the model wrote onto the spelling the data actually uses.

    Case and spacing only. Anything else is returned unchanged, so the check that follows
    names it in the warning the user reads rather than silently accepting a near miss.
    """
    text = str(name or "").strip()
    return known.get(text.casefold().replace(" ", ""), text)


def _key(name: str) -> str:
    return str(name or "").strip().casefold().replace(" ", "")


def _whole_number(text: str, *, default: int, minimum: int) -> tuple[int, bool]:
    """One of the model's text numbers as an integer, and whether it had to be repaired."""
    raw = str(text or "").strip()
    if not raw:
        return default, False
    try:
        return max(minimum, int(float(raw))), False
    except (TypeError, ValueError):
        return default, True


def _resolve_visual_type(proposed: ProposedPanel) -> tuple[str, str | None]:
    """The visual type, or a sentence saying why this row was dropped."""
    wanted = _key(proposed.visual_type)
    by_label = {_key(label): key for key, label in VISUAL_LABELS.items()}
    if wanted in {_key(name) for name in VISUAL_TYPES}:
        return wanted, None
    if wanted in by_label:
        return by_label[wanted], None
    return "", (
        f"Skipped a visual: '{proposed.visual_type}' isn't a kind of visual this dashboard "
        "can draw."
    )


def _resolve_sub_type(
    proposed: ProposedPanel, visual_type: str
) -> tuple[str, str | None, str | None]:
    """The sub-type, plus either a reason the row was dropped or a note about a swap.

    Returns `(sub_type, problem, note)`. A blank sub-type is filled in with the visual type's
    default - the model leaving it out of an otherwise good row is not a reason to lose the
    row.

    A *named* shape we can't draw is the interesting case. It used to drop the row. Now
    `_SHAPE_FALLBACKS` maps most of them onto the nearest shape we can draw and the visual is
    still built - but never silently: the swap comes back as a note the user reads, so a pie
    where a treemap was asked for is explained rather than discovered.
    """
    allowed = sub_types_for(visual_type)
    wanted = _key(proposed.sub_type)
    if not wanted:
        return default_sub_type(visual_type), None, None
    for name in allowed:
        if _key(name) == wanted:
            return name, None, None
    labels = {**FILTER_LABELS, **DASHBOARD_CHART_LABELS}
    for name in allowed:
        if _key(labels.get(name, "")) == wanted:
            return name, None, None

    instead = _SHAPE_FALLBACKS.get(wanted) if visual_type == VISUAL_CHART else None
    if instead and instead in allowed:
        title = str(proposed.title or "").strip() or "A visual"
        return instead, None, (
            f"'{title}' asked for a {proposed.sub_type.strip()}. This dashboard can't draw "
            f"one yet, so it's a {DASHBOARD_CHART_LABELS.get(instead, instead).lower()} "
            "instead - change it in Edit if you'd rather have something else."
        )

    return "", (
        f"Skipped a {VISUAL_LABELS.get(visual_type, visual_type).lower()}: "
        f"'{proposed.sub_type}' isn't one of its styles."
    ), None


def _resolve_aggregation(proposed: ProposedPanel) -> tuple[str, str | None]:
    """The aggregation, or a sentence saying why this row was dropped.

    Unknown means dropped rather than defaulted to a sum: every other field can be wrong in
    a way the user will *see*, but a wrong aggregation just shows a different number with no
    sign that anything happened.
    """
    wanted = _key(proposed.aggregation)
    if not wanted:
        return AGG_SUM, None
    if wanted in DASHBOARD_AGGREGATIONS:
        return wanted, None
    if wanted in _AGGREGATION_SYNONYMS:
        return _AGGREGATION_SYNONYMS[wanted], None
    for key, label in DASHBOARD_AGGREGATIONS.items():
        if _key(label) == wanted:
            return key, None
    return "", f"Skipped '{proposed.title or 'a visual'}': '{proposed.aggregation}' isn't a way of totalling."


def _resolve_sort(proposed: ProposedPanel) -> str:
    """The sort order, falling back to the default. Never a reason to drop a row: the order
    of the bars is visible at a glance and changed in one click."""
    wanted = _key(proposed.sort)
    if wanted in SORT_LABELS:
        return wanted
    for key, label in SORT_LABELS.items():
        if _key(label) == wanted:
            return key
    if wanted:
        logger.info("Ignoring an unknown sort order %r from the model.", proposed.sort)
    return SORT_LARGEST


def _numeric_columns(frame: pd.DataFrame) -> set[str]:
    types = getattr(frame, "dtypes", None)
    if types is None:
        return set()
    return {str(name) for name in frame.columns if _type_word(types[name]) == "number"}


def _build_one(
    proposed: ProposedPanel,
    tables: dict[str, pd.DataFrame],
    main_table: str,
) -> tuple[PanelSpec | None, str | None]:
    """Turns one proposal into a real, checked panel, or says why it can't be.

    Returns `(panel, warning)`. A warning with no panel means the row was dropped and the
    sentence says why; a warning *with* a panel is a note about something that was changed to
    make the row drawable - a shape swapped for the nearest one we have. Both are shown in
    the same list, because both are things the user would rather know than discover.

    Everything that can go wrong here is the model's fault rather than the user's, so each
    failure becomes a sentence - never an exception the dialog would have to catch.
    """
    visual_type, problem = _resolve_visual_type(proposed)
    if problem:
        return None, problem

    sub_type, problem, note = _resolve_sub_type(proposed, visual_type)
    if problem:
        return None, problem

    known_tables = {_key(name): name for name in tables}
    table = _match(proposed.source_table, known_tables) or main_table
    frame = tables.get(table)
    if frame is None:
        return None, (
            f"Skipped '{proposed.title or 'a visual'}': there's no table called "
            f"'{proposed.source_table}' in this dashboard's data."
        )

    known_columns = {_key(column): str(column) for column in frame.columns}

    aggregation, problem = _resolve_aggregation(proposed)
    if problem:
        return None, problem

    top_n, bad_top_n = _whole_number(proposed.top_n, default=0, minimum=0)
    row_number, bad_row = _whole_number(proposed.row_number, default=1, minimum=1)

    panel = PanelSpec(
        visual_type=visual_type,
        sub_type=sub_type,
        source_table=table,
        source_columns=[_match(name, known_columns) for name in _split(proposed.columns)],
        measure_column=_match(proposed.measure_column, known_columns),
        measure_column_2=_match(proposed.measure_column_2, known_columns),
        aggregation=aggregation,
        group_by=_match(proposed.group_by, known_columns),
        colour_by=_match(proposed.colour_by, known_columns),
        sort=_resolve_sort(proposed),
        top_n=top_n,
        title=str(proposed.title or "").strip(),
        properties=clean_properties(proposed.properties),
        row_number=row_number,
    )

    # The same check the Add visual dialog makes, against the same columns. A panel the
    # user built themselves is *listed* with this sentence so they can fix it; one the model
    # invented over a column that isn't there is just noise, so it goes.
    trouble = panel_problems(
        panel,
        {name: [str(c) for c in f.columns] for name, f in tables.items()},
        payload.date_columns(tables),
    )
    if trouble:
        return None, f"Skipped '{panel.display_title()}': {trouble}"

    # The likely model error, and an invisible one: "sales by customer" coming back with the
    # two the wrong way round. The types are already in the prompt, so this is cheap.
    #
    # Counting *different* values is the exception, and an important one: "how many
    # customers" is the usual question and customers are text. Refusing it here would refuse
    # the most obvious use of the whole aggregation.
    if (visual_type in (VISUAL_CARD, VISUAL_CHART)
            and aggregation not in TEXT_FRIENDLY_AGGREGATIONS
            and panel.measure_column not in _numeric_columns(frame)):
        return None, (
            f"Skipped '{panel.display_title()}': '{panel.measure_column}' isn't a number, so "
            "it can't be totalled. Count unique would work over it if you meant to count it."
        )

    if bad_top_n:
        logger.info("A proposed panel had an unreadable top_n %r - showing everything.",
                    proposed.top_n)
    if bad_row:
        logger.info("A proposed panel had an unreadable row number %r - using row 1.",
                    proposed.row_number)
    return panel, note


def _lay_out(panels: list[PanelSpec]) -> None:
    """Gives the visuals row numbers when the model didn't really choose any.

    The model has no idea what looks good, and the common failure is every visual coming
    back on row 1 - which draws six charts squeezed into one strip. When that happens the
    layout is worked out here instead: cards together on the first row, then charts a couple
    to a row, then tables on rows of their own. Filters are untouched; `filter_position`
    decides where they go.
    """
    visuals = [panel for panel in panels if not panel.is_filter()]
    if len(visuals) <= CHARTS_PER_ROW:
        return
    if len({panel.row_number for panel in visuals}) > 1:
        return  # The model did choose a layout. Leave it alone.

    row = 1
    cards = [panel for panel in visuals if panel.visual_type == VISUAL_CARD]
    for panel in cards:
        panel.row_number = row
    if cards:
        row += 1

    charts = [panel for panel in visuals if panel.visual_type == VISUAL_CHART]
    for index, panel in enumerate(charts):
        panel.row_number = row + index // CHARTS_PER_ROW
    if charts:
        row += (len(charts) + CHARTS_PER_ROW - 1) // CHARTS_PER_ROW

    for panel in visuals:
        if panel.visual_type == VISUAL_TABLE:
            panel.row_number = row
            row += 1


# --------------------------------------------------------------------------------------
# The one public call
# --------------------------------------------------------------------------------------


def propose_dashboard(
    profile: dict,
    instruction: str,
    tables: dict[str, pd.DataFrame],
    *,
    main_table: str = "",
    guidance: str = "",
    key_path=None,
) -> tuple[DashboardSpec | None, list[str], str | None]:
    """Asks the Light Model to turn `instruction` into a dashboard spec.

    Args:
        profile: the Light Model profile to call.
        instruction: what the user typed.
        tables: the embedded tables, keyed by the name panels refer to them by - the same
            dict the exporter is handed, so the columns checked here are the columns that
            will exist.
        main_table: which of them is the fact table, used when a proposal names no table.
        guidance: the user's standing preferences, appended to the prompt below the rules.

    Returns:
        `(spec, warnings, clarification)`. `spec` is None when nothing usable came back. The
        warnings name every proposal that was dropped and why, so the dialog can say "3 of 5
        understood" rather than quietly returning fewer visuals than were asked for.

    Never raises: a model that is unreachable or talking nonsense comes back as `None` and
    one warning, with the Add visual dialog still sitting behind the box.
    """
    if not instruction.strip():
        return None, ["Type what you want the dashboard to show first."], None
    if not tables:
        return None, ["There's no data loaded to build a dashboard from yet."], None

    try:
        response = run_structured(
            profile,
            build_prompt(instruction, tables, main_table=main_table, guidance=guidance),
            ProposedDashboard,
            instructions=_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("The dashboard could not be generated: %s", error)
        return None, [f"We couldn't read that description: {error}"], None

    warnings: list[str] = []
    panels: list[PanelSpec] = []

    for proposed in response.panels:
        if len(panels) >= MAX_PANELS:
            warnings.append(
                f"Only the first {MAX_PANELS} visuals were kept. Ask again for the rest."
            )
            break
        panel, warning = _build_one(proposed, tables, main_table)
        if panel is None:
            warnings.append(warning or "A visual couldn't be understood.")
            continue
        if warning:
            # A note, not a refusal: the visual is kept and the change is explained.
            warnings.append(warning)
        panels.append(panel)

    clarification = str(response.clarification or "").strip() or None

    logger.info(
        "Plain-English dashboard produced %d visual(s), %d warning(s).", len(panels),
        len(warnings),
    )

    if not panels:
        return None, warnings, clarification

    _lay_out(panels)

    position = _key(response.filter_position)
    spec = DashboardSpec(
        title=str(response.title or "").strip(),
        subtitle=str(response.subtitle or "").strip(),
        filter_position=position if position in FILTER_POSITIONS else FILTER_TOP,
        main_table=main_table,
        ai_guidance=guidance.strip(),
        panels=panels,
    )
    return spec, warnings, clarification


def describe_panel(panel: PanelSpec) -> str:
    """One proposed visual in the words the user reads before accepting it.

    Its own sentence rather than the spec table's columns, because this is read *before*
    anything exists to select - "Bar chart: Sum of Amount by Customer - row 2" has to stand
    on its own.
    """
    kind = VISUAL_LABELS.get(panel.visual_type, panel.visual_type)
    if panel.is_filter():
        style = FILTER_LABELS.get(panel.sub_type, panel.sub_type)
        return f"**{panel.display_title()}** - {style} on {panel.filter_column()}"

    if panel.visual_type == VISUAL_TABLE:
        return (f"**{panel.display_title()}** - table of "
                f"{', '.join(panel.source_columns) or 'no columns yet'} - row {panel.row_number}")

    total = DASHBOARD_AGGREGATIONS.get(panel.aggregation, panel.aggregation)
    what = "rows" if panel.aggregation == AGG_COUNT else panel.measure_column
    if panel.visual_type == VISUAL_CARD:
        return f"**{panel.display_title()}** - card showing {total} of {what}"

    style = DASHBOARD_CHART_LABELS.get(panel.sub_type, panel.sub_type)
    if panel.sub_type == CHART_HISTOGRAM:
        # A histogram totals nothing - it counts how often each size of number turns up, so
        # "Sum of Amount" would describe a chart that isn't being drawn.
        return (f"**{panel.display_title()}** - histogram showing how {what} is spread"
                f" - row {panel.row_number}")

    breakdown = f" by {panel.group_by}" if panel.group_by else ""
    if panel.measure_column_2:
        what = f"{what} and {panel.measure_column_2}"
    if panel.colour_by:
        breakdown += f", split by {panel.colour_by}"
    return (f"**{panel.display_title()}** - {style.lower()} of {total} of {what}{breakdown}"
            f" - row {panel.row_number}")
