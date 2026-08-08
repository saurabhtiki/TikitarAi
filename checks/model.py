"""What a criteria set is, and every pure operation on one (requirement 6.5).

No Streamlit and no database here: the page holds these objects in session state and
`checks/db.py` stores their JSON, but neither concern reaches this module — which is what
makes the whole shape testable without `AppTest` and without a provider.

Two rules the rest of the package leans on:

**The result contract.** Every criteria's SQL must return `criteria_result` (the value the
rule turns on) and `criteria_met` (the literal `'Yes'` or `'No'`), alongside whatever
columns identify the row. `docs/addtion.md` names the first two; the identifying columns
are here because a failure nobody can attribute cannot be actioned — an email about a bonus
breach has to name the employee. `sql_builder.run_check` enforces all of it.

**A saved run is frozen.** `SavedRun` copies the frame it is given, for the reason
`dashboard/session.py` documents at length: anything that keeps a live reference to a
result quietly empties itself later. Re-running a criteria produces a *new* `SavedRun`
rather than mutating one, so what the report shows is always what the user pressed Save on.

`to_json` deliberately drops that frame. The SQL is what makes a run reproducible; storing
rows would describe data that is no longer loaded — the same trap requirement 6.1 step 5
already calls out for stored conversations.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from analyst.charts import (
    ChartChoices,
    ChartStyle,
    choices_from_dict,
    choices_to_dict,
    style_from_dict,
    style_to_dict,
)
from checks.exceptions import ChecksStorageError

logger = logging.getLogger(__name__)


def new_id() -> str:
    """A short stable id. Used in widget keys, so it must survive reruns unchanged.

    Four lines rather than an import of `dashboard.model.new_id`, deliberately: a criteria
    set is a recipe that will outlive the report it currently feeds — requirement 7.5 stores
    one inside a Task — and a leaf package should not depend on a sibling for a uuid.
    """
    return uuid.uuid4().hex[:12]


# The two columns every criteria's SQL has to produce. Named here rather than spelled out
# at each use, because the prompt, the validator, the row filter and the page all have to
# agree on them exactly.
COLUMN_RESULT = "criteria_result"
COLUMN_MET = "criteria_met"
REQUIRED_COLUMNS = (COLUMN_RESULT, COLUMN_MET)

MET_YES = "Yes"
MET_NO = "No"

# What the result filter offers (addtion.md step 5).
FILTER_ALL = "All"
FILTER_FAILURES = "Failures"
FILTER_PASSES = "Passes"
RESULT_FILTERS = (FILTER_ALL, FILTER_FAILURES, FILTER_PASSES)

ACTION_EMAIL = "email"
ACTION_MEETING = "meeting"
ACTION_TASK = "task"
ACTION_OTHER = "other"
ACTION_KINDS = (ACTION_EMAIL, ACTION_MEETING, ACTION_TASK, ACTION_OTHER)

UNTITLED_CHECK = "Untitled criteria"
UNTITLED_SET = "Untitled criteria set"

# The JSON `to_json` writes carries its own version, so a set saved today can be read back
# after the shape changes rather than failing to load with no explanation.
SCHEMA_VERSION = 1


def _now() -> str:
    """A sortable timestamp for a saved run. Seconds are enough — this labels a run in the
    UI, it does not order concurrent writes."""
    return datetime.now().isoformat(timespec="seconds")


def met_values(frame: pd.DataFrame) -> pd.Series:
    """The `criteria_met` column normalised for comparison.

    Case and surrounding space are stripped because the model writes the literal itself,
    and `'yes'` failing to count as a pass would be a silent wrong answer rather than a
    visible error.
    """
    return frame[COLUMN_MET].astype("string").str.strip().str.casefold()


def filter_rows(frame: pd.DataFrame | None, mode: str) -> pd.DataFrame | None:
    """Applies the All / Failures / Passes filter (addtion.md step 5).

    Returns the frame unchanged for an unknown mode or a frame without `criteria_met`,
    rather than raising: this feeds a display toggle, and a missing column is already
    reported far more clearly by `sql_builder.run_check`.
    """
    if frame is None or frame.empty or COLUMN_MET not in frame.columns:
        return frame
    if mode == FILTER_FAILURES:
        return frame[met_values(frame) == MET_NO.casefold()]
    if mode == FILTER_PASSES:
        return frame[met_values(frame) == MET_YES.casefold()]
    return frame


@dataclass
class SavedRun:
    """One test run, locked in by the user pressing Save (addtion.md step 6).

    Attributes:
        sql: the statement that produced it — the part that is worth persisting, because
            it is what could be run again against a later file.
        frame: a *copy* of the rows. Session-only; never written to disk.
        ran_at: when the run happened, shown so a stale result is recognisable as one.
        pass_count / fail_count: precomputed, so the page and the remarks prompt don't each
            re-derive them from the frame and risk disagreeing.
    """

    sql: str = ""
    frame: pd.DataFrame | None = None
    ran_at: str = field(default_factory=_now)
    pass_count: int = 0
    fail_count: int = 0

    @property
    def row_count(self) -> int:
        return 0 if self.frame is None else len(self.frame)

    def failures(self) -> pd.DataFrame | None:
        return filter_rows(self.frame, FILTER_FAILURES)


def freeze_run(sql: str, frame: pd.DataFrame) -> SavedRun:
    """Turns a test result into a saved run, counting passes and failures.

    The frame is copied here rather than by the caller so there is exactly one place the
    rule can be broken.
    """
    normalised = met_values(frame) if COLUMN_MET in frame.columns else pd.Series(dtype="string")
    return SavedRun(
        sql=sql,
        frame=frame.copy(),
        pass_count=int((normalised == MET_YES.casefold()).sum()),
        fail_count=int((normalised == MET_NO.casefold()).sum()),
    )


@dataclass
class ActionDraft:
    """A follow-up the user configured for a criteria's failures (addtion.md step 7).

    A draft and only a draft: `confirmed` records that the user reviewed and accepted the
    wording, which is what puts it into the report. Nothing in this stage sends anything,
    which is why there is no `sent_at` — adding one before there is a sender would be a
    field that lies.

    Attributes:
        kind: one of `ACTION_KINDS`.
        recipients: `{"to": [...], "cc": [...], "bcc": [...]}` for an email, `{"attendees":
            [...]}` for a meeting, `{"owner": [...]}` for a task. One field rather than
            three optional ones, because every kind wants "who", spelled differently.
        when: the meeting date-time or task due date, as the user typed it. Free text on
            purpose — it is drafted into prose, not scheduled against.
        subject: the email subject, the meeting title, or the task name.
        body: the drafted email body, agenda or description. The user's to edit from the
            moment the model returns it.
    """

    action_id: str = field(default_factory=new_id)
    kind: str = ACTION_EMAIL
    recipients: dict[str, list[str]] = field(default_factory=dict)
    when: str = ""
    subject: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    confirmed: bool = False

    def display_recipients(self) -> str:
        """The "who" of this action as one readable line, for the report and the list."""
        parts = [
            f"{label.title()}: {', '.join(people)}"
            for label, people in self.recipients.items()
            if people
        ]
        return " · ".join(parts)


@dataclass
class Check:
    """One business rule, from the sentence the user wrote to the report item it became.

    Attributes:
        check_id: stable across reruns; forms the widget keys for this check's controls,
            which is why it is generated once and never derived from the name.
        hint_tables / hint_columns: the user's guess at what the rule touches (addtion.md
            step 2). A hint, not a constraint — the model also receives the real schema and
            is told it may correct them.
        sql: the current statement, whether or not it has been saved.
        last_error: why the last attempt failed, or None. Shown beside the SQL and fed back
            into the next generation, which is what makes step 4 a refine rather than a
            retry.
        saved_run: the locked-in run, or None. Its presence is the only thing that puts
            this criteria into the report.
        chart / chart_style: how to draw this criteria's result, or None for no chart. Two
            values rather than a drawn figure, for the same reason `sql` is stored rather
            than the rows it returned: a recipe survives being saved and re-run next month,
            where a picture of last month's numbers doesn't.

    There is deliberately no `pinned_item_id` here, unlike `analyst.session.ChatMessage`.
    Which report item this criteria owns is answered by `PinnedItem.source_id`, looked up
    fresh each time — and two records of the same ownership is one that can go stale.
    """

    check_id: str = field(default_factory=new_id)
    name: str = ""
    criteria_text: str = ""
    hint_tables: list[str] = field(default_factory=list)
    hint_columns: list[str] = field(default_factory=list)
    sql: str | None = None
    last_error: str | None = None
    saved_run: SavedRun | None = None
    remarks: str = ""
    actions: list[ActionDraft] = field(default_factory=list)
    chart: ChartChoices | None = None
    chart_style: ChartStyle | None = None

    def display_name(self) -> str:
        """What to label this criteria. Never empty."""
        return (self.name or "").strip() or UNTITLED_CHECK

    def is_saved(self) -> bool:
        return self.saved_run is not None

    def confirmed_actions(self) -> list[ActionDraft]:
        return [action for action in self.actions if action.confirmed]


@dataclass
class CheckSet:
    """A persona plus the criteria written under it — the unit that gets saved and reloaded.

    `persona` is addtion.md's top-of-tab field: who the LLM is acting as, plus any context
    it should apply. It is passed as the `knowledge_base` argument that
    `analyst/commentary.py` has carried unused since Stage 6 — requirement 7.2's
    domain-rules slot, filled a stage early.

    `summary` is the whole-set overview written under the summary chart — the counterpart of
    `Check.remarks` one level up. Stored with the set rather than recomputed, because it is
    the user's to edit once written: an AI draft they have reworded must not be silently
    replaced the next time the page reruns.
    """

    set_id: int | None = None
    name: str = ""
    persona: str = ""
    summary: str = ""
    checks: list[Check] = field(default_factory=list)

    def display_name(self) -> str:
        return (self.name or "").strip() or UNTITLED_SET

    def saved_checks(self) -> list[Check]:
        """The criteria that have a locked-in run. Nothing else reaches the report."""
        return [check for check in self.checks if check.is_saved()]


# --------------------------------------------------------------------------------------
# Operations on a set
# --------------------------------------------------------------------------------------


def add_check(check_set: CheckSet, name: str = "") -> Check:
    """Appends a criteria and returns it."""
    check = Check(name=(name or "").strip())
    check_set.checks.append(check)
    return check


def find_check(check_set: CheckSet, check_id: str) -> Check | None:
    return next((check for check in check_set.checks if check.check_id == check_id), None)


def remove_check(check_set: CheckSet, check_id: str) -> bool:
    """Drops a criteria. Returns whether it was there.

    The dashboard item it produced is deliberately left alone: it is a finished result the
    user may still want in the report, and the Dashboard has its own Remove button for it.
    """
    check = find_check(check_set, check_id)
    if check is None:
        return False
    check_set.checks.remove(check)
    return True


def find_action(check: Check, action_id: str) -> ActionDraft | None:
    return next((action for action in check.actions if action.action_id == action_id), None)


def remove_action(check: Check, action_id: str) -> bool:
    action = find_action(check, action_id)
    if action is None:
        return False
    check.actions.remove(action)
    return True


# --------------------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------------------


def _action_to_dict(action: ActionDraft) -> dict:
    return {
        "action_id": action.action_id,
        "kind": action.kind,
        "recipients": {label: list(people) for label, people in action.recipients.items()},
        "when": action.when,
        "subject": action.subject,
        "body": action.body,
        "tags": list(action.tags),
        "confirmed": action.confirmed,
    }


def _check_to_dict(check: Check) -> dict:
    # `saved_run` keeps its SQL and its counts but not its rows — see the module docstring.
    run = check.saved_run
    return {
        "check_id": check.check_id,
        "name": check.name,
        "criteria_text": check.criteria_text,
        "hint_tables": list(check.hint_tables),
        "hint_columns": list(check.hint_columns),
        "sql": check.sql,
        "remarks": check.remarks,
        "last_run": None
        if run is None
        else {
            "sql": run.sql,
            "ran_at": run.ran_at,
            "pass_count": run.pass_count,
            "fail_count": run.fail_count,
        },
        "actions": [_action_to_dict(action) for action in check.actions],
        "chart": choices_to_dict(check.chart),
        "chart_style": style_to_dict(check.chart_style),
    }


def to_json(check_set: CheckSet) -> str:
    """Serialises a set for storage.

    Shaped as a recipe rather than a snapshot, so requirement 7.5's `task_json` can embed
    one of these unchanged when Task Builder arrives.
    """
    # No version bump for `summary`: an older reader uses `.get` for every key it knows and
    # simply ignores this one, and a set written before the field existed reads back as an
    # empty summary — which is exactly what it had.
    payload = {
        "version": SCHEMA_VERSION,
        "persona": check_set.persona,
        "summary": check_set.summary,
        "checks": [_check_to_dict(check) for check in check_set.checks],
    }
    return json.dumps(payload, indent=2)


def _action_from_dict(raw: dict) -> ActionDraft:
    return ActionDraft(
        action_id=str(raw.get("action_id") or new_id()),
        kind=str(raw.get("kind") or ACTION_EMAIL),
        recipients={
            str(label): [str(person) for person in people or []]
            for label, people in (raw.get("recipients") or {}).items()
        },
        when=str(raw.get("when") or ""),
        subject=str(raw.get("subject") or ""),
        body=str(raw.get("body") or ""),
        tags=[str(tag) for tag in raw.get("tags") or []],
        confirmed=bool(raw.get("confirmed")),
    )


def _check_from_dict(raw: dict) -> Check:
    # A reloaded criteria has no `saved_run`: its rows were never stored, and rebuilding one
    # with an empty frame would put an item claiming zero failures into the report. The
    # stored SQL comes back so the user can re-test it in one press against today's data.
    #
    # The chart comes back with it, and is why the style is only rebuilt when there is a
    # chart to wear it: a style with nothing to draw would be state with no visible cause.
    chart = choices_from_dict(raw.get("chart"))
    return Check(
        check_id=str(raw.get("check_id") or new_id()),
        name=str(raw.get("name") or ""),
        criteria_text=str(raw.get("criteria_text") or ""),
        hint_tables=[str(name) for name in raw.get("hint_tables") or []],
        hint_columns=[str(name) for name in raw.get("hint_columns") or []],
        sql=raw.get("sql") or None,
        remarks=str(raw.get("remarks") or ""),
        actions=[_action_from_dict(action) for action in raw.get("actions") or []],
        chart=chart,
        chart_style=style_from_dict(raw.get("chart_style")) if chart is not None else None,
    )


def from_json(text: str, *, set_id: int | None = None, name: str = "") -> CheckSet:
    """Rebuilds a set from stored JSON.

    Raises:
        ChecksStorageError: if the text isn't the JSON object this module writes. The set
            in front of the user is never replaced by a partial load — the caller only
            swaps it in once this returns.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("Stored criteria set %s could not be parsed.", set_id)
        raise ChecksStorageError(
            "This saved criteria set couldn't be read — its stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning("Stored criteria set %s was %s, not an object.", set_id, type(payload).__name__)
        raise ChecksStorageError("This saved criteria set couldn't be read — it isn't in the expected format.")

    version = payload.get("version")
    if version is not None and version > SCHEMA_VERSION:
        logger.warning("Criteria set %s was saved by a newer version (%s).", set_id, version)
        raise ChecksStorageError(
            "This criteria set was saved by a newer version of the app and can't be opened here."
        )

    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list):
        raw_checks = []

    return CheckSet(
        set_id=set_id,
        name=name,
        persona=str(payload.get("persona") or ""),
        summary=str(payload.get("summary") or ""),
        checks=[_check_from_dict(raw) for raw in raw_checks if isinstance(raw, dict)],
    )
