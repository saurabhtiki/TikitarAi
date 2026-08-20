class ReportItemError(Exception):
    """Base class for every failure raised by the report_items package.

    The view catches this one type at its boundary and turns it into an `st.error` beside
    the item that failed, so one bad item never takes the other twenty — or the Task being
    assembled around them — down with it.
    """


class ReportItemSqlError(ReportItemError):
    """A report item's SQL couldn't be written, wouldn't run, or came back unusable.

    The message is written to be read by the user *and* fed straight back to the model on
    the next attempt: `sql_builder.generate_and_run` does exactly that with it, which is
    what makes regenerating a refinement rather than a fresh guess.
    """


class ColumnStepError(ReportItemError):
    """A column step couldn't be applied.

    Wraps whatever `analyst.column_intent` raised. Separate from `ReportItemSqlError`
    because the two fail differently: a report item that won't run leaves the data
    untouched and is retried freely, while a column step changes the tables every later
    item reads — so a failure here is reported and the step stays unapplied rather than
    half-applied.
    """


class ReportItemStorageError(ReportItemError):
    """A stored list of report items couldn't be read back."""
