"""Pinning a loaded table into a report, and telling those items apart afterwards.

A report item can come from three places, and the difference matters:

* a **criteria** on a producer page, which owns its item and rewrites its wording on every
  run — the user may not discard it;
* a **loaded table**, pinned here — the user's to title, comment, place and discard;
* an answer the user pinned by hand, which has no source at all.

The middle one is what this module is for. It holds no reading logic: the tables are
already loaded in DuckDB, cleaned and typed, and re-reading the original workbook would
produce a second, subtly different copy of the same numbers. All that is needed is a stable
name for each pinned table, so that loading next month's file and pinning it again refreshes
the numbers under the title and comment already written above them.

Charts and pivot tables are deliberately absent. Excel stores a chart as a recipe rather
than as a picture, so importing one could only ever mean redrawing it, and a redrawn chart
does not match the workbook it came from; a pivot table flattened into a plain frame does
not look like a pivot either. Both are brought in by hand instead, as an image or as an
HTML block.
"""

import logging

logger = logging.getLogger(__name__)

# Every item pinned from a loaded table carries a `source_id` starting with this.
SOURCE_PREFIX = "loaded:"

# What the same items were called before they came from loaded tables rather than from an
# uploaded workbook. Read but never written: a report built then still has these ids in it,
# and they must keep counting as imported, or its items would lose their Discard button.
LEGACY_PREFIX = "excel:"

# The most rows one pinned table may carry. Far above anything a person reads — the report
# shows the first `PREVIEW_ROWS` and the Excel download gets every row this holds — and low
# enough that pinning a few large tables cannot fill the session on its own.
ROW_CEILING = 200_000

# How many rows of a pinned table are shown on screen and in the HTML report. The Excel
# download is not capped: that is the file a person actually works in.
PREVIEW_ROWS = 500


def source_key(table_name: str) -> str:
    """The stable identity of one pinned table.

    Built from the table's name rather than from the file it arrived in, because the name is
    what survives a reload: removing `Sales.xlsx` and adding a corrected copy gives Streamlit
    a brand new file id, but the table is still called `Sales`, and pinning it again should
    refresh the item the user has already titled rather than pin a second one beside it.
    """
    return f"{SOURCE_PREFIX}{table_name}"


def is_imported(source_id: str | None) -> bool:
    """Whether a pinned item came from a loaded table rather than from a producer page."""
    if not source_id:
        return False
    return str(source_id).startswith((SOURCE_PREFIX, LEGACY_PREFIX))


def truncation_note(pinned_rows: int, total_rows: int) -> str:
    """What to say under a table that holds fewer rows than the table it came from."""
    return (
        f"Only the first {pinned_rows:,} of {total_rows:,} rows were pinned. "
        "Summarise the table first if you need the rest."
    )
