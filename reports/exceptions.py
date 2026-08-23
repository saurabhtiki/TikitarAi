"""Failures from the saved report datasets (Phase 13).

One exception, deliberately: from the page's point of view "the saved data couldn't be
written" and "the saved data couldn't be read" are the same event — the report's data is
not usable right now — and both are recoverable by re-running the report.
"""


class ReportDataError(RuntimeError):
    """A report's saved dataset couldn't be written, read, or removed."""
