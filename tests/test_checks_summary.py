"""The set-level summary chart (requirement 6.5).

Pure functions over saved runs, so nothing here needs `AppTest` or a provider — the figures
are built and asserted directly, which is the point of keeping `checks/summary.py` free of
Streamlit.
"""

import pandas as pd

from checks import summary
from checks.model import Check, freeze_run

BONUS = pd.DataFrame(
    {
        "employee": ["Ana", "Bo", "Cy"],
        "criteria_result": [4.0, 12.0, 9.0],
        "criteria_met": ["Yes", "No", "No"],
    }
)

PF = pd.DataFrame(
    {
        "employee": ["Ana", "Bo"],
        "criteria_result": [1.0, 2.0],
        "criteria_met": ["Yes", "Yes"],
    }
)


def _saved(name: str, frame: pd.DataFrame) -> Check:
    check = Check(name=name)
    check.saved_run = freeze_run("SELECT 1", frame)
    return check


def _both() -> list[Check]:
    return [_saved("Bonus cap", BONUS), _saved("PF match", PF)]


class TestCounts:
    def test_one_row_per_saved_criteria(self):
        frame = summary.counts_frame(_both())
        assert list(frame[summary.COLUMN_CRITERIA]) == ["Bonus cap", "PF match"]
        assert list(frame[summary.COLUMN_BREACHED]) == [2, 0]
        assert list(frame[summary.COLUMN_PASSED]) == [1, 2]

    def test_an_unsaved_criteria_is_left_out(self):
        """Nothing unsaved is in the report, and a bar for it would promise an item that
        isn't there."""
        frame = summary.counts_frame([_saved("Bonus cap", BONUS), Check(name="Untested")])
        assert list(frame[summary.COLUMN_CRITERIA]) == ["Bonus cap"]

    def test_an_empty_set_still_has_its_columns(self):
        """Callers shouldn't have to test for the shape before reading it."""
        frame = summary.counts_frame([])
        assert frame.empty
        assert summary.COLUMN_BREACHED in frame.columns

    def test_the_totals_add_up_across_the_set(self):
        assert summary.totals(summary.counts_frame(_both())) == (2, 5, 2)

    def test_an_empty_set_totals_zero_rather_than_raising(self):
        assert summary.totals(summary.counts_frame([])) == (0, 0, 0)


class TestCharts:
    def test_the_count_chart_stacks_met_against_breached(self):
        figure = summary.count_chart(summary.counts_frame(_both()))
        assert figure.layout.barmode == "stack"
        assert [trace.name for trace in figure.data] == [summary.LABEL_PASSED, summary.LABEL_BREACHED]
        assert list(figure.data[1].x) == [2, 0]

    def test_it_is_horizontal_and_reads_top_down(self):
        """Plotly draws the first category at the bottom of a horizontal bar chart, which
        would list the criteria in the reverse of the order they were written."""
        figure = summary.count_chart(summary.counts_frame(_both()))
        assert all(trace.orientation == "h" for trace in figure.data)
        assert figure.layout.yaxis.autorange == "reversed"

    def test_every_bar_carries_its_value(self):
        figure = summary.count_chart(summary.counts_frame(_both()))
        assert all(trace.texttemplate for trace in figure.data)

    def test_it_grows_with_the_number_of_criteria(self):
        """A fixed height squashes ten rules into a smear."""
        one = summary.count_chart(summary.counts_frame([_saved("Bonus cap", BONUS)]))
        many = summary.count_chart(summary.counts_frame(_both() * 5))
        assert many.layout.height > one.layout.height

    def test_the_percent_chart_normalises_each_rule_to_a_hundred(self):
        """Absolute counts alone can't compare 2 of 3 against 2 of 90,000."""
        figure = summary.percent_chart(summary.counts_frame(_both()))
        breached = list(figure.data[1].x)
        assert round(breached[0]) == 67
        assert breached[1] == 0
        assert tuple(figure.layout.xaxis.range) == (0, 100)

    def test_its_labels_are_percentages_not_raw_counts(self):
        """The shares are computed here rather than left to Plotly's `barnorm`, which
        normalises the drawing but not the values a text template sees."""
        figure = summary.percent_chart(summary.counts_frame(_both()))
        assert all("%" in trace.texttemplate for trace in figure.data)

    def test_a_criteria_with_no_records_is_drawn_as_zero_not_dropped(self):
        """Dividing by its total is a divide by zero; losing the bar would also put the two
        charts out of step with each other."""
        empty = Check(name="Nothing matched")
        empty.saved_run = freeze_run("SELECT 1", BONUS.head(0))
        figure = summary.percent_chart(summary.counts_frame([empty]))
        assert list(figure.data[1].x) == [0.0]

    def test_neither_chart_is_drawn_for_an_empty_set(self):
        """None rather than an empty axis, so the page shows its own message instead."""
        frame = summary.counts_frame([])
        assert summary.count_chart(frame) is None
        assert summary.percent_chart(frame) is None


class TestCombined:
    """The one figure the report gets, because a pinned item holds only one."""

    def test_both_panels_are_in_it(self):
        """Counts alone would leave the report comparing 3 breaches against 300 with no way
        to see that the first was 3 of 4 — which is what the shares panel is for."""
        figure = summary.combined_chart(summary.counts_frame(_both()))
        assert [trace.xaxis for trace in figure.data] == ["x", "x", "x2", "x2"]
        assert list(figure.data[1].x) == [2, 0]
        assert round(list(figure.data[3].x)[0]) == 67

    def test_the_legend_names_each_series_once(self):
        """Four traces, two names. A legend listing "Met (Yes)" twice invites the reader to
        look for a difference between them."""
        figure = summary.combined_chart(summary.counts_frame(_both()))
        assert [trace.showlegend for trace in figure.data] == [True, True, False, False]
        assert all(trace.legendgroup for trace in figure.data)

    def test_the_share_panel_keeps_its_own_scale(self):
        figure = summary.combined_chart(summary.counts_frame(_both()))
        assert tuple(figure.layout.xaxis2.range) == (0, 100)
        assert figure.layout.xaxis.range is None

    def test_it_stacks_and_reads_top_down_like_the_panels_on_screen(self):
        figure = summary.combined_chart(summary.counts_frame(_both()))
        assert figure.layout.barmode == "stack"
        assert figure.layout.yaxis.autorange == "reversed"

    def test_nothing_is_drawn_for_an_empty_set(self):
        assert summary.combined_chart(summary.counts_frame([])) is None
