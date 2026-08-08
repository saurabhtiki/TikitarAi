"""AppTest coverage for chat types on the Chat page (requirement 6.6).

The end-to-end behaviour, through real widgets and a real DuckDB session: save a setup,
select it again, upload next month's file, and have the links and column descriptions
already there — or be told exactly why not.

The test that matters most is `test_a_text_date_column_is_fixed_on_the_way_in`. Everything
else is convenience; that one is correctness, because a date column loaded as text answers
`joining_date < …` with wrong rows and no error at all.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin

# Imported for its side effect: `AppTest` puts the page's own directory first on sys.path,
# after which a bare `import dashboard` inside the page finds `app_pages/dashboard.py`
# instead of the package. Importing the package here settles it in `sys.modules` first.
from dashboard import session as dashboard_session  # noqa: F401  isort:skip
from chat_types import db as chat_type_db
from chat_types import session as chat_type_session
from checks import db as checks_db
from engine import session as engine_session
from engine.relationships import Relationship
from llm.db import init_llm_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = str(PROJECT_ROOT / "app_pages" / "chat_with_data.py")

# Two months of the same shape. In March every joining date is blank, which is what makes
# detection call the column text — the case the saved type has to overrule.
SALARY_JANUARY = b"emp_id,joining_date,bonus\n001,2024-01-05,100\n002,2024-01-06,200\n"
SALARY_MARCH = b"emp_id,joining_date,bonus\n003,,150\n004,,250\n"
SALARY_BROKEN = b"emp_id,joining_date,bonus\n005,not a date,150\n"
SALARY_MISSING_COLUMN = b"emp_id,joining_date\n006,2024-03-01\n"
SALARY_EXTRA_COLUMN = b"emp_id,joining_date,bonus,internal_note\n007,2024-03-02,50,ignore\n"

EMPLOYEE_CSV = b"emp_id,name\n001,Ana\n002,Bo\n003,Cy\n004,Di\n"
LEAVE_CSV = b"emp_id,days\n001,3\n"


def _make_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()
    init_llm_table()
    chat_type_db.init_chat_types_table()
    checks_db.init_check_sets_table()

    app = AppTest.from_file(PAGE_PATH, default_timeout=90)
    app.session_state["user_id"] = 1
    app.session_state["email"] = "admin@admin.com"
    app.session_state["role"] = "normal_user"
    app.run()
    return app


def _upload(app, *files):
    app.session_state[engine_session.STEP_UPLOAD] = True
    app.run()
    app.file_uploader(key=engine_session.DE_UPLOADER_KEY).set_value(
        [(name, content, "text/csv") for name, content in files]
    )
    app.run()
    return app


def _save_as(app, name):
    app.text_input(key="ct_name_input").set_value(name).run()
    app.button(key="ct_save_button").click().run()
    return app


def _select(app, name):
    app.selectbox(key=chat_type_session.CT_PICKER_KEY).set_value(name).run()
    return app


def _types(app, table):
    for loaded in app.session_state[engine_session.DE_TABLES_KEY].values():
        if loaded.table_name == table:
            return loaded.semantic_types
    return {}


class TestTheBar:
    def test_it_starts_on_the_ad_hoc_path(self, tmp_path, monkeypatch):
        # Every earlier stage's behaviour, unchanged: no chat type, nothing required.
        app = _make_app(tmp_path, monkeypatch)
        assert app.selectbox(key=chat_type_session.CT_PICKER_KEY).value == chat_type_session.NEW_CHAT_TYPE
        assert chat_type_session.CT_ACTIVE_KEY not in app.session_state

    def test_saving_needs_a_name_and_some_data(self, tmp_path, monkeypatch):
        app = _make_app(tmp_path, monkeypatch)
        assert app.button(key="ct_save_button").disabled

    def test_a_saved_setup_is_offered_back_in_the_picker(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        assert "Salary processing" in app.selectbox(key=chat_type_session.CT_PICKER_KEY).options
        assert app.session_state[chat_type_session.CT_ACTIVE_KEY].display_name() == "Salary processing"

    def test_saving_captures_the_tables_links_and_descriptions(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("salary.csv", SALARY_JANUARY),
            ("employee.csv", EMPLOYEE_CSV),
        )
        app.session_state[engine_session.DE_RELATIONSHIPS_KEY] = [
            Relationship("salary", "emp_id", "employee", "emp_id")
        ]
        _save_as(app, "Salary processing")

        stored = chat_type_db.load_type(1, 1)
        assert sorted(stored.table_names()) == ["employee", "salary"]
        assert stored.relationships[0].parent_table == "employee"

    def test_saving_the_same_name_updates_rather_than_adding(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")
        _save_as(app, "Salary processing")
        assert len(chat_type_db.list_types(1)) == 1

    @pytest.mark.skip(
        reason="The Delete button is commented out in `_render_chat_type_bar` — deleting a "
        "chat type is not offered on the page for now. Kept rather than removed because the "
        "handler, `chat_types.db.delete_type` and its un-scoping of criteria sets are all "
        "still there: un-comment the button and this passes again."
    )
    def test_deleting_puts_the_session_back_on_the_ad_hoc_path(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        app.button(key="ct_delete_button").click().run()

        assert chat_type_db.list_types(1) == []
        assert chat_type_session.CT_ACTIVE_KEY not in app.session_state


class TestMatchingAnUpload:
    def test_a_text_date_column_is_fixed_on_the_way_in(self, tmp_path, monkeypatch):
        """The whole point of the feature.

        March's file has no joining dates at all, so detection calls the column text. Left
        that way, `joining_date < '2024-04-01'` becomes a string comparison that returns
        wrong rows with no error — so the saved type overrules detection.
        """
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_MARCH))

        assert _types(fresh, "salary")["joining_date"] == "date"
        assert "came in as Text and was read as a Date" in " ".join(
            element.value for element in fresh.caption
        )

    def test_a_matching_upload_restores_the_links_and_descriptions(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("salary.csv", SALARY_JANUARY),
            ("employee.csv", EMPLOYEE_CSV),
        )
        app.session_state[engine_session.DE_RELATIONSHIPS_KEY] = [
            Relationship("salary", "emp_id", "employee", "emp_id")
        ]
        entries = app.session_state[engine_session.DE_DICTIONARY_KEY]
        for entry in entries:
            if entry.column == "bonus":
                entry.description = "Monthly bonus"
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_MARCH), ("employee.csv", EMPLOYEE_CSV))

        assert fresh.session_state[engine_session.DE_RELATIONSHIPS_KEY][0].parent_table == "employee"
        described = {
            entry.column: entry.description
            for entry in fresh.session_state[engine_session.DE_DICTIONARY_KEY]
        }
        assert described["bonus"] == "Monthly bonus"

    def test_a_missing_column_blocks_and_says_which(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_MISSING_COLUMN))

        assert any("problem(s) to fix" in element.value for element in fresh.error)
        assert any("**bonus** is missing in **salary**" in element.value for element in fresh.markdown)

    def test_a_column_that_wont_convert_is_refused_with_the_values(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_BROKEN))

        problems = " ".join(element.value for element in fresh.markdown)
        assert "couldn't be read as a Date" in problems and "'not a date'" in problems

    def test_a_missing_file_blocks(self, tmp_path, monkeypatch):
        app = _upload(
            _make_app(tmp_path, monkeypatch),
            ("salary.csv", SALARY_JANUARY),
            ("employee.csv", EMPLOYEE_CSV),
        )
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_JANUARY))

        assert any("no uploaded file matches" in element.value for element in fresh.markdown)

    def test_an_extra_column_is_dropped_and_mentioned(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_EXTRA_COLUMN))

        assert "internal_note" not in _types(fresh, "salary")
        assert any("not imported" in element.value for element in fresh.caption)

    def test_an_extra_file_is_not_imported(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_JANUARY), ("leave.csv", LEAVE_CSV))

        assert "leave" not in [
            table.table_name for table in fresh.session_state[engine_session.DE_TABLES_KEY].values()
        ]
        assert any("not imported" in element.value for element in fresh.caption)

    def test_a_file_that_is_entirely_unexpected_still_says_so(self, tmp_path, monkeypatch):
        # Every uploaded file discarded means no tables at all, and without the note the
        # file would appear to vanish on upload with nothing said.
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("leave.csv", LEAVE_CSV))

        assert any("not imported" in element.value for element in fresh.caption)

    def test_a_clean_match_says_so(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_JANUARY))

        assert any("matched this chat type exactly" in element.value for element in fresh.success)


class TestSelectingAfterTheFilesAreLoaded:
    """The other order: upload first, then pick the chat type.

    The saved types are applied while the file is being *read*, so a table already in
    DuckDB can't be brought under a chat type chosen afterwards — it was typed by detection
    and the text is gone. Selecting therefore re-reads the uploader's files.
    """

    def test_the_saved_types_are_applied_to_files_uploaded_first(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _upload(fresh, ("salary.csv", SALARY_MARCH))
        assert _types(fresh, "salary")["joining_date"] == "text"

        _select(fresh, "Salary processing")

        assert _types(fresh, "salary")["joining_date"] == "date"
        # A match with the retyping noted, rather than the silent "exactly" variant.
        assert any("1 table(s) matched" in element.value for element in fresh.success)

    def test_a_file_that_doesnt_match_still_blocks_in_this_order(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _upload(fresh, ("salary.csv", SALARY_MISSING_COLUMN))
        _select(fresh, "Salary processing")

        assert any("**bonus** is missing in **salary**" in element.value for element in fresh.markdown)

    def test_switching_back_to_new_leaves_the_data_loaded(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        _select(app, chat_type_session.NEW_CHAT_TYPE)

        assert "salary" in [
            table.table_name for table in app.session_state[engine_session.DE_TABLES_KEY].values()
        ]


class TestBlockingTheViews:
    def test_chat_is_closed_while_the_upload_doesnt_match(self, tmp_path, monkeypatch):
        # A text date column answers questions with wrong rows rather than an error, which
        # is the one outcome this feature exists to prevent.
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_MISSING_COLUMN))
        fresh.session_state["de_view"] = "Chat"
        fresh.run()

        assert any("Fix the problems listed in Step 1" in element.value for element in fresh.info)
        assert not fresh.chat_input

    def test_checks_is_closed_too(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_MISSING_COLUMN))
        fresh.session_state["de_view"] = "Checks"
        fresh.run()

        assert any("straight onto your Dashboard" in element.value for element in fresh.info)

    def test_the_ad_hoc_path_is_never_blocked(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_MISSING_COLUMN))
        app.session_state["de_view"] = "Chat"
        app.run()
        assert not any("Fix the problems listed in Step 1" in element.value for element in app.info)


class TestWhereTheReportIsShown:
    """The check reports inside Step 1, not in a panel of its own.

    A green banner, a retype note and a link warning are setup feedback: worth reading once,
    not worth a bordered box above every question the user asks for the rest of the session.
    Collapsing Step 1 takes them off the screen. What must not be missed — a blocking
    problem, or a file that didn't import — re-opens the step instead.
    """

    def _matched_and_collapsed(self, tmp_path, monkeypatch, view):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        app.session_state[engine_session.STEP_UPLOAD] = False
        app.session_state["de_view"] = view
        app.run()
        return app

    def test_a_matched_upload_says_nothing_once_step_one_is_collapsed(self, tmp_path, monkeypatch):
        app = self._matched_and_collapsed(tmp_path, monkeypatch, "Chat")

        assert not any("matched" in element.value for element in app.success)
        assert app.chat_input

    def test_a_blocking_problem_re_opens_step_one_and_is_read_there(self, tmp_path, monkeypatch):
        # Chat says "fix the problems listed in Step 1", so Step 1 has to be showing them.
        # Step 1 collapses itself the moment files load, which is exactly when the problem
        # appears — without the re-open, the message would arrive already hidden.
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        # Not `_upload`, which opens the step first — the point here is a step that is
        # already shut when the files arrive.
        fresh.session_state[engine_session.STEP_UPLOAD] = False
        fresh.run()
        fresh.file_uploader(key=engine_session.DE_UPLOADER_KEY).set_value(
            [("salary.csv", SALARY_MISSING_COLUMN, "text/csv")]
        ).run()

        assert fresh.session_state[engine_session.STEP_UPLOAD] is True
        assert any("**bonus** is missing in **salary**" in element.value for element in fresh.markdown)

    def test_collapsing_it_again_is_respected(self, tmp_path, monkeypatch):
        # Re-opening on every run would fight a user who read the problem and shut the step;
        # the header keeps saying "needs attention" either way.
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_MISSING_COLUMN))
        fresh.session_state[engine_session.STEP_UPLOAD] = False
        fresh.run()

        assert fresh.session_state[engine_session.STEP_UPLOAD] is False

    def test_a_discarded_file_re_opens_step_one_too(self, tmp_path, monkeypatch):
        # Nothing is blocked, but a file that appears to vanish on upload is worse than a
        # blocked one: the user has no reason to look for a message at all.
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        fresh.session_state[engine_session.STEP_UPLOAD] = False
        _upload(fresh, ("salary.csv", SALARY_JANUARY), ("leave.csv", LEAVE_CSV))

        assert fresh.session_state[engine_session.STEP_UPLOAD] is True
        assert any("not imported" in element.value for element in fresh.caption)

    def test_a_collapsed_step_still_applies_the_saved_setup(self, tmp_path, monkeypatch):
        # Only the *reporting* moved inside Step 1. Retyping the columns and restoring the
        # links has to happen whether or not the step is open, or landing on Chat with it
        # shut would query an untyped table.
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        fresh = _make_app(tmp_path, monkeypatch)
        _select(fresh, "Salary processing")
        _upload(fresh, ("salary.csv", SALARY_MARCH))
        fresh.session_state[engine_session.STEP_UPLOAD] = False
        fresh.session_state["de_view"] = "Chat"
        fresh.run()

        assert _types(fresh, "salary")["joining_date"] == "date"


class TestStartOver:
    def test_it_discards_the_data_but_keeps_the_chat_type_selected(self, tmp_path, monkeypatch):
        # Discarding this month's files to upload next month's against the same setup is
        # the normal way to use a chat type, so the selection survives.
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        app.session_state["de_view"] = "Setup"
        app.run()
        app.button(key="de_start_over_button").click().run()

        assert app.session_state[engine_session.DE_TABLES_KEY] == {}
        assert app.session_state[chat_type_session.CT_ACTIVE_KEY].display_name() == "Salary processing"
        assert [row["name"] for row in chat_type_db.list_types(1)] == ["Salary processing"]

    def test_the_saved_setup_applies_again_to_the_next_upload(self, tmp_path, monkeypatch):
        app = _upload(_make_app(tmp_path, monkeypatch), ("salary.csv", SALARY_JANUARY))
        _save_as(app, "Salary processing")

        app.session_state["de_view"] = "Setup"
        app.run()
        app.button(key="de_start_over_button").click().run()
        _upload(app, ("salary.csv", SALARY_MARCH))

        assert _types(app, "salary")["joining_date"] == "date"
