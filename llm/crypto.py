import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from llm.exceptions import LLMDatabaseError

logger = logging.getLogger(__name__)

DEFAULT_KEY_PATH = Path("data") / "encryption.key"


def _load_or_create_key(key_path: Path | str = DEFAULT_KEY_PATH) -> bytes:
    """Loads the app's Fernet encryption key, generating and persisting one on first use.

    Raises:
        LLMDatabaseError: if the key file can't be read or written.
    """
    key_path = Path(key_path)
    try:
        if key_path.exists():
            return key_path.read_bytes()

        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        logger.info("Generated a new encryption key at %s.", key_path)
        return key
    except OSError as error:
        logger.exception("Failed to load or create the encryption key at %s.", key_path)
        raise LLMDatabaseError("Could not access the encryption key needed to store API keys.") from error


def encrypt_api_key(plain_key: str, key_path: Path | str = DEFAULT_KEY_PATH) -> str:
    """Encrypts an API key for storage. Raises LLMDatabaseError on any failure."""
    try:
        fernet = Fernet(_load_or_create_key(key_path))
        return fernet.encrypt(plain_key.encode("utf-8")).decode("utf-8")
    except LLMDatabaseError:
        raise
    except Exception as error:
        logger.exception("Failed to encrypt an API key.")
        raise LLMDatabaseError("Could not encrypt the API key.") from error


def decrypt_api_key(encrypted_key: str, key_path: Path | str = DEFAULT_KEY_PATH) -> str:
    """Decrypts a stored API key. Raises LLMDatabaseError on any failure."""
    try:
        fernet = Fernet(_load_or_create_key(key_path))
        return fernet.decrypt(encrypted_key.encode("utf-8")).decode("utf-8")
    except LLMDatabaseError:
        raise
    except InvalidToken as error:
        logger.exception("Failed to decrypt a stored API key: invalid token.")
        raise LLMDatabaseError("Could not decrypt the stored API key.") from error
