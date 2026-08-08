class ChatTypeError(Exception):
    """Base class for every failure raised by the chat_types package.

    The page catches this one type at its boundary, so a chat type that can't be read
    costs the user the chat type and nothing else — the tables they just uploaded stay
    loaded and still queryable without one.
    """


class ChatTypeStorageError(ChatTypeError):
    """A saved chat type couldn't be read or written.

    Same reasoning as `checks.exceptions.ChecksStorageError`: a chat type exists precisely
    to outlive the session, so a storage failure is worth surfacing rather than swallowing.
    A failed save costs the save, never the setup on screen.
    """


class ChatTypeMatchError(ChatTypeError):
    """An upload can't be reconciled with the chat type it was checked against.

    Raised only for failures of the check itself (a table that can't be described, a
    column that can't be dropped). An upload that simply *doesn't match* is not an
    exception — it is a `matching.MatchReport` with blocking problems in it, because the
    whole point is to show the user every problem at once rather than the first one.
    """
