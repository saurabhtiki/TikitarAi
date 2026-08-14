"""Session state for both sides of a meeting (requirement 6.7, Phase 1).

The only Streamlit-coupled module in the package, matching the convention `llm/session.py`
and `cleaner/session.py` already set.

Two unrelated jobs live here because both are session state and neither is big enough for a
module. The creator's keys (`meeting_*`) track which meeting is open. The invitee's
(`invitee_*`) track who has been verified — and are deliberately **not** the `auth/` keys,
because an invitee has no account: `st.session_state["user_id"]` must stay empty on that
side, so that nothing which checks it can mistake an invitee for a logged-in employee.

`invitee_route_params` is here rather than inline in `streamlit_app.py` so that the routing
decision can be tested without booting the app.
"""

import logging

import streamlit as st

logger = logging.getLogger(__name__)

MEETING_OPEN_ID_KEY = "meeting_open_id"
MEETING_SHARE_ID_KEY = "meeting_share_id"
MEETING_FLASH_KEY = "meeting_flash"

INVITEE_VERIFIED_KEY = "invitee_verified"
INVITEE_ID_KEY = "invitee_id"
INVITEE_MEETING_ID_KEY = "invitee_meeting_id"

MEETING_PARAM = "m"
TOKEN_PARAM = "t"


def invitee_route_params() -> tuple[int, str] | None:
    """The `(meeting_id, token)` in the URL, or None if this isn't an invitee link.

    Returns None for anything missing or malformed rather than raising, so a stray `?m=`
    on the normal login URL falls through to the ordinary app instead of showing an error
    page to an employee who was only trying to log in.

    The meeting id is returned for convenience only. `db.resolve_token` decides which
    meeting the token actually belongs to — a link whose id has been edited by hand must
    not open a different meeting.
    """
    try:
        raw_meeting = st.query_params.get(MEETING_PARAM)
        raw_token = st.query_params.get(TOKEN_PARAM)
    except Exception:  # noqa: BLE001 — query params are unavailable in some contexts
        logger.exception("Could not read the URL query parameters.")
        return None

    if not raw_meeting or not raw_token:
        return None

    try:
        meeting_id = int(str(raw_meeting).strip())
    except (TypeError, ValueError):
        logger.info("Ignoring an invitee link with a non-numeric meeting id: %r", raw_meeting)
        return None

    return meeting_id, str(raw_token).strip()


# --------------------------------------------------------------------------------------
# Invitee side
# --------------------------------------------------------------------------------------


def mark_verified(meeting_id: int, invitee_id: int) -> None:
    """Records that this browser session has entered the right access code.

    Scoped to the meeting and invitee it was verified for, so a second link opened in the
    same tab has to be verified on its own rather than inheriting the first one's unlock.
    """
    st.session_state[INVITEE_VERIFIED_KEY] = True
    st.session_state[INVITEE_MEETING_ID_KEY] = meeting_id
    st.session_state[INVITEE_ID_KEY] = invitee_id


def is_verified(meeting_id: int, invitee_id: int) -> bool:
    """Whether this session has already unlocked this particular invitee's chat."""
    return bool(
        st.session_state.get(INVITEE_VERIFIED_KEY)
        and st.session_state.get(INVITEE_MEETING_ID_KEY) == meeting_id
        and st.session_state.get(INVITEE_ID_KEY) == invitee_id
    )


def clear_verification() -> None:
    """Drops the invitee unlock. Used when a link resolves to a different invitee."""
    for key in (INVITEE_VERIFIED_KEY, INVITEE_MEETING_ID_KEY, INVITEE_ID_KEY):
        st.session_state.pop(key, None)


# --------------------------------------------------------------------------------------
# Creator side
# --------------------------------------------------------------------------------------


def open_meeting(meeting_id: int | None) -> None:
    """Opens a meeting's detail view, or returns to the list when given None."""
    st.session_state[MEETING_OPEN_ID_KEY] = meeting_id


def open_meeting_id() -> int | None:
    """The meeting whose detail view is showing, or None for the list."""
    return st.session_state.get(MEETING_OPEN_ID_KEY)


def flash(message: str) -> None:
    """Queues a message to show after the next rerun.

    Needed because the reruns that follow saving a meeting or generating a status replace
    the screen — anything written just before one never reaches the user. Same reason
    `settings_llm_flash` exists.
    """
    st.session_state[MEETING_FLASH_KEY] = message


def take_flash() -> str:
    """The queued message, cleared as it is read."""
    return st.session_state.pop(MEETING_FLASH_KEY, "")
