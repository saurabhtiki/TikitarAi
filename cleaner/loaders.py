"""Reading uploaded CSV/Excel files into raw DataFrames.

Every cell is read as text, deliberately. Section 4.1 of the requirements lists `id`
as a target column type, and once pandas has parsed `00123` as the number `123` no
later cleaning step can restore the leading zeros — the most common real-world bug
with employee numbers, account codes and invoice references. Reading as text also
makes "fix numbers stored as text" a meaningful action and "reset to raw" honest:
raw really is what the file contained. Typing then happens as an explicit, replayable
`set_column_types` step instead of an invisible pandas guess.
"""

import csv
import io
import logging
import zipfile
from pathlib import Path

import pandas as pd
from openpyxl.utils.exceptions import InvalidFileException

from cleaner.exceptions import FileParseError, SheetNotFoundError, UnsupportedFileTypeError

logger = logging.getLogger(__name__)

# A .xlsx is a zip archive, so a truncated or mislabelled upload surfaces as a zip error
# rather than anything pandas-shaped.
_WORKBOOK_READ_ERRORS = (
    ValueError,
    OSError,
    KeyError,
    zipfile.BadZipFile,
    InvalidFileException,
    pd.errors.ParserError,
)

CSV_EXTENSIONS = {".csv", ".txt", ".tsv"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}

# Encodings tried in order. latin-1 maps every byte to a character, so it never fails —
# it is the deliberate last resort that guarantees decode_text always returns something.
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

_SNIFF_SAMPLE_CHARS = 65536


def _extension(file_name: str) -> str:
    return Path(file_name).suffix.lower()


def is_csv(file_name: str) -> bool:
    """True if `file_name` looks like a delimited text file the cleaner can read."""
    return _extension(file_name) in CSV_EXTENSIONS


def is_excel(file_name: str) -> bool:
    """True if `file_name` looks like a modern Excel workbook the cleaner can read."""
    return _extension(file_name) in EXCEL_EXTENSIONS


def _reject_unsupported(file_name: str) -> None:
    """Raises UnsupportedFileTypeError for anything the cleaner can't read.

    Legacy .xls is called out by name because it needs a separate engine (xlrd) that
    this project deliberately doesn't install — a clear message beats a cryptic
    pandas one.
    """
    extension = _extension(file_name)
    if extension == ".xls":
        logger.error("Rejected legacy .xls upload: %s", file_name)
        raise UnsupportedFileTypeError(
            f"'{file_name}' is a legacy .xls file. Please re-save it as .xlsx and upload it again."
        )
    logger.error("Rejected unsupported upload type '%s': %s", extension, file_name)
    raise UnsupportedFileTypeError(
        f"'{file_name}' isn't a supported file type. Please upload a .csv or .xlsx file."
    )


def decode_text(file_bytes: bytes) -> str:
    """Decodes uploaded delimited-text bytes to a string.

    Tries UTF-8 (tolerating a byte-order mark), then Windows-1252, then Latin-1, so
    CSVs exported from Excel on Windows arrive as readable text rather than mojibake.

    Raises:
        FileParseError: if the upload is empty.
    """
    if not file_bytes:
        raise FileParseError("The uploaded file is empty.")

    for encoding in _ENCODINGS:
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

    # Unreachable in practice: latin-1 decodes any byte sequence. Kept so a future
    # change to _ENCODINGS can't silently fall through to returning None.
    logger.error("Every candidate encoding failed for a %d-byte upload.", len(file_bytes))
    raise FileParseError("This file's text encoding couldn't be recognised.")


def sniff_delimiter(text: str) -> str:
    """Guesses the column delimiter from the first part of a delimited-text file.

    Falls back to a comma when the sample is too ambiguous to call. Never raises —
    a wrong guess produces a readable (if single-column) table the user can see, which
    is far better than blocking the upload.
    """
    sample = text[:_SNIFF_SAMPLE_CHARS]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        logger.info("Delimiter sniffing was inconclusive; defaulting to a comma.")
        return ","


def list_sheet_names(file_bytes: bytes, file_name: str) -> list[str]:
    """Returns a workbook's sheet names in workbook order, or [] for a CSV.

    Raises:
        UnsupportedFileTypeError: if the file type isn't readable.
        FileParseError: if the workbook can't be opened.
    """
    if is_csv(file_name):
        return []
    if not is_excel(file_name):
        _reject_unsupported(file_name)

    try:
        with pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl") as workbook:
            return list(workbook.sheet_names)
    except _WORKBOOK_READ_ERRORS as error:
        logger.exception("Could not read the sheet list from %s.", file_name)
        raise FileParseError(f"'{file_name}' couldn't be opened as an Excel workbook.") from error


def read_table(file_bytes: bytes, file_name: str, sheet_name: str | None = None) -> pd.DataFrame:
    """Reads one table from an upload, with every cell as text.

    `sheet_name` selects the worksheet for Excel files and is ignored for CSVs. Empty
    cells become missing values; the literal strings that pandas would normally treat
    as missing (`NA`, `N/A`, `null`, …) are preserved as text, so the user decides what
    counts as missing through an explicit cleaning step rather than the reader deciding
    silently.

    Raises:
        UnsupportedFileTypeError: if the file type isn't readable.
        SheetNotFoundError: if `sheet_name` isn't present in the workbook.
        FileParseError: if the file can't be parsed.
    """
    if is_csv(file_name):
        return _read_csv(file_bytes, file_name)
    if is_excel(file_name):
        return _read_excel(file_bytes, file_name, sheet_name)
    _reject_unsupported(file_name)


def _read_csv(file_bytes: bytes, file_name: str) -> pd.DataFrame:
    text = decode_text(file_bytes)
    delimiter = sniff_delimiter(text)
    try:
        frame = pd.read_csv(
            io.StringIO(text),
            sep=delimiter,
            dtype=str,
            keep_default_na=False,
            na_values=[""],
            skip_blank_lines=False,
        )
    except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as error:
        logger.exception("Could not parse %s as delimited text.", file_name)
        raise FileParseError(f"'{file_name}' couldn't be read as a CSV file.") from error
    return _normalise_columns(frame)


def _read_excel(file_bytes: bytes, file_name: str, sheet_name: str | None) -> pd.DataFrame:
    available = list_sheet_names(file_bytes, file_name)
    target = sheet_name if sheet_name is not None else (available[0] if available else None)

    if target is None:
        logger.error("Workbook %s contains no worksheets.", file_name)
        raise FileParseError(f"'{file_name}' contains no worksheets.")
    if target not in available:
        logger.error("Sheet '%s' not found in %s. Available: %s", target, file_name, available)
        raise SheetNotFoundError(f"'{file_name}' has no sheet named '{target}'.")

    try:
        frame = pd.read_excel(
            io.BytesIO(file_bytes),
            sheet_name=target,
            dtype=str,
            keep_default_na=False,
            na_values=[""],
            engine="openpyxl",
        )
    except _WORKBOOK_READ_ERRORS as error:
        logger.exception("Could not read sheet '%s' from %s.", target, file_name)
        raise FileParseError(f"Sheet '{target}' in '{file_name}' couldn't be read.") from error
    return _normalise_columns(frame)


def _normalise_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Forces column labels to plain strings.

    An all-numeric header row otherwise yields integer column labels, which then fail
    every by-name lookup a cleaning step performs. Duplicate names are left as pandas
    mangled them (`Amount`, `Amount.1`) rather than being renamed positionally —
    positional references are far more fragile when a Task is replayed against a
    different file in Stage 8.
    """
    frame = frame.copy()
    frame.columns = [str(column) for column in frame.columns]
    return frame


def has_mangled_duplicate_columns(frame: pd.DataFrame, raw_header: list[str] | None = None) -> list[str]:
    """Returns the columns pandas renamed to resolve duplicate headers (`Amount.1`, …).

    Surfaced in the UI so the user understands why a column they expected to see once
    appears twice under slightly different names.
    """
    suffixed = [column for column in frame.columns if _looks_mangled(column, frame.columns)]
    if suffixed:
        logger.info("Detected mangled duplicate column names: %s", suffixed)
    return suffixed


def _looks_mangled(column: str, all_columns) -> bool:
    base, _, tail = column.rpartition(".")
    return bool(base) and tail.isdigit() and base in set(all_columns)
