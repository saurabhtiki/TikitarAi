"""`pin_result` — a report item owned by its producer (requirement 6.5).

`pin` exists for chat answers, where each press is its own one-off snapshot. A criteria is
different: it gets refined and re-run, and the report has to show the new numbers rather
than the old ones sitting next to them. This suite is what holds that behaviour, since
getting it wrong is invisible until a user has arranged a report and finds two of everything.

`dashboard/session.py` is Streamlit-coupled by design, so each scenario runs as a tiny
script through `AppTest.from_function` and hands its result back in `st.session_state` —
the honest way to give session-state code a session.
"""

import pandas as pd
from streamlit.testing.v1 import AppTest

FIRST = pd.DataFrame({"employee": ["Ana"], "criteria_result": [4.0], "criteria_met": ["Yes"]})
SECOND = pd.DataFrame({"employee": ["Bo"], "criteria_result": [12.0], "criteria_met": ["No"]})

SOURCE = "check:abc123"


def _run(scenario):
    """Runs one scenario as a Streamlit script and returns its session state.

    `from_function` re-executes the function's *source*, so it sees none of this module's
    globals — every scenario takes the fixtures it needs as arguments instead.
    """
    app = AppTest.from_function(
        scenario, kwargs={"source": SOURCE, "first": FIRST, "second": SECOND}, default_timeout=30
    )
    app.run()
    assert not app.exception
    return app.session_state


class TestFirstPin:
    def test_it_lands_in_the_pool_carrying_its_source(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            st.session_state["item"] = dashboard_session.pin_result(
                source, heading="Bonus cap", comment="- One breach.", frame=first
            )
            st.session_state["report"] = dashboard_session.get_report()

        state = _run(scenario)
        item = state["item"]

        assert state["report"].pool == [item]
        assert item.source_id == SOURCE
        assert item.display_heading() == "Bonus cap"
        assert item.comment == "- One breach."

    def test_the_frame_is_a_copy(self):
        """The same rule `pin` follows: an item that referenced a live frame would change
        under the user the next time the criteria was tested."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            source_frame = first.copy()
            item = dashboard_session.pin_result(source, heading="Bonus cap", frame=source_frame)
            source_frame.loc[0, "employee"] = "changed"
            st.session_state["item"] = item

        assert _run(scenario)["item"].frame.loc[0, "employee"] == "Ana"


class TestResaving:
    def test_saving_again_updates_the_same_item(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            first_item = dashboard_session.pin_result(source, heading="Bonus cap", frame=first)
            second_item = dashboard_session.pin_result(
                source, heading="Bonus cap v2", comment="- Two.", frame=second
            )
            st.session_state["same"] = first_item is second_item
            st.session_state["report"] = dashboard_session.get_report()

        state = _run(scenario)
        pool = state["report"].pool

        assert state["same"] is True
        assert len(pool) == 1
        assert pool[0].display_heading() == "Bonus cap v2"
        assert pool[0].comment == "- Two."
        assert list(pool[0].frame["employee"]) == ["Bo"]

    def test_an_item_already_filed_is_updated_where_it_sits(self):
        """Pulling it back to the pool on every refine would undo the arrangement the user
        built, which is the opposite of helpful."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session
            from dashboard.model import add_section, assign_item

            item = dashboard_session.pin_result(source, heading="Bonus cap", frame=first)
            report = dashboard_session.get_report()
            section = add_section(report, "Exceptions")
            assign_item(report, item.item_id, section.subsections[0].node_id)

            dashboard_session.pin_result(source, heading="Bonus cap", frame=second)
            st.session_state["report"] = report

        report = _run(scenario)["report"]
        placed = report.sections[0].subsections[0].items

        assert report.pool == []
        assert len(placed) == 1
        assert list(placed[0].frame["employee"]) == ["Bo"]

    def test_a_stale_chart_is_not_carried_over(self):
        """`png` caches the *previous* figure — keeping it would export a chart of last
        run's numbers under this run's table."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            item = dashboard_session.pin_result(source, heading="Bonus cap", frame=first)
            item.png = b"stale-image"
            dashboard_session.pin_result(source, heading="Bonus cap", frame=second)
            st.session_state["item"] = item

        assert _run(scenario)["item"].png is None

    def test_an_item_the_user_removed_comes_back_fresh(self):
        """Saving again is an unambiguous request for it to be in the report, and a
        silently-dropped save would leave the button appearing to do nothing."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session
            from dashboard.model import remove_item

            first_item = dashboard_session.pin_result(source, heading="Bonus cap", frame=first)
            report = dashboard_session.get_report()
            remove_item(report, first_item.item_id)

            second_item = dashboard_session.pin_result(source, heading="Bonus cap", frame=second)
            st.session_state["same"] = first_item is second_item
            st.session_state["report"] = report

        state = _run(scenario)
        assert state["same"] is False
        assert len(state["report"].pool) == 1

    def test_two_producers_do_not_collide(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.pin_result("check:one", heading="First", frame=first)
            dashboard_session.pin_result("check:two", heading="Second", frame=second)
            st.session_state["report"] = dashboard_session.get_report()

        from dashboard.model import find_item_by_source

        report = _run(scenario)["report"]
        assert len(report.pool) == 2
        assert find_item_by_source(report, "check:two").display_heading() == "Second"


class TestUnpinSource:
    def test_it_removes_the_item(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.pin_result(source, heading="Bonus cap", frame=first)
            st.session_state["removed"] = dashboard_session.unpin_source(source)
            st.session_state["report"] = dashboard_session.get_report()

        state = _run(scenario)
        assert state["removed"] is True
        assert state["report"].pool == []

    def test_unpinning_something_that_was_never_pinned_is_a_no_op(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            st.session_state["removed"] = dashboard_session.unpin_source("check:nothing")

        assert _run(scenario)["removed"] is False


class TestChatPinsAreUnaffected:
    def test_a_chat_pin_has_no_source(self):
        """`source_id` is defaulted so every existing pin keeps behaving exactly as it did —
        each chat press is its own snapshot, with no owner to update."""

        def scenario(source, first, second):
            import streamlit as st

            from analyst.session import ChatMessage
            from dashboard import session as dashboard_session

            st.session_state["item"] = dashboard_session.pin(
                ChatMessage(role="assistant", text="An answer", question="A question")
            )

        assert _run(scenario)["item"].source_id is None
