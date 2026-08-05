class DataEngineError(Exception):
    """Base class for every failure raised by the engine package."""


class TableLoadError(DataEngineError):
    """An uploaded table couldn't be prepared or registered into DuckDB."""


class UnsafeSqlError(DataEngineError):
    """SQL was rejected by the guardrails before it ever reached DuckDB.

    Requirements 5.5 scopes agent- and user-written SQL strictly to the session's own
    tables. This is raised at the guard, never after execution, so the rejected
    statement has no chance to touch the filesystem or another table.
    """


class RelationshipError(DataEngineError):
    """A relationship couldn't be established between two tables.

    Carries the offending rows where there are any, because requirements 5.2 asks for
    the exact rows in full rather than a bare count — that is what makes the problem
    findable in the source file.
    """

    def __init__(self, message: str, offending_rows=None):
        super().__init__(message)
        self.offending_rows = offending_rows


class CalculatedColumnError(DataEngineError):
    """A calculated column couldn't be added or removed.

    The message carries DuckDB's own wording (`Binder Error: Referenced column "bsic"
    not found`), which names the actual mistake far better than anything this app
    could paraphrase.
    """
