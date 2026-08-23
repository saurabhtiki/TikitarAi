"""`pin_imported` — a report item that came out of a workbook.

The rule this suite holds is the one the whole Excel import rests on: the numbers belong to
the file, the words belong to the user. Import a corrected or a next-month workbook and the
data refreshes underneath the title and comment already written above it.

`dashboard/session.py` is Streamlit-coupled by design, so each scenario runs as a tiny
script through `AppTest.from_function`, the same arrangement `test_dashboard_pin_result.py`
uses.
"""

import pandas as pd
from streamlit.testing.v1 import AppTest

FIRST = pd.DataFrame({"Region": ["North"], "Revenue": [10]})
SECOND = pd.DataFrame({"Region": ["North"], "Revenue": [99]})

SOURCE = "loaded:Sales"


def _run(scenario):
    app = AppTest.from_function(
        scenario, kwargs={"source": SOURCE, "first": FIRST, "second": SECOND}, default_timeout=30
    )
    app.run()
    assert not app.exception
    return app.session_state


class TestFirstImport:
    def test_it_lands_in_the_pool_named_after_the_sheet(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            st.session_state["item"] = dashboard_session.pin_imported(
                source, heading="Sales", frame=first
            )
            st.session_state["report"] = dashboard_session.get_report()

        state = _run(scenario)
        item = state["item"]

        assert state["report"].pool == [item]
        assert item.display_heading() == "Sales"
        assert item.comment == ""
        assert item.source_id == SOURCE

    def test_the_frame_is_a_copy(self):
        """The same rule every other pin follows — an item referencing a live frame would
        change under the user."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            item = dashboard_session.pin_imported(source, heading="Sales", frame=first)
            first.loc[0, "Revenue"] = 0
            st.session_state["pinned_value"] = item.frame.loc[0, "Revenue"]

        assert _run(scenario)["pinned_value"] == 10


class TestReImport:
    def test_the_numbers_refresh_without_a_second_item_appearing(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            dashboard_session.pin_imported(source, heading="Sales", frame=first)
            st.session_state["item"] = dashboard_session.pin_imported(
                source, heading="Sales", frame=second
            )
            st.session_state["report"] = dashboard_session.get_report()

        state = _run(scenario)

        assert len(state["report"].pool) == 1
        assert state["item"].frame.loc[0, "Revenue"] == 99

    def test_the_title_and_comment_the_user_wrote_are_kept(self):
        """The point of the whole feature. `pin_result` overwrites both, which is right for
        a criteria that writes its own wording and wrong for an imported sheet."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            item = dashboard_session.pin_imported(source, heading="Sales", frame=first)
            item.heading = "Revenue by region"
            item.comment = "<p><b>North</b> leads again.</p>"

            dashboard_session.pin_imported(source, heading="Sales", frame=second)
            st.session_state["item"] = item

        item = _run(scenario)["item"]

        assert item.heading == "Revenue by region"
        assert item.comment == "<p><b>North</b> leads again.</p>"

    def test_an_item_the_user_never_titled_takes_the_sheets_name_again(self):
        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            item = dashboard_session.pin_imported(source, heading="Sales", frame=first)
            item.heading = "   "

            st.session_state["item"] = dashboard_session.pin_imported(
                source, heading="Sales", frame=second
            )

        assert _run(scenario)["item"].heading == "Sales"

    def test_the_cached_chart_image_is_thrown_away(self):
        """It is a picture of last month's numbers. Keeping it would export a chart that
        disagrees with the table printed beside it."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session

            item = dashboard_session.pin_imported(source, heading="Sales", frame=first)
            item.png = b"an old rendering"

            st.session_state["item"] = dashboard_session.pin_imported(
                source, heading="Sales", frame=second
            )

        assert _run(scenario)["item"].png is None

    def test_an_item_the_user_discarded_comes_back(self):
        """Importing the file again is an unambiguous request for its contents to be in the
        report — the same rule `pin_result` follows."""

        def scenario(source, first, second):
            import streamlit as st

            from dashboard import session as dashboard_session
            from dashboard.model import remove_item

            item = dashboard_session.pin_imported(source, heading="Sales", frame=first)
            remove_item(dashboard_session.get_report(), item.item_id)

            dashboard_session.pin_imported(source, heading="Sales", frame=second)
            st.session_state["report"] = dashboard_session.get_report()

        assert len(_run(scenario)["report"].pool) == 1
