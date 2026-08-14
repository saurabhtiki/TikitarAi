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
from meetings.db import init_meetings_tables
from meetings.exceptions import MeetingStorageError
from meetings.session import invitee_route_params

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
        init_meetings_tables()
    except (
        AuthDatabaseError,
        LLMDatabaseError,
        ChecksStorageError,
        ChatTypeStorageError,
        MeetingStorageError,
    ):
        logger.exception("Failed to bootstrap the application database.")
        raise
    return True


try:
    bootstrap_database()
except (AuthDatabaseError, LLMDatabaseError, ChecksStorageError, MeetingStorageError):
    st.error("The application couldn't start because the database is unavailable.")
    st.stop()

# An invitee link is answered before the login gate: an invitee has no account, and their
# token is their identity (requirement 6.7, spec 2). Checked after the database is ready,
# because the page it renders reads from it immediately. A malformed or absent link returns
# None and falls through to the ordinary app.
_invitee_route = invitee_route_params()
if _invitee_route is not None:
    from app_pages.meeting_invitee import render_invitee_page

    render_invitee_page(*_invitee_route)
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
        # Its own section rather than part of Explore: a meeting is a workflow with two
        # sides (creator here, invitee on a link), not a data-exploration tool.
        "Meetings": [
            st.Page("app_pages/meetings.py", title="Meetings", icon=":material/groups:")
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
