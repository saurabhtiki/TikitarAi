"""AppTest coverage for the Checks view (requirement 6.5).

Driven through the Chat page, because that is where the view lives — the third option on
`de_view`. Everything is exercised through real widgets, and the only thing stubbed is
`sql_builder.generate_and_run`, which is the single seam between this page and a provider.

The two behaviours worth the most here are the ones that would be invisible until a user
had done real work: **distinct widget keys per criteria**, and **Save reaching the
Dashboard exactly once** no matter how many times a rule is refined.
"""

from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_pages import checks_view
from auth.db import init_db, seed_default_admin
from checks import db as checks_db
from checks import session as checks_session
from checks.exceptions import CheckSqlError
from dashboard import session as dashboard_session
from engine import session as engine_session
from llm.db import create_profile, init_llm_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "chat_with_data.py")

SALARY_CSV = b"employee,department,basic,bonus\nAna,HR,1000,40\nBo,HR,1000,120\nCy,Accounts,2000,90\n"

RESULT = pd.DataFrame(
    {
        "employee": ["Ana", "Bo", "Cy"],
        "criteria_result": [4.0, 12.0, 4.5],
        "criteria_met": ["Yes", "No", "Yes"],
    }
)

STUB_SQL = "SELECT employee, bonus / basic * 100 AS criteria_result, 'Yes' AS criteria_met FROM salary"

# The same three records, with the whole source row the widened instruction now asks for.
WIDE_RESULT = pd.DataFrame(
    {
        "employee": ["Ana", "Bo", "Cy"],
        "department": ["HR", "HR", "Accounts"],
        "basic": [1000, 1000, 2000],
        "criteria_result": [4.0, 12.0, 4.5],
        "criteria_met": ["Yes", "No", "Yes"],
    }
)


def _make_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    checks_db.init_check_sets_table()
    # The page needs a selectable connection before it will call a model at all.
    create_profile(1, "Local", "local", "http://localhost:1234", None, "llama-3")

    app = AppTest.from_file(PAGE_PATH, default_timeout=90)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.run()
    return app


def _loaded(tmp_path, monkeypatch):
    """A session with one table loaded and the Checks view open."""
    app = _make_app(tmp_path, monkeypatch)
    app.session_state[engine_session.STEP_UPLOAD] = True
    app.run()
    app.file_uploader(key=engine_session.DE_UPLOADER_KEY).set_value([("salary.csv", SALARY_CSV, "text/csv")])
    app.run()
    app.session_state["de_view"] = "Checks"
    app.run()
    return app


def _add_criteria(app, name):
    app.button(key="ck_add_check").click().run()
    app.text_input(key="ck_new_check_name").set_value(name).run()
    app.button(key="ck_new_check_add").click().run()
    return app


def _check_ids(app):
    return [check.check_id for check in app.session_state[checks_session.CK_SET_KEY].checks]


def _stub_test(monkeypatch, frame=RESULT, sql=STUB_SQL, identity_columns=("employee",)):
    monkeypatch.setattr(
        checks_view.sql_builder,
        "generate_and_run",
        lambda *args, **kwargs: (sql, frame, list(identity_columns)),
    )


class TestTheView:
    def test_checks_is_offered_beside_chat(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert app.segmented_control(key="de_view").options == ["Setup", "Chat", "Checks"]

    def test_without_data_it_asks_for_a_file_rather_than_showing_an_empty_form(self, tmp_path, monkeypatch):
        """Every criteria is written against the loaded schema, so an empty session has
        nothing to write a rule about."""
        app = _make_app(tmp_path, monkeypatch)
        app.session_state["de_view"] = "Checks"
        app.run()
        assert not app.exception
        assert any("Upload your data first" in info.value for info in app.info)
        assert not [button for button in app.button if button.key == "ck_add_check"]

    def test_with_data_it_offers_the_design_form(self, tmp_path, monkeypatch):
        app = _loaded(tmp_path, monkeypatch)
        assert not app.exception
        assert app.text_area(key="ck_persona")
        assert app.button(key="ck_add_check")


class TestCriteria:
    def test_adding_one_creates_its_expander(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        assert not app.exception
        check_id = _check_ids(app)[0]
        assert app.text_area(key=f"ck_text_{check_id}") is not None

    def test_two_criteria_get_their_own_widgets(self, tmp_path, monkeypatch):
        """The `settings.py:210` trap: a key shared between two criteria keeps showing
        whichever was created first, because `value=` is applied only on creation."""
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        app = _add_criteria(app, "PF mismatch")

        first, second = _check_ids(app)
        app.text_area(key=f"ck_text_{first}").set_value("Rule one").run()
        app.text_area(key=f"ck_text_{second}").set_value("Rule two").run()

        checks = app.session_state[checks_session.CK_SET_KEY].checks
        assert [check.criteria_text for check in checks] == ["Rule one", "Rule two"]

    def test_deleting_one_leaves_the_other(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        app = _add_criteria(app, "PF mismatch")
        first = _check_ids(app)[0]

        app.button(key=f"ck_delete_{first}").click().run()

        assert [check.name for check in app.session_state[checks_session.CK_SET_KEY].checks] == ["PF mismatch"]


class TestTestingAndSaving:
    def _tested(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        return app, check_id

    def test_a_successful_test_shows_the_rows_and_the_headline(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        assert not app.exception
        assert app.session_state[checks_session.CK_RUNTIME_KEY][check_id].frame is not None
        assert any("1** of **3" in markdown.value for markdown in app.markdown)

    def test_a_failure_is_shown_and_kept_for_the_next_refine(self, tmp_path, monkeypatch):
        """The error is what the refine loop feeds back to the model, so it has to survive
        the rerun that follows the press."""
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Something impossible.").run()

        def boom(*args, **kwargs):
            raise CheckSqlError("The result is missing criteria_met.")

        monkeypatch.setattr(checks_view.sql_builder, "generate_and_run", boom)
        app.button(key=f"ck_test_{check_id}").click().run()

        assert not app.exception
        assert any("missing criteria_met" in error.value for error in app.error)
        check = app.session_state[checks_session.CK_SET_KEY].checks[0]
        assert check.last_error == "The result is missing criteria_met."

    def test_nothing_reaches_the_report_until_save_is_pressed(self, tmp_path, monkeypatch):
        app, _ = self._tested(tmp_path, monkeypatch)
        assert dashboard_session.DB_REPORT_KEY not in app.session_state or not (
            app.session_state[dashboard_session.DB_REPORT_KEY].pool
        )

    def test_no_criteria_draws_a_chart_until_one_is_asked_for(self, tmp_path, monkeypatch):
        """An *automatic* chart per criteria is one rule's rows drawn again under the table
        they came from, for every rule down the page. A chart the user asks for and builds
        is a different object, so the button is offered and nothing is drawn until it."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        assert app.session_state[checks_session.CK_SET_KEY].checks[0].chart is None
        assert not [box for box in app.selectbox if box.key == f"ck_chart_kind_{check_id}"]
        assert app.button(key=f"ck_chart_new_{check_id}")

    def test_saving_pins_it_with_the_criteria_name_as_its_heading(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("- One breach.", []))

        app.button(key=f"ck_save_{check_id}").click().run()

        assert not app.exception
        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        assert len(pool) == 1
        assert pool[0].display_heading() == "Bonus cap"
        assert pool[0].comment == "- One breach."
        assert len(pool[0].frame) == 3

    def test_saving_twice_after_a_refine_updates_the_one_item(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", []))
        app.button(key=f"ck_save_{check_id}").click().run()

        refined = RESULT.head(1)
        _stub_test(monkeypatch, frame=refined)
        app.button(key=f"ck_test_{check_id}").click().run()
        app.button(key=f"ck_save_{check_id}").click().run()

        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        assert len(pool) == 1
        assert len(pool[0].frame) == 1

    def test_a_provider_hiccup_on_the_remarks_still_saves_the_run(self, tmp_path, monkeypatch):
        """The remarks are one part of the item; losing them must not cost the user the
        run they just pressed Save on."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        monkeypatch.setattr(
            checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", ["Couldn't write the remarks."])
        )

        app.button(key=f"ck_save_{check_id}").click().run()

        assert len(app.session_state[dashboard_session.DB_REPORT_KEY].pool) == 1
        assert any("Couldn't write the remarks" in warning.value for warning in app.warning)


class TestReusingSavedSql:
    """Requirement 6.5's recipe half: re-running a set costs no model calls.

    The SQL is the part `to_json` stores, precisely because it is what can be run again next
    month. Regenerating it on every run would spend a provider call per criteria to replace a
    query the user has already tuned — and could come back shaped differently.
    """

    def _tested(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        return app, check_id

    def _no_provider(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("should not have called the provider")

        monkeypatch.setattr(checks_view.sql_builder, "generate_and_run", boom)

    def test_a_criteria_with_no_sql_yet_offers_only_generation(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()

        assert not [button for button in app.button if button.key == f"ck_run_{check_id}"]
        assert app.button(key=f"ck_test_{check_id}").label == "Generate & test SQL"

    def test_a_tested_criteria_runs_again_without_a_model(self, tmp_path, monkeypatch):
        """The real DuckDB connection runs the stored statement — nothing is stubbed but the
        generation that must not happen."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        self._no_provider(monkeypatch)

        app.button(key=f"ck_run_{check_id}").click().run()

        assert not app.exception
        frame = app.session_state[checks_session.CK_RUNTIME_KEY][check_id].frame
        assert list(frame["employee"]) == ["Ana", "Bo", "Cy"]

    def test_a_query_that_no_longer_fits_says_so_rather_than_regenerating(self, tmp_path, monkeypatch):
        """A renamed table is the user's call to make, not a reason to spend a model call on
        a query they may want to fix by hand."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.session_state[checks_session.CK_SET_KEY].checks[0].sql = "SELECT * FROM last_years_table"
        app.run()
        self._no_provider(monkeypatch)

        app.button(key=f"ck_run_{check_id}").click().run()

        assert not app.exception
        assert any("could not run" in error.value for error in app.error)
        # Kept on the check, so pressing Regenerate next tells the model what went wrong.
        assert "last_years_table" in app.session_state[checks_session.CK_SET_KEY].checks[0].last_error
        assert app.session_state[checks_session.CK_RUNTIME_KEY][check_id].frame is None

    def test_regenerating_is_still_one_press_away(self, tmp_path, monkeypatch):
        """Step 4's refine loop is unchanged — it just isn't what pressing Run does."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        _stub_test(monkeypatch, frame=RESULT.head(1), sql="SELECT 1 AS criteria_result")

        app.button(key=f"ck_test_{check_id}").click().run()

        assert not app.exception
        assert len(app.session_state[checks_session.CK_RUNTIME_KEY][check_id].frame) == 1
        assert app.session_state[checks_session.CK_SET_KEY].checks[0].sql == "SELECT 1 AS criteria_result"


class TestWhatGetsPinned:
    """Requirement 6.5: the report shows the table the user was looking at.

    `RESULT` is three records — Ana and Cy pass, Bo breaches — so each filter pins a
    different number of rows while the run behind it stays the same three.
    """

    def _tested(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", []))
        return app, check_id

    def _pool(self, app):
        return app.session_state[dashboard_session.DB_REPORT_KEY].pool

    def test_the_failures_view_pins_only_the_failures(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.segmented_control(key=f"ck_filter_{check_id}").set_value("Failures").run()
        app.button(key=f"ck_save_{check_id}").click().run()

        assert not app.exception
        assert len(self._pool(app)[0].frame) == 1
        assert list(self._pool(app)[0].frame["employee"]) == ["Bo"]

    def test_a_partial_table_says_so_in_its_heading(self, tmp_path, monkeypatch):
        """The remarks printed above it quote the whole run's counts, so a table of one row
        under an unqualified heading would read as the entire result."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.segmented_control(key=f"ck_filter_{check_id}").set_value("Failures").run()
        app.button(key=f"ck_save_{check_id}").click().run()

        assert self._pool(app)[0].display_heading() == "Bonus cap — breaches only"

    def test_the_all_view_pins_everything_and_needs_no_qualifier(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.button(key=f"ck_save_{check_id}").click().run()

        assert len(self._pool(app)[0].frame) == 3
        assert self._pool(app)[0].display_heading() == "Bonus cap"

    def test_the_saved_run_keeps_every_row_whatever_is_filtered(self, tmp_path, monkeypatch):
        """The counts, the remarks prompt and every action draft come off the saved run. If
        the filter reached it, saving on "Passes" would report zero breaches and leave the
        Actions tab with nothing to write about."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.segmented_control(key=f"ck_filter_{check_id}").set_value("Passes").run()
        app.button(key=f"ck_save_{check_id}").click().run()

        run = app.session_state[checks_session.CK_SET_KEY].checks[0].saved_run
        assert run.row_count == 3
        assert (run.pass_count, run.fail_count) == (2, 1)
        # ...while the report shows what was on screen.
        assert len(self._pool(app)[0].frame) == 2

    def test_changing_the_filter_and_saving_again_replaces_the_one_item(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.button(key=f"ck_save_{check_id}").click().run()
        app.segmented_control(key=f"ck_filter_{check_id}").set_value("Failures").run()
        app.button(key=f"ck_save_{check_id}").click().run()

        assert len(self._pool(app)) == 1
        assert len(self._pool(app)[0].frame) == 1


class TestChoosingColumns:
    """The generated SQL now returns the whole source row; this is how much of it is shown.

    `WIDE_RESULT` carries three source columns where the model named only one as identifying,
    so the default and a widened selection are visibly different.
    """

    def _tested(self, tmp_path, monkeypatch, identity=("employee",)):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch, frame=WIDE_RESULT, identity_columns=identity)
        app.button(key=f"ck_test_{check_id}").click().run()
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", []))
        assert not app.exception
        return app, check_id

    def _shown(self, app, check_id):
        """The result table on screen. Found by its contract columns rather than by key —
        `AppTest`'s dataframe elements do not expose one."""
        rendered = [
            frame.value for frame in app.dataframe if "criteria_result" in frame.value.columns
        ]
        assert len(rendered) == 1
        return rendered[0]

    def test_the_default_is_what_the_model_called_identifying(self, tmp_path, monkeypatch):
        """Which is what keeps every criteria written before the picker existed looking
        exactly as it did."""
        app, check_id = self._tested(tmp_path, monkeypatch)

        assert app.session_state[checks_session.CK_SET_KEY].checks[0].display_columns == ["employee"]
        assert list(self._shown(app, check_id).columns) == [
            "employee",
            "criteria_result",
            "criteria_met",
        ]

    def test_the_contract_columns_are_never_offered_and_always_shown(self, tmp_path, monkeypatch):
        """They are the criteria's answer, not one of its details."""
        app, check_id = self._tested(tmp_path, monkeypatch)

        picker = app.multiselect(key=f"ck_display_columns_{check_id}")
        assert picker.options == ["employee", "department", "basic"]

        picker.set_value([]).run()

        assert list(self._shown(app, check_id).columns) == ["criteria_result", "criteria_met"]

    def test_widening_the_selection_widens_the_table(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)

        app.multiselect(key=f"ck_display_columns_{check_id}").set_value(
            ["employee", "department"]
        ).run()

        assert list(self._shown(app, check_id).columns) == [
            "employee",
            "department",
            "criteria_result",
            "criteria_met",
        ]

    def test_the_selection_reaches_the_report_but_not_the_saved_run(self, tmp_path, monkeypatch):
        """The rule Save already followed for rows, now holding for columns too: the report
        gets what is on screen, while the counts and the remarks read the whole run."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.multiselect(key=f"ck_display_columns_{check_id}").set_value(
            ["employee", "basic"]
        ).run()

        app.button(key=f"ck_save_{check_id}").click().run()

        assert not app.exception
        pinned = app.session_state[dashboard_session.DB_REPORT_KEY].pool[0]
        assert list(pinned.frame.columns) == [
            "employee",
            "basic",
            "criteria_result",
            "criteria_met",
        ]

        run = app.session_state[checks_session.CK_SET_KEY].checks[0].saved_run
        assert list(run.frame.columns) == list(WIDE_RESULT.columns)
        assert (run.pass_count, run.fail_count) == (2, 1)

    def test_the_column_and_row_filters_compose(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.multiselect(key=f"ck_display_columns_{check_id}").set_value(["employee"]).run()
        app.segmented_control(key=f"ck_filter_{check_id}").set_value("Failures").run()

        shown = self._shown(app, check_id)

        assert list(shown["employee"]) == ["Bo"]
        assert list(shown.columns) == ["employee", "criteria_result", "criteria_met"]

    def test_a_criteria_the_model_gave_no_identity_for_shows_everything(self, tmp_path, monkeypatch):
        """Better wide than blank: a result nobody can attribute is the failure this seeding
        exists to avoid, not one to reproduce."""
        app, check_id = self._tested(tmp_path, monkeypatch, identity=())

        assert app.session_state[checks_session.CK_SET_KEY].checks[0].display_columns == [
            "employee",
            "department",
            "basic",
        ]

    def test_a_chosen_selection_survives_a_regeneration(self, tmp_path, monkeypatch):
        """Seeding is for a criteria that has never been shown. Once the user has chosen,
        pressing Regenerate must not quietly reset what they picked."""
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.multiselect(key=f"ck_display_columns_{check_id}").set_value(["department"]).run()

        app.button(key=f"ck_test_{check_id}").click().run()

        assert app.session_state[checks_session.CK_SET_KEY].checks[0].display_columns == [
            "department"
        ]


class TestRemovingFromTheReport:
    """The Dashboard no longer discards what a criteria owns, so removal lives here."""

    def _saved(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", []))
        app.button(key=f"ck_save_{check_id}").click().run()
        return app, check_id

    def test_the_button_appears_only_once_something_is_saved(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5%.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()

        assert not [button for button in app.button if button.key == f"ck_unsave_{check_id}"]

    def test_it_takes_the_item_out_and_releases_the_run(self, tmp_path, monkeypatch):
        """The run goes with the item: its presence is what lists this criteria in the
        Actions tab, and offering follow-ups for a result the user has just unreported would
        draft emails about a report nobody is getting."""
        app, check_id = self._saved(tmp_path, monkeypatch)
        app.button(key=f"ck_unsave_{check_id}").click().run()

        assert not app.exception
        assert app.session_state[dashboard_session.DB_REPORT_KEY].pool == []
        assert app.session_state[checks_session.CK_SET_KEY].checks[0].saved_run is None
        # The rule itself survives — Test then Save puts it straight back.
        assert app.session_state[checks_session.CK_SET_KEY].checks[0].sql == STUB_SQL

    def test_deleting_the_criteria_takes_its_report_item_with_it(self, tmp_path, monkeypatch):
        """Nothing else can remove it now that the Dashboard won't: a tile left behind here
        would be undeletable short of Start over."""
        app, check_id = self._saved(tmp_path, monkeypatch)
        app.button(key=f"ck_delete_{check_id}").click().run()

        assert not app.exception
        assert app.session_state[dashboard_session.DB_REPORT_KEY].pool == []

    def test_deleting_it_also_takes_its_confirmed_follow_ups(self, tmp_path, monkeypatch):
        app, check_id = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(checks_view.checks_actions, "draft_action", lambda *a, **k: (a[3], []))
        app.button(key=f"ck_add_action_{check_id}").click().run()
        app.button(key=f"ck_new_action_add_{check_id}").click().run()
        action_id = app.session_state[checks_session.CK_SET_KEY].checks[0].actions[0].action_id
        app.button(key=f"ck_action_confirm_{check_id}_{action_id}").click().run()
        assert len(app.session_state[dashboard_session.DB_REPORT_KEY].pool) == 2

        app.button(key=f"ck_delete_{check_id}").click().run()
        assert app.session_state[dashboard_session.DB_REPORT_KEY].pool == []


class TestSummary:
    """The set-level picture at the foot of the Design tab."""

    def _saved(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", []))
        app.button(key=f"ck_save_{check_id}").click().run()
        return app, check_id

    def test_it_waits_for_something_to_summarise(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        assert not app.exception
        assert any("joins this summary" in info.value for info in app.info)
        assert not app.get("plotly_chart")

    def test_a_saved_criteria_draws_both_charts(self, tmp_path, monkeypatch):
        """Counts and shares answer different questions — how many records breached, and how
        badly each rule is breached — so both are drawn."""
        app, _ = self._saved(tmp_path, monkeypatch)

        assert not app.exception
        assert len(app.get("plotly_chart")) == 2
        assert any("1** breach(es) across **3** record(s)" in md.value for md in app.markdown)

    def test_only_saved_criteria_are_counted(self, tmp_path, monkeypatch):
        """An unsaved criteria is not in the report, and a bar for it would promise an item
        that isn't there."""
        app, _ = self._saved(tmp_path, monkeypatch)
        app = _add_criteria(app, "PF mismatch")

        assert any("1 criteria not counted here" in caption.value for caption in app.caption)
        assert len(app.get("plotly_chart")) == 2

    def test_the_written_summary_lands_in_the_box(self, tmp_path, monkeypatch):
        """The box already exists by then, so `value=` can no longer reach it — the draft has
        to be written to the widget's own session key or it never appears."""
        app, _ = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(
            checks_view.checks_remarks, "write_set_summary", lambda *a, **k: ("- One rule, one breach.", [])
        )

        app.button(key="ck_summary_write").click().run()

        assert not app.exception
        assert app.session_state[checks_session.CK_SET_KEY].summary == "- One rule, one breach."
        assert app.text_area(key=checks_session.CK_SUMMARY_TEXT_KEY).value == "- One rule, one breach."

    def test_a_provider_hiccup_leaves_the_box_for_the_user(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(
            checks_view.checks_remarks,
            "write_set_summary",
            lambda *a, **k: ("", ["Couldn't write the summary: no answer."]),
        )

        app.button(key="ck_summary_write").click().run()

        assert not app.exception
        assert any("Couldn't write the summary" in warning.value for warning in app.warning)
        assert app.text_area(key=checks_session.CK_SUMMARY_TEXT_KEY) is not None

    def test_the_summary_is_not_in_the_report_until_saved(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)
        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        assert [item.source_id for item in pool] == [checks_session.source_id_for(_check_ids(app)[0])]

    def test_saving_it_pins_the_chart_and_the_counts(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(checks_view.checks_remarks, "write_set_summary", lambda *a, **k: ("- All good.", []))
        app.button(key="ck_summary_write").click().run()
        app.button(key="ck_summary_save").click().run()

        assert not app.exception
        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        item = next(one for one in pool if one.source_id == checks_session.SUMMARY_SOURCE_ID)
        assert item.display_heading().endswith("— overview")
        assert item.comment == "- All good."
        # Both panels, not just the counts: an item holds one figure, and a report showing
        # absolute counts alone can't say whether 1 breach of 3 is worse than 1 of 90,000.
        assert item.has_chart()
        assert [trace.xaxis for trace in item.figure.data] == ["x", "x", "x2", "x2"]
        # The counts travel as a table too, so the Excel export carries numbers rather than
        # a picture of them.
        assert list(item.frame["criteria"]) == ["Bonus cap"]

    def test_saving_twice_keeps_one_overview(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)
        app.button(key="ck_summary_save").click().run()
        app.button(key="ck_summary_save").click().run()

        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        assert len([item for item in pool if item.source_id == checks_session.SUMMARY_SOURCE_ID]) == 1

    def test_it_can_be_taken_back_out(self, tmp_path, monkeypatch):
        app, _ = self._saved(tmp_path, monkeypatch)
        app.button(key="ck_summary_save").click().run()
        app.button(key="ck_summary_unsave").click().run()

        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        assert not [item for item in pool if item.source_id == checks_session.SUMMARY_SOURCE_ID]


class TestActions:
    def _saved(self, tmp_path, monkeypatch):
        """A session with one criteria tested and saved, ready for a follow-up."""
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", []))
        app.button(key=f"ck_save_{check_id}").click().run()
        return app, check_id

    def test_without_a_saved_criteria_it_says_what_to_do_first(self, tmp_path, monkeypatch):
        app = _loaded(tmp_path, monkeypatch)
        assert any("Save a criteria's results" in info.value for info in app.info)

    def test_a_saved_criteria_offers_a_follow_up(self, tmp_path, monkeypatch):
        app, check_id = self._saved(tmp_path, monkeypatch)
        assert not app.exception
        assert app.button(key=f"ck_add_action_{check_id}")

    def test_drafting_one_fills_its_wording(self, tmp_path, monkeypatch):
        app, check_id = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(
            checks_view.checks_actions,
            "draft_action",
            lambda profile, persona, check, draft, **kwargs: (
                setattr(draft, "body", "Bo received 12% of basic.") or (draft, [])
            ),
        )

        app.button(key=f"ck_add_action_{check_id}").click().run()
        app.text_input(key=f"ck_new_action_to_{check_id}").set_value("hr@example.com").run()
        app.button(key=f"ck_new_action_add_{check_id}").click().run()

        assert not app.exception
        check = app.session_state[checks_session.CK_SET_KEY].checks[0]
        assert len(check.actions) == 1
        assert check.actions[0].body == "Bo received 12% of basic."
        assert check.actions[0].recipients["to"] == ["hr@example.com"]

    def test_a_draft_is_not_in_the_report_until_it_is_confirmed(self, tmp_path, monkeypatch):
        app, check_id = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(
            checks_view.checks_actions, "draft_action", lambda *a, **k: (a[3], [])
        )
        app.button(key=f"ck_add_action_{check_id}").click().run()
        app.button(key=f"ck_new_action_add_{check_id}").click().run()

        # The criteria's own result item, and nothing else.
        assert len(app.session_state[dashboard_session.DB_REPORT_KEY].pool) == 1

        action_id = app.session_state[checks_session.CK_SET_KEY].checks[0].actions[0].action_id
        app.button(key=f"ck_action_confirm_{check_id}_{action_id}").click().run()

        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        assert len(pool) == 2
        assert any("follow-up actions" in item.display_heading() for item in pool)

    def test_unconfirming_takes_it_back_out(self, tmp_path, monkeypatch):
        """Otherwise the report keeps printing a follow-up the user has changed their mind
        about, with no way to remove it short of deleting the tile by hand."""
        app, check_id = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(checks_view.checks_actions, "draft_action", lambda *a, **k: (a[3], []))
        app.button(key=f"ck_add_action_{check_id}").click().run()
        app.button(key=f"ck_new_action_add_{check_id}").click().run()

        action_id = app.session_state[checks_session.CK_SET_KEY].checks[0].actions[0].action_id
        app.button(key=f"ck_action_confirm_{check_id}_{action_id}").click().run()
        app.button(key=f"ck_action_confirm_{check_id}_{action_id}").click().run()

        assert len(app.session_state[dashboard_session.DB_REPORT_KEY].pool) == 1

    def test_deleting_a_confirmed_draft_removes_it_from_the_report_too(self, tmp_path, monkeypatch):
        app, check_id = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(checks_view.checks_actions, "draft_action", lambda *a, **k: (a[3], []))
        app.button(key=f"ck_add_action_{check_id}").click().run()
        app.button(key=f"ck_new_action_add_{check_id}").click().run()

        action_id = app.session_state[checks_session.CK_SET_KEY].checks[0].actions[0].action_id
        app.button(key=f"ck_action_confirm_{check_id}_{action_id}").click().run()
        app.button(key=f"ck_action_delete_{check_id}_{action_id}").click().run()

        assert app.session_state[checks_session.CK_SET_KEY].checks[0].actions == []
        assert len(app.session_state[dashboard_session.DB_REPORT_KEY].pool) == 1

    def test_a_meeting_draft_renders_and_offers_its_file(self, tmp_path, monkeypatch):
        """A meeting takes the `.ics` branch of the download button, which builds a real
        calendar file at render time — so a broken one is an exception on this page, not a
        surprise when someone clicks."""
        app, check_id = self._saved(tmp_path, monkeypatch)
        monkeypatch.setattr(checks_view.checks_actions, "draft_action", lambda *a, **k: (a[3], []))
        app.button(key=f"ck_add_action_{check_id}").click().run()
        app.segmented_control(key=f"ck_new_action_kind_{check_id}").set_value("meeting").run()
        app.text_input(key=f"ck_new_action_when_{check_id}").set_value("2026-08-14 10:00").run()
        app.button(key=f"ck_new_action_add_{check_id}").click().run()

        assert not app.exception
        draft = app.session_state[checks_session.CK_SET_KEY].checks[0].actions[0]
        assert draft.kind == "meeting"
        assert draft.when == "2026-08-14 10:00"
        assert len(app.get("download_button")) == 1


class TestSavedSets:
    def test_a_set_survives_being_reloaded(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        app.text_input(key="ck_set_name").set_value("Payroll checks").run()
        app.text_area(key="ck_persona").set_value("You are a finance controller.").run()

        app.button(key="ck_save_set").click().run()
        assert not app.exception

        stored = checks_db.list_sets(1)
        assert [row["name"] for row in stored] == ["Payroll checks"]
        reloaded = checks_db.load_set(stored[0]["set_id"], 1)
        assert reloaded.persona == "You are a finance controller."
        assert [check.criteria_text for check in reloaded.checks] == ["Bonus must be at most 5% of basic."]

    def test_a_reloaded_set_runs_on_its_stored_sql(self, tmp_path, monkeypatch):
        """What a saved set is for: next month's file, the same rules, no model calls."""
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        app.text_input(key="ck_set_name").set_value("Payroll checks").run()
        app.button(key="ck_save_set").click().run()

        # What `replace_set` does on Load: the rules come back, the session's results don't.
        stored = checks_db.list_sets(1)
        app.session_state[checks_session.CK_SET_KEY] = checks_db.load_set(stored[0]["set_id"], 1)
        app.session_state[checks_session.CK_RUNTIME_KEY] = {}
        app.run()

        def boom(*args, **kwargs):
            raise AssertionError("should not have called the provider")

        monkeypatch.setattr(checks_view.sql_builder, "generate_and_run", boom)
        app.button(key=f"ck_run_{check_id}").click().run()

        assert not app.exception
        assert len(app.session_state[checks_session.CK_RUNTIME_KEY][check_id].frame) == 3

    def test_saving_is_refused_until_the_set_has_a_name(self, tmp_path, monkeypatch):
        app = _loaded(tmp_path, monkeypatch)
        assert app.button(key="ck_save_set").disabled


class TestStartOver:
    def test_it_clears_the_criteria_but_not_the_saved_sets(self, tmp_path, monkeypatch):
        """A saved set is a recipe — being able to run the same rules against a different
        file is the whole point of storing one."""
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        app.text_input(key="ck_set_name").set_value("Payroll checks").run()
        app.button(key="ck_save_set").click().run()

        app.session_state["de_view"] = "Setup"
        app.run()
        app.button(key="de_start_over_button").click().run()

        assert not app.exception
        assert checks_session.CK_SET_KEY not in app.session_state
        assert [row["name"] for row in checks_db.list_sets(1)] == ["Payroll checks"]


class TestTheCriteriaChart:
    """A chart the user builds under a criteria's result, and pins with it.

    The set-level summary at the foot of the tab compares the rules to each other; this is
    the picture of one rule's own rows, which is the other half of what an exception report
    wants and the half that has to be asked for.
    """

    def _tested(self, tmp_path, monkeypatch):
        app = _add_criteria(_loaded(tmp_path, monkeypatch), "Bonus cap")
        check_id = _check_ids(app)[0]
        app.text_area(key=f"ck_text_{check_id}").set_value("Bonus must be at most 5% of basic.").run()
        _stub_test(monkeypatch)
        app.button(key=f"ck_test_{check_id}").click().run()
        monkeypatch.setattr(checks_view.checks_remarks, "write_remarks", lambda *a, **k: ("", []))
        return app, check_id

    def _charted(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.button(key=f"ck_chart_new_{check_id}").click().run()
        return app, check_id

    def test_generating_one_opens_the_panel_pre_filled(self, tmp_path, monkeypatch):
        app, check_id = self._charted(tmp_path, monkeypatch)
        assert not app.exception
        assert app.session_state[checks_session.CK_SET_KEY].checks[0].chart is not None
        assert app.selectbox(key=f"ck_chart_kind_{check_id}")
        assert app.multiselect(key=f"ck_chart_y_{check_id}").value

    def test_the_panel_offers_the_combo_type_and_the_aggregate_toggle(self, tmp_path, monkeypatch):
        app, check_id = self._charted(tmp_path, monkeypatch)
        # AppTest reports the formatted labels, which is what the user actually picks from.
        combo = checks_view.charts.CHART_LABELS[checks_view.charts.CHART_COMBO]
        assert combo in app.selectbox(key=f"ck_chart_kind_{check_id}").options
        assert app.toggle(key=f"ck_chart_agg_{check_id}") is not None

    def test_a_generated_chart_arrives_already_aggregated(self, tmp_path, monkeypatch):
        """A criteria result is one row per employee, so the first thing worth seeing is the
        total per department rather than a bar per person."""
        app, check_id = self._charted(tmp_path, monkeypatch)
        assert app.toggle(key=f"ck_chart_agg_{check_id}").value is True
        assert app.session_state[checks_session.CK_SET_KEY].checks[0].chart.aggregations

    def test_the_panel_keeps_its_open_state_as_a_widget(self, tmp_path, monkeypatch):
        """Every control in the panel ends in `st.rerun`, so an expander that took its state
        from the call would shut itself on every change. Registering it as a widget — `key`
        plus `on_change` — is what makes the browser hand the open state back each run.

        Asserted through the id rather than by opening it: `AppTest` rebuilds widget state
        from the tree's *widget* nodes each run, and an expander isn't one, so the open state
        can't be driven from a test. The id is only stamped on a stateful expander, so it is
        the part of the mechanism this suite can hold in place.
        """
        app, check_id = self._charted(tmp_path, monkeypatch)
        # An expander given an icon arrives as a Status node in the test tree.
        panel = next(block for block in app.status if block.label == "Customize chart")
        assert panel.proto.id

    def test_the_aggregate_toggle_asks_what_to_compute_for_each_value(self, tmp_path, monkeypatch):
        """The function pickers are keyed by column name, so they only exist once a column is
        chosen — which is why `ChartKeys` records what it hands out instead of listing it."""
        app, check_id = self._charted(tmp_path, monkeypatch)
        app.toggle(key=f"ck_chart_agg_{check_id}").set_value(True).run()
        app.multiselect(key=f"ck_chart_y_{check_id}").set_value(["criteria_result"]).run()

        assert not app.exception
        assert app.multiselect(key=f"ck_chart_fn_criteria_result_{check_id}").value == ["sum"]
        chart = app.session_state[checks_session.CK_SET_KEY].checks[0].chart
        assert chart.aggregate_by_x
        assert [(a.column, a.function) for a in chart.aggregations] == [("criteria_result", "sum")]

    def test_aggregating_lets_a_text_column_be_counted(self, tmp_path, monkeypatch):
        """A criteria result whose only number is `criteria_result` still charts as "how many
        breaches per employee" — which is the count of a text column."""
        app, check_id = self._charted(tmp_path, monkeypatch)
        app.toggle(key=f"ck_chart_agg_{check_id}").set_value(True).run()

        assert "criteria_met" in app.multiselect(key=f"ck_chart_y_{check_id}").options
        app.multiselect(key=f"ck_chart_y_{check_id}").set_value(["criteria_met"]).run()
        assert app.multiselect(key=f"ck_chart_fn_criteria_met_{check_id}").value == ["count"]

    def test_changing_a_control_is_kept_on_the_criteria(self, tmp_path, monkeypatch):
        app, check_id = self._charted(tmp_path, monkeypatch)
        app.selectbox(key=f"ck_chart_kind_{check_id}").set_value(
            checks_view.charts.CHART_BAR_HORIZONTAL
        ).run()
        assert app.session_state[checks_session.CK_SET_KEY].checks[0].chart.kind == (
            checks_view.charts.CHART_BAR_HORIZONTAL
        )

    def test_two_criteria_get_their_own_chart_widgets(self, tmp_path, monkeypatch):
        """The same trap the rest of this page has: a key shared between two criteria keeps
        showing whichever was created first."""
        app, first = self._charted(tmp_path, monkeypatch)
        app = _add_criteria(app, "PF mismatch")
        second = _check_ids(app)[1]
        app.text_area(key=f"ck_text_{second}").set_value("PF must match.").run()
        app.button(key=f"ck_test_{second}").click().run()
        app.button(key=f"ck_chart_new_{second}").click().run()

        assert app.selectbox(key=f"ck_chart_kind_{first}")
        assert app.selectbox(key=f"ck_chart_kind_{second}")

    def test_saving_pins_the_chart_beside_the_table(self, tmp_path, monkeypatch):
        app, check_id = self._charted(tmp_path, monkeypatch)
        app.button(key=f"ck_save_{check_id}").click().run()

        item = app.session_state[dashboard_session.DB_REPORT_KEY].pool[0]
        assert item.has_chart()
        assert item.has_table()

    def test_saving_without_one_pins_the_table_alone(self, tmp_path, monkeypatch):
        app, check_id = self._tested(tmp_path, monkeypatch)
        app.button(key=f"ck_save_{check_id}").click().run()

        item = app.session_state[dashboard_session.DB_REPORT_KEY].pool[0]
        assert not item.has_chart()
        assert item.has_table()

    def test_removing_the_chart_and_saving_again_leaves_the_item_table_only(self, tmp_path, monkeypatch):
        """The item is updated in place, so an item that keeps a chart the criteria no longer
        has would be a report showing something the user deleted."""
        app, check_id = self._charted(tmp_path, monkeypatch)
        app.button(key=f"ck_save_{check_id}").click().run()
        assert app.session_state[dashboard_session.DB_REPORT_KEY].pool[0].has_chart()

        app.button(key=f"ck_chart_remove_{check_id}").click().run()
        app.button(key=f"ck_save_{check_id}").click().run()

        pool = app.session_state[dashboard_session.DB_REPORT_KEY].pool
        assert len(pool) == 1
        assert not pool[0].has_chart()

    def test_the_chart_follows_the_filter_the_table_is_showing(self, tmp_path, monkeypatch):
        """A report item headed "breaches only" must not carry a chart that quietly includes
        the passes — the two are drawn from the same rows on purpose."""
        app, check_id = self._charted(tmp_path, monkeypatch)
        app.segmented_control(key=f"ck_filter_{check_id}").set_value("Failures").run()
        app.button(key=f"ck_save_{check_id}").click().run()

        item = app.session_state[dashboard_session.DB_REPORT_KEY].pool[0]
        assert len(item.frame) == 1
        assert len(item.figure.data[0].x) == 1

    def test_the_chart_is_stored_with_the_set_and_comes_back_with_it(self, tmp_path, monkeypatch):
        """The point of storing choices rather than a figure: next month's file gets the
        same chart without anyone rebuilding it."""
        app, check_id = self._charted(tmp_path, monkeypatch)
        original = app.session_state[checks_session.CK_SET_KEY].checks[0].chart

        app.text_input(key="ck_set_name").set_value("Payroll checks").run()
        app.button(key="ck_save_set").click().run()

        stored = checks_db.list_sets(1)
        reloaded = checks_db.load_set(stored[0]["set_id"], 1)

        assert not app.exception
        assert reloaded.checks[0].chart == original
