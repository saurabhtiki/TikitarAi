"""Lets AppTest drive the comment box.

The comment editor is a custom Streamlit component, and `AppTest` cannot type into one:
it only ever gets the component's `default` back. Swapping the Quill call for a plain text
area keeps every test around it honest — the key, the value handed in, the value handed
back and the remount after an AI draft are all still the real code's.
"""

import streamlit as st

from app_pages import comment_editor


def editor_key(base_key: str, revision: int = 0) -> str:
    """The key the stand-in text area is registered under.

    The revision is part of it by design: an AI draft bumps it to remount the editor, so a
    test that expects the drafted wording on screen looks it up at revision 1.
    """
    return f"{base_key}__r{revision}"


def stub_comment_editor(monkeypatch) -> None:
    """Replaces the Quill component with a text area for the length of one test."""

    def fake_quill(value="", placeholder="", html=False, toolbar=None, key=None):
        return st.text_area(
            "Comment",
            value=value,
            key=key,
            placeholder=placeholder,
            label_visibility="collapsed",
            help="Stand-in for the rich text comment editor.",
        )

    monkeypatch.setattr(comment_editor, "st_quill", fake_quill)
