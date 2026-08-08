import logging
from pathlib import Path
import streamlit as st
from PIL import Image
from auth.db import init_db, seed_default_admin
from auth.exceptions import AuthDatabaseError
from auth.service import is_authenticated
from branding import LOGO_PATH
from chat_types.db import init_chat_types_table
from chat_types.exceptions import ChatTypeStorageError
from checks.db import init_check_sets_table
from checks.exceptions import ChecksStorageError
from llm.db import init_llm_table
from llm.exceptions import LLMDatabaseError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="TikitarAi", page_icon=LOGO_PATH, layout="wide")
st.html(Path("style.css"))

with st.container(horizontal=True):
    st.image(Image.open("static/tikitar-logo.webp"), width=50)
    st.header(":blue[Tikitar-AI]")


@st.cache_resource
def bootstrap_database() -> bool:
    """Creates the users table and seeds the default superuser on first run. Runs once per process."""
    try:
        init_db()
        seed_default_admin()
        init_llm_table()
        # Before `check_sets`, whose `chat_type_id` refers to it (requirement 6.6).
        init_chat_types_table()
        init_check_sets_table()
    except (AuthDatabaseError, LLMDatabaseError, ChecksStorageError, ChatTypeStorageError):
        logger.exception("Failed to bootstrap the application database.")
        raise
    return True


try:
    bootstrap_database()
except (AuthDatabaseError, LLMDatabaseError, ChecksStorageError):
    st.error("The application couldn't start because the database is unavailable.")
    st.stop()

if not is_authenticated():
    pages = [st.Page("app_pages/login.py", title="Log in", icon=":material/login:")]
else:
    # Sections rather than a flat list, because Utilities is an open-ended category
    # (spec 4) that more standalone tools get added to over time.
    pages = {
        "": [st.Page("app_pages/home.py", title="Home", icon=":material/home:", default=True)],
        "Explore": [
            st.Page("app_pages/chat_with_data.py", title="Chat with data", icon=":material/forum:"),
            # Directly after the chat, because pinning an answer there is the only way
            # anything gets here — the two pages are one workflow (requirement 6.1–6.3).
            st.Page("app_pages/dashboard.py", title="Dashboard", icon=":material/dashboard:"),
        ],
        "Utilities": [
            st.Page("app_pages/data_cleaner.py", title="Data cleaner", icon=":material/cleaning_services:")
        ],
        "Account": [st.Page("app_pages/settings.py", title="Settings", icon=":material/settings:")],
    }
    if st.session_state.get("role") == "superuser":
        pages["Admin"] = [
            st.Page(
                "app_pages/user_management.py",
                title="User management",
                icon=":material/manage_accounts:",
            )
        ]

navigation = st.navigation(pages)
navigation.run()
