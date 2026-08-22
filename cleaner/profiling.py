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
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype, is_string_dtype

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
_BLANK_TEXT = re.compile(rf"^[\s{INVISIBLE_WHITESPACE}]*$")


def blank_mask(series: pd.Series) -> pd.Series:
    """True wherever a value is missing *or* looks empty on screen.

    One definition of "blank", shared by the stats panel, the missing-values metric and
    the fill and empty-row steps, because they have to agree: a cell holding a single
    space reads as empty to the person looking at it, and a panel that calls it filled
    while a fill step refuses to touch it is just confusing. `str.strip()` alone is not
    enough — it leaves the non-breaking space that Excel exports are full of.
    """
    if not is_string_dtype(series):
        return series.isna().astype(bool)
    text = series.astype("string")
    return (text.isna() | text.str.match(_BLANK_TEXT).fillna(False)).astype(bool)


def blank_count(frame: pd.DataFrame) -> int:
    """How many cells across the whole frame are missing or visually blank."""
    if frame.empty:
        return 0
    return sum(int(blank_mask(frame[column]).sum()) for column in frame.columns)


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


DATE_FORMAT_CHOICES: list[tuple[str, str | None]] = [
    ("Day-Month-Year — 14-08-2024", "%d-%m-%Y"),
    ("Day-Month-Year — 14/08/2024", "%d/%m/%Y"),
    ("Day-Month-Year (short) — 14-08-24", "%d-%m-%y"),
    ("Day-Mon-Year — 14-Aug-2024", "%d-%b-%Y"),
    ("Day-Mon-Year (short) — 14-Aug-24", "%d-%b-%y"),
    ("Day Month Year — 14 August 2024", "%d %B %Y"),
    ("Month-Day-Year (US) — 08-14-2024", "%m-%d-%Y"),
    ("Month-Day-Year (US) — 08/14/2024", "%m/%d/%Y"),
    ("Year-Month-Day — 2024-08-14", "%Y-%m-%d"),
    ("Year/Month/Day — 2024/08/14", "%Y/%m/%d"),
    ("Month-Year — 08-2024", "%m-%Y"),
    ("Mon-Year — Aug-2024", "%b-%Y"),
    ("With time — 14-08-2024 15:30", "%d-%m-%Y %H:%M"),
    ("With time — 2024-08-14 15:30:00", "%Y-%m-%d %H:%M:%S"),
    ("Auto-detect (risky — may read days as months)", None),
]

DEFAULT_DATE_FORMAT = "%d-%m-%Y"

# Both orders parse the same string whenever every day-of-month is 12 or lower, and the
# two readings disagree ("05-08-2024" is either 5 Aug or 8 May). A perfect score on one
# is therefore not proof it is the right one, so `best_date_format` flags the clash.
_DAY_FIRST_CODES = ("%d-%m-", "%d/%m/", "%d.%m.")
_MONTH_FIRST_CODES = ("%m-%d-", "%m/%d/", "%m.%d.")

# Scoring every candidate format over a large column inside a dialog is slow and adds
# nothing: a few hundred values settle which format fits.
DATE_SAMPLE_ROWS = 500


def _format_orientation(date_format: str | None) -> str | None:
    """'day', 'month' or None — which unit a numeric format puts first."""
    if not date_format:
        return None
    if any(code in date_format for code in _DAY_FIRST_CODES):
        return "day"
    if any(code in date_format for code in _MONTH_FIRST_CODES):
        return "month"
    return None


def date_format_scores(
    series: pd.Series, choices: list[tuple[str, str | None]] | None = None
) -> list[tuple[str | None, int, int]]:
    """Scores each candidate date format against the column's real values.

    Returns `(date_format, parsed_count, present_count)` per candidate, best first. Only
    the first `DATE_SAMPLE_ROWS` present values are tested so a large column stays
    responsive inside a dialog; the counts are therefore of the sample, not the column.
    """
    candidates = [date_format for _, date_format in (choices or DATE_FORMAT_CHOICES)]
    sample = series[series.notna()].head(DATE_SAMPLE_ROWS)
    present = int(len(sample))
    if present == 0:
        return [(date_format, 0, 0) for date_format in candidates]

    scores: list[tuple[str | None, int, int]] = []
    for date_format in candidates:
        try:
            _, failed = parse_datetime_series(sample, date_format)
            scores.append((date_format, present - len(failed), present))
        except (ValueError, TypeError) as error:
            # A malformed directive raises rather than coercing; it simply scores zero.
            logger.debug("Date format %r could not be scored: %s", date_format, error)
            scores.append((date_format, 0, present))

    # A stable sort keeps catalogue order within a tie, so the day-first default wins
    # against the US order on data that fits both.
    scores.sort(key=lambda score: -score[1])
    return scores


def best_date_format(
    series: pd.Series, choices: list[tuple[str, str | None]] | None = None
) -> tuple[str | None, int, int, bool]:
    """Picks the format that reads the most values, for pre-selecting the dropdown.

    Returns `(date_format, parsed_count, present_count, is_ambiguous)`. `is_ambiguous` is
    True when a day-first and a month-first format both score perfectly — the data fits
    either reading and only the user knows which one is meant.
    """
    scores = date_format_scores(series, choices)
    if not scores:
        return None, 0, 0, False

    date_format, parsed, present = scores[0]
    perfect_orientations = {
        _format_orientation(candidate)
        for candidate, hits, total in scores
        if total > 0 and hits == total
    }
    is_ambiguous = {"day", "month"} <= perfect_orientations
    return date_format, parsed, present, is_ambiguous


def date_format_preview(
    series: pd.Series, date_format: str | None, limit: int = 3
) -> list[tuple[str, str]]:
    """Sample `(original, parsed)` pairs so the user can eyeball the reading before applying."""
    sample = series[series.notna()].head(DATE_SAMPLE_ROWS)
    if sample.empty:
        return []
    try:
        parsed, _ = parse_datetime_series(sample, date_format)
    except (ValueError, TypeError) as error:
        logger.debug("Date format %r could not be previewed: %s", date_format, error)
        return []

    pairs: list[tuple[str, str]] = []
    for original, value in zip(sample.astype("string"), parsed):
        if pd.isna(value):
            continue
        pairs.append((str(original), value.strftime("%d %b %Y")))
        if len(pairs) >= limit:
            break
    return pairs


def date_format_failures(series: pd.Series, date_format: str | None, limit: int = 3) -> list[str]:
    """The first few values this format cannot read, to name in the warning."""
    sample = series[series.notna()].head(DATE_SAMPLE_ROWS)
    if sample.empty:
        return []
    try:
        _, failed = parse_datetime_series(sample, date_format)
    except (ValueError, TypeError) as error:
        logger.debug("Date format %r could not be checked for failures: %s", date_format, error)
        return [str(value) for value in sample.astype("string").head(limit)]
    return [str(value) for value in sample.loc[failed].astype("string").head(limit)]


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


def effective_column_type(series: pd.Series, declared: str | None = None) -> str:
    """The type a column actually has now, rather than a fresh guess at it.

    The dtype decides where it can: a numeric or datetime column is what it is. For the
    string-backed types it cannot — pandas stores text, categorical and id identically —
    so a type the user explicitly set wins over detection. Without that, setting a column
    of plain digits to `id` would leave the panel still reporting `numeric`, flatly
    contradicting the choice the user just made.
    """
    if is_numeric_dtype(series):
        return NUMERIC
    if is_datetime64_any_dtype(series):
        return DATE
    if declared in COLUMN_TYPES:
        return declared
    return detect_column_type(series)


def column_stats(
    frame: pd.DataFrame, sample_size: int = 3, declared_types: dict[str, str] | None = None
) -> pd.DataFrame:
    """One row per column: name, current type, counts, blank percentage and samples.

    Rendered read-only in the UI so the auto-suggested `categorical` columns stay
    reviewable at a glance without an editable grid. `declared_types` carries the types
    the recipe explicitly set, from `pipeline.declared_column_types`.

    Blanks are counted with `blank_mask`, so a cell holding only spaces is reported the
    way it looks — empty — instead of as a filled value.
    """
    row_count = len(frame)
    declared_types = declared_types or {}
    records = []

    for column in frame.columns:
        series = frame[column]
        blanks = blank_mask(series)
        missing = int(blanks.sum())
        samples = series[~blanks].astype("string").head(sample_size).tolist()
        records.append(
            {
                "column": str(column),
                "column_type": effective_column_type(series, declared_types.get(str(column))),
                "non_null": row_count - missing,
                "missing": missing,
                "missing_pct": round(missing / row_count * 100, 1) if row_count else 0.0,
                "unique": int(series.nunique()),
                "sample_values": ", ".join(samples),
            }
        )

    return pd.DataFrame.from_records(
        records,
        columns=["column", "column_type", "non_null", "missing", "missing_pct", "unique", "sample_values"],
    )


def text_columns(frame: pd.DataFrame) -> list[str]:
    """Columns the text-cleanup actions can operate on.

    Numeric and datetime columns are excluded because `str.replace` silently no-ops on
    them — applying a text action there would look like it worked and change nothing.
    """
    return [str(column) for column in frame.columns if is_string_dtype(frame[column])]


def numeric_columns(frame: pd.DataFrame) -> list[str]:
    """Columns that can be summed or averaged.

    The mirror of `text_columns`, and used the same way: to steer a picker towards the
    columns an action can actually work on, rather than letting someone choose a column
    whose sum would come back blank.
    """
    return [str(column) for column in frame.columns if is_numeric_dtype(frame[column])]
