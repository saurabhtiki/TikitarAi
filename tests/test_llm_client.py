"""Building an Agno model from a saved profile, and Test connection (requirement 3.4).

No test here makes a network call. Every provider interaction is monkeypatched at the
`Agent` boundary, so the suite stays fast, offline and deterministic — and a failing
test always means this code is wrong rather than that someone's API key expired.
"""

import pytest
from pydantic import BaseModel

from llm import client
from llm.crypto import encrypt_api_key


class Reply(BaseModel):
    answer: str


@pytest.fixture
def key_path(tmp_path):
    return tmp_path / "encryption.key"


@pytest.fixture
def cloud_profile(key_path):
    return {
        "profile_id": 1,
        "nickname": "My OpenAI",
        "provider_type": "cloud",
        "base_url": "https://api.openai.com/v1",
        "api_key_encrypted": encrypt_api_key("sk-secret", key_path=key_path),
        "default_model": "gpt-4o-mini",
    }


@pytest.fixture
def local_profile():
    return {
        "profile_id": 2,
        "nickname": "Office LM Studio",
        "provider_type": "local",
        "base_url": "http://localhost:1234/v1",
        "api_key_encrypted": None,
        "default_model": "qwen2.5-7b-instruct",
    }


class _FakeAgent:
    """Stands in for `agno.agent.Agent`, recording what it was constructed with."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeAgent.last = self
        self.response = None

    def run(self, prompt):
        self.prompt = prompt
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _patch_agent(monkeypatch, response):
    # Reset the class-level record, so "was an agent ever constructed?" means this test
    # rather than whichever one ran before it.
    _FakeAgent.last = None

    def factory(**kwargs):
        agent = _FakeAgent(**kwargs)
        agent.response = response
        return agent

    monkeypatch.setattr(client, "Agent", factory)


class _Response:
    def __init__(self, content):
        self.content = content


class TestBuildModel:
    def test_a_cloud_profile_carries_its_model_url_and_decrypted_key(self, cloud_profile, key_path):
        model = client.build_model(cloud_profile, key_path=key_path)
        assert model.id == "gpt-4o-mini"
        assert model.base_url == "https://api.openai.com/v1"
        assert model.api_key == "sk-secret"

    def test_a_local_profile_needs_no_key(self, local_profile):
        model = client.build_model(local_profile)
        assert model.api_key == "not-provided"
        assert model.base_url == "http://localhost:1234/v1"

    def test_local_providers_do_not_request_strict_structured_output(self, local_profile, cloud_profile, key_path):
        """Local servers reject strict schemas often enough that asking is a net loss."""
        assert client.build_model(local_profile).strict_output is False
        assert client.build_model(cloud_profile, key_path=key_path).strict_output is True

    def test_a_custom_profile_works_the_same_way(self):
        model = client.build_model(
            {
                "provider_type": "custom",
                "base_url": "https://gateway.internal/v1",
                "api_key_encrypted": None,
                "default_model": "whatever",
            }
        )
        assert model.base_url == "https://gateway.internal/v1"

    def test_a_missing_model_name_is_refused_before_any_request(self, local_profile):
        local_profile["default_model"] = "  "
        with pytest.raises(client.LLMConnectionError, match="model name"):
            client.build_model(local_profile)

    def test_an_undecryptable_key_is_an_error_not_a_silent_anonymous_connection(self, local_profile, key_path):
        local_profile["api_key_encrypted"] = "not-a-real-token"
        with pytest.raises(client.LLMConnectionError, match="couldn't be read"):
            client.build_model(local_profile, key_path=key_path)

    def test_every_default_base_url_is_https_or_localhost(self):
        for url in client.DEFAULT_BASE_URLS.values():
            assert url.startswith("https://") or url.startswith("http://localhost")


class TestConnection:
    def test_success_reports_the_model_and_its_reply(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, _Response("ok"))
        succeeded, message = client.test_connection(local_profile)
        assert succeeded
        assert "qwen2.5-7b-instruct" in message
        assert "ok" in message

    def test_failure_reports_the_providers_own_reason(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, RuntimeError("Incorrect API key provided: sk-xxx"))
        succeeded, message = client.test_connection(local_profile)
        assert not succeeded
        assert "Incorrect API key provided" in message

    def test_an_unreachable_host_reports_that(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, ConnectionError("Connection error."))
        succeeded, message = client.test_connection(local_profile)
        assert not succeeded
        assert "Connection error" in message

    def test_an_empty_reply_counts_as_a_failure(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, _Response(""))
        succeeded, message = client.test_connection(local_profile)
        assert not succeeded
        assert "empty" in message

    def test_a_bad_profile_fails_without_ever_reaching_a_provider(self, monkeypatch, local_profile):
        local_profile["default_model"] = ""
        _patch_agent(monkeypatch, _Response("ok"))
        succeeded, message = client.test_connection(local_profile)
        assert not succeeded
        assert "model name" in message
        assert _FakeAgent.last is None

    def test_it_never_raises(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, Exception("anything at all"))
        assert client.test_connection(local_profile)[0] is False

    def test_a_huge_provider_error_is_trimmed_to_something_readable(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, RuntimeError("Rate limited\n" + "x" * 5000))
        _, message = client.test_connection(local_profile)
        assert message.startswith("Rate limited")
        assert len(message) <= 300


class TestRunStructured:
    def test_the_parsed_object_comes_back(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, _Response(Reply(answer="42")))
        assert client.run_structured(local_profile, "q", Reply).answer == "42"

    def test_the_schema_is_passed_to_the_agent(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, _Response(Reply(answer="42")))
        client.run_structured(local_profile, "q", Reply, instructions="be brief")
        assert _FakeAgent.last.kwargs["output_schema"] is Reply
        assert _FakeAgent.last.kwargs["instructions"] == "be brief"

    def test_a_provider_failure_raises_with_its_message(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, RuntimeError("Rate limited"))
        with pytest.raises(client.LLMConnectionError, match="Rate limited"):
            client.run_structured(local_profile, "q", Reply)

    def test_an_unparseable_reply_raises_rather_than_returning_a_string(self, monkeypatch, local_profile):
        _patch_agent(monkeypatch, _Response("just some prose"))
        with pytest.raises(client.LLMConnectionError, match="expected structure"):
            client.run_structured(local_profile, "q", Reply)


class TestDescribeProfile:
    def test_it_reads_as_a_label(self, cloud_profile):
        assert client.describe_profile(cloud_profile) == "My OpenAI (gpt-4o-mini)"

    def test_it_copes_with_a_half_filled_profile(self):
        assert client.describe_profile({}) == "Unnamed (no model)"
