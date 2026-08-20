"""Dashboard — arrange what was pinned in Chat with Data, then download it.

Requirements 6.3 and 6.4. Open to every logged-in role, so this page follows
`settings.py` and `chat_with_data.py` and carries no `require_role` guard.

The three views — Build, Preview, Download — live in `app_pages/report_view.py`, shared with
Task Builder's Report view (requirement 7.3 step 5). What is left here is what is only true
of *this* report: it is the session Dashboard, fed by Chat with data, and it is not saved.

Requirement 6.3 is explicit that the dashboard exists for the session only, which is why the
page says so plainly rather than leaving the user to find out by refreshing.
"""

import logging

import streamlit as st

from app_pages import report_view
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from dashboard import session as dashboard_session
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

# Where pinned items come from. The two pages are one workflow, so an empty Dashboard
# offers the way back rather than just saying there is nothing here.
CHAT_PAGE = "app_pages/chat_with_data.py"

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


def _go_to_chat() -> None:
    st.switch_page(CHAT_PAGE)


EMPTY_POOL = report_view.EmptyPool(
    message=(
        "Nothing pinned yet. Ask a question in Chat with data, then press "
        "**Pin to Dashboard** under any answer."
    ),
    button_label="Go to Chat with data",
    on_click=_go_to_chat,
    icon=":material/forum:",
    button_help="Ask a question, then pin the answer to bring it back here.",
)


if profile is not None:
    render_sidebar(profile)

    st.subheader("📊 Dashboard")
    st.caption(
        ":blue[Arrange what you pinned in Chat with data, then download it as HTML or Excel. "
        "This report lives for this session only — download it before you close the tab.]"
    )

    # Said even though this *is* the default: the choice outlives the run that made it, so a
    # page that stayed silent would work on whichever report Task Builder last selected.
    dashboard_session.use_report()

    report_view.render_report_workspace(dashboard_session.get_report(), empty_pool=EMPTY_POOL)
