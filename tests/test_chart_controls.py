"""What a chart starts as, before anyone touches a control.

`app_pages/chart_controls.py` is mostly widgets, and the page suites drive those through
`AppTest`. This covers the part that is a decision rather than a widget: `seed_choices`,
which is the single answer to "what does a chart the user asked for look like on arrival",
shared by the Generate chart buttons on both pages and by Reset.
"""

import pandas as pd

from analyst import charts
from app_pages import chart_controls

REPEATED_DATES = pd.DataFrame(
    {
        "invoice_month": ["Jan", "Jan", "Feb", "Feb", "Feb"],
        "amount": [10.0, 15.0, 20.0, 5.0, 5.0],
    }
)


class TestSeedingAChart:
    def test_a_new_chart_starts_aggregated(self):
        """The requested default. A result with one row per invoice is the normal case, and
        drawing it raw gives a bar per invoice rather than the monthly total."""
        chosen, _ = chart_controls.seed_choices(REPEATED_DATES)
        assert chosen.aggregate_by_x is True

    def test_each_value_arrives_with_something_to_compute(self):
        """Aggregation on with nothing to compute would be a chart that draws nothing until
        the user opens the panel and finds the box they were never told about."""
        chosen, _ = chart_controls.seed_choices(REPEATED_DATES)
        assert chosen.aggregations == [charts.Aggregation("amount", charts.AGG_SUM)]

    def test_the_seeded_chart_actually_draws_the_totals(self):
        chosen, _ = chart_controls.seed_choices(REPEATED_DATES)
        figure, _ = charts.render_chart(REPEATED_DATES, chosen)
        assert sorted(figure.data[0].y) == [25.0, 30.0]

    def test_a_text_column_is_counted_rather_than_summed(self):
        frame = pd.DataFrame({"department": ["Sales", "Sales"], "owner": ["Ana", "Bo"], "n": [1, 2]})
        chosen, _ = chart_controls.seed_choices(frame)
        functions = {entry.column: entry.function for entry in chosen.aggregations}
        assert functions.get("n") == charts.AGG_SUM
        assert charts.AGG_SUM not in {
            entry.function for entry in chosen.aggregations if entry.column == "owner"
        }

    def test_a_scatter_is_left_unaggregated(self):
        """Its x axis is a second measure: grouping a continuous number destroys the
        relationship the chart exists to show."""
        frame = pd.DataFrame({"basic": [1.0, 2.0, 3.0], "tax": [0.1, 0.2, 0.3]})
        chosen, _ = chart_controls.seed_choices(frame)
        assert chosen.kind == charts.CHART_SCATTER
        assert chosen.aggregate_by_x is False

    def test_nothing_chartable_still_seeds_nothing(self):
        chosen, warnings = chart_controls.seed_choices(pd.DataFrame({"name": ["Ana"]}))
        assert chosen is None
        assert warnings

    def test_the_automatic_pipeline_chart_is_not_changed(self):
        """`choose_chart` is what an answer's own chart is built from, and its SQL has
        already grouped — aggregating it again would count pre-counted rows."""
        chosen, _ = charts.choose_chart(REPEATED_DATES)
        assert chosen.aggregate_by_x is False
        assert chosen.aggregations == []
