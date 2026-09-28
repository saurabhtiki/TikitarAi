"""For each lists: questions repeated for every row of an uploaded list (phase 52).

A list is stored like a Table item's sheet (`AgendaTable`, keyed by the list's name), but it
is never shown as a grid: its rows are walked through in the chat by `steps.walk`, and each
answer is written back against the row it was about. No Streamlit and no SQL here.
"""

import logging
from dataclasses import dataclass, field
from io import BytesIO

import pandas as pd

from meetings import steps
from meetings.exceptions import MeetingStorageError
from meetings.model import AgendaTable, Meeting, StepAnswer

logger = logging.getLogger(__name__)

INVITEE_COLUMN = "Invitee"


@dataclass
class InviteeLists:
    """One invitee's lists: the sheet behind each (by `steps.loop_key`) and their rows in it."""

    tables: dict[str, AgendaTable] = field(default_factory=dict)
    rows: dict[str, list[int]] = field(default_factory=dict)

    def row_context(self, step: steps.Step | None) -> str:
        """The row a step is about, as "Bill No: 1001; Amount: 5000", or "" for an ordinary step."""
        if step is None or step.row is None:
            return ""
        table = self.tables.get(steps.loop_key(step.question.loop))
        if table is None or not 0 <= step.row < len(table.base_data):
            return ""
        values = table.base_data[step.row]
        return "; ".join(f"{column}: {values.get(column, '')}" for column in table.all_columns())


def list_tables(meeting: Meeting, agenda_tables: list[AgendaTable]) -> dict[str, AgendaTable]:
    """The sheets behind this meeting's For each lists, by `steps.loop_key`.

    A sheet left behind by a list that was renamed or removed is not included.
    """
    wanted = {steps.loop_key(name) for name in steps.loop_names(meeting)}
    return {
        steps.loop_key(table.item_ref): table
        for table in agenda_tables
        if steps.loop_key(table.item_ref) in wanted
    }


def _same_person(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def rows_for_invitee(table: AgendaTable, invitee: dict) -> list[int]:
    """The row numbers this invitee is asked about.

    With a match column, only rows whose value there is the invitee's name or email (case and
    spacing ignored); without one, every row.
    """
    column = (table.match_column or "").strip()
    if not column:
        return list(range(len(table.base_data)))
    wanted = {_same_person(invitee.get("name")), _same_person(invitee.get("email"))} - {""}
    return [index for index, row in enumerate(table.base_data) if _same_person(row.get(column)) in wanted]


def invitee_lists(tables: dict[str, AgendaTable], invitee: dict) -> InviteeLists:
    """`InviteeLists` for one invitee, from the meeting's list sheets keyed by `steps.loop_key`."""
    return InviteeLists(
        tables=tables,
        rows={key: rows_for_invitee(table, invitee) for key, table in tables.items()},
    )


def rows_done(meeting: Meeting, name: str, walk: steps.Walk, answers: dict[str, StepAnswer], rows: list[int]) -> int:
    """How many of these rows have every question answered, given up on, or jumped over."""
    skipped = {step.key for step in walk.skipped}
    questions = steps.loop_questions(meeting, name)
    done = 0
    for row in rows:
        keys = [steps.answer_key(item.item, row) for item in questions]
        if all(key in skipped or (key in answers and answers[key].is_finished()) for key in keys):
            done += 1
    return done


def progress_label(meeting: Meeting, name: str, walk: steps.Walk, answers: dict[str, StepAnswer], rows: list[int]) -> str:
    """ "Outstanding invoices: 3 of 12 row(s) done", or "… skipped" when a Go to jumped over it all."""
    if not rows:
        return f"{name}: no rows for this invitee"
    skipped = {step.key for step in walk.skipped}
    questions = steps.loop_questions(meeting, name)
    if all(steps.answer_key(item.item, row) in skipped for item in questions for row in rows):
        return f"{name}: skipped"
    return f"{name}: {rows_done(meeting, name, walk, answers, rows)} of {len(rows)} row(s) done"


def results_frame(
    meeting: Meeting,
    name: str,
    tables: dict[str, AgendaTable],
    invitees: list[dict],
    all_answers: dict[int, dict[str, StepAnswer]],
) -> pd.DataFrame:
    """The list with its answers written back: one row per invitee per list row.

    The list's own columns first, then one column per question. A cell a Go to jumped over
    reads "— skipped"; one not reached yet is blank. `tables` is every list sheet of the
    meeting, because an invitee's path runs through all of them.
    """
    key = steps.loop_key(name)
    table = tables[key]
    questions = steps.loop_questions(meeting, name)
    columns = [INVITEE_COLUMN, *table.all_columns(), *[item.item for item in questions]]
    records = []
    for invitee in invitees:
        answers = all_answers.get(invitee["invitee_id"], {})
        lists = invitee_lists(tables, invitee)
        walk = steps.walk(meeting, answers, lists.rows)
        skipped = {step.key for step in walk.skipped}
        for row in lists.rows[key]:
            record = {INVITEE_COLUMN: invitee["name"]}
            record.update({column: str(table.base_data[row].get(column, "")) for column in table.all_columns()})
            for item in questions:
                answer_key = steps.answer_key(item.item, row)
                record[item.item] = (
                    steps.SKIPPED_CELL if answer_key in skipped else steps.display_answer(answers.get(answer_key))
                )
            records.append(record)
    return pd.DataFrame(records, columns=columns)


def to_excel_bytes(frame: pd.DataFrame, sheet_name: str) -> bytes:
    """The results as an .xlsx file.

    Raises:
        MeetingStorageError: if the file can't be written.
    """
    buffer = BytesIO()
    try:
        # Excel sheet names are at most 31 characters and can't hold some punctuation.
        safe_name = "".join(ch for ch in sheet_name if ch not in "[]:*?/\\")[:31] or "List"
        frame.to_excel(buffer, index=False, sheet_name=safe_name, engine="openpyxl")
    except (ValueError, OSError, ImportError) as error:
        logger.exception("Could not write the list answers for '%s' to Excel.", sheet_name)
        raise MeetingStorageError(f"Could not build the Excel file: {error}") from error
    return buffer.getvalue()
