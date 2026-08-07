class DashboardError(Exception):
    """Base class for every failure raised by the dashboard package.

    The page catches this one type at its boundary and turns it into an `st.error`, so a
    report that can't be exported never takes the page — or the arrangement the user just
    spent ten minutes on — down with it.
    """


class ReportExportError(DashboardError):
    """A report couldn't be turned into a downloadable file.

    Raised before anything is written, so a failed export leaves no half-built workbook
    or truncated HTML behind. The message names what to change — usually "this table is
    too big for Excel" — rather than describing the writer that gave up.
    """


class StyleValidationError(DashboardError):
    """Hand-edited CSS was rejected.

    Requirement 6.4 allows the user to edit the export stylesheet but says it is accepted
    "only after validation". Raised with every problem found listed in the message, and
    the previously accepted stylesheet stays in force — a broken edit costs the change,
    never the report.
    """
