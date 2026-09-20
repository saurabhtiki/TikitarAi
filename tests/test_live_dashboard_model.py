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


def test_duplicate_panel_shares_no_mutable_state_with_the_original():
    spec = m.DashboardSpec()
    panel = m.add_panel(spec)
    panel.properties = {"border": True}

    copy = m.duplicate_panel(spec, panel.panel_id)
    copy.properties["border"] = False
    copy.source_columns.append("Amount")

    assert panel.properties == {"border": True}
    assert panel.source_columns == []
    assert copy.panel_id != panel.panel_id
    # The copy sits directly after the original, not at the end.
    assert spec.panels.index(copy) == spec.panels.index(panel) + 1


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


def test_the_ai_guidance_survives_a_round_trip():
    """The user writes it once and expects it to still apply next month, so it is stored
    with the dashboard rather than left in the session."""
    spec = m.DashboardSpec(ai_guidance="always show currency in INR")
    assert m.from_json(m.to_json(spec)).ai_guidance == "always show currency in INR"


def test_a_dashboard_saved_before_the_guidance_existed_still_opens():
    raw = json.dumps({"version": 1, "dashboard": {"title": "Older"}, "panels": []})
    back = m.from_json(raw)
    assert back.title == "Older"
    assert back.ai_guidance == ""


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
