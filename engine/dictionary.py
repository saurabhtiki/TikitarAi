"""The column data dictionary: descriptions, synonyms, and the agent's schema context.

Requirement 5.3's payoff is `schema_context()` — the block injected into the agent's
prompt for every question. It carries table and column names, the confirmed
relationships (enforced as a foreign key or not — see `engine.relationships.enforceable`),
and each column's description and synonyms, which is what turns "how many units did we
shift?" into SQL that finds `qty`.

Every entry is keyed on `(table, column)`, never on the column name alone. That is what
resolves requirement 5.3's stated ambiguity between `Customer.Name` and `Stock.Name`:
they are two entries with two descriptions, and the rendered context always qualifies
them.

In Chat with Data these entries live only for the session. In Task Builder (Stage 7)
the same objects are serialized as the Task's schema signature, which is why they are
plain dataclasses of JSON primitives.
"""

import logging
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from engine.duckdb_session import describe_table, quote_identifier
from engine.exceptions import DataEngineError

logger = logging.getLogger(__name__)

SAMPLE_VALUE_LIMIT = 5

# The grid's column order, shared by the editor and the merge, so a reordered display
# can't silently reattach one column's description to another.
GRID_COLUMNS = ["table", "column", "type", "description", "also_known_as"]


@dataclass
class ColumnEntry:
    """One column's dictionary entry."""

    table: str
    column: str
    sql_type: str
    semantic_type: str
    description: str = ""
    synonyms: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.table, self.column)

    @property
    def qualified_name(self) -> str:
        return f"{self.table}.{self.column}"


def parse_synonyms(text: str) -> list[str]:
    """Splits a comma-separated synonym field into a clean list.

    Requirement 5.3 wants several synonyms per column (`qty` → "quantity, units, units
    sold, count"), and a plain comma-separated text box is the least friction for that.
    Blanks are dropped and order is preserved, with case-insensitive de-duplication.
    """
    seen: set[str] = set()
    result: list[str] = []
    for part in str(text or "").split(","):
        token = part.strip()
        if token and token.lower() not in seen:
            seen.add(token.lower())
            result.append(token)
    return result


def sample_values(
    connection: duckdb.DuckDBPyConnection, table: str, column: str, limit: int = SAMPLE_VALUE_LIMIT
) -> list[str]:
    """Returns a few distinct non-blank values from a column.

    Feeds the Light Model's suggestion prompt: requirement 5.3's example turns `qty`
    with values `5, 12, 3` into "Number of units sold per transaction", which the name
    alone would never yield.

    Never raises — samples are a nicety, and losing them should cost a weaker suggestion
    rather than the whole dictionary.
    """
    try:
        rows = connection.execute(
            f"SELECT DISTINCT CAST({quote_identifier(column)} AS VARCHAR) AS value "
            f"FROM {quote_identifier(table)} "
            f"WHERE {quote_identifier(column)} IS NOT NULL LIMIT {int(limit)}"
        ).fetchall()
    except duckdb.Error:
        logger.warning("Could not sample values from %s.%s.", table, column, exc_info=True)
        return []

    return [str(value) for (value,) in rows if str(value).strip()]


def build_dictionary(
    connection: duckdb.DuckDBPyConnection,
    tables: list[str],
    semantic_types: dict[str, dict[str, str]] | None = None,
    existing: list[ColumnEntry] | None = None,
) -> list[ColumnEntry]:
    """Builds one entry per column across every table.

    `existing` carries the entries already on screen, so descriptions the user has typed
    survive a rebuild, a new upload, or an added calculated column. A column that has
    disappeared drops out; a new one arrives blank. Losing typed descriptions on every
    schema change would make the dictionary not worth filling in.
    """
    semantic_types = semantic_types or {}
    previous = {entry.key: entry for entry in (existing or [])}
    entries: list[ColumnEntry] = []

    for table in tables:
        try:
            columns = describe_table(connection, table)
        except DataEngineError:
            logger.exception("Skipping '%s' in the dictionary — it couldn't be described.", table)
            continue

        for column, sql_type in columns:
            carried = previous.get((table, column))
            entries.append(
                ColumnEntry(
                    table=table,
                    column=column,
                    sql_type=sql_type,
                    semantic_type=semantic_types.get(table, {}).get(column, ""),
                    description=carried.description if carried else "",
                    synonyms=list(carried.synonyms) if carried else [],
                )
            )

    return entries


def to_grid(entries: list[ColumnEntry]) -> pd.DataFrame:
    """Renders the dictionary as the editable grid the user sees."""
    return pd.DataFrame.from_records(
        [
            {
                "table": entry.table,
                "column": entry.column,
                "type": entry.semantic_type or entry.sql_type,
                "description": entry.description,
                "also_known_as": ", ".join(entry.synonyms),
            }
            for entry in entries
        ],
        columns=GRID_COLUMNS,
    )


def merge_edits(entries: list[ColumnEntry], edited: pd.DataFrame) -> list[ColumnEntry]:
    """Folds the grid's edits back into the entries.

    Matched on `(table, column)` rather than row position, because a sorted or filtered
    grid returns its rows in display order — merging positionally would attach one
    column's description to another, and the user would have no way to tell.

    Only `description` and `also_known_as` are read back; table, column and type are
    display-only, so an edit there is ignored rather than corrupting the schema.
    """
    if edited is None or edited.empty:
        return entries

    by_key = {
        (str(row["table"]), str(row["column"])): row
        for _, row in edited.iterrows()
        if "table" in edited.columns and "column" in edited.columns
    }

    merged: list[ColumnEntry] = []
    for entry in entries:
        row = by_key.get(entry.key)
        if row is None:
            merged.append(entry)
            continue
        merged.append(
            ColumnEntry(
                table=entry.table,
                column=entry.column,
                sql_type=entry.sql_type,
                semantic_type=entry.semantic_type,
                description=str(row.get("description") or "").strip(),
                synonyms=parse_synonyms(row.get("also_known_as")),
            )
        )
    return merged


def apply_suggestions(entries: list[ColumnEntry], suggestions: dict, *, overwrite: bool = False) -> list[ColumnEntry]:
    """Merges Light-Model suggestions into the entries.

    Blank entries are filled by default and anything the user has already written is
    left alone — requirement 5.3 has the user reviewing and editing suggestions, not
    being overruled by them. `overwrite=True` is the explicit "suggest for all columns"
    path.
    """
    updated: list[ColumnEntry] = []
    for entry in entries:
        suggestion = suggestions.get(entry.key)
        if suggestion is None or (entry.description and not overwrite):
            updated.append(entry)
            continue
        updated.append(
            ColumnEntry(
                table=entry.table,
                column=entry.column,
                sql_type=entry.sql_type,
                semantic_type=entry.semantic_type,
                description=suggestion.description.strip(),
                synonyms=list(suggestion.synonyms or []),
            )
        )
    return updated


def describe_progress(entries: list[ColumnEntry]) -> tuple[int, int]:
    """Returns `(described, total)` — the step-3 summary line."""
    return sum(1 for entry in entries if entry.description.strip()), len(entries)


def schema_context(entries: list[ColumnEntry], relationships: list | None = None) -> str:
    """Renders the schema block injected into the agent's prompt for every question.

    Requirement 5.5 lists exactly what the agent needs: table and column names, the
    foreign keys, and the descriptions and aliases. Foreign keys are stated as joinable
    pairs because that is the form the agent has to write — telling it a constraint
    exists is less useful than telling it which two columns to join on.
    """
    if not entries:
        return "No tables are loaded."

    lines: list[str] = ["Tables and columns:"]
    by_table: dict[str, list[ColumnEntry]] = {}
    for entry in entries:
        by_table.setdefault(entry.table, []).append(entry)

    for table, table_entries in by_table.items():
        lines.append(f"\nTable {table}:")
        for entry in table_entries:
            parts = [f"  - {entry.column} ({entry.sql_type}"]
            parts.append(f", {entry.semantic_type})" if entry.semantic_type else ")")
            if entry.description:
                parts.append(f": {entry.description}")
            if entry.synonyms:
                parts.append(f" [also called: {', '.join(entry.synonyms)}]")
            lines.append("".join(parts))

    if relationships:
        lines.append("\nRelationships (join on these):")
        for relationship in relationships:
            lines.append(
                f"  - {relationship.child_table}.{relationship.child_column} = "
                f"{relationship.parent_table}.{relationship.parent_column}"
            )
        lines.append(
            "\nSome of these may have rows with no match on the other side, so prefer a "
            "LEFT JOIN unless the question needs only rows that match on both sides."
        )

    return "\n".join(lines)
