import streamlit as st

from auth.db import init_db, seed_default_admin
from auth.service import authenticate, is_authenticated, login, logout


def _seed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_db()
    seed_default_admin()


def test_authenticate_correct_credentials(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    user = authenticate("admin@admin.com", "nimda")
    assert user is not None
    assert user["role"] == "superuser"


def test_authenticate_wrong_password(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    assert authenticate("admin@admin.com", "wrong-password") is None


def test_authenticate_unknown_email(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    assert authenticate("nobody@example.com", "nimda") is None


def test_login_sets_exactly_three_session_keys(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    st.session_state.clear()
    user = authenticate("admin@admin.com", "nimda")

    login(user)

    assert set(st.session_state.keys()) == {"user_id", "email", "role"}
    assert st.session_state["email"] == "admin@admin.com"
    assert st.session_state["role"] == "superuser"
    assert is_authenticated() is True


def test_logout_clears_session_state_wholesale():
    st.session_state.clear()
    st.session_state["user_id"] = 1
    st.session_state["email"] = "admin@admin.com"
    st.session_state["role"] = "superuser"
    st.session_state["some_future_feature_key"] = "value"

    logout()

    assert dict(st.session_state) == {}
    assert is_authenticated() is False
