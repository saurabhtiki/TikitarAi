import sqlite3

import pytest

from auth.db import create_user, init_db, seed_default_admin
from llm.crypto import decrypt_api_key
from llm.db import (
    create_profile,
    create_profiles,
    delete_profile,
    init_llm_table,
    list_profiles,
    set_default_model,
    set_light_model,
    unset_default_model,
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


def test_create_profiles_saves_one_row_per_model(db_path):
    created, failure = create_profiles(
        1, "OpenRouter", "cloud", "https://openrouter.ai/api/v1", "sk-secret", ["gpt-4o-mini", "gpt-4o"], db_path
    )

    assert failure is None
    assert len(created) == 2
    profiles = list_profiles(1, db_path)
    assert [profile["nickname"] for profile in profiles] == ["OpenRouter — gpt-4o", "OpenRouter — gpt-4o-mini"]
    assert sorted(profile["default_model"] for profile in profiles) == ["gpt-4o", "gpt-4o-mini"]
    assert {profile["base_url"] for profile in profiles} == {"https://openrouter.ai/api/v1"}
    key_path = db_path.with_name("encryption.key")
    assert {decrypt_api_key(profile["api_key_encrypted"], key_path) for profile in profiles} == {"sk-secret"}


def test_create_profiles_with_one_model_keeps_the_plain_nickname(db_path):
    created, failure = create_profiles(1, "My OpenAI", "cloud", None, "sk-secret", ["gpt-4o-mini"], db_path)

    assert failure is None
    assert created[0]["nickname"] == "My OpenAI"
    assert created[0]["default_model"] == "gpt-4o-mini"


def test_create_profiles_reports_how_far_it_got(db_path, monkeypatch):
    """There is no transaction across the rows, so a failure part-way must report the rows
    that were saved rather than look like nothing happened."""
    real_create = create_profile

    def fail_on_the_second(*args, **kwargs):
        if args[1].endswith("gpt-4o"):
            raise LLMDatabaseError("boom")
        return real_create(*args, **kwargs)

    monkeypatch.setattr("llm.db.create_profile", fail_on_the_second)

    created, failure = create_profiles(1, "OpenRouter", "cloud", None, None, ["gpt-4o-mini", "gpt-4o"], db_path)

    assert failure == "boom"
    assert [profile["nickname"] for profile in created] == ["OpenRouter — gpt-4o-mini"]
    assert len(list_profiles(1, db_path)) == 1


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


def test_set_default_model_only_one_flagged_at_a_time(db_path):
    first = create_profile(1, "Profile A", "local", "http://localhost:1234", None, "llama-3", db_path)
    second = create_profile(1, "Profile B", "local", "http://localhost:5678", None, "phi-3", db_path)

    set_default_model(first["profile_id"], 1, db_path)
    set_default_model(second["profile_id"], 1, db_path)

    profiles = {profile["profile_id"]: profile for profile in list_profiles(1, db_path)}
    assert profiles[first["profile_id"]]["is_default_model"] == 0
    assert profiles[second["profile_id"]]["is_default_model"] == 1


def test_unset_default_model(db_path):
    created = create_profile(1, "Profile A", "local", "http://localhost:1234", None, "llama-3", db_path)
    set_default_model(created["profile_id"], 1, db_path)

    unset_default_model(created["profile_id"], 1, db_path)

    assert list_profiles(1, db_path)[0]["is_default_model"] == 0


def test_set_default_model_clears_the_light_flag(db_path):
    """A profile can't be both: session_profiles hides the light model from the picker, so a
    light-and-default profile would be a default nothing could select."""
    created = create_profile(1, "Profile A", "local", "http://localhost:1234", None, "llama-3", db_path)
    set_light_model(created["profile_id"], 1, db_path)

    set_default_model(created["profile_id"], 1, db_path)

    profile = list_profiles(1, db_path)[0]
    assert profile["is_default_model"] == 1
    assert profile["is_light_model"] == 0


def test_set_light_model_clears_the_default_flag(db_path):
    created = create_profile(1, "Profile A", "local", "http://localhost:1234", None, "llama-3", db_path)
    set_default_model(created["profile_id"], 1, db_path)

    set_light_model(created["profile_id"], 1, db_path)

    profile = list_profiles(1, db_path)[0]
    assert profile["is_light_model"] == 1
    assert profile["is_default_model"] == 0


def test_set_default_model_not_owned_by_user_raises(db_path):
    created = create_profile(1, "User1 profile", "local", "http://localhost:1234", None, "llama-3", db_path)

    with pytest.raises(LLMDatabaseError):
        set_default_model(created["profile_id"], 2, db_path)

    assert list_profiles(1, db_path)[0]["is_default_model"] == 0


def test_init_llm_table_adds_is_default_column_to_an_existing_table(tmp_path):
    """Databases created before the default-model flag existed must gain the column, with
    existing rows reading 0 — nobody had designated a default yet."""
    path = tmp_path / "legacy.db"
    init_db(path)
    seed_default_admin(path)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE llm_profiles (
               profile_id        INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id           INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
               nickname          TEXT NOT NULL,
               provider_type     TEXT NOT NULL CHECK (provider_type IN ('local', 'cloud', 'custom')),
               base_url          TEXT,
               api_key_encrypted TEXT,
               default_model     TEXT NOT NULL,
               is_light_model    INTEGER NOT NULL DEFAULT 0,
               created_at        TEXT NOT NULL DEFAULT (datetime('now'))
           );"""
    )
    connection.execute(
        """INSERT INTO llm_profiles (user_id, nickname, provider_type, base_url, default_model)
           VALUES (1, 'Old profile', 'local', 'http://localhost:1234', 'llama-3');"""
    )
    connection.commit()
    connection.close()

    init_llm_table(path)

    profiles = list_profiles(1, path)
    assert profiles[0]["is_default_model"] == 0
    set_default_model(profiles[0]["profile_id"], 1, path)
    assert list_profiles(1, path)[0]["is_default_model"] == 1
