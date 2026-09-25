"""Plain English in, a dashboard spec out (phase 33), one round at a time (phase 35).

The user types *"total sales card, sales by customer as a bar chart and a customer filter"*
and gets back the same rows the Add visual dialog would have built, one per visual. Then they
type *"make the region chart horizontal and add a monthly trend"*, and **that** round changes
those two things and touches nothing else. This is the only place in the dashboard feature
where a model is called at all.

Two things make a round an edit rather than a rewrite, and neither is a matter of asking the
model nicely:

- **The dashboard is the model's only memory.** `llm.client.run_structured` is stateless, so
  every request carries the page as it stands - `describe_spec_for_prompt`, field by field,
  numbered. A chat transcript would drift from the page the moment the user edited a visual
  by hand; the page cannot disagree with itself.
- **A round returns edits, not a page.** Each proposal names an `action` and, for `update`
  and `remove`, the number of the visual it means. A visual no proposal names is left alone,
  so it cannot be lost to a model that simply forgot to write it out a second time.

`_DESIGN_RULES` is the third piece: a written set of professional-dashboard rules sent with
every request, which is where a page that looks designed rather than dumped comes from.

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

There is no separate box for standing preferences since phase 36. "Always show currency in
INR" is a sentence, and the instruction the user types is already a sentence - a second box
only asked them to decide which of the two a preference belonged in, and the answer never
mattered, since both were sent in the same request anyway.

Like `llm.suggestions`, `propose_dashboard` never raises: a model that is down or talking
nonsense is a degraded result the user asks again about, not a broken screen.
"""

import logging
import re
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, Field

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
    SORT_AUTOMATIC,
    SORT_LARGEST,
)
from live_dashboard import payload
# For `measure_label` only: the name a measure is given is the column the chart aggregates
# into, the entry in its legend and the words in this sentence, and those three must agree.
from live_dashboard import vega_spec
from live_dashboard.flatten import COLUMN_SEPARATOR
from live_dashboard.model import (
    AGG_DISTINCT,
    VISUAL_FILTER,
    AGG_MEDIAN,
    AGG_PERCENT_OF_TOTAL,
    AGG_RUNNING_TOTAL,
    AGG_STDEV,
    CHART_BOXPLOT,
    CHART_DONUT,
    TEXT_FRIENDLY_AGGREGATIONS,
    CHART_HISTOGRAM,
    DASHBOARD_AGGREGATIONS,
    DASHBOARD_CHART_LABELS,
    DASHBOARD_SORT_LABELS,
    DEFAULT_CURRENCY,
    DashboardSpec,
    FILTER_DROPDOWN,
    FILTER_LABELS,
    FILTER_MULTISELECT,
    FILTER_POSITIONS,
    DEFAULT_FILTER_POSITION,
    FORMAT_CURRENCY,
    PanelSpec,
    VISUAL_CARD,
    VISUAL_CHART,
    VISUAL_LABELS,
    VISUAL_TABLE,
    VISUAL_TYPES,
    AXIS_LEFT,
    AXIS_RIGHT,
    CARD_SIZES,
    CURRENCY_CODES,
    LABELLABLE_CHARTS,
    LABELS_ON,
    LEGEND_POSITIONS,
    MAX_MEASURES_PER_CHART,
    MULTI_MEASURE_CHARTS,
    NAMED_COLOURS,
    NUMBER_FORMATS,
    PANEL_SIZES,
    PANEL_WIDTHS,
    TABLE_DRILLDOWN,
    TABLE_FLAT,
    TABLE_LABELS,
    WIDTH_FULL,
    clean_measures,
    clean_properties,
    property_problems,
    default_sub_type,
    panel_problems,
    measures_text,
    properties_text,
    sub_types_for,
)
from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

#: How many of a table's columns go into the prompt. A flattened table with three hundred
#: columns would otherwise crowd out the catalog itself.
PROMPT_COLUMN_LIMIT = 80

#: The most visuals one instruction may produce. A page of twenty is not a dashboard, it is
#: a scroll, and the user can always ask again for more.
MAX_PANELS = 20

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

#: What each round may do to a visual that already exists.
ACTION_ADD = "add"
ACTION_UPDATE = "update"
ACTION_REMOVE = "remove"
ACTIONS = (ACTION_ADD, ACTION_UPDATE, ACTION_REMOVE)

#: What a blank box means when there is nothing on the dashboard yet - the requirement's
#: "leave it blank and press Generate". With visuals already there a blank box is refused
#: instead: it is ambiguous between "add something" and "start over", and one of those is
#: destructive.
DEFAULT_INSTRUCTION = "Design a clear, professional dashboard for this data."

#: The rules that hold whether the user is asking for a first draft or for one change to a
#: dashboard that already exists. Kept apart from the two openings below so that the draft
#: prompt and the round prompt cannot drift into disagreeing about what a chart needs.
_CORE_RULES = """Hard rules:
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
- A "drilldown" table is different: `columns` is the LEVELS to group by, outermost first
  ("Category, SubCategory, Item") - two to eight of them - and it needs an `aggregation`
  and, unless that is "count", a `measure_column`, exactly like a chart. It shows one
  total per group, not the rows themselves. Use "flat" when the user wants the rows.
- A "drilldown" table can total SEVERAL numbers - one column each, in the order asked for.
  The first goes in `measure_column` + `aggregation` and the rest in `more_measures`,
  exactly as on a chart; the axis is ignored here, because columns have no sides. Asked for
  "sum of Cost, sum of Quantity, and the average, minimum and maximum of Price", write them
  all. A "flat" table cannot: it lists rows, so it has no totals to add a number to.
- Some sub-types need one more field, and are dropped without it:
  bar_stacked, bar_grouped and heatmap need `colour_by` (the second breakdown);
  combo needs a right-axis entry in `more_measures` (the number drawn as a line);
  histogram needs `measure_column` and NO `group_by` - it shows one number's spread.
- "percent_of_total" only works on a chart, never a card. "running_total" only works on a
  chart broken down by a DATE column.
- row_number lays the page out: visuals sharing a number sit side by side. Filters ignore
  row_number - say where they go with filter_position ("top" or "left").
- If the request cannot be done with this catalog and these columns, return an EMPTY panels
  list and say why in one plain sentence in `clarification`. Never return a visual that is
  merely close - a wrong chart is worse than an honest no.
- Never return an update that leaves the visual exactly as it was. If nothing in the catalog
  does what was asked (a setting that doesn't exist), that is an honest no too.
- How a visual LOOKS is asked for in `properties`, never by changing its columns. "Show
  the numbers on the bars" is properties="labels:yes"; "make it taller" is "size:tall";
  "move the key to the bottom" is "legend:bottom". When you change one setting, write out
  the settings the visual already has as well, or they are lost."""

#: The written design rules - the "professional dashboard" skill of phase 35.
#:
#: This is where a page that looks designed rather than dumped comes from, and it costs
#: nothing but prose - provided every shape it recommends really exists, which is the whole
#: reason phase 34 widened the catalog first. `_DESIGN_RULE_NAMES` below pins each one to
#: the catalog so the prose cannot go on recommending a chart we stopped being able to draw.
_DESIGN_RULES = """Design rules - follow these unless the user asks for something else:
- Open with the headline numbers: at most 4 cards, together on row 1. A wall of cards is a
  scoreboard, not a dashboard.
- Where the data has a date column, give the page one trend - a line or an area chart
  broken down by that date - on a row of its own.
- At most 2 charts to a row. Tables go on rows of their own, near the bottom.
- Where the data has a hierarchy (category then subcategory then item, region then branch),
  a "drilldown" table reads better than a flat one: the reader sees the top totals and
  opens only the branch they care about. Use "flat" when the rows themselves are the point.
- Propose at most 12 visuals unless the user asks for more.
- Sort a bar chart "largest" so the biggest bar comes first, unless it is broken down by a
  date, where "automatic" keeps the dates in order. When the breakdown is month-year TEXT
  such as "Apr-2024" (words sort alphabetically, not in time), use sort "date" so the bars
  run oldest to newest - the same when the user asks for the months "in date order".
- Use "bar_horizontal" when the breakdown is long text: customer, product and employee
  names overlap badly on an upright bar.
- Use "pie" or "donut" only for a breakdown with a handful of categories. For anything
  longer a bar reads better - and set top_n to keep it short.
- Put money columns on "format:currency" in properties, and name the currency with
  "currency:INR" unless the user says otherwise.
- Give every visual a title that names the number: Total sales by region, never Chart 1.
- Add a filter for the one or two columns a reader would want to narrow by - usually a
  date and the main category.
- Leave filter_position as "left" unless the user asks for them across the top: down the
  side, filters never push the first row of visuals off the screen.
- A filter on a text column with more than a handful of values is a "dropdown" or a
  "multiselect" - both open only when the reader clicks them, so a long list of customers
  costs no height until it is wanted.
- Turn "labels:yes" on for a pie or a donut, and for a bar chart with few enough bars to
  read. On a chart with many bars the numbers collide, so leave them off unless asked.
- Asked for several totals of the SAME column ("min, average and max salary"), put them
  all on the LEFT with `more_measures` - they share a unit, so they must share an axis to
  be compared. Use "right" only when the units differ (rupees against a headcount),
  which is what a combo chart is.
- On a drill-down table, write the totals in the order the user listed them - the first is
  the leftmost column. Set "row_count:yes" only if they ask how many rows are behind each
  group; a drill-down that lists its own totals does not need the count as well.
- Use "width:full" for the one chart the page is really about, and for a wide table. Two
  half-width charts to a row is the ordinary case and needs no width at all.
- Filter on a RELATED table's column rather than on one table's own column where both
  exist: the related spelling is carried on every table it reaches, so that one filter
  narrows the whole page instead of a single visual. This applies to ANY column that comes
  from a related table, not just one example - e.g. "Employee Master - Department" AND
  "Employee Master - Name" both follow the same "Table - Column" spelling, never the raw
  "Department" or "Name" on its own."""

#: Every catalog entry `_DESIGN_RULES` recommends by name, declared beside the prose so a
#: test can pin all of them at once. Cheaper than a record per shape, and it catches the one
#: failure that matters: a rule that keeps recommending something the page cannot draw.
_DESIGN_RULE_NAMES: tuple[str, ...] = (
    TABLE_FLAT,
    TABLE_DRILLDOWN,
    CHART_LINE,
    CHART_AREA,
    CHART_BAR_HORIZONTAL,
    CHART_PIE,
    CHART_DONUT,
    SORT_LARGEST,
    SORT_AUTOMATIC,
    FORMAT_CURRENCY,
    DEFAULT_CURRENCY,
    DEFAULT_FILTER_POSITION,
    FILTER_DROPDOWN,
    FILTER_MULTISELECT,
    LABELS_ON,
    WIDTH_FULL,
)

#: The first draft: an empty dashboard, built from one description.
_INSTRUCTIONS = f"""You turn one plain-English request into rows of a dashboard spec. Each row is one visual,
and every row you return is added to the page.

{_CORE_RULES}

{_DESIGN_RULES}"""

#: One round of editing a dashboard that already exists.
#:
#: The difference that matters is `action`. A round returns *edits*, never the whole page
#: again: a visual the model does not mention is left exactly as it is. That is what stops a
#: page losing a chart to a model that simply forgot to write it out a second time - which
#: returning the full list every round would have made a one-in-ten event.
_ROUND_INSTRUCTIONS = f"""You are editing a dashboard that already exists. Every visual on it is listed for you,
numbered. Return ONLY the changes the user asked for - never the whole page again.

Each row you return says what to do with `action`:
- "add": a brand new visual. Leave `target` empty.
- "update": replace visual number `target`. Write out ALL of that visual's fields, not only
  the ones you are changing - the listing shows you what they currently are. A field you
  leave blank becomes blank, which is how you take away a colour split or a top_n.
- "remove": delete visual number `target`. No other field is needed.

A visual you do not mention is left alone, so do not re-send one you are not changing.
Set `title`, `subtitle` or `filter_position` only when the user asked to change them; leave
them blank otherwise.

{_CORE_RULES}

{_DESIGN_RULES}"""

#: The words a checklist line starts with - one per visual type, in the user's language
#: rather than the catalog's keys, because the user reads and edits these lines.
CHECKLIST_KINDS = ("Card", "Filter", "Chart", "Table")

#: Phase 45, step one: plan the page as a short list the user can edit, and build nothing.
#: Plain lines rather than spec rows, because the person reading them is not a developer -
#: "Chart: Sum of Amount by Customer (horizontal bar)" is a line anyone can correct.
_CHECKLIST_DRAFT_INSTRUCTIONS = f"""You plan a dashboard before it is built. Read the tables and write a short checklist:
ONE plain line per visual, and nothing else.

Each line starts with what the visual is - {', '.join(f'"{kind}:"' for kind in CHECKLIST_KINDS)} -
and then says what it shows in a few words, using the real column names from the schema.
Put a chart's or table's style in brackets at the end. For example:
Card: Total Amount
Filter: Customer - CustName
Chart: Sum of Amount by Customer - CustName (horizontal bar)
Chart: Sum of Amount by TxnDate (line)
Table: Sum of Amount by Category, then Item (drill-down)

Only plan what the catalog can draw and the columns really hold. Never write code, field
names or explanations - a person reads this list and edits it. If nothing can be planned,
return no lines and say why in one plain sentence in `clarification`.

{_DESIGN_RULES}"""

#: Phase 45, step two: build the list the user approved - exactly that list. The line
#: number each visual carries back is what lets `build_from_checklist` say which line was
#: not built, and drop a visual that belongs to no line, rather than trusting the model to
#: have counted.
_CHECKLIST_BUILD_INSTRUCTIONS = f"""You build a dashboard from a numbered list the user has already approved. Every line
is ONE visual, and every row you return is added to the page.

Strict rules for the list:
- Return exactly one visual per line, in the list's order, and set `line` to that line's
  number ("1", "2", ...).
- Never add a visual that is not on the list. Never merge two lines into one visual, and
  never split one line into two.
- Build what the line says. Where it names a column loosely ("sales"), use the real column;
  where it gives a style in brackets ("(horizontal bar)"), use that sub-type.
- If ONE line cannot be built with this catalog and these columns, leave out only that
  line and say why in `clarification`, naming its number. Build every other line.
- The list decides WHAT is on the page. Use the design rules below only for HOW: titles,
  sorting, formats and which row each visual sits on.

{_CORE_RULES}

{_DESIGN_RULES}"""


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

    `action` and `target` are what make a *round* (phase 35) an edit rather than a rewrite:
    a row says which existing visual it changes, and a visual no row names is left alone.
    On a first draft both are ignored - everything proposed is added.
    """

    action: str = Field(
        default=ACTION_ADD, description="add, update or remove. Only rounds use it"
    )
    target: str = Field(
        default="", description="For update/remove: the number of the visual to change"
    )
    visual_type: str = Field(..., description="filter, card, chart or table")
    sub_type: str = Field(default="", description="A sub-type allowed for that visual type")
    source_table: str = Field(default="", description="The table this visual reads")
    columns: str = Field(
        default="", description="Comma separated; the filter's column, or a table's columns"
    )
    measure_column: str = Field(default="", description="The number column to aggregate")
    more_measures: str = Field(
        default="",
        description="Other numbers this chart draws, as "
                    "'column:aggregation:left_or_right', separated by ';'. Example: "
                    "'Salary:minimum:left; Salary:maximum:left'",
    )
    aggregation: str = Field(default="", description="An aggregation from the catalog")
    group_by: str = Field(default="", description="The column the number is broken down by")
    colour_by: str = Field(default="", description="Optional second breakdown, for a legend")
    sort: str = Field(default="", description="A sort order from the catalog")
    top_n: str = Field(default="", description="How many categories to keep, digits only")
    title: str = Field(default="", description="The heading shown on the visual")
    row_number: str = Field(default="", description="Which row it sits on, digits only")
    properties: str = Field(
        default="",
        description="Optional look settings from the catalog, e.g. "
                    "'format:currency, labels:yes, size:tall'",
    )
    # Phase 45: which line of the user's checklist this visual is for. Text for the same
    # reason as `row_number`, and on the reply only - `PanelSpec` never carries it, so no
    # saved dashboard changes shape.
    line: str = Field(
        default="",
        description="Only when building from a numbered list: the list line this visual is "
                    "for, digits only",
    )


class ProposedChecklist(BaseModel):
    """The plan Generate shows before it builds (phase 45): one plain line per visual."""

    lines: list[str] = Field(
        default_factory=list,
        description="One line per visual, e.g. 'Card: Total Amount'",
    )
    clarification: str = Field(
        default="", description="Why no dashboard can be planned, if it can't"
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
    sub_labels.update(TABLE_LABELS)

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
        f"{key} ({label})" for key, label in DASHBOARD_SORT_LABELS.items()
    ))
    lines.append(describe_properties_for_prompt())
    return "\n".join(lines)


def describe_properties_for_prompt() -> str:
    """The `properties` vocabulary, as prompt text.

    Built from the same tuples `model.clean_properties` checks against, so a setting
    cannot be offered here and then dropped there - which is exactly the shape of the bug
    phase 38 was written for. A pie was asked for data labels, nothing in the vocabulary
    said what those were, and the round reported a change it had not made.
    """
    labellable = ", ".join(LABELLABLE_CHARTS)
    return (
        "Properties (optional, written as 'name:value' separated by commas. Anything "
        "not listed here is dropped):\n"
        f"    format: {', '.join(NUMBER_FORMATS)}; currency: "
        f"{', '.join(CURRENCY_CODES)}\n"
        f"    labels: yes or no - prints the number on the chart. Only on {labellable}; "
        "on a pie or donut it prints each slice's percentage of the whole.\n"
        f"    legend: {', '.join(LEGEND_POSITIONS)} - where the colour key goes\n"
        "    axis_titles: yes or no - the words under and beside the axes\n"
        f"    colour: {', '.join(NAMED_COLOURS)} - one colour, for a chart with no "
        "colour_by\n"
        f"    size: {', '.join(PANEL_SIZES)} - how tall a chart is drawn\n"
        f"    width: {', '.join(PANEL_WIDTHS)} - full takes the whole row\n"
        f"    card_size: {', '.join(CARD_SIZES)} - how big a card prints its number\n"
        "    row_count: yes or no - the 'Rows' column on a drill-down table, counting the "
        "rows behind each group's total. Only on a drill-down.\n"
        "    border: yes or no; radius: 0 to 2"
    )


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


def column_notes(entries, separator: str = COLUMN_SEPARATOR) -> dict[str, str]:
    """What each column *means*, from the data dictionary the user filled in during Setup.

    Requirement 5.3's descriptions and synonyms are already built for the chat agent and are
    simply not used here today. They are worth more to a generator than any amount of extra
    prose: `Amount` is a guess, `Amount - invoice value after discount, INR` is an
    instruction, and `[also called: units, qty]` is what lets "how many units" find the
    right column instead of the one that merely sounds right.

    `engine.dictionary.schema_context` cannot be reused as it stands, because it renders the
    *base* tables and this module sees the flattened ones, where `CustName` arrives as
    `Customer - CustName`. So a note is keyed on the **bare column name** and matched against
    either spelling by `_note_for`.

    Two tables carrying the same column name with different descriptions: the first wins and
    the clash is logged. That is the same call `payload.date_columns` makes for the same
    reason - the cost of being wrong here is a slightly-off hint, never a wrong chart, since
    every column the model names is checked against the real data afterwards either way.

    Args:
        entries: `engine.dictionary.ColumnEntry` records, or anything carrying the same
            `table` / `column` / `description` / `synonyms` attributes.
        separator: how `flatten` joins a master's name onto its column.

    Returns:
        `{bare column name: note}`, with columns carrying neither a description nor a synonym
        left out entirely - an empty note would only lengthen the prompt.
    """
    notes: dict[str, str] = {}
    for entry in entries or []:
        column = str(getattr(entry, "column", "") or "").strip()
        if not column:
            continue
        description = str(getattr(entry, "description", "") or "").strip()
        synonyms = [str(name).strip() for name in getattr(entry, "synonyms", None) or []]
        synonyms = [name for name in synonyms if name]
        if not description and not synonyms:
            continue

        also = f"[also called: {', '.join(synonyms)}]" if synonyms else ""
        note = " ".join(part for part in (description, also) if part)
        if column in notes:
            if notes[column] != note:
                logger.info(
                    "Two tables describe a column called %r differently; keeping the first.",
                    column,
                )
            continue
        notes[column] = note
    return notes


def _note_for(column: str, notes: dict[str, str]) -> str:
    """One flattened column's dictionary note, or an empty string.

    Tries the name as it stands first, then the part after the last separator - so both
    `Amount` and `Customer - CustName` find their entry.
    """
    if not notes:
        return ""
    if column in notes:
        return notes[column]
    if COLUMN_SEPARATOR in column:
        return notes.get(column.rsplit(COLUMN_SEPARATOR, 1)[-1].strip(), "")
    return ""


def describe_tables_for_prompt(tables: dict[str, pd.DataFrame],
                               notes: dict[str, str] | None = None) -> str:
    """The tables the dashboard can draw from, their columns, and each column's type.

    These are the *flattened* tables, so a parent's attributes already appear as columns of
    every table that reaches it - which is why the model never has to think about joins at
    all, and why the same `Customer - CustName` may be listed under more than one table.

    `notes` is `column_notes`' output. Left out, this renders exactly what it rendered before
    phase 35, so a session with an empty data dictionary loses nothing.
    """
    notes = notes or {}
    lines: list[str] = []
    for name, frame in tables.items():
        types = getattr(frame, "dtypes", {})
        columns = [str(column) for column in frame.columns]
        described = []
        for column in columns[:PROMPT_COLUMN_LIMIT]:
            shown_column = (
                f"{column} ({_type_word(types[column])})" if column in types else column
            )
            note = _note_for(column, notes)
            described.append(f"{shown_column} - {note}" if note else shown_column)
        shown = "; ".join(described)
        if len(columns) > PROMPT_COLUMN_LIMIT:
            shown += f"; ... ({len(columns) - PROMPT_COLUMN_LIMIT} more)"
        lines.append(f"- {name}: {shown or '(no columns)'}")
    return "\n".join(lines) or "(no tables available)"


#: Every key `model._panel_to_dict` writes, mapped to the name it is shown under when the
#: current dashboard is described back to the model - or `""` for the ones deliberately not
#: shown. The labels match `ProposedPanel`'s own field names, so a model editing a visual can
#: copy `label=value` straight across.
#:
#: A dict rather than a list of labels so that a field added to `PanelSpec` later has to be
#: *decided about*: `tests/test_live_dashboard_ai_spec.py` asserts the keys here are exactly
#: the keys written to JSON, and a new field silently missing from every round is the kind of
#: bug that takes a month to notice.
_SPEC_FIELD_LABELS: dict[str, str] = {
    # The panel id is ours; the number in the listing is the handle the model is given.
    "panel_id": "",
    # Written as the "kind / style" head of each line rather than as a labelled field.
    "visual_type": "",
    "sub_type": "",
    "source_table": "source_table",
    "source_columns": "columns",
    "measure_column": "measure_column",
    "extra_measures": "more_measures",
    "aggregation": "aggregation",
    "group_by": "group_by",
    "colour_by": "colour_by",
    "sort": "sort",
    "top_n": "top_n",
    "title": "title",
    "properties": "properties",
    "row_number": "row_number",
}


#: Which labelled fields each kind of visual actually uses. A card carrying `sort=largest`
#: is noise in a prompt phase 34 already flagged as crowded - and worse, it invites a round
#: to "fix" a field that changes nothing.
_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    VISUAL_FILTER: frozenset({"source_table", "columns", "title"}),
    VISUAL_TABLE: frozenset({"source_table", "columns", "title", "row_number",
                             "properties"}),
    VISUAL_CARD: frozenset({"source_table", "measure_column", "aggregation", "title",
                            "row_number", "properties"}),
}

#: What a drill-down table adds to a plain table's fields: the number it totals on every
#: level. A flat table keeps the shorter list - `aggregation` defaults to "sum" on every
#: panel whether or not anything totals, so printing it on a flat table would advertise a
#: field that changes nothing and invite a round to "fix" it.
#: `more_measures` is here too since phase 41: a round that cannot see the eight totals a
#: drill-down already has would replace them with the one it was asked to add.
_DRILLDOWN_EXTRA_FIELDS = frozenset({"measure_column", "aggregation", "more_measures"})


def _panel_fields_for_prompt(panel: PanelSpec) -> str:
    allowed = _FIELDS_BY_KIND.get(panel.visual_type)
    if panel.visual_type == VISUAL_TABLE and panel.sub_type == TABLE_DRILLDOWN:
        allowed = allowed | _DRILLDOWN_EXTRA_FIELDS
    parts: list[str] = []
    for name, label in _SPEC_FIELD_LABELS.items():
        if not label:
            continue
        if allowed is not None and label not in allowed:
            continue
        value = getattr(panel, name, "")
        if name == "source_columns":
            text = ", ".join(str(item) for item in value)
        elif name == "properties":
            text = properties_text(value)
        elif name == "extra_measures":
            # The same one line the model writes them on. A dict printed raw would teach the
            # next round to answer in dicts, which the flat text field cannot carry.
            text = measures_text(value)
        elif name == "top_n":
            text = str(value) if value else ""
        else:
            text = str(value or "")
        if text:
            parts.append(f"{label}={text}")
    return " | ".join(parts)


def describe_spec_for_prompt(spec: DashboardSpec) -> str:
    """The dashboard as it stands, numbered, one line of fields per visual.

    **This is the model's entire memory of the conversation.** `llm.client.run_structured` is
    deliberately stateless, so each round is told what the page looks like rather than what
    was said about it. A chat history would drift from the page the moment the user edited a
    visual by hand; the page cannot disagree with itself.

    Field-shaped rather than the prose `describe_panel` writes, and the difference is not
    cosmetic: a round has to be able to *re-state* a visual it is editing, and "bar chart of
    Sum of Amount by Month" does not round-trip into `measure_column`, `aggregation` and
    `group_by`. `describe_panel` stays what it is - the sentence a human reads afterwards.

    The numbers are positions in `spec.panels`, which is what `_target_panel` resolves
    `target` against, so the two cannot disagree about which visual is number 3.
    """
    header = (
        f"Dashboard title: {spec.title or '(none)'} | "
        f"subtitle: {spec.subtitle or '(none)'} | "
        f"filter_position: {spec.filter_position}"
    )
    if not spec.panels:
        return f"{header}\nVisuals on it now: (none yet)"

    lines = [header, "Visuals on it now:"]
    for number, panel in enumerate(spec.panels, start=1):
        head = f"{number}. {panel.visual_type} / {panel.sub_type}"
        fields = _panel_fields_for_prompt(panel)
        lines.append(f"{head} | {fields}" if fields else head)
    return "\n".join(lines)


#: How the flattened tables relate to each other, said once in the prompt (phase 39).
#:
#: Every table is embedded carrying the columns of the parents it can reach, under the
#: same `Parent - Column` spelling on each. That is the one fact a model needs in order
#: to stop asking for a single chart over two unrelated tables, and to know that one
#: filter is enough for all of them.
_RELATED_TABLES_NOTE = (
    "How these tables relate:\n"
    "- A column named like 'Employee Master - Department' belongs to a related table "
    "and is already carried on this one. Use it exactly as you would any other column "
    "of the table you found it on.\n"
    "- The same related column appears on every table it reaches, so ONE filter on it "
    "narrows all of them at once. Never add the same filter twice for different "
    "tables.\n"
    "- One visual reads ONE table. Two numbers that live on different tables (a salary "
    "and an attendance count) are two visuals, not one chart - and both still react to "
    "the same filter."
)


def build_prompt(instruction: str, tables: dict[str, pd.DataFrame], *,
                 notes: dict[str, str] | None = None,
                 current: DashboardSpec | None = None) -> str:
    """The whole ask: the schema, the catalog, the page so far, and the request.

    `current` is what turns a first draft into a round: the dashboard as it stands is
    described in full, and the request that follows is read as a change to it rather than as
    a page of its own.
    """
    parts = [
        "Tables available, with their columns:\n"
        + describe_tables_for_prompt(tables, notes),
        "",
        _RELATED_TABLES_NOTE,
        "",
        "Visual catalog:\n" + describe_catalog_for_prompt(),
        "",
    ]

    if current is not None:
        parts.append("The dashboard as it stands:\n" + describe_spec_for_prompt(current))
        parts.append("")
        parts.append("What to change:\n" + instruction.strip())
    else:
        parts.append("Dashboard to build:\n" + instruction.strip())

    return "\n".join(parts)


# --------------------------------------------------------------------------------------
# Reading one proposal back
# --------------------------------------------------------------------------------------


def _extra_measures(proposed: "ProposedPanel", known_columns: dict[str, str]) -> list[dict]:
    """`"Salary:minimum:left; Headcount:count:right"` as the list the panel keeps.

    One flat text field rather than a list of objects, for the reason every other field
    here is text: a provider's strict structured output handles nested lists badly, and a
    schema a provider fills in wrongly is worse than a string this parses itself.

    Whatever cannot be read is simply left out. `clean_measures` is the gate that decides
    what a measure may be at all, and `panel_problems` afterwards decides whether the
    columns named really exist - so nothing invented here reaches a chart.
    """
    entries = []
    for chunk in str(proposed.more_measures or "").split(";"):
        parts = [piece.strip() for piece in chunk.split(":")]
        parts += [""] * (3 - len(parts))  # a line that named only a column still reads
        column, total, axis = parts[0], _key(parts[1]), parts[2].lower()
        if not any(parts):
            continue
        entries.append({
            # "min" and "avg" are the spellings a model reaches for, and the same synonym
            # table the main aggregation goes through maps them onto the catalog.
            "column": _match(column, known_columns) if column else "",
            "aggregation": _AGGREGATION_SYNONYMS.get(total, total),
            "axis": axis or AXIS_LEFT,
        })
    return clean_measures(entries)


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


def _prefer_prefixed(column: str, table: str, known_columns: dict[str, str]) -> str:
    """A filter narrows by the prefixed spelling whenever the table offers one.

    A parent table carries every column twice (phase 39's `_own_prefixed_columns`): its own
    plain name, which only that table understands, and `Table - Column`, which every child
    joined to it also carries. A filter built on the plain name looked fine in preview but
    quietly stopped at the parent - this is what stops that mismatch from ever reaching the
    page, rather than relying on the model to pick the right spelling every time.
    """
    if not column or COLUMN_SEPARATOR in column:
        return column
    prefixed_key = _key(f"{table}{COLUMN_SEPARATOR}{column}")
    return known_columns.get(prefixed_key, column)


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


def _resolve_sub_type(proposed: ProposedPanel, visual_type: str) -> tuple[str, str | None]:
    """The sub-type, plus a sentence about it when there is one.

    Returns `(sub_type, message)` - the same convention `_build_one` uses, so the two agree:
    a blank sub-type means the message is a refusal, and a filled one means it is a note the
    user should read anyway. A blank *proposal* is filled in with the visual type's default;
    the model leaving a field out of an otherwise good row is not a reason to lose the row.

    A *named* shape we can't draw is the interesting case. It used to drop the row. Now
    `_SHAPE_FALLBACKS` maps most of them onto the nearest shape we can draw and the visual is
    still built - but never silently: the swap comes back as a note the user reads, so a pie
    where a treemap was asked for is explained rather than discovered.
    """
    allowed = sub_types_for(visual_type)
    wanted = _key(proposed.sub_type)
    if not wanted:
        return default_sub_type(visual_type), None
    for name in allowed:
        if _key(name) == wanted:
            return name, None
    labels = {**FILTER_LABELS, **DASHBOARD_CHART_LABELS, **TABLE_LABELS}
    for name in allowed:
        if _key(labels.get(name, "")) == wanted:
            return name, None

    instead = _SHAPE_FALLBACKS.get(wanted) if visual_type == VISUAL_CHART else None
    if instead and instead in allowed:
        title = str(proposed.title or "").strip() or "A visual"
        return instead, (
            f"'{title}' asked for a {proposed.sub_type.strip()}. This dashboard can't draw "
            f"one yet, so it's a {DASHBOARD_CHART_LABELS.get(instead, instead).lower()} "
            "instead - change it in Edit if you'd rather have something else."
        )

    return "", (
        f"Skipped a {VISUAL_LABELS.get(visual_type, visual_type).lower()}: "
        f"'{proposed.sub_type}' isn't one of its styles."
    )


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
    if wanted in DASHBOARD_SORT_LABELS:
        return wanted
    for key, label in DASHBOARD_SORT_LABELS.items():
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


def _columns_named(proposed: ProposedPanel) -> list[str]:
    """Every column one proposal names, whatever kind of visual it is."""
    named = _split(proposed.columns)
    for value in (proposed.measure_column, proposed.group_by, proposed.colour_by):
        text = str(value or "").strip()
        if text:
            named.append(text)
    return named


def _table_for(proposed: ProposedPanel, tables: dict[str, pd.DataFrame]) -> str:
    """Which table a visual reads, when the proposal's own answer is not one we have.

    There is no "main table" to fall back on since phase 39 - every table is embedded, each
    carrying the columns of the parents it reaches, so the honest question is no longer
    "which table is the page about" but **"which table has the columns this visual names"**.
    A card of `Amount` broken down by `Employee Master - Department` belongs on Salary
    because that is where both of those columns are.

    Only for a proposal that named **no** table. A proposal naming a table we do not have
    is returned unchanged so the check that follows refuses it by name: a wrong table is a
    guess about what the user meant, and quietly redirecting it to whichever table happens
    to carry columns of those names is precisely the silent wrong answer the whole module
    is built to avoid.
    """
    known_tables = {_key(name): name for name in tables}
    named = _match(proposed.source_table, known_tables)
    if named:
        return named

    wanted = [_key(column) for column in _columns_named(proposed)]
    if wanted:
        for name, frame in tables.items():
            have = {_key(column) for column in frame.columns}
            if all(column in have for column in wanted):
                return name

    return named or next(iter(tables), "")


def _build_one(
    proposed: ProposedPanel,
    tables: dict[str, pd.DataFrame],
    available_columns: dict[str, list[str]],
    date_columns: frozenset[str],
) -> tuple[PanelSpec | None, str | None]:
    """Turns one proposal into a real, checked panel, or says why it can't be.

    Returns `(panel, warning)`. A warning with no panel means the row was dropped and the
    sentence says why; a warning *with* a panel is a note about something that was changed to
    make the row drawable - a shape swapped for the nearest one we have. Both are shown in
    the same list, because both are things the user would rather know than discover.

    `available_columns` and `date_columns` are passed in rather than derived from `tables`
    here: they are the same for every proposal in one answer, and this runs once per visual.

    Everything that can go wrong here is the model's fault rather than the user's, so each
    failure becomes a sentence - never an exception the dialog would have to catch.
    """
    visual_type, problem = _resolve_visual_type(proposed)
    if problem:
        return None, problem

    sub_type, note = _resolve_sub_type(proposed, visual_type)
    if not sub_type:
        return None, note

    table = _table_for(proposed, tables)
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
        source_columns=[
            _prefer_prefixed(_match(name, known_columns), table, known_columns)
            if visual_type == VISUAL_FILTER
            else _match(name, known_columns)
            for name in _split(proposed.columns)
        ],
        measure_column=_match(proposed.measure_column, known_columns),
        extra_measures=_extra_measures(proposed, known_columns),
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
    trouble = panel_problems(panel, available_columns, date_columns)
    if trouble:
        return None, f"Skipped '{panel.display_title()}': {trouble}"

    # A setting that was understood but cannot apply to this shape - labels on a heatmap,
    # one colour over a chart already split by colour. The visual is kept and drawn; the
    # sentence is what stops the setting failing silently.
    unusable = property_problems(panel)
    if unusable:
        note = f"{note} {unusable}".strip() if note else unusable

    # The likely model error, and an invisible one: "sales by customer" coming back with the
    # two the wrong way round. The types are already in the prompt, so this is cheap.
    #
    # Counting *different* values is the exception, and an important one: "how many
    # customers" is the usual question and customers are text. Refusing it here would refuse
    # the most obvious use of the whole aggregation.
    # A drill-down table totals a number at every level, so it is in this check for the
    # same reason a card is: summed text is a silent column of zeroes, not a visible error.
    totals_a_number = visual_type in (VISUAL_CARD, VISUAL_CHART) or sub_type == TABLE_DRILLDOWN
    if (totals_a_number
            and aggregation not in TEXT_FRIENDLY_AGGREGATIONS
            and panel.measure_column not in _numeric_columns(frame)):
        return None, (
            f"Skipped '{panel.display_title()}': '{panel.measure_column}' isn't a number, so "
            "it can't be totalled. Count unique would work over it if you meant to count it."
        )

    # A drill-down's extra totals are columns of numbers exactly like its first one, so a
    # text column among them is the same silent column of zeroes - eight times over.
    if sub_type == TABLE_DRILLDOWN:
        numeric = _numeric_columns(frame)
        for one in panel.extra_measures:
            column = str(one.get("column") or "")
            if one.get("aggregation") in TEXT_FRIENDLY_AGGREGATIONS:
                continue
            if column not in numeric:
                return None, (
                    f"Skipped '{panel.display_title()}': '{column}' isn't a number, so it "
                    "can't be one of this table's totals."
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
    notes: dict[str, str] | None = None,
    key_path=None,
) -> tuple[DashboardSpec | None, list[str], str | None]:
    """Asks the Light Model to turn `instruction` into a dashboard spec.

    Args:
        profile: the Light Model profile to call.
        instruction: what the user typed.
        tables: the embedded tables, keyed by the name panels refer to them by - the same
            dict the exporter is handed, so the columns checked here are the columns that
            will exist.
        notes: `column_notes`' output - what each column means, from the Setup dictionary.

    Returns:
        `(spec, warnings, clarification)`. `spec` is None when nothing usable came back. The
        warnings name every proposal that was dropped and why, so the page can say "3 of 5
        understood" rather than quietly returning fewer visuals than were asked for.

    Never raises: a model that is unreachable or talking nonsense comes back as `None` and
    one warning, with the dashboard on screen untouched.
    """
    if not instruction.strip():
        return None, ["Type what you want the dashboard to show first."], None
    if not tables:
        return None, ["There's no data loaded to build a dashboard from yet."], None

    try:
        response = run_structured(
            profile,
            build_prompt(instruction, tables, notes=notes),
            ProposedDashboard,
            instructions=_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("The dashboard could not be generated: %s", error)
        return None, [f"We couldn't read that description: {error}"], None

    warnings: list[str] = []
    panels: list[PanelSpec] = []

    # Both depend only on `tables`, which nothing in the loop changes - so they are read
    # once rather than rebuilt for each of the ten proposals.
    available_columns = {name: [str(column) for column in frame.columns]
                         for name, frame in tables.items()}
    date_columns = payload.date_columns(tables)

    for proposed in response.panels:
        if len(panels) >= MAX_PANELS:
            warnings.append(
                f"Only the first {MAX_PANELS} visuals were kept. Ask again for the rest."
            )
            break
        panel, warning = _build_one(
            proposed, tables, available_columns, date_columns
        )
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
        filter_position=position if position in FILTER_POSITIONS
        else DEFAULT_FILTER_POSITION,
        panels=panels,
    )
    return spec, warnings, clarification


# --------------------------------------------------------------------------------------
# Rounds (phase 35)
# --------------------------------------------------------------------------------------


@dataclass
class RoundResult:
    """What one round of editing did, in the shape the page needs to report it.

    Counts and sentences together, so the view can write "Added 1 visual, changed 1" without
    re-deriving anything from the spec - and so a test can assert what a round *did* rather
    than diffing two dashboards.

    Attributes:
        added / updated: the panels as they ended up, for `describe_panel` to write out.
        removed: the titles of the visuals that went, since the panels themselves are gone.
        notes: every sentence the user should read - a shape swapped for the nearest one we
            can draw, an edit pointing at a visual that isn't there, a proposal refused for
            naming a column the data doesn't have. Notes are not failures; `failed` is.
        clarification: the model's own reason the request couldn't be done, when it gave one.
        failed: nothing was attempted at all - no model configured, no data, an unreachable
            provider, or a blank box that needed words. The dashboard is untouched.
    """

    added: list[PanelSpec] = field(default_factory=list)
    updated: list[PanelSpec] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clarification: str | None = None
    failed: bool = False

    def changed(self) -> bool:
        """Whether the dashboard is different because of this round."""
        return bool(self.added or self.updated or self.removed)

    def summary(self) -> str:
        """The one line the page prints above the notes."""
        parts: list[str] = []
        if self.added:
            parts.append(f"added {len(self.added)} visual(s)")
        if self.updated:
            parts.append(f"changed {len(self.updated)}")
        if self.removed:
            parts.append(f"removed {len(self.removed)}")
        if not parts:
            return "Nothing on the dashboard changed"
        sentence = ", ".join(parts)
        return sentence[0].upper() + sentence[1:]


def _target_panel(target: str, panels: list[PanelSpec]) -> PanelSpec | None:
    """The visual an edit points at, by its number in `describe_spec_for_prompt`'s listing.

    None for anything that isn't a number in range - which is reported as a skipped edit
    rather than guessed at, because guessing here edits the wrong chart.
    """
    raw = str(target or "").strip()
    if not raw:
        return None
    try:
        number = int(float(raw))
    except (TypeError, ValueError):
        return None
    if 1 <= number <= len(panels):
        return panels[number - 1]
    return None


def _apply_settings(spec: DashboardSpec, response: "ProposedDashboard",
                    result: RoundResult) -> None:
    """Applies the page-level settings a round asked to change, and only those.

    Blank means "not asked about" here, unlike on a first draft where the model writes them
    every time. Without that rule a round adding one chart would blank a title the user
    typed themselves.
    """
    title = str(response.title or "").strip()
    if title and title != spec.title:
        spec.title = title
        result.notes.append(f"Renamed the dashboard to '{title}'.")

    subtitle = str(response.subtitle or "").strip()
    if subtitle and subtitle != spec.subtitle:
        spec.subtitle = subtitle

    position = _key(response.filter_position)
    if position in FILTER_POSITIONS and position != spec.filter_position:
        spec.filter_position = position
        result.notes.append(f"Moved the filters to the {position}.")


def _first_draft(profile: dict, instruction: str, tables: dict[str, pd.DataFrame],
                 spec: DashboardSpec, *, notes: dict[str, str] | None,
                 key_path) -> RoundResult:
    """The opening round, on a dashboard with nothing on it yet.

    `propose_dashboard` is reused unchanged rather than reimplemented: with no visuals there
    is nothing to edit, so "everything proposed is added" is exactly right, and `_lay_out`
    needs to run - which it must not do on any later round.
    """
    proposed, warnings_out, clarification = propose_dashboard(
        profile, instruction, tables, notes=notes, key_path=key_path,
    )
    result = RoundResult(notes=list(warnings_out), clarification=clarification)
    if proposed is None or not proposed.panels:
        result.failed = True
        return result

    spec.panels = list(proposed.panels)
    spec.title = proposed.title or spec.title
    spec.subtitle = proposed.subtitle or spec.subtitle
    spec.filter_position = proposed.filter_position
    result.added = list(proposed.panels)
    return result


def revise_dashboard(
    profile: dict,
    instruction: str,
    tables: dict[str, pd.DataFrame],
    spec: DashboardSpec,
    *,
    notes: dict[str, str] | None = None,
    focus: PanelSpec | None = None,
    key_path=None,
) -> RoundResult:
    """One round of the conversation: reads the instruction and edits `spec` in place.

    This is the whole of phase 35's user flow - type, see it, say what to change, see it
    again. The dashboard is the only memory: `describe_spec_for_prompt` sends the page as it
    stands with every request, so there is no transcript to drift out of step with it.

    What makes this an *edit* rather than a rewrite is mechanical, not a matter of asking the
    model nicely: a proposal names an `action` and, for `update` and `remove`, the number of
    the visual it means. A visual no proposal names is not touched, so it cannot be lost to a
    model that simply forgot to write it out again.

    Args:
        profile: the Light Model profile to call.
        instruction: what the user typed. Blank is allowed **only** on an empty dashboard,
            where it means `DEFAULT_INSTRUCTION`; with visuals already there it is refused,
            because "add something" and "start over" are not the same request.
        tables: the embedded tables, keyed as panels refer to them.
        spec: the dashboard being edited. Mutated only once a round has produced a change,
            so a failed or empty round leaves the page exactly as the user left it.
        notes: `column_notes`' output, so the model knows what the columns mean.
        focus: one visual the round is confined to - what the Edit button beside a visual
            passes. The model is told which number it may change, and every edit it returns
            is pointed at that visual here regardless of what it wrote, so "make it
            horizontal" typed under one chart can never reshape another.

    Returns:
        A `RoundResult`. Never raises: an unreachable model is a `failed` result carrying one
        sentence, with the dashboard and its download still on screen.
    """
    text = str(instruction or "").strip()

    if not tables:
        return RoundResult(
            failed=True,
            notes=["There's no data loaded to build a dashboard from yet."],
        )

    if not spec.panels:
        return _first_draft(
            profile, text or DEFAULT_INSTRUCTION, tables, spec,
            notes=notes, key_path=key_path,
        )

    if not text:
        return RoundResult(
            failed=True,
            notes=["Say what you'd like changed - for example 'make the region chart "
                   "horizontal' or 'add a monthly trend'."],
        )

    asked = text
    if focus is not None:
        if focus not in spec.panels:
            return RoundResult(
                failed=True,
                notes=["That visual is no longer on the dashboard."],
            )
        number = spec.panels.index(focus) + 1
        asked = (
            f"Change only visual number {number} ('{focus.display_title()}'). Return exactly "
            f"one edit, with action 'update' and target '{number}' - or action 'remove' with "
            f"that target if the user asks for it to go. Leave every other visual alone. "
            f"The user says: {text}"
        )

    try:
        response = run_structured(
            profile,
            build_prompt(asked, tables, notes=notes, current=spec),
            ProposedDashboard,
            instructions=_ROUND_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("The dashboard could not be changed: %s", error)
        return RoundResult(failed=True, notes=[f"We couldn't make that change: {error}"])

    result = RoundResult(clarification=str(response.clarification or "").strip() or None)

    # Both depend only on `tables`, which nothing in the loop changes.
    available_columns = {name: [str(column) for column in frame.columns]
                         for name, frame in tables.items()}
    date_columns = payload.date_columns(tables)

    # Edited on a copy, and swapped in at the end. A round that produces nothing usable must
    # leave the page alone rather than half-apply itself.
    working = list(spec.panels)

    for proposed in response.panels:
        if focus is not None and result.changed():
            # One visual was opened, so one visual is changed. A model that answered with
            # three edits is describing a page the user did not ask about.
            result.notes.append(
                "Only the visual you opened was changed. Use the box at the top to change "
                "the rest of the dashboard."
            )
            break

        action = _key(proposed.action) or ACTION_ADD
        if action not in ACTIONS:
            result.notes.append(
                f"Skipped an edit: '{proposed.action}' isn't something that can be done to "
                "a visual."
            )
            continue

        if focus is not None:
            # The user pressed Edit on one visual, so that is the one this round may touch.
            # The model's own `target` is not trusted here - it is answering about a page it
            # can see all of, and an off-by-one would edit the chart next to the one the
            # user was looking at.
            target = focus
            if action == ACTION_ADD:
                action = ACTION_UPDATE
        else:
            target = None
        if target is None and action in (ACTION_UPDATE, ACTION_REMOVE):
            target = _target_panel(proposed.target, spec.panels)
            if target is None:
                result.notes.append(
                    f"Skipped an edit: there's no visual number '{proposed.target}' on this "
                    "dashboard."
                )
                continue

        if action == ACTION_REMOVE:
            if target in working:
                working.remove(target)
                result.removed.append(target.display_title())
            continue

        if action == ACTION_ADD and len(working) >= MAX_PANELS:
            result.notes.append(
                f"This dashboard already has {MAX_PANELS} visuals. Remove one before adding "
                "another."
            )
            continue

        panel, warning = _build_one(
            proposed, tables, available_columns, date_columns
        )
        if panel is None:
            result.notes.append(warning or "A change couldn't be understood.")
            continue
        if warning:
            # A note, not a refusal: the visual is kept and the change is explained.
            result.notes.append(warning)

        if action == ACTION_UPDATE and target is not None:
            # The id stays the target's. It is the element id in the exported page and the
            # key of every widget in the edit dialog, so churning it each round would reset
            # the user's selection and break a link a reader had already bookmarked.
            panel.panel_id = target.panel_id
            if not str(proposed.row_number or "").strip():
                # Nothing was said about where it sits, so it stays where it was rather than
                # jumping to row 1 on the strength of a field the model left out.
                panel.row_number = target.row_number
            if panel == target:
                # Every field matches, so nothing would change. Counting it would report
                # "Changed 1 ..." for a request the catalog has no setting for - the model
                # re-sends the same visual when it can't do what was asked.
                result.notes.append(
                    f"That didn't change '{target.display_title()}'. It may not be something "
                    "the dashboard can do yet - open 'What can I ask?' to see what can."
                )
                continue
            if target not in working:
                # An earlier edit in this same round already replaced or removed it, so
                # there is nothing left to swap. Two edits to one visual is a model
                # answering twice; the first one stands.
                result.notes.append(
                    f"Skipped a second change to '{target.display_title()}' - it was "
                    "already changed in this round."
                )
                continue
            working[working.index(target)] = panel
            result.updated.append(panel)
        else:
            working.append(panel)
            result.added.append(panel)

    if not result.changed():
        if not result.notes and not result.clarification:
            result.notes.append(
                "Nothing on the dashboard changed. Try naming the visual you mean by the "
                "title shown above it on the dashboard."
            )
        return result

    # `_lay_out` is deliberately not re-run. It exists to rescue a first draft where every
    # visual came back on row 1; re-running it would shuffle a page the user has accepted.
    spec.panels = working
    _apply_settings(spec, response, result)

    logger.info(
        "A dashboard round added %d, changed %d, removed %d visual(s), %d note(s).",
        len(result.added), len(result.updated), len(result.removed), len(result.notes),
    )
    return result


# --------------------------------------------------------------------------------------
# The checklist Generate asks about first (phase 45)
# --------------------------------------------------------------------------------------

#: A bullet or a number someone typed at the start of a line: "- ", "* ", "3. ", "3) ".
_LINE_MARKER = re.compile(r"^\s*(?:[-*•]+|\d+\s*[.)])\s*")


def checklist_lines(text) -> list[str]:
    """The lines of a checklist, cleaned: one per visual, blanks and bullets gone.

    Takes the text box's contents or a list of lines, because the same cleaning applies to
    what the model wrote and to what the user typed - "1. Card: Total" and "- Card: Total"
    are both one line, and the numbers are put back when the list is sent to be built.
    """
    raw = text.splitlines() if isinstance(text, str) else [str(one) for one in text or []]
    lines = []
    for one in raw:
        cleaned = _LINE_MARKER.sub("", str(one)).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def draft_checklist(
    profile: dict,
    tables: dict[str, pd.DataFrame],
    *,
    notes: dict[str, str] | None = None,
    wishes: str = "",
    key_path=None,
) -> tuple[list[str], list[str]]:
    """Plans a dashboard as a short list of lines, and builds nothing.

    Step one of Generate since phase 45. The user reads the list, edits it, and only then is
    anything built - so a wrong guess costs a line of text rather than a page.

    Args:
        profile: the model to ask - the Light Model where there is one.
        tables: the embedded tables, the same dict the build will be checked against.
        notes: `column_notes`' output, so the plan uses what the columns mean.
        wishes: the "Anything else?" box - preferences that are not a visual.

    Returns:
        `(lines, messages)`. `lines` is empty when nothing came back, and `messages` then
        says why, so the dialog can invite the user to write their own. Never raises.
    """
    if not tables:
        return [], ["There's no data loaded to build a dashboard from yet."]

    asked = DEFAULT_INSTRUCTION
    if str(wishes or "").strip():
        asked += f"\nThe user also says: {wishes.strip()}"

    try:
        response = run_structured(
            profile,
            build_prompt(asked, tables, notes=notes),
            ProposedChecklist,
            instructions=_CHECKLIST_DRAFT_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("The dashboard checklist could not be drafted: %s", error)
        return [], [f"We couldn't draft a list: {error} Write your own lines below, or "
                    "press Skip the list to let the AI decide."]

    lines = checklist_lines(response.lines)
    messages: list[str] = []
    if len(lines) > MAX_PANELS:
        lines = lines[:MAX_PANELS]
        messages.append(f"Only the first {MAX_PANELS} lines were kept.")
    if not lines:
        messages.append(
            str(response.clarification or "").strip()
            or "The AI didn't suggest anything. Write your own lines below."
        )

    logger.info("Drafted a dashboard checklist of %d line(s).", len(lines))
    return lines, messages


def _checklist_instruction(lines: list[str], wishes: str) -> str:
    """The approved list, numbered, as the request the build prompt ends with."""
    numbered = "\n".join(f"{number}. {text}" for number, text in enumerate(lines, start=1))
    asked = f"Build exactly these {len(lines)} visual(s), one per line:\n{numbered}"
    if str(wishes or "").strip():
        asked += ("\n\nAlso keep this in mind for the whole page (it adds no visual): "
                  f"{wishes.strip()}")
    return asked


def build_from_checklist(
    profile: dict,
    lines: list[str],
    tables: dict[str, pd.DataFrame],
    spec: DashboardSpec,
    *,
    notes: dict[str, str] | None = None,
    wishes: str = "",
    key_path=None,
) -> RoundResult:
    """Builds exactly the lines the user approved - one visual each, nothing extra.

    Step two of Generate since phase 45. "Strict" is enforced here rather than hoped for:
    every visual the model returns names the line it is for, so a visual naming no line (or
    a line that already has one) is dropped with a sentence, and a line with no visual
    behind it is reported by its number with the reason the checker gave - "Line 4 not
    built (Card: Total bonus): ... there is no column called 'Bonus'".

    The column check happens here, after the model has answered, rather than on the text
    beforehand: "Card: Headcount" names no column and is still a perfectly good line, so
    guessing columns from free text would raise false alarms.

    Mutates `spec` only when at least one line was built, like `revise_dashboard`, so a
    failed build leaves the page as it was. Never raises.
    """
    lines = checklist_lines(lines)
    if not tables:
        return RoundResult(
            failed=True, notes=["There's no data loaded to build a dashboard from yet."],
        )
    if not lines:
        return RoundResult(
            failed=True,
            notes=["Write at least one line - for example 'Card: Total Amount' - or press "
                   "Skip the list to let the AI decide."],
        )
    if len(lines) > MAX_PANELS:
        return RoundResult(
            failed=True,
            notes=[f"That list has {len(lines)} lines. A dashboard holds at most "
                   f"{MAX_PANELS} visuals - remove a few and build again."],
        )

    try:
        response = run_structured(
            profile,
            build_prompt(_checklist_instruction(lines, wishes), tables, notes=notes),
            ProposedDashboard,
            instructions=_CHECKLIST_BUILD_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("The dashboard could not be built from the checklist: %s", error)
        return RoundResult(failed=True, notes=[f"We couldn't build the dashboard: {error}"])

    available_columns = {name: [str(column) for column in frame.columns]
                         for name, frame in tables.items()}
    date_columns = payload.date_columns(tables)

    built: dict[int, PanelSpec] = {}
    reasons: dict[int, str] = {}
    extra_notes: list[str] = []
    for proposed in response.panels:
        number, _ = _whole_number(proposed.line, default=0, minimum=0)
        title = str(proposed.title or "").strip() or "a visual"
        if not 1 <= number <= len(lines):
            extra_notes.append(f"Dropped '{title}': it isn't on your list.")
            continue
        if number in built:
            extra_notes.append(
                f"Dropped '{title}': line {number} already has its visual - one per line."
            )
            continue
        panel, warning = _build_one(proposed, tables, available_columns, date_columns)
        if panel is None:
            reasons.setdefault(number, warning or "it couldn't be understood.")
            continue
        if warning:
            extra_notes.append(f"Line {number}: {warning}")
        built[number] = panel

    # The lines that were not built come first: they are what the user asked for and did
    # not get, which matters more than a visual the model added on its own.
    missing = [
        f"Line {number} not built ({text}): "
        + reasons.get(number, "the AI gave no visual for it.")
        for number, text in enumerate(lines, start=1) if number not in built
    ]
    result = RoundResult(
        notes=missing + extra_notes,
        clarification=str(response.clarification or "").strip() or None,
    )

    panels = [built[number] for number in sorted(built)]
    if not panels:
        result.failed = True
        return result

    _lay_out(panels)
    spec.panels = panels
    spec.title = str(response.title or "").strip() or spec.title
    spec.subtitle = str(response.subtitle or "").strip() or spec.subtitle
    position = _key(response.filter_position)
    spec.filter_position = (position if position in FILTER_POSITIONS
                            else DEFAULT_FILTER_POSITION)
    result.added = panels

    logger.info("Built %d of %d checklist line(s), %d note(s).", len(panels), len(lines),
                len(result.notes))
    return result


def describe_panel(panel: PanelSpec) -> str:
    """One proposed visual in the words the user reads before accepting it.

    A whole sentence, because since phase 36 this is the only description of a visual the
    user gets: the spec table that used to list them column by column is gone, and what is
    left is the round's own account of what it changed.
    """
    if panel.is_filter():
        style = FILTER_LABELS.get(panel.sub_type, panel.sub_type)
        return f"**{panel.display_title()}** - {style} on {panel.filter_column()}"

    if panel.visual_type == VISUAL_TABLE:
        if panel.sub_type == TABLE_DRILLDOWN:
            # Named level by level with arrows, because the ORDER is the whole visual: the
            # same three columns the other way round is a different table to read.
            total = DASHBOARD_AGGREGATIONS.get(panel.aggregation, panel.aggregation)
            what = "rows" if panel.aggregation == AGG_COUNT else panel.measure_column
            levels = " -> ".join(panel.source_columns) or "no levels yet"
            if panel.extra_measures:
                # Named one by one, like a chart's: the user asked for five totals, and
                # "and 4 more" would not tell them whether they got the five they meant.
                others = ", ".join(vega_spec.measure_label(one)
                                   for one in panel.extra_measures)
                what = f"{what}, with {others}"
            return (f"**{panel.display_title()}** - drill-down table of {total} of {what} "
                    f"by {levels} - row {panel.row_number}")
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
    if panel.extra_measures:
        # Named one by one rather than counted: "and 2 more" tells the user nothing about
        # whether the chart they asked for is the chart they are about to get.
        others = ", ".join(vega_spec.measure_label(one) for one in panel.extra_measures)
        what = f"{what}, with {others}"
    if panel.colour_by:
        breakdown += f", split by {panel.colour_by}"
    return (f"**{panel.display_title()}** - {style.lower()} of {total} of {what}{breakdown}"
            f" - row {panel.row_number}")
