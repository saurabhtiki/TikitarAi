"""Sending Transform Data's finished tables to a Report (phase 47) - the checks, no UI.

A report expects particular files with particular columns. The send dialog asks which of
the report's files each ticked table becomes, and this module answers the two questions
that decide whether the Send button may be pressed:

- **which file is the likely one** (`default_target`), so the common case - one cleaned
  table, one expected file, or matching names - needs no choice at all;
- **what stops it** (`send_problems`): two tables sent as the same file, nothing chosen, or
  a column the report expects that the table hasn't got.

A missing column is refused **here**, before sending, rather than on the Reports page.
There, a missing column is fixed by remapping while the file is re-read, and a sent table
has no file to re-read - so the fix belongs in Transform Data, as a Rename step. Values
that won't convert (text in a number column) are still reported by the Reports page's
usual match check, in the words it uses for an upload.
"""

import pandas as pd

from chat_types.model import ChatType

#: The dropdown choice for "leave this table out".
NOT_SENT = "— don't send —"


def _normalise(name: str) -> str:
    return "".join(character for character in str(name).lower() if character.isalnum())


def default_target(source_name: str, expected_tables: list[str], chosen_count: int) -> str:
    """The report file a ticked table most likely is, or `NOT_SENT` when it can't be told.

    A name match first (`Sales_April` does not match `sales`, but `sales data` matches
    `Sales_Data`), then the one-and-one case: a single table sent to a report expecting a
    single file can only mean one thing.
    """
    wanted = _normalise(source_name)
    for name in expected_tables:
        if _normalise(name) == wanted:
            return name
    if chosen_count == 1 and len(expected_tables) == 1:
        return expected_tables[0]
    return NOT_SENT


def send_problems(assignments: dict[str, str], frames: dict[str, pd.DataFrame],
                  schema: ChatType) -> list[str]:
    """Every reason the send must wait, as sentences. Empty means it can go.

    Args:
        assignments: `{ticked table: report file it becomes, or NOT_SENT}`.
        frames: the ticked tables themselves.
        schema: the files and columns the report expects.
    """
    chosen = {source: target for source, target in assignments.items() if target and target != NOT_SENT}
    if not chosen:
        return ["Choose which of the report's files at least one table is."]

    problems = []
    seen: dict[str, str] = {}
    for source, target in chosen.items():
        if target in seen:
            problems.append(
                f"**{seen[target]}** and **{source}** are both sent as **{target}**. "
                "Pick one of them."
            )
            continue
        seen[target] = source

        expected = schema.table(target)
        frame = frames.get(source)
        if expected is None or frame is None:
            problems.append(f"**{target}** isn't one of this report's files any more.")
            continue
        present = {str(column) for column in frame.columns}
        missing = [column for column in expected.column_names if column not in present]
        if missing:
            listed = ", ".join(f"**{column}**" for column in missing)
            problems.append(
                f"**{source}** has no {listed}, which the report's **{target}** file needs. "
                "Add a Rename step (or the missing column) here, then send again."
            )
    return problems


def unsent_files(assignments: dict[str, str], schema: ChatType) -> list[str]:
    """The report's files nothing is being sent as - still to be uploaded the normal way."""
    sent = set(assignments.values())
    return [name for name in schema.table_names() if name not in sent]
