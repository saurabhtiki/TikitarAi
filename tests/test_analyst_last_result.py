"""`analyst.session.last_result` — what "show that as a chart" refers back to.

The transcript is the only record of the rows a follow-up means, and this is what the page
reads to rescue a chart request the model answered without running a query (see
`analyst.pipeline._chart_the_previous_result`). Picking the wrong message would put an
older result under a chart the user believes describes the newest one, so the choosing
rules are worth holding down.

`analyst/session.py` is Streamlit-coupled by design, so each scenario runs as a tiny script
through `AppTest.from_function`, the same way `tests/test_dashboard_pin_result.py` does.
"""

import pandas as pd
from streamlit.testing.v1 import AppTest

ROWS = pd.DataFrame({"department": ["Sales", "Ops"], "total": [4000.0, 2000.0]})
OLDER = pd.DataFrame({"department": ["Sales"], "headcount": [3]})


def _run(scenario):
    """Runs one scenario as a Streamlit script and returns its session state."""
    app = AppTest.from_function(
        scenario, kwargs={"rows": ROWS, "older": OLDER}, default_timeout=30
    )
    app.run()
    assert not app.exception
    return app.session_state


class TestLastResult:
    def test_the_most_recent_answer_with_rows_is_returned(self):
        def scenario(rows, older):
            import streamlit as st

            from analyst import session as chat_session
            from analyst.session import ChatMessage

            chat_session.append_message(
                ChatMessage(role="assistant", text="Older", frame=older, sql="SELECT 1")
            )
            chat_session.append_message(
                ChatMessage(role="assistant", text="Newer", frame=rows, sql="SELECT 2")
            )
            st.session_state["result"] = chat_session.last_result()

        frame, sql = _run(scenario)["result"]
        assert list(frame.columns) == ["department", "total"]
        assert sql == "SELECT 2"

    def test_a_later_answer_without_rows_does_not_hide_the_one_before_it(self):
        """The whole point: the turn that produced no query is the one being rescued."""

        def scenario(rows, older):
            import streamlit as st

            from analyst import session as chat_session
            from analyst.session import ChatMessage

            chat_session.append_message(ChatMessage(role="assistant", text="Rows", frame=rows, sql="SELECT 2"))
            chat_session.append_message(ChatMessage(role="assistant", text="I can't draw charts."))
            st.session_state["result"] = chat_session.last_result()

        frame, sql = _run(scenario)["result"]
        assert frame is not None
        assert sql == "SELECT 2"

    def test_a_failed_answer_is_skipped(self):
        def scenario(rows, older):
            import streamlit as st

            from analyst import session as chat_session
            from analyst.session import ChatMessage

            chat_session.append_message(
                ChatMessage(role="assistant", text="Broken", frame=rows, is_error=True)
            )
            st.session_state["result"] = chat_session.last_result()

        assert _run(scenario)["result"] == (None, None)

    def test_an_empty_transcript_has_nothing_to_offer(self):
        def scenario(rows, older):
            import streamlit as st

            from analyst import session as chat_session

            st.session_state["result"] = chat_session.last_result()

        assert _run(scenario)["result"] == (None, None)
