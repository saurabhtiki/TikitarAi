"""What a saved report dataset knows about itself, and how it is written down.

The rows live in a DuckDB file; **this** is everything else the chat needs to make sense
of them. Requirement 5.5 lists what the agent must be told about the data — table and
column names, each column's type, its description and synonyms, and the confirmed links —
and `engine.dictionary.schema_context` is what renders that block. So a saved dataset
stores exactly the two inputs that function takes, and nothing else: rebuild them and the
report chat gets the identical context Chat with Data gives.

Plain dataclasses of JSON primitives, for the reason `engine.dictionary` already gives
about `ColumnEntry`: they are serialised, so they cannot hold anything that isn't.

No Streamlit and no database here — `reports/db.py` stores this JSON and
`reports/store.py` owns the rows it describes.
"""

import json
import logging
from dataclasses import dataclass, field

from engine.dictionary import ColumnEntry
from engine.relationships import Relationship
from reports.exceptions import ReportDataError

logger = logging.getLogger(__name__)

# Bumped only when a stored setup would be read *wrongly* by the current code.
SCHEMA_VERSION = 1


@dataclass
class StoredTable:
    """One table inside a report's dataset file."""

    name: str
    row_count: int = 0


@dataclass
class ReportSetup:
    """A saved dataset's schema: its tables, its column meanings, and its links.

    Attributes:
        tables: what is in the file, in the order it was written.
        dictionary: one entry per column — type, description, synonyms.
        relationships: the confirmed links, stated as joinable pairs in the context block.
    """

    tables: list[StoredTable] = field(default_factory=list)
    dictionary: list[ColumnEntry] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    def table_names(self) -> list[str]:
        return [table.name for table in self.tables]

    def total_rows(self) -> int:
        return sum(int(table.row_count or 0) for table in self.tables)


def to_json(setup: ReportSetup) -> str:
    """Serialises a setup for storage.

    Raises:
        ReportDataError: if it can't be written as JSON.
    """
    try:
        payload = {
            "version": SCHEMA_VERSION,
            "tables": [{"name": table.name, "row_count": int(table.row_count or 0)} for table in setup.tables],
            "dictionary": [
                {
                    "table": entry.table,
                    "column": entry.column,
                    "sql_type": entry.sql_type,
                    "semantic_type": entry.semantic_type,
                    "description": entry.description,
                    "synonyms": list(entry.synonyms or []),
                }
                for entry in setup.dictionary
            ],
            "relationships": [
                {
                    "child_table": link.child_table,
                    "child_column": link.child_column,
                    "parent_table": link.parent_table,
                    "parent_column": link.parent_column,
                }
                for link in setup.relationships
            ],
        }
        return json.dumps(payload, indent=2)
    except (TypeError, ValueError) as error:
        logger.exception("Could not serialise a report dataset setup.")
        raise ReportDataError(f"This report's schema couldn't be saved ({error}).") from error


def from_json(text: str) -> ReportSetup:
    """Rebuilds a setup from stored JSON.

    Unknown keys are ignored and missing ones take their defaults, so a payload written
    before a field existed still opens — the same tolerance `tasks.model.from_json` has.

    Raises:
        ReportDataError: if the text isn't the JSON object this module writes.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("A stored report dataset setup could not be parsed.")
        raise ReportDataError(
            "This report's saved schema couldn't be read — its stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning("A stored report setup was %s, not an object.", type(payload).__name__)
        raise ReportDataError("This report's saved schema isn't in the expected format.")

    version = payload.get("version")
    if version is not None and version > SCHEMA_VERSION:
        logger.warning("A report dataset was saved by a newer version (%s).", version)
        raise ReportDataError(
            "This report's data was saved by a newer version of the app and can't be opened here."
        )

    try:
        tables = [
            StoredTable(name=str(item.get("name", "")), row_count=int(item.get("row_count", 0) or 0))
            for item in payload.get("tables", [])
            if isinstance(item, dict) and item.get("name")
        ]
        dictionary = [
            ColumnEntry(
                table=str(item.get("table", "")),
                column=str(item.get("column", "")),
                sql_type=str(item.get("sql_type", "")),
                semantic_type=str(item.get("semantic_type", "")),
                description=str(item.get("description", "") or ""),
                synonyms=list(item.get("synonyms", []) or []),
            )
            for item in payload.get("dictionary", [])
            if isinstance(item, dict) and item.get("table") and item.get("column")
        ]
        relationships = [
            Relationship(
                child_table=str(item.get("child_table", "")),
                child_column=str(item.get("child_column", "")),
                parent_table=str(item.get("parent_table", "")),
                parent_column=str(item.get("parent_column", "")),
            )
            for item in payload.get("relationships", [])
            if isinstance(item, dict) and item.get("child_table") and item.get("parent_table")
        ]
    except (AttributeError, TypeError, ValueError) as error:
        logger.exception("A stored report dataset setup held unreadable entries.")
        raise ReportDataError(f"This report's saved schema couldn't be read ({error}).") from error

    return ReportSetup(tables=tables, dictionary=dictionary, relationships=relationships)
