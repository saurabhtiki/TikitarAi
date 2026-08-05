"""Guardrails for SQL that originates outside this codebase.

Requirements 5.5 scopes agent-written SQL strictly to the current session's own DuckDB
tables: no filesystem access, no dropping or renaming tables outside the session's own
working tables, and nothing persisted to disk unless the user explicitly exports.

This module is written in Stage 5 rather than Stage 6 because the calculated-column
dialog already accepts a free-text SQL expression typed by a user, which is the same
trust boundary the agent will sit behind. Stage 6's `DuckDbTools` agent reuses these
functions unchanged.

The approach is deliberately a **denylist of capabilities plus an allowlist of table
names**, applied to the statement before execution. It is not a SQL parser and does not
pretend to be one — DuckDB is the only thing that truly parses SQL here. What it does
guarantee is that the specific escapes that matter in an in-memory analytics session
(reading files, attaching databases, installing extensions, writing to disk, touching a
table the session doesn't own) are refused before DuckDB ever sees them.
"""

import logging
import re

from engine.exceptions import UnsafeSqlError

logger = logging.getLogger(__name__)

# Capabilities that let SQL leave the session: read or write the filesystem, attach
# another database, or load an extension that could do either.
_FORBIDDEN_FUNCTIONS = (
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "read_json",
    "read_json_auto",
    "read_text",
    "read_blob",
    "read_ndjson",
    "parquet_scan",
    "glob",
    "sniff_csv",
)

_FORBIDDEN_STATEMENTS = (
    "attach",
    "detach",
    "copy",
    "export",
    "import",
    "install",
    "load",
    "pragma",
    "set",
    "reset",
    "call",
)

# Statements that name a table they will change. Everything else is read-only enough
# that the allowlist doesn't apply.
_TABLE_MUTATING = re.compile(
    r"\b(?:drop|alter|truncate|create(?:\s+or\s+replace)?)\s+"
    r"(?:temp\s+|temporary\s+)?(?:table|view)\s+(?:if\s+exists\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

_STATEMENT_START = re.compile(r"^\s*([A-Za-z_]+)")

# A string literal or quoted identifier can legitimately contain the word "attach" or a
# semicolon. Blanking them first stops `WHERE note = 'copy; drop table x'` from being
# refused as if it were two statements.
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_literals(sql: str) -> str:
    """Returns `sql` with comments removed and quoted text blanked out.

    Length is preserved for quoted runs so offsets stay meaningful, and the blanks are
    spaces rather than nothing so `'a';'b'` doesn't collapse into one token.
    """
    without_comments = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql))
    return _QUOTED.sub(lambda match: " " * len(match.group(0)), without_comments)


def split_statements(sql: str) -> list[str]:
    """Splits on semicolons that aren't inside a string literal or comment.

    Trailing semicolons and blank fragments are dropped, so `SELECT 1;` is one
    statement rather than two.
    """
    scrubbed = strip_literals(sql)
    statements: list[str] = []
    start = 0

    for position, character in enumerate(scrubbed):
        if character == ";":
            fragment = sql[start:position].strip()
            if fragment:
                statements.append(fragment)
            start = position + 1

    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def assert_safe_sql(sql: str, allowed_tables: set[str] | None = None) -> str:
    """Checks one statement and returns it unchanged, or raises.

    `allowed_tables` is the session's own table names. Any statement that would drop,
    alter, truncate or replace a table not in that set is refused — this is what keeps
    a stray `DROP TABLE users` from reaching a DuckDB instance that happens to share a
    process with the rest of the app.

    Passing `allowed_tables=None` means "no table may be mutated at all", which is the
    right default for a pure read path.

    Raises:
        UnsafeSqlError: if the statement is empty, is really several statements, uses a
            filesystem/extension capability, or names a table outside `allowed_tables`.
    """
    if not sql or not sql.strip():
        raise UnsafeSqlError("No SQL was provided.")

    statements = split_statements(sql)
    if len(statements) > 1:
        logger.warning("Rejected multi-statement SQL (%d statements).", len(statements))
        raise UnsafeSqlError(
            f"Only one SQL statement can run at a time, but {len(statements)} were provided."
        )

    scrubbed = strip_literals(sql)
    lowered = scrubbed.lower()

    leading = _STATEMENT_START.match(scrubbed)
    if leading and leading.group(1).lower() in _FORBIDDEN_STATEMENTS:
        keyword = leading.group(1).upper()
        logger.warning("Rejected '%s' statement.", keyword)
        raise UnsafeSqlError(f"'{keyword}' statements aren't allowed — this session works only on your uploaded tables.")

    for function_name in _FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{re.escape(function_name)}\s*\(", lowered):
            logger.warning("Rejected SQL using the '%s' function.", function_name)
            raise UnsafeSqlError(
                f"'{function_name}' reads from the filesystem, which isn't allowed here. "
                "Upload the file instead."
            )

    permitted = {name.lower() for name in (allowed_tables or set())}
    for match in _TABLE_MUTATING.finditer(scrubbed):
        target = match.group(1)
        if target.lower() not in permitted:
            logger.warning("Rejected SQL targeting table '%s', which isn't in this session.", target)
            raise UnsafeSqlError(
                f"'{target}' isn't one of this session's tables, so it can't be changed."
            )

    return sql


def assert_safe_expression(expression: str) -> str:
    """Checks a calculated-column expression — a SQL fragment, not a statement.

    Wrapped into a probe `SELECT` before the usual checks run, so a fragment that
    smuggles in `1); DROP TABLE sales; --` is caught as multi-statement exactly as it
    would be on the real path.

    Raises:
        UnsafeSqlError: if the fragment is empty or would escape a single expression.
    """
    if not expression or not expression.strip():
        raise UnsafeSqlError("No formula was provided.")

    assert_safe_sql(f"SELECT {expression}")
    return expression.strip()
