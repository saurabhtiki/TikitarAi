class LLMDatabaseError(Exception):
    """Raised when a SQLite operation on llm_profiles fails (connection, integrity,
    ownership check, etc.)."""
