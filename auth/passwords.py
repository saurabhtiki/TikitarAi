import logging

import bcrypt

logger = logging.getLogger(__name__)


def hash_password(plain_password: str) -> str:
    """Hashes a plaintext password with bcrypt, using a freshly generated salt.

    Raises:
        ValueError: if `plain_password` is empty.
    """
    if not plain_password:
        raise ValueError("Password must not be empty.")
    try:
        hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
        return hashed.decode("utf-8")
    except Exception:
        logger.exception("Failed to hash password.")
        raise


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Checks a plaintext password against a stored bcrypt hash.

    Returns False (rather than raising) for malformed or empty input, since a
    verification failure should read to callers the same as a wrong password.
    """
    if not plain_password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        logger.warning("Encountered malformed password hash during verification.")
        return False
