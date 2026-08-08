"""AppTest coverage for the Chat with Data page.

Uploads really are drivable through `AppTest` in the installed Streamlit (the Data
Cleaner's suite established that), so these tests exercise the page through actual
uploads rather than by injecting state. Dialogs are driven through the session-state
open flag, which is why the page uses that idiom rather than `if st.button(...)`.
"""

import io
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from agno.db.sqlite import SqliteDb
from streamlit.testing.v1 import AppTest

from analyst import charts, pipeline
from analyst import session as chat_session
from analyst.exceptions import ChatStorageError
from analyst.pipeline import Answer
from auth.db import init_db, seed_default_admin
from chat_types.db import init_chat_types_table
from cleaner import session as cleaner_session
from dashboard import session as dashboard_session
from engine import columns as engine_columns
from engine import session as engine_session
from engine.relationships import Relationship
from llm.db import create_profile, init_llm_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "chat_with_data.py")

SALES_CSV = b"cust_id,sku,basic\nc1,s1,100\nc2,s2,50\nc1,s1,250\n"
# Three rows that match plus one that doesn't. One orphan out of two rows would be 50%
# containment, below the suggestion threshold, so the link would never be proposed at
# all — and "proposed, then flagged" is the case this screen exists for.
SALES_WITH_ORPHAN_CSV = b"cust_id,sku,basic\nc1,s1,100\nc2,s2,50\nc3,s1,20\nc9,s1,75\n"
CUSTOMER_CSV = b"id,name\nc1,Ana\nc2,Bo\nc3,Cy\n"
STOCK_CSV = b"sku,name\ns1,Widget\ns2,Gadget\n"
STAFF_CSV = b"emp_id,dept\n007,Sales\n008,Ops\n"


def _make_app(tmp_path, monkeypatch, role="normal_user"):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    init_chat_types_table()
    app = AppTest.from_file(PAGE_PATH, default_timeout=60)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = role
    app.run()
    return app


def _open_step(app, step_key):
    """Expands a step, since a collapsed one deliberately doesn't run its body."""
    app.session_state[step_key] = True
    app.run()
    return app


def _upload(app, *files: tuple[str, bytes]):
    """Uploads through the real widget.

    Step 1 is re-opened first because it auto-collapses once data is loaded, taking the
    uploader with it — so changing the file set is exactly this two-step gesture for a
    real user too.
    """
    _open_step(app, engine_session.STEP_UPLOAD)
    app.file_uploader(key=engine_session.DE_UPLOADER_KEY).set_value(
        [(name, payload, "text/csv") for name, payload in files]
    )
    app.run()
    return app


class TestAccess:
    @pytest.mark.parametrize("role", ["normal_user", "admin", "superuser"])
    def test_every_role_reaches_the_page(self, tmp_path, monkeypatch, role):
        """Requirement 2.2 grants Chat with Data to all three roles."""
        app = _make_app(tmp_path, monkeypatch, role=role)
        assert not app.exception
        assert any("Chat with Data" in heading.value for heading in app.subheader)

    def test_it_starts_by_asking_for_a_file(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert any("Upload a CSV or Excel file" in info.value for info in app.info)


class TestUpload:
    def test_an_upload_becomes_a_queryable_table(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert not app.exception
        assert engine_session.DE_TABLES_KEY in app.session_state
        tables = app.session_state[engine_session.DE_TABLES_KEY]
        assert [table.table_name for table in tables.values()] == ["sales"]

    def test_several_files_become_several_tables(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customer.csv", CUSTOMER_CSV),
            ("stock.csv", STOCK_CSV),
        )
        names = {table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
        assert names == {"sales", "customer", "stock"}

    def test_removing_a_file_drops_its_table(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("customer.csv", CUSTOMER_CSV)
        )
        _upload(app, ("sales.csv", SALES_CSV))
        names = {table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
        assert names == {"sales"}

    def test_leading_zeros_survive_the_upload(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("staff.csv", STAFF_CSV))
        table = next(iter(app.session_state[engine_session.DE_TABLES_KEY].values()))
        assert table.semantic_types["emp_id"] == "id"

    def test_an_unreadable_file_errors_without_taking_the_page_down(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.file_uploader(key=engine_session.DE_UPLOADER_KEY).set_value(
            [("broken.xlsx", b"not a workbook", "application/octet-stream")]
        )
        app.run()
        assert not app.exception
        assert app.error


class TestSteps:
    def test_the_links_step_is_hidden_for_a_single_table(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert not any("link" in expander.label.lower() for expander in app.status)

    def test_the_links_step_appears_for_two_tables(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("customer.csv", CUSTOMER_CSV)
        )
        assert any("link" in expander.label.lower() for expander in app.status)

    def test_step_one_collapses_once_data_is_loaded(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        step = next(item for item in app.status if item.label.startswith("✅ Step 1"))
        assert "sales" in step.label

    def test_a_collapsed_step_does_not_run_its_body(self, tmp_path, monkeypatch):
        """The `.open` gate — the reason a collapsed dictionary doesn't re-query."""
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[engine_session.STEP_DICTIONARY] = False
        app.run()
        assert not [button for button in app.button if button.key == "de_suggest_button"]

        _open_step(app, engine_session.STEP_DICTIONARY)
        assert [button for button in app.button if button.key == "de_suggest_button"]

    def test_a_queued_step_change_is_applied_once_and_then_forgotten(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[engine_session.DE_PENDING_STEPS_KEY] = {engine_session.STEP_DICTIONARY: True}
        app.run()

        assert app.session_state[engine_session.STEP_DICTIONARY] is True
        assert engine_session.DE_PENDING_STEPS_KEY not in app.session_state


class TestRelationships:
    def _loaded(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_CSV),
            ("customer.csv", CUSTOMER_CSV),
            ("stock.csv", STOCK_CSV),
        )
        return _open_step(app, engine_session.STEP_LINKS)

    def test_links_are_suggested(self, tmp_path, monkeypatch):
        app = self._loaded(tmp_path, monkeypatch)
        candidates = app.session_state[engine_session.DE_CANDIDATES_KEY]
        assert {candidate.relationship.child_column for candidate in candidates} >= {"cust_id", "sku"}

    def test_accepting_a_link_records_it(self, tmp_path, monkeypatch):
        app = self._loaded(tmp_path, monkeypatch)
        accept = next(button for button in app.button if button.key.startswith("de_accept_"))
        accept.click().run()
        assert app.session_state[engine_session.DE_RELATIONSHIPS_KEY]

    def test_confirming_enforces_a_real_constraint(self, tmp_path, monkeypatch):
        import duckdb

        app = self._loaded(tmp_path, monkeypatch)
        app.session_state[engine_session.DE_RELATIONSHIPS_KEY] = [
            Relationship("sales", "sku", "stock", "sku")
        ]
        app.run()
        next(button for button in app.button if button.key == "de_confirm_links_button").click().run()

        connection = app.session_state[engine_session.DE_CONNECTION_KEY]
        with pytest.raises(duckdb.Error):
            connection.execute("INSERT INTO sales VALUES ('c1', 'NOT_A_SKU', 1.0)")

    def test_confirming_collapses_step_two_and_opens_step_three(self, tmp_path, monkeypatch):
        app = self._loaded(tmp_path, monkeypatch)
        app.session_state[engine_session.DE_RELATIONSHIPS_KEY] = [
            Relationship("sales", "sku", "stock", "sku")
        ]
        app.run()
        next(button for button in app.button if button.key == "de_confirm_links_button").click().run()

        assert app.session_state[engine_session.STEP_LINKS] is False
        assert app.session_state[engine_session.STEP_DICTIONARY] is True

    def test_a_link_with_orphans_can_still_be_accepted(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_WITH_ORPHAN_CSV),
            ("customer.csv", CUSTOMER_CSV),
        )
        _open_step(app, engine_session.STEP_LINKS)

        # Offered for inspection *and* acceptance — a mismatch is shown, not blocked.
        assert [button for button in app.button if button.key.startswith("de_inspect_")]
        accept = next(button for button in app.button if button.key.startswith("de_accept_"))
        accept.click().run()
        assert app.session_state[engine_session.DE_RELATIONSHIPS_KEY]

    def test_inspecting_orphans_shows_the_rows_in_full(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_WITH_ORPHAN_CSV),
            ("customer.csv", CUSTOMER_CSV),
        )
        _open_step(app, engine_session.STEP_LINKS)
        next(button for button in app.button if button.key.startswith("de_inspect_")).click().run()

        assert not app.exception
        shown = pd.concat([frame.value for frame in app.dataframe]) if app.dataframe else pd.DataFrame()
        assert "c9" in shown.to_string()
        assert "basic" in list(shown.columns)

    def test_confirming_a_bad_link_keeps_it_declared_but_unenforced(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("sales.csv", SALES_WITH_ORPHAN_CSV),
            ("customer.csv", CUSTOMER_CSV),
        )
        _open_step(app, engine_session.STEP_LINKS)
        app.session_state[engine_session.DE_RELATIONSHIPS_KEY] = [
            Relationship("sales", "cust_id", "customer", "id")
        ]
        app.run()
        next(button for button in app.button if button.key == "de_confirm_links_button").click().run()

        assert not app.error
        assert app.info
        # Still confirmed for querying...
        assert app.session_state[engine_session.DE_RELATIONSHIPS_KEY] == [
            Relationship("sales", "cust_id", "customer", "id")
        ]
        # ...but not enforced as a real constraint — an orphaned insert is not rejected.
        connection = app.session_state[engine_session.DE_CONNECTION_KEY]
        connection.execute("INSERT INTO sales VALUES ('nope', 's1', 1.0)")


class TestCleanerHandoff:
    def test_no_button_when_the_cleaner_is_empty(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert not [button for button in app.button if button.key == "de_adopt_cleaner_button"]

    def _with_cleaner_tables(self, tmp_path, monkeypatch):
        """Loads files through the Data Cleaner page, then opens Chat with Data on the
        same session state — which is exactly what a real user does."""
        monkeypatch.chdir(tmp_path)
        init_db()
        seed_default_admin()
        init_llm_table()

        cleaner_app = AppTest.from_file(str(PROJECT_ROOT / "app_pages" / "data_cleaner.py"), default_timeout=60)
        cleaner_app.session_state["user_id"] = 1
        cleaner_app.session_state["email"] = "admin@admin.com"
        cleaner_app.session_state["role"] = "normal_user"
        cleaner_app.run()
        cleaner_app.file_uploader(key=cleaner_session.DC_UPLOADER_KEY).set_value(
            [("sales.csv", SALES_CSV, "text/csv")]
        )
        cleaner_app.run()
        return cleaner_app

    def test_cleaned_tables_are_offered_and_adopted(self, tmp_path, monkeypatch):
        cleaner_app = self._with_cleaner_tables(tmp_path, monkeypatch)
        assert cleaner_app.session_state[cleaner_session.DC_TABLES_KEY]

        app = AppTest.from_file(PAGE_PATH, default_timeout=60)
        for key, value in cleaner_app.session_state.filtered_state.items():
            app.session_state[key] = value
        app.run()

        adopt = next(button for button in app.button if button.key == "de_adopt_cleaner_button")
        adopt.click().run()

        assert not app.exception
        tables = app.session_state[engine_session.DE_TABLES_KEY]
        assert tables
        assert all(table.from_cleaner for table in tables.values())

    def test_adoption_survives_the_uploader_widget_state_being_gone(self, tmp_path, monkeypatch):
        """Regression test for the reported crash: `st.file_uploader`'s own widget state
        is ephemeral and, on a real page switch, is not guaranteed to still hold the
        uploaded bytes on a page other than the one that rendered it — unlike
        `AppTest`'s session-state copy above, which doesn't reproduce that loss. Adoption
        must work from `cleaner_session.cached_file_bytes()` alone, so this drops
        `DC_UPLOADER_KEY` before adopting to prove it isn't what's being read."""
        cleaner_app = self._with_cleaner_tables(tmp_path, monkeypatch)

        app = AppTest.from_file(PAGE_PATH, default_timeout=60)
        for key, value in cleaner_app.session_state.filtered_state.items():
            app.session_state[key] = value
        if cleaner_session.DC_UPLOADER_KEY in app.session_state:
            del app.session_state[cleaner_session.DC_UPLOADER_KEY]
        app.run()

        adopt = next(button for button in app.button if button.key == "de_adopt_cleaner_button")
        adopt.click().run()

        assert not app.exception
        tables = app.session_state[engine_session.DE_TABLES_KEY]
        assert tables
        assert all(table.from_cleaner for table in tables.values())

    def test_a_saved_summary_adopts_as_its_own_table(self, tmp_path, monkeypatch):
        """A summary is an ordinary cleaner table with a `derived_from`, which is what
        lets it reach the engine without any handoff code of its own. Its recipe lives on
        its parent, so this also pins that `effective_steps` — not `.steps` — is what
        adoption resolves."""
        cleaner_app = self._with_cleaner_tables(tmp_path, monkeypatch)
        source_id = next(iter(cleaner_app.session_state[cleaner_session.DC_TABLES_KEY]))

        control = next(
            widget
            for widget in cleaner_app.segmented_control
            if (widget.key or "").startswith("dc_cmd_summarise_")
        )
        control.set_value("group_summarise").run()
        cleaner_app.multiselect(key=f"dc_summarise_groupby_{source_id}").set_value(["cust_id"]).run()
        cleaner_app.multiselect(key=f"dc_summarise_values_{source_id}").set_value(["basic"]).run()
        cleaner_app.button(key=f"dc_save_summary_group_summarise_{source_id}").click().run()

        cleaner_tables = cleaner_app.session_state[cleaner_session.DC_TABLES_KEY]
        assert len(cleaner_tables) == 2

        app = AppTest.from_file(PAGE_PATH, default_timeout=60)
        for key, value in cleaner_app.session_state.filtered_state.items():
            app.session_state[key] = value
        app.run()
        next(button for button in app.button if button.key == "de_adopt_cleaner_button").click().run()

        assert not app.exception
        tables = app.session_state[engine_session.DE_TABLES_KEY]
        assert len(tables) == 2
        assert all(table.from_cleaner for table in tables.values())
        assert len({table.table_name for table in tables.values()}) == 2
        summary = next(table for table in tables.values() if "summary" in table.table_id)
        assert "sum_of_basic" in summary.semantic_types

    def test_adopting_twice_re_snapshots_rather_than_duplicating(self, tmp_path, monkeypatch):
        cleaner_app = self._with_cleaner_tables(tmp_path, monkeypatch)

        app = AppTest.from_file(PAGE_PATH, default_timeout=60)
        for key, value in cleaner_app.session_state.filtered_state.items():
            app.session_state[key] = value
        app.run()

        for _ in range(2):
            next(button for button in app.button if button.key == "de_adopt_cleaner_button").click().run()

        assert len(app.session_state[engine_session.DE_TABLES_KEY]) == 1


class TestShowCurrentData:
    """A transcript records what was asked, not what the data became."""

    def _open(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[engine_session.DE_DIALOG_KEY] = {"action": "show_data", "payload": {}}
        app.run()
        return app

    def test_the_table_is_shown_with_its_shape(self, tmp_path, monkeypatch):
        app = self._open(tmp_path, monkeypatch)

        assert not app.exception
        assert app.dataframe
        assert any("3 row(s) x 3 column(s)" in caption.value for caption in app.caption)

    def test_a_column_added_after_loading_is_already_there(self, tmp_path, monkeypatch):
        """The point of the dialog: it reflects the conversational changes, not the upload."""
        app = self._open(tmp_path, monkeypatch)
        connection = app.session_state[engine_session.DE_CONNECTION_KEY]
        engine_columns.add_calculated_column(connection, "sales", "tax", "basic * 0.10")
        # What `bump_rebuild` does, done from outside a script run: the preview is cached
        # on this counter, so without it the dialog would show the pre-change schema.
        app.session_state[engine_session.DE_REBUILD_KEY] = (
            app.session_state[engine_session.DE_REBUILD_KEY] + 1
        )
        # `_cached_preview` is `scope="session"`, which partitions per Streamlit session in
        # production but not across `AppTest`s in one pytest process — so an earlier test's
        # `("sales", 1, 100)` entry would be served here.
        st.cache_data.clear()
        app.run()

        # Any dataframe will do: the only other one on the page is step 1's file list,
        # which has no column called `tax` to be confused with.
        assert any("tax" in list(frame.value.columns) for frame in app.dataframe)


DEFAULT_STUB_ANSWER = Answer(question="stub", text="A stubbed answer.")


class TestChatPanel:
    def _in_chat(self, tmp_path, monkeypatch, with_model=True, answer=DEFAULT_STUB_ANSWER):
        """Opens the Chat view with the pipeline stubbed.

        Stubbed by default, not per test: an unpatched page test reaches
        `pipeline.answer`, which builds a real client and opens a real socket to whatever
        base URL the profile names. It fails fast here, but a test suite must not depend
        on a connection being refused — so no test in this class can reach the network.
        """
        monkeypatch.setattr(pipeline, "answer", lambda *args, **kwargs: answer)
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        if with_model:
            create_profile(1, "Stub", "local", "http://localhost:1234/v1", None, "stub-model")
        app.session_state["de_view"] = "Chat"
        app.run()
        return app

    def test_the_actions_menu_holds_only_what_chat_cannot_do(self, tmp_path, monkeypatch):
        """Adding and updating columns are conversational, so a menu entry for either
        would be a second way to say the same sentence. Deleting keeps its form because
        picking the column from a list is safer than naming it in prose."""
        app = self._in_chat(tmp_path, monkeypatch)
        menu = next(item for item in app.menu_button if item.key == "an_actions_menu")
        assert set(menu.options) == {"Show current data"}

    def test_the_input_is_disabled_until_a_session_model_is_picked(self, tmp_path, monkeypatch):
        """Requirement 3.3: the agent runs on the active session model. Saying so up front
        beats failing on the user's first question."""
        app = self._in_chat(tmp_path, monkeypatch, with_model=False)
        assert app.chat_input[0].disabled
        assert any("session model" in caption.value for caption in app.caption)

    def test_the_input_is_live_once_a_model_is_configured(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch)
        assert not app.chat_input[0].disabled

    def test_a_question_and_its_answer_both_land_in_the_transcript(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch)
        app.chat_input[0].set_value("total basic by sku").run()

        messages = app.session_state[chat_session.AN_MESSAGES_KEY]
        assert [message.role for message in messages] == [
            chat_session.ROLE_USER,
            chat_session.ROLE_ASSISTANT,
        ]
        assert messages[0].text == "total basic by sku"

    def test_an_answer_renders_its_table_sql_and_commentary(self, tmp_path, monkeypatch):
        answer = Answer(
            question="total basic by sku",
            text="s1 carries most of the value.",
            sql="SELECT sku, sum(basic) AS total FROM sales GROUP BY sku",
            frame=pd.DataFrame({"sku": ["s1", "s2"], "total": [350.0, 50.0]}),
            outputs={"dataframe", "commentary"},
        )
        app = self._in_chat(tmp_path, monkeypatch, answer=answer)
        app.chat_input[0].set_value("total basic by sku").run()

        assert not app.exception
        assert len(app.chat_message) == 2
        assert any("s1 carries most" in markdown.value for markdown in app.markdown)
        assert app.dataframe
        assert any("GROUP BY sku" in code.value for code in app.code)

    def test_the_transcript_survives_a_rerun_without_re_answering(self, tmp_path, monkeypatch):
        """Everything rendered comes from what was stored when the question was answered,
        so scrolling the chat costs nothing."""
        calls = []

        def counting_answer(*args, **kwargs):
            calls.append(args)
            return DEFAULT_STUB_ANSWER

        app = self._in_chat(tmp_path, monkeypatch)
        monkeypatch.setattr(pipeline, "answer", counting_answer)
        app.chat_input[0].set_value("anything").run()
        app.run()
        app.run()

        assert len(calls) == 1
        assert len(app.chat_message) == 2

    def test_a_failure_renders_as_an_error_message_not_a_broken_page(self, tmp_path, monkeypatch):
        answer = Answer(question="x", text="The model couldn't be reached.", is_error=True)
        app = self._in_chat(tmp_path, monkeypatch, answer=answer)
        app.chat_input[0].set_value("anything").run()

        assert not app.exception
        assert any("couldn't be reached" in error.value for error in app.error)

    def _chart_answer(self) -> Answer:
        """A charted answer carrying the choices behind it, as the pipeline builds it."""
        frame = pd.DataFrame({"sku": ["s1", "s2"], "total": [350.0, 50.0]})
        choices, _ = charts.choose_chart(frame, "total basic by sku as a chart")
        figure, _ = charts.render_chart(frame, choices)
        return Answer(
            question="total basic by sku as a chart",
            text="s1 carries most of the value.",
            sql="SELECT sku, sum(basic) AS total FROM sales GROUP BY sku",
            frame=frame,
            figure=figure,
            choices=choices,
            style=charts.ChartStyle(),
            outputs={"chart", "commentary"},
        )

    def _split_chart_answer(self) -> Answer:
        """A chart with a legend split, so it has two series to lay out."""
        frame = pd.DataFrame(
            {
                "sku": ["s1", "s1", "s2", "s2"],
                "status": ["open", "shut", "open", "shut"],
                "total": [10.0, 20.0, 30.0, 40.0],
            }
        )
        choices, _ = charts.choose_chart(frame, "total by sku as a chart")
        figure, _ = charts.render_chart(frame, choices)
        return Answer(
            question="total by sku as a chart",
            text="Here.",
            frame=frame,
            figure=figure,
            choices=choices,
            style=charts.ChartStyle(),
            outputs={"chart"},
        )

    def test_a_charted_answer_offers_controls_to_redraw_it(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        assert not app.exception
        assert [box.key for box in app.selectbox if box.key == "an_chart_kind_1"]
        assert [box.key for box in app.multiselect if box.key == "an_chart_y_1"]

    def test_the_controls_open_showing_the_chart_that_was_drawn(self, tmp_path, monkeypatch):
        """Opening the panel and closing it again must not change anything, so the widgets
        start on the automatic choices rather than on a guess of their own."""
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        kind = next(box for box in app.selectbox if box.key == "an_chart_kind_1")
        axis = next(box for box in app.selectbox if box.key == "an_chart_x_1")
        assert kind.value == charts.CHART_BAR
        assert axis.value == "sku"

    def test_only_the_types_this_result_supports_are_offered(self, tmp_path, monkeypatch):
        """One numeric column can't make a scatter, and the picker must not offer one."""
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        # AppTest reports the labels the user sees, not the values behind them.
        kind = next(box for box in app.selectbox if box.key == "an_chart_kind_1")
        assert charts.CHART_LABELS[charts.CHART_SCATTER] not in kind.options
        assert charts.CHART_LABELS[charts.CHART_BAR_HORIZONTAL] in kind.options

    def test_choosing_a_new_type_redraws_the_stored_chart(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        kind = next(box for box in app.selectbox if box.key == "an_chart_kind_1")
        kind.set_value(charts.CHART_BAR_HORIZONTAL).run()

        assert not app.exception
        message = app.session_state[chat_session.AN_MESSAGES_KEY][1]
        assert message.choices.kind == charts.CHART_BAR_HORIZONTAL
        assert message.figure.data[0].orientation == "h"

    def test_redrawing_does_not_ask_the_model_again(self, tmp_path, monkeypatch):
        """The rows are already in session state — a redraw is a redraw, not a new question."""
        calls = []

        def counting_answer(*args, **kwargs):
            calls.append(args)
            return self._chart_answer()

        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        monkeypatch.setattr(pipeline, "answer", counting_answer)
        app.chat_input[0].set_value("total basic by sku as a chart").run()
        assert len(calls) == 1

        next(box for box in app.selectbox if box.key == "an_chart_kind_1").set_value(
            charts.CHART_LINE
        ).run()
        assert len(calls) == 1

    def test_moving_the_axis_onto_the_legend_column_does_not_break(self, tmp_path, monkeypatch):
        """The legend's options depend on the axis, so the two must not be able to collide."""
        app = self._in_chat(tmp_path, monkeypatch, answer=self._split_chart_answer())
        app.chat_input[0].set_value("total by sku as a chart").run()

        next(box for box in app.selectbox if box.key == "an_chart_x_1").set_value("status").run()

        assert not app.exception
        assert app.session_state[chat_session.AN_MESSAGES_KEY][1].figure is not None

    def test_clearing_every_value_keeps_the_last_chart(self, tmp_path, monkeypatch):
        """An empty pick can't be drawn, and must not blank the chart that was there."""
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        next(box for box in app.multiselect if box.key == "an_chart_y_1").set_value([]).run()

        assert not app.exception
        assert app.session_state[chat_session.AN_MESSAGES_KEY][1].figure is not None
        assert any("at least one value" in caption.value for caption in app.caption)

    def test_the_style_tab_offers_its_controls(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        assert not app.exception
        assert [box for box in app.text_input if box.key == "an_chart_title_1"]
        assert [box for box in app.selectbox if box.key == "an_chart_palette_1"]
        assert [box for box in app.checkbox if box.key == "an_chart_values_1"]
        assert [box for box in app.slider if box.key == "an_chart_height_1"]

    def test_a_typed_title_replaces_the_derived_one(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        next(box for box in app.text_input if box.key == "an_chart_title_1").set_value(
            "Q3 sales by SKU"
        ).run()

        assert not app.exception
        message = app.session_state[chat_session.AN_MESSAGES_KEY][1]
        assert message.style.title == "Q3 sales by SKU"
        assert message.figure.layout.title.text == "Q3 sales by SKU"

    def test_style_survives_a_change_of_chart_type(self, tmp_path, monkeypatch):
        """Style and data are stored apart precisely so this holds."""
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        next(box for box in app.text_input if box.key == "an_chart_title_1").set_value("Mine").run()
        next(box for box in app.selectbox if box.key == "an_chart_kind_1").set_value(
            charts.CHART_LINE
        ).run()

        message = app.session_state[chat_session.AN_MESSAGES_KEY][1]
        assert message.choices.kind == charts.CHART_LINE
        assert message.figure.layout.title.text == "Mine"

    def test_showing_values_labels_the_chart(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        next(box for box in app.checkbox if box.key == "an_chart_values_1").set_value(True).run()

        assert not app.exception
        message = app.session_state[chat_session.AN_MESSAGES_KEY][1]
        assert message.style.show_values is True
        assert message.figure.data[0].texttemplate

    def test_the_stacking_control_appears_only_with_series_to_stack(self, tmp_path, monkeypatch):
        """One series has nothing to stack against, so the control would do nothing."""
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()
        assert not [box for box in app.selectbox if box.key == "an_chart_series_1"]

        split = self._in_chat(tmp_path, monkeypatch, answer=self._split_chart_answer())
        split.chat_input[0].set_value("total by sku as a chart").run()
        assert [box for box in split.selectbox if box.key == "an_chart_series_1"]

    def test_stacking_a_split_chart_redraws_it_stacked(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch, answer=self._split_chart_answer())
        app.chat_input[0].set_value("total by sku as a chart").run()

        next(box for box in app.selectbox if box.key == "an_chart_series_1").set_value(
            charts.SERIES_PERCENT
        ).run()

        assert not app.exception
        message = app.session_state[chat_session.AN_MESSAGES_KEY][1]
        assert message.figure.layout.barnorm == "percent"

    def test_restyling_does_not_ask_the_model_again(self, tmp_path, monkeypatch):
        calls = []

        def counting_answer(*args, **kwargs):
            calls.append(args)
            return self._chart_answer()

        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        monkeypatch.setattr(pipeline, "answer", counting_answer)
        app.chat_input[0].set_value("total basic by sku as a chart").run()
        assert len(calls) == 1

        next(box for box in app.checkbox if box.key == "an_chart_legend_1").set_value(False).run()
        assert len(calls) == 1
        assert app.session_state[chat_session.AN_MESSAGES_KEY][1].figure.layout.showlegend is False

    def test_reset_puts_the_automatic_chart_back(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        next(box for box in app.text_input if box.key == "an_chart_title_1").set_value("Mine").run()
        next(box for box in app.selectbox if box.key == "an_chart_kind_1").set_value(
            charts.CHART_LINE
        ).run()
        next(button for button in app.button if button.key == "an_chart_reset_1").click().run()

        assert not app.exception
        message = app.session_state[chat_session.AN_MESSAGES_KEY][1]
        assert message.style == charts.ChartStyle()
        assert message.choices.kind == charts.CHART_BAR
        assert message.figure.layout.title.text != "Mine"

    def test_reset_clears_the_controls_as_well_as_the_chart(self, tmp_path, monkeypatch):
        """Controls left showing selections the chart no longer reflects would be worse
        than not resetting at all."""
        app = self._in_chat(tmp_path, monkeypatch, answer=self._chart_answer())
        app.chat_input[0].set_value("total basic by sku as a chart").run()

        next(box for box in app.text_input if box.key == "an_chart_title_1").set_value("Mine").run()
        next(button for button in app.button if button.key == "an_chart_reset_1").click().run()

        assert next(box for box in app.text_input if box.key == "an_chart_title_1").value == ""

    def test_a_message_without_a_chart_offers_no_controls(self, tmp_path, monkeypatch):
        answer = Answer(
            question="list the rows",
            text="Here they are.",
            frame=pd.DataFrame({"sku": ["s1"], "total": [1.0]}),
            outputs={"dataframe"},
        )
        app = self._in_chat(tmp_path, monkeypatch, answer=answer)
        app.chat_input[0].set_value("list the rows").run()

        assert not [box for box in app.selectbox if box.key == "an_chart_kind_1"]

    def test_a_conversational_column_change_is_recorded_like_a_dialog_one(self, tmp_path, monkeypatch):
        """Requirement 7.5 replays one ordered statement list, so it must not matter which
        door the change came through."""
        answer = Answer(
            question="Add tax = 10% of basic",
            text="Added `tax` to `sales`.",
            sql='ALTER TABLE "sales" ADD COLUMN "tax" DOUBLE',
            statements=['ALTER TABLE "sales" ADD COLUMN "tax" DOUBLE'],
        )
        app = self._in_chat(tmp_path, monkeypatch, answer=answer)
        app.chat_input[0].set_value("Add tax = 10% of basic").run()

        assert any("ADD COLUMN" in statement for statement in app.session_state[engine_session.DE_STATEMENTS_KEY])

    def test_clearing_the_chat_keeps_the_tables(self, tmp_path, monkeypatch):
        app = self._in_chat(tmp_path, monkeypatch)
        app.chat_input[0].set_value("anything").run()
        next(button for button in app.button if button.key == "an_clear_chat_button").click().run()

        assert not app.session_state[chat_session.AN_MESSAGES_KEY]
        assert app.session_state[engine_session.DE_TABLES_KEY]


class TestAgentSessionStorage:
    """The agent keeps its own record of the conversation, on disk.

    Two jobs: follow-up questions resolve against what came before, and the stored runs
    are what a chat-history screen will later be built from.
    """

    def _ask(self, tmp_path, monkeypatch, question="anything"):
        received: dict = {}

        def capturing_answer(*args, **kwargs):
            received.update(kwargs)
            return DEFAULT_STUB_ANSWER

        monkeypatch.setattr(pipeline, "answer", capturing_answer)
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        create_profile(1, "Stub", "local", "http://localhost:1234/v1", None, "stub-model")
        app.session_state["de_view"] = "Chat"
        app.run()
        app.chat_input[0].set_value(question).run()
        return app, received

    def test_the_question_carries_a_store_and_a_session_id(self, tmp_path, monkeypatch):
        _, received = self._ask(tmp_path, monkeypatch)
        assert isinstance(received["db"], SqliteDb)
        assert received["session_id"].startswith("chat-")

    def test_the_store_is_a_real_file_on_disk(self, tmp_path, monkeypatch):
        """History that only exists in memory can't be listed back in a later session.

        The file is created lazily on first use, so this reads from the store to prove it
        is genuinely backed by the path it names.
        """
        _, received = self._ask(tmp_path, monkeypatch)
        expected = tmp_path / chat_session.CHAT_DB_PATH

        assert Path(received["db"].db_file) == expected
        received["db"].get_sessions()
        assert expected.exists()

    def test_the_same_store_is_reused_across_questions(self, tmp_path, monkeypatch):
        """Rebuilding it per run would reopen the file on every keystroke, and hand the
        agent an empty memory each time."""
        app, received = self._ask(tmp_path, monkeypatch, "first")
        first_db, first_session = received["db"], received["session_id"]
        app.chat_input[0].set_value("second").run()

        assert received["db"] is first_db
        assert received["session_id"] == first_session

    def test_clearing_the_chat_starts_a_new_agent_session(self, tmp_path, monkeypatch):
        """A cleared transcript the model still remembers is worse than no clear button:
        the next answer would refer back to questions the user can no longer see. The
        conversation just cleared stays in the store — clearing the screen is not deleting
        the record."""
        app, received = self._ask(tmp_path, monkeypatch)
        first_session = received["session_id"]
        next(button for button in app.button if button.key == "an_clear_chat_button").click().run()
        app.chat_input[0].set_value("after clearing").run()

        assert received["session_id"] != first_session
        # Same store, new conversation in it — clearing the screen is not deleting a record.
        assert received["db"] is not None

    def test_confirmed_relationships_reach_the_pipeline(self, tmp_path, monkeypatch):
        """What makes a cross-table conversational add ('bonus = ... if department is HR')
        possible at all: the links confirmed in Setup have to actually reach the call that
        can use them, not just exist in session state."""
        app, received = self._ask(tmp_path, monkeypatch)
        link = Relationship("sales", "sku", "stock", "sku")
        app.session_state[engine_session.DE_RELATIONSHIPS_KEY] = [link]
        app.chat_input[0].set_value("second").run()

        assert received["relationships"] == [link]

    def test_a_store_that_will_not_open_costs_the_history_not_the_answer(self, tmp_path, monkeypatch):
        def refuse(*args, **kwargs):
            raise ChatStorageError("Chat history couldn't be opened.")

        monkeypatch.setattr(chat_session, "agent_db", refuse)
        app, received = self._ask(tmp_path, monkeypatch)

        assert not app.exception
        assert received["db"] is None
        # The question was still answered, and the failure rides along with the answer —
        # written to the page directly it would be discarded by the rerun that follows.
        messages = app.session_state[chat_session.AN_MESSAGES_KEY]
        assert len(messages) == 2
        assert any("couldn't be opened" in warning for warning in messages[-1].warnings)


class TestPinToDashboard:
    """Requirement 6.1 steps 3 and 4: one click, no prompts, into the unplaced pool."""

    def _answered(self, tmp_path, monkeypatch, answer=None, question="anything"):
        monkeypatch.setattr(pipeline, "answer", lambda *args, **kwargs: answer or DEFAULT_STUB_ANSWER)
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        create_profile(1, "Stub", "local", "http://localhost:1234/v1", None, "stub-model")
        app.session_state["de_view"] = "Chat"
        app.run()
        app.chat_input[0].set_value(question).run()
        return app

    def _pool(self, app):
        return app.session_state[dashboard_session.DB_REPORT_KEY].pool

    def test_the_button_is_live(self, tmp_path, monkeypatch):
        app = self._answered(tmp_path, monkeypatch)
        assert not next(button for button in app.button if button.key == "an_pin_1").disabled

    def test_a_commentary_only_answer_can_be_pinned_too(self, tmp_path, monkeypatch):
        """Written commentary is one of requirement 6.2's three output types, so gating the
        button on a chart or a table would put a third of the outputs out of reach."""
        answer = Answer(question="why?", text="Because volumes fell.", outputs={"commentary"})
        app = self._answered(tmp_path, monkeypatch, answer=answer, question="why?")
        assert any(button.key == "an_pin_1" for button in app.button)

    def test_a_failed_answer_offers_no_pin(self, tmp_path, monkeypatch):
        answer = Answer(question="x", text="The model couldn't be reached.", is_error=True)
        app = self._answered(tmp_path, monkeypatch, answer=answer)
        assert not any(button.key == "an_pin_1" for button in app.button)

    def test_a_question_offers_no_pin(self, tmp_path, monkeypatch):
        """The user's own message is not an output."""
        app = self._answered(tmp_path, monkeypatch)
        assert not any(button.key == "an_pin_0" for button in app.button)

    def test_one_click_adds_exactly_one_pool_entry(self, tmp_path, monkeypatch):
        answer = Answer(
            question="total basic by sku",
            text="s1 carries most of the value.",
            sql="SELECT sku FROM sales",
            frame=pd.DataFrame({"sku": ["s1"], "total": [350.0]}),
            outputs={"dataframe", "commentary"},
        )
        app = self._answered(tmp_path, monkeypatch, answer=answer, question="total basic by sku")
        next(button for button in app.button if button.key == "an_pin_1").click().run()

        pool = self._pool(app)
        assert len(pool) == 1
        assert pool[0].heading == "total basic by sku"
        assert pool[0].comment == "s1 carries most of the value."
        assert pool[0].sql == "SELECT sku FROM sales"
        assert list(pool[0].frame["sku"]) == ["s1"]

    def test_a_pinned_frame_is_a_copy_not_the_transcripts_own(self, tmp_path, monkeypatch):
        """`analyst.session` releases the payload of older transcript messages, so a pinned
        item that referenced one would empty itself a dozen questions later."""
        answer = Answer(
            question="q",
            text="t",
            frame=pd.DataFrame({"sku": ["s1"], "total": [350.0]}),
            outputs={"dataframe"},
        )
        app = self._answered(tmp_path, monkeypatch, answer=answer)
        next(button for button in app.button if button.key == "an_pin_1").click().run()

        message = app.session_state[chat_session.AN_MESSAGES_KEY][1]
        message.release_payload()
        app.run()

        assert self._pool(app)[0].frame is not None
        assert list(self._pool(app)[0].frame["sku"]) == ["s1"]

    def test_pinning_does_not_re_answer_the_question(self, tmp_path, monkeypatch):
        """Requirement 6.1 step 3: a single click that costs nothing."""
        calls = []

        def counting_answer(*args, **kwargs):
            calls.append(args)
            return DEFAULT_STUB_ANSWER

        app = self._answered(tmp_path, monkeypatch)
        monkeypatch.setattr(pipeline, "answer", counting_answer)
        next(button for button in app.button if button.key == "an_pin_1").click().run()

        assert calls == []
        assert len(self._pool(app)) == 1

    def test_the_button_says_so_once_the_answer_is_pinned(self, tmp_path, monkeypatch):
        """A button that stays live after pinning reads as "press me again", which is
        exactly what produced duplicate tiles."""
        app = self._answered(tmp_path, monkeypatch)
        next(button for button in app.button if button.key == "an_pin_1").click().run()

        button = next(button for button in app.button if button.key == "an_pin_1")
        assert button.disabled
        assert button.label == "Pinned to Dashboard"

    def test_pressing_it_twice_still_pins_once(self, tmp_path, monkeypatch):
        """The disabled state is what the user sees; this is the guarantee underneath it,
        so a double-click landing two presses on one run can't make two copies either."""
        app = self._answered(tmp_path, monkeypatch)
        next(button for button in app.button if button.key == "an_pin_1").click().run()
        next(button for button in app.button if button.key == "an_pin_1").click().run()

        assert len(self._pool(app)) == 1

    def test_discarding_the_pinned_copy_offers_the_button_again(self, tmp_path, monkeypatch):
        """The state is the item being in the report, not a flag on the message — so
        throwing it away on the Dashboard genuinely un-pins the answer."""
        app = self._answered(tmp_path, monkeypatch)
        next(button for button in app.button if button.key == "an_pin_1").click().run()

        app.session_state[dashboard_session.DB_REPORT_KEY].pool.clear()
        app.run()

        button = next(button for button in app.button if button.key == "an_pin_1")
        assert not button.disabled
        assert button.label == "Pin to Dashboard"


class TestSurvivingNavigation:
    """Leaving the page and coming back must not cost the session.

    Streamlit drops a keyed widget's value once that widget stops being rendered, and
    `st.file_uploader` is the one widget with no `persist_state` to opt out. Deleting the
    uploader's key before a run *is* what a page switch does, so that is how it is
    simulated here — there is no other mechanism involved.
    """

    def _navigate_away_and_back(self, app):
        del app.session_state[engine_session.DE_UPLOADER_KEY]
        app.run()
        return app

    def test_coming_back_keeps_the_loaded_tables(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        assert len(app.session_state[engine_session.DE_TABLES_KEY]) == 1

        self._navigate_away_and_back(app)
        assert not app.exception
        assert [table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()] == ["sales"]

    def test_it_keeps_them_on_every_later_run_too(self, tmp_path, monkeypatch):
        """The uploader is rebuilt empty on the return run, so from the next run onward it
        reports an empty list quite legitimately. Detaching once is what stops that
        reading as "the user removed everything" a moment later."""
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        self._navigate_away_and_back(app)
        app.run()
        app.run()

        assert app.session_state[engine_session.DE_TABLES_KEY]

    def test_the_chat_transcript_survives_with_them(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[chat_session.AN_MESSAGES_KEY] = [
            chat_session.ChatMessage(role=chat_session.ROLE_USER, text="what sold most?")
        ]
        self._navigate_away_and_back(app)

        assert len(app.session_state[chat_session.AN_MESSAGES_KEY]) == 1

    def test_a_later_upload_adds_rather_than_replaces(self, tmp_path, monkeypatch):
        """The uploader knows nothing about the detached tables, so its next file has to
        join them rather than reconcile them away."""
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        self._navigate_away_and_back(app)
        _upload(app, ("customer.csv", CUSTOMER_CSV))

        names = {table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
        assert names == {"sales", "customer"}

    def test_the_page_explains_the_empty_looking_uploader(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        self._navigate_away_and_back(app)

        assert any("still here and still queryable" in caption.value for caption in app.caption)

    def test_removing_a_file_still_drops_its_table_while_the_uploader_owns_it(
        self, tmp_path, monkeypatch
    ):
        """The fix must not cost the normal case: while the uploader is reporting, taking
        a file out of it still takes the table with it."""
        app = _upload(
            _make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("customer.csv", CUSTOMER_CSV)
        )
        assert len(app.session_state[engine_session.DE_TABLES_KEY]) == 2

        _upload(app, ("sales.csv", SALES_CSV))
        names = {table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
        assert names == {"sales"}


class TestRemoveTable:
    """The buttons live under the loaded-tables summary, so Step 1 has to be open — which
    it is whenever someone is actually managing their files."""

    def test_it_drops_one_table_and_leaves_the_others(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("customer.csv", CUSTOMER_CSV)
        )
        _open_step(app, engine_session.STEP_UPLOAD)
        tables = app.session_state[engine_session.DE_TABLES_KEY]
        sales_id = next(table_id for table_id, table in tables.items() if table.table_name == "sales")

        next(button for button in app.button if button.key == f"de_remove_table_{sales_id}").click().run()

        names = {table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
        assert names == {"customer"}

    def test_the_uploader_does_not_load_it_straight_back(self, tmp_path, monkeypatch):
        """The widget has no API for taking a file out of it, so its entry is still there
        after Remove — and would reconcile the table back in on the very next run."""
        app = _upload(
            _make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("customer.csv", CUSTOMER_CSV)
        )
        _open_step(app, engine_session.STEP_UPLOAD)
        tables = app.session_state[engine_session.DE_TABLES_KEY]
        sales_id = next(table_id for table_id, table in tables.items() if table.table_name == "sales")

        next(button for button in app.button if button.key == f"de_remove_table_{sales_id}").click().run()
        app.run()
        app.run()

        names = {table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
        assert names == {"customer"}

    def test_it_reaches_a_table_the_uploader_no_longer_holds(self, tmp_path, monkeypatch):
        """The reason the button exists: a detached table has no entry in the uploader, so
        without this the only way to drop it was Start over."""
        app = _upload(
            _make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("customer.csv", CUSTOMER_CSV)
        )
        del app.session_state[engine_session.DE_UPLOADER_KEY]
        app.run()
        _open_step(app, engine_session.STEP_UPLOAD)

        tables = app.session_state[engine_session.DE_TABLES_KEY]
        sales_id = next(table_id for table_id, table in tables.items() if table.table_name == "sales")
        next(button for button in app.button if button.key == f"de_remove_table_{sales_id}").click().run()

        names = {table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()}
        assert names == {"customer"}

    def test_it_takes_any_link_that_used_the_table_with_it(self, tmp_path, monkeypatch):
        """A relationship naming a table that is gone would break the next rebuild."""
        app = _upload(
            _make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV), ("customer.csv", CUSTOMER_CSV)
        )
        app.session_state[engine_session.DE_RELATIONSHIPS_KEY] = [
            Relationship("sales", "cust_id", "customer", "id")
        ]
        _open_step(app, engine_session.STEP_UPLOAD)

        tables = app.session_state[engine_session.DE_TABLES_KEY]
        customer_id = next(table_id for table_id, table in tables.items() if table.table_name == "customer")
        next(button for button in app.button if button.key == f"de_remove_table_{customer_id}").click().run()

        assert app.session_state[engine_session.DE_RELATIONSHIPS_KEY] == []


class TestReset:
    def test_start_over_clears_everything(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        next(button for button in app.button if button.key == "de_start_over_button").click().run()

        assert not app.session_state[engine_session.DE_TABLES_KEY]
        assert any("Upload a CSV or Excel file" in info.value for info in app.info)

    def test_start_over_clears_the_transcript_too(self, tmp_path, monkeypatch):
        """A chat about tables that no longer exist would read as if it described whatever
        is loaded next."""
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[chat_session.AN_MESSAGES_KEY] = [
            chat_session.ChatMessage(role=chat_session.ROLE_USER, text="old question")
        ]
        app.run()
        next(button for button in app.button if button.key == "de_start_over_button").click().run()

        assert chat_session.AN_MESSAGES_KEY not in app.session_state

    def test_start_over_clears_the_dashboard_too(self, tmp_path, monkeypatch):
        """A report built on tables that no longer exist would read as if it described
        whatever is loaded next, exactly as a stale transcript would."""
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[dashboard_session.DB_REPORT_KEY] = dashboard_session.Report(title="Old")
        app.run()
        next(button for button in app.button if button.key == "de_start_over_button").click().run()

        assert dashboard_session.DB_REPORT_KEY not in app.session_state

    def test_start_over_begins_a_new_stored_conversation(self, tmp_path, monkeypatch):
        """Start-over releases the handle and the session id, so the next question opens a
        new conversation. It does not delete anything: the store is the chat history, and
        loading different files is not a reason to erase what was asked about the old ones."""
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[chat_session.AN_AGENT_SESSION_KEY] = "chat-old"
        app.run()
        next(button for button in app.button if button.key == "de_start_over_button").click().run()

        assert chat_session.AN_AGENT_DB_KEY not in app.session_state
        assert chat_session.AN_AGENT_SESSION_KEY not in app.session_state
