"""Building the downloadable multi-sheet workbook."""

import io
import logging
from collections.abc import Mapping, Sequence

import pandas as pd
from pandas.api.types import is_string_dtype

from cleaner.exceptions import ExportError
from cleaner.naming import CLEANING_LOG_SHEET_NAME

logger = logging.getLogger(__name__)

MAX_EXCEL_ROWS = 1_048_576
MAX_EXCEL_COLUMNS = 16_384
MAX_EXCEL_CELL_CHARS = 32_767

_MIN_COLUMN_WIDTH = 8
_MAX_COLUMN_WIDTH = 60


def _check_limits(sheet_name: str, frame: pd.DataFrame) -> None:
    """Raises ExportError before writing rather than letting xlsxwriter fail opaquely
    part-way through a large workbook."""
    if len(frame) + 1 > MAX_EXCEL_ROWS:
        raise ExportError(
            f"'{sheet_name}' has {len(frame):,} rows, more than Excel's limit of "
            f"{MAX_EXCEL_ROWS - 1:,}. Filter or split the data before exporting."
        )
    if len(frame.columns) > MAX_EXCEL_COLUMNS:
        raise ExportError(
            f"'{sheet_name}' has {len(frame.columns):,} columns, more than Excel's limit of "
            f"{MAX_EXCEL_COLUMNS:,}."
        )

    for column in frame.columns:
        series = frame[column]
        if not is_string_dtype(series):
            continue
        longest = series.astype("string").str.len().max()
        if pd.notna(longest) and longest > MAX_EXCEL_CELL_CHARS:
            raise ExportError(
                f"'{sheet_name}' column '{column}' contains a value of {int(longest):,} characters, "
                f"more than Excel's limit of {MAX_EXCEL_CELL_CHARS:,} per cell."
            )


def _column_width(series: pd.Series, header: str) -> int:
    try:
        widest_value = series.astype("string").str.len().max()
    except (ValueError, TypeError):
        widest_value = 0
    widest = max(int(widest_value) if pd.notna(widest_value) else 0, len(str(header)))
    return min(max(widest + 2, _MIN_COLUMN_WIDTH), _MAX_COLUMN_WIDTH)


def _write_sheet(writer: pd.ExcelWriter, sheet_name: str, frame: pd.DataFrame) -> None:
    frame.to_excel(writer, sheet_name=sheet_name, index=False, na_rep="")
    worksheet = writer.sheets[sheet_name]

    if len(frame.columns):
        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, max(len(frame), 1), len(frame.columns) - 1)
        for position, column in enumerate(frame.columns):
            worksheet.set_column(position, position, _column_width(frame[column], column))


def _log_frame(log: Mapping[str, Sequence[str]]) -> pd.DataFrame:
    records = []
    for sheet_name, lines in log.items():
        if not lines:
            records.append({"Sheet": sheet_name, "Step": "", "Action": "No cleaning steps were applied."})
            continue
        for position, line in enumerate(lines, start=1):
            records.append({"Sheet": sheet_name, "Step": position, "Action": line})
    return pd.DataFrame.from_records(records, columns=["Sheet", "Step", "Action"])


def build_workbook(
    tables: Sequence[tuple[str, pd.DataFrame]],
    log: Mapping[str, Sequence[str]] | None = None,
) -> bytes:
    """Builds one multi-sheet .xlsx in memory, plus a sheet recording what was cleaned.

    `tables` is an ordered sequence of `(sheet_name, frame)` pairs whose names must
    already be sanitized and de-duplicated by `cleaner.naming.sanitize_sheet_names`.
    `log` maps those same sheet names to their human-readable cleaning log lines, so
    the downloaded workbook documents itself.

    Raises:
        ExportError: if there is nothing to export, if a table exceeds one of Excel's
            hard limits, or if the workbook can't be written.
    """
    if not tables:
        raise ExportError("There's nothing to export yet — upload and clean a file first.")

    for sheet_name, frame in tables:
        _check_limits(sheet_name, frame)

    buffer = io.BytesIO()
    try:
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            for sheet_name, frame in tables:
                _write_sheet(writer, sheet_name, frame)
            _write_sheet(writer, CLEANING_LOG_SHEET_NAME, _log_frame(log or {}))
    except (ValueError, OSError, KeyError) as error:
        logger.exception("Failed to build the cleaned workbook for %d table(s).", len(tables))
        raise ExportError("The cleaned workbook couldn't be created. Please try again.") from error

    return buffer.getvalue()
