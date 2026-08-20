import logging

import streamlit as st

from auth.exceptions import AuthDatabaseError
from auth.service import authenticate, login
from branding import LOGO_PATH

logger = logging.getLogger(__name__)

_, center, _ = st.columns([1, 2, 1])

with center:
    
    st.subheader("🔐Log in to continue")

    with st.form("login_form"):
        email = st.text_input(
            "Email",
            key="login_email",
            help="The email address associated with your account.",
        )
        password = st.text_input(
            "Password",
            type="password",
            key="login_password",
            help="Your account password.",
        )
        submitted = st.form_submit_button(
            "Log in",
            icon=":material/login:",type="primary",
            help="Submit your credentials to sign in.",
        )

    if submitted:
        if not email or not password:
            st.warning("Please enter both email and password.", icon=":material/error:")
        else:
            try:
                with st.spinner("Signing in..."):
                    user = authenticate(email, password)
            except AuthDatabaseError:
                logger.exception("Database error during login attempt for %s.", email)
                st.error("We couldn't reach the login database. Please try again shortly.")
            else:
                if user is None:
                    st.error("Incorrect email or password.")
                else:
                    login(user)
                    st.rerun()

    st.caption("Contact your administrator for access.")
