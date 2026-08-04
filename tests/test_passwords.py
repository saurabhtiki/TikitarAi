from auth.passwords import hash_password, verify_password


def test_hash_differs_from_plaintext():
    assert hash_password("nimda") != "nimda"


def test_hash_is_salted_differently_each_time():
    assert hash_password("nimda") != hash_password("nimda")


def test_verify_password_correct():
    assert verify_password("nimda", hash_password("nimda")) is True


def test_verify_password_incorrect():
    assert verify_password("wrong", hash_password("nimda")) is False


def test_verify_password_malformed_hash_returns_false():
    assert verify_password("nimda", "not-a-real-hash") is False
