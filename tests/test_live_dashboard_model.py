"""The dashboard spec: list operations, the derived columns, and the JSON round-trip.

The tests that matter most here are `clean_properties` and `panel_problems`.

`clean_properties` is a security boundary, not a convenience: it is the only thing standing
between the free-text Properties cell and the exported page, so a test that an unknown key is
dropped is a test that the page has no CSS-injection surface.

`panel_problems` is the requirement's promise that a visual needing a link nobody confirmed
says so, rather than being silently joined on a guess.
"""

import json

import pytest

from live_dashboard import model as m
from live_dashboard.exceptions import DashboardStorageError


def chart(**overrides) -> m.PanelSpec:
    panel = m.PanelSpec(
        visual_type=m.VISUAL_CHART,
        sub_type=m.CHART_BAR,
        source_table="main",
        measure_column="Amount",
        group_by="Category",
    )
    for name, value in overrides.items():
        setattr(panel, name, value)
    return panel


def filter_panel(column: str = "Customer - Name", table: str = "main") -> m.PanelSpec:
    return m.PanelSpec(
        visual_type=m.VISUAL_FILTER,
        sub_type=m.FILTER_DROPDOWN,
        source_table=table,
        source_columns=[column],
        title="Customer",
    )


COLUMNS = {"main": ["Amount", "Category", "Customer - Name", "Region2"], "Calendar": ["TheDate"]}


# ------------------------------------------------------------------ list operations


def test_add_panel_puts_each_visual_on_its_own_row():
    spec = m.DashboardSpec()
    first = m.add_panel(spec, m.VISUAL_CHART)
    second = m.add_panel(spec, m.VISUAL_CHART)
    assert (first.row_number, second.row_number) == (1, 2)


def test_add_panel_repairs_an_unknown_visual_type():
    spec = m.DashboardSpec()
    panel = m.add_panel(spec, "hologram")
    assert panel.visual_type == m.VISUAL_CHART


def test_remove_panel_reports_a_panel_that_is_already_gone():
    spec = m.DashboardSpec()
    panel = m.add_panel(spec)
    assert m.remove_panel(spec, panel.panel_id) is True
    assert m.remove_panel(spec, panel.panel_id) is False


def test_move_panel_refuses_to_walk_off_either_end():
    spec = m.DashboardSpec()
    first = m.add_panel(spec)
    second = m.add_panel(spec)

    assert m.move_panel(spec, first.panel_id, -1) is False
    assert m.move_panel(spec, second.panel_id, 1) is False
    assert m.move_panel(spec, first.panel_id, 1) is True
    assert spec.panels == [second, first]


# ------------------------------------------------------------------ derived columns


def test_group_into_rows_groups_by_row_number_and_leaves_filters_out():
    spec = m.DashboardSpec()
    left = m.add_panel(spec, m.VISUAL_CHART, "Left")
    right = m.add_panel(spec, m.VISUAL_CHART, "Right")
    spec.panels.append(filter_panel())
    left.row_number = right.row_number = 1

    rows = m.group_into_rows(spec.panels)
    assert [[panel.title for panel in row] for row in rows] == [["Left", "Right"]]


def test_group_into_rows_sorts_by_row_number_not_list_order():
    spec = m.DashboardSpec()
    late = m.add_panel(spec, m.VISUAL_CHART, "Late")
    early = m.add_panel(spec, m.VISUAL_CHART, "Early")
    late.row_number, early.row_number = 9, 2

    assert [row[0].title for row in m.group_into_rows(spec.panels)] == ["Early", "Late"]


def test_depends_on_filters_lists_filters_over_the_same_table():
    spec = m.DashboardSpec()
    panel = chart()
    spec.panels.extend([panel, filter_panel()])

    assert [item.title for item in m.depends_on_filters(spec, panel)] == ["Customer"]


def test_depends_on_filters_ignores_a_filter_over_a_different_table():
    spec = m.DashboardSpec()
    panel = chart()
    spec.panels.extend([panel, filter_panel(column="TheDate", table="Calendar")])

    assert m.depends_on_filters(spec, panel) == []


def test_a_filter_is_never_narrowed_by_another_filter():
    """The reader must always be able to get back out, so a filter's choices stay whole."""
    spec = m.DashboardSpec()
    one, two = filter_panel(), filter_panel(column="Category")
    spec.panels.extend([one, two])

    assert m.depends_on_filters(spec, one) == []


# ------------------------------------------------------------------ panel problems


def test_a_panel_with_every_column_present_has_no_problem():
    assert m.panel_problems(chart(), COLUMNS) == ""


def test_a_column_the_confirmed_links_do_not_reach_is_named_in_the_warning():
    problem = m.panel_problems(chart(group_by="Region"), COLUMNS)
    assert "Region" in problem and "Relationships" in problem


def test_a_table_this_dashboard_does_not_embed_is_reported():
    problem = m.panel_problems(chart(source_table="Budget"), COLUMNS)
    assert "Budget" in problem


def test_a_count_needs_no_measure_column():
    assert m.panel_problems(chart(aggregation="count", measure_column=""), COLUMNS) == ""


def test_anything_other_than_a_count_does_need_one():
    assert "total" in m.panel_problems(chart(measure_column=""), COLUMNS)


def test_a_filter_with_no_column_is_reported():
    panel = filter_panel()
    panel.source_columns = []
    assert "narrow on" in m.panel_problems(panel, COLUMNS)


def test_a_table_panel_needs_at_least_one_column():
    panel = m.PanelSpec(visual_type=m.VISUAL_TABLE, source_table="main")
    assert "at least one column" in m.panel_problems(panel, COLUMNS)


# --------------------------------------------------- drill-down tables (phase 40)


def drilldown(**overrides) -> m.PanelSpec:
    """A drill-down table reading its fields the way phase 40 defines: `source_columns` are
    the levels to group by, and `measure_column` + `aggregation` are the total on each."""
    panel = m.PanelSpec(
        visual_type=m.VISUAL_TABLE,
        sub_type=m.TABLE_DRILLDOWN,
        source_table="main",
        source_columns=["Category", "Customer - Name"],
        measure_column="Amount",
        aggregation=m.AGG_SUM,
    )
    for name, value in overrides.items():
        setattr(panel, name, value)
    return panel


def test_a_drilldown_with_levels_and_a_number_is_fine():
    assert m.panel_problems(drilldown(), COLUMNS) == ""


def test_a_drilldown_with_one_level_has_nothing_to_drill_into():
    problem = m.panel_problems(drilldown(source_columns=["Category"]), COLUMNS)
    assert "at least two levels" in problem


def test_a_drilldown_with_no_levels_is_told_what_a_level_is():
    """The empty case says "columns to group by", not "columns to show" - the flat table's
    sentence would send the user to fix the wrong thing."""
    problem = m.panel_problems(drilldown(source_columns=[]), COLUMNS)
    assert "group by" in problem


def test_a_drilldown_with_too_many_levels_is_refused():
    levels = ["Category", "Customer - Name", "Region2", "Amount", "Category"]
    problem = m.panel_problems(drilldown(source_columns=levels), COLUMNS)
    assert str(m.MAX_DRILLDOWN_LEVELS) in problem


def test_a_drilldown_level_no_link_reaches_is_reported_like_any_other_column():
    problem = m.panel_problems(
        drilldown(source_columns=["Category", "Region"]), COLUMNS
    )
    assert "Region" in problem and "isn't reachable" in problem


def test_a_drilldown_needs_a_number_unless_it_is_counting():
    """Exactly what a card needs, because it computes exactly what a card computes - once
    per group instead of once per page."""
    assert "total" in m.panel_problems(drilldown(measure_column=""), COLUMNS)
    counting = drilldown(measure_column="", aggregation=m.AGG_COUNT)
    assert m.panel_problems(counting, COLUMNS) == ""


def test_a_drilldown_refuses_the_totals_that_are_measured_against_a_chart():
    """A percentage of total and a running total are about a chart's other bars. A tree of
    groups has no single whole and no order, so both would print a confident wrong number."""
    for aggregation in (m.AGG_PERCENT_OF_TOTAL, m.AGG_RUNNING_TOTAL):
        problem = m.panel_problems(drilldown(aggregation=aggregation), COLUMNS)
        assert "drill-down" in problem, aggregation


def test_a_flat_table_still_ignores_the_measure_and_the_aggregation():
    """The whole reason phase 40 needed no new field and no migration: a flat table reads
    `source_columns` as the columns to show and never looks at the rest."""
    flat = m.PanelSpec(
        visual_type=m.VISUAL_TABLE, sub_type=m.TABLE_FLAT, source_table="main",
        source_columns=["Amount"], measure_column="", aggregation=m.AGG_SUM,
    )
    assert m.panel_problems(flat, COLUMNS) == ""


# ------------------------------------- several totals on a drill-down (phase 41)


def test_a_drilldown_can_total_several_numbers():
    """The phase's whole point. A chart puts its extra numbers on an axis; a table puts each
    one in a column of its own, which is why the check had to leave the chart branch."""
    panel = drilldown(extra_measures=[
        {"column": "Region2", "aggregation": "average", "axis": m.AXIS_LEFT},
        {"column": "Amount", "aggregation": "minimum", "axis": m.AXIS_RIGHT},
    ])
    assert m.panel_problems(panel, COLUMNS) == ""


def test_a_drilldowns_extra_column_must_be_reachable_too():
    """The same sentence a chart's extra measure gets, because it is the same mistake."""
    panel = drilldown(extra_measures=m.clean_measures(
        [{"column": "Nowhere", "aggregation": m.AGG_SUM, "axis": m.AXIS_LEFT}]
    ))
    assert "isn't reachable" in m.panel_problems(panel, COLUMNS)


def test_a_flat_table_cannot_carry_extra_totals():
    """A flat table prints the rows themselves, so there is nothing for a second total to be
    a total of - and saying so is what stops four measures vanishing silently."""
    flat = m.PanelSpec(
        visual_type=m.VISUAL_TABLE, sub_type=m.TABLE_FLAT, source_table="main",
        source_columns=["Amount"],
        extra_measures=[{"column": "Amount", "aggregation": m.AGG_SUM, "axis": m.AXIS_LEFT}],
    )
    assert "drill-down table can show several" in m.panel_problems(flat, COLUMNS)


def test_all_measures_lists_the_panels_own_first():
    """The order is the arrangement: column one is the total the user named first."""
    panel = drilldown(extra_measures=[
        {"column": "Region2", "aggregation": "average", "axis": m.AXIS_LEFT},
    ])
    assert [one["column"] for one in panel.all_measures()] == ["Amount", "Region2"]
    assert [one["aggregation"] for one in panel.all_measures()] == [m.AGG_SUM, "average"]


def test_the_cap_counts_the_panels_own_measure():
    """Four numbers in all, the same cap a chart has and for the same reason: past four the
    reader is scanning columns rather than comparing them."""
    asked = [{"column": "Amount", "aggregation": m.AGG_SUM, "axis": m.AXIS_LEFT}] * 9
    panel = drilldown(extra_measures=m.clean_measures(asked))
    assert len(panel.all_measures()) == m.MAX_MEASURES_PER_CHART


def test_the_rows_column_shows_for_one_total_and_hides_for_several():
    """The complaint that started the phase: a table asked for five numbers printed a sixth
    column nobody asked for. One total leaves room for the count; five do not."""
    assert drilldown().wants_row_count() is True
    several = drilldown(extra_measures=[
        {"column": "Amount", "aggregation": "average", "axis": m.AXIS_LEFT},
    ])
    assert several.wants_row_count() is False


def test_asking_for_the_rows_column_overrides_either_default():
    """Neither answer is unreachable, which is what makes it a setting rather than a rule."""
    several = drilldown(
        extra_measures=[{"column": "Amount", "aggregation": "average",
                         "axis": m.AXIS_LEFT}],
        properties={"row_count": True},
    )
    assert several.wants_row_count() is True
    assert drilldown(properties={"row_count": False}).wants_row_count() is False


def test_the_rows_column_setting_on_anything_else_is_reported():
    """Only a drill-down has groups to count the rows of. Said rather than dropped, which is
    the whole job of `property_problems`."""
    flat = m.PanelSpec(
        visual_type=m.VISUAL_TABLE, sub_type=m.TABLE_FLAT, source_table="main",
        source_columns=["Amount"], properties={"row_count": True},
    )
    assert "drill-down" in m.property_problems(flat)
    assert m.property_problems(drilldown(properties={"row_count": True})) == ""


def test_row_count_round_trips_through_the_properties_text():
    """`clean_properties` and `properties_text` are each other's inverse, or a setting is
    lost the next time a round re-states the visual it is editing."""
    assert m.clean_properties("row_count:no") == {"row_count": False}
    assert m.properties_text({"row_count": True}) == "row_count:yes"


def test_every_table_sub_type_has_a_label():
    """The catalog the AI is shown is rendered from these, so a sub-type with no label is a
    shape offered to the model as a bare keyword."""
    assert set(m.TABLE_LABELS) == set(m.TABLE_SUB_TYPES)


# ------------------------------------------------------------------ properties


def test_clean_properties_keeps_only_what_it_recognises():
    cleaned = m.clean_properties("border:yes, radius:0.5, format:currency, height:400")
    assert cleaned == {"border": True, "radius": 0.5, "number_format": "currency", "height": 400}


def test_clean_properties_drops_an_unknown_key():
    """The whole CSS-injection defence: nothing unrecognised survives into the page."""
    assert m.clean_properties("background:url(javascript:alert(1))") == {}


def test_clean_properties_clamps_values_that_are_out_of_range():
    cleaned = m.clean_properties("radius:99, height:99999")
    assert cleaned["radius"] == m.MAX_CORNER_RADIUS
    assert cleaned["height"] == m.MAX_PANEL_HEIGHT


def test_clean_properties_ignores_a_value_it_cannot_read():
    assert m.clean_properties("radius:wide, height:tall") == {}


def test_clean_properties_rejects_a_number_format_it_does_not_offer():
    assert m.clean_properties("format:klingon") == {}


def test_properties_text_round_trips_through_clean_properties():
    original = m.clean_properties("border:no, radius:0.5, format:percent, height:300")
    assert m.clean_properties(m.properties_text(original)) == original


# ------------------------------------------------------------------ serialisation


def test_a_dashboard_survives_a_round_trip():
    spec = m.DashboardSpec(title="Sales", subtitle="Monthly", main_table="Transactions",
                           theme=m.THEME_DARK, filter_position=m.FILTER_LEFT)
    panel = chart(top_n=5, title="By category")
    panel.properties = m.clean_properties("format:currency")
    spec.panels.extend([panel, filter_panel()])

    back = m.from_json(m.to_json(spec))

    assert (back.title, back.theme, back.filter_position) == ("Sales", m.THEME_DARK, m.FILTER_LEFT)
    assert [p.panel_id for p in back.panels] == [p.panel_id for p in spec.panels]
    assert back.panels[0].top_n == 5
    assert back.panels[0].properties == {"number_format": "currency"}


def test_a_dashboard_saved_with_the_old_guidance_setting_still_opens():
    """Phase 36 took the guidance box away. A Task saved while it existed carries the
    setting, and must open rather than fail on a key the spec no longer has."""
    raw = json.dumps({
        "version": 1,
        "dashboard": {"title": "Older", "ai_guidance": "always show currency in INR"},
        "panels": [],
    })
    back = m.from_json(raw)
    assert back.title == "Older"
    assert not hasattr(back, "ai_guidance")


def test_a_logo_survives_a_round_trip():
    spec = m.DashboardSpec(logo_bytes=b"\x89PNG\r\n", logo_mime="image/png")
    back = m.from_json(m.to_json(spec))
    assert back.logo_bytes == b"\x89PNG\r\n"
    assert back.logo_data_uri().startswith("data:image/png;base64,")


def test_a_panel_with_an_unknown_visual_type_is_repaired_rather_than_dropped():
    """Losing a panel silently is worse than mis-typing one."""
    raw = json.dumps({
        "version": m.SCHEMA_VERSION,
        "panels": [{"panel_id": "abc", "visual_type": "hologram", "sub_type": "spiral"}],
    })
    back = m.from_json(raw)
    assert len(back.panels) == 1
    assert back.panels[0].visual_type == m.VISUAL_CHART
    assert back.panels[0].sub_type == m.CHART_BAR


def test_properties_are_re_cleaned_on_the_way_back_in():
    """A payload edited by hand gains nothing by carrying something else."""
    raw = json.dumps({
        "version": m.SCHEMA_VERSION,
        "panels": [{"panel_id": "abc", "properties": {"background": "url(x)", "border": "yes"}}],
    })
    assert m.from_json(raw).panels[0].properties == {"border": True}


def test_an_unreadable_top_n_falls_back_to_showing_everything():
    raw = json.dumps({"panels": [{"panel_id": "abc", "top_n": "lots"}]})
    assert m.from_json(raw).panels[0].top_n == 0


def test_malformed_json_is_refused():
    with pytest.raises(DashboardStorageError):
        m.from_json("{not json")


def test_a_payload_that_is_not_an_object_is_refused():
    with pytest.raises(DashboardStorageError):
        m.from_json("[1, 2, 3]")


def test_a_dashboard_from_a_newer_version_is_refused():
    raw = json.dumps({"version": m.SCHEMA_VERSION + 1, "panels": []})
    with pytest.raises(DashboardStorageError):
        m.from_json(raw)


def test_an_empty_payload_reads_as_an_empty_dashboard():
    """What a Task saved before phase 32 looks like."""
    spec = m.from_json("{}")
    assert spec.panels == []
    assert spec.display_title() == m.UNTITLED_DASHBOARD


# ------------------------------------------------- phase 34: the wider vocabulary

#: The same tables, with one column known to hold dates - what a running total needs.
DATES = frozenset({"TxnDate"})
DATED_COLUMNS = {"main": COLUMNS["main"] + ["TxnDate"]}


def test_a_percentage_of_total_on_a_card_is_refused_with_a_reason():
    """There is nothing on a card for the percentage to be *of* - the honest answer would
    always be 100%. On a chart the other bars are the total, which is why it works there."""
    card = chart(visual_type=m.VISUAL_CARD, aggregation=m.AGG_PERCENT_OF_TOTAL, group_by="")
    problem = m.panel_problems(card, COLUMNS)

    assert "percentage of total" in problem.lower()
    assert "chart" in problem


def test_a_percentage_of_total_on_a_chart_is_fine():
    assert m.panel_problems(chart(aggregation=m.AGG_PERCENT_OF_TOTAL), COLUMNS) == ""


def test_a_running_total_over_something_with_no_order_is_refused():
    """A running total adds each category to the ones before it, so the order has to mean
    something. Over unordered labels the climbing line is an accident of sorting."""
    panel = chart(aggregation=m.AGG_RUNNING_TOTAL, group_by="Category")
    problem = m.panel_problems(panel, DATED_COLUMNS, DATES)

    assert "date" in problem.lower()


def test_a_running_total_over_a_date_is_accepted():
    panel = chart(aggregation=m.AGG_RUNNING_TOTAL, group_by="TxnDate")
    assert m.panel_problems(panel, DATED_COLUMNS, DATES) == ""


def test_a_running_total_is_left_alone_when_the_dates_are_not_known():
    """`None` means "not known here" rather than "nothing is a date" - the check is skipped
    rather than guessed at, or a preview with no type information would refuse everything."""
    panel = chart(aggregation=m.AGG_RUNNING_TOTAL, group_by="Category")
    assert m.panel_problems(panel, COLUMNS) == ""


def test_a_histogram_needs_no_breakdown():
    """The one chart with no group-by, and the assumption this phase had to unpick."""
    panel = chart(sub_type=m.CHART_HISTOGRAM, group_by="")
    assert m.panel_problems(panel, COLUMNS) == ""


def test_a_histogram_still_needs_the_number_it_bins():
    panel = chart(sub_type=m.CHART_HISTOGRAM, group_by="", measure_column="",
                  aggregation=m.AGG_COUNT)
    assert "spread" in m.panel_problems(panel, COLUMNS)


def test_a_stacked_bar_asks_for_the_second_breakdown_it_needs():
    problem = m.panel_problems(chart(sub_type=m.CHART_BAR_STACKED), COLUMNS)
    assert "colour it by" in problem

    fixed = chart(sub_type=m.CHART_BAR_STACKED, colour_by="Region2")
    assert m.panel_problems(fixed, COLUMNS) == ""


def test_a_combo_chart_asks_for_its_second_number():
    problem = m.panel_problems(chart(sub_type=m.CHART_COMBO), COLUMNS)
    assert "second number" in problem

    unreachable = chart(sub_type=m.CHART_COMBO, extra_measures=m.clean_measures(
        [{"column": "Nowhere", "aggregation": "sum", "axis": "right"}]))
    assert "isn't reachable" in m.panel_problems(unreachable, COLUMNS)

    fixed = chart(sub_type=m.CHART_COMBO, extra_measures=m.clean_measures(
        [{"column": "Region2", "aggregation": "sum", "axis": "right"}]))
    assert m.panel_problems(fixed, COLUMNS) == ""


def test_clean_properties_keeps_a_real_currency_and_drops_an_invented_one():
    """The same discipline every other property follows: the code goes straight into the
    reader's browser, so only the nine on the list get there."""
    assert m.clean_properties("format:currency, currency:inr")["currency"] == "INR"
    assert "currency" not in m.clean_properties("format:currency, currency:BITCOIN")


def test_properties_text_round_trips_the_currency():
    cleaned = m.clean_properties("format:currency, currency:USD")
    assert m.properties_text(cleaned) == "format:currency, currency:USD"
    assert m.clean_properties(m.properties_text(cleaned)) == cleaned


def test_extra_measures_survive_being_saved_and_read_back():
    measures = m.clean_measures([
        {"column": "Amount", "aggregation": "minimum", "axis": "left"},
        {"column": "Amount", "aggregation": "count", "axis": "right"},
    ])
    spec = m.DashboardSpec(panels=[chart(extra_measures=measures)])

    assert m.from_json(m.to_json(spec)).panels[0].extra_measures == measures


def test_an_extra_measure_is_kept_only_when_every_part_of_it_is_known():
    assert m.clean_measures([{"column": "Amount", "aggregation": "nonsense",
                             "axis": "left"}]) == []
    assert m.clean_measures([{"column": "Amount", "aggregation": "sum",
                             "axis": "sideways"}]) == []
    assert m.clean_measures([{"column": "", "aggregation": "sum",
                             "axis": "left"}]) == []
    assert m.clean_measures("Amount:sum:left") == []

    # A percentage of total is measured against the rest of the chart, so a second
    # number sharing that chart is two answers to a question that has one.
    assert m.clean_measures([{"column": "Amount", "aggregation": "percent_of_total",
                             "axis": "left"}]) == []

    # Counting rows needs no column, which is what makes "salary and headcount" work.
    assert len(m.clean_measures([{"column": "", "aggregation": "count",
                                 "axis": "right"}])) == 1


def test_a_chart_is_held_to_four_numbers_in_all():
    too_many = [{"column": "Amount", "aggregation": "sum", "axis": "left"}] * 9
    assert len(m.clean_measures(too_many)) == m.MAX_MEASURES_PER_CHART - 1


def test_a_shape_that_draws_one_number_says_so_rather_than_drawing_the_wrong_thing():
    pie = chart(sub_type=m.CHART_PIE, extra_measures=m.clean_measures(
        [{"column": "Amount", "aggregation": "minimum", "axis": "left"}]))
    assert "one number" in m.panel_problems(pie, COLUMNS)


def test_a_dashboard_saved_before_this_phase_still_loads():
    """A dashboard saved with `measure_column_2` opens showing what it always showed:
    the second number as a line, against the right-hand axis."""
    older = json.loads(m.to_json(m.DashboardSpec(
        panels=[chart(sub_type=m.CHART_COMBO, aggregation="average")])))
    for panel in older["panels"]:
        panel.pop("extra_measures")
        panel["measure_column_2"] = "Quantity"

    read_back = m.from_json(json.dumps(older))

    assert read_back.panels[0].extra_measures == [
        {"column": "Quantity", "aggregation": "average", "axis": m.AXIS_RIGHT}
    ]


def test_a_dashboard_saved_with_neither_spelling_still_loads():
    older = json.loads(m.to_json(m.DashboardSpec(panels=[chart()])))
    for panel in older["panels"]:
        panel.pop("extra_measures")

    assert m.from_json(json.dumps(older)).panels[0].extra_measures == []


# ------------------------------------------------------- phase 38: the look settings


def test_every_look_setting_is_kept_only_when_it_is_one_of_the_named_choices():
    """The whole reason `clean_properties` exists, applied to seven new settings at once:
    these values are written into a page a reader opens, so a word off the list never
    arrives."""
    kept = m.clean_properties(
        "labels:yes, legend:bottom, axis_titles:no, colour:green, "
        "size:tall, width:full, card_size:large"
    )
    assert kept == {
        "labels": True, "legend": "bottom", "axis_titles": False, "colour": "green",
        "size": "tall", "width": "full", "card_size": "large",
    }

    invented = m.clean_properties(
        "legend:diagonal, colour:#ff0000, size:enormous, width:120px, card_size:huge, "
        "labels:maybe"
    )
    assert invented == {}


def test_a_look_setting_survives_being_saved_and_read_back():
    cleaned = m.clean_properties("labels:yes, legend:none, colour:purple, size:short")
    assert m.clean_properties(m.properties_text(cleaned)) == cleaned

    spec = m.DashboardSpec(panels=[chart(properties=cleaned)])
    assert m.from_json(m.to_json(spec)).panels[0].properties == cleaned


def test_size_sets_the_height_and_an_exact_height_still_wins():
    """`height:420` is what a dashboard saved before phase 38 carries, so it has to keep
    meaning what it meant - `size` is the word the AI is taught to use from now on."""
    assert chart(properties=m.clean_properties("size:tall")).height() == m.PANEL_SIZES["tall"]
    assert chart(properties=m.clean_properties("size:short")).height() == m.PANEL_SIZES["short"]
    assert chart(properties=m.clean_properties("size:tall, height:420")).height() == 420
    assert chart().height() > 0


def test_a_setting_a_shape_cannot_use_is_reported_rather_than_silently_dropped():
    """The phase 38 bug in one sentence: the user asked for something, did not get it, and
    nothing said so."""
    heatmap = chart(sub_type=m.CHART_HEATMAP, colour_by="Region2",
                    properties=m.clean_properties("labels:yes"))
    assert "data labels" in m.property_problems(heatmap)

    split = chart(colour_by="Region2", properties=m.clean_properties("colour:green"))
    assert "Region2" in m.property_problems(split)

    fine = chart(properties=m.clean_properties("labels:yes, colour:green"))
    assert m.property_problems(fine) == ""
