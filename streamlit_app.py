import logging
import zipfile
import json
from datetime import datetime, date
from io import BytesIO
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
from cleaner.db import init_cleaning_templates_table
from cleaner.exceptions import TemplateStorageError
from llm.db import init_llm_table
from llm.exceptions import LLMDatabaseError
from meetings.db import init_meetings_tables
from meetings.exceptions import MeetingStorageError
from meetings.session import invitee_route_params
from tasks.db import init_tasks_table
from tasks.exceptions import TaskStorageError

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
        init_tasks_table()
        init_cleaning_templates_table()
    except (
        AuthDatabaseError,
        LLMDatabaseError,
        ChecksStorageError,
        ChatTypeStorageError,
        MeetingStorageError,
        TaskStorageError,
        TemplateStorageError,
    ):
        logger.exception("Failed to bootstrap the application database.")
        raise
    return True


try:
    bootstrap_database()
except (
    AuthDatabaseError,
    LLMDatabaseError,
    ChecksStorageError,
    MeetingStorageError,
    TemplateStorageError,
):
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
        "": [st.Page("app_pages/home.py", title="Home", icon="🏠", default=True)],
        "Explore": [
            st.Page("app_pages/chat_with_data.py", title="Chat with data", icon=":material/chat:"),
            # Directly after the chat, because pinning an answer there is the only way
            # anything gets here — the two pages are one workflow (requirement 6.1–6.3).
            st.Page("app_pages/dashboard.py", title="Dashboard", icon="📊"),
        ],
        # Its own section rather than part of Explore: a meeting is a workflow with two
        # sides (creator here, invitee on a link), not a data-exploration tool.
        "Meetings": [
            st.Page("app_pages/meetings.py", title="Meetings", icon="👨‍💻")
        ],
        # Requirement 8's first line: running a saved task is open to any logged-in user,
        # unlike building one. The section therefore exists for everyone, and Task builder
        # is added to it below only for the two roles that may build.
        "Automate": [
            st.Page("app_pages/run_task.py", title="Run a task", icon="📊")
        ],
        "Utilities": [
            st.Page("app_pages/data_cleaner.py", title="Data cleaner", icon="🧹"),
            st.Page("app_pages/add_files.py", title="Append Files", icon="➕"),
            st.Page("app_pages/mergedata.py", title="Merge Files", icon="🔀"),
            st.Page("app_pages/PdfExtracter.py", title="PDF Extracter", icon="🗂️"),
            st.Page("app_pages/reco_any.py", title="Reconcile Data", icon="↔️"),
            st.Page("app_pages/reco2B.py", title="Reconcile GSTR-2B", icon="📄")
        ],
        "Account": [st.Page("app_pages/settings.py", title="Settings", icon=":material/settings:")],
    }
    # Requirement 2.2 grants Task Builder to admins and superusers only. Registered
    # conditionally rather than left in the sidebar for everyone to be refused at, the same
    # call `user_management.py` gets — the page still carries its own `require_role` guard,
    # since navigation is not access control.
    if "backup_zip" not in st.session_state:
        st.session_state.backup_zip = None

    if "backup_ready" not in st.session_state:
        st.session_state.backup_ready = False
    #function to create a zip file of the backup paths
    def create_backup_zip():
        settings_path = Path("settings.json")
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)

        backup_list = settings.get("backup", {}).get("backupList", [])
        if not backup_list:
            raise ValueError("No backup paths configured in settings.json")

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in backup_list:
                path = Path(entry)
                if not path.exists():
                    raise FileNotFoundError(f"Backup path not found: {entry}")

                if path.is_file():
                    zf.write(path, path.name)
                elif path.is_dir():
                    for file_path in sorted(path.rglob("*")):
                        if file_path.is_file():
                            arcname = str(file_path.relative_to(path.parent))
                            zf.write(file_path, arcname)

        buffer.seek(0)
        return buffer
    if st.session_state.get("role") in ("admin", "superuser"):
        pages["Automate"].append(
            st.Page("app_pages/task_builder.py", title="Task builder", icon="🛠️")
        )
    #button for backup on click download backup files
        if st.sidebar.button(":material/backup: Backup", width="stretch", key="backup_btn"):
            try:
                zip_buffer = create_backup_zip()
                st.session_state.backup_zip = zip_buffer.getvalue()
                st.session_state.backup_ready = True
                st.toast("Backup created successfully!", icon="✅")
            except Exception as e:
                st.toast(f"Backup failed: {str(e)}", icon="❌")

            if st.session_state.get("backup_ready"):
                if st.sidebar.download_button(
                    "📥 Download Backup",
                    data=st.session_state.backup_zip,
                    file_name=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                    mime="application/zip",
                    key="download_backup_btn"
                ):
                    st.toast("Backup downloaded successfully!", icon="✅")
                    st.session_state.backup_ready = False
                    st.session_state.backup_zip = None
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
