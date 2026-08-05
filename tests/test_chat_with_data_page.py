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
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from cleaner import session as cleaner_session
from engine import session as engine_session
from engine.relationships import Relationship
from llm.db import init_llm_table

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


class TestCalculatedColumns:
    def test_the_dialog_adds_a_column_and_records_its_sql(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[engine_session.DE_DIALOG_KEY] = {"action": "add_column", "payload": {}}
        app.run()

        app.text_input(key="de_calc_name").set_value("tax").run()
        app.text_input(key="de_calc_expression").set_value("basic * 0.10").run()
        next(button for button in app.button if button.key == "de_calc_add_button").click().run()

        assert not app.exception
        statements = app.session_state[engine_session.DE_STATEMENTS_KEY]
        assert any("ADD COLUMN" in statement for statement in statements)

        connection = app.session_state[engine_session.DE_CONNECTION_KEY]
        assert connection.execute("SELECT tax FROM sales ORDER BY tax").fetchall()[0][0] == 5.0

    def test_a_bad_formula_is_reported_and_adds_nothing(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        app.session_state[engine_session.DE_DIALOG_KEY] = {"action": "add_column", "payload": {}}
        app.run()

        app.text_input(key="de_calc_name").set_value("tax").run()
        app.text_input(key="de_calc_expression").set_value("bsic * 0.10").run()

        assert app.warning
        assert not app.session_state[engine_session.DE_STATEMENTS_KEY]


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

    def test_adopting_twice_re_snapshots_rather_than_duplicating(self, tmp_path, monkeypatch):
        cleaner_app = self._with_cleaner_tables(tmp_path, monkeypatch)

        app = AppTest.from_file(PAGE_PATH, default_timeout=60)
        for key, value in cleaner_app.session_state.filtered_state.items():
            app.session_state[key] = value
        app.run()

        for _ in range(2):
            next(button for button in app.button if button.key == "de_adopt_cleaner_button").click().run()

        assert len(app.session_state[engine_session.DE_TABLES_KEY]) == 1


class TestReset:
    def test_start_over_clears_everything(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("sales.csv", SALES_CSV))
        next(button for button in app.button if button.key == "de_start_over_button").click().run()

        assert not app.session_state[engine_session.DE_TABLES_KEY]
        assert any("Upload a CSV or Excel file" in info.value for info in app.info)
