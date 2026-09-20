"""The Vega-Lite specs, chart type by chart type.

This is the payoff of building specs in Python rather than JavaScript: every chart the
exported dashboard can draw is asserted here in ordinary pytest, with no browser, no
screenshot and no provider.

What the tests are really checking is that the *aggregation is declared rather than computed*
- `{"aggregate": "sum"}` on the measure channel, not a pre-totalled frame. That is what lets
the browser re-total on every cross-filter click, and it is the whole reason this feature can
be interactive offline.
"""

from analyst.charts import CHART_PALETTES, PALETTE_BOLD
from live_dashboard import model as m
from live_dashboard import vega_spec as vs


def panel(**overrides) -> m.PanelSpec:
    spec = m.PanelSpec(
        visual_type=m.VISUAL_CHART,
        sub_type=m.CHART_BAR,
        source_table="main",
        measure_column="Amount",
        group_by="Category",
    )
    for name, value in overrides.items():
        setattr(spec, name, value)
    return spec


# ------------------------------------------------------------------ marks


def test_every_chart_type_produces_something_drawable():
    """Every shape the catalog offers has drawing code behind it.

    The loop matters more than any single case: a sub-type added to the list without being
    encoded would fail here, rather than in a file already sitting in a reader's inbox.
    """
    for sub_type in m.CHART_SUB_TYPES:
        built = vs.build_vega_spec(panel(sub_type=sub_type, measure_column_2="Quantity",
                                         colour_by="Region"))
        if "layer" in built:
            assert all(layer["mark"]["type"] for layer in built["layer"]), sub_type
        else:
            assert built["mark"]["type"], sub_type


def test_a_bar_puts_the_category_on_x_and_the_measure_on_y():
    built = vs.build_vega_spec(panel(sub_type=m.CHART_BAR))
    assert built["encoding"]["x"]["field"] == "Category"
    assert built["encoding"]["y"]["aggregate"] == "sum"


def test_a_horizontal_bar_swaps_the_two_channels():
    built = vs.build_vega_spec(panel(sub_type=m.CHART_BAR_HORIZONTAL))
    assert built["encoding"]["y"]["field"] == "Category"
    assert built["encoding"]["x"]["aggregate"] == "sum"


def test_a_pie_uses_theta_and_makes_the_category_the_legend():
    built = vs.build_vega_spec(panel(sub_type=m.CHART_PIE))
    assert built["mark"]["type"] == "arc"
    assert built["encoding"]["theta"]["aggregate"] == "sum"
    assert built["encoding"]["color"]["field"] == "Category"


def test_a_pie_fills_its_panel_like_every_other_chart():
    """A pie once had to carry a fixed width, because `"container"` drew it at radius zero.

    That was never the pie's fault: vega-embed's wrapper is `display: inline-block`, so it
    sized itself from contents that were waiting on it, and *every* chart collapsed. The
    stylesheet fixes the wrapper, so the arc can fill its panel like the rest.
    """
    assert vs.build_vega_spec(panel(sub_type=m.CHART_PIE))["width"] == "container"


# ------------------------------------------------------------------ category labels


def test_the_category_labels_choose_their_own_angle_in_the_browser():
    """Vega-Lite decides this at compile time, where the panel width is not yet known, and
    defaults to sideways. The expression defers it to where the width is real."""
    built = vs.build_vega_spec(panel(sub_type=m.CHART_BAR))
    assert built["encoding"]["x"]["axis"]["labelAngle"] == vs.LABEL_ANGLE_EXPRESSION
    assert "domain('x')" in vs.LABEL_ANGLE_EXPRESSION["expr"]


def test_a_horizontal_bars_categories_are_left_alone():
    """They already run flat down the side, and the expression's `domain('x')` would be the
    measure axis there - the wrong scale entirely."""
    built = vs.build_vega_spec(panel(sub_type=m.CHART_BAR_HORIZONTAL))
    assert "axis" not in built["encoding"]["y"]


def test_a_scatter_plots_raw_points_rather_than_totals():
    """Aggregating first would collapse the very spread a scatter exists to show."""
    built = vs.build_vega_spec(panel(sub_type=m.CHART_SCATTER))
    assert "aggregate" not in built["encoding"]["y"]
    assert built["encoding"]["x"]["type"] == "quantitative"


# ------------------------------------------------------------------ aggregation


def test_every_aggregation_maps_to_a_vega_lite_operation():
    assert vs.vega_aggregate("average") == "mean"
    assert vs.vega_aggregate("minimum") == "min"
    assert vs.vega_aggregate("maximum") == "max"
    assert vs.vega_aggregate("sum") == "sum"


def test_an_unknown_aggregation_falls_back_rather_than_breaking_the_chart():
    assert vs.vega_aggregate("median-ish") == "sum"


def test_a_count_carries_no_field():
    """Naming a column would count its non-null values, which is a different question."""
    built = vs.build_vega_spec(panel(aggregation="count"))
    assert built["encoding"]["y"] == {
        "type": "quantitative", "title": "Count", "aggregate": "count"
    }


def test_the_measure_axis_is_titled_from_the_aggregation():
    assert vs.measure_title(panel(aggregation="average")) == "Average of Amount"
    assert vs.measure_title(panel(aggregation="count")) == "Count"


# ------------------------------------------------------------------ sorting and limits


def test_biggest_first_sorts_by_the_measure_channel():
    assert vs.build_vega_spec(panel(sort="largest"))["encoding"]["x"]["sort"] == "-y"


def test_biggest_first_follows_the_channel_on_a_horizontal_bar():
    built = vs.build_vega_spec(panel(sub_type=m.CHART_BAR_HORIZONTAL, sort="largest"))
    assert built["encoding"]["y"]["sort"] == "-x"


def test_smallest_first_sorts_the_other_way():
    assert vs.build_vega_spec(panel(sort="smallest"))["encoding"]["x"]["sort"] == "y"


def test_automatic_leaves_a_time_axis_alone_but_sorts_a_category_one():
    line = vs.build_vega_spec(panel(sub_type=m.CHART_LINE, sort="automatic"))
    bar = vs.build_vega_spec(panel(sub_type=m.CHART_BAR, sort="automatic"))
    assert line["encoding"]["x"]["sort"] is None
    assert bar["encoding"]["x"]["sort"] == "-y"


def test_top_n_becomes_a_transform_rather_than_trimming_the_data():
    """The rows are shared, so one panel's Top 10 must not take them from the others."""
    built = vs.build_vega_spec(panel(top_n=10))
    operations = [list(step)[0] for step in built["transform"]]
    assert operations == ["aggregate", "window", "filter"]
    assert "10" in built["transform"][2]["filter"]


def test_no_top_n_means_no_transform_at_all():
    assert "transform" not in vs.build_vega_spec(panel(top_n=0))


# ------------------------------------------------------------------ colour and theme


def test_a_colour_column_becomes_a_legend_using_the_chosen_palette():
    built = vs.build_vega_spec(panel(colour_by="Region"), palette_name=PALETTE_BOLD)
    assert built["encoding"]["color"]["field"] == "Region"
    assert built["encoding"]["color"]["scale"]["range"] == CHART_PALETTES[PALETTE_BOLD]


def test_a_single_series_still_takes_the_palettes_first_colour():
    """Otherwise the export would draw in Vega-Lite's default blue and stop matching the app."""
    built = vs.build_vega_spec(panel(), palette_name=PALETTE_BOLD)
    assert built["mark"]["color"] == CHART_PALETTES[PALETTE_BOLD][0]


def test_an_unknown_palette_falls_back_to_the_default():
    built = vs.build_vega_spec(panel(colour_by="Region"), palette_name="neon")
    assert built["encoding"]["color"]["scale"]["range"] == CHART_PALETTES["default"]


def test_the_dark_theme_changes_the_axis_colours():
    light = vs.build_vega_spec(panel(), theme="light")
    dark = vs.build_vega_spec(panel(), theme=m.THEME_DARK)
    assert light["config"]["axis"]["labelColor"] != dark["config"]["axis"]["labelColor"]


# ------------------------------------------------------------------ dates and selection


def test_a_line_over_a_date_column_gets_a_time_axis():
    built = vs.build_vega_spec(
        panel(sub_type=m.CHART_LINE, group_by="When"), date_columns=frozenset({"When"})
    )
    assert built["encoding"]["x"]["type"] == "temporal"


def test_a_line_over_a_column_that_is_not_a_date_stays_categorical():
    built = vs.build_vega_spec(panel(sub_type=m.CHART_LINE), date_columns=frozenset({"When"}))
    assert built["encoding"]["x"]["type"] == "nominal"


def test_a_chart_carries_the_selection_that_makes_it_clickable():
    built = vs.build_vega_spec(panel())
    assert built["params"][0]["name"] == vs.SELECTION_NAME
    assert built["params"][0]["select"]["fields"] == ["Category"]


def test_a_scatter_has_nothing_to_select():
    assert "params" not in vs.build_vega_spec(panel(sub_type=m.CHART_SCATTER))


def test_the_spec_reads_from_the_named_dataset_the_runtime_refills():
    """If these two ever disagreed, every chart would silently show everything."""
    assert vs.build_vega_spec(panel())["data"] == {"name": vs.DATA_NAME}


def test_an_unknown_chart_type_is_drawn_plainly_rather_than_left_as_a_hole():
    assert vs.build_vega_spec(panel(sub_type="hologram"))["mark"]["type"] == "bar"


def test_a_panels_height_is_honoured():
    built = vs.build_vega_spec(panel(properties={"height": 500}))
    assert built["height"] == 500 - vs.CHART_CHROME_HEIGHT


# ------------------------------------------------- phase 34: the wider vocabulary


def test_a_stacked_and_a_grouped_bar_differ_only_by_the_offset():
    """The single line between them, and worth pinning: they are the same encoding, and a
    grouped bar that lost its `xOffset` would silently come back stacked."""
    stacked = vs.build_vega_spec(panel(sub_type=m.CHART_BAR_STACKED, colour_by="Region"))
    grouped = vs.build_vega_spec(panel(sub_type=m.CHART_BAR_GROUPED, colour_by="Region"))

    assert "xOffset" not in stacked["encoding"]
    assert grouped["encoding"]["xOffset"]["field"] == "Region"
    assert stacked["encoding"]["color"]["field"] == "Region"

    without_offset = dict(grouped["encoding"])
    without_offset.pop("xOffset")
    assert without_offset == stacked["encoding"]


def test_a_donut_is_a_pie_with_a_hole():
    pie = vs.build_vega_spec(panel(sub_type=m.CHART_PIE))
    donut = vs.build_vega_spec(panel(sub_type=m.CHART_DONUT))

    assert pie["mark"]["innerRadius"] == 0
    assert donut["mark"]["innerRadius"] > 0
    assert donut["encoding"]["theta"]["aggregate"] == "sum"


def test_a_histogram_bins_one_number_and_needs_no_breakdown():
    """The first chart with no group-by: the bars *are* the breakdown."""
    built = vs.build_vega_spec(panel(sub_type=m.CHART_HISTOGRAM, group_by=""))

    assert built["encoding"]["x"]["bin"] is True
    assert built["encoding"]["x"]["field"] == "Amount"
    assert built["encoding"]["y"]["aggregate"] == "count"
    assert "params" not in built  # a bin is not something anyone filters by


def test_a_heatmap_puts_two_categories_on_the_axes_and_the_number_in_the_colour():
    built = vs.build_vega_spec(panel(sub_type=m.CHART_HEATMAP, colour_by="Region"))

    assert built["mark"]["type"] == "rect"
    assert built["encoding"]["x"]["type"] == "nominal"
    assert built["encoding"]["y"]["field"] == "Region"
    assert built["encoding"]["color"]["aggregate"] == "sum"
    assert built["encoding"]["color"]["scale"]["scheme"] == vs.HEATMAP_SCHEME


def test_a_box_plot_is_drawn_from_the_rows_rather_than_from_totals():
    """Aggregating first would hand the mark one number per category to draw a box around."""
    built = vs.build_vega_spec(panel(sub_type=m.CHART_BOXPLOT))

    assert built["mark"]["type"] == "boxplot"
    assert "aggregate" not in built["encoding"]["y"]
    assert built["encoding"]["y"]["field"] == "Amount"


def test_a_combo_chart_layers_a_line_over_its_bars_on_its_own_scale():
    built = vs.build_vega_spec(panel(sub_type=m.CHART_COMBO, measure_column_2="Quantity"))

    assert "mark" not in built and "encoding" not in built
    bars, line = built["layer"]
    assert bars["mark"]["type"] == "bar"
    assert line["mark"]["type"] == "line"
    assert bars["encoding"]["y"]["field"] == "Amount"
    assert line["encoding"]["y"]["field"] == "Quantity"
    # Two numbers in different units: one axis would flatten the smaller into the baseline.
    assert built["resolve"]["scale"]["y"] == "independent"
    # A layered spec takes its selection on the layer the reader actually clicks.
    assert bars["params"][0]["name"] == vs.SELECTION_NAME


def test_a_percentage_of_total_is_a_join_then_a_division():
    built = vs.build_vega_spec(panel(aggregation=m.AGG_PERCENT_OF_TOTAL))
    kinds = [list(step)[0] for step in built["transform"]]

    assert kinds == ["aggregate", "joinaggregate", "calculate"]
    assert built["encoding"]["y"]["field"] == vs.TRANSFORM_FIELD
    assert "aggregate" not in built["encoding"]["y"]  # already totalled by the transform


def test_a_percentage_of_total_survives_a_month_with_nothing_in_it():
    """A zero grand total is a real dataset, not a rare one. Without the guard every bar
    comes back null and the chart draws empty with nothing to explain it."""
    calculation = vs.build_vega_spec(panel(aggregation=m.AGG_PERCENT_OF_TOTAL))["transform"][-1]
    assert "_grand_total ?" in calculation["calculate"]


def test_a_running_total_adds_up_along_the_breakdown():
    built = vs.build_vega_spec(panel(aggregation=m.AGG_RUNNING_TOTAL, group_by="TxnDate"))
    window = [step for step in built["transform"] if "window" in step][0]

    assert window["window"][0]["op"] == "sum"
    assert window["sort"] == [{"field": "TxnDate", "order": "ascending"}]
    assert window["frame"] == [None, 0]  # everything up to here, not the whole column


def test_first_and_last_are_built_out_of_a_window_because_vega_lite_has_no_such_total():
    for aggregation, operation in [(m.AGG_FIRST, "first_value"), (m.AGG_LAST, "last_value")]:
        built = vs.build_vega_spec(panel(aggregation=aggregation))
        assert built["transform"][0]["window"][0]["op"] == operation


def test_a_top_n_is_left_off_a_total_that_is_already_computed():
    """Ranking the totals again would total the totals. Left alone rather than half-applied:
    a Top 10 that quietly changed what "% of total" meant would be worse than no Top 10."""
    built = vs.build_vega_spec(panel(aggregation=m.AGG_PERCENT_OF_TOTAL, top_n=5))
    assert not any("filter" in step for step in built["transform"])


def test_every_aggregation_on_offer_reaches_vega_one_way_or_the_other():
    """The loop that makes this phase safe to extend.

    A fourteenth aggregation added to the catalog without being wired up fails here, in
    pytest, rather than in a dashboard already downloaded.
    """
    for aggregation in m.DASHBOARD_AGGREGATIONS:
        built = vs.build_vega_spec(panel(aggregation=aggregation, group_by="TxnDate"))
        measure = built["encoding"]["y"]
        if aggregation in m.TRANSFORM_AGGREGATIONS:
            assert built.get("transform"), aggregation
            assert measure["field"] == vs.TRANSFORM_FIELD, aggregation
        else:
            assert measure["aggregate"] in vs._VEGA_AGGREGATES.values(), aggregation


def test_the_measure_title_names_what_was_done_to_the_number():
    assert vs.measure_title(panel(aggregation=m.AGG_MEDIAN)) == "Median of Amount"
    assert vs.measure_title(panel(aggregation=m.AGG_PERCENT_OF_TOTAL)) == "% of total Amount"
    assert vs.measure_title(panel(aggregation=m.AGG_RUNNING_TOTAL)).startswith("Running total")
