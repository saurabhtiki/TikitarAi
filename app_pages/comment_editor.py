"""The comment box, with a small formatting toolbar (bold, italic, underline, lists).

Both places that let a person write a report comment — the Report Builder's item list and
the report's structure editor — call `comment_editor` here, so a comment is written the
same way wherever it is written, and there is one place that decides what the toolbar
offers.

What comes back is markup, already put through `rich_text.sanitize_comment`, so the value
stored on the item is the value the exports render — nothing is sanitized twice or
sanitized only on the way out.

Two details worth knowing before changing this:

* The editor is a custom component, and a custom component reads its `value` only when it
  mounts. Filling the box from code — which is what the "Write comment" button does —
  therefore has to remount it, and `reset_comment_editor` does that by bumping a revision
  number that forms part of the widget key.
* The component takes no `help` argument, so the tooltip lives on the label written above
  it instead.
"""

import logging

import streamlit as st
from streamlit_quill import st_quill

from dashboard.rich_text import sanitize_comment, to_editor_html

logger = logging.getLogger(__name__)

# Deliberately shorter than Quill's default toolbar: the four things asked for, plus the
# eraser that takes formatting back off. Anything else a person could paste in is dropped
# by the sanitizer anyway, so offering it would only promise formatting the report cannot
# print.
COMMENT_TOOLBAR = [
    ["bold", "italic", "underline"],
    [{"list": "ordered"}, {"list": "bullet"}],
    ["clean"],
]


def render_comment(comment: str, *, key_hint: str = "") -> None:
    """Shows a comment on screen the way the report will print it.

    `unsafe_allow_html` is safe here for the same reason it is in the export template: what
    is written has been through `sanitize_comment`, which allows a handful of formatting
    tags and no attributes at all. Anything else in the comment is already gone.
    """
    formatted = sanitize_comment(comment)
    if not formatted:
        return
    try:
        st.markdown(
            # `pre-wrap` for the same reason the export template has it: a comment written
            # before the toolbar existed holds its line breaks as newlines, not as markup.
            # The colour is the page's own, dimmed, rather than a fixed grey, so the
            # preview follows the light or dark theme the person is using.
            f'<div style="opacity: 0.7; font-style: italic; white-space: pre-wrap;">{formatted}</div>',
            unsafe_allow_html=True,
        )
    except (RuntimeError, ValueError, TypeError):
        logger.exception("A comment preview (%s) could not be rendered.", key_hint or "unnamed")
        st.caption(":orange[This comment couldn't be shown here. It is unchanged in the report.]")


def _revision_key(key: str) -> str:
    return f"{key}__revision"


def reset_comment_editor(key: str) -> None:
    """Makes the next render of this editor pick up the item's comment again.

    Called after something other than typing changes the comment — the AI draft button.
    Without it the box would keep showing what was in it when it mounted.
    """
    st.session_state[_revision_key(key)] = st.session_state.get(_revision_key(key), 0) + 1


def comment_editor(
    *,
    label: str,
    value: str,
    key: str,
    help_text: str,
    placeholder: str = "",
    show_label: bool = True,
) -> str:
    """One comment box. Returns the comment as sanitized markup.

    On any failure the comment already on the item is handed straight back rather than
    lost, and the person is told the toolbar is unavailable — a comment box that cannot
    render is a reason to keep the words, not to drop them.
    """
    if show_label:
        st.markdown(f"**{label}**", help=help_text)

    revision = st.session_state.get(_revision_key(key), 0)
    try:
        written = st_quill(
            value=to_editor_html(value),
            placeholder=placeholder,
            html=True,
            toolbar=COMMENT_TOOLBAR,
            key=f"{key}__r{revision}",
        )
    except (RuntimeError, ValueError, TypeError, OSError):
        logger.exception("The comment editor for '%s' could not be rendered.", key)
        st.error(
            "The formatting toolbar couldn't be loaded, so this comment can't be edited "
            "right now. Your existing comment is safe — reload the page to try again.",
            icon=":material/error:",
        )
        return value or ""

    return sanitize_comment(written)
