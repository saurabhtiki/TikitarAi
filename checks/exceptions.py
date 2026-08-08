class ChecksError(Exception):
    """Base class for every failure raised by the checks package.

    The page catches this one type at its boundary and turns it into an `st.error` inside
    the affected criteria's own expander, so one broken rule never costs the user the other
    rules they have already written and tested.
    """


class CheckSqlError(ChecksError):
    """A criteria couldn't be turned into SQL that runs and answers the question.

    Covers the provider failing, a statement the safety guard refuses, DuckDB rejecting
    it, and — the common case — a result that doesn't carry the `criteria_met` /
    `criteria_result` columns the rest of the feature is built on. The message names what
    is wrong with the statement, because it is also what the refine loop feeds back to the
    model on the next attempt.
    """


class ChecksStorageError(ChecksError):
    """A saved criteria set couldn't be read or written.

    Unlike the dashboard, a criteria set is meant to outlive the session — so a storage
    failure is worth surfacing rather than swallowing. The in-session set is untouched by
    it: a failed save costs the save, never the work.
    """
