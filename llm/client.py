"""Turning a stored provider profile into a working Agno model.

One `OpenAILike` path serves local, cloud and custom profiles alike. That is a
deliberate simplification, and it follows from requirement 3.1 rather than from
laziness: base URL is always editable, model names are free text because provider
catalogs change too fast to hardcode, and every provider the spec names (OpenAI,
Anthropic, Google, Groq, LM Studio, Ollama) exposes an OpenAI-compatible Chat
Completions endpoint. Carrying six provider-specific Agno classes would buy nothing
here and would break the moment a user points `provider_type = cloud` at a gateway.

The API surface below was checked against the installed Agno (2.8.6), not recalled:
`Agent` takes `output_schema` (there is no `response_model`), `OpenAILike` lives at
`agno.models.openai.like`, and `Agent.run()` returns a `RunOutput` whose `.content`
holds the parsed object.

Nothing in this module imports Streamlit, so every function is testable without
`AppTest` and without a network call.
"""

import logging
from pathlib import Path

from agno.agent import Agent
from agno.models.openai.like import OpenAILike

from llm.crypto import decrypt_api_key
from llm.exceptions import LLMDatabaseError

logger = logging.getLogger(__name__)

# Requirement 3.1: a cloud profile's base URL defaults to the provider's standard
# endpoint but stays editable, for proxies and gateways. These are the OpenAI-compatible
# endpoints, which is what `OpenAILike` speaks.
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "together": "https://api.together.xyz/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "lm_studio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
}

# A local server that isn't running should fail in seconds, not hang the page. The
# light auxiliary tasks this client runs are all small, so a short ceiling is right.
DEFAULT_TIMEOUT_SECONDS = 60.0
TEST_TIMEOUT_SECONDS = 20.0

# Local providers reject strict structured outputs often enough that requesting them is
# a net loss: the schema still guides the model, and a deviation is caught by Pydantic
# rather than by the provider returning an error.
_LOCAL_PROVIDER = "local"


class LLMConnectionError(Exception):
    """A provider request failed. Carries the provider's own message, which names the
    real cause (invalid key, rate limited, unreachable host) far better than a
    paraphrase — requirement 3.4 asks for exactly that."""


def _api_key_for(profile: dict, key_path: Path | str | None = None) -> str | None:
    """Decrypts a profile's stored API key, or returns None when it has none.

    A missing key is normal — requirement 3.1 makes it optional for local providers —
    so it is not an error. A key that is present but undecryptable *is*, because
    silently connecting without it would produce a baffling 401 instead.
    """
    encrypted = profile.get("api_key_encrypted")
    # Not just `if not encrypted:` — a profile dict built from a pandas
    # DataFrame (e.g. a UI table row) silently turns a NULL/None value into
    # a float NaN, which is truthy and would otherwise reach .encode() below.
    if not isinstance(encrypted, str) or not encrypted:
        return None

    try:
        if key_path is None:
            return decrypt_api_key(encrypted)
        return decrypt_api_key(encrypted, key_path=key_path)
    except LLMDatabaseError as error:
        logger.exception("Could not decrypt the API key for profile %s.", profile.get("profile_id"))
        raise LLMConnectionError(
            "The saved API key for this connection couldn't be read. Re-enter it in Settings."
        ) from error


def build_model(
    profile: dict, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, key_path: Path | str | None = None
) -> OpenAILike:
    """Builds the Agno model for a saved provider profile.

    Raises:
        LLMConnectionError: if the profile names no model, or its key can't be decrypted.
    """
    model_id = str(profile.get("default_model") or "").strip()
    if not model_id:
        raise LLMConnectionError("This connection has no model name set. Add one in Settings.")

    provider_type = str(profile.get("provider_type") or "").strip().lower()
    api_key = _api_key_for(profile, key_path)

    return OpenAILike(
        id=model_id,
        # OpenAILike defaults api_key to the literal "not-provided", which local servers
        # accept and cloud ones reject with a clear 401 — the right failure either way.
        api_key=api_key or "not-provided",
        base_url=str(profile.get("base_url") or "").strip() or None,
        timeout=timeout,
        strict_output=provider_type != _LOCAL_PROVIDER,
    )


def test_connection(profile: dict, *, key_path: Path | str | None = None) -> tuple[bool, str]:
    """Fires one minimal request and reports what happened (requirement 3.4).

    Returns:
        `(True, message)` on success, `(False, provider's failure reason)` otherwise.

    Never raises: this is a diagnostic the user runs *because* something might be wrong,
    so an exception escaping would defeat its purpose.
    """
    try:
        model = build_model(profile, timeout=TEST_TIMEOUT_SECONDS, key_path=key_path)
    except LLMConnectionError as error:
        return False, str(error)

    try:
        agent = Agent(model=model, telemetry=False)
        response = agent.run("Reply with the single word: ok")
    except Exception as error:  # noqa: BLE001 — providers raise their own exception types
        logger.info("Test connection failed for profile %s: %s", profile.get("profile_id"), error)
        return False, _readable_error(error)

    reply = str(getattr(response, "content", "") or "").strip()
    model_name = profile.get("default_model")
    if not reply:
        return False, f"{model_name} connected but returned an empty response."
    return True, f"Connected to {model_name}. It replied: {reply[:80]}"


def run_structured(
    profile: dict,
    prompt: str,
    output_schema,
    *,
    instructions: str | None = None,
    key_path: Path | str | None = None,
):
    """Runs one narrow, structured task and returns the parsed Pydantic object.

    Used for the small auxiliary calls requirement 3.2 designates the Light Model for.
    Kept deliberately single-purpose — no tools, no history, no session storage — because
    each call having one job is what makes it reliable enough to run unattended.

    Raises:
        LLMConnectionError: if the provider fails or returns something unparseable.
    """
    model = build_model(profile, key_path=key_path)

    try:
        agent = Agent(
            model=model,
            instructions=instructions,
            output_schema=output_schema,
            telemetry=False,
        )
        response = agent.run(prompt)
    except Exception as error:  # noqa: BLE001 — providers raise their own exception types
        logger.exception("Structured LLM call failed for profile %s.", profile.get("profile_id"))
        raise LLMConnectionError(_readable_error(error)) from error

    content = getattr(response, "content", None)
    if not isinstance(content, output_schema):
        logger.warning(
            "Structured call returned %s rather than %s.", type(content).__name__, output_schema.__name__
        )
        raise LLMConnectionError(
            f"{profile.get('default_model')} didn't return the expected structure. "
            "A larger or more capable light model usually fixes this."
        )
    return content


def _readable_error(error: Exception) -> str:
    """Trims a provider exception to something a user can act on.

    Provider SDKs attach entire HTTP bodies to their exceptions; the first line carries
    the actionable part (`Connection error`, `Incorrect API key provided`) and the rest
    is noise on screen.
    """
    text = str(error).strip() or error.__class__.__name__
    first_line = text.splitlines()[0]
    return first_line[:300]


def describe_profile(profile: dict) -> str:
    """A one-line label for a profile, for pickers and confirmations."""
    nickname = profile.get("nickname") or "Unnamed"
    model_name = profile.get("default_model") or "no model"
    return f"{nickname} ({model_name})"
