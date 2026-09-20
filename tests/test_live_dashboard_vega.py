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


def test_every_chart_type_produces_a_spec_with_a_mark():
    for sub_type in m.CHART_SUB_TYPES:
        built = vs.build_vega_spec(panel(sub_type=sub_type))
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
