"""Checking an upload against the chat type it was loaded under (requirement 6.6).

Pure: no Streamlit, no database, no DuckDB. `engine.loading.prepare_declared_table` does
the per-table work while the files are being read — it is the only place that can, since
whether a column converts is a question about the text in the file — and this module turns
what it found into one report for the whole upload, plus the sentences the page prints.

Two rules shape everything here.

**Every problem at once.** A user with three missing columns should fix three columns and
re-upload once, not discover them one refused load at a time. So nothing raises: a mismatch
is a `MatchReport` with things in it.

**Blocking and worth mentioning are different.** A missing column means the setup cannot be
applied. An extra column means one was dropped, which the user should know about but which
stops nothing. Mixing the two would either bury the real problem or make an ordinary tidy-up
look like a failure.
"""

import logging
from dataclasses import dataclass, field

from chat_types.model import ChatType
from engine.loading import ConversionFailure, DeclaredLoad

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MissingColumn:
    """A column the chat type expects that the uploaded file doesn't have."""

    table: str
    column: str


@dataclass(frozen=True)
class RetypedColumn:
    """A column loaded as its saved type rather than the one detection would have chosen."""

    table: str
    column: str
    detected: str
    applied: str


@dataclass(frozen=True)
class RefusedColumn:
    """A column whose values won't convert to the saved type. Always blocking."""

    table: str
    column: str
    semantic_type: str
    failed_count: int
    examples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MismatchedColumn:
    """A column whose loaded type isn't the saved one, on a table that can't be re-read.

    Only ever raised for a table the declared load never saw — one adopted from the Data
    Cleaner, or detached from the uploader after a trip to another page. There is no raw
    text left to re-type it from, so this blocks rather than being fixed silently: the
    remedy is to upload the file again.
    """

    table: str
    column: str
    expected: str
    actual: str


@dataclass(frozen=True)
class DroppedColumn:
    """An extra column the chat type doesn't expect, left out of the loaded table."""

    table: str
    column: str


@dataclass
class MatchReport:
    """Everything that differed between an upload and the chat type it was checked against."""

    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[MissingColumn] = field(default_factory=list)
    refused_columns: list[RefusedColumn] = field(default_factory=list)
    mismatched_columns: list[MismatchedColumn] = field(default_factory=list)
    retyped_columns: list[RetypedColumn] = field(default_factory=list)
    dropped_columns: list[DroppedColumn] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)
    matched_tables: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the chat type's setup can be applied to what was uploaded."""
        return not (
            self.missing_tables
            or self.missing_columns
            or self.refused_columns
            or self.mismatched_columns
        )

    @property
    def has_notes(self) -> bool:
        return bool(self.retyped_columns or self.dropped_columns or self.extra_tables)

    def problems(self) -> list[str]:
        """The blocking differences, as sentences naming the file and the column.

        Ordered tables-then-columns-then-values, which is the order the user can fix them
        in: a missing file explains its own missing columns.
        """
        lines = [
            f"**{table}** — no uploaded file matches this table." for table in self.missing_tables
        ]
        lines += [
            f"**{missing.column}** is missing in **{missing.table}**."
            for missing in self.missing_columns
        ]
        for refused in self.refused_columns:
            examples = ", ".join(f"'{value}'" for value in refused.examples)
            lines.append(
                f"**{refused.column}** in **{refused.table}** couldn't be read as "
                f"{_type_label(refused.semantic_type)} — {refused.failed_count:,} value(s) "
                f"like {examples} won't convert."
            )
        lines += [
            f"**{mismatch.column}** in **{mismatch.table}** is "
            f"{_type_label(mismatch.actual)} but this chat type expects "
            f"{_type_label(mismatch.expected)}. Upload the file again so it can be read as saved."
            for mismatch in self.mismatched_columns
        ]
        return lines

    def notes(self) -> list[str]:
        """The differences worth mentioning that stopped nothing."""
        lines = [
            f"**{retyped.column}** in **{retyped.table}** came in as "
            f"{_type_label(retyped.detected)} and was read as {_type_label(retyped.applied)}, "
            "as saved in this chat type."
            for retyped in self.retyped_columns
        ]
        if self.dropped_columns:
            listed = ", ".join(f"**{dropped.table}.{dropped.column}**" for dropped in self.dropped_columns)
            lines.append(f"{len(self.dropped_columns)} extra column(s) not imported: {listed}.")
        if self.extra_tables:
            listed = ", ".join(f"**{table}**" for table in self.extra_tables)
            lines.append(f"{len(self.extra_tables)} extra file(s) not imported: {listed}.")
        return lines

    def summary(self) -> str:
        """The one line the page puts on the match panel."""
        if not self.ok:
            return f"{len(self.problems())} problem(s) to fix before this chat type can be used."
        matched = len(self.matched_tables)
        if self.has_notes:
            return f"{matched} table(s) matched, with {len(self.notes())} thing(s) worth knowing."
        return f"{matched} table(s) matched this chat type exactly."


# Semantic types are stored as bare lowercase words, which read badly mid-sentence.
_TYPE_LABELS = {
    "text": "Text",
    "categorical": "Category",
    "numeric": "a Number",
    "date": "a Date",
    "id": "an ID",
}


def _type_label(semantic_type: str) -> str:
    return _TYPE_LABELS.get(semantic_type, semantic_type or "Text")


def normalise(table_name: str) -> str:
    """The form two table names are compared in.

    Case-insensitive because the name is derived from a filename: renaming
    `Salary Master.xlsx` to `SALARY MASTER.xlsx` has not changed which table it is.
    """
    return table_name.strip().lower()


def check_upload(
    chat_type: ChatType, outcomes: dict[str, DeclaredLoad], loaded_types: dict[str, dict[str, str]]
) -> MatchReport:
    """Builds the whole-upload report.

    Args:
        chat_type: the setup the upload is being checked against.
        outcomes: what `engine.loading.prepare_declared_table` found, per loaded table name.
        loaded_types: every table currently loaded and its columns' semantic types, as they
            actually stand — `engine.session.semantic_types_by_table()`.

    A table with no entry in `outcomes` is one the declared load never saw: adopted from
    the Data Cleaner, or detached from the uploader. There is no raw text left to re-type
    it from, so its types are checked as they stand instead of being applied.

    Never raises. An upload that doesn't match is a report, not an exception — see the
    module docstring.
    """
    report = MatchReport()
    expected = {normalise(name): name for name in chat_type.table_names()}
    actual = {normalise(name): name for name in loaded_types}

    for key, expected_name in expected.items():
        if key not in actual:
            report.missing_tables.append(expected_name)
            continue

        table_name = actual[key]
        expected_types = chat_type.table(expected_name).types_by_column()
        report.matched_tables.append(table_name)

        outcome = outcomes.get(table_name)
        if outcome is None:
            _verify_as_loaded(report, table_name, expected_types, loaded_types[table_name])
        else:
            _fold_in(report, table_name, outcome, expected_types)

    report.extra_tables = [name for key, name in actual.items() if key not in expected]

    if not report.ok:
        logger.info(
            "Upload didn't match chat type '%s': %d missing table(s), %d missing column(s), %d refused column(s).",
            chat_type.display_name(),
            len(report.missing_tables),
            len(report.missing_columns),
            len(report.refused_columns),
        )
    return report


def _fold_in(
    report: MatchReport, table_name: str, outcome: DeclaredLoad, expected_types: dict[str, str]
) -> None:
    """Adds one table's load outcome to the report, qualified by the table it happened in.

    `DeclaredLoad.retyped` records what detection *would* have said. What was actually
    applied is the chat type's own saved type, which is `expected_types` — the outcome
    doesn't carry it, because it would only be repeating what the caller already holds.
    """
    report.missing_columns += [MissingColumn(table_name, column) for column in outcome.missing_columns]
    report.refused_columns += [_refused(table_name, failure) for failure in outcome.failures]
    report.retyped_columns += [
        RetypedColumn(table_name, column, detected, expected_types.get(column, ""))
        for column, detected in outcome.retyped.items()
    ]
    report.dropped_columns += [DroppedColumn(table_name, column) for column in outcome.dropped_columns]


def _verify_as_loaded(
    report: MatchReport, table_name: str, expected_types: dict[str, str], actual_types: dict[str, str]
) -> None:
    """Checks a table the declared load never touched, against the types it already has.

    The Data Cleaner handoff and any table detached from the uploader arrive here. Nothing
    can be applied to them, so the only useful question is whether what they arrived with
    is what the chat type says it should be — and saying nothing would let a text date
    column through under a green banner, which is the failure this whole feature is about.

    Extra columns are ignored rather than reported: nothing dropped them, and calling them
    a problem the user can't act on would only stand between them and their data.
    """
    for column, expected in expected_types.items():
        if column not in actual_types:
            report.missing_columns.append(MissingColumn(table_name, column))
        elif actual_types[column] != expected:
            report.mismatched_columns.append(
                MismatchedColumn(table_name, column, expected, actual_types[column])
            )


def _refused(table_name: str, failure: ConversionFailure) -> RefusedColumn:
    return RefusedColumn(
        table=table_name,
        column=failure.column,
        semantic_type=failure.semantic_type,
        failed_count=failure.failed_count,
        examples=list(failure.examples),
    )
