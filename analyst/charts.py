"""Turning a result set into a chart, deterministically (requirement 6.2).

The chart is built from the frame the SQL already returned, never from a second model
call. Requirement 6.2 asks that all three output types be "derived from the same computed
result, so they never disagree with one another" — a model asked to invent chart data
could disagree with the table sitting directly above it, and this cannot.

The work is split in two so the same code serves both the automatic chart and the user's
own adjustments to it:

- `choose_chart` reads the shape and dtypes of the result and decides what to plot, which
  is the same reasoning a person applies: a label beside a number is a bar, a date beside
  a number is a line, two numbers are a scatter.
- `render_chart` draws whatever it is told to draw.

`build_chart` is the two together, and is what the pipeline calls. The page hands
`render_chart` the user's picks instead, which is why a customized chart cannot drift from
what the automatic one would have produced — they run the same drawing code.

What to plot and how it looks are two separate values, `ChartChoices` and `ChartStyle`,
because they have different lifetimes: switching a bar to a line changes the data mapping
and should keep the title, palette and labels the user chose. Both are plain data rather
than baked into the figure, so a chart can be described, stored and redrawn later — which
is what a pinned dashboard tile will need.

When nothing plots sensibly, this returns None and the caller falls back to the table — a
missing chart is a far smaller failure than a misleading one.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

logger = logging.getLogger(__name__)

# Beyond this, a categorical axis stops being readable and the bars become a smear. The
# chart shows the largest slice and says so, rather than silently plotting a wall.
MAX_CATEGORIES = 30

# A pie chart is only honest for a handful of parts of one whole.
MAX_PIE_SLICES = 8

# `SELECT department, min(x), max(x), avg(x)` is three measures over one label and must
# plot as three grouped series. Past this many, the groups are too thin to read.
MAX_MEASURES = 6

# Long enough for "Min basic salary, Max basic salary and Avg basic salary by Department",
# short enough not to push the plot area off the bottom of the card.
MAX_TITLE_CHARS = 90

# Plotly's own default, kept as the starting height so a chart nobody customizes looks
# exactly as it did before the height control existed.
DEFAULT_CHART_HEIGHT = 450
MIN_CHART_HEIGHT = 300
MAX_CHART_HEIGHT = 900

_SHARE_WORDS = ("share", "proportion", "percentage", "percent", "split", "breakdown", "mix", "pie")

CHART_BAR = "bar"
CHART_BAR_HORIZONTAL = "bar_horizontal"
CHART_LINE = "line"
CHART_AREA = "area"
CHART_SCATTER = "scatter"
CHART_PIE = "pie"

# What each type is called on screen. Ordered as the picker should list them: the two bars
# together, then the two time-shaped types, then the specialised pair.
CHART_LABELS: dict[str, str] = {
    CHART_BAR: "Bar",
    CHART_BAR_HORIZONTAL: "Bar — horizontal",
    CHART_LINE: "Line",
    CHART_AREA: "Area",
    CHART_SCATTER: "Scatter",
    CHART_PIE: "Pie",
}


@dataclass
class ChartChoices:
    """What to plot: one x column, one or more measures, and an optional colour split.

    Attributes:
        kind: one of the `CHART_*` constants.
        x: the column along the category or time axis. On a pie this names the slices; on a
            horizontal bar it is still the category, drawn up the side rather than along
            the bottom.
        measures: the numeric column(s) to plot. Several become several series; types that
            can only draw one (pie, scatter, anything with a colour split) use the first
            and say so.
        colour: the column that splits the measure into a legend, or None.
    """

    kind: str
    x: str
    measures: list[str] = field(default_factory=list)
    colour: str | None = None


# --------------------------------------------------------------------------------------
# Style
# --------------------------------------------------------------------------------------

PALETTE_DEFAULT = "default"
PALETTE_BOLD = "bold"
PALETTE_PASTEL = "pastel"
PALETTE_SAFE = "safe"
PALETTE_MUTED = "muted"
PALETTE_SINGLE = "single"

# Qualitative sequences only: every colour axis in here is a category, never a scale.
CHART_PALETTES: dict[str, list[str]] = {
    PALETTE_DEFAULT: list(px.colors.qualitative.Plotly),
    PALETTE_BOLD: list(px.colors.qualitative.Bold),
    PALETTE_PASTEL: list(px.colors.qualitative.Pastel),
    PALETTE_SAFE: list(px.colors.qualitative.Safe),
    PALETTE_MUTED: list(px.colors.qualitative.Set2),
    # Express cycles the sequence, so a one-entry list paints every series the same. Only
    # offered where there is a single series to paint — see `available_palettes`.
    PALETTE_SINGLE: ["#4C78A8"],
}

PALETTE_LABELS: dict[str, str] = {
    PALETTE_DEFAULT: "Default",
    PALETTE_BOLD: "Bold",
    PALETTE_PASTEL: "Pastel",
    PALETTE_SAFE: "Colour-blind safe",
    PALETTE_MUTED: "Muted",
    PALETTE_SINGLE: "Single colour",
}

SERIES_GROUPED = "grouped"
SERIES_STACKED = "stacked"
SERIES_PERCENT = "percent"

SERIES_LABELS: dict[str, str] = {
    SERIES_GROUPED: "Side by side",
    SERIES_STACKED: "Stacked",
    SERIES_PERCENT: "100% stacked",
}

# What a 100% stacked chart's measure axis is actually showing, since it is no longer the
# measure's own units.
PERCENT_AXIS_TITLE = "Share of total (%)"

SORT_AUTOMATIC = "automatic"
SORT_LARGEST = "largest"
SORT_SMALLEST = "smallest"
SORT_LABEL = "label"
SORT_ORIGINAL = "original"

SORT_LABELS: dict[str, str] = {
    SORT_AUTOMATIC: "Automatic",
    SORT_LARGEST: "Biggest first",
    SORT_SMALLEST: "Smallest first",
    SORT_LABEL: "By name",
    SORT_ORIGINAL: "As the query returned",
}


@dataclass
class ChartStyle:
    """How the chart looks, as opposed to what it plots.

    Every default reproduces the chart this module drew before any of these existed, so an
    untouched `ChartStyle()` is invisible.

    Attributes:
        title: the heading, or None to keep the one derived from the columns. Left as None
            rather than filled in, so the automatic title keeps improving as the user
            changes columns and only stops the moment they write their own.
        palette: a key of `CHART_PALETTES`.
        series: how several series share the space — see the `SERIES_*` constants. Applies
            to bars and areas; the other types draw one series over another regardless.
        show_values: prints each point's value on the chart itself.
        show_legend: a single-series chart's legend is a caption for one thing, and worth
            being able to turn off.
        sort: how the categories are ordered — see the `SORT_*` constants. `SORT_AUTOMATIC`
            is this module's own judgement: time forwards, a single run of bars
            biggest-first, everything else as the query returned it.
        top_n: how many categories to keep, or None for the automatic cap.
        height: the plot height in pixels. A horizontal bar of twenty categories needs
            more room than the default gives it.
    """

    title: str | None = None
    palette: str = PALETTE_DEFAULT
    series: str = SERIES_GROUPED
    show_values: bool = False
    show_legend: bool = True
    sort: str = SORT_AUTOMATIC
    top_n: int | None = None
    height: int = DEFAULT_CHART_HEIGHT


def _numeric_columns(frame: pd.DataFrame) -> list[str]:
    return [name for name in frame.columns if pd.api.types.is_numeric_dtype(frame[name])]


def _datetime_columns(frame: pd.DataFrame) -> list[str]:
    return [name for name in frame.columns if pd.api.types.is_datetime64_any_dtype(frame[name])]


def _label_columns(frame: pd.DataFrame, numeric: list[str], temporal: list[str]) -> list[str]:
    """Everything that isn't a measure or a date — the candidates for a category axis."""
    return [name for name in frame.columns if name not in numeric and name not in temporal]


def _pretty(name: str) -> str:
    """`avg_basic_salary` reads as 'Avg basic salary' on an axis or in a legend."""
    readable = str(name).replace("_", " ").strip()
    return readable[:1].upper() + readable[1:] if readable else str(name)


def _join_names(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C' — the way a person would say the list aloud."""
    readable = [_pretty(name) for name in names]
    if len(readable) == 1:
        return readable[0]
    return f"{', '.join(readable[:-1])} and {readable[-1]}"


def _limit_measures(numeric: list[str], warnings: list[str]) -> list[str]:
    """Caps how many series share one axis, and says which ones were left out."""
    if len(numeric) <= MAX_MEASURES:
        return numeric
    warnings.append(
        f"Charting the first {MAX_MEASURES} of {len(numeric)} measures — the full result is in the table."
    )
    return numeric[:MAX_MEASURES]


# --------------------------------------------------------------------------------------
# What can be drawn
# --------------------------------------------------------------------------------------


def is_chartable(frame: pd.DataFrame | None) -> bool:
    """Whether there is anything here to plot at all: two columns, one of them a number."""
    if frame is None or frame.empty or frame.shape[1] < 2:
        return False
    return bool(_numeric_columns(frame))


def available_chart_types(frame: pd.DataFrame | None) -> list[str]:
    """The chart types this particular result supports, in the order to offer them.

    Only types that would draw something honest are listed, so the picker cannot be used
    to produce a broken chart: a scatter needs two numeric columns to relate, and a pie
    needs a small number of non-negative parts of one whole.
    """
    if not is_chartable(frame):
        return []

    numeric = _numeric_columns(frame)
    kinds = [CHART_BAR, CHART_BAR_HORIZONTAL, CHART_LINE, CHART_AREA]

    if len(numeric) >= 2:
        kinds.append(CHART_SCATTER)
    if _pie_would_be_honest(frame, numeric):
        kinds.append(CHART_PIE)
    return kinds


def _pie_would_be_honest(frame: pd.DataFrame, numeric: list[str]) -> bool:
    """A pie needs few enough slices to read, and values that are parts of one whole.

    Negative values are the important exclusion: a slice cannot be smaller than nothing, so
    Plotly draws the absolute value and the chart quietly says something untrue.
    """
    if len(frame) > MAX_PIE_SLICES:
        return False
    if len(frame.columns) - len(numeric) < 1:
        return False
    return any((frame[measure].fillna(0) >= 0).all() for measure in numeric)


def _is_temporal(frame: pd.DataFrame, column: str) -> bool:
    return column in frame.columns and pd.api.types.is_datetime64_any_dtype(frame[column])


# --------------------------------------------------------------------------------------
# Which style controls this particular chart can honour
# --------------------------------------------------------------------------------------
#
# Each of these answers "would this control do anything here?", so the page can leave out
# the ones that wouldn't. A stacking control on a chart with one series is a control that
# does nothing, which is worse than no control at all.


def series_count(frame: pd.DataFrame | None, choices: ChartChoices) -> int:
    """How many series the chart draws: one per legend value, or one per measure."""
    if frame is None:
        return 0
    if choices.colour and choices.colour in frame.columns:
        return int(frame[choices.colour].nunique())
    return len([name for name in choices.measures if name in frame.columns])


def supports_series_layout(frame: pd.DataFrame | None, choices: ChartChoices) -> bool:
    """Whether stacking is a real choice: bars or areas, with something to stack."""
    if choices.kind not in (CHART_BAR, CHART_BAR_HORIZONTAL, CHART_AREA):
        return False
    return series_count(frame, choices) > 1


def supports_sorting(frame: pd.DataFrame | None, choices: ChartChoices) -> bool:
    """Whether reordering means anything.

    A scatter has no category axis to order, and reordering a time axis would put the
    chart's own subject out of sequence.
    """
    if frame is None or choices.kind == CHART_SCATTER:
        return False
    return choices.x in frame.columns and not _is_temporal(frame, choices.x)


def top_n_ceiling(frame: pd.DataFrame | None, choices: ChartChoices) -> int:
    """The most categories this chart could show, or 0 when limiting doesn't apply.

    Only the types that already cap themselves are limitable — a long time series is the
    point of a time series, not a problem to be trimmed.
    """
    if frame is None or choices.kind not in (CHART_BAR, CHART_BAR_HORIZONTAL, CHART_PIE):
        return 0
    if choices.x not in frame.columns:
        return 0
    count = int(frame[choices.x].nunique())
    return count if count > 1 else 0


def default_top_n(frame: pd.DataFrame | None, choices: ChartChoices) -> int:
    """How many categories the automatic cap keeps — where the control starts from."""
    ceiling = top_n_ceiling(frame, choices)
    if not ceiling:
        return 0
    return min(ceiling, MAX_PIE_SLICES if choices.kind == CHART_PIE else MAX_CATEGORIES)


def available_palettes(frame: pd.DataFrame | None, choices: ChartChoices) -> list[str]:
    """Every palette, minus the single colour when there is more than one series to tell
    apart — painting four series the same colour makes an unreadable chart."""
    palettes = list(CHART_PALETTES)
    if series_count(frame, choices) > 1:
        palettes.remove(PALETTE_SINGLE)
    return palettes


# --------------------------------------------------------------------------------------
# Choosing
# --------------------------------------------------------------------------------------


def choose_chart(
    frame: pd.DataFrame | None, question: str = ""
) -> tuple[ChartChoices | None, list[str]]:
    """Picks what to plot from the shape of the result, without drawing anything.

    Returns:
        `(choices, warnings)`. `choices` is None when the frame can't be plotted, and the
        warnings say why.
    """
    warnings: list[str] = []

    if frame is None or frame.empty:
        return None, ["There were no rows to chart."]
    if frame.shape[1] < 2:
        return None, ["A chart needs at least two columns — this result has one."]

    numeric = _numeric_columns(frame)
    if not numeric:
        return None, ["A chart needs a numeric column, and this result has none."]

    temporal = _datetime_columns(frame)
    labels = _label_columns(frame, numeric, temporal)
    measures = _limit_measures(numeric, warnings)

    if temporal:
        # A label beside a date is one line per category. That spends the colour axis, and
        # `render_chart` trims the measures to match — over time, one line per category is
        # the more useful reading of a frame that offers both.
        return ChartChoices(
            kind=CHART_LINE, x=temporal[0], measures=measures, colour=labels[0] if labels else None
        ), warnings

    if labels:
        return _choose_category_chart(frame, labels, measures, question, warnings)

    return ChartChoices(kind=CHART_SCATTER, x=numeric[0], measures=[numeric[1]]), warnings


def _choose_category_chart(
    frame: pd.DataFrame,
    labels: list[str],
    measures: list[str],
    question: str,
    warnings: list[str],
) -> tuple[ChartChoices, list[str]]:
    """Measures by category — a pie when the question asks about shares, else bars.

    Every numeric column becomes its own series. A `min`/`max`/`avg` breakdown arrives as
    one label column and three measures, and plotting only the first would answer a third
    of the question while looking like the whole of it.

    A second label column (e.g. "count by status, department for legend") becomes the
    colour split instead — but only with a single measure, because a bar can encode a
    second category by colour or several measures by colour, not both.
    """
    x_column = labels[0]
    colour = labels[1] if len(labels) >= 2 and len(measures) == 1 else None

    shown = 2 if colour else 1
    if len(labels) > shown:
        warnings.append(
            f"Charting by {_join_names(labels[:shown])} only — "
            f"'{_join_names(labels[shown:])}' isn't shown."
        )

    wants_share = any(word in question.lower() for word in _SHARE_WORDS)
    if (
        wants_share
        and colour is None
        and len(measures) == 1
        and len(frame) <= MAX_PIE_SLICES
        and (frame[measures[0]] >= 0).all()
    ):
        return ChartChoices(kind=CHART_PIE, x=x_column, measures=measures), warnings

    return ChartChoices(kind=CHART_BAR, x=x_column, measures=measures, colour=colour), warnings


# --------------------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------------------


def build_chart(frame: pd.DataFrame | None, question: str = "") -> tuple[go.Figure | None, list[str]]:
    """Builds the chart a result set supports, or explains why there isn't one.

    Returns:
        `(figure, warnings)`. The figure is None when the frame can't be plotted; the
        warnings say what happened (too few columns, nothing numeric, categories
        truncated) so the page can show it beside the table rather than failing silently.

    Never raises: a chart is one of three outputs and must not be able to cost the user
    the other two.
    """
    choices, warnings = choose_chart(frame, question)
    if choices is None:
        return None, warnings

    figure, draw_warnings = render_chart(frame, choices)
    return figure, warnings + draw_warnings


def render_chart(
    frame: pd.DataFrame | None, choices: ChartChoices, style: ChartStyle | None = None
) -> tuple[go.Figure | None, list[str]]:
    """Draws exactly what `choices` asks for, adjusting only what would break the chart.

    This is the seam the customize controls use, so a hand-picked chart and an automatic
    one are drawn by identical code. Choices that can't be honoured as given — a pie of
    three measures, a legend with four hundred values — are narrowed rather than refused,
    and each narrowing is reported.

    Args:
        style: how it should look. The default reproduces the chart exactly as this module
            drew it before any styling existed.

    Never raises, for the same reason `build_chart` doesn't.
    """
    warnings: list[str] = []
    style = style or ChartStyle()

    if frame is None or frame.empty:
        return None, ["There were no rows to chart."]

    measures = [name for name in choices.measures if name in frame.columns]
    if not measures or choices.x not in frame.columns:
        return None, ["That combination of columns isn't in this result any more."]

    measures = _limit_measures(measures, warnings)
    colour = _usable_colour(frame, choices.colour, warnings)

    # One series per colour already spends the colour axis, so a second measure would have
    # to share it and the two would be indistinguishable.
    if colour is not None and len(measures) > 1:
        warnings.append(
            f"Charting {_pretty(measures[0])} only — one series per '{_pretty(colour)}' "
            "leaves no room for the other measures."
        )
        measures = measures[:1]

    try:
        ordered = _cap_rows(frame, choices.kind, choices.x, measures, colour, style, warnings)
        ordered = _sort_rows(ordered, choices.kind, choices.x, measures, colour, style)
        return _draw(ordered, choices.kind, choices.x, measures, colour, style, warnings)
    except (ValueError, TypeError, KeyError) as error:
        # Plotly raises these for shapes it can't map; the table is still perfectly good.
        logger.exception("Could not chart a %s result as a %s.", frame.shape, choices.kind)
        return None, warnings + [f"This result couldn't be charted ({error})."]


def _usable_colour(frame: pd.DataFrame, colour: str | None, warnings: list[str]) -> str | None:
    """Drops a legend column with too many values to be a legend."""
    if colour is None or colour not in frame.columns:
        return None
    if frame[colour].nunique() <= MAX_CATEGORIES:
        return colour
    warnings.append(
        f"'{_pretty(colour)}' has too many values to use as a legend, so the chart is "
        "combined without it."
    )
    return None


def _cap_rows(
    frame: pd.DataFrame,
    kind: str,
    x_column: str,
    measures: list[str],
    colour: str | None,
    style: ChartStyle,
    warnings: list[str],
) -> pd.DataFrame:
    """Keeps a category axis readable. Time series are left whole — a long one is the point.

    The automatic cap is what stops an unreadable wall of bars; `style.top_n` replaces it
    when the user has said how many they want, in either direction.
    """
    if kind not in (CHART_BAR, CHART_BAR_HORIZONTAL, CHART_PIE):
        return frame

    limit = style.top_n or (MAX_PIE_SLICES if kind == CHART_PIE else MAX_CATEGORIES)
    category_count = frame[x_column].nunique() if colour is not None else len(frame)
    if category_count <= limit:
        return frame

    noun = "slices" if kind == CHART_PIE else f"'{_pretty(x_column)}' values"
    warnings.append(f"Charting the top {limit} of {category_count} {noun} — the full result is in the table.")

    if colour is not None:
        # Ranking whole categories, not individual bars, so a kept category keeps every
        # part of its legend split rather than losing the smaller ones.
        keep = frame.groupby(x_column)[measures[0]].sum().nlargest(limit).index
        return frame[frame[x_column].isin(keep)]
    if len(measures) == 1:
        return frame.nlargest(limit, measures[0])
    # With several measures there is no single "largest", so the query's own order stands.
    return frame.head(limit)


def _sort_rows(
    frame: pd.DataFrame,
    kind: str,
    x_column: str,
    measures: list[str],
    colour: str | None,
    style: ChartStyle,
) -> pd.DataFrame:
    """Orders the categories, either by this module's judgement or by the user's."""
    if style.sort == SORT_ORIGINAL:
        return frame
    if style.sort == SORT_AUTOMATIC:
        return _sort_automatically(frame, kind, x_column, measures, colour)
    if style.sort == SORT_LABEL:
        # Horizontal categories are drawn bottom-up, so A-to-Z reads down the axis only
        # when the frame is ordered Z-to-A.
        return frame.sort_values(x_column, ascending=kind != CHART_BAR_HORIZONTAL, kind="stable")

    biggest_first = style.sort == SORT_LARGEST
    ascending = biggest_first if kind == CHART_BAR_HORIZONTAL else not biggest_first
    return _sort_by_measure(frame, x_column, measures[0], colour, ascending)


def _sort_by_measure(
    frame: pd.DataFrame, x_column: str, measure: str, colour: str | None, ascending: bool
) -> pd.DataFrame:
    """Orders whole categories by a measure, keeping each one's rows together.

    With a legend split a category owns one row per legend value, so sorting the rows
    themselves would interleave the categories. The categories are ranked by their totals
    instead, and their rows travel with them.
    """
    if colour is None:
        return frame.sort_values(measure, ascending=ascending, kind="stable")

    totals = frame.groupby(x_column)[measure].sum().sort_values(ascending=ascending)
    ranks = {name: position for position, name in enumerate(totals.index)}
    ranked = frame.assign(_chart_rank=frame[x_column].map(ranks))
    return ranked.sort_values("_chart_rank", kind="stable").drop(columns="_chart_rank")


def _sort_automatically(
    frame: pd.DataFrame, kind: str, x_column: str, measures: list[str], colour: str | None
) -> pd.DataFrame:
    """Time runs forwards; a single run of bars runs biggest-first; everything else keeps
    the order the query asked for."""
    if _is_temporal(frame, x_column):
        return frame.sort_values(x_column)
    if kind == CHART_PIE:
        return frame.sort_values(measures[0], ascending=False)
    if kind in (CHART_BAR, CHART_BAR_HORIZONTAL) and len(measures) == 1 and colour is None:
        # Horizontal bars are drawn bottom-up, so ascending puts the biggest at the top.
        return frame.sort_values(measures[0], ascending=kind == CHART_BAR_HORIZONTAL)
    return frame


def _draw(
    frame: pd.DataFrame,
    kind: str,
    x_column: str,
    measures: list[str],
    colour: str | None,
    style: ChartStyle,
    warnings: list[str],
) -> tuple[go.Figure, list[str]]:
    """Hands the settled data to Plotly and titles the result."""
    # Express melts a list of measures into one long frame, and `y=[a]` labels the axis
    # 'value' where `y=a` labels it 'a' — so a single measure is passed on its own.
    y_value: str | list[str] = measures[0] if len(measures) == 1 else measures
    preposition = "over" if _is_temporal(frame, x_column) else "by"
    palette = CHART_PALETTES.get(style.palette, CHART_PALETTES[PALETTE_DEFAULT])

    if kind == CHART_PIE:
        figure = px.pie(
            frame, names=x_column, values=measures[0], hole=0.35, color_discrete_sequence=palette
        )
        title = f"{_pretty(measures[0])} by {_pretty(x_column)}"
        return _finish(figure, kind, title, style, warnings)

    if kind == CHART_SCATTER:
        figure = px.scatter(
            frame, x=x_column, y=measures[0], color=colour, color_discrete_sequence=palette
        )
        if colour is None:
            # A one-trace scatter has no name, so the legend would render an empty row.
            figure.update_traces(name=_pretty(measures[0]), showlegend=True)
        title = f"{_pretty(measures[0])} vs {_pretty(x_column)}"

    elif kind == CHART_BAR_HORIZONTAL:
        # Orientation swaps the roles: the measure runs along the bottom, the category up
        # the side, which is what makes long labels readable.
        figure = px.bar(
            frame,
            x=y_value,
            y=x_column,
            color=colour,
            orientation="h",
            color_discrete_sequence=palette,
            **_bar_layout(style),
        )
        figure.update_layout(
            xaxis_title=_pretty(measures[0]) if len(measures) == 1 else "Value",
            yaxis_title=_pretty(x_column),
        )
        title = f"{_join_names(measures)} {preposition} {_pretty(x_column)}"

    elif kind == CHART_AREA:
        figure = px.area(frame, x=x_column, y=y_value, color=colour, color_discrete_sequence=palette)
        _lay_out_areas(figure, style)
        _name_axes(figure, measures)
        title = f"{_join_names(measures)} {preposition} {_pretty(x_column)}"

    elif kind == CHART_LINE:
        figure = px.line(
            frame, x=x_column, y=y_value, color=colour, markers=True, color_discrete_sequence=palette
        )
        _name_axes(figure, measures)
        title = f"{_join_names(measures)} {preposition} {_pretty(x_column)}"

    else:
        figure = px.bar(
            frame,
            x=x_column,
            y=y_value,
            color=colour,
            color_discrete_sequence=palette,
            **_bar_layout(style),
        )
        _name_axes(figure, measures, x_title=_pretty(x_column))
        title = f"{_join_names(measures)} {preposition} {_pretty(x_column)}"

    if colour is not None:
        # Named after the column, since with a colour split the legend entries are its
        # values and an unlabelled row of them doesn't say what they have in common.
        figure.update_layout(legend_title_text=_pretty(colour))
        title = f"{_pretty(measures[0])} {preposition} {_pretty(x_column)} and {_pretty(colour)}"

    return _finish(figure, kind, title, style, warnings)


def _bar_layout(style: ChartStyle) -> dict[str, str]:
    """Express's bar arguments for the chosen series layout.

    'relative' rather than 'stack' so a negative value stacks downwards from the axis
    instead of being added to the pile as though it were a positive one. The normalisation
    that turns a stack into a 100% stack is a layout property, applied in `_finish`.
    """
    if style.series in (SERIES_STACKED, SERIES_PERCENT):
        return {"barmode": "relative"}
    return {"barmode": "group"}


def _lay_out_areas(figure: go.Figure, style: ChartStyle) -> None:
    """The same three layouts for areas, which Express expresses on the traces instead.

    A single area is left exactly as Express drew it: with nothing to stack against, the
    layout isn't a question, and the page doesn't ask it either.

    Un-stacking has to re-point the fill as well — Express fills each area to the one below
    it, and once they are no longer piled up there is nothing below but the axis. They then
    genuinely overlap, hence the transparency.
    """
    if len(figure.data) < 2:
        return
    if style.series == SERIES_GROUPED:
        figure.update_traces(stackgroup=None, fill="tozeroy", opacity=0.55)
    elif style.series == SERIES_PERCENT:
        figure.update_traces(groupnorm="percent")


def _finish(
    figure: go.Figure, kind: str, title: str, style: ChartStyle, warnings: list[str]
) -> tuple[go.Figure, list[str]]:
    """Applies everything that doesn't depend on which chart type was drawn."""
    if style.series == SERIES_PERCENT and kind in (CHART_BAR, CHART_BAR_HORIZONTAL, CHART_AREA):
        if kind != CHART_AREA:
            figure.update_layout(barnorm="percent")
        # The measure axis is no longer in the measure's own units, and an axis still
        # labelled 'Salary' while showing 0-100 would be a lie.
        axis = "xaxis_title" if kind == CHART_BAR_HORIZONTAL else "yaxis_title"
        figure.update_layout(**{axis: PERCENT_AXIS_TITLE})

    if style.show_values:
        _label_values(figure, kind, style)

    return _styled(figure, style.title or title, style), warnings


def _label_values(figure: go.Figure, kind: str, style: ChartStyle) -> None:
    """Prints each point's own value on the chart.

    Bars carry their label natively; the line, area and scatter families are all scatter
    traces underneath and only draw text when their mode asks for it, which is why the
    mode is extended rather than replaced — replacing it would drop the markers.
    """
    if kind == CHART_PIE:
        figure.update_traces(textinfo="value+percent")
        return

    is_bar = kind in (CHART_BAR, CHART_BAR_HORIZONTAL)
    template = "%{x:,}" if kind == CHART_BAR_HORIZONTAL else "%{y:,}"
    if is_bar:
        # Outside a stacked bar the labels would sit on top of the segment above.
        position = "outside" if style.series == SERIES_GROUPED else "inside"
    else:
        position = "top center"

    figure.update_traces(texttemplate=template, textposition=position)
    if is_bar:
        return

    for trace in figure.data:
        mode = getattr(trace, "mode", None)
        if mode and "text" not in mode:
            trace.mode = f"{mode}+text"


def _name_axes(figure: go.Figure, measures: list[str], x_title: str | None = None) -> None:
    """Replaces Plotly's generic 'value'/'variable' labels for a multi-measure frame.

    Passing a list of columns as `y` makes Express melt the frame, and the melted column
    names are what end up on the axis and the legend heading — neither means anything to
    the person reading the chart.
    """
    figure.update_layout(
        yaxis_title=_pretty(measures[0]) if len(measures) == 1 else "Value",
        legend_title_text="",
    )
    if x_title is not None:
        figure.update_layout(xaxis_title=x_title)


def _styled(figure: go.Figure, title: str, style: ChartStyle) -> go.Figure:
    """One consistent look: a title, a border and a legend on every chart.

    Backgrounds are transparent so the chart follows the app theme rather than punching a
    white rectangle into a dark page, and the border is drawn in a neutral grey that reads
    on both.
    """
    figure.update_layout(
        title={
            "text": title[:MAX_TITLE_CHARS],
            "x": 0,
            "xanchor": "left",
            "font": {"size": 16},
        },
        height=style.height,
        showlegend=style.show_legend,
        # Inside the plot area, so the border below encloses the legend with it rather than
        # leaving it floating outside a box.
        legend={
            "orientation": "h",
            "yanchor": "top",
            "y": 0.99,
            "xanchor": "right",
            "x": 0.99,
            "bgcolor": "rgba(0,0,0,0)",
        },
        margin={"l": 10, "r": 10, "t": 55, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    figure.add_shape(
        type="rect",
        xref="paper",
        yref="paper",
        x0=0,
        y0=0,
        x1=1,
        y1=1,
        line={"color": "rgba(128,128,128,0.4)", "width": 1},
        fillcolor="rgba(0,0,0,0)",
        layer="below",
    )
    figure.update_xaxes(showgrid=False)
    figure.update_yaxes(gridcolor="rgba(128,128,128,0.2)")
    return figure
