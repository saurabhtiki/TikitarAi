"""Cross-invitee comparison matrices (requirement 6.7, Phase 2, spec 8).

Three frames, one per matrix the dashboard shows, all pure: rows in, `DataFrame` out, no
Streamlit and no database. Each is a **read of data that already exists** — spec 8 is
explicit that viewing a matrix costs no AI call, because the summaries were written at close
and the evaluation answers at extraction.

Spec 3a warns that a table comparison only works when every invitee filled in the identical
template. Here that holds structurally rather than by check: an `AgendaTable` belongs to the
*meeting*, so one grid is what every invitee was given. There is no per-invitee template that
could diverge.
"""

import logging

import pandas as pd

from meetings.model import AgendaTable, EvaluationAnswer, EvaluationField, Meeting

logger = logging.getLogger(__name__)

NOT_DISCUSSED = "Not discussed"
NOT_STARTED = "—"

# The leading column of each matrix. Named rather than an index so the frame survives being
# handed to `st.dataframe(hide_index=True)` with its row labels still on screen.
ROW_LABEL = "Item"
FIELD_LABEL = "Field"


def invitee_labels(invitees: list[dict]) -> dict[int, str]:
    """`{invitee_id: column heading}`, disambiguated only where it has to be.

    A name is what the creator wants to read across the top. Two invitees called "Raj" would
    make two columns nobody can tell apart, so a repeated name — and only a repeated name —
    gains its email.
    """
    counts: dict[str, int] = {}
    for invitee in invitees:
        name = str(invitee.get("name") or invitee.get("email") or "Invitee").strip()
        counts[name] = counts.get(name, 0) + 1

    labels = {}
    for invitee in invitees:
        name = str(invitee.get("name") or invitee.get("email") or "Invitee").strip()
        label = f"{name} ({invitee.get('email')})" if counts.get(name, 0) > 1 else name
        labels[invitee["invitee_id"]] = label
    return labels


def consolidated_frame(meeting: Meeting, invitees: list[dict], summaries: dict[int, object]) -> pd.DataFrame:
    """Rows = agenda items, columns = invitees, cells = what each of them said.

    `summaries` maps an invitee id to their `MeetingSummary` — the final MoM where they have
    closed, the live status where they haven't, whichever the caller found. An invitee with
    neither gets `NOT_STARTED` rather than "Not discussed": nothing has been generated for
    them yet, which is a different claim from having been asked and not answered.
    """
    labels = invitee_labels(invitees)
    rows = []

    for item in meeting.agenda:
        row = {ROW_LABEL: item.item}
        for invitee in invitees:
            summary = summaries.get(invitee["invitee_id"])
            row[labels[invitee["invitee_id"]]] = _cell_for(summary, item.item)
        rows.append(row)

    columns = [ROW_LABEL, *labels.values()]
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


def _cell_for(summary, item_title: str) -> str:
    """One invitee's line on one agenda item."""
    if summary is None:
        return NOT_STARTED

    for entry in getattr(summary, "agenda_items", []):
        if entry.item.strip().lower() != item_title.strip().lower():
            continue
        # A table item's notes are already its completion line, so they read correctly here
        # whether or not any rows were filled.
        if entry.notes.strip():
            return entry.notes.strip()
        return "Discussed" if entry.discussed else NOT_DISCUSSED

    return NOT_DISCUSSED


def evaluation_frame(
    fields: list[EvaluationField], invitees: list[dict], answers: list[EvaluationAnswer]
) -> pd.DataFrame:
    """Rows = evaluation questions, columns = invitees, cells = "8 yrs (High)" (spec 3b).

    Both halves of the cell are shown, never the tag alone. The bucket is what makes the
    column scannable, but it is a judgement made by a model — leaving the raw answer beside
    it is what lets the creator see when the judgement is wrong.
    """
    labels = invitee_labels(invitees)
    by_key = {(answer.field_id, answer.invitee_id): answer for answer in answers}

    rows = []
    for field_spec in fields:
        row = {FIELD_LABEL: field_spec.question}
        for invitee in invitees:
            answer = by_key.get((field_spec.field_id, invitee["invitee_id"]))
            row[labels[invitee["invitee_id"]]] = answer.display() if answer else NOT_STARTED
        rows.append(row)

    columns = [FIELD_LABEL, *labels.values()]
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


def table_comparison_frame(
    table: AgendaTable,
    column: str,
    invitees: list[dict],
    responses: dict[int, dict[int, dict]],
) -> pd.DataFrame:
    """One editable column of one grid, stacked across invitees (spec 3a).

    One column at a time, not the whole grid at once: a table with three editable columns
    across four invitees is twelve columns of numbers that nobody can read down. The caller
    picks which one is being compared.

    Rows are labelled with the grid's **first locked column** — a bill number or a party name
    is what makes a row recognisable, and falling back to "Row 4" when there is no locked
    column is honest about having nothing better.
    """
    labels = invitee_labels(invitees)
    label_column = table.locked_columns[0] if table.locked_columns else ""

    rows = []
    for index, base_row in enumerate(table.base_data):
        name = str(base_row.get(label_column, "")).strip() if label_column else ""
        row = {ROW_LABEL: name or f"Row {index + 1}"}
        for invitee in invitees:
            filled = responses.get(invitee["invitee_id"], {}).get(index, {})
            row[labels[invitee["invitee_id"]]] = str(filled.get(column, "") or NOT_STARTED)
        rows.append(row)

    columns = [ROW_LABEL, *labels.values()]
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
