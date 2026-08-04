import logging

import streamlit as st

from auth.db import init_db, seed_default_admin
from auth.exceptions import AuthDatabaseError
from auth.service import is_authenticated
from branding import LOGO_PATH

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="TikitarAi", page_icon=LOGO_PATH, layout="wide")


@st.cache_resource
def bootstrap_database() -> bool:
    """Creates the users table and seeds the default superuser on first run. Runs once per process."""
    try:
        init_db()
        seed_default_admin()
    except AuthDatabaseError:
        logger.exception("Failed to bootstrap the user database.")
        raise
    return True


try:
    bootstrap_database()
except AuthDatabaseError:
    st.error("The application couldn't start because the user database is unavailable.")
    st.stop()

if not is_authenticated():
    pages = [st.Page("app_pages/login.py", title="Log in", icon=":material/login:")]
else:
    pages = [st.Page("app_pages/home.py", title="Home", icon=":material/home:", default=True)]
    if st.session_state.get("role") == "superuser":
        pages.append(
            st.Page(
                "app_pages/user_management.py",
                title="User management",
                icon=":material/manage_accounts:",
            )
        )

navigation = st.navigation(pages)
navigation.run()
