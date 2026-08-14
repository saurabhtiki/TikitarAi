class MeetingError(Exception):
    """Base class for every failure raised by the meetings package.

    Both pages catch this one type at their boundary. On the creator side that costs the
    action and nothing else; on the invitee side it matters more, because an invitee has no
    other page to fall back to — a failure there has to render as a message on the chat
    screen rather than a traceback.
    """


class MeetingStorageError(MeetingError):
    """A meeting, invitee, session or message couldn't be read or written.

    Same reasoning as `chat_types.exceptions.ChatTypeStorageError`, with one addition: the
    message table *is* the conversation, so a failed write here is not a cosmetic loss. The
    invitee page reports it and leaves the typed message on screen rather than clearing the
    input as if it had been sent.
    """


class MeetingAccessError(MeetingError):
    """A token or access code couldn't be resolved or verified.

    Deliberately raised for failures of the *check* — a malformed token, a storage failure
    while recording an attempt. A simply-wrong access code is not an exception: it is a
    `(False, message)` return from `access.verify_code`, because being told the code is
    wrong is the normal path through that screen, not an error condition.
    """


class MeetingAgentError(MeetingError):
    """An agent call for a meeting failed in a way the caller can't paper over.

    Raised only where there is nothing usable to fall back to — the Summary Agent returning
    nothing on Close Chat, say. The per-turn chat call and the running-summary fold both
    handle their own failures instead: a fold that fails costs only freshness, and a turn
    that fails must leave the invitee's own message saved.
    """
