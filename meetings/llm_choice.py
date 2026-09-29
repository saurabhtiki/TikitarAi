"""Which model a meeting's chats run on (phase 56).

The organiser picks any profile from Settings — the light one included — and may change it at
any time. Only the `profile_id` is stored, so a fixed typo in Settings takes effect at once. A
meeting whose chosen profile was later deleted falls back to the default model rather than
refusing to run, and the organiser is told.

No Streamlit here; the pages draw the dropdown and the warning.
"""

import logging
from dataclasses import dataclass

from llm.db import list_profiles
from llm.exceptions import LLMDatabaseError
from meetings.model import Meeting

logger = logging.getLogger(__name__)


@dataclass
class ModelChoice:
    """The profile to use, and whether the chosen one was missing (so the default stood in)."""

    profile: dict | None = None
    fell_back: bool = False


def owned_profiles(user_id: int) -> list[dict]:
    """Every profile the user owns, or none if they can't be read. Never raises."""
    try:
        return list_profiles(user_id)
    except LLMDatabaseError:
        logger.exception("Could not list LLM profiles for user %s.", user_id)
        return []


def profile_label(profile: dict) -> str:
    """How a profile reads in the dropdown, e.g. "Local (llama-3) (light)"."""
    label = str(profile.get("nickname") or "").strip() or f"Model {profile.get('profile_id')}"
    model = str(profile.get("default_model") or "").strip()
    if model and model not in label:
        label += f" ({model})"
    if profile.get("is_light_model"):
        label += " (light)"
    elif profile.get("is_default_model"):
        label += " (default)"
    return label


def default_of(profiles: list[dict]) -> dict | None:
    """The model marked as default in Settings, or None if none is marked."""
    return next((profile for profile in profiles if profile.get("is_default_model")), None)


def resolve(profiles: list[dict], chosen_id: int | None) -> ModelChoice:
    """The profile a meeting runs on: the chosen one, or the default if none/deleted."""
    if chosen_id is not None:
        for profile in profiles:
            if profile["profile_id"] == chosen_id:
                return ModelChoice(profile, fell_back=False)
        logger.info("Meeting model %s no longer exists; using the default.", chosen_id)
        return ModelChoice(default_of(profiles), fell_back=True)
    return ModelChoice(default_of(profiles), fell_back=False)


def meeting_profile(meeting: Meeting) -> ModelChoice:
    """The model this meeting's chats, summaries and extractions use."""
    if meeting.created_by is None:
        return ModelChoice()
    return resolve(owned_profiles(meeting.created_by), meeting.profile_id)
