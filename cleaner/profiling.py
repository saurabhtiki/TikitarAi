"""Column type detection and per-column statistics.

Also owns the value parsers (`parse_numeric_series`, `parse_datetime_series`) that
both detection and the cleaning steps use, so "what counts as a number" is defined in
exactly one place. `cleaner.steps` imports them from here; the dependency runs one way,
`pipeline -> steps -> profiling -> pandas`.
"""

import logging
import re
import warnings

import pandas as pd
from pandas.api.types import is_numeric_dtype, is_string_dtype

logger = logging.getLogger(__name__)

TEXT = "text"
CATEGORICAL = "categorical"
NUMERIC = "numeric"
DATE = "date"
ID = "id"

COLUMN_TYPES = [TEXT, CATEGORICAL, NUMERIC, DATE, ID]

# Whitespace that .str.strip() misses: non-breaking space (rife in Excel exports),
# the zero-width family, and the byte-order mark.
INVISIBLE_WHITESPACE = " ​‌‍⁠﻿"

_CURRENCY_AND_SPACE = re.compile(rf"[$€£₹¥₩₽¢\s{INVISIBLE_WHITESPACE}]")
_ACCOUNTING_PARENTHESES = re.compile(r"^\((.*)\)$")
_TRAILING_MINUS = re.compile(r"^(.*)-$")
_LEADING_ZERO_NUMBER = re.compile(r"^0\d+$")
_ALPHANUMERIC_ID = re.compile(r"^[A-Za-z0-9._\-/]+$")


def parse_numeric_series(
    series: pd.Series,
    decimal_separator: str = ".",
    parentheses_are_negative: bool = True,
) -> tuple[pd.Series, pd.Index]:
    """Parses a text column into numbers, tolerating real-world formatting.

    Handles currency symbols, thousands separators, non-breaking and zero-width
    characters, accounting parentheses (`(300)` -> `-300`) and a trailing minus
    (`300-` -> `-300`).

    `decimal_separator` is explicit and never inferred: `1.200` is twelve hundred in
    de-DE and 1.2 in en-US, and guessing wrong silently produces wrong money.

    Returns the numeric series and the index of values that were present before but
    became missing after — exactly the cells that couldn't be read, which is what the
    coercion warning reports.
    """
    text = series.astype("string")
    was_present = text.notna()

    cleaned = text.str.replace(_CURRENCY_AND_SPACE, "", regex=True)

    if decimal_separator == ",":
        cleaned = cleaned.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    else:
        cleaned = cleaned.str.replace(",", "", regex=False)

    if parentheses_are_negative:
        cleaned = cleaned.str.replace(_ACCOUNTING_PARENTHESES, r"-\1", regex=True)
    cleaned = cleaned.str.replace(_TRAILING_MINUS, r"-\1", regex=True)

    numeric = pd.to_numeric(cleaned, errors="coerce")
    failed = series.index[was_present & numeric.isna()]
    return numeric, failed


def parse_datetime_series(series: pd.Series, date_format: str | None = None) -> tuple[pd.Series, pd.Index]:
    """Parses a text column into dates, returning the parsed series and the index of
    values that were present before but couldn't be read.

    `date_format`, when given, is applied strictly; otherwise pandas infers it.
    """
    was_present = series.notna()
    with warnings.catch_warnings():
        # Mixed or ambiguous formats warn per-column; the failure count reported back to
        # the user is the actionable signal, so the warning itself is noise here.
        warnings.simplefilter("ignore")
        parsed = pd.to_datetime(series, errors="coerce", format=date_format)
    failed = series.index[was_present & parsed.isna()]
    return parsed, failed


def numeric_parse_rate(series: pd.Series, decimal_separator: str = ".") -> float:
    """Fraction of the column's present values that parse as numbers. 0.0 if all missing."""
    present = series.notna().sum()
    if present == 0:
        return 0.0
    _, failed = parse_numeric_series(series, decimal_separator)
    return float((present - len(failed)) / present)


def date_parse_rate(series: pd.Series, date_format: str | None = None) -> float:
    """Fraction of the column's present values that parse as dates. 0.0 if all missing."""
    present = series.notna().sum()
    if present == 0:
        return 0.0
    _, failed = parse_datetime_series(series, date_format)
    return float((present - len(failed)) / present)


def _has_preserved_leading_zeros(series: pd.Series) -> bool:
    """The decisive identifier signal, trusted at any sample size.

    Leading zeros only survive because the file was read as text, and typing such a
    column as numeric would destroy them irreversibly — so a single `00123` is enough.
    """
    present = series.dropna().astype("string")
    return bool(not present.empty and present.str.match(_LEADING_ZERO_NUMBER).any())


def _looks_like_a_code(series: pd.Series, unique_ratio: float, id_min_rows: int) -> bool:
    """The weak identifier signal: near-unique, alphanumeric, and not numeric — `EMP-0042`.

    Deliberately checked *after* numeric and date detection, because dates share this
    shape exactly: `2024-01-03` is near-unique across a column and matches the same
    character set. Near-uniqueness also needs a real sample behind it — in a handful of
    rows almost every text column is "unique", which would label a two-row Department
    column as an identifier.
    """
    present = series.dropna().astype("string")
    if present.empty or unique_ratio <= 0.95 or len(present) < id_min_rows:
        return False
    if not present.str.match(_ALPHANUMERIC_ID).all():
        return False
    return numeric_parse_rate(present) < 0.95


def detect_column_type(
    series: pd.Series,
    *,
    numeric_threshold: float = 0.95,
    date_threshold: float = 0.90,
    categorical_max_unique: int = 50,
    categorical_max_ratio: float = 0.5,
    categorical_min_rows: int = 20,
    id_min_rows: int = 10,
) -> str:
    """Suggests one of text/categorical/numeric/date/id for a column.

    Order matters and each position is deliberate: preserved leading zeros mark an
    identifier outright; then numeric, so a run of plain integers isn't mistaken for
    years; then date; then the weaker "looks like a code" identifier test, which has to
    come after date because `2024-01-03` has exactly that shape; then low-cardinality
    categorical; else text.

    Never raises — detection is only a suggestion the user can override, so anything
    unexpected falls back to `text`.
    """
    present = series.dropna()
    if present.empty:
        return TEXT

    if is_numeric_dtype(series):
        return NUMERIC

    row_count = len(present)
    distinct = present.nunique()
    unique_ratio = distinct / row_count

    try:
        if _has_preserved_leading_zeros(series):
            return ID
        if numeric_parse_rate(series) >= numeric_threshold:
            return NUMERIC
        if date_parse_rate(series) >= date_threshold:
            return DATE
        if _looks_like_a_code(series, unique_ratio, id_min_rows):
            return ID
    except (ValueError, TypeError):
        logger.exception("Type detection failed for column '%s'; falling back to text.", series.name)
        return TEXT

    is_low_cardinality = (
        row_count >= categorical_min_rows
        and distinct <= categorical_max_unique
        and unique_ratio <= categorical_max_ratio
    )
    if is_low_cardinality:
        return CATEGORICAL
    return TEXT


def detect_column_types(frame: pd.DataFrame, **kwargs) -> dict[str, str]:
    """Runs `detect_column_type` across every column, returning {column: type}."""
    return {str(column): detect_column_type(frame[column], **kwargs) for column in frame.columns}


def column_stats(frame: pd.DataFrame, sample_size: int = 3) -> pd.DataFrame:
    """One row per column: name, detected type, counts, missing percentage and samples.

    Rendered read-only in the UI so the auto-suggested `categorical` columns stay
    reviewable at a glance without an editable grid.
    """
    row_count = len(frame)
    records = []

    for column in frame.columns:
        series = frame[column]
        missing = int(series.isna().sum())
        samples = series.dropna().astype("string").head(sample_size).tolist()
        records.append(
            {
                "column": str(column),
                "detected_type": detect_column_type(series),
                "non_null": row_count - missing,
                "missing": missing,
                "missing_pct": round(missing / row_count * 100, 1) if row_count else 0.0,
                "unique": int(series.nunique()),
                "sample_values": ", ".join(samples),
            }
        )

    return pd.DataFrame.from_records(
        records,
        columns=["column", "detected_type", "non_null", "missing", "missing_pct", "unique", "sample_values"],
    )


def text_columns(frame: pd.DataFrame) -> list[str]:
    """Columns the text-cleanup actions can operate on.

    Numeric and datetime columns are excluded because `str.replace` silently no-ops on
    them — applying a text action there would look like it worked and change nothing.
    """
    return [str(column) for column in frame.columns if is_string_dtype(frame[column])]
