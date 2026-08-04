import pytest

from llm.crypto import _load_or_create_key, decrypt_api_key, encrypt_api_key
from llm.exceptions import LLMDatabaseError


@pytest.fixture
def key_path(tmp_path):
    return tmp_path / "encryption.key"


def test_load_or_create_key_persists_across_calls(key_path):
    first = _load_or_create_key(key_path)
    second = _load_or_create_key(key_path)

    assert first == second
    assert key_path.exists()


def test_encrypt_decrypt_round_trip(key_path):
    encrypted = encrypt_api_key("sk-super-secret", key_path)

    assert encrypted != "sk-super-secret"
    assert decrypt_api_key(encrypted, key_path) == "sk-super-secret"


def test_decrypt_with_wrong_key_raises(key_path, tmp_path):
    encrypted = encrypt_api_key("sk-super-secret", key_path)

    other_key_path = tmp_path / "other.key"
    with pytest.raises(LLMDatabaseError):
        decrypt_api_key(encrypted, other_key_path)
