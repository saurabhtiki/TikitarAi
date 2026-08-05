"""Detecting, checking, and enforcing relationships between the session's tables.

Requirement 5.2 asks for real foreign keys, not informational metadata: financial data
needs accurate results, so a confirmed relationship becomes an actual DuckDB
`FOREIGN KEY`. Three facts about DuckDB shape everything here, all verified against a
live connection rather than assumed:

1. **Foreign keys cannot be added with `ALTER TABLE`.** They must appear inside
   `CREATE TABLE`. So confirming relationships is not an alteration, it is a **rebuild**
   of the working tables from their immutable base tables.
2. **A foreign key must reference a `PRIMARY KEY` or `UNIQUE` column.** Requirement 5.2
   only calls for the orphan pre-check; DuckDB forces a second one, because a parent key
   that repeats cannot carry the constraint at all. Both checks run before enforcement
   and both report their offending rows in full.
3. **A parent table cannot be dropped while a child still references it.** Rebuilds
   therefore drop children first (reverse topological order) and create parents first.

The pre-checks are the real correctness guarantee, and they are what the user sees. The
constraint is the enforcement that keeps the guarantee true for every later query.
"""

import logging
import re
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from engine.duckdb_session import base_name, describe_table, quote_identifier, row_count
from engine.exceptions import DataEngineError, RelationshipError

logger = logging.getLogger(__name__)

# How many offending rows to pull back for display. The user needs enough to find the
# problem in the source file, not the entire failing join.
MAX_OFFENDING_ROWS = 500

# A candidate needs most of the child's values to land in the parent before it's worth
# suggesting. Set below 1.0 deliberately: a real relationship with a handful of bad rows
# is exactly the case requirement 5.2 wants surfaced, not hidden.
MIN_CONTAINMENT = 0.60

# Below this, the "parent" side repeats too much to be a key at all.
MIN_PARENT_UNIQUENESS = 0.99

_ID_SUFFIX = re.compile(r"_?(id|no|code|key|num|number|ref)$", re.IGNORECASE)

# Column names that identify *the table they sit on* rather than anything of their own.
# `customer.id` means "customer", which is what lets it pair with `sales.cust_id`.
_GENERIC_KEY_NAMES = {"id", "no", "code", "key", "num", "number", "ref", "pk"}

# Shortest abbreviation trusted as a prefix match. Three is deliberate, not arbitrary:
# requirement 5.2's own example is `emp_id` against an Employee master, and `emp` is
# three characters. False pairs this admits are filtered by the value-overlap gate
# below, and in any case only ever reach the user as a suggestion to review.
MIN_ABBREVIATION_LENGTH = 3


@dataclass(frozen=True)
class Relationship:
    """One confirmed link: every `child_table.child_column` value exists in the parent."""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str

    @property
    def label(self) -> str:
        return f"{self.child_table}.{self.child_column} → {self.parent_table}.{self.parent_column}"


@dataclass
class RelationshipCheck:
    """The verdict on one candidate, with the rows behind it."""

    relationship: Relationship
    ok: bool
    message: str
    containment: float = 0.0
    duplicate_parent_keys: pd.DataFrame = field(default_factory=pd.DataFrame)
    orphan_rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    orphan_count: int = 0


@dataclass
class RelationshipCandidate:
    """A suggested link, ranked, with the numbers that justify it."""

    relationship: Relationship
    containment: float
    parent_uniqueness: float
    matched_values: int
    distinct_child_values: int

    @property
    def confidence(self) -> float:
        return round(self.containment * self.parent_uniqueness, 4)

    def summary(self) -> str:
        """The plain-English justification shown next to the suggestion."""
        return (
            f"{self.containment:.0%} of {self.relationship.child_table}."
            f"{self.relationship.child_column} values exist in {self.relationship.parent_table}."
            f"{self.relationship.parent_column}"
        )


def _column_tokens(table: str, column: str) -> set[str]:
    """The identities a column might be naming.

    A set rather than one normalized string, because the same column legitimately goes
    by several names and a single token can't represent that. `sales.cust_id` is both
    "cust_id" and "cust"; `customer.id` is not "id" at all — it is "customer".

    That last case is the important one. A bare `id`, `code` or `key` identifies the
    table it sits on, so it returns the *table's* name and deliberately drops the raw
    column name. Keeping "id" would make `customer.id` and `stock.id` look like the same
    thing, which is the most obvious wrong pairing this could produce.
    """
    raw = str(column).strip().lower()
    table_token = str(table).strip().lower()
    singular = table_token[:-1] if table_token.endswith("s") else table_token

    if raw in _GENERIC_KEY_NAMES:
        return {token for token in (table_token, singular) if token}

    tokens = {raw}
    stripped = _ID_SUFFIX.sub("", raw).strip("_")
    if stripped:
        tokens.add(stripped)

    # `customer_id` sitting on the `customer` table names its own key, so it reduces the
    # same way a bare `id` would.
    for prefix in (f"{table_token}_", f"{singular}_"):
        if raw.startswith(prefix) and len(raw) > len(prefix):
            rest = raw[len(prefix) :]
            tokens.add(rest)
            tokens.add(_ID_SUFFIX.sub("", rest).strip("_"))

    return {token for token in tokens if token}


def _tokens_match(left: set[str], right: set[str]) -> bool:
    """True if two columns plausibly name the same thing.

    Exact overlap first, then abbreviation: `cust` against `customer`, `emp` against
    `employee`. Abbreviation is one-directional prefixing only — `emp` matches
    `employee` but `dept` does not match `depot`, which shares no prefix past `dep`…
    it does, in fact, and that is the point: this stage only proposes candidates. The
    value-overlap query in `_score_pair` is what decides, and a wrong pair fails it.
    """
    if left & right:
        return True

    for left_token in left:
        for right_token in right:
            shorter, longer = sorted((left_token, right_token), key=len)
            if len(shorter) >= MIN_ABBREVIATION_LENGTH and longer.startswith(shorter):
                return True
    return False


def _column_stats(connection: duckdb.DuckDBPyConnection, table: str, column: str) -> tuple[int, int]:
    """Returns `(present_rows, distinct_values)` for one column, blanks excluded."""
    quoted_table, quoted_column = quote_identifier(table), quote_identifier(column)
    result = connection.execute(
        f"SELECT count({quoted_column}), count(DISTINCT {quoted_column}) FROM {quoted_table}"
    ).fetchone()
    return (int(result[0]), int(result[1])) if result else (0, 0)


def _overlap(
    connection: duckdb.DuckDBPyConnection, relationship: Relationship
) -> tuple[int, int]:
    """Returns `(distinct_child_values, how_many_of_them_exist_in_the_parent)`.

    Compared as text on both sides, because a `cust_id` read as VARCHAR on one table and
    BIGINT on the other is the same key to the user and would otherwise never match.
    """
    child = quote_identifier(relationship.child_table)
    child_column = quote_identifier(relationship.child_column)
    parent = quote_identifier(relationship.parent_table)
    parent_column = quote_identifier(relationship.parent_column)

    result = connection.execute(
        f"""
        WITH child_values AS (
            SELECT DISTINCT CAST({child_column} AS VARCHAR) AS key_value
            FROM {child} WHERE {child_column} IS NOT NULL
        ),
        parent_values AS (
            SELECT DISTINCT CAST({parent_column} AS VARCHAR) AS key_value
            FROM {parent} WHERE {parent_column} IS NOT NULL
        )
        SELECT
            (SELECT count(*) FROM child_values),
            (SELECT count(*) FROM child_values
             WHERE key_value IN (SELECT key_value FROM parent_values))
        """
    ).fetchone()
    return (int(result[0]), int(result[1])) if result else (0, 0)


def suggest_relationships(
    connection: duckdb.DuckDBPyConnection, tables: list[str]
) -> list[RelationshipCandidate]:
    """Proposes candidate relationships across the session's tables.

    Two signals, exactly as requirement 5.2 describes: matching column names, then a
    real value-overlap query between the candidate columns. Name matching alone would
    happily pair two unrelated `name` columns; overlap alone would compare every column
    against every other, which is quadratic in columns and pairs `amount` with `qty`.

    Direction is decided by the data, not the names: the side whose values are unique is
    the parent. When both are unique it's a one-to-one link and the pair is emitted in
    the order the tables were given.

    Nothing is auto-accepted — requirement 5.2 has every suggestion reviewed. Never
    raises: a table that can't be inspected is skipped and logged, because a failed
    suggestion should cost the user a manual entry, not the whole screen.
    """
    columns_by_table: dict[str, list[tuple[str, str]]] = {}
    for table in tables:
        try:
            columns_by_table[table] = describe_table(connection, table)
        except DataEngineError:
            logger.exception("Skipping '%s' in relationship suggestion — it couldn't be described.", table)

    candidates: list[RelationshipCandidate] = []
    seen: set[frozenset[tuple[str, str]]] = set()

    for left_table, left_columns in columns_by_table.items():
        for right_table, right_columns in columns_by_table.items():
            if left_table >= right_table:
                continue

            for left_column, _ in left_columns:
                left_tokens = _column_tokens(left_table, left_column)
                for right_column, _ in right_columns:
                    if not _tokens_match(left_tokens, _column_tokens(right_table, right_column)):
                        continue

                    pair = frozenset({(left_table, left_column), (right_table, right_column)})
                    if pair in seen:
                        continue
                    seen.add(pair)

                    candidate = _score_pair(
                        connection, left_table, left_column, right_table, right_column
                    )
                    if candidate is not None:
                        candidates.append(candidate)

    candidates.sort(key=lambda item: (item.confidence, item.matched_values), reverse=True)
    return candidates


def _parent_score(column: str, uniqueness: float) -> tuple[float, int]:
    """How parent-like one side of a candidate pair is. Higher wins.

    Uniqueness decides first — a repeating column cannot be anyone's key. The tiebreak
    is what matters in practice, though: with genuinely one-to-one sample data both
    sides are perfectly unique, and picking alphabetically gets the direction backwards
    half the time. A bare `id` or `code` is a primary key naming its own table, while
    `cust_id` on `sales` is a reference to somewhere else — so the generic name is the
    parent, which is right for `sales.cust_id → customer.id` and every shape like it.
    """
    is_generic_key = 1 if str(column).strip().lower() in _GENERIC_KEY_NAMES else 0
    return (round(uniqueness, 6), is_generic_key)


def _score_pair(
    connection: duckdb.DuckDBPyConnection,
    left_table: str,
    left_column: str,
    right_table: str,
    right_column: str,
) -> RelationshipCandidate | None:
    """Scores one name-matched column pair, choosing the direction and returning None
    if neither direction is strong enough to be worth showing."""
    try:
        left_present, left_distinct = _column_stats(connection, left_table, left_column)
        right_present, right_distinct = _column_stats(connection, right_table, right_column)
    except duckdb.Error:
        logger.exception("Could not gather stats for %s.%s / %s.%s.", left_table, left_column, right_table, right_column)
        return None

    if left_present == 0 or right_present == 0:
        return None

    left_uniqueness = left_distinct / left_present
    right_uniqueness = right_distinct / right_present

    left_score = _parent_score(left_column, left_uniqueness)
    right_score = _parent_score(right_column, right_uniqueness)

    if right_score >= left_score:
        relationship = Relationship(left_table, left_column, right_table, right_column)
        parent_uniqueness = right_uniqueness
    else:
        relationship = Relationship(right_table, right_column, left_table, left_column)
        parent_uniqueness = left_uniqueness

    if parent_uniqueness < MIN_PARENT_UNIQUENESS:
        return None

    try:
        distinct_child_values, matched = _overlap(connection, relationship)
    except duckdb.Error:
        logger.exception("Could not measure overlap for %s.", relationship.label)
        return None

    if distinct_child_values == 0:
        return None

    containment = matched / distinct_child_values
    if containment < MIN_CONTAINMENT:
        return None

    return RelationshipCandidate(
        relationship=relationship,
        containment=round(containment, 4),
        parent_uniqueness=round(parent_uniqueness, 4),
        matched_values=matched,
        distinct_child_values=distinct_child_values,
    )


def duplicate_parent_keys(
    connection: duckdb.DuckDBPyConnection, relationship: Relationship, limit: int = MAX_OFFENDING_ROWS
) -> pd.DataFrame:
    """Returns the parent rows whose key value repeats.

    DuckDB requires a foreign key's parent column to be UNIQUE, so a repeated key makes
    the relationship impossible — and the repetition is nearly always the real data
    problem (a master file with a duplicated record), which is why the full rows come
    back rather than just the offending values.
    """
    parent = quote_identifier(relationship.parent_table)
    parent_column = quote_identifier(relationship.parent_column)

    return connection.execute(
        f"""
        SELECT * FROM {parent}
        WHERE {parent_column} IN (
            SELECT {parent_column} FROM {parent}
            WHERE {parent_column} IS NOT NULL
            GROUP BY {parent_column} HAVING count(*) > 1
        )
        ORDER BY {parent_column}
        LIMIT {int(limit)}
        """
    ).df()


def orphan_rows(
    connection: duckdb.DuckDBPyConnection, relationship: Relationship, limit: int = MAX_OFFENDING_ROWS
) -> tuple[pd.DataFrame, int]:
    """Returns the child rows whose key has no match in the parent, and the total count.

    Requirement 5.2 is explicit that these come back **in full** — every column's value,
    not a row number — because that is what makes the row findable in the source file.
    Hence `SELECT child.*` rather than a key list.

    Rows whose key is NULL are excluded: a foreign key permits NULLs, so they are not
    orphans and flagging them would send the user hunting for a problem that isn't one.
    """
    child = quote_identifier(relationship.child_table)
    child_column = quote_identifier(relationship.child_column)
    parent = quote_identifier(relationship.parent_table)
    parent_column = quote_identifier(relationship.parent_column)

    unmatched = f"""
        FROM {child} AS child_table
        WHERE child_table.{child_column} IS NOT NULL
          AND CAST(child_table.{child_column} AS VARCHAR) NOT IN (
              SELECT CAST({parent_column} AS VARCHAR) FROM {parent}
              WHERE {parent_column} IS NOT NULL
          )
    """

    total = connection.execute(f"SELECT count(*) {unmatched}").fetchone()
    rows = connection.execute(f"SELECT child_table.* {unmatched} LIMIT {int(limit)}").df()
    return rows, int(total[0]) if total else 0


def check_relationship(
    connection: duckdb.DuckDBPyConnection, relationship: Relationship
) -> RelationshipCheck:
    """Runs both pre-checks and returns a verdict with the offending rows attached.

    Requirement 5.2's contract: if anything is found, the relationship is **not**
    established, and the exact rows are shown so the user can fix the source data and
    re-upload.
    """
    try:
        duplicates = duplicate_parent_keys(connection, relationship)
        if not duplicates.empty:
            return RelationshipCheck(
                relationship=relationship,
                ok=False,
                message=(
                    f"'{relationship.parent_column}' repeats in {relationship.parent_table}, so it "
                    "can't identify a row. Remove the duplicate records and upload again."
                ),
                duplicate_parent_keys=duplicates,
            )

        orphans, orphan_total = orphan_rows(connection, relationship)
        distinct_child_values, matched = _overlap(connection, relationship)
        containment = round(matched / distinct_child_values, 4) if distinct_child_values else 0.0

        if orphan_total:
            return RelationshipCheck(
                relationship=relationship,
                ok=False,
                message=(
                    f"{orphan_total:,} row(s) in {relationship.child_table} have a "
                    f"'{relationship.child_column}' that doesn't exist in "
                    f"{relationship.parent_table}. Fix them in the source file and upload again."
                ),
                containment=containment,
                orphan_rows=orphans,
                orphan_count=orphan_total,
            )
    except duckdb.Error as error:
        logger.exception("Could not check relationship %s.", relationship.label)
        return RelationshipCheck(
            relationship=relationship,
            ok=False,
            message=f"This link couldn't be checked ({error}).",
        )

    return RelationshipCheck(
        relationship=relationship,
        ok=True,
        message=f"Every {relationship.child_table}.{relationship.child_column} value matches.",
        containment=containment,
    )


def enforceable(connection: duckdb.DuckDBPyConnection, candidates: list[Relationship]) -> list[Relationship]:
    """The subset of `candidates` clean enough to become a real foreign key.

    A relationship with unmatched or duplicate-parent rows is still useful for the agent
    to join on (see `dictionary.schema_context`) — it just can't be enforced, since
    DuckDB would refuse the `CREATE TABLE` outright. Only relationships that pass both
    pre-checks are ever handed to `enforce()`.
    """
    return [item for item in candidates if check_relationship(connection, item).ok]


def order_tables(tables: list[str], relationships: list[Relationship]) -> list[str]:
    """Sorts tables parents-first, so each can be created after what it references.

    Raises:
        RelationshipError: if the relationships form a cycle, naming the tables involved
            — a cycle can't be built in any order, and saying which tables are in it is
            the only actionable thing to report.
    """
    remaining = list(dict.fromkeys(tables))
    parents_of = {table: set() for table in remaining}
    for relationship in relationships:
        if relationship.child_table in parents_of and relationship.parent_table in remaining:
            if relationship.parent_table != relationship.child_table:
                parents_of[relationship.child_table].add(relationship.parent_table)

    ordered: list[str] = []
    while remaining:
        ready = [table for table in remaining if parents_of[table] <= set(ordered)]
        if not ready:
            raise RelationshipError(
                "These links form a loop, so the tables can't be built in any order: "
                + ", ".join(sorted(remaining))
                + ". Remove one of the links to break the loop."
            )
        # Sorted, so a rebuild is deterministic and its tests aren't order-flaky.
        ordered.extend(sorted(ready))
        remaining = [table for table in remaining if table not in ordered]

    return ordered


def _create_table_sql(
    connection: duckdb.DuckDBPyConnection, table: str, relationships: list[Relationship]
) -> str:
    """Builds the `CREATE TABLE` for one working table, constraints inline.

    Inline is not a style choice: DuckDB cannot add a foreign key with `ALTER TABLE`, so
    this is the only place a constraint can be declared.
    """
    columns = describe_table(connection, base_name(table))
    definitions = [f"{quote_identifier(column)} {sql_type}" for column, sql_type in columns]

    # Parent-side keys need UNIQUE for a foreign key to reference them at all.
    unique_columns = sorted(
        {
            relationship.parent_column
            for relationship in relationships
            if relationship.parent_table == table
        }
    )
    definitions.extend(f"UNIQUE ({quote_identifier(column)})" for column in unique_columns)

    definitions.extend(
        f"FOREIGN KEY ({quote_identifier(relationship.child_column)}) "
        f"REFERENCES {quote_identifier(relationship.parent_table)}"
        f"({quote_identifier(relationship.parent_column)})"
        for relationship in relationships
        if relationship.child_table == table
    )

    return f"CREATE TABLE {quote_identifier(table)} (\n  " + ",\n  ".join(definitions) + "\n)"


def enforce(
    connection: duckdb.DuckDBPyConnection,
    tables: list[str],
    relationships: list[Relationship],
    replay_statements: list[str] | None = None,
) -> list[str]:
    """Rebuilds the working tables with the confirmed relationships as real foreign keys.

    The whole rebuild is one transaction, so a failure part-way leaves the session
    exactly as it was rather than half-built. Children are dropped before parents and
    parents created before children, because DuckDB refuses to drop a table another
    still references.

    `replay_statements` are the calculated-column statements recorded so far. A rebuild
    re-creates the working tables from their base tables, which would otherwise silently
    discard every calculated column the user added — so they are replayed on the way
    out, in the order they were applied. That ordered list is also exactly what
    requirement 7.5 asks Task Builder to persist.

    Returns:
        The table names rebuilt, parents first.

    Raises:
        RelationshipError: if the relationships form a cycle, or DuckDB refuses the
            rebuild — most often because a pre-check was skipped and the data really
            does violate the constraint.
    """
    ordered = order_tables(tables, relationships)

    try:
        connection.execute("BEGIN TRANSACTION")
    except duckdb.Error as error:
        logger.exception("Could not start the rebuild transaction.")
        raise RelationshipError("The tables couldn't be rebuilt.") from error

    try:
        for table in reversed(ordered):
            connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")

        for table in ordered:
            connection.execute(_create_table_sql(connection, table, relationships))
            connection.execute(
                f"INSERT INTO {quote_identifier(table)} "
                f"SELECT * FROM {quote_identifier(base_name(table))}"
            )

        for statement in replay_statements or []:
            connection.execute(statement)

        connection.execute("COMMIT")
    except duckdb.Error as error:
        connection.execute("ROLLBACK")
        logger.exception("Rebuild failed; the session was rolled back.")
        raise RelationshipError(
            f"The tables couldn't be linked, so nothing was changed ({error})."
        ) from error

    logger.info("Rebuilt %d table(s) with %d relationship(s).", len(ordered), len(relationships))
    return ordered


def to_dot(tables: list[str], relationships: list[Relationship]) -> str:
    """Builds Graphviz DOT for the relationship diagram (requirement 5.2).

    Rendered by `st.graphviz_chart`, which accepts a raw DOT string and draws it
    client-side — the `graphviz` Python package is not a dependency.
    """
    lines = [
        "digraph relationships {",
        "  rankdir=LR;",
        "  bgcolor=transparent;",
        '  node [shape=box, style="rounded,filled", fillcolor="#EFF6FF", '
        'color="#2563EB", fontname="Helvetica", fontsize=11];',
        '  edge [color="#64748B", fontname="Helvetica", fontsize=9];',
    ]

    for table in tables:
        lines.append(f'  "{table}" [label="{table}"];')

    for relationship in relationships:
        lines.append(
            f'  "{relationship.child_table}" -> "{relationship.parent_table}" '
            f'[label="{relationship.child_column} → {relationship.parent_column}"];'
        )

    lines.append("}")
    return "\n".join(lines)


def describe_relationships(
    connection: duckdb.DuckDBPyConnection, relationships: list[Relationship]
) -> pd.DataFrame:
    """A one-row-per-link summary table for display."""
    records = []
    for relationship in relationships:
        try:
            child_rows = row_count(connection, relationship.child_table)
        except DataEngineError:
            child_rows = 0
        records.append(
            {
                "From": f"{relationship.child_table}.{relationship.child_column}",
                "To": f"{relationship.parent_table}.{relationship.parent_column}",
                "Rows": child_rows,
            }
        )
    return pd.DataFrame.from_records(records, columns=["From", "To", "Rows"])
