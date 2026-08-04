class AuthDatabaseError(Exception):
    """Raised when a SQLite operation in auth.db fails (connection, integrity, etc.)."""
