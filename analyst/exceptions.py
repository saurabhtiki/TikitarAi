class AnalystError(Exception):
    """Base class for every failure raised by the analyst package.

    The page catches this one type at its boundary and turns it into an assistant message
    in the transcript, so a bad question never takes the chat down.
    """


class AgentRunError(AnalystError):
    """The agent couldn't answer a question.

    Covers the provider failing, the model never running a query, and the model returning
    something the page can't render. The message is written for the person who asked the
    question, not for a log.
    """


class ChatStorageError(AnalystError):
    """The store holding past conversations couldn't be opened.

    Never fatal to the chat itself: the caller drops back to answering without history
    rather than refusing the question. Losing the record of a conversation is a smaller
    failure than losing the conversation.
    """


class ChartError(AnalystError):
    """A result set couldn't be turned into a chart.

    Raised only when a chart was explicitly asked for. When one is merely offered
    alongside other output, `charts.build_chart` returns None instead — a missing chart
    should not cost the user their table and commentary.
    """
