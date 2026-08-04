import logging

import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from auth.service import logout
from branding import LOGO_PATH

logger = logging.getLogger(__name__)

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None

if profile is not None:
    with st.sidebar:
        st.image(LOGO_PATH, width=50)
        st.markdown(f"**{profile['name']}**")
        st.caption(profile["email"])
        st.caption(profile["role"].replace("_", " ").title())
        st.button(
            "Log out",
            key="logout_button",
            icon=":material/logout:",
            help="End your session and return to the login screen.",
            on_click=logout,
        )

    st.title(f"Welcome, {profile['name']}")
    st.caption(f"Role: {profile['role'].replace('_', ' ').title()}")
    st.info(
        "This is a placeholder home screen. LLM configuration, Data Cleaner, "
        "Chat with Data, and Task Builder arrive in later build phases.",
        icon=":material/construction:",
    )
