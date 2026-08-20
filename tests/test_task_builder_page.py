"""AppTest coverage for the Task Builder page (requirement 7).

The whole page, driven through real widgets. Only two things are stubbed — the SQL builder
and the AI connection — because those are the seams between this page and a provider.

Four behaviours here are worth more than the rest, because each would stay invisible until
someone had done real work and then be expensive to discover:

- **the role gate**, since a Task is an admin artefact;
- **pinning landing in the Task's report and not the session Dashboard**, which is the whole
  reason `dashboard.session` grew an active-report key;
- **the Checks view arriving without its set bar and without Actions**, which is the user's
  own shaping of requirement 7.1; and
- **a Task saved and re-opened**, which is the point of the page existing.
"""

from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_pages import report_items_view
from auth.db import create_user, init_db, seed_default_admin
from checks.db import init_check_sets_table
from chat_types.db import init_chat_types_table
from engine import session as engine_session
from llm.db import create_profile, init_llm_table
from tasks.db import init_tasks_table, list_tasks

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "task_builder.py")

SALARY_CSV = b"employee,department,basic,bonus\nAna,HR,1000,40\nBo,HR,1000,120\nCy,Accounts,2000,90\n"

RESULT = pd.DataFrame({"department": ["HR", "Accounts"], "pay": [2000.0, 2000.0]})

STUB_SQL = "SELECT department, sum(basic) AS pay FROM salary GROUP BY department"


TASK_NAME = "Monthly salary review"


def _gate(tmp_path, monkeypatch, role="admin"):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    init_chat_types_table()
    init_check_sets_table()
    init_tasks_table()
    # The page needs a selectable connection before it will call a model at all.
    create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")

    app = AppTest.from_file(PAGE_PATH, default_timeout=90)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = role
    app.run()
    return app


def _make_app(tmp_path, monkeypatch, role="admin", name=TASK_NAME):
    """The page with a task open — which every view but the gate needs.

    The gate is the page's first screen now, so a test that wants the views has to answer its
    one question first. `_gate` is the same page stopped one step earlier.
    """
    app = _gate(tmp_path, monkeypatch, role=role)
    if not [button for button in app.button if button.key == "tb_create_task"]:
        # A role that was refused the page never sees the gate.
        return app
    app.text_input(key="tb_new_task_name").set_value(name).run()
    app.button(key="tb_create_task").click().run()
    assert not app.exception
    return app


def _open_saved(app, task_id):
    """Opens a saved task from the gate."""
    app.button(key=f"tb_gate_open_{task_id}").click().run()
    assert not app.exception
    return app


def _switch(app):
    """Presses Switch task, which lands on the gate when there is nothing unsaved."""
    app.button(key="tb_switch_task").click().run()
    assert not app.exception
    return app


def _loaded(tmp_path, monkeypatch):
    """A session with one table loaded, sitting on Setup."""
    app = _make_app(tmp_path, monkeypatch)
    app.session_state[engine_session.STEP_UPLOAD] = True
    app.run()
    app.file_uploader(key=engine_session.DE_UPLOADER_KEY).set_value(
        [("salary.csv", SALARY_CSV, "text/csv")]
    )
    app.run()
    return app


def _view(app, name):
    app.session_state["tb_view"] = name
    app.run()
    assert not app.exception
    return app


def _items(app):
    """The report item list, or [] when the key was never created.

    A helper because `AppTest.session_state` is a `SafeSessionState`, which has no `.get` —
    reading a missing key on it raises rather than returning a default.
    """
    return app.session_state["ri_items"] if "ri_items" in app.session_state else []


def _stub_generate(monkeypatch):
    monkeypatch.setattr(
        report_items_view.sql_builder, "generate_and_run", lambda *args, **kwargs: (STUB_SQL, RESULT)
    )


def _add_item(app, heading="Payroll by department"):
    app.button(key="ri_add_item").click().run()
    app.text_input(key="ri_new_item_heading").set_value(heading).run()
    app.button(key="ri_new_item_add").click().run()
    assert not app.exception
    return app.session_state["ri_items"][-1].item_id


class TestAccess:
    def test_an_admin_gets_the_page(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch, role="admin")

        assert not app.exception
        assert app.segmented_control(key="tb_view").options == [
            "Setup",
            "Report-Items",
            "Checks",
            "Report",
        ]

    def test_a_superuser_gets_the_page(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch, role="superuser")

        assert not app.exception
        assert app.segmented_control(key="tb_view")

    def test_a_normal_user_is_refused(self, tmp_path, monkeypatch):
        """The page carries its own guard as well as being registered conditionally:
        navigation is not access control."""
        app = _make_app(tmp_path, monkeypatch, role="normal_user")

        assert any("permission" in error.value for error in app.error)
        assert not [control for control in app.segmented_control if control.key == "tb_view"]


class TestTheGate:
    """One question first: open a saved task, or start a new one.

    Everything below the gate is about a task the user has already chosen, which is what the
    old screen — an empty name box, a Save button and four views — left them to infer.
    """

    def test_the_page_opens_on_the_gate_and_nothing_else(self, tmp_path, monkeypatch):
        app = _gate(tmp_path, monkeypatch)

        assert not app.exception
        assert app.text_input(key="tb_new_task_name") is not None
        assert not [control for control in app.segmented_control if control.key == "tb_view"]

    def test_a_new_task_cannot_be_created_without_a_name(self, tmp_path, monkeypatch):
        app = _gate(tmp_path, monkeypatch)

        assert app.button(key="tb_create_task").disabled

    def test_creating_a_task_opens_the_views(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)

        assert app.segmented_control(key="tb_view") is not None
        assert app.session_state["tb_task_name"] == TASK_NAME
        assert app.session_state["tb_task_id"] is None

    def test_a_name_already_in_use_is_refused(self, tmp_path, monkeypatch):
        """`tasks.db.save_task` reads a name in use as "update that row", so a collision is
        not an error later — it is a silent overwrite of the other task."""
        app = _make_app(tmp_path, monkeypatch)
        app.button(key="tb_save_task").click().run()
        _switch(app)

        app.text_input(key="tb_new_task_name").set_value(TASK_NAME).run()
        app.button(key="tb_create_task").click().run()

        assert any("already have a task" in error.value for error in app.error)
        assert not [control for control in app.segmented_control if control.key == "tb_view"]

    def test_the_gate_lists_a_saved_task_and_opens_it(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.button(key="tb_save_task").click().run()
        _switch(app)
        task_id = list_tasks(1)[0]["task_id"]

        _open_saved(app, task_id)

        assert app.session_state["tb_task_id"] == task_id
        assert app.segmented_control(key="tb_view") is not None

    def test_deleting_from_the_gate_asks_first(self, tmp_path, monkeypatch):
        """Open and Delete are the same motion on a card, unlike the dropdown this replaced."""
        app = _make_app(tmp_path, monkeypatch)
        app.button(key="tb_save_task").click().run()
        _switch(app)
        task_id = list_tasks(1)[0]["task_id"]

        app.button(key=f"tb_gate_delete_{task_id}").click().run()

        assert not app.exception
        assert list_tasks(1) != []

        app.button(key="tb_delete_confirm").click().run()

        assert not app.exception
        assert list_tasks(1) == []

    def test_cancelling_the_delete_keeps_the_task(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.button(key="tb_save_task").click().run()
        _switch(app)
        task_id = list_tasks(1)[0]["task_id"]
        app.button(key=f"tb_gate_delete_{task_id}").click().run()

        app.button(key="tb_delete_cancel").click().run()

        assert [row["name"] for row in list_tasks(1)] == [TASK_NAME]

    def test_switching_with_nothing_unsaved_goes_straight_back(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.button(key="tb_save_task").click().run()

        app.button(key="tb_switch_task").click().run()

        assert not [button for button in app.button if button.key == "tb_switch_discard"]
        assert app.text_input(key="tb_new_task_name") is not None

    def test_switching_with_unsaved_work_asks_first(self, tmp_path, monkeypatch):
        """The one press that can lose work."""
        app = _view(_loaded(tmp_path, monkeypatch), "Report-Items")
        _add_item(app)
        _view(app, "Setup")

        app.button(key="tb_switch_task").click().run()

        assert app.button(key="tb_switch_discard") is not None
        # Still on the task until the question is answered.
        assert app.segmented_control(key="tb_view") is not None

    def test_save_and_switch_writes_before_leaving(self, tmp_path, monkeypatch):
        app = _view(_loaded(tmp_path, monkeypatch), "Report-Items")
        _add_item(app)
        _view(app, "Setup")
        app.button(key="tb_switch_task").click().run()

        app.button(key="tb_switch_save").click().run()

        assert not app.exception
        assert [row["name"] for row in list_tasks(1)] == [TASK_NAME]
        assert app.text_input(key="tb_new_task_name") is not None


class TestRenaming:
    def test_renaming_changes_the_open_tasks_name(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)

        app.button(key="tb_rename_task").click().run()
        app.text_input(key="tb_rename_name").set_value("Quarterly review").run()
        app.button(key="tb_rename_confirm").click().run()

        assert not app.exception
        assert app.session_state["tb_task_name"] == "Quarterly review"

    def test_renaming_onto_another_tasks_name_is_refused(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        app.button(key="tb_save_task").click().run()
        _switch(app)
        app.text_input(key="tb_new_task_name").set_value("Bonus audit").run()
        app.button(key="tb_create_task").click().run()

        app.button(key="tb_rename_task").click().run()
        app.text_input(key="tb_rename_name").set_value(TASK_NAME).run()
        app.button(key="tb_rename_confirm").click().run()

        assert any("already have a task" in error.value for error in app.error)
        assert app.session_state["tb_task_name"] == "Bonus audit"


class TestTheShape:
    def test_there_is_no_chat_type_picker(self, tmp_path, monkeypatch):
        """A Task *is* the saved setup, so a second saved-setup concept on the same screen
        would be two answers to one question."""
        app = _make_app(tmp_path, monkeypatch)

        assert not [box for box in app.selectbox if box.key == "ct_picker"]

    def test_the_task_header_sits_above_the_view_control(self, tmp_path, monkeypatch):
        """The name is a heading now, not a box: Save means write *this* task, rather than
        "write whatever the name box says", which quietly overwrote another task if it said
        that task's name."""
        app = _make_app(tmp_path, monkeypatch)

        assert not [box for box in app.text_input if box.key == "tb_task_name"]
        assert app.button(key="tb_save_task")
        assert app.button(key="tb_rename_task")
        assert app.button(key="tb_switch_task")

    def test_the_persona_and_description_live_at_the_foot_of_setup(self, tmp_path, monkeypatch):
        """Requirement 7.2: one persona for the whole Task. It is on Setup rather than in
        either view because *both* Report-Items and Checks read it."""
        app = _loaded(tmp_path, monkeypatch)

        assert app.text_area(key="tb_persona") is not None
        assert app.text_input(key="tb_task_description") is not None

    def test_the_upload_widget_survives_a_view_change(self, tmp_path, monkeypatch):
        """The hidden mount is not dead UI: a widget that stops being rendered stops
        reporting its value, and `sync_tables` would drop every loaded table."""
        app = _loaded(tmp_path, monkeypatch)
        assert engine_session.get_tables

        _view(app, "Report-Items")
        _view(app, "Checks")
        _view(app, "Setup")

        assert list(app.session_state[engine_session.DE_TABLES_KEY])

    def test_the_page_keeps_the_same_top_level_shape_in_every_state(self, tmp_path, monkeypatch):
        """The fix for two "Step 1 · Your data" headers on screen at once.

        Streamlit addresses an element by its position among its parent's children, and only
        clears what the previous run left behind when a run finishes without asking for
        another. A message that appears on some runs, or a gate that replaces half the page,
        moves everything below it — and the browser keeps the old copy at the old position. So
        the flash, the gate and the open Task each get a container that is always there, and
        the page's top level is the same four children whatever is showing.
        """
        shapes = {}

        app = _gate(tmp_path, monkeypatch)
        shapes["gate"] = [child.type for child in app.main.children.values()]

        app.text_input(key="tb_new_task_name").set_value(TASK_NAME).run()
        # This run carries the "task created" message; the next one has nothing to say.
        app.button(key="tb_create_task").click().run()
        shapes["with a flash"] = [child.type for child in app.main.children.values()]

        app.run()
        shapes["settled"] = [child.type for child in app.main.children.values()]

        _view(app, "Checks")
        shapes["off setup"] = [child.type for child in app.main.children.values()]

        _switch(app)
        shapes["back at the gate"] = [child.type for child in app.main.children.values()]

        assert len(set(map(tuple, shapes.values()))) == 1, shapes


class TestGates:
    def test_report_items_asks_for_data_before_showing_the_list(self, tmp_path, monkeypatch):
        app = _view(_make_app(tmp_path, monkeypatch), "Report-Items")

        assert any("Upload your data" in info.value for info in app.info)
        assert not [button for button in app.button if button.key == "ri_add_item"]

    def test_checks_asks_for_data_too(self, tmp_path, monkeypatch):
        app = _view(_make_app(tmp_path, monkeypatch), "Checks")

        assert any("Upload your data" in info.value for info in app.info)

    def test_a_blocked_view_carries_its_own_way_back_to_setup(self, tmp_path, monkeypatch):
        """Step 1 isn't on this screen, so "scroll up" would name something off screen."""
        app = _view(_make_app(tmp_path, monkeypatch), "Report-Items")

        app.button(key="tb_items_go_to_setup_button").click().run()

        assert app.session_state["tb_view"] == "Setup"


class TestChecksIsReused:
    def test_the_criteria_set_bar_is_not_offered(self, tmp_path, monkeypatch):
        """The criteria are saved with the Task, so Save set / Load set would be a second
        answer to one question."""
        app = _view(_loaded(tmp_path, monkeypatch), "Checks")

        assert not [box for box in app.text_input if box.key == "ck_set_name"]
        assert not [button for button in app.button if button.key == "ck_save_set"]
        assert not [box for box in app.text_area if box.key == "ck_persona"]

    def test_the_design_form_is_still_there(self, tmp_path, monkeypatch):
        app = _view(_loaded(tmp_path, monkeypatch), "Checks")

        assert app.button(key="ck_add_check")

    def test_there_is_no_actions_tab(self, tmp_path, monkeypatch):
        """A drafted email answers *this month's* exceptions; a Task is a reusable recipe."""
        app = _view(_loaded(tmp_path, monkeypatch), "Checks")

        assert "Actions" not in [tab.label for tab in app.tabs]

    def test_the_tasks_persona_becomes_the_sets_persona(self, tmp_path, monkeypatch):
        """One value, so there is no second place for the two to disagree."""
        app = _loaded(tmp_path, monkeypatch)
        app.text_area(key="tb_persona").set_value("You are a finance controller.").run()

        _view(app, "Checks")

        assert app.session_state["ck_set"].persona == "You are a finance controller."


class TestPinningGoesToTheTask:
    def test_a_pinned_item_lands_in_the_tasks_report(self, tmp_path, monkeypatch):
        app = _view(_loaded(tmp_path, monkeypatch), "Report-Items")
        item_id = _add_item(app)
        _stub_generate(monkeypatch)
        app.text_area(key=f"ri_request_{item_id}").set_value("Total pay per department").run()
        app.button(key=f"ri_generate_{item_id}").click().run()

        app.button(key=f"ri_pin_{item_id}").click().run()

        assert not app.exception
        assert len(app.session_state["tb_report"].pool) == 1
        # The session Dashboard is not merely empty — it was never created.
        assert "db_report" not in app.session_state

    def test_the_report_view_shows_what_was_pinned(self, tmp_path, monkeypatch):
        app = _view(_loaded(tmp_path, monkeypatch), "Report-Items")
        item_id = _add_item(app)
        _stub_generate(monkeypatch)
        app.text_area(key=f"ri_request_{item_id}").set_value("Total pay per department").run()
        app.button(key=f"ri_generate_{item_id}").click().run()
        app.button(key=f"ri_pin_{item_id}").click().run()

        _view(app, "Report")

        assert not app.exception
        assert app.text_input(key="db_report_title") is not None

    def test_an_empty_report_offers_the_way_to_make_one(self, tmp_path, monkeypatch):
        app = _view(_loaded(tmp_path, monkeypatch), "Report")

        assert not app.exception
        assert any("Pin to report" in info.value for info in app.info)


class TestColumnStepsAgainstRealTables:
    """The dictionary side of a column step, which needs tables actually loaded.

    `test_report_items_page.py` drives the view on its own, with no upload behind it, so the
    entry a new column gets in the data dictionary can only be checked from here.
    """

    class _Change:
        action = "add"
        summary = "Added `tax` to `salary`."
        warnings: list[str] = []
        explanation = "Ten percent of basic pay, as tax."
        statements = [
            "ALTER TABLE salary ADD COLUMN tax DOUBLE",
            "UPDATE salary SET tax = basic * 0.10",
        ]

    def _applied(self, monkeypatch, app):
        _view(app, "Report-Items")
        app.button(key="ri_add_column_step").click().run()
        app.text_input(key="ri_new_item_heading").set_value("Add tax").run()
        app.button(key="ri_new_item_add").click().run()
        item_id = app.session_state["ri_items"][-1].item_id

        def apply_for_real(*args, **kwargs):
            # The stub stands in for the *model*, not for the engine: the dictionary is
            # rebuilt from DuckDB, so a step that never touched the table would leave nothing
            # for this test to find.
            change = self._Change()
            for statement in change.statements:
                engine_session.connection().execute(statement)
            return change

        monkeypatch.setattr(report_items_view.column_intent, "handle_message", apply_for_real)
        app.text_area(key=f"ri_request_{item_id}").set_value("Add tax = 10% of basic").run()
        app.button(key=f"ri_apply_{item_id}").click().run()
        assert not app.exception
        return app, item_id

    def test_the_new_column_gets_the_ai_description_in_the_dictionary(self, tmp_path, monkeypatch):
        """The payoff runs past Setup's grid: `schema_context()` then explains the column to
        every item below the step."""
        app, _ = self._applied(monkeypatch, _loaded(tmp_path, monkeypatch))

        entries = app.session_state[engine_session.DE_DICTIONARY_KEY]
        added = [entry for entry in entries if entry.column == "tax"]
        assert added and added[0].description == "Ten percent of basic pay, as tax."

    def test_a_column_that_was_already_there_keeps_its_own_description(self, tmp_path, monkeypatch):
        """`dictionary.apply_suggestions`' rule: typed text is the user's, and a generated
        sentence is not worth losing it for."""
        app = _loaded(tmp_path, monkeypatch)
        for entry in app.session_state[engine_session.DE_DICTIONARY_KEY]:
            if entry.column == "basic":
                entry.description = "Monthly basic pay."

        app, _ = self._applied(monkeypatch, app)

        entries = app.session_state[engine_session.DE_DICTIONARY_KEY]
        basic = [entry for entry in entries if entry.column == "basic"]
        assert basic and basic[0].description == "Monthly basic pay."


class TestSavingAndOpening:
    def _saved(self, tmp_path, monkeypatch):
        app = _view(_loaded(tmp_path, monkeypatch), "Report-Items")
        item_id = _add_item(app, "Payroll by department")
        _stub_generate(monkeypatch)
        app.text_area(key=f"ri_request_{item_id}").set_value("Total pay per department").run()
        app.button(key=f"ri_generate_{item_id}").click().run()
        app.button(key=f"ri_pin_{item_id}").click().run()

        _view(app, "Setup")
        app.text_area(key="tb_persona").set_value("You are a finance controller.").run()
        app.button(key="tb_save_task").click().run()
        assert not app.exception
        return app, item_id

    def test_saving_writes_a_row_and_says_so(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)

        rows = list_tasks(1)
        assert [row["name"] for row in rows] == [TASK_NAME]
        assert any("Saved" in message.value for message in app.success)

    def test_the_saved_row_holds_the_recipe_and_none_of_the_rows(self, tmp_path, monkeypatch):
        """The rule the whole stage rests on: a Task run next month must not report last
        month's numbers."""
        self._saved(tmp_path, monkeypatch)

        from tasks.db import load_task

        loaded = load_task(list_tasks(1)[0]["task_id"], 1)

        assert loaded.persona == "You are a finance controller."
        assert [item.heading for item in loaded.report_items] == ["Payroll by department"]
        assert loaded.report_items[0].sql == STUB_SQL
        assert loaded.report_items[0].saved_run is None
        assert loaded.schema.table_names() == ["salary"]

    def test_opening_a_task_puts_its_recipe_back_on_screen(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)
        task_id = list_tasks(1)[0]["task_id"]
        # Everything on screen is discarded, so what comes back is genuinely from storage.
        # Start over also closes the task, which lands on the gate.
        app.button(key="tb_start_over_button").click().run()
        assert _items(app) == []

        _open_saved(app, task_id)

        assert [item.heading for item in app.session_state["ri_items"]] == ["Payroll by department"]
        assert app.session_state["tb_persona"] == "You are a finance controller."
        assert app.session_state["tb_task_name"] == TASK_NAME

    def test_a_reopened_task_reads_as_saved(self, tmp_path, monkeypatch):
        """Opening a task must not report it as edited before anything has been touched —
        which is why the fingerprint covers the recipe only and not the live session."""
        app, _ = self._saved(tmp_path, monkeypatch)
        task_id = list_tasks(1)[0]["task_id"]
        app.button(key="tb_start_over_button").click().run()
        _open_saved(app, task_id)

        app.button(key="tb_switch_task").click().run()

        assert not [button for button in app.button if button.key == "tb_switch_discard"]

    def test_a_reopened_item_carries_its_sql_but_no_result(self, tmp_path, monkeypatch):
        """A loaded recipe describes data that is not loaded yet — one press per item
        re-runs it against this month's file, with no provider call."""
        app, _ = self._saved(tmp_path, monkeypatch)
        task_id = list_tasks(1)[0]["task_id"]
        app.button(key="tb_start_over_button").click().run()
        _open_saved(app, task_id)

        item = app.session_state["ri_items"][0]
        assert item.sql == STUB_SQL
        assert item.saved_run is None

    def test_saving_twice_updates_rather_than_accumulates(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)

        app.button(key="tb_save_task").click().run()

        assert len(list_tasks(1)) == 1

    def test_the_persona_survives_a_save_pressed_from_another_view(self, tmp_path, monkeypatch):
        """The regression the earlier tests all missed by saving from Setup.

        The persona box is only created on Setup, and Streamlit drops a widget's value on any
        run where the widget is not rendered — while Save sits in the task bar, on every view.
        Without `persist_state`, this saves an empty persona and the reopened Task has none.
        """
        app = _loaded(tmp_path, monkeypatch)
        app.text_area(key="tb_persona").set_value("You are a finance controller.").run()

        _view(app, "Checks")
        app.button(key="tb_save_task").click().run()
        assert not app.exception

        _switch(app)
        _open_saved(app, list_tasks(1)[0]["task_id"])
        _view(app, "Setup")

        assert app.text_area(key="tb_persona").value == "You are a finance controller."

    def test_a_blank_persona_does_not_wipe_the_criteria_sets_copy(self, tmp_path, monkeypatch):
        """The knock-on: `render_checks` writes the caller's persona onto the set, and the
        caller's box may simply not have been rendered on this run."""
        app = _loaded(tmp_path, monkeypatch)
        app.text_area(key="tb_persona").set_value("You are a finance controller.").run()
        _view(app, "Checks")
        assert app.session_state["ck_set"].persona == "You are a finance controller."

        app.session_state["tb_persona"] = ""
        app.run()

        assert app.session_state["ck_set"].persona == "You are a finance controller."


class TestStartOver:
    def test_it_clears_the_tasks_report_along_with_everything_else(self, tmp_path, monkeypatch):
        app = _view(_loaded(tmp_path, monkeypatch), "Report-Items")
        item_id = _add_item(app)
        _stub_generate(monkeypatch)
        app.text_area(key=f"ri_request_{item_id}").set_value("Total pay").run()
        app.button(key=f"ri_generate_{item_id}").click().run()
        app.button(key=f"ri_pin_{item_id}").click().run()

        _view(app, "Setup")
        app.button(key="tb_start_over_button").click().run()

        assert not app.exception
        assert _items(app) == []
        assert "tb_report" not in app.session_state
