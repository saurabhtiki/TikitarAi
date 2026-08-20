"""Replaying a saved Task against the tables currently loaded (requirement 8.2).

No Streamlit here, and no session state: this takes a `Task`, a DuckDB connection and a
schema context, and hands back a `RunResult` with the report filled in. That is what makes
the whole of requirement 8.2 testable with an in-memory DuckDB and a stubbed provider — and
`runner/session.py` is the thin Streamlit layer that collects those four arguments.

**The stored SQL is the path; the model is the fallback.** Every report item and every
criteria came out of storage carrying the statement that produced it, so **producing the
numbers costs no provider call**: nothing to pay for, nothing to wait on, and no chance of a model
answering a slightly different question than the one the Task's author reviewed and saved.
Only when a stored statement actually fails — a column renamed, a type changed — does
`generate_and_run` get a turn, and the step is recorded as `fallback` so requirement 8.2
step 5's summary can say which answers are the model's rather than the recipe's.

The one part of a run that *is* a provider call by design is redrafting the commentary
(requirement 8.2 step 4), and `rewrite_comments` is how the page lets the user decline it.

**A column step never falls back.** Regenerating a column *definition* from plain language
would silently change the numbers every item below it computes, and the report would look
perfectly fine while being wrong. A failed column step is reported and the run continues, so
the items that don't depend on it still land.

**Order is the content of the list.** A column step changes what every item below it sees,
so the report items are replayed in the order they were written, with each column step's own
statements executed in place rather than all the calculated columns being applied up front.
Applying them up front is *nearly* the same thing and is wrong in exactly one case that
matters: a step that updates a column an earlier item already read.

**Nothing here is a snapshot of anything.** The report the run fills is a deep copy of the
Task's skeleton, so running twice produces the same report rather than a doubled one, and the
Task held in session state is never touched by having been run.
"""

import copy
import logging
from collections.abc import Callable
from pathlib import Path

import duckdb
import pandas as pd

from analyst import charts, commentary
from checks import remarks as checks_remarks
from checks import sql_builder as checks_sql
from checks import summary as checks_summary
from checks.exceptions import CheckSqlError
from checks.model import (
    SUMMARY_SOURCE_ID,
    Check,
    filter_rows,
    freeze_run,
    mode_from_heading,
    project_columns,
    source_id_for as check_source_id_for,
)
from dashboard.model import PinnedItem, Report, find_item_by_source
from engine.duckdb_session import execute
from engine.exceptions import DataEngineError
from report_items import sql_builder as items_sql
from report_items.exceptions import ReportItemSqlError
from report_items.model import ReportItem, source_id_for
from runner.model import (
    KIND_CHECK,
    KIND_COLUMN,
    KIND_ITEM,
    KIND_SUMMARY,
    STATUS_FAILED,
    STATUS_FALLBACK,
    STATUS_OK,
    STATUS_SKIPPED,
    RunResult,
    StepResult,
    now,
)
from tasks.model import Task

logger = logging.getLogger(__name__)

# What a placed item says when its own step failed. Left **in** the report rather than
# removed: a designed report with a point silently missing is worse than one that admits the
# point couldn't be produced, and the run summary names it either way.
MISSING_NOTE = "Not produced in this run — {reason}"

StageCallback = Callable[[str], None]


def _announce(on_stage: StageCallback | None, message: str) -> None:
    """Tells the caller what is happening now, if it asked to be told.

    Requirement 8.2 step 7: a run against a real month's files takes minutes, and a screen
    that says nothing for that long reads as one that has hung.
    """
    if on_stage is not None:
        on_stage(message)


# --------------------------------------------------------------------------------------
# Calculated columns
# --------------------------------------------------------------------------------------


def unowned_statements(task: Task) -> list[str]:
    """The recorded calculated columns that belong to no column step, in their stored order.

    Almost always empty. `report_items_view` records a column step's statements on the item
    *and* appends them to the engine's ordered list, so the two normally describe the same
    changes — but the engine's list is session-wide, and a Task can be saved in a session
    where a column arrived some other way. Those have no position among the report items, so
    they are applied before the list rather than dropped: a statement that was part of the
    recorded setup and is silently not replayed is a column every later item is missing.
    """
    owned = {statement for item in task.report_items for statement in item.statements}
    return [statement for statement in task.calculated_columns if statement not in owned]


def _apply_statements(
    connection: duckdb.DuckDBPyConnection, statements: list[str], allowed: set[str]
) -> None:
    """Runs an ordered group of `ALTER`/`UPDATE` statements, stopping at the first failure.

    Stopping matters: the statements in one group are one change ("add the column, then fill
    it"), so carrying on past a failure would leave a column that exists and holds nothing,
    which reads to every item below as a legitimate answer of all-nulls.

    Raises:
        DataEngineError: carrying DuckDB's own message, which names the real problem far
            better than a paraphrase would.
    """
    for statement in statements:
        execute(connection, statement, allowed_tables=allowed)


def _replay_column_step(
    connection: duckdb.DuckDBPyConnection, item: ReportItem, allowed: set[str], result: RunResult
) -> None:
    """One column step, applied to the loaded tables. See the module docstring on why this
    one kind of step has no fallback."""
    if not item.statements:
        result.record(
            StepResult(
                KIND_COLUMN,
                item.display_heading(),
                STATUS_SKIPPED,
                "This column step has no recorded statements, so there was nothing to apply.",
            )
        )
        return

    try:
        _apply_statements(connection, item.statements, allowed)
    except DataEngineError as error:
        logger.exception("Column step '%s' failed during a task run.", item.display_heading())
        result.record(
            StepResult(
                KIND_COLUMN,
                item.display_heading(),
                STATUS_FAILED,
                f"The recorded change couldn't be applied ({error}). Anything below it that "
                "uses the column it adds will fail too.",
            )
        )
        return

    result.record(
        StepResult(
            KIND_COLUMN,
            item.display_heading(),
            STATUS_OK,
            item.summary or f"{len(item.statements)} statement(s) applied.",
        )
    )


# --------------------------------------------------------------------------------------
# Report items
# --------------------------------------------------------------------------------------


def _replay_report_item(
    connection: duckdb.DuckDBPyConnection,
    item: ReportItem,
    *,
    profile: dict | None,
    persona: str,
    schema_context: str,
    key_path: Path | str | None,
    result: RunResult,
) -> tuple[pd.DataFrame | None, StepResult]:
    """One report item: its stored SQL, then the model, then a recorded failure.

    Returns the rows to put in the report — None when there are none — and the step that was
    recorded for it. The step is handed back rather than looked up afterwards because the
    placing code adds *notes* to it (a chart that wouldn't draw, a comment that wasn't
    rewritten), and reaching back for "the last step recorded" to do that is a coupling that
    breaks silently the first time anything is recorded in between.
    """
    label = item.display_heading()

    if not (item.sql or "").strip():
        return None, result.record(
            StepResult(
                KIND_ITEM,
                label,
                STATUS_SKIPPED,
                "This item was saved without any SQL, so there was nothing to run.",
            )
        )

    try:
        frame = items_sql.run_item(connection, item.sql)
    except ReportItemSqlError as error:
        first_failure = str(error)
    else:
        return frame, result.record(StepResult(KIND_ITEM, label, STATUS_OK, f"{len(frame):,} row(s)."))

    logger.info("Report item '%s' failed on its saved SQL: %s", label, first_failure)

    if profile is None:
        return None, result.record(
            StepResult(
                KIND_ITEM,
                label,
                STATUS_FAILED,
                f"The saved SQL didn't run ({first_failure}) and there is no AI connection "
                "selected to rewrite it.",
            )
        )

    # The failure is put on the item before regenerating, which is what makes this a refine
    # rather than a retry: `generate_and_run` sends the previous statement *and* what was
    # wrong with it, so the model changes the least it can rather than answering the request
    # from scratch with different column names — in a report, a heading that quietly changed
    # between one month and the next.
    item.last_error = first_failure
    try:
        rewritten, frame = items_sql.generate_and_run(
            profile, persona, item, schema_context, connection, key_path=key_path
        )
    except ReportItemSqlError as error:
        return None, result.record(
            StepResult(
                KIND_ITEM,
                label,
                STATUS_FAILED,
                f"The saved SQL didn't run ({first_failure}), and the rewritten one didn't "
                f"either ({error}).",
            )
        )

    item.sql = rewritten
    return frame, result.record(
        StepResult(
            KIND_ITEM,
            label,
            STATUS_FALLBACK,
            f"The saved SQL didn't run ({first_failure}), so the AI rewrote it. "
            f"{len(frame):,} row(s) — worth checking the statement before this report goes out.",
        )
    )


# --------------------------------------------------------------------------------------
# Criteria
# --------------------------------------------------------------------------------------


def _replay_check(
    connection: duckdb.DuckDBPyConnection,
    check: Check,
    *,
    profile: dict | None,
    persona: str,
    schema_context: str,
    key_path: Path | str | None,
    result: RunResult,
) -> tuple[pd.DataFrame | None, StepResult]:
    """One criteria, on the same stored-SQL-then-fallback path a report item takes.

    Returns the **whole** result, every row and every column, and the step recorded for it.
    Narrowing it to what the report shows happens at the placing, because the counts, the
    remarks and the frozen run all read the full frame — the rule
    `checks_view._save_to_report` already follows.

    The run is frozen onto the check here rather than at the placing, so a criteria that ran
    but was never pinned still counts towards the overview at the foot of the report. A rule
    absent from that comparison because nobody placed its table is a rule the reader has no
    idea was even tested.
    """
    label = check.display_name()

    if not (check.sql or "").strip():
        return None, result.record(
            StepResult(
                KIND_CHECK,
                label,
                STATUS_SKIPPED,
                "This criteria was saved without any SQL, so there was nothing to run.",
            )
        )

    try:
        frame = checks_sql.run_check(connection, check.sql)
    except CheckSqlError as error:
        first_failure = str(error)
    else:
        check.saved_run = freeze_run(check.sql or "", frame)
        return frame, result.record(StepResult(KIND_CHECK, label, STATUS_OK, _breach_line(frame)))

    logger.info("Criteria '%s' failed on its saved SQL: %s", label, first_failure)

    if profile is None:
        return None, result.record(
            StepResult(
                KIND_CHECK,
                label,
                STATUS_FAILED,
                f"The saved SQL didn't run ({first_failure}) and there is no AI connection "
                "selected to rewrite it.",
            )
        )

    check.last_error = first_failure
    try:
        rewritten, frame, _identity = checks_sql.generate_and_run(
            profile, persona, check, schema_context, connection, key_path=key_path
        )
    except CheckSqlError as error:
        return None, result.record(
            StepResult(
                KIND_CHECK,
                label,
                STATUS_FAILED,
                f"The saved SQL didn't run ({first_failure}), and the rewritten one didn't "
                f"either ({error}).",
            )
        )

    check.sql = rewritten
    check.saved_run = freeze_run(rewritten, frame)
    return frame, result.record(
        StepResult(
            KIND_CHECK,
            label,
            STATUS_FALLBACK,
            f"The saved SQL didn't run ({first_failure}), so the AI rewrote it. "
            f"{_breach_line(frame)} Worth checking the statement before this report goes out.",
        )
    )


def _breach_line(frame: pd.DataFrame) -> str:
    """The pass/fail headline for a criteria's step, worded as the Checks view words it."""
    run = freeze_run("", frame)
    return f"{run.fail_count:,} of {len(frame):,} record(s) breached this rule."


# --------------------------------------------------------------------------------------
# Filling the report
# --------------------------------------------------------------------------------------


def _place(report: Report, source_id: str) -> PinnedItem | None:
    """The skeleton's slot for this producer, or None when it was never placed.

    An unplaced result is dropped rather than appended to the pool: the exports walk the
    section tree only, so a pool entry would be work done to produce something nobody sees,
    and the run summary already records that the step ran.
    """
    return find_item_by_source(report, source_id)


def _write_comment(
    slot: PinnedItem,
    question: str,
    frame: pd.DataFrame,
    sql: str | None,
    *,
    profile: dict | None,
    persona: str,
    rewrite: bool,
    key_path: Path | str | None,
    step: StepResult,
) -> None:
    """Puts the note under an item — rewritten for this month's numbers where it can be.

    Requirement 8.2 step 4 asks for the Task's persona to be applied "wherever commentary is
    generated", and the recorded comment describes *last* month's rows: a report that reprints
    "headcount fell by four" over a month it rose is worse than one with no comment at all.

    So the draft is rewritten, and the recorded wording is what it falls back to — when there
    is no provider, when the call fails, or when the user asked for the saved wording to be
    kept. A failure here costs the fresh wording, never the item.
    """
    if not rewrite or profile is None:
        return

    written, warnings = commentary.write_commentary(
        profile, question, frame, sql, knowledge_base=persona, key_path=key_path
    )
    step.notes.extend(warnings)
    if written:
        slot.comment = written


def _draw_chart(spec_owner, frame: pd.DataFrame, step: StepResult):
    """This item's chart over this month's rows, or None when it has no chart or won't draw.

    Drawn from the rows being placed rather than from anything stored, for the reason both
    views that pin already give: the report gets a chart of exactly what the report is
    getting. A chart that fails to draw costs the chart, never the item — the table and the
    comment are the point, and the picture is the part that can be missing.
    """
    if spec_owner.chart is None:
        return None
    figure, warnings = charts.render_chart(frame, spec_owner.chart, spec_owner.chart_style)
    if figure is None:
        step.notes.append("This item's chart couldn't be drawn from this month's result: " + " ".join(warnings))
    return figure


def _mark_missing(slot: PinnedItem, reason: str) -> None:
    """Leaves a placed item in the report saying it couldn't be produced. See `MISSING_NOTE`."""
    slot.frame = None
    slot.figure = None
    slot.png = None
    slot.comment = MISSING_NOTE.format(reason=reason)


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------


def run_recipe(
    connection: duckdb.DuckDBPyConnection,
    task: Task,
    *,
    loaded_tables: list[str],
    schema_context: str,
    profile: dict | None = None,
    rewrite_comments: bool = True,
    on_stage: StageCallback | None = None,
    key_path: Path | str | None = None,
) -> RunResult:
    """Requirement 8.2 steps 2–4: replay the recipe and fill the report skeleton.

    Step 1 — putting the recorded links and column meanings back — is **not** here, because
    it is the one part that has to write session state. `runner.session.execute_run` does it
    first and then calls this.

    Args:
        connection: the session's DuckDB, with this month's tables already loaded.
        task: the recipe. Its report items and criteria are mutated in place when a fallback
            rewrites their SQL, which is why the caller hands over a copy.
        loaded_tables: what may be written to, for the guard on the column statements.
        schema_context: the schema block a fallback's prompt receives.
        profile: the LLM profile, or None. None is a legitimate way to run: every stored
            statement works or its step is recorded as failed, and nothing is asked of a
            model that isn't there.
        rewrite_comments: whether to redraft the commentary and remarks for this month's
            numbers. Off keeps the recorded wording, which is the right answer for a report
            whose comments the author has hand-written.
        on_stage: called with a sentence each time the run moves on, for the progress display.

    Returns:
        A `RunResult` whose `report` is ready to preview and download. Never raises for a
        step that failed — that is what `StepResult` is for.
    """
    result = RunResult(report=copy.deepcopy(task.report), started_at=now())
    allowed = set(loaded_tables)
    persona = task.persona

    leading = unowned_statements(task)
    if leading:
        _announce(on_stage, "Applying the recorded calculated columns…")
        try:
            _apply_statements(connection, leading, allowed)
            result.record(
                StepResult(KIND_COLUMN, "Recorded calculated columns", STATUS_OK, f"{len(leading)} statement(s) applied.")
            )
        except DataEngineError as error:
            logger.exception("The task's leading calculated columns failed to replay.")
            result.record(
                StepResult(
                    KIND_COLUMN,
                    "Recorded calculated columns",
                    STATUS_FAILED,
                    f"They couldn't be applied ({error}).",
                )
            )

    total = len(task.report_items)
    for position, item in enumerate(task.report_items, start=1):
        _announce(on_stage, f"Report item {position} of {total} — {item.display_heading()}")

        if item.is_column_step():
            _replay_column_step(connection, item, allowed, result)
            continue

        frame, step = _replay_report_item(
            connection,
            item,
            profile=profile,
            persona=persona,
            schema_context=schema_context,
            key_path=key_path,
            result=result,
        )
        _place_report_item(
            result,
            item,
            frame,
            step,
            profile=profile,
            persona=persona,
            rewrite_comments=rewrite_comments,
            key_path=key_path,
        )

    _replay_checks(
        connection,
        task,
        result,
        profile=profile,
        schema_context=schema_context,
        rewrite_comments=rewrite_comments,
        on_stage=on_stage,
        key_path=key_path,
    )

    result.finished_at = now()
    logger.info(
        "Task run finished: %s",
        ", ".join(f"{status}={count}" for status, count in result.counts().items()),
    )
    return result


def _place_report_item(
    result: RunResult,
    item: ReportItem,
    frame: pd.DataFrame | None,
    step: StepResult,
    *,
    profile: dict | None,
    persona: str,
    rewrite_comments: bool,
    key_path: Path | str | None,
) -> None:
    """Writes one report item's rows into the slot the skeleton holds for it."""
    slot = _place(result.report, source_id_for(item))
    if slot is None:
        return

    if frame is None:
        _mark_missing(slot, step.detail or "the item didn't run.")
        return

    slot.sql = item.sql
    slot.frame = frame.copy()
    slot.png = None
    slot.figure = _draw_chart(item, frame, step)
    _write_comment(
        slot,
        item.request or item.display_heading(),
        frame,
        item.sql,
        profile=profile,
        persona=persona,
        rewrite=rewrite_comments,
        key_path=key_path,
        step=step,
    )


def _replay_checks(
    connection: duckdb.DuckDBPyConnection,
    task: Task,
    result: RunResult,
    *,
    profile: dict | None,
    schema_context: str,
    rewrite_comments: bool,
    on_stage: StageCallback | None,
    key_path: Path | str | None,
) -> None:
    """Every criteria, then the overview that compares them (requirement 8.2 step 3).

    The persona used is the **Task's**, written onto the set as Task Builder does, so a run
    speaks in the voice the Task was recorded with rather than whatever the embedded criteria
    set happened to be saved under.
    """
    check_set = task.checks
    check_set.persona = task.persona

    total = len(check_set.checks)
    for position, check in enumerate(check_set.checks, start=1):
        _announce(on_stage, f"Criteria {position} of {total} — {check.display_name()}")

        frame, step = _replay_check(
            connection,
            check,
            profile=profile,
            persona=task.persona,
            schema_context=schema_context,
            key_path=key_path,
            result=result,
        )
        _place_check(
            result,
            check,
            frame,
            step,
            profile=profile,
            persona=task.persona,
            rewrite_comments=rewrite_comments,
            key_path=key_path,
        )

    _place_summary(result, check_set, on_stage=on_stage)


def _place_check(
    result: RunResult,
    check: Check,
    frame: pd.DataFrame | None,
    step: StepResult,
    *,
    profile: dict | None,
    persona: str,
    rewrite_comments: bool,
    key_path: Path | str | None,
) -> None:
    """Writes one criteria's rows into its slot, narrowed exactly as the Checks view narrows them.

    Two narrowings, both recovered rather than re-decided: the **columns** from
    `Check.display_columns`, and the **filter** from the heading the item was pinned under —
    `mode_from_heading`, because a criteria never stored which of All / Failures / Passes was
    on screen when it was saved, and the heading is the record of it that every Task already
    written carries. Pinning every row into an item headed "breaches only" would put the
    passing records into a report that says it holds the failures.

    The run was already frozen onto the check by `_replay_check`, from the **whole** frame,
    because that is what the remarks read.
    """
    slot = _place(result.report, check_source_id_for(check.check_id))
    if slot is None:
        return

    if frame is None:
        _mark_missing(slot, step.detail or "the criteria didn't run.")
        return

    shown = filter_rows(project_columns(check, frame), mode_from_heading(check, slot.heading))

    slot.sql = check.sql
    slot.frame = None if shown is None else shown.copy()
    slot.png = None
    slot.figure = None if shown is None or shown.empty else _draw_chart(check, shown, step)

    if rewrite_comments and profile is not None:
        written, warnings = checks_remarks.write_remarks(profile, persona, check, key_path=key_path)
        step.notes.extend(warnings)
        if written:
            check.remarks = written
            slot.comment = written


def _place_summary(result: RunResult, check_set, *, on_stage: StageCallback | None) -> None:
    """Rebuilds the criteria-set overview, if the report holds one.

    Only if: the overview is pinned by its own button in the Checks view, and a Task whose
    author never pressed it has no slot for one. Building the chart anyway would be work with
    nowhere to put it.

    The counts come from the criteria that actually produced a run **this** time, so a rule
    that failed is absent from the comparison rather than shown as zero breaches — which
    would read as a clean bill of health for the one rule nobody checked.
    """
    slot = _place(result.report, SUMMARY_SOURCE_ID)
    if slot is None:
        return

    _announce(on_stage, "Building the criteria overview…")
    step = result.record(StepResult(KIND_SUMMARY, "Criteria overview", STATUS_OK))

    saved = check_set.saved_checks()
    if not saved:
        _mark_missing(slot, "no criteria produced a result to compare.")
        step.status = STATUS_FAILED
        step.detail = "No criteria produced a result, so there was nothing to compare."
        return

    frame = checks_summary.counts_frame(saved)
    slot.frame = frame.copy()
    slot.png = None
    slot.figure = checks_summary.combined_chart(frame)
    step.detail = f"{len(saved)} criteria compared."
