"""Finding a meeting on the My meetings list (phase 56).

A text search on the subject and a status filter, like Find a report on Chat with reports.
No Streamlit and no database; the page passes in the rows `db.list_meetings` returns.
"""

import datetime
import logging

from utils.dates import DATE_TEXT_FORMAT

logger = logging.getLogger(__name__)

STATUS_ALL = "All"
STATUS_WAITING = "Waiting on invitees"
STATUS_FINISHED = "All finished"
STATUS_NO_INVITEES = "No invitees"
STATUS_OPTIONS = [STATUS_ALL, STATUS_WAITING, STATUS_FINISHED, STATUS_NO_INVITEES]


def status_of(row: dict) -> str:
    """One meeting's status, from its invitee and finished counts."""
    invitees, finished = int(row.get("invitee_count") or 0), int(row.get("closed_count") or 0)
    if invitees == 0:
        return STATUS_NO_INVITEES
    return STATUS_FINISHED if finished >= invitees else STATUS_WAITING


def filter_meetings(rows: list[dict], query: str, status: str = STATUS_ALL) -> list[dict]:
    """The meetings whose subject contains `query` (any case) and that have `status`."""
    needle = str(query or "").strip().casefold()
    return [
        row
        for row in rows
        if needle in str(row.get("subject") or "").casefold()
        and (status == STATUS_ALL or status_of(row) == status)
    ]


def created_on(created_at: str) -> str:
    """A stored timestamp as dd-mm-yyyy, or the raw text if it doesn't parse."""
    try:
        return datetime.datetime.strptime(str(created_at)[:10], "%Y-%m-%d").strftime(DATE_TEXT_FORMAT)
    except (TypeError, ValueError):
        logger.warning("Could not read the timestamp %r.", created_at)
        return str(created_at or "")
