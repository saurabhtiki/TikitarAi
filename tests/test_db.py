import pytest

from auth.db import (
    get_user_by_email,
    get_user_by_id,
    init_db,
    seed_default_admin,
)
from auth.exceptions import AuthDatabaseError
from auth.passwords import verify_password


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "tikitarai.db"


def test_init_db_is_idempotent(db_path):
    init_db(db_path)
    init_db(db_path)  # should not raise


def test_seed_default_admin_creates_one_superuser(db_path):
    init_db(db_path)
    seed_default_admin(db_path)

    user = get_user_by_email("admin@admin.com", db_path)
    assert user is not None
    assert user["role"] == "superuser"
    assert verify_password("nimda", user["password_hash"]) is True


def test_seed_default_admin_does_not_duplicate(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    seed_default_admin(db_path)

    user = get_user_by_email("admin@admin.com", db_path)
    assert user is not None
    # Re-fetch by id and confirm there is exactly one row for this email.
    duplicate_check = get_user_by_id(user["user_id"] + 1, db_path)
    assert duplicate_check is None


def test_duplicate_email_raises(db_path):
    init_db(db_path)
    seed_default_admin(db_path)

    from auth.db import get_connection

    with pytest.raises(AuthDatabaseError):
        with get_connection(db_path) as connection:
            connection.execute(
                "INSERT INTO users (email, name, password_hash, role) VALUES (?, ?, ?, ?);",
                ("admin@admin.com", "Duplicate", "hash", "normal_user"),
            )


def test_get_user_by_email_missing_returns_none(db_path):
    init_db(db_path)
    assert get_user_by_email("nobody@example.com", db_path) is None


def test_get_user_by_id_missing_returns_none(db_path):
    init_db(db_path)
    assert get_user_by_id(999, db_path) is None


def test_get_user_by_id_round_trip(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    admin = get_user_by_email("admin@admin.com", db_path)

    fetched = get_user_by_id(admin["user_id"], db_path)
    assert fetched == admin
