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
from dataclasses import dataclass, field

import pandas as pd

from cleaner import loaders, pipeline, profiling
from cleaner.exceptions import DataCleanerError
from engine.exceptions import TableLoadError

logger = logging.getLogger(__name__)

# How many offending values to quote when a declared type won't apply. Enough to recognise
# the problem in the source file, not enough to fill the screen.
MAX_FAILED_EXAMPLES = 3


@dataclass(frozen=True)
class ConversionFailure:
    """A declared type that this month's values won't convert to.

    Requirement 6.6 is deliberate about what happens next: the load is **refused**, not
    tolerated. A date column left as text turns `joining_date < '2024-04-01'` into a string
    comparison that returns wrong rows with no error at all, and in the Checks view that
    becomes a wrong Yes/No in a report. `examples` is what makes the refusal actionable —
    the user goes back to the file and fixes those values.
    """

    column: str
    semantic_type: str
    failed_count: int
    examples: list[str] = field(default_factory=list)


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
    return prepare_raw_frame(read_raw(file_bytes, file_name, sheet_name), source=f"'{file_name}'")


def read_raw(file_bytes: bytes, file_name: str, sheet_name: str | None = None) -> pd.DataFrame:
    """Reads one uploaded table as text, before any typing.

    Exposed on its own because a chat type (requirement 6.6) has to look at the values
    *before* they are typed: whether a column can be read as the saved type is a question
    about the text in the file, and asking it of an already-typed column would mean
    converting twice — the second time from whatever the first conversion produced.

    Raises:
        TableLoadError: if the file can't be read.
    """
    try:
        return loaders.read_table(file_bytes, file_name, sheet_name)
    except DataCleanerError as error:
        logger.exception("Could not read '%s' (sheet %s) for the data engine.", file_name, sheet_name)
        raise TableLoadError(str(error)) from error


def declaration_failures(
    raw: pd.DataFrame, declared: dict[str, str], limit: int = MAX_FAILED_EXAMPLES
) -> list[ConversionFailure]:
    """The declared types this all-text frame won't accept, with the values that refuse.

    Only `numeric` and `date` can fail. `text`, `categorical` and `id` are all stored as
    text, so every value converts by definition — which is also why a chat type saying "id"
    over a column of plain integers is applied happily and keeps them as strings.

    Blank cells are not failures: an empty date is a missing value, not a broken one, and
    refusing a file over them would reject almost every real month's data.
    """
    failures: list[ConversionFailure] = []

    for column, semantic_type in declared.items():
        if column not in raw.columns or semantic_type not in (profiling.NUMERIC, profiling.DATE):
            continue

        series = raw[column]
        present = series[~profiling.blank_mask(series)]
        if present.empty:
            continue

        if semantic_type == profiling.NUMERIC:
            _, failed = profiling.parse_numeric_series(present)
        else:
            _, failed = profiling.parse_datetime_series(present)

        if len(failed) == 0:
            continue

        failures.append(
            ConversionFailure(
                column=column,
                semantic_type=semantic_type,
                failed_count=len(failed),
                examples=[str(value) for value in present.loc[failed].unique()[:limit]],
            )
        )

    return failures


def prepare_raw_frame(
    raw: pd.DataFrame, *, source: str = "this table", declared: dict[str, str] | None = None
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Applies column types to an all-text frame — declared ones where given, detected
    otherwise.

    Split out from `prepare_table` so the Data Cleaner handoff can reuse the identical
    typing path without going back through a file reader.

    `declared` is requirement 6.6's chat type speaking, and it **overrules detection** for
    the columns it names. That is the whole mechanism: a date column whose values are all
    blank this month detects as text, and applying the saved `date` here is what keeps
    every later comparison a real date comparison. Detection still decides every column the
    chat type doesn't mention. Callers that care whether the declared types will actually
    convert must ask `declaration_failures` first — anything unconvertible lands here as a
    blank, exactly as the Data Cleaner's own type step does.

    Raises:
        TableLoadError: if the typing step can't be applied.
    """
    effective = profiling.detect_column_types(raw)
    for column, semantic_type in (declared or {}).items():
        if column in effective:
            effective[column] = semantic_type

    by_column = {
        column: {"target_type": column_type}
        for column, column_type in effective.items()
        if column_type != profiling.TEXT
    }

    if not by_column:
        return raw, effective

    try:
        typed = pipeline.apply_steps(raw, [pipeline.make_step("set_column_types", {"by_column": by_column})])
    except DataCleanerError as error:
        logger.exception("Could not apply column types to %s.", source)
        raise TableLoadError(f"{source} couldn't be typed for analysis.") from error

    return typed, semantic_types(typed, declared=effective)


@dataclass(frozen=True)
class DeclaredLoad:
    """What applying a chat type's saved types did to one table (requirement 6.6).

    `prepare_declared_table` returns one of these alongside the frame so the page can say
    exactly what happened rather than only whether it worked. Everything here is per-column
    and phrased from the file's point of view, because that is where the user goes to fix it.
    """

    retyped: dict[str, str] = field(default_factory=dict)
    failures: list[ConversionFailure] = field(default_factory=list)
    dropped_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """Whether the saved types were actually applied.

        False means the frame came back typed by detection instead — either because a
        column wouldn't convert or because one the chat type expects isn't in the file.
        The caller reports the problem; it does not get a half-typed table.
        """
        return not self.failures and not self.missing_columns


def prepare_declared_table(
    raw: pd.DataFrame, declared: dict[str, str], *, source: str = "this table"
) -> tuple[pd.DataFrame, dict[str, str], DeclaredLoad]:
    """Loads an all-text frame against a chat type's saved column list and types.

    Requirement 6.6's fixed-or-refused rule, in one place:

    - every expected column converts → the saved types are applied, extra columns are
      dropped, and `DeclaredLoad.retyped` names the columns detection would have got wrong
    - anything won't convert, or an expected column isn't there → **nothing is applied**.
      The frame comes back typed by plain detection and `DeclaredLoad` carries the reasons.

    The all-or-nothing part matters. Applying the types that happen to work would leave a
    table that looks like it matched the chat type while one column silently didn't, which
    is precisely the outcome the requirement exists to prevent.

    Raises:
        TableLoadError: if the typing step can't be applied.
    """
    present = list(raw.columns)
    missing = [column for column in declared if column not in present]
    failures = declaration_failures(raw, declared)

    if missing or failures:
        frame, detected = prepare_raw_frame(raw, source=source)
        return frame, detected, DeclaredLoad(failures=failures, missing_columns=missing)

    extras = [column for column in present if column not in declared]
    kept = raw[[column for column in present if column in declared]]

    detected = profiling.detect_column_types(kept)
    retyped = {
        column: detected[column]
        for column, semantic_type in declared.items()
        if detected.get(column) != semantic_type
    }

    frame, applied = prepare_raw_frame(kept, source=source, declared=declared)
    if extras:
        logger.info("Dropped %d column(s) from %s that the chat type doesn't expect.", len(extras), source)

    return frame, applied, DeclaredLoad(retyped=retyped, dropped_columns=extras)


def prepare_cleaned_frame(
    frame: pd.DataFrame, declared_types: dict[str, str] | None = None
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Prepares a frame the Data Cleaner has already cleaned and typed.

    The handoff path (`engine.session.adopt_cleaner_tables`). The recipe has already run,
    so this only reads the semantic types back off the result rather than re-detecting
    and potentially disagreeing with the cleaning log the user just read.
    """
    return frame, semantic_types(frame, declared=declared_types)
