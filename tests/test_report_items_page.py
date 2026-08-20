"""AppTest coverage for the Report-Items view (requirement 7.3 step 3).

Driven with `AppTest.from_function` rather than through a page file, for the reason
`test_dashboard_active_report.py` gives: the view is a fragment, and a real session is all it
needs. `app_pages/task_builder.py` gets its own page test when it lands; this one is about the
view's own behaviour, so it stays honest even if the page around it is rearranged.

Two seams are stubbed, and only two — the ones between this view and a provider:
`sql_builder.generate_and_run` / `run_item`, and `column_intent.handle_message`. Everything
else is real widgets and real session state.

The behaviours worth the most here are the ones that would stay invisible until someone had
done real work: **distinct widget keys per item**, **a pin that lands exactly once no matter
how often an item is re-run**, **a column step that can never be pinned**, and **the deletion
rule** that keeps a step from being pulled out from under the items written against it.
"""

import pandas as pd
from streamlit.testing.v1 import AppTest

from app_pages import report_items_view
from report_items.model import KIND_COLUMN, KIND_REPORT

RESULT = pd.DataFrame(
    {
        "department": ["HR", "Accounts"],
        "people": [2, 1],
        "total_pay": [2000.0, 2000.0],
    }
)

STUB_SQL = "SELECT department, count(*) AS people, sum(basic) AS total_pay FROM salary GROUP BY department"


class _Change:
    """What `column_intent.handle_message` returns, without importing the real dataclass into
    every test that only cares about the three fields the view reads."""

    def __init__(
        self,
        statements,
        summary="Added `tax` to `salary`.",
        warnings=None,
        explanation="Ten percent of basic pay, as tax.",
    ):
        self.action = "add"
        self.statements = statements
        self.summary = summary
        self.warnings = warnings or []
        self.explanation = explanation


def _scenario(persona, report_key):
    """The whole app under test: the view, standing on its own.

    Everything it needs is imported inside it and passed as arguments, because
    `AppTest.from_function` re-executes the function's *source* in a script of its own — a
    closure over a name defined in this module would not be there to close over.

    `report_key` is what a host page declares once at the top of its run. None leaves the
    default, so the tests that don't care which report never have to say.
    """
    from app_pages import report_items_view as view
    from dashboard import session as dashboard_session

    if report_key is not None:
        dashboard_session.use_report(report_key)
    view.render_report_items(1, persona)


def _app(monkeypatch, persona: str = "", report_key: str | None = None) -> AppTest:
    # Stubbed here rather than in each test: every button that reaches a provider checks for a
    # connection first, and a view that always reported "no AI connection is selected" would
    # pass a lot of tests for the wrong reason.
    monkeypatch.setattr(
        report_items_view.llm_session, "active_profile", lambda user_id: {"nickname": "Local"}
    )
    app = AppTest.from_function(_scenario, args=(persona, report_key), default_timeout=60)
    app.run()
    assert not app.exception
    return app


def _add(app: AppTest, kind: str = KIND_REPORT, heading: str = "Payroll by department") -> AppTest:
    key = "ri_add_item" if kind == KIND_REPORT else "ri_add_column_step"
    app.button(key=key).click().run()
    app.text_input(key="ri_new_item_heading").set_value(heading).run()
    app.button(key="ri_new_item_add").click().run()
    assert not app.exception
    return app


def _with_schema(app: AppTest, monkeypatch) -> AppTest:
    """Puts a schema in front of the view, so the hint pickers have options to offer.

    The view stands on its own here with no upload behind it, and a `st.multiselect` refuses
    a value that isn't in its options — so without this, setting a hint would silently do
    nothing and the test would pass for the wrong reason.
    """
    options = report_items_view.SchemaOptions(
        ["salary"],
        ["salary.employee", "salary.basic"],
        frozenset({"salary"}),
        frozenset({"salary.employee", "salary.basic"}),
    )
    monkeypatch.setattr(
        report_items_view.SchemaOptions, "current", classmethod(lambda cls: options)
    )
    app.run()
    return app


def _items(app: AppTest):
    return app.session_state["ri_items"]


def _ids(app: AppTest):
    return [item.item_id for item in _items(app)]


def _stub_generate(monkeypatch, frame=RESULT, sql=STUB_SQL):
    monkeypatch.setattr(
        report_items_view.sql_builder, "generate_and_run", lambda *args, **kwargs: (sql, frame)
    )


def _report(app: AppTest, key: str = "db_report"):
    return app.session_state[key]


def _generated(monkeypatch, app: AppTest) -> tuple[AppTest, str]:
    """One report item with a stubbed result on screen — the state most tests start from."""
    _stub_generate(monkeypatch)
    item_id = _ids(app)[0]
    app.text_area(key=f"ri_request_{item_id}").set_value("Total pay per department").run()
    app.button(key=f"ri_generate_{item_id}").click().run()
    assert not app.exception
    return app, item_id


# --------------------------------------------------------------------------------------


class TestTheEmptyView:
    def test_it_offers_both_kinds_rather_than_one_control_that_hides_the_choice(self, monkeypatch):
        app = _app(monkeypatch)

        assert app.button(key="ri_add_item")
        assert app.button(key="ri_add_column_step")

    def test_it_says_what_to_do_instead_of_showing_an_empty_list(self, monkeypatch):
        app = _app(monkeypatch)

        assert any("Add a report item" in info.value for info in app.info)


class TestAddingItems:
    def test_a_report_item_gets_its_card(self, monkeypatch):
        app = _add(_app(monkeypatch))

        item_id = _ids(app)[0]
        assert _items(app)[0].kind == KIND_REPORT
        assert app.text_area(key=f"ri_request_{item_id}") is not None

    def test_a_column_step_gets_a_different_card(self, monkeypatch):
        """Applied, not queried — so Apply rather than Generate SQL. The hint pickers it
        *does* share with a report item are covered by `TestColumnSteps`."""
        app = _add(_app(monkeypatch), KIND_COLUMN, "Add tax")

        item_id = _ids(app)[0]
        assert _items(app)[0].kind == KIND_COLUMN
        assert app.button(key=f"ri_apply_{item_id}")
        assert not [button for button in app.button if button.key == f"ri_generate_{item_id}"]

    def test_items_are_appended_in_order(self, monkeypatch):
        """Never inserted: an item's position decides which column steps have run before it."""
        app = _add(_app(monkeypatch), heading="First")
        app = _add(app, KIND_COLUMN, "Second")
        app = _add(app, heading="Third")

        assert [item.heading for item in _items(app)] == ["First", "Second", "Third"]

    def test_two_items_get_their_own_widgets(self, monkeypatch):
        """The `settings.py` trap: a key shared between two items keeps showing whichever was
        created first, because `value=` is applied only when a key is created."""
        app = _add(_app(monkeypatch), heading="One")
        app = _add(app, heading="Two")

        first, second = _ids(app)
        app.text_area(key=f"ri_request_{first}").set_value("Request one").run()
        app.text_area(key=f"ri_request_{second}").set_value("Request two").run()

        assert [item.request for item in _items(app)] == ["Request one", "Request two"]

    def test_adding_without_a_heading_is_refused_rather_than_making_an_unnamed_item(self, monkeypatch):
        app = _app(monkeypatch)
        app.button(key="ri_add_item").click().run()

        assert app.button(key="ri_new_item_add").disabled
        assert "ri_items" not in app.session_state or _items(app) == []


class TestGeneratingAndRunning:
    def test_a_generated_statement_and_its_rows_are_recorded(self, monkeypatch):
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))

        assert _items(app)[0].sql == STUB_SQL
        assert app.session_state["ri_runtime"][item_id].frame.equals(RESULT)
        assert app.dataframe

    def test_generate_is_refused_until_there_is_a_request(self, monkeypatch):
        """Nothing is sent to a provider on an empty request — the SQL builder refuses it too,
        but the button saying so costs no round trip."""
        app = _add(_app(monkeypatch))

        assert app.button(key=f"ri_generate_{_ids(app)[0]}").disabled

    def test_once_there_is_sql_re_running_it_costs_no_provider_call(self, monkeypatch):
        """The whole point of storing the SQL: running last month's recipe against this
        month's file must not spend a model call per item."""
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))

        calls = []
        monkeypatch.setattr(
            report_items_view.sql_builder,
            "generate_and_run",
            lambda *args, **kwargs: calls.append(1) or (STUB_SQL, RESULT),
        )
        monkeypatch.setattr(report_items_view.sql_builder, "run_item", lambda *args: RESULT)

        app.button(key=f"ri_run_{item_id}").click().run()

        assert not app.exception
        assert calls == []

    def test_a_failure_is_shown_and_kept_for_the_next_refine(self, monkeypatch):
        from report_items.exceptions import ReportItemSqlError

        app = _add(_app(monkeypatch))
        item_id = _ids(app)[0]

        def fail(*args, **kwargs):
            raise ReportItemSqlError("That query could not run: no such column")

        monkeypatch.setattr(report_items_view.sql_builder, "generate_and_run", fail)
        app.text_area(key=f"ri_request_{item_id}").set_value("Something impossible").run()
        app.button(key=f"ri_generate_{item_id}").click().run()

        assert not app.exception
        assert any("no such column" in error.value for error in app.error)
        # On the item as well as on screen: this is what the next generation sends back.
        assert "no such column" in _items(app)[0].last_error

    def test_an_empty_result_is_reported_as_an_answer_not_an_error(self, monkeypatch):
        app = _add(_app(monkeypatch))
        item_id = _ids(app)[0]
        _stub_generate(monkeypatch, frame=RESULT.iloc[0:0])
        app.text_area(key=f"ri_request_{item_id}").set_value("Overdue invoices").run()
        app.button(key=f"ri_generate_{item_id}").click().run()

        assert not app.error
        assert any("may be the answer" in info.value for info in app.info)


class TestPinning:
    def test_pinning_puts_the_item_in_the_report(self, monkeypatch):
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))

        app.button(key=f"ri_pin_{item_id}").click().run()

        pool = _report(app).pool
        assert len(pool) == 1
        assert pool[0].source_id == f"item:{item_id}"
        assert pool[0].heading == "Payroll by department"
        assert pool[0].frame.equals(RESULT)

    def test_pinning_twice_updates_the_same_item_rather_than_adding_a_second(self, monkeypatch):
        """`pin_result` is idempotent on `source_id`, which is what makes re-running an item
        after refining it leave the report with one copy rather than a pile."""
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))
        app.button(key=f"ri_pin_{item_id}").click().run()

        app.button(key=f"ri_pin_{item_id}").click().run()

        assert len(_report(app).pool) == 1

    def test_the_button_changes_wording_once_it_is_pinned(self, monkeypatch):
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))
        app.button(key=f"ri_pin_{item_id}").click().run()

        assert app.button(key=f"ri_pin_{item_id}").label == "Update in report"
        assert app.button(key=f"ri_unpin_{item_id}")

    def test_removing_takes_it_out_and_releases_the_run(self, monkeypatch):
        """The saved run goes with the item: leaving it would keep the card claiming to be in
        a report it is no longer in."""
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))
        app.button(key=f"ri_pin_{item_id}").click().run()

        app.button(key=f"ri_unpin_{item_id}").click().run()

        assert _report(app).pool == []
        assert _items(app)[0].saved_run is None
        assert _items(app)[0].sql == STUB_SQL

    def test_it_pins_into_whichever_report_the_page_declared(self, monkeypatch):
        """The failure this guards against is silent: a Task's item landing on the session
        Dashboard, or a chat answer landing in a Task."""
        app = _app(monkeypatch, report_key="tb_report")
        app, item_id = _generated(monkeypatch, _add(app))

        app.button(key=f"ri_pin_{item_id}").click().run()

        assert len(_report(app, "tb_report").pool) == 1
        assert "db_report" not in app.session_state


class TestComments:
    def test_the_comment_is_drafted_on_a_button_not_on_every_keystroke(self, monkeypatch):
        """This view re-executes on every keystroke anywhere on the page, so an automatic
        draft would be one provider request per character typed."""
        calls = []
        monkeypatch.setattr(
            report_items_view.commentary,
            "write_commentary",
            lambda *args, **kwargs: calls.append(kwargs) or ("HR is the larger department.", []),
        )
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))

        assert calls == []
        app.button(key=f"ri_comment_write_{item_id}").click().run()

        assert _items(app)[0].comment == "HR is the larger department."
        assert app.text_area(key=f"ri_comment_{item_id}").value == "HR is the larger department."

    def test_the_task_persona_reaches_the_comment_as_its_domain_rules(self, monkeypatch):
        """Requirement 7.2: one persona per Task, applied wherever wording is generated."""
        calls = []
        monkeypatch.setattr(
            report_items_view.commentary,
            "write_commentary",
            lambda *args, **kwargs: calls.append(kwargs) or ("Noted.", []),
        )
        app = _app(monkeypatch, persona="You are a finance controller.")
        app, item_id = _generated(monkeypatch, _add(app))
        app.button(key=f"ri_comment_write_{item_id}").click().run()

        assert calls[0]["knowledge_base"] == "You are a finance controller."

    def test_the_pinned_copy_carries_the_comment(self, monkeypatch):
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))
        app.text_area(key=f"ri_comment_{item_id}").set_value("Written by hand.").run()

        app.button(key=f"ri_pin_{item_id}").click().run()

        assert _report(app).pool[0].comment == "Written by hand."


class TestColumnSteps:
    def _applied(self, monkeypatch, app=None, statements=None):
        app = app or _add(_app(monkeypatch), KIND_COLUMN, "Add tax")
        item_id = _ids(app)[-1]
        monkeypatch.setattr(
            report_items_view.column_intent,
            "handle_message",
            lambda *args, **kwargs: _Change(
                statements
                or ["ALTER TABLE salary ADD COLUMN tax DOUBLE", "UPDATE salary SET tax = basic * 0.10"]
            ),
        )
        app.text_area(key=f"ri_request_{item_id}").set_value("Add tax = 10% of basic").run()
        app.button(key=f"ri_apply_{item_id}").click().run()
        assert not app.exception
        return app, item_id

    def test_applying_records_the_statements_on_the_step(self, monkeypatch):
        app, item_id = self._applied(monkeypatch)

        step = _items(app)[0]
        assert step.applied is True
        assert step.statements[0].startswith("ALTER TABLE salary")
        assert step.summary == "Added `tax` to `salary`."

    def test_the_statements_also_join_the_engines_ordered_list(self, monkeypatch):
        """That list is what requirement 8.2 replays, and it must not be able to disagree
        with the step that produced it."""
        app, _ = self._applied(monkeypatch)

        from engine import session as engine_session

        assert app.session_state[engine_session.DE_STATEMENTS_KEY] == [
            "ALTER TABLE salary ADD COLUMN tax DOUBLE",
            "UPDATE salary SET tax = basic * 0.10",
        ]

    def test_an_applied_step_cannot_be_applied_twice(self, monkeypatch):
        """Re-running an `ALTER TABLE … ADD COLUMN` on a column that already exists is an
        error, not an idempotent no-op."""
        app, item_id = self._applied(monkeypatch)

        assert not [button for button in app.button if button.key == f"ri_apply_{item_id}"]
        assert app.text_area(key=f"ri_request_{item_id}").disabled

    def test_a_column_step_is_never_pinnable(self, monkeypatch):
        """The distinction the two kinds exist for: a step's output is the changed data, so
        there is nothing to put in a report."""
        app, item_id = self._applied(monkeypatch)

        assert not [button for button in app.button if button.key == f"ri_pin_{item_id}"]
        assert "db_report" not in app.session_state

    def test_a_column_step_offers_the_same_hint_pickers_as_everything_else(self, monkeypatch):
        """Every other AI-driven input in the app lets the user point at what it touches.
        The column step, added last, was the one that never got them."""
        app = _add(_app(monkeypatch), KIND_COLUMN, "Add tax")
        item_id = _ids(app)[0]

        assert app.multiselect(key=f"ri_hint_tables_{item_id}") is not None
        assert app.multiselect(key=f"ri_hint_columns_{item_id}") is not None

    def test_the_hints_reach_the_prompt_as_a_hint(self, monkeypatch):
        """A hint, never a constraint — the model also has the real schema, and the wording
        is `checks.sql_builder.build_prompt`'s so the two paths read alike to a model."""
        app = _with_schema(_add(_app(monkeypatch), KIND_COLUMN, "Add tax"), monkeypatch)
        item_id = _ids(app)[0]
        captured = {}

        def record(*args, **kwargs):
            captured.update(kwargs)
            return _Change(["ALTER TABLE salary ADD COLUMN tax DOUBLE"])

        monkeypatch.setattr(report_items_view.column_intent, "handle_message", record)
        app.multiselect(key=f"ri_hint_tables_{item_id}").set_value(["salary"]).run()
        app.multiselect(key=f"ri_hint_columns_{item_id}").set_value(["salary.basic"]).run()
        app.text_area(key=f"ri_request_{item_id}").set_value("Add tax = 10% of basic").run()
        app.button(key=f"ri_apply_{item_id}").click().run()

        assert captured["column_hint"] == "tables: salary; columns: salary.basic"

    def test_the_hints_freeze_with_the_request_once_applied(self, monkeypatch):
        """A live picker over a change already made would suggest it could still steer it."""
        app, item_id = self._applied(monkeypatch)

        assert app.multiselect(key=f"ri_hint_tables_{item_id}").disabled
        assert app.multiselect(key=f"ri_hint_columns_{item_id}").disabled

    def test_an_applied_step_shows_the_ai_description_and_offers_the_data(self, monkeypatch):
        """A step's output *is* the data, which lives on Setup — so it says where it went,
        and offers a look without leaving the list."""
        app, item_id = self._applied(monkeypatch)

        assert _items(app)[0].description == "Ten percent of basic pay, as tax."
        assert any("Ten percent of basic" in caption.value for caption in app.caption)
        assert app.button(key=f"ri_view_data_{item_id}")

    def test_a_refused_change_is_reported_and_the_step_stays_unapplied(self, monkeypatch):
        from analyst.exceptions import AnalystError

        app = _add(_app(monkeypatch), KIND_COLUMN, "Add tax")
        item_id = _ids(app)[0]

        def refuse(*args, **kwargs):
            raise AnalystError("I couldn't tell which table and column you meant.")

        monkeypatch.setattr(report_items_view.column_intent, "handle_message", refuse)
        app.text_area(key=f"ri_request_{item_id}").set_value("do the thing").run()
        app.button(key=f"ri_apply_{item_id}").click().run()

        assert not app.exception
        assert any("which table and column" in error.value for error in app.error)
        assert _items(app)[0].applied is False
        assert app.button(key=f"ri_apply_{item_id}")


class TestDeletion:
    def test_a_report_item_can_always_go_and_takes_its_pinned_copy_with_it(self, monkeypatch):
        app, item_id = _generated(monkeypatch, _add(_app(monkeypatch)))
        app.button(key=f"ri_pin_{item_id}").click().run()

        app.button(key=f"ri_delete_{item_id}").click().run()

        assert _items(app) == []
        assert _report(app).pool == []

    def test_the_last_column_step_can_go(self, monkeypatch):
        app = _add(_app(monkeypatch), heading="An item")
        app = _add(app, KIND_COLUMN, "Add tax")
        step_id = _ids(app)[1]

        app.button(key=f"ri_delete_{step_id}").click().run()

        assert [item.heading for item in _items(app)] == ["An item"]

    def test_a_column_step_with_anything_under_it_is_refused_with_a_reason(self, monkeypatch):
        """Everything below a step was written against the columns it added. Removing it here
        would break them all — and the failure would surface next month, in a report."""
        app = _add(_app(monkeypatch), KIND_COLUMN, "Add tax")
        app = _add(app, heading="Tax by department")
        step_id = _ids(app)[0]

        app.button(key=f"ri_delete_{step_id}").click().run()

        assert not app.exception
        assert any("last column step" in error.value for error in app.error)
        assert len(_items(app)) == 2

    def test_removing_what_is_under_it_frees_the_step(self, monkeypatch):
        app = _add(_app(monkeypatch), KIND_COLUMN, "Add tax")
        app = _add(app, heading="Tax by department")
        step_id, later_id = _ids(app)

        app.button(key=f"ri_delete_{later_id}").click().run()
        app.button(key=f"ri_delete_{step_id}").click().run()

        assert _items(app) == []
