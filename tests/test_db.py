import pytest

from auth.db import (
    create_user,
    delete_user,
    get_user_by_email,
    get_user_by_id,
    init_db,
    list_users,
    seed_default_admin,
    update_user,
)
from auth.exceptions import AuthDatabaseError, DuplicateEmailError, ProtectedAccountError
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


def test_list_users_returns_all_accounts(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    create_user("bob@example.com", "Bob", "password123", "normal_user", db_path)
    create_user("alice@example.com", "Alice", "password123", "admin", db_path)

    users = list_users(db_path)

    assert len(users) == 3
    assert {user["email"] for user in users} == {"admin@admin.com", "bob@example.com", "alice@example.com"}


def test_create_user_success(db_path):
    init_db(db_path)
    seed_default_admin(db_path)

    created = create_user("bob@example.com", "Bob", "password123", "normal_user", db_path)

    assert created["role"] == "normal_user"
    fetched = get_user_by_email("bob@example.com", db_path)
    assert fetched["user_id"] == created["user_id"]
    assert verify_password("password123", fetched["password_hash"]) is True


def test_create_user_duplicate_email_raises_specific_error(db_path):
    init_db(db_path)
    seed_default_admin(db_path)

    with pytest.raises(DuplicateEmailError):
        create_user("admin@admin.com", "Duplicate", "password123", "normal_user", db_path)


def test_create_user_empty_password_raises_value_error(db_path):
    init_db(db_path)
    seed_default_admin(db_path)

    with pytest.raises(ValueError):
        create_user("bob@example.com", "Bob", "", "normal_user", db_path)


def test_update_user_success(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    created = create_user("bob@example.com", "Bob", "password123", "normal_user", db_path)

    updated = update_user(created["user_id"], "Bobby", "bobby@example.com", "admin", db_path)

    assert updated["name"] == "Bobby"
    assert updated["email"] == "bobby@example.com"
    assert updated["role"] == "admin"


def test_update_user_duplicate_email_raises(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    create_user("bob@example.com", "Bob", "password123", "normal_user", db_path)
    alice = create_user("alice@example.com", "Alice", "password123", "normal_user", db_path)

    with pytest.raises(DuplicateEmailError):
        update_user(alice["user_id"], "Alice", "bob@example.com", "normal_user", db_path)


def test_update_user_seeded_admin_role_change_blocked(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    admin = get_user_by_email("admin@admin.com", db_path)

    with pytest.raises(ProtectedAccountError):
        update_user(admin["user_id"], admin["name"], admin["email"], "admin", db_path)


def test_update_user_seeded_admin_name_email_change_allowed(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    admin = get_user_by_email("admin@admin.com", db_path)

    updated = update_user(admin["user_id"], "New Admin Name", "newadmin@example.com", "superuser", db_path)

    assert updated["name"] == "New Admin Name"
    assert updated["email"] == "newadmin@example.com"
    assert updated["role"] == "superuser"


def test_delete_user_success(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    created = create_user("bob@example.com", "Bob", "password123", "normal_user", db_path)

    delete_user(created["user_id"], db_path)

    assert get_user_by_id(created["user_id"], db_path) is None


def test_delete_user_seeded_admin_blocked(db_path):
    init_db(db_path)
    seed_default_admin(db_path)
    admin = get_user_by_email("admin@admin.com", db_path)

    with pytest.raises(ProtectedAccountError):
        delete_user(admin["user_id"], db_path)

    assert get_user_by_email("admin@admin.com", db_path) is not None


def test_delete_user_nonexistent_raises(db_path):
    init_db(db_path)
    seed_default_admin(db_path)

    with pytest.raises(AuthDatabaseError):
        delete_user(999, db_path)
