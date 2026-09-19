"""The named tables a pipeline works on, and the rules about their names.

A workspace is a plain `dict[str, NamedFrame]` keyed by name, in insertion order, plus the
pure functions in this module. A dict rather than a class with methods, to match the
function style the `cleaner` package uses, and because the replay in `transform.pipeline`
wants to thread a value through a fold rather than mutate an object.

**Nothing here mutates.** `put_frame` and `drop_frame` return a new dict, exactly as every
step executor returns a new DataFrame. That is what makes full replay safe: the workspace
built from the uploads is never touched by the steps that read it, so re-running the same
steps twice gives the same answer.

**A table's name is also its sheet name in the download.** So names are held to Excel's
rules from the moment they are typed, rather than being quietly rewritten at export time —
a user who named a table `Q1: North/South` should find out immediately, not discover a
sheet called `Q1_ North_South` in a workbook a month later.
"""

import logging
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from cleaner.naming import MAX_SHEET_NAME_LENGTH, sanitize_sheet_name
from transform.exceptions import DuplicateFrameNameError, FrameNotFoundError

logger = logging.getLogger(__name__)

#: What an unnameable table falls back to, e.g. a file called `.csv`.
FALLBACK_FRAME_NAME = "Table"

FrameOrigin = Literal["upload", "step"]


@dataclass(frozen=True)
class NamedFrame:
    """One named table in the workspace.

    Attributes:
        name: what the user calls it, and what its sheet is called in the download.
        frame: the data.
        origin: `upload` for a table that came from a file, `step` for one a step created.
            The page shows the two differently, and only `upload` tables survive a
            "delete the last step".
        source_label: where it came from, for the caption under its tab —
            `sales_data.xlsx - Sheet1`, or `step 2: Join tables`.
        upload_file_id: Streamlit's per-upload id, for an `upload` table. This is what ties
            a table back to the file still sitting in the uploader, so that removing the
            file removes the table. Meaningless in any later session, which is why phase
            26 will store the file *name* in a saved pipeline instead.
        created_by_step: the 0-based index of the step that made it, for a `step` table.
    """

    name: str
    frame: pd.DataFrame
    origin: FrameOrigin = "upload"
    source_label: str = ""
    upload_file_id: str | None = None
    created_by_step: int | None = None

    @property
    def shape_label(self) -> str:
        """`1,204 rows x 9 columns`, for the table list and the tab caption."""
        rows = len(self.frame)
        columns = len(self.frame.columns)
        row_word = "row" if rows == 1 else "rows"
        column_word = "column" if columns == 1 else "columns"
        return f"{rows:,} {row_word} x {columns:,} {column_word}"


def normalise_frame_name(raw: str) -> str:
    """The stored form of a table name.

    Excel's worksheet rules, reused rather than reinvented: forbidden characters become
    underscores, whitespace collapses, and the result is clamped to 31 characters. Never
    raises — there is always some legal name, which is why an empty box becomes `Table`
    rather than an error.
    """
    return sanitize_sheet_name(raw, fallback=FALLBACK_FRAME_NAME)


def name_key(name: str) -> str:
    """The form two table names are compared in.

    Case-insensitive, because Excel refuses to put `Sales` and `sales` in one workbook and
    a name that is legal on this page must stay legal in the download.
    """
    return normalise_frame_name(name).casefold()


def suggest_frame_name(base: str, taken: list[str] | set[str] | None = None) -> str:
    """A free name derived from `base`: `sales`, then `sales_2`, `sales_3`, ...

    The suffix rule is `cleaner.naming.deduplicate_sheet_names`' rule, deliberately — the
    export runs every name through that function, so any other rule here would mean a
    table renamed at download time. The base is re-truncated so the suffixed name still
    fits inside 31 characters.
    """
    cleaned = normalise_frame_name(base)
    claimed = {name_key(name) for name in (taken or [])}
    if cleaned.casefold() not in claimed:
        return cleaned

    suffix_number = 2
    while True:
        suffix = f"_{suffix_number}"
        candidate = f"{cleaned[: MAX_SHEET_NAME_LENGTH - len(suffix)]}{suffix}"
        if candidate.casefold() not in claimed:
            return candidate
        suffix_number += 1


def frame_names(workspace: dict[str, NamedFrame]) -> list[str]:
    """Every table name, in the order the tables were created."""
    return list(workspace)


def has_frame(workspace: dict[str, NamedFrame], name: str) -> bool:
    """Whether a table by this name exists, compared case-insensitively."""
    wanted = name_key(name)
    return any(name_key(existing) == wanted for existing in workspace)


def get_frame(workspace: dict[str, NamedFrame], name: str) -> NamedFrame:
    """The named table.

    Raises:
        FrameNotFoundError: if no table goes by that name. The message lists what is
            available, because the usual cause is a step whose source table was renamed or
            removed, and the next question is always "so what is there?".
    """
    wanted = name_key(name)
    for existing, held in workspace.items():
        if name_key(existing) == wanted:
            return held
    available = ", ".join(frame_names(workspace)) or "none"
    logger.error("Step asked for table '%s', which isn't in the workspace.", name)
    raise FrameNotFoundError(f"There's no table called '{name}'. Tables available: {available}.")


def get_dataframe(workspace: dict[str, NamedFrame], name: str) -> pd.DataFrame:
    """Just the data of the named table.

    Raises:
        FrameNotFoundError: if no table goes by that name.
    """
    return get_frame(workspace, name).frame


def put_frame(
    workspace: dict[str, NamedFrame], frame: NamedFrame, *, replace: bool = True
) -> dict[str, NamedFrame]:
    """Returns a new workspace with `frame` added or replaced.

    Replacing keeps the table's original position, so a step that updates a table in place
    doesn't shuffle the tabs on screen.

    Args:
        replace: False to insist the name is free. This is how a step whose output mode is
            "new table" refuses to overwrite an existing one.

    Raises:
        DuplicateFrameNameError: if `replace` is False and the name is taken.
    """
    existing_key = next((held for held in workspace if name_key(held) == name_key(frame.name)), None)
    if existing_key is not None and not replace:
        raise DuplicateFrameNameError(
            f"A table named '{frame.name}' already exists - choose another name."
        )

    if existing_key is not None:
        # Rebuild in order so the table keeps its place rather than jumping to the end,
        # and so a differently-spelled but equal name adopts the new spelling.
        return {
            (frame.name if held == existing_key else held): (
                frame if held == existing_key else value
            )
            for held, value in workspace.items()
        }

    updated = dict(workspace)
    updated[frame.name] = frame
    return updated


def drop_frame(workspace: dict[str, NamedFrame], name: str) -> dict[str, NamedFrame]:
    """Returns a new workspace without the named table.

    Silent when the name isn't there: the callers are "the user removed a file" and "the
    last step was deleted", and in both the goal is the table's absence, not proof it was
    ever present.
    """
    wanted = name_key(name)
    return {held: value for held, value in workspace.items() if name_key(held) != wanted}


def rename_frame(
    workspace: dict[str, NamedFrame], old_name: str, new_name: str
) -> dict[str, NamedFrame]:
    """Returns a new workspace with one table renamed, keeping its position.

    Raises:
        FrameNotFoundError: if `old_name` isn't in the workspace.
        DuplicateFrameNameError: if `new_name` is already taken by a different table.
    """
    held = get_frame(workspace, old_name)
    cleaned = normalise_frame_name(new_name)

    if name_key(cleaned) != name_key(old_name) and has_frame(workspace, cleaned):
        raise DuplicateFrameNameError(
            f"A table named '{cleaned}' already exists - choose another name."
        )

    renamed = NamedFrame(
        name=cleaned,
        frame=held.frame,
        origin=held.origin,
        source_label=held.source_label,
        upload_file_id=held.upload_file_id,
        created_by_step=held.created_by_step,
    )
    return {
        (cleaned if name_key(key) == name_key(old_name) else key): (
            renamed if name_key(key) == name_key(old_name) else value
        )
        for key, value in workspace.items()
    }


def columns_of(workspace: dict[str, NamedFrame], name: str) -> list[str]:
    """The named table's column names, or an empty list if there is no such table.

    Non-raising because its caller is the form renderer, and a form with no table chosen
    yet is an ordinary half-filled state rather than an error.
    """
    try:
        return [str(column) for column in get_dataframe(workspace, name).columns]
    except FrameNotFoundError:
        return []


def uploads_only(workspace: dict[str, NamedFrame]) -> dict[str, NamedFrame]:
    """Just the tables that came from files.

    The starting point every replay folds the steps over.
    """
    return {name: held for name, held in workspace.items() if held.origin == "upload"}
