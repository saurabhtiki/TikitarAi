"""The set-level picture: how every criteria fared, side by side (requirement 6.5).

One chart for the whole set rather than one per criteria. A criteria's own rows are already
on screen as a table, and a chart of them repeated down the page compares nothing — what an
exception report wants is "which rules are breached, and how badly", which is a single
stacked bar with one bar per rule.

Two charts are built from the same counts because they answer different questions and
neither substitutes for the other: the absolute one says *how many* records breached (a bar
of 300 breaches matters more than a bar of 3), and the percentage one says *how badly* each
rule is breached (3 of 4 is a worse rule than 300 of 90,000). They sit side by side.

`combined_chart` puts both of them into **one** figure, for the report. A `PinnedItem`
holds a single figure, and pinning only the absolute one would have left the report making
exactly the comparison the percentage chart exists to prevent — so the report gets the pair,
as two panels sharing one set of criteria labels.

No Streamlit here, matching the rest of `checks/` — the frames and the figures are all
buildable and assertable without `AppTest`.

The counts come from each criteria's `SavedRun`, which precomputed them at Save time. They
are deliberately *not* recounted from the rows: this function runs on every rerun of the
Design tab, and rescanning every result on every keystroke is exactly the cost `SavedRun`
exists to avoid.
"""

import logging

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from checks.model import Check

logger = logging.getLogger(__name__)

COLUMN_CRITERIA = "criteria"
COLUMN_PASSED = "met"
COLUMN_BREACHED = "breached"
COLUMN_RECORDS = "records"

LABEL_PASSED = "Met (Yes)"
LABEL_BREACHED = "Breached (No)"

# Blue for met, red for breached. The blue is `charts.PALETTE_SINGLE`'s own colour, so a
# summary chart sits beside a chat chart without clashing; the red is the Vega/Plotly
# qualitative red the same family uses for a negative series.
COLOUR_PASSED = "#4C78A8"
COLOUR_BREACHED = "#E45756"

# A bar per criteria plus room for the axis and legend. A fixed height squashes ten rules
# into a smear and leaves a single rule floating in white space.
ROW_HEIGHT = 46
BASE_HEIGHT = 140
MIN_HEIGHT = 240
MAX_HEIGHT = 900

COUNT_TITLE = "Records by criteria"
PERCENT_TITLE = "Share of records by criteria"

COUNT_AXIS = "Records"
PERCENT_AXIS = "Share of records (%)"

# Thousands separators on the counts, whole percentages on the shares — a bar labelled
# "66.7%" says nothing "67%" doesn't, and two decimals inside a narrow segment don't fit.
COUNT_TEMPLATE = "%{x:,}"
PERCENT_TEMPLATE = "%{x:.0f}%"


def counts_frame(checks: list[Check]) -> pd.DataFrame:
    """One row per saved criteria: how many records met the rule and how many breached it.

    Criteria without a saved run are skipped rather than shown as empty bars — nothing that
    has not been saved is in the report, and a bar for one would promise a report item that
    isn't there.

    The frame is returned with its columns declared even when empty, so callers can rely on
    the shape without testing for it.
    """
    rows = []
    for check in checks:
        run = check.saved_run
        if run is None:
            continue
        rows.append(
            {
                COLUMN_CRITERIA: check.display_name(),
                COLUMN_PASSED: run.pass_count,
                COLUMN_BREACHED: run.fail_count,
                COLUMN_RECORDS: run.pass_count + run.fail_count,
            }
        )
    return pd.DataFrame(rows, columns=[COLUMN_CRITERIA, COLUMN_PASSED, COLUMN_BREACHED, COLUMN_RECORDS])


def _height(frame: pd.DataFrame) -> int:
    return max(MIN_HEIGHT, min(MAX_HEIGHT, BASE_HEIGHT + ROW_HEIGHT * len(frame)))


def _bars(frame: pd.DataFrame, passed, breached, template: str, *, show_legend: bool = True) -> list[go.Bar]:
    """The two traces a panel is made of, in stacking order.

    `passed` and `breached` are passed in rather than read off the frame because the panels
    plot different numbers — counts and shares — from the same rows.

    `show_legend` is False for the second panel of `combined_chart`: its traces carry the
    same two names as the first, and a legend listing "Met (Yes)" twice invites the reader to
    look for a difference between them.
    """
    return [
        go.Bar(
            y=frame[COLUMN_CRITERIA],
            x=values,
            name=name,
            # Grouped so the one legend still toggles the matching series in both panels.
            legendgroup=name,
            showlegend=show_legend,
            orientation="h",
            marker_color=colour,
            texttemplate=template,
            textposition="inside",
            insidetextanchor="middle",
            # Plotly hides a label that doesn't fit its segment; showing it outside instead
            # keeps a small count readable rather than silently dropping it.
            cliponaxis=False,
        )
        for name, values, colour in (
            (LABEL_PASSED, passed, COLOUR_PASSED),
            (LABEL_BREACHED, breached, COLOUR_BREACHED),
        )
    ]


def _shares(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Each criteria's passes and breaches as percentages of its own record count.

    Computed here rather than left to Plotly's `barnorm`, because the data labels have to
    read as percentages too — `barnorm` normalises the drawing but not the values a text
    template sees, which would print raw counts on a percentage axis.

    A criteria whose run counted no records at all divides by zero; those bars come back as
    zero rather than NaN, so the criteria still appears in the same order as its neighbours.
    """
    totals = frame[COLUMN_RECORDS].replace(0, pd.NA)
    return (
        (frame[COLUMN_PASSED] / totals * 100).fillna(0.0),
        (frame[COLUMN_BREACHED] / totals * 100).fillna(0.0),
    )


def _stacked(frame: pd.DataFrame, passed, breached, title: str, axis_title: str, template: str) -> go.Figure:
    """One panel on its own: horizontal, stacked, labelled."""
    figure = go.Figure(_bars(frame, passed, breached, template))

    figure.update_layout(
        barmode="stack",
        title=title,
        xaxis_title=axis_title,
        yaxis_title=None,
        height=_height(frame),
        margin=dict(l=10, r=10, t=60, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        uniformtext=dict(mode="hide", minsize=10),
    )
    # Plotly draws the first category at the bottom of a horizontal bar chart; reversing the
    # axis lists the criteria in the order they were written down the page.
    figure.update_yaxes(autorange="reversed")
    return figure


def count_chart(frame: pd.DataFrame) -> go.Figure | None:
    """Absolute counts — how many records each rule was tested against, and how many failed.

    Returns None for an empty frame rather than an empty chart, so the caller shows its own
    "nothing saved yet" message instead of an axis with no bars.
    """
    if frame.empty:
        return None
    return _stacked(
        frame,
        frame[COLUMN_PASSED],
        frame[COLUMN_BREACHED],
        COUNT_TITLE,
        COUNT_AXIS,
        COUNT_TEMPLATE,
    )


def percent_chart(frame: pd.DataFrame) -> go.Figure | None:
    """The same bars normalised to 100%, so rules of very different sizes compare."""
    if frame.empty:
        return None

    passed, breached = _shares(frame)
    figure = _stacked(frame, passed, breached, PERCENT_TITLE, PERCENT_AXIS, PERCENT_TEMPLATE)
    figure.update_xaxes(range=[0, 100])
    return figure


def combined_chart(frame: pd.DataFrame) -> go.Figure | None:
    """Both panels in one figure — what the report gets.

    A `PinnedItem` holds exactly one figure, so the report has to choose. Pinning the counts
    alone would leave a reader comparing "3 breaches" against "300 breaches" with no way to
    see that the first rule failed three records out of four and the second three hundred out
    of ninety thousand — which is the comparison the shares panel exists to make. So both go,
    side by side, sharing one column of criteria labels.

    Titles sit on the panels rather than over the figure: each axis means something different,
    and one heading spanning both would have to describe neither.
    """
    if frame.empty:
        return None

    figure = make_subplots(
        rows=1,
        cols=2,
        shared_yaxes=True,
        horizontal_spacing=0.06,
        subplot_titles=(COUNT_TITLE, PERCENT_TITLE),
    )

    for bar in _bars(frame, frame[COLUMN_PASSED], frame[COLUMN_BREACHED], COUNT_TEMPLATE):
        figure.add_trace(bar, row=1, col=1)

    passed, breached = _shares(frame)
    for bar in _bars(frame, passed, breached, PERCENT_TEMPLATE, show_legend=False):
        figure.add_trace(bar, row=1, col=2)

    figure.update_layout(
        barmode="stack",
        height=_height(frame),
        margin=dict(l=10, r=10, t=80, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.08, xanchor="left", x=0),
        uniformtext=dict(mode="hide", minsize=10),
    )
    figure.update_yaxes(autorange="reversed")
    figure.update_xaxes(title_text=COUNT_AXIS, row=1, col=1)
    figure.update_xaxes(title_text=PERCENT_AXIS, range=[0, 100], row=1, col=2)
    return figure


def totals(frame: pd.DataFrame) -> tuple[int, int, int]:
    """`(criteria, records, breaches)` across the whole set, for the headline line."""
    if frame.empty:
        return 0, 0, 0
    return len(frame), int(frame[COLUMN_RECORDS].sum()), int(frame[COLUMN_BREACHED].sum())
