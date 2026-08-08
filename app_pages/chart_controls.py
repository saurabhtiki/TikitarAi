"""The "Customize chart" panel, shared by every page that shows a result.

`analyst.charts` decides what can be drawn and draws it; this module is the widgets that
ask. It lives at the page layer because it imports Streamlit, and it is one module rather
than one per page because the Chat transcript and the Checks results tab want the identical
panel — a control added for a criteria and missing from an answer would be a difference
nobody could explain.

Nothing here calls a model or re-runs any SQL. The frame is already in hand, so changing a
dropdown is a redraw of rows that have already been fetched.

Two tabs, because the two halves answer different questions — **Data** is what the chart is
of, **Style** is what it looks like — and they are two separate values for the same reason,
so changing the chart type keeps the title and colours the user picked.

Widget keys come from a `ChartKeys`, which the caller builds — `an_chart_kind_3` for the
fourth message of a transcript, `ck_chart_kind_a1b2` for a criteria. One panel per answer
and one per rule can then coexist, and "reset" can find exactly the widgets it owns.
"""

import logging
from dataclasses import dataclass, field

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from analyst import charts

logger = logging.getLogger(__name__)

# What the legend picker calls "no split at all". Not a column name, so it cannot collide
# with one.
NO_SPLIT = "None"

# How many aggregation function pickers stand side by side before wrapping. Four across is
# already narrow in a sidebar-width expander.
FUNCTION_COLUMNS = 3

# Every widget this panel can create whose name is known in advance. The aggregation
# function pickers are the exception — they are named after whichever columns are selected —
# which is why `ChartKeys` records what it hands out rather than this list being the whole
# answer.
CONTROL_NAMES = (
    "kind",
    "x",
    "agg",
    "y",
    "colour",
    "line",
    "axis2",
    "title",
    "palette",
    "series",
    "values",
    "legend",
    "sort",
    "top",
    "height",
)


@dataclass
class ChartKeys:
    """Names one panel's widgets, and remembers which ones it named.

    The shape is `{prefix}_{name}{suffix}` rather than a single prefix because that is the
    shape both pages already use for everything else — `an_chart_kind_1`, `ck_filter_a1b2` —
    and a chart panel is not the place to introduce a second convention.

    `created` is what makes a reset exact. A prefix sweep would have to guess whether
    `an_chart_kind_11` belongs to message 1 or message 11, and the function pickers are
    named after columns and so cannot be listed in advance; recording what was actually
    handed out this run answers both.
    """

    prefix: str
    suffix: str
    created: set[str] = field(default_factory=set)

    def of(self, name: str) -> str:
        key = f"{self.prefix}_{name}{self.suffix}"
        self.created.add(key)
        return key

    def owned(self) -> set[str]:
        """Every key this panel could hold — the ones named this run, and the fixed ones,
        which may be sitting in session state from a run where they were shown."""
        return self.created | {f"{self.prefix}_{name}{self.suffix}" for name in CONTROL_NAMES}


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------


def render_data_controls(
    frame: pd.DataFrame, current: charts.ChartChoices, kinds: list[str], keys: ChartKeys
) -> charts.ChartChoices | None:
    """The **Data** tab: what the chart is of.

    Returns:
        The choices as they now stand, or None when nothing is selected to plot — which is
        a state the user can reach by emptying the Values box, and is not an error.
    """
    chosen_kind = st.selectbox(
        "Chart type",
        options=kinds,
        index=kinds.index(current.kind) if current.kind in kinds else 0,
        format_func=lambda kind: charts.CHART_LABELS.get(kind, kind),
        key=keys.of("kind"),
        help="Only the types that suit this result are listed — a pie needs a few non-negative parts of one whole, a scatter needs two numeric columns.",
    )

    axis_choices = charts.axis_options(frame, chosen_kind)
    _drop_stale_selection(keys.of("x"), axis_choices)
    chosen_x = st.selectbox(
        "X axis" if chosen_kind != charts.CHART_PIE else "Slices",
        options=axis_choices,
        index=axis_choices.index(current.x) if current.x in axis_choices else 0,
        key=keys.of("x"),
        help="The category or date the values are grouped along. On a horizontal bar this runs up the side instead.",
    )

    aggregate = st.toggle(
        "Aggregate values by X axis",
        value=current.aggregate_by_x,
        key=keys.of("agg"),
        help="Turn on when several rows share the same X value — one row per invoice, charted as a monthly total. Off, each row is plotted as it stands.",
    )

    chosen_values = _render_value_picker(frame, current, aggregate, keys)
    aggregations = (
        _render_function_pickers(frame, current, chosen_values, keys) if aggregate else []
    )
    chosen_split = _render_split_picker(frame, current, chosen_x, chosen_values, keys)

    if not chosen_values or (aggregate and not aggregations):
        return None

    settled = charts.ChartChoices(
        kind=chosen_kind,
        x=chosen_x,
        measures=[] if aggregate else list(chosen_values),
        colour=None if chosen_split == NO_SPLIT else chosen_split,
        aggregate_by_x=aggregate,
        aggregations=aggregations,
    )
    if chosen_kind == charts.CHART_COMBO:
        settled.line_measures, settled.secondary_axis = _render_combo_controls(
            frame, current, settled, keys
        )
    return settled


def _render_value_picker(
    frame: pd.DataFrame, current: charts.ChartChoices, aggregate: bool, keys: ChartKeys
) -> list[str]:
    """The Values box, whose options widen the moment aggregation is switched on.

    Plotted as they stand, values have to be numbers. Counted or ranked, any column is a
    number — which is how "how many records per department" becomes a chart of a result
    that holds no numeric column at all.
    """
    options = charts.value_options(frame, aggregated=aggregate)
    # The two modes keep their measures in different fields, so the box has to read whichever
    # one matches the toggle as it stands, not the one the stored chart was built with.
    aggregated_columns = _unique([entry.column for entry in current.aggregations])
    stored = aggregated_columns if aggregate else list(current.measures)
    # Falling back to the other field is what makes flipping the toggle keep the selection
    # instead of emptying the box: the columns are the same question either way, and the
    # options list drops any the new mode can't offer.
    stored = stored or (list(current.measures) if aggregate else aggregated_columns)
    _drop_stale_selections(keys.of("y"), options)
    return st.multiselect(
        "Values",
        options=options,
        default=[name for name in stored if name in options],
        key=keys.of("y"),
        help="The columns to plot. Pick several to compare them side by side — types that can only draw one will use the first.",
    )


def _render_function_pickers(
    frame: pd.DataFrame, current: charts.ChartChoices, columns: list[str], keys: ChartKeys
) -> list[charts.Aggregation]:
    """One "what to compute" box per chosen column, side by side.

    Several functions on one column is the useful case rather than an exotic one: the sum
    and the count of the same amount, one as bars and one as the line, is the shape most
    combo charts want.
    """
    if not columns:
        return []

    st.caption("What to compute for each value:")
    aggregations: list[charts.Aggregation] = []
    slots = st.columns(min(len(columns), FUNCTION_COLUMNS))
    for position, column in enumerate(columns):
        with slots[position % len(slots)]:
            options = charts.aggregation_options(frame, column)
            if not options:
                continue
            stored = [
                entry.function
                for entry in current.aggregations
                if entry.column == column and entry.function in options
            ]
            _drop_stale_selections(keys.of(f"fn_{column}"), options)
            picked = st.multiselect(
                charts.pretty_name(column),
                options=options,
                default=stored or [charts.default_aggregation(frame, column)],
                format_func=lambda name: charts.AGGREGATION_LABELS.get(name, name),
                key=keys.of(f"fn_{column}"),
                help=f"How to combine the '{column}' values that share an X value. Pick more than one to draw them as separate series.",
            )
            aggregations.extend(charts.Aggregation(column=column, function=name) for name in picked)
    return aggregations


def _render_split_picker(
    frame: pd.DataFrame,
    current: charts.ChartChoices,
    chosen_x: str,
    chosen_values: list[str],
    keys: ChartKeys,
) -> str:
    """The legend picker, which can't offer a column already spent on an axis or a value."""
    # Read from the widgets rather than from `current`, which is a run behind them: moving
    # the x axis onto the column currently used as the legend would otherwise leave the
    # legend holding a value its own option list no longer offers.
    live_x = st.session_state.get(keys.of("x"), chosen_x)
    live_values = st.session_state.get(keys.of("y"), chosen_values)
    splits = [NO_SPLIT, *(name for name in frame.columns if name != live_x and name not in live_values)]

    _drop_stale_selection(keys.of("colour"), splits)
    return st.selectbox(
        "Colour / legend",
        options=splits,
        index=splits.index(current.colour) if current.colour in splits else 0,
        key=keys.of("colour"),
        help="Splits each value into one coloured series per value of this column — e.g. status, giving a legend of statuses.",
    )


def _render_combo_controls(
    frame: pd.DataFrame, current: charts.ChartChoices, settled: charts.ChartChoices, keys: ChartKeys
) -> tuple[list[str], bool]:
    """Which of a combo's measures are lines, and whether they get their own axis.

    The picker lists the measures by the names they will carry in the legend, which under
    aggregation are not the column names — 'Sum of amount', not 'amount'. Asking
    `charts.series_names` rather than building the list here is what keeps the two in step.
    """
    names = charts.series_names(frame, settled)
    _drop_stale_selections(keys.of("line"), names)
    lines = st.multiselect(
        "Draw as line",
        options=names,
        default=[name for name in current.line_measures if name in names],
        key=keys.of("line"),
        help="The measures drawn as a line over the bars. Leave empty and the last one is used.",
    )
    secondary = st.checkbox(
        "Put the line on a second Y axis",
        value=current.secondary_axis,
        key=keys.of("axis2"),
        help="Give the line its own scale on the right. Worth it whenever the line and the bars are in different units — a count of records against the amount at stake.",
    )
    return lines, secondary


# --------------------------------------------------------------------------------------
# Style
# --------------------------------------------------------------------------------------


def render_style_controls(
    frame: pd.DataFrame,
    chosen: charts.ChartChoices,
    current: charts.ChartStyle,
    keys: ChartKeys,
    *,
    title_placeholder: str = "",
) -> charts.ChartStyle:
    """The **Style** tab: what the chart looks like.

    Controls that this particular chart couldn't honour are left out rather than shown
    doing nothing — there is no stacking to choose with one series, and no order to choose
    along a time axis. What is left out keeps whatever it was, so switching a bar to a line
    and back doesn't quietly discard the stacking that was set.
    """
    # An empty box means "use the title derived from the columns", so the automatic title
    # keeps up as the data controls change. Showing the current one as a placeholder says
    # what will be used without pinning it in place.
    title = st.text_input(
        "Title",
        value=current.title or "",
        placeholder=title_placeholder,
        key=keys.of("title"),
        help="Leave blank to keep the title built from the column names.",
    )

    palettes = charts.available_palettes(frame, chosen)
    _drop_stale_selection(keys.of("palette"), palettes)
    left, right = st.columns(2)
    with left:
        palette = st.selectbox(
            "Colours",
            options=palettes,
            index=palettes.index(current.palette) if current.palette in palettes else 0,
            format_func=lambda name: charts.PALETTE_LABELS.get(name, name),
            key=keys.of("palette"),
            help="The colour sequence the series are drawn in. 'Colour-blind safe' is the one to pick for a chart other people will read.",
        )
    with right:
        series = current.series
        if charts.supports_series_layout(frame, chosen):
            layouts = list(charts.SERIES_LABELS)
            series = st.selectbox(
                "Series",
                options=layouts,
                index=layouts.index(current.series) if current.series in layouts else 0,
                format_func=lambda name: charts.SERIES_LABELS.get(name, name),
                key=keys.of("series"),
                help="How the series share the space: side by side to compare them, stacked for the total, 100% stacked to compare their shares.",
            )

    show_values = st.checkbox(
        "Show values on the chart",
        value=current.show_values,
        key=keys.of("values"),
        help="Prints each point's own number on the chart. Best kept off when there are many bars, where the labels start to overlap.",
    )
    show_legend = st.checkbox(
        "Show legend",
        value=current.show_legend,
        key=keys.of("legend"),
        help="Turn off when there is only one series and the legend is just naming it again.",
    )

    sort = current.sort
    top_n = current.top_n
    ceiling = charts.top_n_ceiling(frame, chosen)
    order_column, top_column = st.columns(2)
    with order_column:
        if charts.supports_sorting(frame, chosen):
            orders = list(charts.SORT_LABELS)
            sort = st.selectbox(
                "Sort",
                options=orders,
                index=orders.index(current.sort) if current.sort in orders else 0,
                format_func=lambda name: charts.SORT_LABELS.get(name, name),
                key=keys.of("sort"),
                help="'Automatic' is biggest-first for a single run of bars and chronological over time.",
            )
    with top_column:
        if ceiling:
            automatic = charts.default_top_n(frame, chosen)
            picked = st.slider(
                "Show top",
                min_value=1,
                max_value=ceiling,
                value=min(current.top_n or automatic, ceiling),
                key=keys.of("top"),
                help="How many categories to draw. The rest stay in the table below.",
            )
            # Left as None while it matches the automatic cap, so the cap can keep moving
            # with the chart type instead of being frozen the moment this panel is opened.
            top_n = None if picked == automatic else picked

    height = st.slider(
        "Height",
        min_value=charts.MIN_CHART_HEIGHT,
        max_value=charts.MAX_CHART_HEIGHT,
        value=current.height,
        step=30,
        key=keys.of("height"),
        help="Taller is easier to read when there are many categories, especially on a horizontal bar.",
    )

    return charts.ChartStyle(
        title=title.strip() or None,
        palette=palette,
        series=series,
        show_values=show_values,
        show_legend=show_legend,
        sort=sort,
        top_n=top_n,
        height=height,
    )


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def seed_choices(
    frame: pd.DataFrame | None, question: str = ""
) -> tuple[charts.ChartChoices | None, list[str]]:
    """The chart a result gets before anyone customizes it.

    `charts.choose_chart` picks the columns; this adds the one thing a chart a *person* asked
    for wants that an automatic one doesn't — **aggregation on by default**. A result with one
    row per invoice is the normal case, and plotting it raw draws a bar per invoice, so the
    user's first sight of their chart should already be the monthly totals.

    The default lives here rather than on `ChartChoices` deliberately. Off is what every
    stored spec written before the field existed reads back as, and what
    `analyst.pipeline`'s automatic chart keeps — its SQL has already grouped, so aggregating
    it again would count pre-counted rows and answer 1 per group with no error to show for it.

    Scatter is the exception: its x axis is a second measure, and grouping a continuous
    number turns the relationship it exists to show into noise.
    """
    chosen, warnings = charts.choose_chart(frame, question)
    if chosen is None or chosen.kind == charts.CHART_SCATTER:
        return chosen, warnings

    chosen.aggregate_by_x = True
    chosen.aggregations = [
        charts.Aggregation(column=name, function=charts.default_aggregation(frame, name))
        for name in chosen.measures
    ]
    return chosen, warnings


def figure_title(figure: go.Figure | None) -> str:
    """The title on a chart right now, used as the Title box's placeholder."""
    try:
        return str(figure.layout.title.text or "")
    except AttributeError:
        # A figure without a title layout is not worth losing the whole panel over.
        logger.debug("Chart figure had no readable title.", exc_info=True)
        return ""


def clear_controls(keys: ChartKeys) -> None:
    """Forgets every widget this panel owns, so it reopens on whatever is drawn next."""
    for key in keys.owned():
        st.session_state.pop(key, None)


def render_reset_button(keys: ChartKeys, *, help_text: str) -> bool:
    """The "Reset to automatic" button. True when pressed, with the widgets already cleared.

    Clearing here rather than leaving it to the caller means the caller cannot forget: the
    stored choices and the widgets holding them have to go together, or the panel reopens
    showing selections the chart no longer reflects.

    Drawn last, after every other control, so that `keys` has by then been handed every name
    this panel uses and the clearing is complete.
    """
    if not st.button(
        "Reset to automatic",
        key=f"{keys.prefix}_reset{keys.suffix}",
        icon=":material/restart_alt:",
        help=help_text,
    ):
        return False
    clear_controls(keys)
    return True


def _unique(names: list[str]) -> list[str]:
    """The names in the order given, each once — `dict` keeps insertion order."""
    return list(dict.fromkeys(names))


def _drop_stale_selection(key: str, options: list[str]) -> None:
    """Forgets a stored selection the options no longer contain.

    The panel's option lists depend on each other — the legend can't offer the column now
    on the x axis, and the single-colour palette disappears the moment there are two series
    — so a selection made a run ago can stop being offered. Dropping it lets the widget
    fall back to its default instead of holding a value it can't show.
    """
    if key in st.session_state and st.session_state[key] not in options:
        del st.session_state[key]


def _drop_stale_selections(key: str, options: list[str]) -> None:
    """The same, for a multiselect, keeping the choices that are still on offer.

    Turning aggregation off narrows the Values box from every column to the numeric ones,
    so a text column picked to be counted has to be dropped — but the numbers picked
    alongside it are still perfectly valid and are not worth making the user pick again.
    """
    stored = st.session_state.get(key)
    if not isinstance(stored, list):
        return
    kept = [name for name in stored if name in options]
    if len(kept) != len(stored):
        st.session_state[key] = kept
