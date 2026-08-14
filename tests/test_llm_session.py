"""Which profile a session lands on when the user hasn't picked one.

Driven through AppTest rather than called directly: llm.session reads st.session_state, which
only exists inside a script run.
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from auth.db import init_db, seed_default_admin
from llm.db import create_profile, init_llm_table, set_default_model, set_light_model
from llm.session import LLM_ACTIVE_PROFILE_KEY

# Records what active_profile() resolved to, so the assertions can read it off session_state.
_SCRIPT = """
import streamlit as st

from llm.session import active_profile, default_profile

resolved = active_profile(1)
st.session_state["resolved_nickname"] = resolved["nickname"] if resolved else None
designated = default_profile(1)
st.session_state["designated_nickname"] = designated["nickname"] if designated else None
"""


def _make_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()  # user_id 1
    init_llm_table()
    return AppTest.from_string(_SCRIPT, default_timeout=10)


def _add(nickname, model):
    return create_profile(1, nickname, "local", "http://localhost:1234", None, model, Path("data") / "tikitarai.db")


def test_active_profile_falls_back_to_the_designated_default(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)
    _add("A first by nickname", "llama-3")
    chosen = _add("Z last by nickname", "phi-3")
    set_default_model(chosen["profile_id"], 1, Path("data") / "tikitarai.db")

    at.run()

    assert not at.exception
    assert at.session_state["resolved_nickname"] == "Z last by nickname"
    assert at.session_state["designated_nickname"] == "Z last by nickname"


def test_active_profile_falls_back_to_the_first_profile_when_no_default_is_set(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)
    _add("A first by nickname", "llama-3")
    _add("Z last by nickname", "phi-3")

    at.run()

    assert not at.exception
    assert at.session_state["resolved_nickname"] == "A first by nickname"
    assert at.session_state["designated_nickname"] is None


def test_an_explicit_session_choice_beats_the_default(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)
    picked = _add("A first by nickname", "llama-3")
    default = _add("Z last by nickname", "phi-3")
    set_default_model(default["profile_id"], 1, Path("data") / "tikitarai.db")
    at.session_state[LLM_ACTIVE_PROFILE_KEY] = picked["profile_id"]

    at.run()

    assert not at.exception
    assert at.session_state["resolved_nickname"] == "A first by nickname"


def test_a_stale_selection_falls_back_to_the_default(tmp_path, monkeypatch):
    at = _make_app(tmp_path, monkeypatch)
    _add("A first by nickname", "llama-3")
    default = _add("Z last by nickname", "phi-3")
    set_default_model(default["profile_id"], 1, Path("data") / "tikitarai.db")
    at.session_state[LLM_ACTIVE_PROFILE_KEY] = 9999

    at.run()

    assert not at.exception
    assert at.session_state["resolved_nickname"] == "Z last by nickname"


def test_the_light_model_is_never_the_fallback(tmp_path, monkeypatch):
    """set_default_model clears the light flag, but a database written before that rule could
    still hold both — session_profiles hides the light model, so the fallback must skip it."""
    at = _make_app(tmp_path, monkeypatch)
    light = _add("A first by nickname", "llama-3")
    set_light_model(light["profile_id"], 1, Path("data") / "tikitarai.db")
    _add("Z last by nickname", "phi-3")

    at.run()

    assert not at.exception
    assert at.session_state["resolved_nickname"] == "Z last by nickname"
