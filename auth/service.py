import logging

import streamlit as st

from auth.db import get_user_by_email
from auth.exceptions import AuthDatabaseError
from auth.passwords import verify_password

logger = logging.getLogger(__name__)


def authenticate(email: str, password: str) -> dict | None:
    """Verifies credentials against the users table.

    Returns the user record on success. Returns None for both an unknown
    email and a wrong password (same value in both cases) so the UI never
    reveals which one was incorrect.

    Raises:
        AuthDatabaseError: propagated from auth.db on real database failures.
    """
    try:
        user = get_user_by_email(email)
    except AuthDatabaseError:
        logger.error("Database error while authenticating email %s.", email)
        raise

    if user is None:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def login(user: dict) -> None:
    """Sets session state to exactly {user_id, email, role} for the given user record."""
    st.session_state["user_id"] = user["user_id"]
    st.session_state["email"] = user["email"]
    st.session_state["role"] = user["role"]


def logout() -> None:
    """Clears session state wholesale, so any keys added by later features are cleared too."""
    st.session_state.clear()


def is_authenticated() -> bool:
    """Returns True iff a user_id is present in session state."""
    return st.session_state.get("user_id") is not None


def require_role(*allowed_roles: str) -> bool:
    """Returns True iff the current session's role is one of allowed_roles.

    Intended for use at the top of a role-gated page as defense-in-depth: the
    primary gate is that streamlit_app.py never lists such a page for
    disallowed roles, but Streamlit pages remain directly navigable by path
    even when absent from st.navigation's list.
    """
    return st.session_state.get("role") in allowed_roles
