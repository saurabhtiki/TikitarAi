class AuthDatabaseError(Exception):
    """Raised when a SQLite operation in auth.db fails (connection, integrity, etc.)."""


class DuplicateEmailError(AuthDatabaseError):
    """Raised when creating or renaming a user would collide with an existing email."""


class ProtectedAccountError(AuthDatabaseError):
    """Raised when an operation targets the seeded superuser account, which can never be
    deleted or have its role changed."""


class InvalidPasswordError(AuthDatabaseError):
    """Raised when a self-service password change is submitted with the wrong current password."""


class PhotoProcessingError(Exception):
    """Raised when an uploaded profile photo can't be read or processed. Not a database
    error, so it's kept separate from AuthDatabaseError and caught independently."""
