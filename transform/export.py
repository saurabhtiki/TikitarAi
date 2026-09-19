"""Turning chosen tables into one downloadable workbook.

Thin on purpose. `cleaner.export.build_workbook` already knows Excel's row, column and cell
limits, how wide to make each column, and how to freeze the header — none of which is worth
a second implementation that could drift from the first. This module's whole job is to
decide *which* tables go in and to write the steps alongside them, then hand over.

The workbook carries **every row**. The 500-row cap is what the screen shows, never what is
exported, which is the same rule phase 16 set for the report tables.
"""

import logging

from cleaner.exceptions import ExportError
from cleaner.export import build_workbook
from cleaner.naming import sanitize_sheet_names
from transform.exceptions import TransformExportError
from transform.pipeline import TransformStep, step_headline
from transform.workspace import NamedFrame, name_key

logger = logging.getLogger(__name__)


def build_transform_workbook(
    workspace: dict[str, NamedFrame],
    selected_names: list[str],
    steps: list[TransformStep] | None = None,
) -> bytes:
    """Builds one .xlsx holding the chosen tables, plus a sheet listing the steps.

    Table names are already held to Excel's rules by `workspace.normalise_frame_name`, so
    `sanitize_sheet_names` here is a belt-and-braces pass that also settles any
    case-insensitive collision Excel would refuse.

    Args:
        workspace: every table currently in the workspace.
        selected_names: the tables to export, in the order the user chose them.
        steps: the pipeline, written into the log sheet so the workbook explains itself.

    Returns:
        The workbook's bytes.

    Raises:
        TransformExportError: if nothing was selected, if a chosen table has gone, or if
            Excel's limits are exceeded.
    """
    if not selected_names:
        raise TransformExportError("Choose at least one table to download.")

    chosen: list[tuple[str, NamedFrame]] = []
    missing: list[str] = []
    for name in selected_names:
        held = next(
            (frame for held_name, frame in workspace.items() if name_key(held_name) == name_key(name)),
            None,
        )
        if held is None:
            missing.append(name)
        else:
            chosen.append((name, held))

    if missing:
        raise TransformExportError(
            f"These tables aren't in the workspace any more: {', '.join(missing)}. "
            f"Pick again and download."
        )

    sheet_names = sanitize_sheet_names([name for name, _ in chosen])
    tables = [(sheet_name, held.frame) for sheet_name, (_, held) in zip(sheet_names, chosen, strict=True)]

    # One log sheet for the whole pipeline rather than one per table: a step here can read
    # two tables and write a third, so "what was done to this sheet" is not answerable
    # sheet by sheet the way it is in the Data Cleaner.
    log = {sheet_names[0]: _step_lines(steps or [])} if sheet_names else {}

    try:
        return build_workbook(tables, log)
    except ExportError as error:
        logger.exception("Could not build the transform workbook for %d table(s).", len(tables))
        raise TransformExportError(str(error)) from error


def _step_lines(steps: list[TransformStep]) -> list[str]:
    """The pipeline as plain lines for the log sheet."""
    if not steps:
        return ["No steps were added - the tables are as they were uploaded."]
    return [step_headline(step, index) for index, step in enumerate(steps)]
