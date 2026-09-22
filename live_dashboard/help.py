"""What the dashboard can and cannot be asked for, in plain English (phase 38).

The page is driven entirely by typing a sentence at a model, and a box with no edges is hard
to aim at: phase 38 began with a user asking a pie chart to "show data labels", which was a
perfectly reasonable request the vocabulary had no word for. The setting now exists - but the
lesson that outlasts it is that the user could not have known either way.

So this is the written edge of the box. The **Can do** half is built from the very constants
the AI catalog is rendered from (`model.DASHBOARD_CHART_LABELS`, `DASHBOARD_AGGREGATIONS`,
the property vocabularies), which is the only thing that keeps a help page honest: a shape
added in a later phase appears here the moment it joins the tuple, and one removed disappears.
The **Can't do yet** half is written by hand, because a list of things that are absent cannot
be derived from what is present.

No Streamlit here, matching the rest of `live_dashboard/` - `session.py` is the only module
that imports it. This returns Markdown; the page decides where to put it.
"""

import logging

from live_dashboard.model import (
    CARD_SIZES,
    CURRENCY_CODES,
    DASHBOARD_AGGREGATIONS,
    DASHBOARD_CHART_LABELS,
    FILTER_LABELS,
    LABELLABLE_CHARTS,
    LEGEND_POSITIONS,
    MAX_MEASURES_PER_CHART,
    NAMED_COLOURS,
    PANEL_SIZES,
    PANEL_WIDTHS,
)

logger = logging.getLogger(__name__)


def _listed(names) -> str:
    """`a, b and c` - the way a sentence names a handful of things."""
    names = [str(name) for name in names]
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def capabilities_markdown(max_visuals: int) -> str:
    """The whole help panel as Markdown.

    Args:
        max_visuals: the cap a round enforces, passed in rather than imported so this module
            stays clear of `ai_spec` - the help would otherwise import the thing it describes.

    Returns:
        Markdown with three parts: what can be asked for, what cannot be yet, and how to get
        a better answer. Never raises; every value it prints is a constant this app owns.
    """
    shapes = _listed(sorted(label.lower() for label in DASHBOARD_CHART_LABELS.values()))
    labellable = _listed(sorted(
        DASHBOARD_CHART_LABELS.get(name, name).lower() for name in LABELLABLE_CHARTS
    ))
    totals = _listed(sorted(label.lower() for label in DASHBOARD_AGGREGATIONS.values()))
    filters = _listed(sorted(label.lower() for label in FILTER_LABELS.values()))

    return f"""**You can ask for**

| Option | What you can set | Example |
| --- | --- | --- |
| Visuals | Cards (one big number), charts, tables and filters, up to {max_visuals} on a page | *"add a pie of headcount by department"* |
| Chart shapes | {shapes} | *"show this as a donut"* |
| Ways to total a number | {totals} | *"show the average salary"* |
| Several numbers on one chart | Up to {MAX_MEASURES_PER_CHART} numbers together; different units go on a second axis on the right | *"show min, average and max salary by department"* or *"salary as bars and headcount as a line"* |
| How it looks | **Data labels** on {labellable} (a pie shows each slice's percentage), **legend** {_listed(LEGEND_POSITIONS)}, **axis titles** on/off, **colour** from {_listed(NAMED_COLOURS)}, ****size {_listed(PANEL_SIZES)}, **width** {_listed(PANEL_WIDTHS)}, **card size** {_listed(CARD_SIZES)} | *"turn on data labels"*, *"make it medium"* |
| Numbers written properly | **Currency** in any of {_listed(CURRENCY_CODES)} | *"show amounts in currency"* |
| Filters | {filters}, down the left or across the top; a number filter has two handles | *"between 30,000 and 60,000"* |
| One filter across related tables | A column from a linked table shows up on every table it reaches, as "Table - Column" | *"filter on Employee Master - Department"* |
| Tidying up | Sorting, top N, layout, renaming, removing | *"sort largest first"*, *"only the top 10"*, *"rename it to Monthly spend"* |

**Not yet**

- **Maps.** There is no map in the exported page, and a bar chart of geography is a wrong
  answer rather than a near one.
- **Your own colour per slice or per bar**, and colour codes like `#ff0000`. Only the named
  colours above.
- **Changing the data itself** - new columns, fixing spellings, combining tables. That is
  what **Transform Data** is for; the dashboard only ever draws what is already there.
- **Sums across visuals**, targets and forecasts.
- **Writing formulas** into a chart.

**Tips**

- **One thing per press.** Two unrelated requests in one sentence usually gets one of them.
- **Use a visual's own Edit button** to change just that one. Then you can say *"make it
  horizontal"* without naming it.
- **Name a visual by the title printed above it** when you use the box at the top.
- **Undo** puts the dashboard back if a change was not what you meant.
- If nothing changes, the dashboard is telling you it has no setting for what you asked -
  try one of the words above."""
