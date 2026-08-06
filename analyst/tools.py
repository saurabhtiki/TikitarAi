"""The agent's DuckDB toolkit, with requirement 5.5's guardrails actually applied.

Requirement 5.5 names `agno.tools.duckdb.DuckDbTools`, and this is that toolkit — but
subclassed rather than used as it ships, for two concrete reasons found by reading the
installed source (Agno 2.8.6, `agno/tools/duckdb.py`):

1. **Its `run_query` applies no guardrails at all.** It hands the string straight to
   DuckDB, so `read_csv('C:/Users/…/secrets.csv')` would read the filesystem and
   `DROP TABLE users` would reach a table this session doesn't own — exactly the two
   escapes requirement 5.5 forbids. It also splits on `;` and silently runs only the
   first fragment, so a rejected second statement would be discarded rather than
   reported. Routing through `engine.duckdb_session.run_query` puts `engine/guards.py`
   back in the path, which is what that module was written ahead of time for.
2. **It returns a flat string, never a dataframe.** Requirement 5.5 step 3 asks for the
   result set "as a dataframe for the output-type logic to handle", and requirement 6.2
   needs the chart, the table and the commentary all built from that one frame. Capturing
   the frame on the way past is the only way to get it — the model's prose rendering of a
   result is not a result.

The toolkit is also cut down to three read-only tools. The eleven it ships with include
`load_local_csv_to_table`, `export_table_to_path` and `load_s3_path_to_table`, all of
which cross the session boundary by design. Column changes do not go through here either:
they run through `engine/columns.py`, which probes first and wraps in a transaction.
"""

import logging

import duckdb
import pandas as pd
from agno.tools.duckdb import DuckDbTools

from engine.duckdb_session import run_query as guarded_run_query
from engine.exceptions import DataEngineError

logger = logging.getLogger(__name__)

# The only tools the agent gets. Everything else in DuckDbTools either touches the
# filesystem, touches S3, or writes — none of which belongs in a session-scoped,
# in-memory analysis.
READ_ONLY_TOOLS = ["show_tables", "describe_table", "run_query"]

# What the model sees of a result. It needs enough rows to reason about and describe the
# answer; it does not need all 50,000, and sending them would crowd out the schema.
MODEL_PREVIEW_ROWS = 30
MODEL_PREVIEW_CHARS = 4000


class SessionDuckDbTools(DuckDbTools):
    """`DuckDbTools` scoped to one session's tables, with every query recorded.

    Attributes:
        executions: every successful query this run, as `(sql, dataframe)` in order. The
            last entry is the one the answer is built from.
        failures: the SQL and error text of every rejected or failed query, kept so a
            question that never succeeded can still explain what went wrong.
    """

    def __init__(self, connection: duckdb.DuckDBPyConnection):
        super().__init__(connection=connection, include_tools=READ_ONLY_TOOLS)
        self.executions: list[tuple[str, pd.DataFrame]] = []
        self.failures: list[tuple[str, str]] = []

    def run_query(self, query: str) -> str:
        """Runs one read-only SQL statement against this session's tables.

        Args:
            query: a single DuckDB SQL statement. Only one statement per call.

        Returns:
            str: the rows, as text, or the reason the query could not run.
        """
        sql = str(query or "").replace("`", "").strip()

        try:
            # `allowed_tables=None` means "no table may be mutated at all" — the tightest
            # setting the guard offers, and the right one for a tool that only reads.
            frame = guarded_run_query(self.connection, sql, allowed_tables=None)
        except DataEngineError as error:
            # Returned rather than raised, deliberately: this is how the agent learns it
            # used a column that doesn't exist and tries again. Raising would end the run
            # on the first typo.
            logger.info("Agent query refused or failed: %s", error)
            self.failures.append((sql, str(error)))
            return f"That query could not run: {error}"
        except duckdb.Error as error:
            logger.info("Agent query failed in DuckDB: %s", error)
            self.failures.append((sql, str(error)))
            return f"That query could not run: {error}"

        self.executions.append((sql, frame))
        logger.info("Agent query returned %d row(s), %d column(s).", len(frame), frame.shape[1])
        return render_for_model(frame)

    @property
    def last_result(self) -> tuple[str, pd.DataFrame] | None:
        """The most recent successful query and its rows, or None if none succeeded."""
        return self.executions[-1] if self.executions else None


def render_for_model(frame: pd.DataFrame) -> str:
    """Renders a result as compact text for the model, capped in both rows and characters.

    Two caps, not one: 30 wide rows can still be enormous, and a result the model can't
    read past is worse than one it knows was truncated.
    """
    if frame.empty:
        return "The query ran and returned no rows."

    head = frame.head(MODEL_PREVIEW_ROWS)
    text = head.to_csv(index=False).strip()
    if len(text) > MODEL_PREVIEW_CHARS:
        text = text[:MODEL_PREVIEW_CHARS].rsplit("\n", 1)[0] + "\n…"

    footer = f"\n({len(frame)} row(s) in total"
    if len(frame) > MODEL_PREVIEW_ROWS:
        footer += f", first {MODEL_PREVIEW_ROWS} shown"
    footer += ")"
    return text + footer
