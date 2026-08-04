import logging

import pandas as pd
import streamlit as st

from auth.db import create_user, delete_user, get_user_by_id, list_users, update_user
from auth.exceptions import AuthDatabaseError, DuplicateEmailError, ProtectedAccountError
from auth.service import logout, require_role
from branding import LOGO_PATH

logger = logging.getLogger(__name__)

ROLE_OPTIONS = ["normal_user", "admin", "superuser"]


def _reset_table_selection() -> None:
    """Clears the dataframe's row selection, including its own widget state.

    Needed after any create/update/delete: the table is re-sorted by name and
    may have a different row count on the next run, so a previously selected
    positional row index could point at the wrong row or be out of bounds.
    """
    st.session_state["um_selected_user_id"] = None
    st.session_state["um_users_table"] = {"selection": {"rows": [], "columns": []}}

if not require_role("superuser"):
    st.error("You don't have permission to view this page.", icon=":material/lock:")
    st.stop()

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


@st.dialog("Add new user")
def _open_add_user_dialog() -> None:
    with st.form("um_add_user_form"):
        email = st.text_input("Email", key="um_add_email", help="Must be unique.")
        name = st.text_input("Name", key="um_add_name", help="Full display name.")
        role = st.selectbox(
            "Role",
            options=ROLE_OPTIONS,
            key="um_add_role",
            help="Determines this user's permissions.",
        )
        password = st.text_input(
            "Initial password",
            type="password",
            key="um_add_password",
            help="The user can change this later from their own profile.",
        )
        submitted = st.form_submit_button("Save", icon=":material/save:", help="Create the account.")

    if submitted:
        _handle_create_user(email, name, role, password)


def _handle_create_user(email: str, name: str, role: str, password: str) -> None:
    if not email or not name or not password:
        st.warning("Email, name, and password are all required.", icon=":material/error:")
        return
    try:
        create_user(email=email, name=name, password=password, role=role)
    except DuplicateEmailError as error:
        logger.warning("Duplicate email on user creation: %s", email)
        st.error(str(error))
    except ValueError as error:
        logger.warning("Invalid password on user creation for %s.", email)
        st.error(str(error))
    except AuthDatabaseError:
        logger.exception("Database error while creating user %s.", email)
        st.error("We couldn't create the user. Please try again shortly.")
    else:
        _reset_table_selection()
        st.rerun()


@st.dialog("Edit user")
def _open_edit_user_dialog(user_id: int) -> None:
    try:
        current = get_user_by_id(user_id)
    except AuthDatabaseError:
        logger.exception("Database error while loading user_id %s for edit.", user_id)
        st.error("We couldn't load this user's details.")
        return
    if current is None:
        st.error("This user no longer exists.")
        return

    with st.form("um_edit_user_form"):
        name = st.text_input("Name", value=current["name"], key="um_edit_name", help="Full display name.")
        email = st.text_input("Email", value=current["email"], key="um_edit_email", help="Must be unique.")
        role = st.selectbox(
            "Role",
            options=ROLE_OPTIONS,
            index=ROLE_OPTIONS.index(current["role"]),
            key="um_edit_role",
            help="Determines this user's permissions.",
        )
        submitted = st.form_submit_button("Save", icon=":material/save:", help="Save changes.")

    if submitted:
        _handle_update_user(user_id, name, email, role)


def _handle_update_user(user_id: int, name: str, email: str, role: str) -> None:
    if not name or not email:
        st.warning("Name and email are required.", icon=":material/error:")
        return
    try:
        update_user(user_id=user_id, name=name, email=email, role=role)
    except ProtectedAccountError as error:
        logger.warning("Blocked protected-account role change for user_id %s.", user_id)
        st.error(str(error))
    except DuplicateEmailError as error:
        logger.warning("Duplicate email on user update: %s", email)
        st.error(str(error))
    except AuthDatabaseError:
        logger.exception("Database error while updating user_id %s.", user_id)
        st.error("We couldn't save these changes. Please try again shortly.")
    else:
        _reset_table_selection()
        st.rerun()


@st.dialog("Delete user")
def _open_delete_confirmation_dialog(user_id: int) -> None:
    try:
        current = get_user_by_id(user_id)
    except AuthDatabaseError:
        logger.exception("Database error while loading user_id %s for delete.", user_id)
        st.error("We couldn't load this user's details.")
        return
    if current is None:
        st.error("This user no longer exists.")
        return

    st.warning(
        f"Delete {current['name']} ({current['email']})? This cannot be undone.",
        icon=":material/warning:",
    )
    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        if st.button(
            "Confirm delete",
            key="um_confirm_delete_button",
            icon=":material/delete_forever:",
            help="Permanently delete this account.",
        ):
            _handle_delete_user(user_id)
    with cancel_col:
        if st.button(
            "Cancel",
            key="um_cancel_delete_button",
            help="Close without deleting.",
        ):
            st.rerun()


def _handle_delete_user(user_id: int) -> None:
    try:
        delete_user(user_id)
    except ProtectedAccountError as error:
        logger.warning("Blocked delete of protected account, user_id %s.", user_id)
        st.error(str(error))
    except AuthDatabaseError:
        logger.exception("Database error while deleting user_id %s.", user_id)
        st.error("We couldn't delete this user. Please try again shortly.")
    else:
        _reset_table_selection()
        st.rerun()


if profile is not None:
    st.title("User management")
    st.caption("Add, edit, or remove user accounts and change roles.")

    try:
        users = list_users()
    except AuthDatabaseError:
        logger.exception("Database error while loading the user list.")
        st.error("We couldn't load the user list. Please try again shortly.")
        st.stop()

    if st.button(
        "Add new",
        key="um_add_user_button",
        icon=":material/person_add:",
        help="Create a new user account.",
    ):
        _open_add_user_dialog()

    users_df = pd.DataFrame(users)[["user_id", "photo_path", "name", "email", "role"]]

    st.caption("Select a row to edit or delete that user.")
    selection = st.dataframe(
        users_df,
        key="um_users_table",
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "user_id": None,
            "photo_path": st.column_config.ImageColumn("Photo", help="Profile photo, if set."),
            "name": "Name",
            "email": "Email",
            "role": st.column_config.TextColumn("Role", help="superuser / admin / normal_user"),
        },
    )

    selected_rows = [row for row in selection["selection"]["rows"] if row < len(users_df)]
    if selected_rows:
        selected_user_id = int(users_df.iloc[selected_rows[0]]["user_id"])
        st.session_state["um_selected_user_id"] = selected_user_id

        edit_col, delete_col = st.columns(2)
        with edit_col:
            if st.button(
                "Edit",
                key="um_edit_button",
                icon=":material/edit:",
                help="Change this user's name, email, or role.",
            ):
                _open_edit_user_dialog(selected_user_id)
        with delete_col:
            if st.button(
                "Delete",
                key="um_delete_button",
                icon=":material/delete:",
                help="Permanently remove this user account.",
            ):
                _open_delete_confirmation_dialog(selected_user_id)
    else:
        st.session_state["um_selected_user_id"] = None
