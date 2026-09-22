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

Phase 39 adds a second button with a second model: **Generate Dashboard** designs a page
from nothing using the session's own profile, while Update the dashboard and Edit stay on
the Light Model. Both are stubbed here, and which profile reached `revise_dashboard` is
recorded, because "the good model designs and the light model edits" is a promise about
cost that nothing else on the page would reveal if it broke.

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
    saved_active = llm_session.active_profile
    saved_revise = ai_spec.revise_dashboard
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(engine_session, name, value)
        llm_session.light_profile = saved_light
        llm_session.active_profile = saved_active
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
    dashboard_view.llm_session.active_profile = lambda user_id: {
        "profile_id": 2,
        "nickname": "Good",
        "default_model": "big-model",
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
            # Recorded rather than asserted here: the page's job is to say *which* visual an
            # Edit button meant, and confining the round to it is `ai_spec`'s.
            st.session_state["test_round_focus"] = kwargs.get("focus")
            st.session_state["test_round_profile"] = profile
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
    dashboard_view.llm_session.active_profile = lambda user_id: None

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


def test_there_is_no_main_table_to_pick():
    """Phase 39: every table is embedded with its own parents joined on, so there is no
    longer one table the dashboard is built from - and nothing to choose."""
    app = _app()
    assert "ld_main_table" not in {widget.key for widget in app.selectbox}


def test_the_caption_says_what_was_joined():
    app = _app()
    captions = " ".join(caption.value for caption in app.caption)
    assert "Transactions" in captions and "Customer" in captions


def test_there_is_nothing_to_download_before_a_visual_exists():
    app = _app()
    assert not app.get("download_button")


# ------------------------------------------------------------------ what is on the page


def test_there_is_no_spec_table_and_no_form_to_edit_a_visual_by_hand():
    """Phase 36's change in shape, in one test. Two ways to change one dashboard meant two
    things to learn, and the table described in eight columns what the preview shows in full
    a few inches below it. Phase 37's per-visual Edit button is not a second way back: it
    opens the same round, scoped to one visual, and fills in no fields itself."""
    app = _add_chart(_app())

    assert not app.dataframe
    keys = {button.key for button in app.button}
    assert not keys & {"ld_add_panel_button", "ld_edit_button",
                       "ld_duplicate_button", "ld_delete_button"}
    assert "ld_ai_open" in keys


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


def test_every_table_is_embedded_each_carrying_its_parents_columns():
    """The phase 39 fix, on the page: Customer used to be folded into Transactions and
    embedded nowhere of its own, so a filter on a Customer column could not narrow a visual
    that read Customer directly. Both tables now carry `Customer - CustName`."""
    app = _add_chart(_app())
    data = app.session_state["ld_built_data"]

    assert sorted(data.tables) == ["Customer", "Transactions"]
    assert len(data.tables["Transactions"]) == 3
    assert len(data.tables["Customer"]) == 2  # never multiplied by the join
    assert "Customer - CustName" in data.tables["Transactions"].columns
    assert "Customer - CustName" in data.tables["Customer"].columns


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
    """Opens the dialog, types an instruction, scripts what the round did, and runs it.

    The box moved into a dialog in phase 37, so every round now starts with the press that
    opens it - which is the one thing a test of this page must not skip, since a dialog that
    never opens has no box to type in.
    """
    app.session_state["test_round_plan"] = plan
    app.button(key="ld_ai_open").click().run()
    app.text_area(key="ld_ai_instruction").set_value(instruction).run()
    app.button(key="ld_ai_generate").click().run()
    assert not app.exception
    return app


def test_a_round_changes_the_dashboard_in_place_with_no_accept_step():
    """Phase 35's change in shape, which phase 37's dialog did not undo: the round lands on
    the page straight away - no Replace, no Add to it, nothing to accept - and the dialog
    that asked for it closes behind it."""
    app = _add_chart(_app(), title="Built by hand")
    _round(app, "add a second chart", add=["Asked for"])

    assert [panel.title for panel in _spec(app).panels] == ["Built by hand", "Asked for"]
    assert "ld_dialog" not in app.session_state


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

    app.button(key="ld_undo_round_0").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]


def test_undo_is_offered_only_once_there_is_a_round_to_undo():
    """There is no page-level Undo button at all now - a button with nothing to undo said
    nothing about which round it meant."""
    app = _add_chart(_app())
    keys = {button.key for button in app.button}
    assert "ld_ai_undo" not in keys
    assert not any(key.startswith("ld_undo_round_") for key in keys)

    _round(app, "add one", add=["Generated"])
    assert not app.button(key="ld_undo_round_0").disabled


def test_each_round_has_its_own_undo_and_only_the_newest_is_live():
    """Phase 39's change: one stash meant the second round of an afternoon threw away the
    only copy that could take you back. Older rounds are shown disabled rather than hidden,
    so the history says plainly that undoing goes newest first."""
    app = _add_chart(_app(), title="Built by hand")
    _round(app, "add one", add=["First round"])
    _round(app, "add another", add=["Second round"])

    assert app.button(key="ld_undo_round_0").disabled is False
    assert app.button(key="ld_undo_round_1").disabled is True

    app.button(key="ld_undo_round_0").click().run()

    assert [panel.title for panel in _spec(app).panels] == ["Built by hand", "First round"]
    # The round it undid is gone, so what was the second-newest is now the live one.
    assert app.button(key="ld_undo_round_0").disabled is False
    assert "ld_undo_round_1" not in {button.key for button in app.button}


def test_undo_drops_the_undone_round_from_the_history_and_says_so():
    """The bug: Undo restored the dashboard but left "What was asked, and what changed"
    still describing the round that was just reverted - the history and the page disagreeing
    about what is actually on screen."""
    app = _add_chart(_app(), title="Built by hand")
    _round(app, "add one", add=["Generated"])
    assert "add one" in " ".join(element.value for element in app.markdown)

    app.button(key="ld_undo_round_0").click().run()

    assert not any("add one" in element.value for element in app.markdown)
    assert any("Undone" in success.value for success in app.success)


def test_a_round_that_changed_nothing_does_not_light_up_undo():
    """A button that restores the page to exactly what it already is teaches the user to
    distrust the next one."""
    app = _add_chart(_app())
    _round(app, "do something impossible", notes=["Nothing on the dashboard changed."])

    assert not any(button.key.startswith("ld_undo_round_") for button in app.button)
    assert len(_spec(app).panels) == 1


def test_a_blank_box_with_visuals_already_there_asks_for_words():
    """Run against the real `revise_dashboard`: the refusal happens before any model is
    reached, which is the point - "add something" and "start over" are not one request.

    The dialog is left open with the refusal inside it, because a sentence explaining what
    to type belongs beside the box it is to be typed in."""
    app = _add_chart(_app(), title="Built by hand")
    app.button(key="ld_ai_open").click().run()
    app.text_area(key="ld_ai_instruction").set_value("").run()
    app.button(key="ld_ai_generate").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]
    assert any("Say what you'd like changed" in warning.value for warning in app.warning)
    assert app.session_state["ld_dialog"] == "ask"


# ------------------------------------------------------------------ phase 39: Generate


def test_generate_is_the_only_button_on_an_empty_page():
    """Update the dashboard has nothing to update yet, and "Build the dashboard" was the
    same button under a second name."""
    app = _app()
    keys = {button.key for button in app.button}
    assert "ld_generate" in keys
    assert "ld_ai_open" not in keys


def test_generate_designs_a_dashboard_with_the_session_model_not_the_light_one():
    """The one judgement call on this page - laying out a page from nothing - is made once,
    so it is worth the better model. Every round after it stays Light."""
    app = _app()
    app.session_state["test_round_plan"] = {"add": ["Sales by customer"]}
    app.button(key="ld_generate").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["Sales by customer"]
    assert app.session_state["test_round_profile"]["nickname"] == "Good"

    _round(app, "rename it", retitle={0: "Renamed"})
    assert app.session_state["test_round_profile"]["nickname"] == "Light"


def test_generate_over_an_existing_dashboard_asks_before_replacing_it():
    """It replaces the page, edits and all, so one stray click must not be enough."""
    app = _add_chart(_app(), title="Built by hand")
    app.session_state["test_round_plan"] = {"add": ["Designed"]}

    app.button(key="ld_generate").click().run()
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]
    assert "test_round_profile" not in app.session_state

    app.button(key="ld_generate_yes").click().run()
    assert [panel.title for panel in _spec(app).panels] == ["Designed"]


def test_saying_no_to_generate_keeps_the_dashboard():
    app = _add_chart(_app(), title="Built by hand")
    app.button(key="ld_generate").click().run()
    app.button(key="ld_generate_no").click().run()

    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]
    assert app.button(key="ld_generate") is not None


def test_undo_after_generate_brings_the_old_dashboard_back():
    app = _add_chart(_app(), title="Built by hand")
    app.session_state["test_round_plan"] = {"add": ["Designed"]}
    app.button(key="ld_generate").click().run()
    app.button(key="ld_generate_yes").click().run()

    app.button(key="ld_undo_round_0").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]


def test_a_generate_that_produced_nothing_leaves_the_dashboard_alone():
    """The panels are emptied on the way in, so a round that fails has to put them back -
    otherwise a provider outage silently clears the page."""
    app = _add_chart(_app(), title="Built by hand")
    app.session_state["test_round_plan"] = {"fail": True}

    app.button(key="ld_generate").click().run()
    app.button(key="ld_generate_yes").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]


# ------------------------------------------------------------------ phase 37: the dialogs


def test_the_instruction_box_is_behind_the_button_rather_than_on_the_page():
    """An always-open text area pushed the dashboard it describes below the fold on every
    visit. One sentence is written once, not lived in."""
    app = _add_chart(_app())
    assert not app.text_area

    app.button(key="ld_ai_open").click().run()
    assert app.text_area(key="ld_ai_instruction") is not None


def test_cancelling_the_dialog_changes_nothing():
    app = _add_chart(_app(), title="Built by hand")
    app.button(key="ld_ai_open").click().run()
    app.button(key="ld_ai_cancel").click().run()

    assert not app.exception
    assert "ld_dialog" not in app.session_state
    assert [panel.title for panel in _spec(app).panels] == ["Built by hand"]


def test_every_visual_has_its_own_edit_button():
    app = _add_chart(_app(), title="First")
    _add_chart(app, title="Second", row=2)

    keys = {button.key for button in app.button}
    assert {f"ld_edit_{panel.panel_id}" for panel in _spec(app).panels} <= keys


def test_editing_one_visual_asks_only_about_that_visual():
    """The Edit button is the same round with "which one do you mean" already answered - so
    what reaches `revise_dashboard` is that panel, not a title the user had to copy out."""
    app = _add_chart(_app(), title="First")
    _add_chart(app, title="Second", row=2)
    second = _spec(app).panels[1]

    app.button(key=f"ld_edit_{second.panel_id}").click().run()
    assert app.session_state["ld_dialog"] == second.panel_id

    app.session_state["test_round_plan"] = {"retitle": {1: "Renamed by its own button"}}
    app.text_area(key=f"ld_panel_instruction_{second.panel_id}").set_value("rename it").run()
    app.button(key=f"ld_panel_generate_{second.panel_id}").click().run()

    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["First", "Renamed by its own button"]
    assert app.session_state["test_round_focus"] is second


def test_remove_this_visual_asks_first_and_then_removes_it_and_undo_brings_it_back():
    """Two presses, so one stray click can't delete a visual - and no model is asked."""
    app = _add_chart(_app(), title="First")
    _add_chart(app, title="Second", row=2)
    second = _spec(app).panels[1]

    app.button(key=f"ld_edit_{second.panel_id}").click().run()
    app.button(key=f"ld_panel_remove_{second.panel_id}").click().run()
    assert [panel.title for panel in _spec(app).panels] == ["First", "Second"]
    assert app.button(key=f"ld_panel_remove_yes_{second.panel_id}") is not None

    app.button(key=f"ld_panel_remove_yes_{second.panel_id}").click().run()
    assert not app.exception
    assert [panel.title for panel in _spec(app).panels] == ["First"]
    assert "ld_dialog" not in app.session_state
    assert not app.button(key="ld_undo_round_0").disabled

    app.button(key="ld_undo_round_0").click().run()
    assert [panel.title for panel in _spec(app).panels] == ["First", "Second"]


def test_saying_no_to_remove_keeps_the_visual():
    app = _add_chart(_app(), title="Keep me")
    only = _spec(app).panels[0]

    app.button(key=f"ld_edit_{only.panel_id}").click().run()
    app.button(key=f"ld_panel_remove_{only.panel_id}").click().run()
    app.button(key=f"ld_panel_remove_no_{only.panel_id}").click().run()

    assert [panel.title for panel in _spec(app).panels] == ["Keep me"]
    assert app.button(key=f"ld_panel_remove_{only.panel_id}") is not None


def test_both_dialogs_offer_what_can_i_ask():
    """The box is driven by typing a sentence, so the edge of what it understands has to be
    readable from inside both places a sentence is typed."""
    app = _add_chart(_app(), title="First")
    only = _spec(app).panels[0]

    # Asserted on the help's own text rather than on the expander around it: `AppTest` does
    # not expose expanders, and the words are what the user actually reads.
    app.button(key="ld_ai_open").click().run()
    assert any("You can ask for" in one.value for one in app.markdown)

    app.button(key="ld_ai_cancel").click().run()
    app.button(key=f"ld_edit_{only.panel_id}").click().run()
    assert any("You can ask for" in one.value for one in app.markdown)


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

    warnings = " ".join(warning.value for warning in app.warning)
    assert "No Light Model" in warnings
    assert "No model is configured" in warnings
    keys = {button.key for button in app.button}
    assert "ld_ai_generate" not in keys and "ld_generate" not in keys
    assert app.get("download_button")


def test_reopening_a_visual_after_cancelling_lands_on_remove_and_not_on_the_confirm():
    """The two-press guard only guards if the first press does not linger: a flag left set
    after the dialog was dismissed would put "Yes, remove it" under the user's cursor."""
    app = _add_chart(_app(), title="Keep me")
    only = _spec(app).panels[0]

    app.button(key=f"ld_edit_{only.panel_id}").click().run()
    app.button(key=f"ld_panel_remove_{only.panel_id}").click().run()
    app.button(key=f"ld_panel_cancel_{only.panel_id}").click().run()

    app.button(key=f"ld_edit_{only.panel_id}").click().run()

    assert app.button(key=f"ld_panel_remove_{only.panel_id}") is not None
    assert f"ld_panel_remove_yes_{only.panel_id}" not in {one.key for one in app.button}
    assert [panel.title for panel in _spec(app).panels] == ["Keep me"]


def test_a_broken_filter_is_listed_once_and_not_twice():
    """`filters()` is a subset of `panels`, so walking both counted each broken filter twice."""
    app = _add_chart(_app(), title="Fine")
    app.session_state["ld_spec"].panels.append(m.PanelSpec(
        visual_type=m.VISUAL_FILTER, sub_type=m.FILTER_DROPDOWN, source_table="Transactions",
        source_columns=["No such column"], title="Broken filter",
    ))
    app.run()

    mentions = [one for one in [*app.warning, *app.markdown, *app.caption]
                if "Broken filter" in str(one.value)]
    assert sum(str(one.value).count("Broken filter") for one in mentions) == 1
