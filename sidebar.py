import base64
import logging
import mimetypes
from pathlib import Path

import streamlit as st

from auth.service import logout
from branding import LOGO_PATH
from llm import session as llm_session
from llm.client import LLMConnectionError, describe_profile, test_connection

logger = logging.getLogger(__name__)


def photo_data_uri(photo_path: str) -> str | None:
    """Reads a local photo file and inlines it as a base64 data: URI, since Streamlit
    can't serve arbitrary local file paths as image URLs directly."""
    try:
        path = Path(photo_path)
        mime_type, _ = mimetypes.guess_type(path.name)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type or 'image/png'};base64,{encoded}"
    except OSError:
        logger.warning("Could not read profile photo at %s; falling back to initials.", photo_path)
        return None


def _avatar_and_name_html(profile: dict) -> str:
    data_uri = photo_data_uri(profile["photo_path"]) if profile.get("photo_path") else None
    if data_uri:
        avatar = f'<img src="{data_uri}" style="width:40px;height:40px;border-radius:50%;object-fit:cover;">'
    else:
        initial = profile["name"][:1].upper() if profile.get("name") else "?"
        avatar = (
            '<div style="width:40px;height:40px;border-radius:50%;background:var(--primary-color,#2563EB);'
            'color:white;display:flex;align-items:center;justify-content:center;font-weight:600;">'
            f"{initial}</div>"
        )
    return (
        '<div style="display:flex;align-items:center;gap:10px;margin-top:8px;">'
        f"{avatar}<span style=\"font-weight:600;\">{profile['name']}</span></div>"
    )


def _on_model_pick(user_id: int) -> None:
    """Records the newly picked session model.

    A callback rather than a return-value read, so the choice is stored before the
    script re-executes and every page below sees it on the same run.
    """
    picked = st.session_state.get(llm_session.LLM_PICKER_KEY)
    llm_session.set_active_profile(picked["profile_id"] if picked else None)


def _render_model_picker(user_id: int) -> None:
    """The active session model, plus Test connection (requirements 3.3 and 3.4).

    Lives in the sidebar rather than on one page because requirement 3.3 lets the choice
    change at any point mid-session — so it has to be reachable from wherever the user
    happens to be.
    """
    profiles = llm_session.session_profiles(user_id)
    if not profiles:
        st.caption("No AI connection yet — add one in Settings.")
        return

    active = llm_session.active_profile(user_id)
    st.selectbox(
        "Model",
        options=profiles,
        index=next((position for position, item in enumerate(profiles) if item == active), 0),
        format_func=describe_profile,
        key=llm_session.LLM_PICKER_KEY,
        help="Which saved connection this session uses. You can change it at any time.",
        on_change=_on_model_pick,
        args=(user_id,),
    )

    if st.button(
        "Test connection",
        key="llm_test_connection_button",
        icon=":material/network_check:",
        width="stretch",
        help="Send one tiny request and report whether the provider answered.",
    ):
        target = llm_session.active_profile(user_id)
        if target is None:
            llm_session.record_test_result(False, "No connection is selected.")
        else:
            with st.spinner("Testing…"):
                try:
                    succeeded, message = test_connection(target)
                except LLMConnectionError as error:
                    succeeded, message = False, str(error)
            llm_session.record_test_result(succeeded, message)
        st.rerun(scope="app")

    result = llm_session.last_test_result()
    if result:
        if result["ok"]:
            st.success(result["message"], icon=":material/check_circle:")
        else:
            st.error(result["message"], icon=":material/error:")


def render_sidebar(profile: dict) -> None:
    """Renders the app-wide sidebar: a Log out button always at the top, a round avatar
    + name (no email, per the app's sidebar spec), then the session model picker."""
    #st.logo(LOGO_PATH, size="medium")
    with st.sidebar:
        col1,col2 = st.columns(2,vertical_alignment="center")
        with col1:
              st.html(_avatar_and_name_html(profile))
        with col2:
            st.button(
                "Log out",
                key="logout_button",
                icon=":material/logout:",
                help="End your session and return to the login screen.",
                on_click=logout,type="primary"
            )

        st.divider()
        _render_model_picker(profile["user_id"])

