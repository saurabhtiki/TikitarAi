"""Turning an upload into a typed DataFrame ready for DuckDB.

Deliberately thin. Every hard part — decoding, delimiter sniffing, sheet selection,
reading every cell as text so leading zeros survive, and detecting
text/categorical/numeric/date/id — was solved in Stage 4 and is imported from `cleaner`
rather than written a second time. Two independent type detectors would be the classic
way for a Task authored in Task Builder to behave differently from the same file
explored in Chat with Data.

The one thing this module adds is the distinction the engine needs downstream:
DuckDB has SQL types, but `id` and `categorical` are *semantic* types with no SQL
equivalent (both are VARCHAR), and they are what makes the data dictionary and the
agent's schema context useful. So every load returns the frame **and** its semantic
types alongside.
"""

import logging

import pandas as pd

from cleaner import loaders, pipeline, profiling
from cleaner.exceptions import DataCleanerError
from engine.exceptions import TableLoadError

logger = logging.getLogger(__name__)


def semantic_types(frame: pd.DataFrame, declared: dict[str, str] | None = None) -> dict[str, str]:
    """Returns `{column: text|categorical|numeric|date|id}` for a typed frame.

    `declared` carries types a Data Cleaner recipe explicitly set, which win over
    detection for the string-backed types — pandas stores text, categorical and id
    identically, so re-detecting would silently overrule a choice the user already made.
    Same rule as `cleaner.profiling.effective_column_type`, which does the work.
    """
    declared = declared or {}
    return {
        str(column): profiling.effective_column_type(frame[column], declared.get(str(column)))
        for column in frame.columns
    }


def prepare_table(
    file_bytes: bytes, file_name: str, sheet_name: str | None = None
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Reads one uploaded table and applies its detected column types.

    Reads as text first and types as an explicit step, exactly as the Data Cleaner does,
    so an `id` column of `00123` reaches DuckDB as VARCHAR with its leading zeros rather
    than as the integer 123.

    Returns:
        The typed frame, and `{column: semantic_type}`.

    Raises:
        TableLoadError: if the file can't be read or typed.
    """
    try:
        raw = loaders.read_table(file_bytes, file_name, sheet_name)
    except DataCleanerError as error:
        logger.exception("Could not read '%s' (sheet %s) for the data engine.", file_name, sheet_name)
        raise TableLoadError(str(error)) from error

    return prepare_raw_frame(raw, source=f"'{file_name}'")


def prepare_raw_frame(raw: pd.DataFrame, *, source: str = "this table") -> tuple[pd.DataFrame, dict[str, str]]:
    """Applies detected column types to an all-text frame.

    Split out from `prepare_table` so the Data Cleaner handoff can reuse the identical
    typing path without going back through a file reader.

    Raises:
        TableLoadError: if the typing step can't be applied.
    """
    detected = profiling.detect_column_types(raw)
    by_column = {
        column: {"target_type": column_type}
        for column, column_type in detected.items()
        if column_type != profiling.TEXT
    }

    if not by_column:
        return raw, detected

    try:
        typed = pipeline.apply_steps(raw, [pipeline.make_step("set_column_types", {"by_column": by_column})])
    except DataCleanerError as error:
        logger.exception("Could not apply detected column types to %s.", source)
        raise TableLoadError(f"{source} couldn't be typed for analysis.") from error

    return typed, semantic_types(typed, declared=detected)


def prepare_cleaned_frame(
    frame: pd.DataFrame, declared_types: dict[str, str] | None = None
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Prepares a frame the Data Cleaner has already cleaned and typed.

    The handoff path (`engine.session.adopt_cleaner_tables`). The recipe has already run,
    so this only reads the semantic types back off the result rather than re-detecting
    and potentially disagreeing with the cleaning log the user just read.
    """
    return frame, semantic_types(frame, declared=declared_types)
