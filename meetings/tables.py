"""The grid behind a table agenda item (requirement 6.7, Phase 2, spec 3a).

Everything that turns an uploaded sheet into something an invitee can fill in, and their
edits back into rows to store. No Streamlit and no SQL, so the arithmetic that decides "12 of
40 rows updated" — which the status list, the MoM and the comparison matrix all quote — can
be tested without either.

Phase 59: each editable column can have a Type, a Rule and Required (`ColumnRule`), checked
with the same code as a Question (`steps.check_answer`). Number and Date columns go into the
grid as real numbers and dates, so the grid itself refuses letters and offers a calendar.

The row's identity is its **position in `base_data`**, not a value in it. A bill number would
be a nicer key right up to the first sheet that repeats one, and a duplicate key here would
silently merge two invitees' answers to two different rows.
"""

import datetime
import logging
import math
from io import BytesIO
from pathlib import Path

import pandas as pd

from meetings import steps
from meetings.exceptions import MeetingStorageError
from meetings.model import ANSWER_DATE, ANSWER_NUMBER, QUESTION_ITEM, AgendaItem, AgendaTable, ColumnRule

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


def column_question(column: str, rule: ColumnRule) -> AgendaItem:
    """A column rule dressed as a Question, so `steps` can read and check it."""
    return AgendaItem(item=column, item_type=QUESTION_ITEM, answer_type=rule.answer_type, rule=rule.rule)


def column_rule_problems(rules: dict[str, ColumnRule]) -> list[str]:
    """Why these column rules can't be saved, e.g. "Status: a Choice question needs at least two options"."""
    problems = []
    for column, rule in rules.items():
        problem = steps.rule_problem(column_question(column, rule))
        if problem:
            # `rule_problem` speaks of questions; only its wording after "Column: " is changed.
            problems.append(f"{column}: " + problem.removeprefix(f"{column}: ").replace("question", "column"))
    return problems


def _fits_type(rule: ColumnRule, value: str) -> bool:
    if rule.answer_type == ANSWER_NUMBER:
        return steps.parse_number(value) is not None
    if rule.answer_type == ANSWER_DATE:
        return steps.parse_date(value) is not None
    return True


def unreadable_answers(rules: dict[str, ColumnRule], all_responses: dict[int, dict[int, dict]]) -> list[str]:
    """Saved answers a new Number or Date type can't show, e.g. "Expected price: 'approx 40k'".

    The grid would show them blank and the invitee's next save would wipe them, so a type
    change that hits one is refused. A changed Rule is fine: the answer still shows, and is
    checked on the next save.
    """
    found = []
    for rows in all_responses.values():
        for values in rows.values():
            for column, value in values.items():
                if not _fits_type(rules.get(column) or ColumnRule(), str(value)):
                    found.append(f"{column}: '{value}'")
    return found


def describe_column(column: str, rule: ColumnRule) -> str:
    """ "Expected price (a number more than 0, required)" — for the bot and the How to use steps.
    A plain Text column with no rule is just its name."""
    if rule == ColumnRule():
        return column
    words = steps.describe_rule(column_question(column, rule))
    return f"{column} ({words}{', required' if rule.required else ''})"


def _grid_value(rule: ColumnRule, text: str):
    """A stored answer in the grid's own type: a number, a date, or text. Unreadable = blank."""
    if rule.answer_type == ANSWER_NUMBER:
        number = steps.parse_number(text) if text else None
        return float("nan") if number is None else number
    if rule.answer_type == ANSWER_DATE:
        return steps.parse_date(text) if text else None
    return text


def sheet_value(table: AgendaTable, index: int, column: str) -> str:
    """What the uploaded sheet itself holds in this cell ("" when the row or column is missing)."""
    if 0 <= index < len(table.base_data):
        return str(table.base_data[index].get(column, "") or "").strip()
    return ""


def display_frame(table: AgendaTable, responses: dict[int, dict]) -> pd.DataFrame:
    """The grid as the invitee sees it: locked columns, then the editable ones.

    An editable cell shows the invitee's saved answer, else the value in the uploaded sheet,
    so they update what is there instead of retyping it. Built from `base_data` every time,
    so the locked columns are always the creator's.
    """
    rows = []
    for index, base_row in enumerate(table.base_data):
        row = {column: str(base_row.get(column, "")) for column in table.locked_columns}
        answers = responses.get(index, {})
        for column in table.editable_columns:
            text = str(answers.get(column, "") or "") or sheet_value(table, index, column)
            row[column] = _grid_value(table.rule_for(column), text)
        rows.append(row)

    columns = table.all_columns()
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows, columns=columns)


def responses_from_frame(table: AgendaTable, frame: pd.DataFrame) -> dict[int, dict]:
    """The invitee's edits, as `{row_index: {column: value}}`.

    Only cells the invitee changed count: a value still equal to the uploaded sheet's (as the
    grid shows it) is not an answer, so a pre-filled grid doesn't read as "all rows filled".
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
        values = {}
        for column in table.editable_columns:
            text = cell_text(row.get(column))
            original = cell_text(_grid_value(table.rule_for(column), sheet_value(table, index, column)))
            if text and text != original:
                values[column] = text
        if values:
            answers[index] = values
    return answers


def cell_text(value) -> str:
    """A grid cell as text: blank for empty, dd-mm-yyyy for a date, 45000 (not 45000.0) for a number."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NaT:
        return ""
    if isinstance(value, (datetime.date, pd.Timestamp)):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def check_rows(
    table: AgendaTable, rows: dict[int, dict], today: datetime.date
) -> tuple[dict[int, dict], list[str]]:
    """The invitee's rows checked against the column rules: `(cleaned rows, problems)`.

    A cleaned value is the stored form ("45,000" -> "45000", a date as dd-mm-yyyy, "yes" -> "Yes").
    Only the cells the invitee changed are checked. Required only applies to a row the
    invitee has started (a value from the uploaded sheet counts as filled), so part-way saves
    still work.
    """
    cleaned: dict[int, dict] = {}
    problems = []
    for index, values in sorted(rows.items()):
        kept = {}
        for column in table.editable_columns:
            rule = table.rule_for(column)
            text = str(values.get(column, "") or "").strip()
            if not text:
                if rule.required and not sheet_value(table, index, column):
                    problems.append(f"Row {index + 1}, {column}: this column is required.")
                continue
            check = steps.check_answer(column_question(column, rule), text, today)
            if check.ok:
                kept[column] = check.value
            else:
                reason = check.reason.removeprefix("The answer ")
                problems.append(f"Row {index + 1}, {column}: '{text}' — {reason}")
        if kept:
            cleaned[index] = kept
    return cleaned, problems


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
