"""What a chat type is, and every pure operation on one (requirement 6.6).

No Streamlit and no database here, for the same reason `checks/model.py` has neither: the
page holds one of these in session state and `chat_types/db.py` stores its JSON, and
keeping both concerns out is what makes the shape testable without `AppTest`.

A chat type is a **recipe for the setup**, not a snapshot of data:

- the expected tables, each with its columns and each column's *semantic* type
- the confirmed relationships
- the column descriptions and synonyms

No rows, no transcript, and deliberately **no calculated-column statements** — those are
requirement 7.5's Task recipe, and replaying one whose column the new file didn't bring
would leave the engine half-applied with no good error to show for it.

The semantic type is the load-bearing part. It is not a label: `engine.loading` converts
the column, so `date` reaches DuckDB as a real DATE and `text` as VARCHAR. That is why
`matching.py` treats a saved type as an instruction rather than an expectation.
"""

import json
import logging
from dataclasses import dataclass, field

from chat_types.exceptions import ChatTypeStorageError
from engine.dictionary import ColumnEntry
from engine.relationships import Relationship

logger = logging.getLogger(__name__)

UNTITLED_CHAT_TYPE = "Untitled chat type"

# The stored JSON carries its own version, so a chat type saved today can be read back
# after the shape changes rather than failing to load with no explanation.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ExpectedColumn:
    """One column a matching upload has to provide.

    `semantic_type` is one of `cleaner.profiling.COLUMN_TYPES`. It is stored rather than
    the SQL type because it is the thing the loader can *apply*: handing it back to
    `engine.loading.prepare_raw_frame` as a declared type is what fixes a column that came
    in as text this month.
    """

    name: str
    semantic_type: str


@dataclass
class ExpectedTable:
    """One table a matching upload has to provide."""

    table_name: str
    columns: list[ExpectedColumn] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    def types_by_column(self) -> dict[str, str]:
        return {column.name: column.semantic_type for column in self.columns}


@dataclass
class SavedDescription:
    """One column's meaning, as Step 3 left it.

    Only the two fields the user actually wrote are stored. The types are not: they belong
    to `ExpectedColumn`, and `engine.dictionary.build_dictionary` carries exactly these two
    across a rebuild anyway — see `restored_dictionary`.
    """

    table: str
    column: str
    description: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class ChatType:
    """A saved Steps 1–3 setup.

    `chat_type_id` is None until it has been saved, exactly as `CheckSet.set_id` is.
    """

    chat_type_id: int | None = None
    name: str = ""
    tables: list[ExpectedTable] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    descriptions: list[SavedDescription] = field(default_factory=list)

    def display_name(self) -> str:
        return self.name.strip() or UNTITLED_CHAT_TYPE

    def table_names(self) -> list[str]:
        return [table.table_name for table in self.tables]

    def table(self, table_name: str) -> ExpectedTable | None:
        """The expected table of this name, matched case-insensitively.

        Case-insensitive because the name is derived from a filename by
        `duckdb_session.slugify_table_name`, and a user who renames `Salary Master.xlsx`
        to `SALARY MASTER.xlsx` has not changed which table it is.
        """
        wanted = table_name.strip().lower()
        for table in self.tables:
            if table.table_name.lower() == wanted:
                return table
        return None

    def restored_dictionary(self) -> list[ColumnEntry]:
        """The saved descriptions as dictionary entries, ready to seed the engine.

        The types are left blank on purpose. These entries are never rendered directly —
        they are passed to `engine.dictionary.build_dictionary` as `existing=`, which reads
        only `description` and `synonyms` off them and takes every type from the tables as
        they were actually loaded. Restoring a stored type here would let a saved chat type
        claim a column is a date while the loaded column is text.
        """
        return [
            ColumnEntry(
                table=saved.table,
                column=saved.column,
                sql_type="",
                semantic_type="",
                description=saved.description,
                synonyms=list(saved.synonyms),
            )
            for saved in self.descriptions
        ]


def capture(
    name: str,
    semantic_types_by_table: dict[str, dict[str, str]],
    relationships: list[Relationship],
    dictionary: list[ColumnEntry],
    *,
    chat_type_id: int | None = None,
) -> ChatType:
    """Builds a chat type from the engine state currently on screen.

    Takes plain dicts and lists rather than `engine.session`'s objects, so this module
    never imports the one part of `engine/` that pulls in Streamlit.

    Only described columns are captured. A dictionary is mostly blank rows — storing them
    would triple the payload to say nothing, and a blank description restores to a blank
    description either way.
    """
    tables = [
        ExpectedTable(
            table_name=table_name,
            columns=[ExpectedColumn(name=column, semantic_type=semantic_type) for column, semantic_type in types.items()],
        )
        for table_name, types in semantic_types_by_table.items()
    ]

    descriptions = [
        SavedDescription(
            table=entry.table,
            column=entry.column,
            description=entry.description.strip(),
            synonyms=list(entry.synonyms),
        )
        for entry in dictionary
        if entry.description.strip() or entry.synonyms
    ]

    return ChatType(
        chat_type_id=chat_type_id,
        name=name.strip(),
        tables=tables,
        relationships=list(relationships),
        descriptions=descriptions,
    )


def column_count(chat_type: ChatType) -> int:
    """Every expected column across every expected table — the picker's summary line."""
    return sum(len(table.columns) for table in chat_type.tables)


# --------------------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------------------


def to_json(chat_type: ChatType) -> str:
    """Serialises a chat type for storage. The name and id live in their own columns."""
    payload = {
        "version": SCHEMA_VERSION,
        "tables": [
            {
                "table_name": table.table_name,
                "columns": [
                    {"name": column.name, "semantic_type": column.semantic_type} for column in table.columns
                ],
            }
            for table in chat_type.tables
        ],
        "relationships": [
            {
                "child_table": relationship.child_table,
                "child_column": relationship.child_column,
                "parent_table": relationship.parent_table,
                "parent_column": relationship.parent_column,
            }
            for relationship in chat_type.relationships
        ],
        "descriptions": [
            {
                "table": saved.table,
                "column": saved.column,
                "description": saved.description,
                "synonyms": list(saved.synonyms),
            }
            for saved in chat_type.descriptions
        ],
    }
    return json.dumps(payload, indent=2)


def _table_from_dict(raw: dict) -> ExpectedTable:
    columns = [
        ExpectedColumn(name=str(column.get("name") or ""), semantic_type=str(column.get("semantic_type") or ""))
        for column in raw.get("columns") or []
        if isinstance(column, dict) and str(column.get("name") or "").strip()
    ]
    return ExpectedTable(table_name=str(raw.get("table_name") or ""), columns=columns)


def _relationship_from_dict(raw: dict) -> Relationship | None:
    fields = [
        str(raw.get(key) or "")
        for key in ("child_table", "child_column", "parent_table", "parent_column")
    ]
    # A half-written link would come back as a relationship joining a table to nothing,
    # which `relationships.enforce` would then fail on with a catalog error. Dropping it is
    # the smaller loss, and the user still has the other links.
    if not all(fields):
        logger.warning("Skipping an incomplete relationship in a stored chat type.")
        return None
    return Relationship(*fields)


def from_json(text: str, *, chat_type_id: int | None = None, name: str = "") -> ChatType:
    """Rebuilds a chat type from stored JSON.

    Raises:
        ChatTypeStorageError: if the text isn't the JSON object this module writes. The
            caller only swaps the loaded chat type in once this returns, so a partial load
            never replaces the one already selected.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("Stored chat type %s could not be parsed.", chat_type_id)
        raise ChatTypeStorageError(
            "This saved chat type couldn't be read — its stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning("Stored chat type %s was %s, not an object.", chat_type_id, type(payload).__name__)
        raise ChatTypeStorageError("This saved chat type couldn't be read — it isn't in the expected format.")

    version = payload.get("version")
    if version is not None and version > SCHEMA_VERSION:
        logger.warning("Chat type %s was saved by a newer version (%s).", chat_type_id, version)
        raise ChatTypeStorageError(
            "This chat type was saved by a newer version of the app and can't be opened here."
        )

    raw_tables = payload.get("tables") if isinstance(payload.get("tables"), list) else []
    raw_links = payload.get("relationships") if isinstance(payload.get("relationships"), list) else []
    raw_descriptions = payload.get("descriptions") if isinstance(payload.get("descriptions"), list) else []

    tables = [_table_from_dict(raw) for raw in raw_tables if isinstance(raw, dict)]
    relationships = [_relationship_from_dict(raw) for raw in raw_links if isinstance(raw, dict)]
    descriptions = [
        SavedDescription(
            table=str(raw.get("table") or ""),
            column=str(raw.get("column") or ""),
            description=str(raw.get("description") or ""),
            synonyms=[str(word) for word in raw.get("synonyms") or []],
        )
        for raw in raw_descriptions
        if isinstance(raw, dict)
    ]

    return ChatType(
        chat_type_id=chat_type_id,
        name=name,
        tables=[table for table in tables if table.table_name],
        relationships=[link for link in relationships if link is not None],
        descriptions=descriptions,
    )
