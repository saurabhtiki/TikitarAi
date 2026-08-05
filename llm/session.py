"""Which model this session is using (requirement 3.3).

Kept in its own `llm_*` session-state namespace, separate from working data (uploads,
engine tables, chat history), because requirement 3.3 asks for exactly that separation —
and because it makes "clear the data, keep my model choice" and its opposite both
expressible.

The choice itself is stored as a `profile_id`, never as a copy of the profile. Profiles
change in Settings, and a cached copy would go stale the moment a user fixed a typo in
their base URL — leaving the session quietly talking to the old endpoint.

This and `engine/session.py` are the only Streamlit-coupled modules added this stage,
matching the convention `cleaner/session.py` already sets.
"""

import logging

import streamlit as st

from llm.db import list_profiles
from llm.exceptions import LLMDatabaseError

logger = logging.getLogger(__name__)

LLM_ACTIVE_PROFILE_KEY = "llm_active_profile_id"
LLM_PICKER_KEY = "llm_session_model_picker"
LLM_TEST_RESULT_KEY = "llm_last_test_result"


def available_profiles(user_id: int) -> list[dict]:
    """Every profile the user owns, light model included.

    Never raises — the model picker sits in the sidebar of every page, so a database
    hiccup must not take down whatever the user was actually doing.
    """
    try:
        return list_profiles(user_id)
    except LLMDatabaseError:
        logger.exception("Could not list LLM profiles for user %s.", user_id)
        return []


def session_profiles(user_id: int) -> list[dict]:
    """The profiles offered as the active session model.

    The light model is excluded: requirement 3.2 designates it for small automatic
    auxiliary tasks and 3.3 says it is used in the background without reselecting, so
    offering it as the main model would invite the user to run their whole analysis on
    the deliberately smallest model they own.
    """
    return [profile for profile in available_profiles(user_id) if not profile.get("is_light_model")]


def light_profile(user_id: int) -> dict | None:
    """The account's designated Light Model, or None if none is configured.

    Requirement 3.2: applied automatically wherever the app performs a small structured
    auxiliary task, with no per-action selection.
    """
    for profile in available_profiles(user_id):
        if profile.get("is_light_model"):
            return profile
    return None


def active_profile(user_id: int) -> dict | None:
    """The profile currently selected as this session's model.

    Falls back to the user's first profile when nothing is selected yet, and *re-reads
    it from the database every call* so an edit in Settings takes effect immediately.
    A selection pointing at a deleted profile resolves to None rather than an error.
    """
    profiles = session_profiles(user_id)
    if not profiles:
        return None

    selected_id = st.session_state.get(LLM_ACTIVE_PROFILE_KEY)
    if selected_id is None:
        return profiles[0]

    for profile in profiles:
        if profile["profile_id"] == selected_id:
            return profile

    logger.info("Session model %s is no longer available; falling back.", selected_id)
    return profiles[0]


def set_active_profile(profile_id: int | None) -> None:
    """Selects this session's model. Clears any previous test result, which described
    a different connection and would otherwise read as if it applied to this one."""
    st.session_state[LLM_ACTIVE_PROFILE_KEY] = profile_id
    st.session_state.pop(LLM_TEST_RESULT_KEY, None)


def record_test_result(succeeded: bool, message: str) -> None:
    """Stores the outcome of a Test connection run, so it survives the rerun that
    follows the button click instead of flashing past."""
    st.session_state[LLM_TEST_RESULT_KEY] = {"ok": succeeded, "message": message}


def last_test_result() -> dict | None:
    """The most recent Test connection outcome, or None."""
    return st.session_state.get(LLM_TEST_RESULT_KEY)


def clear_session_llm() -> None:
    """Drops every `llm_*` key. Requirement 2.1 has logout clear the session's LLM
    selection; `logout()`'s wholesale `st.session_state.clear()` already covers that, so
    this exists for the narrower case of resetting the choice without ending the session."""
    for key in (LLM_ACTIVE_PROFILE_KEY, LLM_TEST_RESULT_KEY, LLM_PICKER_KEY):
        st.session_state.pop(key, None)
