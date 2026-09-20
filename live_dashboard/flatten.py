"""Turning a star schema into the one wide table a dashboard embeds (phase 32).

The exported HTML has no database in it, so the joins have to happen *before* the download.
This module is where that happens, and the shape it produces is the answer to the
requirement's "how does filtering work across related tables" question:

**A filter on `Customer - Name` becomes a predicate on a column, not a join.** Because each
master's attributes are pre-joined onto the fact rows, `Customer - Name` and the fact's own
`Amount` end up as two columns of one flat table, and narrowing by customer is then the same
cheap row test as narrowing by amount. The browser never joins anything.

Hierarchies come out of the same walk for free. `join_plan` follows child-to-parent links
outward from the fact table, so `Stock -> SubCategory -> Category` is simply three hops, and
a filter on Category lands on a flattened `Category - Name` column exactly like a one-hop
one.

**Relationships are never guessed here.** They are the links the user already confirmed in
Setup, which `engine.relationships` has checked for orphans and duplicates and enforced as
real foreign keys. A panel that needs a link nobody confirmed is reported by
`model.panel_problems`, not silently joined on a guess - the requirement is explicit that
relationships have one home, and this is not it.

Tables the walk never reaches (a Calendar, a Budget - a different grain entirely) are
embedded whole and separately rather than folded in, because folding them in would repeat
every calendar column on every transaction row.
"""

import logging
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from engine.duckdb_session import quote_identifier, row_count
from engine.exceptions import DataEngineError
from engine.relationships import Relationship
from live_dashboard.exceptions import DashboardDataError

logger = logging.getLogger(__name__)

#: How far the join walk will travel from the fact table. Three hops reaches
#: `Stock -> SubCategory -> Category`, which is the deepest hierarchy the requirement
#: describes; beyond that a "dashboard" is really a data model problem.
MAX_JOIN_DEPTH = 3

#: A master whose rows outnumber the fact's is not a lookup - joining it would multiply the
#: fact rows rather than decorate them, which silently inflates every total on the page.
GRAIN_SAFETY_MARGIN = 1.0

#: Separator between a master's table and its column in the flattened name.
#:
#: **Not a dot, and this is load-bearing.** Vega-Lite reads a dot in a field name as a path
#: into a nested object, so a column literally called `Customer.Name` is looked up as
#: `datum["Customer"]["Name"]` and comes back undefined - every category collapses into one
#: empty group. Vega-Lite does offer a backslash escape for this, but it fixes only the
#: drawing: the click-to-filter expression it generates still reads the escaped spelling,
#: which no row has, so cross-filtering on any master column dies silently. Keeping the
#: separator out of Vega's grammar altogether is the only form where both work.
COLUMN_SEPARATOR = " - "

#: Characters Vega-Lite reads as field-path syntax rather than as part of a name. Any column
#: carrying one is renamed on the way into the dashboard - see `vega_safe_name`.
_VEGA_PATH_CHARACTERS = ".[]"



def vega_safe_name(column: str) -> str:
    """One column name with Vega-Lite's field-path punctuation taken out of it.

    Applied to every column the dashboard embeds, not just the joined ones: a spreadsheet
    column genuinely called `Price.Net` breaks a chart in exactly the same way a badly chosen
    separator would, and the user cannot be expected to know why.

    The replacement is a space, so the rename stays visible and readable in the picker rather
    than looking like a different column.
    """
    cleaned = column
    for character in _VEGA_PATH_CHARACTERS:
        cleaned = cleaned.replace(character, " ")
    cleaned = " ".join(cleaned.split())
    if cleaned != column:
        logger.info("Renamed the column '%s' to '%s' so charts can read it.", column, cleaned)
    return cleaned or column


def _safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """The same frame with every column name safe for a chart, and no name lost to a clash.

    A rename that would collide with a column already present keeps its own suffix rather
    than overwriting it, because silently dropping a column is the one outcome worse than an
    awkward name.
    """
    renamed: list[str] = []
    taken: set[str] = set()
    for column in frame.columns:
        candidate = vega_safe_name(str(column))
        if candidate in taken:
            suffix = 2
            while f"{candidate} {suffix}" in taken:
                suffix += 1
            candidate = f"{candidate} {suffix}"
            logger.info("Two columns flattened to the same name; kept the second as '%s'.",
                        candidate)
        taken.add(candidate)
        renamed.append(candidate)
    frame.columns = renamed
    return frame


@dataclass(frozen=True)
class JoinStep:
    """One hop of the walk: a master table, and how to reach it.

    Attributes:
        alias: the SQL alias this master gets (`m0`, `m1`, ...). Positional rather than
            derived from the table name, because the same table can legitimately appear
            twice on different paths.
        parent_alias: the alias this hop joins *from* - the fact table's alias on hop one,
            another master's on a deeper hop. This is what makes a hierarchy work: hop two
            joins to hop one's alias, not back to the fact.
        prefix: what this master's columns are called in the flattened table, e.g.
            `SubCategory`. The table's own name, so `SubCategory - Name` reads the way the
            user picked it.
    """

    alias: str
    table: str
    column: str
    parent_alias: str
    parent_column: str
    prefix: str
    depth: int


@dataclass
class FlattenPlan:
    """Everything needed to build and explain the flattening query.

    Attributes:
        fact_table: the table whose rows survive one-for-one.
        joins: the masters to decorate it with, in the order they must be joined.
        side_tables: tables the walk never reached, embedded separately.
    """

    fact_table: str
    joins: list[JoinStep] = field(default_factory=list)
    side_tables: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One plain sentence naming what was joined, for the caption under the picker.

        The requirement asks that the app always say what it chose and why, since fact
        detection is a guess the user may need to overrule.
        """
        if not self.joins:
            return f"{self.fact_table} on its own - no confirmed links lead out of it."
        names = ", ".join(dict.fromkeys(step.prefix for step in self.joins))
        return f"{self.fact_table}, with columns from {names} joined on."


def detect_fact_table(relationships: list[Relationship], table_names: list[str],
                      connection: duckdb.DuckDBPyConnection | None = None) -> str:
    """Which table the dashboard should be built around.

    The fact table is the one that *refers to* the most others - a sales line points at a
    customer, a stock item and a date, while a customer master points at nothing. So the
    count of appearances as `Relationship.child_table` is the signal, and it is a good one
    precisely because the relationships were confirmed by hand.

    Ties, and the no-relationships case, fall back to the largest table by row count: with
    nothing else to go on, the biggest table is the one worth putting on a dashboard.

    Returns an empty string when there are no tables at all, rather than raising - the view
    shows its own "load some data first" message in that case.
    """
    if not table_names:
        return ""

    references: dict[str, int] = {name: 0 for name in table_names}
    for relationship in relationships:
        if relationship.child_table in references:
            references[relationship.child_table] += 1

    best = max(references.values())
    candidates = [name for name in table_names if references[name] == best]

    if len(candidates) == 1:
        return candidates[0]

    if connection is None:
        return sorted(candidates)[0]

    def rows(name: str) -> int:
        try:
            return row_count(connection, name)
        except DataEngineError:
            # A table that cannot be counted simply loses the tie-break; it is not a reason
            # to fail the whole screen.
            logger.exception("Could not count '%s' while detecting the fact table.", name)
            return 0

    return max(sorted(candidates), key=rows)


def join_plan(fact_table: str, relationships: list[Relationship],
              table_names: list[str]) -> FlattenPlan:
    """The masters reachable from the fact table, breadth-first, and what is left over.

    Breadth-first rather than depth-first so the shallowest path to a table wins: if a
    Category is reachable both directly and through a SubCategory, the direct link is the one
    that describes the fact row.

    A table is visited once. Without that, a diamond in the relationships (two masters both
    pointing at a third) would join the third twice and duplicate its columns; with it, the
    first path wins and the second is skipped.
    """
    plan = FlattenPlan(fact_table=fact_table)
    visited = {fact_table}
    alias_of = {fact_table: "fact"}

    frontier = [(fact_table, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= MAX_JOIN_DEPTH:
            continue

        for relationship in relationships:
            if relationship.child_table != current:
                continue
            parent = relationship.parent_table
            if parent in visited or parent not in table_names:
                continue

            alias = f"m{len(plan.joins)}"
            visited.add(parent)
            alias_of[parent] = alias
            plan.joins.append(
                JoinStep(
                    alias=alias,
                    table=parent,
                    column=relationship.parent_column,
                    parent_alias=alias_of[current],
                    parent_column=relationship.child_column,
                    prefix=parent,
                    depth=depth + 1,
                )
            )
            frontier.append((parent, depth + 1))

    plan.side_tables = [name for name in table_names if name not in visited]
    return plan


def _master_columns(connection: duckdb.DuckDBPyConnection, step: JoinStep,
                    fact_rows: int, taken: set[str]) -> list[tuple[str, str]]:
    """Which of a master's columns are worth carrying onto every fact row.

    Two exclusions, both about not lying to the user:

    - **The key column itself** is already on the fact row under its own name. Carrying it
      again gives two columns with the same values and no way to tell which to filter on.
    - **A master with more rows than the fact table** is not a lookup at this grain. Joining
      it would fan the fact rows out and quietly double every total, which is the single
      worst thing a dashboard can do, so it is dropped with a warning instead.

    Returns `(source_column, flattened_name)` pairs.
    """
    try:
        master_rows = row_count(connection, step.table)
    except DataEngineError:
        logger.exception("Could not count '%s'; leaving its columns out.", step.table)
        return []

    if fact_rows and master_rows > fact_rows * GRAIN_SAFETY_MARGIN:
        logger.warning(
            "Skipping '%s' (%d rows) - it is larger than the fact table (%d rows), so it is "
            "not a lookup at this grain.",
            step.table, master_rows, fact_rows,
        )
        return []

    try:
        described = connection.execute(
            f"SELECT * FROM {quote_identifier(step.table)} LIMIT 0"
        ).description
    except duckdb.Error:
        logger.exception("Could not describe '%s'; leaving its columns out.", step.table)
        return []

    pairs = []
    for column_info in described or []:
        column = column_info[0]
        if column == step.column:
            continue
        flattened = vega_safe_name(f"{step.prefix}{COLUMN_SEPARATOR}{column}")
        if flattened in taken:
            logger.info("Two masters both offer '%s'; keeping the first.", flattened)
            continue
        taken.add(flattened)
        pairs.append((column, flattened))
    return pairs


def build_flatten_sql(connection: duckdb.DuckDBPyConnection, plan: FlattenPlan) -> str:
    """The one query that produces the embedded table.

    `LEFT JOIN` throughout, deliberately: an inner join would drop a transaction whose
    customer is missing from the master, and a dashboard that silently shows fewer sales than
    the system of record is worse than one that shows a blank customer name.

    Every identifier goes through `quote_identifier`, including the generated aliases, so a
    column called `Order "Ref"` is as safe as any other.
    """
    try:
        fact_rows = row_count(connection, plan.fact_table)
    except DataEngineError as error:
        logger.exception("Could not count the fact table '%s'.", plan.fact_table)
        raise DashboardDataError(
            f"'{plan.fact_table}' couldn't be read, so the dashboard's data can't be built."
        ) from error

    selected = [f"fact.*"]
    taken: set[str] = set()
    joins: list[str] = []

    for step in plan.joins:
        pairs = _master_columns(connection, step, fact_rows, taken)
        if not pairs:
            continue

        joins.append(
            f"LEFT JOIN {quote_identifier(step.table)} AS {quote_identifier(step.alias)} "
            f"ON {quote_identifier(step.parent_alias)}.{quote_identifier(step.parent_column)} "
            f"= {quote_identifier(step.alias)}.{quote_identifier(step.column)}"
        )
        selected.extend(
            f"{quote_identifier(step.alias)}.{quote_identifier(column)} AS "
            f"{quote_identifier(flattened)}"
            for column, flattened in pairs
        )

    statement = (
        "SELECT " + ", ".join(selected) + f" FROM {quote_identifier(plan.fact_table)} AS fact"
    )
    if joins:
        statement += " " + " ".join(joins)
    return statement


def flatten_main_table(connection: duckdb.DuckDBPyConnection, plan: FlattenPlan,
                       limit: int | None = None) -> pd.DataFrame:
    """Runs the flattening query and returns the wide table.

    Raises:
        DashboardDataError: if DuckDB refuses the query. The message names the fact table,
            because that is the thing the user can change.
    """
    statement = build_flatten_sql(connection, plan)
    if limit is not None:
        statement += f" LIMIT {int(limit)}"

    try:
        return _safe_frame(connection.execute(statement).df())
    except duckdb.Error as error:
        logger.exception("The flattening query for '%s' failed.", plan.fact_table)
        raise DashboardDataError(
            f"The dashboard's data couldn't be assembled from '{plan.fact_table}' ({error}). "
            "Check the links this table uses in Setup, under Relationships."
        ) from error


def load_side_table(connection: duckdb.DuckDBPyConnection, table: str) -> pd.DataFrame:
    """One unjoined table, embedded alongside the main one.

    Raises:
        DashboardDataError: if it can't be read.
    """
    try:
        return _safe_frame(connection.execute(f"SELECT * FROM {quote_identifier(table)}").df())
    except duckdb.Error as error:
        logger.exception("Could not read the side table '%s'.", table)
        raise DashboardDataError(f"'{table}' couldn't be read for this dashboard.") from error
