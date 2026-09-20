"""AppTest coverage for the Dashboard view (phase 32).

Driven with `AppTest.from_function` rather than through the page file, for the reason
`test_report_items_page.py` gives: the view is a fragment and a real session is all it needs.

One seam is stubbed - the engine's session, which would otherwise need a real upload. The
DuckDB connection behind it is genuine, so the flattening, the spec building and the whole
export all run for real; only the "where did these tables come from" question is answered by
the test.

The behaviours worth the most here are the ones that stay invisible until someone has done
real work: **the selection reset** (delete a row and the buttons underneath must not silently
point at whatever slid into that index), **a panel with a problem still listed rather than
dropped**, and **the download and the preview coming from one string**.
"""

import pytest
from streamlit.testing.v1 import AppTest

from live_dashboard import model as m

#: What `_scenario` replaces on the shared `engine.session` module.
STUBBED = ("connection", "table_names", "get_relationships", "rebuild_count")


@pytest.fixture(autouse=True)
def leave_the_engine_session_as_it_was_found():
    """Puts the real `engine.session` functions back after each test.

    `_scenario` stubs them by assigning onto the module, which every other test in the run
    shares - so without this, a later test asking the engine for real tables would get this
    file's two-table fixture instead and fail somewhere far away from the cause.
    """
    from engine import session as engine_session

    saved = {name: getattr(engine_session, name) for name in STUBBED}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(engine_session, name, value)


def _scenario():
    """The whole app under test: the view, standing on its own.

    Everything is imported inside, because `AppTest.from_function` re-executes this
    function's *source* in a script of its own - a closure over a module-level name would not
    be there to close over.
    """
    import duckdb

    from app_pages import dashboard_view
    from engine.relationships import Relationship

    connection = duckdb.connect()
    connection.execute("CREATE TABLE Customer(CustID INT, CustName VARCHAR, CreditPeriod INT)")
    connection.execute("CREATE TABLE Transactions(TxnID INT, CustID INT, Amount DOUBLE)")
    connection.execute("INSERT INTO Customer VALUES (5,'ABC Traders',30),(6,'XYZ Corp',45)")
    connection.execute("INSERT INTO Transactions VALUES (1,5,5000.0),(2,6,3200.0),(3,5,1500.0)")

    engine = dashboard_view.engine_session
    engine.connection = lambda: connection
    engine.table_names = lambda: ["Transactions", "Customer"]
    engine.get_relationships = lambda: [Relationship("Transactions", "CustID", "Customer", "CustID")]
    engine.rebuild_count = lambda: 0

    dashboard_view.render_dashboard(1)


def _app() -> AppTest:
    app = AppTest.from_function(_scenario, default_timeout=120)
    app.run()
    assert not app.exception
    return app


def _spec(app: AppTest) -> m.DashboardSpec:
    return app.session_state["ld_spec"]


def _add_chart(app: AppTest, title: str = "Sales by customer", row: int = 1,
               group_by: str = "Customer - CustName") -> AppTest:
    """Adds a chart by mutating the spec, then reruns.

    The add dialog's own widgets are exercised by `test_the_add_dialog_opens`; building the
    rest of the fixtures through it would make every other test depend on the dialog's layout.
    """
    spec = _spec(app)
    panel = m.PanelSpec(
        visual_type=m.VISUAL_CHART, sub_type=m.CHART_BAR, source_table="Transactions",
        measure_column="Amount", group_by=group_by, title=title, row_number=row,
    )
    spec.panels.append(panel)
    app.run()
    assert not app.exception
    return app


# ------------------------------------------------------------------ the empty view


def test_the_view_starts_with_no_visuals_and_says_so():
    app = _app()
    assert any("No visuals yet" in info.value for info in app.info)


def test_the_main_table_is_detected_rather_than_left_blank():
    """Transactions refers to Customer, so it is the fact table."""
    app = _app()
    assert _spec(app).main_table == "Transactions"


def test_the_caption_says_what_was_joined():
    app = _app()
    captions = " ".join(caption.value for caption in app.caption)
    assert "Transactions" in captions and "Customer" in captions


def test_there_is_nothing_to_download_before_a_visual_exists():
    app = _app()
    assert not app.get("download_button")


# ------------------------------------------------------------------ the spec table


def test_a_visual_appears_in_the_spec_table():
    app = _add_chart(_app())
    assert len(app.dataframe) == 1
    frame = app.dataframe[0].value
    assert frame["Title"].tolist() == ["Sales by customer"]


def test_the_spec_table_reports_which_filters_narrow_a_visual():
    app = _add_chart(_app())
    spec = _spec(app)
    spec.panels.append(m.PanelSpec(
        visual_type=m.VISUAL_FILTER, sub_type=m.FILTER_DROPDOWN,
        source_table="Transactions", source_columns=["Customer - CustName"], title="Customer",
    ))
    app.run()

    frame = app.dataframe[0].value
    chart_row = frame[frame["Title"] == "Sales by customer"].iloc[0]
    assert chart_row["Depends on"] == "Customer"


def test_a_visual_naming_an_unreachable_column_is_listed_with_a_warning():
    """Listed, not dropped - the user has to be able to see and fix it."""
    app = _add_chart(_app(), title="By region", group_by="Region")
    frame = app.dataframe[0].value
    row = frame[frame["Title"] == "By region"].iloc[0]
    assert row["Status"].startswith("!")
    assert "Region" in row["Status"]


def test_visuals_sharing_a_row_number_show_the_number_once():
    app = _add_chart(_app(), title="Left", row=1)
    _add_chart(app, title="Right", row=1)
    frame = app.dataframe[0].value
    assert frame["Row"].tolist() == ["1", ""]


def test_filters_are_listed_above_the_visuals():
    app = _add_chart(_app())
    _spec(app).panels.append(m.PanelSpec(
        visual_type=m.VISUAL_FILTER, source_table="Transactions",
        source_columns=["Customer - CustName"], title="Customer",
    ))
    app.run()
    frame = app.dataframe[0].value
    assert frame.iloc[0]["Title"] == "Customer"


# ------------------------------------------------------------------ dialogs


def test_the_add_button_opens_the_add_dialog():
    app = _app()
    app.button(key="ld_add_panel_button").click().run()
    assert app.session_state["ld_open_dialog"][0] == "add"
    assert not app.exception


def test_the_add_dialog_offers_only_the_sub_types_that_fit_the_visual_type():
    app = _app()
    app.button(key="ld_add_panel_button").click().run()

    from analyst.charts import CHART_LABELS

    sub_type = app.selectbox(key="ld_add_sub_type")
    assert list(sub_type.options) == [CHART_LABELS[kind] for kind in m.CHART_SUB_TYPES]


def test_switching_to_a_filter_changes_the_sub_types_on_offer():
    app = _app()
    app.button(key="ld_add_panel_button").click().run()
    app.selectbox(key="ld_add_visual_type").set_value(m.VISUAL_FILTER).run()

    assert list(app.selectbox(key="ld_add_sub_type").options) == [
        m.FILTER_LABELS[kind] for kind in m.FILTER_SUB_TYPES
    ]


def test_the_data_source_picker_tags_fact_and_master_columns():
    app = _app()
    app.button(key="ld_add_panel_button").click().run()
    app.selectbox(key="ld_add_visual_type").set_value(m.VISUAL_FILTER).run()

    # AppTest reports a selectbox's options already run through its format_func, so these
    # are the labels themselves - applying it again would tag each one twice.
    labels = list(app.selectbox(key="ld_add_filter_column").options)
    assert any(label.endswith("Fact") for label in labels)
    assert any(label.endswith("Master/Lookup") for label in labels)


def test_cancelling_the_add_dialog_leaves_the_dashboard_alone():
    app = _app()
    app.button(key="ld_add_panel_button").click().run()
    app.button(key="ld_add_cancel").click().run()
    assert _spec(app).panels == []
    assert "ld_open_dialog" not in app.session_state


# ------------------------------------------------------------------ removing


def test_removing_a_visual_takes_it_off_the_dashboard():
    app = _add_chart(_app())
    panel_id = _spec(app).panels[0].panel_id

    app.session_state["ld_open_dialog"] = ("delete", {"panel_id": panel_id})
    app.run()
    app.button(key=f"ld_delete_confirm_{panel_id}").click().run()

    assert _spec(app).panels == []


def test_removing_a_visual_clears_the_table_selection():
    """Otherwise the buttons underneath silently point at whatever slid into that index."""
    app = _add_chart(_app(), title="First")
    _add_chart(app, title="Second", row=2)
    panel_id = _spec(app).panels[0].panel_id

    app.session_state["ld_spec_table"] = {"selection": {"rows": [0], "columns": []}}
    app.session_state["ld_open_dialog"] = ("delete", {"panel_id": panel_id})
    app.run()
    app.button(key=f"ld_delete_confirm_{panel_id}").click().run()

    assert app.session_state["ld_spec_table"]["selection"]["rows"] == []


# ------------------------------------------------------------------ export


def test_a_dashboard_with_a_visual_offers_a_download():
    app = _add_chart(_app())
    assert app.session_state["ld_spec"].panels
    assert {button.key for button in app.get("download_button")} == {"ld_download_html"}


def test_the_download_is_a_whole_offline_page():
    """AppTest exposes a download button's click state but not its bytes, so the page is
    rebuilt here exactly the way the view builds it."""
    app = _add_chart(_app())
    assert app.get("download_button")

    from live_dashboard import html_export

    data = app.session_state["ld_built_data"]
    rebuilt = html_export.build_dashboard_html(_spec(app), data.tables)
    assert "dashboard-payload" in rebuilt
    assert "vegaEmbed" in rebuilt


def test_the_preview_and_the_download_are_built_from_the_same_data():
    app = _add_chart(_app())
    data = app.session_state["ld_built_data"]
    assert list(data.tables) == ["Transactions"]
    assert len(data.tables["Transactions"]) == 3
    assert "Customer - CustName" in data.tables["Transactions"].columns


# ------------------------------------------------------------------ settings


def test_the_theme_and_filter_position_reach_the_spec():
    app = _add_chart(_app())
    app.segmented_control(key="ld_theme").set_value(m.THEME_DARK).run()
    app.segmented_control(key="ld_filter_position").set_value(m.FILTER_LEFT).run()

    spec = _spec(app)
    assert spec.theme == m.THEME_DARK
    assert spec.filter_position == m.FILTER_LEFT


def test_the_title_reaches_the_spec():
    app = _app()
    app.text_input(key="ld_title").set_value("Monthly sales").run()
    assert _spec(app).title == "Monthly sales"
