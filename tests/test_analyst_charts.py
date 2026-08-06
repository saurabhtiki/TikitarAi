"""Deterministic chart selection from a result set (requirement 6.2)."""

import pandas as pd
import pytest

from analyst import charts


def chart_type(figure) -> str:
    """The Plotly trace type actually drawn — the only thing that proves the choice."""
    return figure.data[0].type


class TestChartSelection:
    def test_a_label_and_a_measure_become_a_bar(self):
        frame = pd.DataFrame({"department": ["Sales", "Ops"], "total": [100, 200]})
        figure, warnings = charts.build_chart(frame, "total by department")
        assert chart_type(figure) == "bar"
        assert warnings == []

    def test_a_date_and_a_measure_become_a_line(self):
        frame = pd.DataFrame(
            {"month": pd.to_datetime(["2026-01-01", "2026-02-01"]), "revenue": [10, 20]}
        )
        figure, _ = charts.build_chart(frame, "revenue over time")
        assert chart_type(figure) == "scatter"  # px.line draws a scatter trace with lines
        assert figure.data[0].mode is not None and "lines" in figure.data[0].mode

    def test_two_measures_become_a_scatter(self):
        frame = pd.DataFrame({"basic": [1.0, 2.0], "tax": [0.1, 0.2]})
        figure, _ = charts.build_chart(frame, "basic against tax")
        assert chart_type(figure) == "scatter"

    def test_a_share_question_over_few_categories_becomes_a_pie(self):
        frame = pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 20, 30]})
        figure, _ = charts.build_chart(frame, "what share of sales by region")
        assert chart_type(figure) == "pie"

    def test_a_share_question_over_many_categories_stays_a_bar(self):
        """A pie with twenty slices is unreadable; the bar still answers the question."""
        frame = pd.DataFrame({"region": [f"r{n}" for n in range(20)], "sales": range(20)})
        figure, _ = charts.build_chart(frame, "share of sales by region")
        assert chart_type(figure) == "bar"

    def test_a_share_question_with_negative_values_stays_a_bar(self):
        """Parts of a whole can't be negative — a pie would silently misrepresent it."""
        frame = pd.DataFrame({"region": ["N", "S"], "profit": [10, -5]})
        figure, _ = charts.build_chart(frame, "share of profit by region")
        assert chart_type(figure) == "bar"

    def test_bars_are_sorted_by_size(self):
        frame = pd.DataFrame({"department": ["Ops", "Sales"], "total": [100, 300]})
        figure, _ = charts.build_chart(frame, "total by department")
        assert list(figure.data[0].x) == ["Sales", "Ops"]


class TestSeveralMeasures:
    """A `min`/`max`/`avg` breakdown is one label column and three measures. Plotting only
    the first answers a third of the question while looking like the whole of it."""

    @staticmethod
    def _salary_spread() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "department": ["Ops", "Sales"],
                "min_basic_salary": [10.0, 20.0],
                "max_basic_salary": [90.0, 80.0],
                "avg_basic_salary": [50.0, 45.0],
            }
        )

    def test_every_measure_becomes_its_own_series(self):
        figure, _ = charts.build_chart(self._salary_spread(), "plot department wise min, max, average")
        assert len(figure.data) == 3

    def test_the_series_are_named_after_their_columns(self):
        figure, _ = charts.build_chart(self._salary_spread(), "plot department wise min, max, average")
        assert {trace.name for trace in figure.data} == {
            "min_basic_salary",
            "max_basic_salary",
            "avg_basic_salary",
        }

    def test_the_bars_are_grouped_not_stacked(self):
        """Stacking a min on top of a max would produce a number that means nothing."""
        figure, _ = charts.build_chart(self._salary_spread(), "plot it")
        assert figure.layout.barmode == "group"

    def test_the_sql_ordering_is_left_alone_with_several_measures(self):
        """With one measure, biggest-first is the readable order. With three, re-sorting by
        whichever came back first would silently override the query's own ORDER BY."""
        figure, _ = charts.build_chart(self._salary_spread(), "plot it")
        assert list(figure.data[0].x) == ["Ops", "Sales"]

    def test_a_share_question_with_several_measures_is_not_a_pie(self):
        """Three measures aren't parts of one whole."""
        figure, _ = charts.build_chart(self._salary_spread(), "share by department")
        assert chart_type(figure) == "bar"

    def test_too_many_measures_are_capped_and_reported(self):
        frame = pd.DataFrame({"label": ["a", "b"], **{f"m{n}": [n, n] for n in range(12)}})
        figure, warnings = charts.build_chart(frame, "plot it")
        assert len(figure.data) == charts.MAX_MEASURES
        assert any("measures" in warning for warning in warnings)

    def test_several_measures_over_time_all_get_a_line(self):
        frame = pd.DataFrame(
            {
                "month": pd.to_datetime(["2026-01-01", "2026-02-01"]),
                "revenue": [10, 20],
                "cost": [5, 8],
            }
        )
        figure, _ = charts.build_chart(frame, "revenue and cost over time")
        assert len(figure.data) == 2

    def test_a_label_over_time_keeps_one_measure_and_says_so(self):
        """The colour axis is already spent on one line per category, so a second measure
        would have to share it and the two would be indistinguishable."""
        frame = pd.DataFrame(
            {
                "month": pd.to_datetime(["2026-01-01", "2026-01-01"]),
                "region": ["N", "S"],
                "revenue": [10, 20],
                "cost": [5, 8],
            }
        )
        figure, warnings = charts.build_chart(frame, "revenue by region over time")
        assert len(figure.data) == 2  # one line per region, not per measure
        assert any("Revenue only" in warning for warning in warnings)


class TestTwoLabelColumns:
    """A second label column (e.g. `status` beside `department`) becomes the legend split,
    not a dropped column — this is what `SELECT department, status, count(*) ... GROUP BY
    department, status` should chart as."""

    @staticmethod
    def _department_status_counts() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "department": ["Sales", "Sales", "Ops", "Ops"],
                "status": ["Present", "Absent", "Present", "Absent"],
                "count": [180, 20, 150, 10],
            }
        )

    def test_the_second_label_becomes_the_colour(self):
        figure, warnings = charts.build_chart(self._department_status_counts(), "count by status")
        assert chart_type(figure) == "bar"
        assert {trace.name for trace in figure.data} == {"Present", "Absent"}
        assert warnings == []

    def test_the_first_label_stays_the_x_axis(self):
        figure, _ = charts.build_chart(self._department_status_counts(), "count by status")
        assert set(figure.data[0].x) == {"Sales", "Ops"}

    def test_the_legend_is_titled_after_the_second_column(self):
        figure, _ = charts.build_chart(self._department_status_counts(), "count by status")
        assert figure.layout.legend.title.text == "Status"

    def test_the_title_names_both_columns(self):
        figure, _ = charts.build_chart(self._department_status_counts(), "count by status")
        assert figure.layout.title.text == "Count by Department and Status"

    def test_the_bars_are_grouped_not_stacked(self):
        figure, _ = charts.build_chart(self._department_status_counts(), "count by status")
        assert figure.layout.barmode == "group"

    def test_two_labels_with_several_measures_only_charts_the_first_label(self):
        """Colour is already spent, if used, on one dimension — with several measures too,
        that would need a second colour axis, so the extra label is dropped and reported."""
        frame = pd.DataFrame(
            {
                "department": ["Sales", "Ops"],
                "status": ["Present", "Absent"],
                "min_basic_salary": [10.0, 20.0],
                "max_basic_salary": [90.0, 80.0],
            }
        )
        figure, warnings = charts.build_chart(frame, "plot it")
        assert chart_type(figure) == "bar"
        assert len(figure.data) == 2  # one trace per measure, not per status
        assert any("Status" in warning for warning in warnings)

    def test_a_third_label_column_is_dropped_and_reported(self):
        frame = pd.DataFrame(
            {
                "department": ["Sales", "Ops"],
                "status": ["Present", "Absent"],
                "shift": ["Day", "Night"],
                "count": [180, 150],
            }
        )
        figure, warnings = charts.build_chart(frame, "count by status")
        assert chart_type(figure) == "bar"
        assert any("Shift" in warning for warning in warnings)

    def test_too_many_values_to_colour_by_falls_back_to_a_plain_bar(self):
        frame = pd.DataFrame(
            {
                "department": ["Sales"] * 40,
                "employee": [f"e{n}" for n in range(40)],
                "count": range(40),
            }
        )
        figure, warnings = charts.build_chart(frame, "count by department")
        assert chart_type(figure) == "bar"
        assert figure.layout.legend.title.text != "Employee"
        assert any("Employee" in warning and "legend" in warning for warning in warnings)

    def test_too_many_x_categories_with_a_colour_split_are_capped(self):
        frame = pd.DataFrame(
            {
                "code": [f"c{n}" for n in range(40) for _ in range(2)],
                "status": ["Present", "Absent"] * 40,
                "count": list(range(80)),
            }
        )
        figure, warnings = charts.build_chart(frame, "count by status")
        assert len(set(figure.data[0].x) | set(figure.data[1].x)) <= charts.MAX_CATEGORIES
        assert any(str(charts.MAX_CATEGORIES) in warning for warning in warnings)


class TestAvailableChartTypes:
    """The picker only offers what this particular result can honestly draw."""

    def test_a_label_and_a_measure_offer_bars_lines_and_areas(self):
        frame = pd.DataFrame({"department": ["Sales", "Ops"], "total": [100, 200]})
        kinds = charts.available_chart_types(frame)
        assert charts.CHART_BAR in kinds
        assert charts.CHART_BAR_HORIZONTAL in kinds
        assert charts.CHART_LINE in kinds
        assert charts.CHART_AREA in kinds

    def test_one_numeric_column_does_not_offer_a_scatter(self):
        """A scatter relates two numbers — with one, there is nothing to relate it to."""
        frame = pd.DataFrame({"department": ["Sales", "Ops"], "total": [100, 200]})
        assert charts.CHART_SCATTER not in charts.available_chart_types(frame)

    def test_two_numeric_columns_offer_a_scatter(self):
        frame = pd.DataFrame({"department": ["Sales", "Ops"], "basic": [1, 2], "tax": [3, 4]})
        assert charts.CHART_SCATTER in charts.available_chart_types(frame)

    def test_a_few_positive_parts_offer_a_pie(self):
        frame = pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 20, 30]})
        assert charts.CHART_PIE in charts.available_chart_types(frame)

    def test_negative_values_do_not_offer_a_pie(self):
        """A slice can't be smaller than nothing — Plotly would draw the absolute value."""
        frame = pd.DataFrame({"region": ["N", "S"], "profit": [10, -5]})
        assert charts.CHART_PIE not in charts.available_chart_types(frame)

    def test_too_many_rows_do_not_offer_a_pie(self):
        frame = pd.DataFrame({"region": [f"r{n}" for n in range(20)], "sales": range(20)})
        assert charts.CHART_PIE not in charts.available_chart_types(frame)

    def test_an_unchartable_result_offers_nothing(self):
        frame = pd.DataFrame({"name": ["Ana"], "city": ["Pune"]})
        assert charts.available_chart_types(frame) == []

    def test_every_offered_type_actually_draws(self):
        """The list is a promise: anything on it must render rather than fail."""
        frame = pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 20, 30], "cost": [5, 8, 9]})
        for kind in charts.available_chart_types(frame):
            figure, _ = charts.render_chart(
                frame, charts.ChartChoices(kind=kind, x="region", measures=["sales"])
            )
            assert figure is not None, kind


class TestRenderChosenChart:
    """The customize path: whatever is picked is what gets drawn."""

    @staticmethod
    def _sales() -> pd.DataFrame:
        return pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 30, 20], "cost": [5, 8, 9]})

    def test_a_horizontal_bar_puts_the_category_up_the_side(self):
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_BAR_HORIZONTAL, x="region", measures=["sales"]),
        )
        assert figure.data[0].orientation == "h"
        assert set(figure.data[0].y) == {"N", "S", "E"}

    def test_a_horizontal_bar_puts_the_biggest_at_the_top(self):
        """Plotly draws a category axis bottom-up, so biggest-first means ascending."""
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_BAR_HORIZONTAL, x="region", measures=["sales"]),
        )
        assert list(figure.data[0].y)[-1] == "S"

    def test_an_area_chart_draws_a_filled_trace(self):
        """`stackgroup` is what fills the area under the line — it's how px.area differs
        from px.line, which draws the same trace type unfilled."""
        figure, _ = charts.render_chart(
            self._sales(), charts.ChartChoices(kind=charts.CHART_AREA, x="region", measures=["sales"])
        )
        assert figure.data[0].stackgroup

    def test_a_line_chart_is_not_filled(self):
        figure, _ = charts.render_chart(
            self._sales(), charts.ChartChoices(kind=charts.CHART_LINE, x="region", measures=["sales"])
        )
        assert not figure.data[0].stackgroup

    def test_a_line_can_be_forced_over_categories(self):
        """Line and area aren't restricted to dates — the user may want the trend anyway."""
        figure, _ = charts.render_chart(
            self._sales(), charts.ChartChoices(kind=charts.CHART_LINE, x="region", measures=["sales"])
        )
        assert "lines" in figure.data[0].mode

    def test_the_chosen_columns_are_the_ones_plotted(self):
        figure, _ = charts.render_chart(
            self._sales(), charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["cost"])
        )
        assert list(figure.data[0].y) == [9, 8, 5]  # cost, biggest-first
        assert list(figure.data[0].x) == ["E", "S", "N"]

    def test_several_chosen_measures_become_several_series(self):
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales", "cost"]),
        )
        assert len(figure.data) == 2

    def test_a_chosen_colour_splits_the_measure(self):
        frame = pd.DataFrame(
            {
                "department": ["Sales", "Sales", "Ops", "Ops"],
                "status": ["Present", "Absent", "Present", "Absent"],
                "count": [180, 20, 150, 10],
            }
        )
        figure, _ = charts.render_chart(
            frame,
            charts.ChartChoices(kind=charts.CHART_BAR, x="status", measures=["count"], colour="department"),
        )
        assert {trace.name for trace in figure.data} == {"Sales", "Ops"}
        assert set(figure.data[0].x) == {"Present", "Absent"}

    def test_a_pie_of_several_measures_uses_the_first_one(self):
        figure, warnings = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_PIE, x="region", measures=["sales", "cost"]),
        )
        assert chart_type(figure) == "pie"
        assert sum(figure.data[0].values) == 60  # sales only

    def test_a_colour_with_several_measures_keeps_the_first_and_says_so(self):
        figure, warnings = charts.render_chart(
            self._sales(),
            charts.ChartChoices(
                kind=charts.CHART_BAR, x="region", measures=["sales", "cost"], colour="region"
            ),
        )
        assert any("Sales only" in warning for warning in warnings)

    def test_a_column_that_is_gone_gives_no_chart_rather_than_an_error(self):
        figure, warnings = charts.render_chart(
            self._sales(), charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["missing"])
        )
        assert figure is None
        assert warnings

    def test_an_empty_frame_gives_no_chart(self):
        figure, warnings = charts.render_chart(
            pd.DataFrame({"region": [], "sales": []}),
            charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales"]),
        )
        assert figure is None
        assert warnings

    def test_every_chosen_type_is_titled_and_bordered_like_the_rest(self):
        for kind in charts.available_chart_types(self._sales()):
            figure, _ = charts.render_chart(
                self._sales(), charts.ChartChoices(kind=kind, x="region", measures=["sales"])
            )
            assert figure.layout.title.text, kind
            assert any(shape.type == "rect" for shape in figure.layout.shapes), kind


class TestChoicesMatchTheAutomaticChart:
    """`choose_chart` is what the customize controls open pre-filled with, so it has to
    describe the chart `build_chart` actually drew."""

    @pytest.mark.parametrize(
        ("frame", "question", "expected_kind"),
        [
            (pd.DataFrame({"department": ["Ops", "Sales"], "total": [100, 300]}), "total by department", charts.CHART_BAR),
            (pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 20, 30]}), "share of sales by region", charts.CHART_PIE),
            (
                pd.DataFrame({"month": pd.to_datetime(["2026-01-01", "2026-02-01"]), "revenue": [10, 20]}),
                "revenue over time",
                charts.CHART_LINE,
            ),
            (pd.DataFrame({"basic": [1.0, 2.0], "tax": [0.1, 0.2]}), "basic against tax", charts.CHART_SCATTER),
        ],
    )
    def test_the_chosen_kind_is_the_kind_that_gets_drawn(self, frame, question, expected_kind):
        choices, _ = charts.choose_chart(frame, question)
        assert choices.kind == expected_kind

    def test_redrawing_the_choices_reproduces_the_automatic_chart(self):
        frame = pd.DataFrame(
            {
                "department": ["Sales", "Sales", "Ops", "Ops"],
                "status": ["Present", "Absent", "Present", "Absent"],
                "count": [180, 20, 150, 10],
            }
        )
        automatic, _ = charts.build_chart(frame, "count by status")
        choices, _ = charts.choose_chart(frame, "count by status")
        redrawn, _ = charts.render_chart(frame, choices)
        assert automatic.layout.title.text == redrawn.layout.title.text
        assert [trace.name for trace in automatic.data] == [trace.name for trace in redrawn.data]

    def test_an_unchartable_frame_chooses_nothing(self):
        choices, warnings = charts.choose_chart(pd.DataFrame({"total": [42]}), "chart it")
        assert choices is None
        assert warnings


class TestDefaultStyleChangesNothing:
    """`ChartStyle()` has to be invisible: a chart nobody customizes must look exactly as
    it did before any of these controls existed."""

    @pytest.mark.parametrize(
        ("frame", "question"),
        [
            (pd.DataFrame({"department": ["Ops", "Sales"], "total": [100, 300]}), "total by department"),
            (pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 20, 30]}), "share of sales by region"),
            (pd.DataFrame({"basic": [1.0, 2.0], "tax": [0.1, 0.2]}), "basic against tax"),
        ],
    )
    def test_passing_the_default_style_draws_the_same_chart(self, frame, question):
        choices, _ = charts.choose_chart(frame, question)
        without, _ = charts.render_chart(frame, choices)
        with_default, _ = charts.render_chart(frame, choices, charts.ChartStyle())
        # Serialised, because a figure dict holds numpy arrays and `==` on those is not a
        # question with a yes-or-no answer.
        assert without.to_json() == with_default.to_json()


class TestStyleTitle:
    @staticmethod
    def _sales() -> pd.DataFrame:
        return pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 30, 20]})

    def _bar(self) -> charts.ChartChoices:
        return charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales"])

    def test_a_given_title_replaces_the_derived_one(self):
        figure, _ = charts.render_chart(
            self._sales(), self._bar(), charts.ChartStyle(title="Q3 regional sales")
        )
        assert figure.layout.title.text == "Q3 regional sales"

    def test_no_title_keeps_the_derived_one(self):
        figure, _ = charts.render_chart(self._sales(), self._bar(), charts.ChartStyle())
        assert figure.layout.title.text == "Sales by Region"

    def test_a_very_long_title_is_trimmed(self):
        figure, _ = charts.render_chart(
            self._sales(), self._bar(), charts.ChartStyle(title="x" * 500)
        )
        assert len(figure.layout.title.text) == charts.MAX_TITLE_CHARS


class TestStyleColours:
    @staticmethod
    def _split() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "department": ["Sales", "Sales", "Ops", "Ops"],
                "status": ["Present", "Absent", "Present", "Absent"],
                "count": [180, 20, 150, 10],
            }
        )

    def _split_bar(self) -> charts.ChartChoices:
        return charts.ChartChoices(
            kind=charts.CHART_BAR, x="department", measures=["count"], colour="status"
        )

    def test_the_chosen_palette_is_the_one_drawn(self):
        figure, _ = charts.render_chart(
            self._split(), self._split_bar(), charts.ChartStyle(palette=charts.PALETTE_BOLD)
        )
        assert figure.data[0].marker.color == charts.CHART_PALETTES[charts.PALETTE_BOLD][0]

    def test_a_different_palette_draws_different_colours(self):
        bold, _ = charts.render_chart(
            self._split(), self._split_bar(), charts.ChartStyle(palette=charts.PALETTE_BOLD)
        )
        pastel, _ = charts.render_chart(
            self._split(), self._split_bar(), charts.ChartStyle(palette=charts.PALETTE_PASTEL)
        )
        assert bold.data[0].marker.color != pastel.data[0].marker.color

    def test_an_unknown_palette_falls_back_rather_than_failing(self):
        figure, warnings = charts.render_chart(
            self._split(), self._split_bar(), charts.ChartStyle(palette="not-a-palette")
        )
        assert figure is not None
        assert figure.data[0].marker.color == charts.CHART_PALETTES[charts.PALETTE_DEFAULT][0]

    def test_the_single_colour_is_withheld_once_there_are_series_to_tell_apart(self):
        assert charts.PALETTE_SINGLE not in charts.available_palettes(self._split(), self._split_bar())

    def test_the_single_colour_is_offered_for_one_series(self):
        frame = pd.DataFrame({"region": ["N", "S"], "sales": [10, 20]})
        choices = charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales"])
        assert charts.PALETTE_SINGLE in charts.available_palettes(frame, choices)


class TestStyleSeriesLayout:
    @staticmethod
    def _split() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "department": ["Sales", "Sales", "Ops", "Ops"],
                "status": ["Present", "Absent", "Present", "Absent"],
                "count": [180, 20, 150, 10],
            }
        )

    def _split_bar(self, kind: str = charts.CHART_BAR) -> charts.ChartChoices:
        return charts.ChartChoices(kind=kind, x="department", measures=["count"], colour="status")

    def test_bars_are_side_by_side_by_default(self):
        figure, _ = charts.render_chart(self._split(), self._split_bar(), charts.ChartStyle())
        assert figure.layout.barmode == "group"

    def test_bars_can_be_stacked(self):
        figure, _ = charts.render_chart(
            self._split(), self._split_bar(), charts.ChartStyle(series=charts.SERIES_STACKED)
        )
        assert figure.layout.barmode == "relative"

    def test_a_hundred_percent_stack_normalises_and_says_so(self):
        figure, _ = charts.render_chart(
            self._split(), self._split_bar(), charts.ChartStyle(series=charts.SERIES_PERCENT)
        )
        assert figure.layout.barnorm == "percent"
        assert figure.layout.yaxis.title.text == charts.PERCENT_AXIS_TITLE

    def test_a_horizontal_hundred_percent_stack_labels_the_other_axis(self):
        figure, _ = charts.render_chart(
            self._split(),
            self._split_bar(charts.CHART_BAR_HORIZONTAL),
            charts.ChartStyle(series=charts.SERIES_PERCENT),
        )
        assert figure.layout.xaxis.title.text == charts.PERCENT_AXIS_TITLE

    def test_stacked_areas_stay_stacked(self):
        figure, _ = charts.render_chart(
            self._split(),
            self._split_bar(charts.CHART_AREA),
            charts.ChartStyle(series=charts.SERIES_STACKED),
        )
        assert all(trace.stackgroup for trace in figure.data)

    def test_side_by_side_areas_are_unstacked_and_filled_to_the_axis(self):
        """Un-stacking has to re-point the fill too, or each area fills to a neighbour that
        is no longer underneath it."""
        figure, _ = charts.render_chart(
            self._split(),
            self._split_bar(charts.CHART_AREA),
            charts.ChartStyle(series=charts.SERIES_GROUPED),
        )
        assert not any(trace.stackgroup for trace in figure.data)
        assert all(trace.fill == "tozeroy" for trace in figure.data)

    def test_a_single_area_is_left_exactly_as_it_was(self):
        """With nothing to stack against, the layout isn't a question — and the page
        doesn't ask it either."""
        frame = pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 30, 20]})
        choices = charts.ChartChoices(kind=charts.CHART_AREA, x="region", measures=["sales"])
        figure, _ = charts.render_chart(frame, choices, charts.ChartStyle(series=charts.SERIES_GROUPED))
        assert figure.data[0].stackgroup

    def test_the_layout_control_is_offered_only_where_it_would_do_something(self):
        one_series = charts.ChartChoices(kind=charts.CHART_BAR, x="department", measures=["count"])
        assert not charts.supports_series_layout(self._split(), one_series)
        assert charts.supports_series_layout(self._split(), self._split_bar())

    def test_the_layout_control_is_not_offered_for_lines_or_pies(self):
        for kind in (charts.CHART_LINE, charts.CHART_PIE, charts.CHART_SCATTER):
            assert not charts.supports_series_layout(self._split(), self._split_bar(kind))


class TestStyleValueLabels:
    @staticmethod
    def _sales() -> pd.DataFrame:
        return pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 30, 20]})

    def test_bars_are_unlabelled_by_default(self):
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales"]),
            charts.ChartStyle(),
        )
        assert not figure.data[0].texttemplate

    def test_bars_can_show_their_values(self):
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales"]),
            charts.ChartStyle(show_values=True),
        )
        assert figure.data[0].texttemplate == "%{y:,}"

    def test_a_horizontal_bar_labels_from_the_other_axis(self):
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_BAR_HORIZONTAL, x="region", measures=["sales"]),
            charts.ChartStyle(show_values=True),
        )
        assert figure.data[0].texttemplate == "%{x:,}"

    def test_a_line_asks_its_own_mode_to_draw_the_text(self):
        """A scatter trace ignores `texttemplate` unless its mode says to draw text — and
        replacing the mode outright would drop the markers."""
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_LINE, x="region", measures=["sales"]),
            charts.ChartStyle(show_values=True),
        )
        assert "text" in figure.data[0].mode
        assert "markers" in figure.data[0].mode

    def test_a_pie_shows_values_beside_its_percentages(self):
        figure, _ = charts.render_chart(
            self._sales(),
            charts.ChartChoices(kind=charts.CHART_PIE, x="region", measures=["sales"]),
            charts.ChartStyle(show_values=True),
        )
        assert "value" in figure.data[0].textinfo


class TestStyleLegendAndHeight:
    @staticmethod
    def _bar() -> charts.ChartChoices:
        return charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales"])

    @staticmethod
    def _sales() -> pd.DataFrame:
        return pd.DataFrame({"region": ["N", "S"], "sales": [10, 20]})

    def test_the_legend_shows_by_default(self):
        figure, _ = charts.render_chart(self._sales(), self._bar(), charts.ChartStyle())
        assert figure.layout.showlegend is True

    def test_the_legend_can_be_hidden(self):
        figure, _ = charts.render_chart(
            self._sales(), self._bar(), charts.ChartStyle(show_legend=False)
        )
        assert figure.layout.showlegend is False

    def test_the_height_is_the_one_asked_for(self):
        figure, _ = charts.render_chart(self._sales(), self._bar(), charts.ChartStyle(height=720))
        assert figure.layout.height == 720

    def test_the_default_height_is_applied(self):
        figure, _ = charts.render_chart(self._sales(), self._bar(), charts.ChartStyle())
        assert figure.layout.height == charts.DEFAULT_CHART_HEIGHT


class TestStyleSorting:
    @staticmethod
    def _sales() -> pd.DataFrame:
        return pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 30, 20]})

    def _bar(self, kind: str = charts.CHART_BAR) -> charts.ChartChoices:
        return charts.ChartChoices(kind=kind, x="region", measures=["sales"])

    def test_biggest_first(self):
        figure, _ = charts.render_chart(
            self._sales(), self._bar(), charts.ChartStyle(sort=charts.SORT_LARGEST)
        )
        assert list(figure.data[0].x) == ["S", "E", "N"]

    def test_smallest_first(self):
        figure, _ = charts.render_chart(
            self._sales(), self._bar(), charts.ChartStyle(sort=charts.SORT_SMALLEST)
        )
        assert list(figure.data[0].x) == ["N", "E", "S"]

    def test_by_name(self):
        figure, _ = charts.render_chart(
            self._sales(), self._bar(), charts.ChartStyle(sort=charts.SORT_LABEL)
        )
        assert list(figure.data[0].x) == ["E", "N", "S"]

    def test_as_the_query_returned_it(self):
        figure, _ = charts.render_chart(
            self._sales(), self._bar(), charts.ChartStyle(sort=charts.SORT_ORIGINAL)
        )
        assert list(figure.data[0].x) == ["N", "S", "E"]

    def test_a_horizontal_bar_reverses_because_its_axis_reads_upwards(self):
        figure, _ = charts.render_chart(
            self._sales(),
            self._bar(charts.CHART_BAR_HORIZONTAL),
            charts.ChartStyle(sort=charts.SORT_LARGEST),
        )
        assert list(figure.data[0].y)[-1] == "S"

    def test_a_split_chart_sorts_whole_categories_not_single_bars(self):
        """Each category owns one row per legend value; sorting the rows themselves would
        interleave the categories."""
        frame = pd.DataFrame(
            {
                "department": ["Sales", "Sales", "Ops", "Ops"],
                "status": ["Present", "Absent", "Present", "Absent"],
                "count": [180, 20, 150, 10],
            }
        )
        figure, _ = charts.render_chart(
            frame,
            charts.ChartChoices(
                kind=charts.CHART_BAR, x="department", measures=["count"], colour="status"
            ),
            charts.ChartStyle(sort=charts.SORT_LARGEST),
        )
        # Sales totals 200 against Ops' 160, and every trace agrees on the order.
        for trace in figure.data:
            assert list(trace.x) == ["Sales", "Ops"]

    def test_sorting_is_not_offered_along_a_time_axis(self):
        frame = pd.DataFrame(
            {"month": pd.to_datetime(["2026-02-01", "2026-01-01"]), "revenue": [20, 10]}
        )
        choices = charts.ChartChoices(kind=charts.CHART_LINE, x="month", measures=["revenue"])
        assert not charts.supports_sorting(frame, choices)

    def test_sorting_is_not_offered_on_a_scatter(self):
        frame = pd.DataFrame({"basic": [1.0, 2.0], "tax": [0.1, 0.2]})
        choices = charts.ChartChoices(kind=charts.CHART_SCATTER, x="basic", measures=["tax"])
        assert not charts.supports_sorting(frame, choices)


class TestStyleTopN:
    @staticmethod
    def _many() -> pd.DataFrame:
        return pd.DataFrame({"region": [f"r{n}" for n in range(12)], "sales": range(12)})

    def _bar(self) -> charts.ChartChoices:
        return charts.ChartChoices(kind=charts.CHART_BAR, x="region", measures=["sales"])

    def test_a_chosen_limit_keeps_that_many_categories(self):
        figure, warnings = charts.render_chart(
            self._many(), self._bar(), charts.ChartStyle(top_n=3)
        )
        assert len(figure.data[0].x) == 3
        assert any("top 3 of 12" in warning for warning in warnings)

    def test_a_chosen_limit_keeps_the_biggest(self):
        figure, _ = charts.render_chart(self._many(), self._bar(), charts.ChartStyle(top_n=3))
        assert set(figure.data[0].x) == {"r11", "r10", "r9"}

    def test_no_limit_falls_back_to_the_automatic_cap(self):
        frame = pd.DataFrame({"region": [f"r{n}" for n in range(50)], "sales": range(50)})
        figure, _ = charts.render_chart(frame, self._bar(), charts.ChartStyle())
        assert len(figure.data[0].x) == charts.MAX_CATEGORIES

    def test_a_limit_above_the_automatic_cap_is_honoured(self):
        """The cap exists to stop an unreadable wall by default, not to overrule someone
        who has asked for the wall."""
        frame = pd.DataFrame({"region": [f"r{n}" for n in range(50)], "sales": range(50)})
        figure, _ = charts.render_chart(frame, self._bar(), charts.ChartStyle(top_n=40))
        assert len(figure.data[0].x) == 40

    def test_the_control_is_offered_only_where_rows_are_capped(self):
        assert charts.top_n_ceiling(self._many(), self._bar()) == 12
        line = charts.ChartChoices(kind=charts.CHART_LINE, x="region", measures=["sales"])
        assert charts.top_n_ceiling(self._many(), line) == 0

    def test_the_control_starts_from_the_automatic_cap(self):
        assert charts.default_top_n(self._many(), self._bar()) == 12
        frame = pd.DataFrame({"region": [f"r{n}" for n in range(50)], "sales": range(50)})
        assert charts.default_top_n(frame, self._bar()) == charts.MAX_CATEGORIES


class TestStyleSurvivesABadCombination:
    def test_an_impossible_style_still_returns_a_chart(self):
        """Style is never a reason to lose the chart — the same contract `render_chart`
        already keeps for the data choices."""
        frame = pd.DataFrame({"region": ["N", "S"], "sales": [10, 20]})
        figure, _ = charts.render_chart(
            frame,
            charts.ChartChoices(kind=charts.CHART_PIE, x="region", measures=["sales"]),
            charts.ChartStyle(series=charts.SERIES_PERCENT, sort=charts.SORT_LABEL, top_n=1),
        )
        assert figure is not None


class TestPresentation:
    """Every chart carries a title, a border and a legend."""

    @pytest.mark.parametrize(
        ("frame", "question"),
        [
            (pd.DataFrame({"department": ["Ops", "Sales"], "total": [100, 300]}), "total by department"),
            (pd.DataFrame({"region": ["N", "S", "E"], "sales": [10, 20, 30]}), "share of sales by region"),
            (
                pd.DataFrame({"month": pd.to_datetime(["2026-01-01", "2026-02-01"]), "revenue": [10, 20]}),
                "revenue over time",
            ),
            (pd.DataFrame({"basic": [1.0, 2.0], "tax": [0.1, 0.2]}), "basic against tax"),
        ],
    )
    def test_every_chart_type_has_a_title_a_border_and_a_legend(self, frame, question):
        figure, _ = charts.build_chart(frame, question)
        assert figure.layout.title.text
        assert figure.layout.showlegend is True
        assert any(shape.type == "rect" for shape in figure.layout.shapes)

    def test_the_title_names_the_measures_and_the_grouping(self):
        frame = pd.DataFrame(
            {"department": ["Ops"], "min_basic_salary": [10.0], "max_basic_salary": [90.0]}
        )
        figure, _ = charts.build_chart(frame, "plot it")
        assert figure.layout.title.text == "Min basic salary and Max basic salary by Department"

    def test_a_long_title_is_cut_rather_than_pushing_the_plot_off_the_card(self):
        frame = pd.DataFrame({"a_very_long_label_column_name_indeed": ["x"], **{f"measure_number_{n}": [n] for n in range(5)}})
        figure, _ = charts.build_chart(frame, "plot it")
        assert len(figure.layout.title.text) <= charts.MAX_TITLE_CHARS

    def test_a_single_series_still_gets_a_named_legend_entry(self):
        """`showlegend` on an unnamed trace renders an empty row, which looks broken."""
        frame = pd.DataFrame({"basic": [1.0, 2.0], "tax": [0.1, 0.2]})
        figure, _ = charts.build_chart(frame, "basic against tax")
        assert figure.data[0].name == "Tax"


class TestUnchartableResults:
    def test_no_rows_gives_no_chart(self):
        figure, warnings = charts.build_chart(pd.DataFrame({"a": [], "b": []}), "chart it")
        assert figure is None
        assert warnings

    def test_none_gives_no_chart(self):
        figure, warnings = charts.build_chart(None, "chart it")
        assert figure is None
        assert warnings

    def test_a_single_column_gives_no_chart(self):
        figure, warnings = charts.build_chart(pd.DataFrame({"total": [42]}), "chart it")
        assert figure is None
        assert "two columns" in warnings[0]

    def test_no_numeric_column_gives_no_chart(self):
        frame = pd.DataFrame({"name": ["Ana", "Bo"], "city": ["Pune", "Delhi"]})
        figure, warnings = charts.build_chart(frame, "chart it")
        assert figure is None
        assert "numeric" in warnings[0]


class TestTruncation:
    def test_too_many_categories_are_capped_and_reported(self):
        frame = pd.DataFrame(
            {"code": [f"c{n}" for n in range(50)], "amount": range(50)}
        )
        figure, warnings = charts.build_chart(frame, "amount by code")
        assert len(figure.data[0].x) == charts.MAX_CATEGORIES
        assert any(str(charts.MAX_CATEGORIES) in warning for warning in warnings)

    def test_the_largest_values_are_the_ones_kept(self):
        frame = pd.DataFrame({"code": [f"c{n}" for n in range(50)], "amount": range(50)})
        figure, _ = charts.build_chart(frame, "amount by code")
        assert figure.data[0].x[0] == "c49"


class TestNeverRaises:
    @pytest.mark.parametrize(
        "frame",
        [
            pd.DataFrame({"a": [None, None], "b": [1, 2]}),
            pd.DataFrame({"a": ["x"], "b": [1]}),
        ],
    )
    def test_awkward_frames_return_a_result_rather_than_exploding(self, frame):
        """A chart is one of three outputs and must not be able to cost the other two."""
        figure, warnings = charts.build_chart(frame, "chart it")
        assert figure is not None or warnings
