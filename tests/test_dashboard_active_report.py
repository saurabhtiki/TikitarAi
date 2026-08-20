"""The active report (requirement 7.3 step 5) — there is more than one report now.

`dashboard/session.py` used to hold exactly one report under one hardcoded key. Task Builder
has its own, and the producers that pin into a report (`checks/`, `report_items/`) must not
have to know which one they are pinning into. So the page says once, at the top of its run,
and everything below follows.

The failure this guards against is silent and expensive: an answer pinned in Chat with data
landing in a Task's report — or worse, a Task's saved report quietly absorbing a chat answer.

Same `AppTest.from_function` arrangement as `test_dashboard_pin_result.py`, for the same
reason: session-state code deserves a real session.
"""

from streamlit.testing.v1 import AppTest


def _run(scenario):
    app = AppTest.from_function(scenario, default_timeout=30)
    app.run()
    assert not app.exception
    return app.session_state


class TestDefault:
    def test_saying_nothing_means_the_session_dashboard(self):
        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.get_report().title = "Untouched"
            st.session_state["key"] = dashboard_session.active_report_key()

        state = _run(scenario)

        assert state["key"] == "db_report"
        assert state["db_report"].title == "Untouched"

    def test_use_report_with_no_argument_is_the_dashboard(self):
        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.use_report("tb_report")
            dashboard_session.use_report()
            st.session_state["key"] = dashboard_session.active_report_key()

        assert _run(scenario)["key"] == "db_report"


class TestTwoReportsStayApart:
    def test_pinning_into_a_task_leaves_the_dashboard_empty(self):
        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.use_report("tb_report")
            dashboard_session.pin_result("item:one", heading="Headcount by grade")

            st.session_state["task_pool"] = len(st.session_state["tb_report"].pool)
            st.session_state["dashboard_exists"] = "db_report" in st.session_state

        state = _run(scenario)

        assert state["task_pool"] == 1
        # Not merely empty — never created. Nothing touched the session Dashboard at all.
        assert state["dashboard_exists"] is False

    def test_each_report_keeps_its_own_items(self):
        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.use_report()
            dashboard_session.pin_result("chat:one", heading="From the chat")

            dashboard_session.use_report("tb_report")
            dashboard_session.pin_result("item:one", heading="From the task")

            st.session_state["dashboard_headings"] = [
                item.heading for item in st.session_state["db_report"].pool
            ]
            st.session_state["task_headings"] = [
                item.heading for item in st.session_state["tb_report"].pool
            ]

        state = _run(scenario)

        assert state["dashboard_headings"] == ["From the chat"]
        assert state["task_headings"] == ["From the task"]

    def test_the_same_source_id_in_two_reports_is_two_items(self):
        """A criteria saved in Chat's Checks and again in a Task's Checks is two report
        items, not one being moved back and forth — `pin_result`'s idempotency is per
        report, which is what makes reusing the Checks view safe."""

        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.use_report()
            dashboard_session.pin_result("check:abc", heading="Bonus cap")

            dashboard_session.use_report("tb_report")
            dashboard_session.pin_result("check:abc", heading="Bonus cap")

            st.session_state["dashboard_pool"] = len(st.session_state["db_report"].pool)
            st.session_state["task_pool"] = len(st.session_state["tb_report"].pool)

        state = _run(scenario)

        assert state["dashboard_pool"] == 1
        assert state["task_pool"] == 1

    def test_unpinning_in_one_report_leaves_the_other_alone(self):
        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.use_report()
            dashboard_session.pin_result("check:abc", heading="Bonus cap")
            dashboard_session.use_report("tb_report")
            dashboard_session.pin_result("check:abc", heading="Bonus cap")

            st.session_state["removed"] = dashboard_session.unpin_source("check:abc")
            st.session_state["dashboard_pool"] = len(st.session_state["db_report"].pool)
            st.session_state["task_pool"] = len(st.session_state["tb_report"].pool)

        state = _run(scenario)

        assert state["removed"] is True
        assert state["task_pool"] == 0
        assert state["dashboard_pool"] == 1


class TestReset:
    def test_start_over_clears_the_active_report_only(self):
        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.use_report()
            dashboard_session.pin_result("chat:one", heading="From the chat")
            dashboard_session.use_report("tb_report")
            dashboard_session.pin_result("item:one", heading="From the task")

            dashboard_session.reset_dashboard()

            st.session_state["task_exists"] = "tb_report" in st.session_state
            st.session_state["dashboard_pool"] = len(st.session_state["db_report"].pool)

        state = _run(scenario)

        assert state["task_exists"] is False
        assert state["dashboard_pool"] == 1


class TestSetReport:
    def test_a_loaded_report_replaces_the_active_one(self):
        def scenario():
            import streamlit as st

            from dashboard import session as dashboard_session
            from dashboard.model import Report

            dashboard_session.use_report("tb_report")
            dashboard_session.pin_result("item:one", heading="Stale")

            dashboard_session.set_report(Report(title="Loaded from a task"))

            st.session_state["title"] = dashboard_session.get_report().title
            st.session_state["pool"] = len(dashboard_session.get_report().pool)

        state = _run(scenario)

        assert state["title"] == "Loaded from a task"
        assert state["pool"] == 0
