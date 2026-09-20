class LiveDashboardError(Exception):
    """Base class for every failure raised by the live_dashboard package.

    The view catches this one type at its boundary and turns it into an `st.error`, so a
    dashboard that can't be built costs the preview and nothing else — the spec the user
    typed stays on screen to be corrected.
    """


class DashboardStorageError(LiveDashboardError):
    """A stored dashboard spec couldn't be read back."""


class DashboardDataError(LiveDashboardError):
    """The dashboard's data couldn't be assembled.

    Raised by `flatten`, where a join the confirmed relationships don't support, or a query
    DuckDB refuses, stops the export before a half-built payload reaches the page.
    """


class DashboardExportError(LiveDashboardError):
    """The HTML file couldn't be rendered.

    Separate from `DashboardDataError` because the two fail at different moments and mean
    different things to the user: one says "your data won't assemble", the other says "the
    data was fine and the page wouldn't build" — most often a missing vendored bundle.
    """
