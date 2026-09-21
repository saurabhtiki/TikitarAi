"""AppTest coverage for the Dashboard view (phase 32).

Driven with `AppTest.from_function` rather than through the page file, for the reason
`test_report_items_page.py` gives: the view is a fragment and a real session is all it needs.

One seam is stubbed - the engine's session, which would otherwise need a real upload. The
DuckDB connection behind it is genuine, so the flattening, the spec building and the whole
export all run for real; only the "where did these tables come from" question is answered by
the test.

Since phase 36 the conversation is the whole page: the spec table, the edit form and the
Add / Duplicate / Remove buttons are gone, so every change to a dashboard is a round. The
behaviours worth the most here are the ones that stay invisible until someone has done real
work: **a visual that cannot be drawn named in words** (with no table, nothing else says
so), **a round changing only what it names**, and **the download and the preview coming
from one string**.

`ai_spec.revise_dashboard` is stubbed with a small script read from session state, because
what these tests are about is what the *page* does with a round - how a round is built is
`test_live_dashboard_ai_spec.py`'s job. With no script set the stub calls the real thing,
which is how the "a blank box with visuals on the page asks for words" case is tested
without a provider.
"""

import pytest
from streamlit.testing.v1 import AppTest

from live_dashboard import model as m

#: What `_scenario` replaces on the shared `engine.session` module.
STUBBED = ("connection", "table_names", "get_relationships", "rebuild_count")

#: The Light Model the Describe dialog is told it has. Stubbed rather than read from the
#: LLM database, so these tests never depend on whether a provider happens to be configured.
LIGHT = {
    "profile_id": 1,
    "nickname": "Light",
    "default_model": "small-model",
    "provider_type": "local",
}


@pytest.fixture(autouse=True)
def leave_the_engine_session_as_it_was_found():
    """Puts the real `engine.session` and `llm.session` functions back after each test.

    `_scenario` stubs them by assigning onto the modules, which every other test in the run
    shares - so without this, a later test asking the engine for real tables would get this
    file's two-table fixture instead and fail somewhere far away from the cause.
    """
    from engine import session as engine_session
    from llm import session as llm_session

    from live_dashboard import ai_spec

    saved = {name: getattr(engine_session, name) for name in STUBBED}
    saved_light = llm_session.light_profile
    saved_revise = ai_spec.revise_dashboard
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(engine_session, name, value)
        llm_session.light_profile = saved_light
        ai_spec.revise_dashboard = saved_revise


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

    dashboard_view.llm_session.light_profile = lambda user_id: {
        "profile_id": 1,
        "nickname": "Light",
        "default_model": "small-model",
        "provider_type": "local",
    }

    # One round, replaced by whatever `test_round_plan` says it did. Inline rather than a
    # helper for the reason this function's docstring gives - only its own source is
    # re-executed - and installed once, so a rerun does not wrap the stub in itself.
    #
    # With no plan in session state the real `revise_dashboard` runs. That is not laziness:
    # the "blank box with visuals already there" refusal happens before any model is
    # reached, so the honest way to test it is to let the real function answer.
    import streamlit as st

    from live_dashboard import ai_spec
    from live_dashboard import model as ld_model

    if not getattr(ai_spec.revise_dashboard, "is_test_stub", False):
        real_revise = ai_spec.revise_dashboard

        def scripted(profile, instruction, tables, spec, **kwargs):
            if "test_round_plan" not in st.session_state:
                return real_revise(profile, instruction, tables, spec, **kwargs)

            plan = st.session_state["test_round_plan"] or {}
            result = ai_spec.RoundResult(notes=list(plan.get("notes") or []))
            if plan.get("fail"):
                result.failed = True
                return result

            for index in sorted(plan.get("remove") or [], reverse=True):
                result.removed.append(spec.panels.pop(index).display_title())
            for index, title in (plan.get("retitle") or {}).items():
                spec.panels[index].title = title
                result.updated.append(spec.panels[index])
            for title in plan.get("add") or []:
                panel = ld_model.PanelSpec(
                    visual_type=ld_model.VISUAL_CHART, sub_type=ld_model.CHART_BAR,
                    source_table="Transactions", measure_column="Amount",
                    group_by="Customer - CustName", title=title, row_number=1,
                )
                spec.panels.append(panel)
                result.added.append(panel)
            return result

        scripted.is_test_stub = True
        ai_spec.revise_dashboard = scripted

    dashboard_view.render_dashboard(1)


def _scenario_without_a_light_model():
    """The same view with no Light Model configured - what most users see on day one."""
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

    dashboard_view.llm_session.light_profile = lambda user_id: None

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

    Since phase 35 there is no Add a visual button to press, and building fixtures through a
    round would make every test depend on the stub's script as well as on the page.
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


# ------------------------------------------------------------------ what is on the page


def test_there_is_no_spec_table_and_no_buttons_to_edit_by_hand():
    """Phase 36's change in shape, in one test. Two ways to change one dashboard meant two
    things to learn, and the table described in eight columns what the preview shows in full
    a few inches below it."""
    app = _add_chart(_app())

    assert not app.dataframe
    keys = {button.key for button in app.button}
    assert not keys & {"ld_add_panel_button", "ld_edit_button",
                       "ld_duplicate_button", "ld_delete_button"}
    assert "ld_ai_generate" in keys


def test_a_visual_that_cannot_be_drawn_is_named_above_the_preview():
    """The one thing a picture of a dashboard cannot show is the visual missing from it.
    This is what the removed table's `Status` column used to say."""
    app = _add_chart(_app(), title="By region", group_by="Region")

    warnings = " ".join(warning.value for warning in app.warning)
    assert "By region" in warnings
    assert "Region" in warnings


def test_a_dashboard_whose_visuals_are_all_fine_says_nothing():
    """A standing warning nobody can clear is a warning nobody reads."""
    app = _add_chart(_app())
    assert not any("can't be drawn" in warning.value for warning in app.warning)


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


# ------------------------------------------------------------------ the conversation


def _round(app: AppTest, instruction: str = "add a chart", **plan) -> AppTest:
    """Types an instruction, scripts what the round did, and presses the button."""
    app.session_state["test_round_plan"] = plan
    app.text_area(key="ld_ai_instruction").set_value(instruction).run()
    app.button(key="ld_ai_generate").click().run()
    assert not app.exception
    return app


def test_a_round_changes_the_dashboard_in_place_with_no_accept_step():
    """The whole of phase 35's change in shape: no Replace, no Add to it, no dialog."""
    app = _add_chart(_app(), title="Built by hand")
    _round(app, "add a second chart", add=["Asked for"])

    assert [panel.title for panel in _spec(app).panels] == ["Built by hand", "Asked for"]
    assert "ld_open_dialog" not in app.session_state


def test_a_round_can_take_a_visual_off_the_dashboard():
    """The Remove button's replacement: it is said, not clicked."""
    app = _add_chart(_app(), title="Keep me")
    _add_chart(app, title="Drop me", row=2)

    _round(app, "drop the second chart", remove=[1])

    assert [panel.title for panel in _spec(app).panels] == ["Keep me"]


def test_a_round_that_changes_one_visual_leaves_the_others_alone():
    app = _add_chart(_app(), title="First")
    _add_chart(app, title="Second", row=2)
    untouched = _spec(app).panels[1].panel_id

    _round(app, "rename the first one", retitle={0: "Renamed"})

    spec = _spec(app)
    assert [panel.title for panel in spec.panels] == ["Renamed", "Second"]
    assert spec.panels[1].panel_id == untouched


def test_what_each_round_did_is_listed_back():
    app = _add_chart(_app())
    _round(app, "add a chart of sales by customer", add=["Sales by customer"])

    written = " ".join(element.value for element in app.markdown)
    assert "add a chart of sales by customer" in written
    assert any("Added 1 visual(s)" in caption.value for caption in app.caption)


def test_a_note_from_a_round_is_shown_as_a_warning():
    """A shape swapped for the nearest one we can draw must not be discovered in someone
    else's inbox three days later."""
    app = _add_chart(_app())
    _round(app, "show products as a treemap", add=["Products"],
           notes=["'Products' asked for a treemap. This dashboard can't draw one yet."])

    assert any("treemap" in warning.value for warning in app.warning)


def test_undo_puts_the_dashboard_back_as_it_was():
    app = _add_chart(_app(), title="Built by hand")
    _round(app, "add one", add=["Generated"])
    assert len(_spec(app).panels) == 2

    app.button(key="ld_ai_undo").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]


def test_undo_is_offered_only_once_there_is_a_round_to_undo():
    app = _add_chart(_app())
    assert app.button(key="ld_ai_undo").disabled

    _round(app, "add one", add=["Generated"])
    assert not app.button(key="ld_ai_undo").disabled


def test_a_round_that_changed_nothing_does_not_light_up_undo():
    """A button that restores the page to exactly what it already is teaches the user to
    distrust the next one."""
    app = _add_chart(_app())
    _round(app, "do something impossible", notes=["Nothing on the dashboard changed."])

    assert app.button(key="ld_ai_undo").disabled
    assert len(_spec(app).panels) == 1


def test_a_blank_box_with_visuals_already_there_asks_for_words():
    """Run against the real `revise_dashboard`: the refusal happens before any model is
    reached, which is the point - "add something" and "start over" are not one request."""
    app = _add_chart(_app(), title="Built by hand")
    app.text_area(key="ld_ai_instruction").set_value("").run()
    app.button(key="ld_ai_generate").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]
    assert any("Say what you'd like changed" in warning.value for warning in app.warning)


def test_with_no_light_model_the_page_says_so_and_a_saved_dashboard_still_downloads():
    """Since phase 36 there is nothing else to do here without a model - so the page has to
    say which setting is missing, and a dashboard saved earlier must still come out."""
    app = AppTest.from_function(_scenario_without_a_light_model, default_timeout=120)
    app.run()
    assert not app.exception

    app.session_state["ld_spec"].panels.append(m.PanelSpec(
        visual_type=m.VISUAL_CHART, sub_type=m.CHART_BAR, source_table="Transactions",
        measure_column="Amount", group_by="Customer - CustName", title="Saved earlier",
    ))
    app.run()

    assert any("No Light Model" in warning.value for warning in app.warning)
    assert "ld_ai_generate" not in {button.key for button in app.button}
    assert app.get("download_button")
