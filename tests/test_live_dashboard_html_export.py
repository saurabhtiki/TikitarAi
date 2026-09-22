"""The exported page: what reaches it, and what cannot.

This file is the security review of the export written down as assertions. The export takes
user text, user data and three vendored JavaScript bundles and welds them into one document
that runs script - so each of the three tests under "escaping" corresponds to one row of the
table in `html_export`'s module docstring.

The rest assert the page is genuinely self-contained: the libraries are inlined, nothing is
fetched, and a panel that cannot be drawn says so on the page rather than vanishing.
"""

import json

import pandas as pd
import pytest

from live_dashboard import html_export
from live_dashboard import model as m
from live_dashboard.exceptions import DashboardExportError


FRAME = pd.DataFrame(
    {
        "Amount": [5000.0, 3200.0],
        "Category": ["Hardware", "Consumables"],
        "Customer - Name": ["ABC", "XYZ"],
        "When": pd.to_datetime(["2026-01-01", "2026-02-01"]),
    }
)


def dashboard(*panels: m.PanelSpec, **settings) -> m.DashboardSpec:
    spec = m.DashboardSpec(title="Sales", **settings)
    spec.panels.extend(panels)
    return spec


def chart(**overrides) -> m.PanelSpec:
    panel = m.PanelSpec(
        visual_type=m.VISUAL_CHART, sub_type=m.CHART_BAR, source_table="main",
        measure_column="Amount", group_by="Category", title="By category",
    )
    for name, value in overrides.items():
        setattr(panel, name, value)
    return panel


def payload_of(html: str) -> dict:
    """The embedded JSON, read back the way the page's own script reads it."""
    block = html.split('<script id="dashboard-payload" type="application/json">')[1]
    return json.loads(block.split("</script>")[0])


# ------------------------------------------------------------------ self-contained


def test_the_page_carries_all_three_vendored_bundles():
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    for name in html_export.VENDOR_FILES:
        assert html_export._asset(name)[:200] in html, name


def test_the_page_fetches_nothing_from_the_network():
    """The whole requirement: it opens the same on a machine with no connection."""
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    body = html.replace(html_export._asset("vega.min.js"), "")
    body = body.replace(html_export._asset("vega-lite.min.js"), "")
    body = body.replace(html_export._asset("vega-embed.min.js"), "")
    assert "src=\"http" not in body
    assert "href=\"http" not in body


def test_the_data_travels_as_columns_and_rows():
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    table = payload_of(html)["tables"]["main"]
    assert table["columns"][:2] == ["Amount", "Category"]
    assert len(table["rows"]) == 2


def test_each_chart_carries_its_spec_and_both_theme_configs():
    """The toggle has no server to ask, so both looks must already be in the file."""
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    panel = payload_of(html)["panels"][0]
    assert panel["spec"]["mark"]["type"] == "bar"
    assert panel["light_config"]["axis"] != panel["dark_config"]["axis"]


# ------------------------------------------------------------------ escaping


def test_a_panel_title_containing_markup_is_escaped():
    spec = dashboard(chart(title="<b>Bold</b><script>alert(1)</script>"))
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert "<b>Bold</b>" not in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;b&gt;Bold" in html


def test_a_dashboard_title_containing_markup_is_escaped():
    spec = dashboard(chart())
    spec.title = '"><img src=x onerror=alert(1)>'
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert "<img src=x" not in html


def test_a_cell_value_containing_a_closing_script_tag_cannot_break_the_payload_block():
    """The one injection a JSON-in-script block invites, and the reason for the escaping."""
    hostile = pd.DataFrame({
        "Amount": [1.0],
        "Category": ["</script><img src=x onerror=alert(1)>"],
    })
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": hostile})

    block = html.split('<script id="dashboard-payload" type="application/json">')[1]
    assert "</script>" not in block.split("</script>")[0] + ""
    assert "<img src=x" not in html
    # ...and the value survives intact as data.
    assert payload_of(html)["tables"]["main"]["rows"][0][1].startswith("</script>")


def test_a_properties_cell_cannot_reach_the_page_as_css():
    panel = chart(properties=m.clean_properties("background:url(javascript:alert(1))"))
    html = html_export.build_dashboard_html(dashboard(panel), {"main": FRAME})
    assert "javascript:alert" not in html


def test_the_logo_is_a_data_uri_rather_than_a_link():
    spec = dashboard(chart())
    spec.logo_bytes, spec.logo_mime = b"\x89PNG\r\n", "image/png"
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert 'src="data:image/png;base64,' in html


# ------------------------------------------------------------------ panels with problems


def test_a_panel_naming_a_missing_column_shows_a_warning_on_the_page():
    spec = dashboard(chart(group_by="Region"))
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert "panel-warning" in html
    assert "Region" in html


def test_a_panel_with_a_problem_is_not_handed_to_the_runtime():
    """Drawing it would just produce a second, worse failure in the browser."""
    spec = dashboard(chart(group_by="Region"))
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert payload_of(html)["panels"] == []


def test_a_good_panel_still_draws_when_another_one_is_broken():
    spec = dashboard(chart(), chart(group_by="Region", title="Broken"))
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert len(payload_of(html)["panels"]) == 1
    assert "panel-warning" in html


# ------------------------------------------------------------------ layout and filters


def test_panels_sharing_a_row_number_are_rendered_in_one_row():
    left, right = chart(title="Left"), chart(title="Right")
    left.row_number = right.row_number = 1
    html = html_export.build_dashboard_html(dashboard(left, right), {"main": FRAME})
    assert html.count('class="panel-row"') == 1


def test_the_filter_position_reaches_the_layout():
    spec = dashboard(chart(), filter_position=m.FILTER_LEFT)
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert "filters-left" in html


def test_a_filter_becomes_a_widget_slot_and_a_payload_entry():
    filter_panel = m.PanelSpec(
        visual_type=m.VISUAL_FILTER, sub_type=m.FILTER_DROPDOWN,
        source_table="main", source_columns=["Customer - Name"], title="Customer",
    )
    html = html_export.build_dashboard_html(dashboard(chart(), filter_panel), {"main": FRAME})
    assert f'id="filter-{filter_panel.panel_id}"' in html
    assert payload_of(html)["filters"][0]["column"] == "Customer - Name"


def test_an_empty_dashboard_says_so_rather_than_rendering_blank():
    html = html_export.build_dashboard_html(dashboard(), {"main": FRAME})
    assert "no visuals yet" in html.lower()


def test_the_theme_the_page_opens_in_is_on_the_root_element():
    spec = dashboard(chart(), theme=m.THEME_DARK)
    html = html_export.build_dashboard_html(spec, {"main": FRAME})
    assert 'data-theme="dark"' in html


# ------------------------------------------------------------------ assets


def test_no_vendored_bundle_contains_a_closing_script_tag():
    """It would end the inlining block early and corrupt every exported page."""
    for name in html_export.VENDOR_FILES:
        assert "</script" not in html_export._asset(name).lower(), name


def test_a_missing_asset_is_reported_rather_than_producing_a_chartless_page():
    html_export._asset.cache_clear()
    with pytest.raises(DashboardExportError):
        html_export._asset("not-a-real-asset.js")
    html_export._asset.cache_clear()


# ------------------------------------------------------------------ chart sizing


def test_the_page_gives_the_chart_wrapper_a_real_width():
    """The bug this guards is silent and total: no error, just empty panels.

    Vega-Lite's `width: "container"` asks vega-embed's wrapper how wide it is, and
    vega-embed's own stylesheet makes that wrapper `display: inline-block` - which takes its
    width from its contents. Each waits on the other, the width resolves to zero and nothing
    is drawn. Both halves of the fix are asserted here because either one alone leaves blank
    charts: the host must carry the class, and the rule must out-specify vega-embed's, whose
    stylesheet is injected after this page's.
    """
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    assert 'class="chart-host"' in html
    assert ".panel .vega-embed { display: block; width: 100%; }" in html


# ------------------------------------------------------------------ filter widgets


def test_a_multiselect_filter_is_tick_boxes_rather_than_a_native_multi_select():
    """`<select multiple>` needs ctrl-click to reach a second value, and - the complaint that
    prompted this - offers no way at all to get back to *everything*. Tick boxes with an All
    row need no instructions.

    Asserted against the runtime asset rather than the page, so an inlined vendor bundle that
    happens to contain the word can't make this pass or fail by accident.
    """
    runtime = html_export._asset("runtime.js")
    html_export._asset.cache_clear()

    assert "buildChoiceList" in runtime
    assert "select.multiple" not in runtime
    assert 'className = "choice choice-all"' in runtime


def test_every_filter_widget_says_how_to_clear_itself():
    """Clear all used to walk the DOM for `[data-filter-input]`, which a widget made of many
    tick boxes can't answer for. Each widget now registers its own reset instead - so a
    widget added later that forgets to is a missing line here, not a chip that won't clear."""
    runtime = html_export._asset("runtime.js")
    html_export._asset.cache_clear()

    assert "data-filter-input" not in runtime
    # One per widget kind: dropdown, tick list, range slider, date range. They go onto the
    # filter's own `resets` list since phase 37, because the same functions now serve Clear
    # all and that one filter's Clear button.
    assert runtime.count("resets.push(") == 4
    # And each filter hands the pair of them to Clear all, exactly once.
    assert runtime.count("resetters.push(") == 1


def test_the_page_ships_the_tick_box_styling():
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    assert ".choice-list {" in html


# ------------------------------------------------- phase 37: layout defaults and Clear


def test_a_new_dashboard_puts_its_filters_down_the_left():
    """A top strip pushes the first row of visuals off the screen as soon as there are three
    filters, which is what real use showed."""
    spec = m.DashboardSpec()
    assert spec.filter_position == m.FILTER_LEFT

    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    assert "filters-left" in html


def test_a_dashboard_saved_with_filters_on_top_still_opens_with_them_on_top():
    """The new default is a default, not a retrofit: a page someone laid out by hand keeps
    the layout they chose."""
    stored = m.to_json(dashboard(chart(), filter_position=m.FILTER_TOP))
    assert m.from_json(stored).filter_position == m.FILTER_TOP


def test_panel_titles_are_centred():
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    assert ".panel h2" in html and "text-align: center" in html


def test_every_filter_gets_a_clear_button_of_its_own():
    """Clear all is the wrong tool when a reader has set four filters and wants three."""
    filter_panel = m.PanelSpec(
        visual_type=m.VISUAL_FILTER, sub_type=m.FILTER_MULTISELECT,
        source_table="main", source_columns=["Customer - Name"], title="Customer",
    )
    spec = dashboard(chart(), filter_panel)
    html = html_export.build_dashboard_html(spec, {"main": FRAME})

    for panel in spec.filters():
        assert f'id="clear-{panel.panel_id}"' in html


def test_the_tick_list_opens_only_when_it_is_clicked():
    """A list of three hundred customers sitting open is most of a screen spent on a control
    nobody has touched yet - so it is a `details`, with what is chosen on its summary."""
    runtime = html_export._asset("runtime.js")
    html_export._asset.cache_clear()

    assert 'createElement("details")' in runtime
    assert 'createElement("summary")' in runtime
    assert "summary.appendChild(readout)" in runtime


# ------------------------------------------------- phase 34: the wider vocabulary


def test_every_shape_and_every_total_survives_a_real_export():
    """The end-to-end guard for this phase.

    A shape that builds a spec in isolation can still fall over on the way into the file -
    and the cost of finding that out later is a blank box in a reader's inbox, discovered
    after the file has been emailed.
    """
    panels = [
        chart(sub_type=sub_type, colour_by="Customer - Name",
              # Only the shapes that can draw a second number are given one; the rest would
              # rightly refuse it, and this test is about every shape reaching the file.
              extra_measures=m.clean_measures(
                  [{"column": "Amount", "aggregation": "sum", "axis": "right"}]
                  if sub_type in m.MULTI_MEASURE_CHARTS else []),
              group_by="" if sub_type == m.CHART_HISTOGRAM else "Category",
              title=f"A {sub_type}", row_number=index + 1)
        for index, sub_type in enumerate(m.CHART_SUB_TYPES)
    ]
    panels += [
        chart(aggregation=aggregation, group_by="When", title=f"Totalled by {aggregation}",
              row_number=99)
        for aggregation in m.DASHBOARD_AGGREGATIONS
    ]

    html = html_export.build_dashboard_html(dashboard(*panels), {"main": FRAME})
    drawn = payload_of(html)["panels"]

    assert len(drawn) == len(panels), "a visual was dropped on the way into the file"
    for entry in drawn:
        assert entry["spec"], entry["panel_id"]


def test_a_card_carries_its_currency_into_the_page():
    """Without this the UI's own example - "always show currency in INR" - cannot work:
    format:currency printed 1,234.00 with no symbol at all."""
    card = chart(visual_type=m.VISUAL_CARD, group_by="", sub_type=m.CARD_SINGLE,
                 properties=m.clean_properties("format:currency, currency:INR"))
    entry = payload_of(html_export.build_dashboard_html(dashboard(card), {"main": FRAME}))

    assert entry["panels"][0]["currency"] == "INR"
    assert entry["panels"][0]["number_format"] == "currency"


def test_a_histogram_offers_no_cross_filter_field():
    """Its bars are bins rather than values, so there is nothing a reader would filter by -
    and wiring one to a field that isn't in the spec would do nothing visible but confusing."""
    built = chart(sub_type=m.CHART_HISTOGRAM, group_by="")
    entry = payload_of(html_export.build_dashboard_html(dashboard(built), {"main": FRAME}))
    assert entry["panels"][0]["select_field"] == ""


def test_a_running_total_over_labels_is_refused_by_the_exporter_too():
    """The app refuses this in the Add dialog and in the spec table; the file has to agree.

    It nearly didn't: the exporter worked out which columns held dates and then checked the
    panels without telling `panel_problems` about them, so the one rule that needs those
    dates was skipped in the only place a reader ever sees.
    """
    climbing = chart(aggregation=m.AGG_RUNNING_TOTAL, group_by="Category",
                     title="Running total by category")
    html = html_export.build_dashboard_html(dashboard(climbing), {"main": FRAME})

    assert "needs a date to run along" in html
    assert payload_of(html)["panels"] == []  # not handed to the runtime to draw anyway


def test_a_running_total_over_a_date_still_exports():
    dated = chart(aggregation=m.AGG_RUNNING_TOTAL, group_by="When", title="Running total")
    html = html_export.build_dashboard_html(dashboard(dated), {"main": FRAME})

    assert "needs a date to run along" not in html
    assert len(payload_of(html)["panels"]) == 1


# ------------------------------------------------------- phase 38: the look settings


def test_a_full_width_panel_and_a_big_card_carry_their_classes_into_the_page():
    """Classes rather than inline numbers: the stylesheet keeps deciding what "full" and
    "large" actually measure, and a class name from a fixed list carries nothing typed."""
    card = m.PanelSpec(visual_type=m.VISUAL_CARD, source_table="main", measure_column="Amount",
                       title="Total", properties=m.clean_properties("card_size:large"))
    wide = chart(row_number=2, properties=m.clean_properties("width:full, size:tall"))

    html = html_export.build_dashboard_html(dashboard(card, wide), {"main": FRAME})

    assert 'class="panel card-large"' in html
    assert 'class="panel panel-full"' in html
    assert f"min-height:{m.PANEL_SIZES['tall']}px" in html
    assert ".panel.panel-full" in html and ".panel.card-large .card-value" in html


def test_a_panel_with_no_look_settings_is_still_a_plain_panel():
    html = html_export.build_dashboard_html(dashboard(chart()), {"main": FRAME})
    assert 'class="panel"' in html


def test_nothing_typed_into_a_look_setting_reaches_the_page_as_styling():
    """`clean_properties` is the gate; this is the proof that the exporter has no way round
    it. Every one of these is dropped before a class or an inline style is built."""
    nasty = chart(properties=m.clean_properties(
        "width:100%;background:url(evil.png), colour:</style><script>alert(1)</script>, "
        "card_size:huge, size:9999px"
    ))

    html = html_export.build_dashboard_html(dashboard(nasty), {"main": FRAME})

    assert "evil.png" not in html
    assert "alert(1)" not in html
    assert 'class="panel"' in html
    assert "min-height" not in html.split("<style>")[0] + html.split("</style>")[-1]


def test_the_disclosure_arrows_are_css_escapes_and_not_stray_control_characters():
    """A heredoc once turned `\\25be` into a raw 0x15 byte plus the letters "be", so every
    exported dashboard's collapsed filter showed the word "be" instead of an arrow."""
    css = html_export._asset("dashboard.css")

    assert '"\\25be"' in css and '"\\25b4"' in css
    assert not [char for char in css if ord(char) < 32 and char not in "\t\r\n"]


# --------------------------------------------- phase 39: one filter, every related table


def test_a_page_over_two_sibling_tables_carries_both_and_one_filter_over_them():
    """The plan's end-to-end case, as far as Python can take it.

    Employee Master with two children that know nothing about each other: a salary total,
    an attendance total, and one Department filter above them. What the export has to get
    right is that **both** tables reach the page, each carrying the same
    `EmployeeMaster - Department` column, and that the filter is a single panel rather than
    one per table. That the filter then narrows both is `test_live_dashboard_runtime.py`'s
    job, because that part happens in the browser.
    """
    salary = pd.DataFrame({
        "Amount": [50000.0, 60000.0],
        "EmployeeMaster - Department": ["HR", "Ops"],
    })
    attendance = pd.DataFrame({
        "Days": [20, 25],
        "EmployeeMaster - Department": ["HR", "Ops"],
    })

    spec = dashboard(
        m.PanelSpec(visual_type=m.VISUAL_CARD, source_table="Salary",
                    measure_column="Amount", aggregation=m.AGG_SUM, title="Total salary"),
        m.PanelSpec(visual_type=m.VISUAL_CARD, source_table="Attendance",
                    measure_column="Days", aggregation=m.AGG_SUM, title="Days present"),
        m.PanelSpec(visual_type=m.VISUAL_FILTER, sub_type=m.FILTER_MULTISELECT,
                    source_table="Salary", source_columns=["EmployeeMaster - Department"],
                    title="Department"),
    )

    document = payload_of(html_export.build_dashboard_html(
        spec, {"Salary": salary, "Attendance": attendance}
    ))

    assert sorted(document["tables"]) == ["Attendance", "Salary"]
    for name in ("Salary", "Attendance"):
        assert "EmployeeMaster - Department" in document["tables"][name]["columns"], name

    assert len(document["filters"]) == 1
    assert document["filters"][0]["column"] == "EmployeeMaster - Department"
    assert {panel["source_table"] for panel in document["panels"]} == {"Salary", "Attendance"}


# --------------------------------------------------------------------------------------
# Drill-down tables (phase 40)
# --------------------------------------------------------------------------------------


def drilldown(**overrides) -> m.PanelSpec:
    panel = m.PanelSpec(
        visual_type=m.VISUAL_TABLE, sub_type=m.TABLE_DRILLDOWN, source_table="main",
        source_columns=["Category", "Customer - Name"], measure_column="Amount",
        aggregation=m.AGG_SUM, title="Sales by category",
    )
    for name, value in overrides.items():
        setattr(panel, name, value)
    return panel


def test_a_drilldown_reaches_the_runtime_with_its_levels_and_its_total():
    """The browser does the grouping, so everything it needs has to be in the payload: the
    levels in order, the number, and how to total it."""
    html = html_export.build_dashboard_html(dashboard(drilldown()), {"main": FRAME})
    entry = payload_of(html)["panels"][0]

    assert entry["sub_type"] == m.TABLE_DRILLDOWN
    assert entry["source_columns"] == ["Category", "Customer - Name"]
    assert entry["measure_column"] == "Amount"
    assert entry["aggregation"] == m.AGG_SUM
    assert "Amount" in entry["measure_label"]


def test_a_drilldown_has_no_search_box():
    """It shows totals per group, not the rows a search would look through - so the box
    would be a control that finds nothing."""
    html = html_export.build_dashboard_html(dashboard(drilldown()), {"main": FRAME})
    assert "<input class=\"table-search\"" not in html

    flat = m.PanelSpec(visual_type=m.VISUAL_TABLE, source_table="main",
                       source_columns=["Amount"], title="Every row")
    assert "<input class=\"table-search\"" in html_export.build_dashboard_html(
        dashboard(flat), {"main": FRAME}
    )
