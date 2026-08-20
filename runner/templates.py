"""Blank files shaped like the ones a Task expects (build-order item 10's sample-file generator).

The build order asks for one; §8 never says what it is. Read against §8.1 steps 2–4, it can
only be this: the schema dialog says which columns to have ready, and a **file with exactly
those headers** is that same answer in the form the user actually needs it — something to
paste this month's data into, or to hand to whoever exports it from the source system.

One file per expected table, because that is how a Task's schema is shaped and how the upload
is matched: a workbook of sheets would be a second layout to explain, and `sync_tables` would
still load it as several tables.

**Headers only, and no example row.** A file that arrives with a made-up employee in it is a
file somebody eventually uploads with the made-up employee still in it. The types are said in
the schema dialog beside the download, where they are words rather than fake data.

No Streamlit here, so a template is testable as the bytes it is.
"""

import csv
import io
import logging

from chat_types.model import ExpectedTable

logger = logging.getLogger(__name__)


def template_csv(table: ExpectedTable) -> bytes:
    """One expected table as a headers-only CSV, encoded as the loader reads it.

    UTF-8 with a BOM, deliberately: this file's whole purpose is to be opened in Excel and
    filled in, and Excel reads a plain UTF-8 CSV as the current code page — which turns a
    column named `Année` into mojibake before the user has typed anything. `cleaner.loaders`
    decodes the BOM back off on the way in.

    `\\r\\n` line endings for the same reason, which is `csv`'s own default.
    """
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow(table.column_names)
    return buffer.getvalue().encode("utf-8-sig")


def template_name(table: ExpectedTable) -> str:
    """What the downloaded template is called.

    Named after the **table**, not after the file it was recorded from, because the table
    name is what the upload is matched on — a template that downloads under the recipe's own
    name for it uploads back as a match with nothing to remap.
    """
    return f"{table.table_name}.csv"
