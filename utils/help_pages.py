"""Page help: one HTML file per page in `static/help/`, opened in a new tab (phase 63).

The file name is the page's `url_path` (`meetings.html` for the Meetings page), so the folder
is the whole registry: drop a file in and that page gets a Help button. A page with no file
shows no button. Served by Streamlit's static serving, like `static/reports/`.
"""

from pathlib import Path

import streamlit as st

from utils.static_files import static_url

HELP_DIR = Path(__file__).resolve().parent.parent / "static" / "help"


def help_file_for(page_path: str, folder: Path = HELP_DIR) -> Path | None:
    """The help file of a page (`"meetings"` -> `meetings.html`), or None when it has none."""
    slug = (page_path or "home").strip("/")
    if not slug or "/" in slug or "\\" in slug or slug.startswith("."):
        return None
    help_file = folder / f"{slug}.html"
    return help_file if help_file.is_file() else None


def help_url(help_file: Path) -> str:
    """The static-serving URL that opens the help file in its own tab."""
    return static_url("help", help_file)


def show_help_button(page_path: str) -> None:
    """A Help button in the sidebar for the current page; nothing when it has no help file."""
    help_file = help_file_for(page_path)
    if help_file is None:
        return
    st.sidebar.link_button(
        "Help",
        help_url(help_file),
        icon=":material/help:",
        key="page_help_open",
        help="Opens the help for this page in a new browser tab.",
        width="stretch",
    )
