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
    SORT_LABELS,
    SORT_LARGEST,
)
from live_dashboard import payload
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
    DEFAULT_CURRENCY,
    DashboardSpec,
    FILTER_LABELS,
    FILTER_POSITIONS,
    FILTER_TOP,
    FORMAT_CURRENCY,
    PanelSpec,
    VISUAL_CARD,
    VISUAL_CHART,
    VISUAL_LABELS,
    VISUAL_TABLE,
    VISUAL_TYPES,
    clean_properties,
    default_sub_type,
    panel_problems,
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
- Some sub-types need one more field, and are dropped without it:
  bar_stacked, bar_grouped and heatmap need `colour_by` (the second breakdown);
  combo needs `measure_column_2` (the number drawn as a line over the bars);
  histogram needs `measure_column` and NO `group_by` - it shows one number's spread.
- "percent_of_total" only works on a chart, never a card. "running_total" only works on a
  chart broken down by a DATE column.
- row_number lays the page out: visuals sharing a number sit side by side. Filters ignore
  row_number - say where they go with filter_position ("top" or "left").
- If the request cannot be done with this catalog and these columns, return an EMPTY panels
  list and say why in one plain sentence in `clarification`. Never return a visual that is
  merely close - a wrong chart is worse than an honest no."""

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
- Propose at most 8 visuals unless the user asks for more.
- Sort a bar chart "largest" so the biggest bar comes first, unless it is broken down by a
  date, where "automatic" keeps the dates in order.
- Use "bar_horizontal" when the breakdown is long text: customer, product and employee
  names overlap badly on an upright bar.
- Use "pie" or "donut" only for a breakdown with a handful of categories. For anything
  longer a bar reads better - and set top_n to keep it short.
- Put money columns on "format:currency" in properties, and name the currency with
  "currency:INR" unless the user says otherwise.
- Give every visual a title that names the number: Total sales by region, never Chart 1.
- Add a filter for the one or two columns a reader would want to narrow by - usually a
  date and the main category."""

#: Every catalog entry `_DESIGN_RULES` recommends by name, declared beside the prose so a
#: test can pin all of them at once. Cheaper than a record per shape, and it catches the one
#: failure that matters: a rule that keeps recommending something the page cannot draw.
_DESIGN_RULE_NAMES: tuple[str, ...] = (
    CHART_LINE,
    CHART_AREA,
    CHART_BAR_HORIZONTAL,
    CHART_PIE,
    CHART_DONUT,
    SORT_LARGEST,
    SORT_AUTOMATIC,
    FORMAT_CURRENCY,
    DEFAULT_CURRENCY,
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

    These are the *flattened* tables, so a master's attributes already appear as columns of
    the main table - which is why the model never has to think about joins at all.

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
    "measure_column_2": "measure_column_2",
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


def _panel_fields_for_prompt(panel: PanelSpec) -> str:
    allowed = _FIELDS_BY_KIND.get(panel.visual_type)
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


def build_prompt(instruction: str, tables: dict[str, pd.DataFrame], *,
                 main_table: str = "", notes: dict[str, str] | None = None,
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
        "Visual catalog:\n" + describe_catalog_for_prompt(),
        "",
    ]
    if main_table:
        parts.append(f"The main table is {main_table}. Prefer it unless the request needs "
                     "another one.\n")

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
    labels = {**FILTER_LABELS, **DASHBOARD_CHART_LABELS}
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
    trouble = panel_problems(panel, available_columns, date_columns)
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
        main_table: which of them is the fact table, used when a proposal names no table.
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
            build_prompt(instruction, tables, main_table=main_table, notes=notes),
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
            proposed, tables, main_table, available_columns, date_columns
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
        filter_position=position if position in FILTER_POSITIONS else FILTER_TOP,
        main_table=main_table,
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
        profile, instruction, tables, main_table=spec.main_table,
        notes=notes, key_path=key_path,
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

    try:
        response = run_structured(
            profile,
            build_prompt(text, tables, main_table=spec.main_table, notes=notes,
                         current=spec),
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
        action = _key(proposed.action) or ACTION_ADD
        if action not in ACTIONS:
            result.notes.append(
                f"Skipped an edit: '{proposed.action}' isn't something that can be done to "
                "a visual."
            )
            continue

        target = None
        if action in (ACTION_UPDATE, ACTION_REMOVE):
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
            proposed, tables, spec.main_table, available_columns, date_columns
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
