import logging

import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None

if profile is not None:
    render_sidebar(profile)

    st.subheader("🏠Home")
    st.caption(f"Role: {profile['role'].replace('_', ' ').title()}")
    st.info(
        "Pick a tool from the sidebar.",
        icon=":material/waving_hand:",
    )
    st.write(":blue[🗪] **Chat with data** uploads your files and get answers to questions about them.")
    st.write("🧹 **Data cleaner** tidies a file before you upload it.")
    st.write("👨‍💻 **Meetings** runs a subject-based chat with each invitee.")
    st.write("📊 **Run a task** plays a saved analysis over uploded files & get Report.")
    st.write("🛠️ Admins also get **Task builder**,which is where a task is recorded in the first place.")

