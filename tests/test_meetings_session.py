"""Reading an invitee link out of the URL (requirement 6.7, Phase 1).

`invitee_route_params` lives in `meetings/session.py` rather than inline in
`streamlit_app.py` precisely so this file can exist: the routing decision is the first thing
that runs on every request, and it must never be able to throw.

Driven through `AppTest` because `st.query_params` needs a script context.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

PROBE = """
import streamlit as st
from meetings.session import invitee_route_params

st.session_state["route"] = invitee_route_params()
"""


def _run(tmp_path, monkeypatch, **params):
    monkeypatch.chdir(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")

    app = AppTest.from_file(str(probe), default_timeout=30)
    for key, value in params.items():
        app.query_params[key] = value
    app.run()
    return app.session_state["route"]


class TestInviteeRouteParams:
    def test_a_well_formed_link_is_read(self, tmp_path, monkeypatch):
        assert _run(tmp_path, monkeypatch, m="7", t="token-abc") == (7, "token-abc")

    def test_no_parameters_at_all_means_the_ordinary_app(self, tmp_path, monkeypatch):
        assert _run(tmp_path, monkeypatch) is None

    def test_a_token_without_a_meeting_id_is_ignored(self, tmp_path, monkeypatch):
        assert _run(tmp_path, monkeypatch, t="token-abc") is None

    def test_a_meeting_id_without_a_token_is_ignored(self, tmp_path, monkeypatch):
        # The token is the identity — an id alone must never open anything.
        assert _run(tmp_path, monkeypatch, m="7") is None

    def test_a_non_numeric_meeting_id_falls_through_rather_than_erroring(self, tmp_path, monkeypatch):
        # A stray `?m=` on the login URL must not show an error page to an employee who was
        # only trying to log in.
        assert _run(tmp_path, monkeypatch, m="not-a-number", t="token-abc") is None

    def test_surrounding_whitespace_is_tolerated(self, tmp_path, monkeypatch):
        assert _run(tmp_path, monkeypatch, m=" 7 ", t=" token-abc ") == (7, "token-abc")
