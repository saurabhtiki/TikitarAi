"""Files wait in the uploader until **Load** is pressed.

Dropping four files in used to start four loads — one per rerun as each file arrived — so
the user watched the page work through half-loaded states before it settled.

The gate is on *which files are handed over*, never on whether the reconciliation runs:
`st.file_uploader` stops reporting its files the moment a run doesn't create it, and
`sync_tables` would then drop every loaded table. That is why these tests assert on what
`_confirm_uploads` returns rather than on `sync_tables` being skipped.

`AppTest` cannot drive a real `st.file_uploader`, so the uploads are stood in for — the only
thing this code reads off one is its `file_id` and its `name`.
"""

from dataclasses import dataclass

from streamlit.testing.v1 import AppTest


@dataclass
class _Upload:
    file_id: str
    name: str


SALES = _Upload("f1", "Sales.xlsx")
COSTS = _Upload("f2", "Costs.xlsx")


def _app(scenario, **kwargs) -> AppTest:
    app = AppTest.from_function(scenario, kwargs=kwargs, default_timeout=30)
    app.run()
    assert not app.exception
    return app


def _passed(app: AppTest) -> list[str]:
    return [upload.name for upload in app.session_state["passed"]]


class TestANewFile:
    def test_it_does_not_load_until_the_button_is_pressed(self):
        def scenario(uploads):
            import streamlit as st

            from app_pages import setup_view

            st.session_state["passed"] = setup_view._confirm_uploads(uploads)

        app = _app(scenario, uploads=[SALES, COSTS])

        assert _passed(app) == []
        assert app.button(key="de_load_files").label == "Load 2 file(s)"

    def test_pressing_load_lets_every_waiting_file_through(self):
        def scenario(uploads):
            import streamlit as st

            from app_pages import setup_view

            st.session_state["passed"] = setup_view._confirm_uploads(uploads)

        app = _app(scenario, uploads=[SALES, COSTS])
        app.button(key="de_load_files").click().run()

        assert _passed(app) == ["Sales.xlsx", "Costs.xlsx"]

    def test_it_is_loaded_on_the_very_run_the_button_is_pressed(self):
        """No `st.rerun` after the press: this is called above `sync_tables`, so waiting for
        another run would leave the page a run behind the button the user just pressed."""

        def scenario(uploads):
            import streamlit as st

            from app_pages import setup_view

            passed = setup_view._confirm_uploads(uploads)
            st.session_state["passed"] = passed
            st.session_state["loaded_this_run"] = len(passed)

        app = _app(scenario, uploads=[SALES])
        app.button(key="de_load_files").click().run()

        assert app.session_state["loaded_this_run"] == 1


class TestAFileAlreadyLoaded:
    def test_it_passes_straight_through_with_no_button(self):
        def scenario(uploads):
            import streamlit as st

            from app_pages import setup_view
            from engine import session

            session.confirm_uploads(["f1"])
            st.session_state["passed"] = setup_view._confirm_uploads(uploads)

        app = _app(scenario, uploads=[SALES])

        assert _passed(app) == ["Sales.xlsx"]
        assert not app.get("button")

    def test_a_second_file_waits_without_holding_up_the_first(self):
        """The one that matters when files arrive one at a time — the already-loaded table
        must not be dropped while its neighbour waits for the button."""

        def scenario(uploads):
            import streamlit as st

            from app_pages import setup_view
            from engine import session

            session.confirm_uploads(["f1"])
            st.session_state["passed"] = setup_view._confirm_uploads(uploads)

        app = _app(scenario, uploads=[SALES, COSTS])

        assert _passed(app) == ["Sales.xlsx"]
        assert app.button(key="de_load_files").label == "Load 1 file(s)"


class TestRemovingAFile:
    def test_removal_needs_no_button(self):
        """Taking a file out of the uploader is unambiguous, so it acts at once — the table
        goes because the file is simply no longer in what's handed to `sync_tables`."""

        def scenario(uploads):
            import streamlit as st

            from app_pages import setup_view
            from engine import session

            session.confirm_uploads(["f1", "f2"])
            st.session_state["passed"] = setup_view._confirm_uploads(uploads)

        app = _app(scenario, uploads=[SALES])

        assert _passed(app) == ["Sales.xlsx"]

    def test_its_confirmation_goes_with_it(self):
        """Otherwise a stale id would sit in session state for a file that can never come
        back — Streamlit issues a new one on every upload."""

        def scenario(uploads):
            import streamlit as st

            from engine import session

            session.confirm_uploads(["f1", "f2"])
            session.forget_unconfirmed({"f1"})
            st.session_state["kept"] = set(session.confirmed_upload_ids())
            st.session_state["passed"] = []

        app = _app(scenario, uploads=[])

        assert app.session_state["kept"] == {"f1"}
