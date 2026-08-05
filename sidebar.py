import base64
import logging
import mimetypes
from pathlib import Path

import streamlit as st

from auth.service import logout
from branding import LOGO_PATH

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


def render_sidebar(profile: dict) -> None:
    """Renders the app-wide sidebar: logo (via st.logo), a Log out button always at the
    top, then a round avatar + name (no email, per the app's sidebar spec)."""
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
      
