"""Report dashboard - one report's saved dashboard, full width, for anyone (phase 46).

A hidden page: it is not in the menu, and is reached from the **Dashboard** button on the
Reports page or on Chat with reports. A page rather than a dialog because Streamlit's widest
dialog is narrower than a dashboard with two charts to a row.

View and download only - see `app_pages/dashboard_viewer.py`.
"""

import logging

import streamlit as st

from app_pages import dashboard_viewer
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from auth.service import is_authenticated
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

#: Where Back goes when the viewer was opened some other way, e.g. a bookmarked URL.
DEFAULT_BACK_PAGE = "app_pages/run_task.py"


if not is_authenticated():
    st.error("Please log in to see a report's dashboard.", icon=":material/lock:")
    st.stop()

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


if profile is not None:
    render_sidebar(profile)
    view = dashboard_viewer.viewing()
    back_page = (view or {}).get("back_page") or DEFAULT_BACK_PAGE

    with st.container(horizontal=True, vertical_alignment="center"):
        name = (view or {}).get("name") or "Report dashboard"
        st.subheader(f"📊 {name}")
        if st.button(
            "Back",
            key="rd_back",
            icon=":material/arrow_back:",
            help="Return to the list of reports.",
        ):
            st.switch_page(back_page)

    if view is None:
        st.info(
            "Pick a report first: press **Dashboard** beside it on the **Reports** page or on "
            "**Chat with reports**.",
            icon=":material/info:",
        )
    else:
        dashboard_viewer.render_saved_dashboard(view["task_id"], name, key_prefix="rd")
