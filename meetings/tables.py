"""The grid behind a table agenda item (requirement 6.7, Phase 2, spec 3a).

Everything that turns an uploaded sheet into something an invitee can fill in, and their
edits back into rows to store. No Streamlit and no SQL, so the arithmetic that decides "12 of
40 rows updated" — which the status list, the MoM and the comparison matrix all quote — can
be tested without either.

The row's identity is its **position in `base_data`**, not a value in it. A bill number would
be a nicer key right up to the first sheet that repeats one, and a duplicate key here would
silently merge two invitees' answers to two different rows.
"""

import logging
from io import BytesIO
from pathlib import Path

import pandas as pd

from meetings.exceptions import MeetingStorageError
from meetings.model import AgendaTable

logger = logging.getLogger(__name__)

CSV_SUFFIXES = (".csv", ".txt")
EXCEL_SUFFIXES = (".xlsx", ".xls", ".xlsm")

SUPPORTED_SUFFIXES = CSV_SUFFIXES + EXCEL_SUFFIXES

# A sheet big enough to be a data export rather than an agenda item. Spec 3a sizes these at
# "30-40+ rows"; the cap is well clear of that and exists so one wrong upload can't put a
# hundred thousand rows into a JSON column and a browser grid.
MAX_ROWS = 2000


def read_source(data: bytes, filename: str) -> pd.DataFrame:
    """The creator's uploaded sheet as a frame, with every value read as text.

    Text on purpose. A column of bill numbers that pandas decides is an integer comes back
    as `1001.0` once one row is blank, and the invitee is then reading a reference number
    that doesn't match their own records. Nothing here does arithmetic on these values —
    they are a question being asked and an answer being given — so the honest type is the
    one the file literally contains.

    Raises:
        MeetingStorageError: if the file can't be read, is empty, or is too large.
    """
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise MeetingStorageError(
            f"'{filename}' isn't a supported table file. Upload a .csv or .xlsx."
        )

    try:
        if suffix in CSV_SUFFIXES:
            frame = pd.read_csv(BytesIO(data), dtype=str, keep_default_na=False)
        else:
            frame = pd.read_excel(BytesIO(data), dtype=str, keep_default_na=False)
    except (ValueError, OSError, pd.errors.ParserError) as error:
        logger.exception("Could not read the uploaded table '%s'.", filename)
        raise MeetingStorageError(f"Could not read '{filename}': {error}") from error

    frame = frame.fillna("")
    frame.columns = [str(column).strip() for column in frame.columns]

    if frame.empty or not len(frame.columns):
        raise MeetingStorageError(f"'{filename}' has no rows to fill in.")
    if len(frame) > MAX_ROWS:
        raise MeetingStorageError(
            f"'{filename}' has {len(frame)} rows — more than the {MAX_ROWS} an agenda table holds."
        )

    return frame


def base_data_from_frame(frame: pd.DataFrame) -> list[dict]:
    """The uploaded sheet as the row dicts `AgendaTable.base_data` stores."""
    return [
        {str(column): str(value) for column, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def display_frame(table: AgendaTable, responses: dict[int, dict]) -> pd.DataFrame:
    """The grid as the invitee sees it: locked columns, then their own answers.

    Built from `base_data` every time rather than from anything stored per invitee, so the
    locked columns are always the creator's — an invitee cannot end up looking at a bill
    amount their own earlier edit changed.
    """
    rows = []
    for index, base_row in enumerate(table.base_data):
        row = {column: str(base_row.get(column, "")) for column in table.locked_columns}
        answers = responses.get(index, {})
        for column in table.editable_columns:
            row[column] = str(answers.get(column, ""))
        rows.append(row)

    columns = table.all_columns()
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


def responses_from_frame(table: AgendaTable, frame: pd.DataFrame) -> dict[int, dict]:
    """The invitee's edits, as `{row_index: {column: value}}`.

    Only the editable columns are read back. An edit to a locked column is discarded rather
    than rejected: `st.data_editor` is told to disable them, so a value arriving in one is
    not a user decision to honour.

    Rows are matched by position, and only the first `len(base_data)` are read — a grid is
    `num_rows="fixed"`, so extra rows mean something has gone wrong upstream and inventing
    answers to rows the creator never asked about is the worse of the two failures.
    """
    if not table.editable_columns:
        return {}

    answers: dict[int, dict] = {}
    records = frame.to_dict(orient="records")
    for index, row in enumerate(records[: len(table.base_data)]):
        values = {
            column: str(row.get(column, "") or "").strip()
            for column in table.editable_columns
            if str(row.get(column, "") or "").strip()
        }
        if values:
            answers[index] = values
    return answers


def completion(table: AgendaTable, filled_rows: int) -> tuple[int, int, int]:
    """`(filled, total, percent)` for one invitee's progress through one grid.

    Spec 3a tracks a table item as a percentage of rows updated rather than as
    Discussed/Not Discussed. A grid with no rows reads as 0%, not as complete: an empty
    table is a setup that isn't finished, and calling it done would put a green tick against
    an item nobody could have answered.
    """
    total = table.row_count()
    filled = max(0, min(int(filled_rows), total))
    percent = int(round(filled * 100 / total)) if total else 0
    return filled, total, percent


def format_completion(filled: int, total: int) -> str:
    """The one-line progress wording, from counts alone.

    Separate from `completion_label` because the MoM builds this from stored counts and has
    no `AgendaTable` in hand — and the status list, the MoM and the matrix quoting three
    different phrasings of the same number would read as three different numbers.
    """
    percent = int(round(filled * 100 / total)) if total else 0
    return f"{filled} of {total} row(s) filled ({percent}%)"


def completion_label(table: AgendaTable, filled_rows: int) -> str:
    """The one-line progress wording for one invitee's progress through one grid."""
    filled, total, _ = completion(table, filled_rows)
    return format_completion(filled, total)
