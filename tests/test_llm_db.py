import pytest

from auth.db import create_user, init_db, seed_default_admin
from llm.crypto import decrypt_api_key
from llm.db import (
    create_profile,
    delete_profile,
    init_llm_table,
    list_profiles,
    set_light_model,
    unset_light_model,
    update_profile,
)
from llm.exceptions import LLMDatabaseError


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "tikitarai.db"
    init_db(path)
    init_llm_table(path)
    seed_default_admin(path)  # user_id 1
    create_user("bob@example.com", "Bob", "password123", "normal_user", path)  # user_id 2
    return path


def test_create_profile_local_has_no_key(db_path):
    created = create_profile(1, "LM Studio", "local", "http://localhost:1234", None, "llama-3", db_path)

    assert created["provider_type"] == "local"
    assert created["api_key_encrypted"] is None


def test_create_profile_encrypts_api_key(db_path):
    created = create_profile(1, "My OpenAI", "cloud", None, "sk-secret", "gpt-4o-mini", db_path)

    assert created["api_key_encrypted"] != "sk-secret"
    key_path = db_path.with_name("encryption.key")
    assert decrypt_api_key(created["api_key_encrypted"], key_path) == "sk-secret"


def test_list_profiles_scoped_to_owner(db_path):
    create_profile(1, "User1 profile", "local", "http://localhost:1234", None, "llama-3", db_path)
    create_profile(2, "User2 profile", "local", "http://localhost:5678", None, "phi-3", db_path)

    user1_profiles = list_profiles(1, db_path)

    assert len(user1_profiles) == 1
    assert user1_profiles[0]["nickname"] == "User1 profile"


def test_update_profile_none_api_key_leaves_it_unchanged(db_path):
    created = create_profile(1, "My OpenAI", "cloud", None, "sk-secret", "gpt-4o-mini", db_path)

    updated = update_profile(created["profile_id"], 1, "My OpenAI v2", "cloud", None, None, "gpt-4o", db_path)

    assert updated["nickname"] == "My OpenAI v2"
    assert updated["api_key_encrypted"] == created["api_key_encrypted"]


def test_update_profile_empty_string_clears_api_key(db_path):
    created = create_profile(1, "My OpenAI", "cloud", None, "sk-secret", "gpt-4o-mini", db_path)

    updated = update_profile(created["profile_id"], 1, "My OpenAI", "cloud", None, "", "gpt-4o-mini", db_path)

    assert updated["api_key_encrypted"] is None


def test_update_profile_not_owned_by_user_raises(db_path):
    created = create_profile(1, "User1 profile", "local", "http://localhost:1234", None, "llama-3", db_path)

    with pytest.raises(LLMDatabaseError):
        update_profile(created["profile_id"], 2, "Hijacked", "local", "http://localhost:1234", None, "llama-3", db_path)


def test_delete_profile_not_owned_by_user_raises(db_path):
    created = create_profile(1, "User1 profile", "local", "http://localhost:1234", None, "llama-3", db_path)

    with pytest.raises(LLMDatabaseError):
        delete_profile(created["profile_id"], 2, db_path)

    assert len(list_profiles(1, db_path)) == 1


def test_delete_profile_success(db_path):
    created = create_profile(1, "User1 profile", "local", "http://localhost:1234", None, "llama-3", db_path)

    delete_profile(created["profile_id"], 1, db_path)

    assert list_profiles(1, db_path) == []


def test_set_light_model_only_one_flagged_at_a_time(db_path):
    first = create_profile(1, "Profile A", "local", "http://localhost:1234", None, "llama-3", db_path)
    second = create_profile(1, "Profile B", "local", "http://localhost:5678", None, "phi-3", db_path)

    set_light_model(first["profile_id"], 1, db_path)
    set_light_model(second["profile_id"], 1, db_path)

    profiles = {profile["profile_id"]: profile for profile in list_profiles(1, db_path)}
    assert profiles[first["profile_id"]]["is_light_model"] == 0
    assert profiles[second["profile_id"]]["is_light_model"] == 1


def test_unset_light_model(db_path):
    created = create_profile(1, "Profile A", "local", "http://localhost:1234", None, "llama-3", db_path)
    set_light_model(created["profile_id"], 1, db_path)

    unset_light_model(created["profile_id"], 1, db_path)

    profiles = list_profiles(1, db_path)
    assert profiles[0]["is_light_model"] == 0
