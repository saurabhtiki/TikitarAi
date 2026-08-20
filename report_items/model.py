"""What a report item is, and every pure operation on one (requirement 7.3 step 3).

No Streamlit and no database here: `report_items/session.py` holds these objects in session
state and `tasks/model.py` embeds their JSON in a Task, but neither concern reaches this
module — which is what makes the whole shape testable without `AppTest` and without a
provider. Deliberately shaped like `checks/model.py`, because it is the same kind of thing:
a recipe that outlives the session it was written in.

**An ordered list of two kinds of item**, and the ordering is not cosmetic:

- A **report item** (`KIND_REPORT`) asks a question of the data and produces a table, a
  chart and a comment — the thing that ends up in the report.
- A **column step** (`KIND_COLUMN`) adds or updates a column. It produces nothing to report;
  its effect is that every item *below* it sees the changed table.

Which is why `can_remove` refuses to delete a column step with another item under it. The
list is read top to bottom on a re-run (requirement 8.2), so removing a step in the middle
would leave every later item's SQL written against a column that no longer exists — and the
failure would surface next month, in a report, rather than here.

**A saved run is frozen.** `SavedRun` copies the frame it is given, for the reason
`dashboard/session.py` documents at length: anything that keeps a live reference to a result
quietly empties itself later.

`to_json` drops that frame, and drops the figure with it. The SQL is what makes a run
reproducible; storing rows would describe data that is no longer loaded.
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
from report_items.exceptions import ReportItemStorageError

logger = logging.getLogger(__name__)

# The two kinds of thing this list holds. See the module docstring.
KIND_REPORT = "report"
KIND_COLUMN = "column"
KINDS = (KIND_REPORT, KIND_COLUMN)

UNTITLED_ITEM = "Untitled report item"
UNTITLED_COLUMN_STEP = "Untitled column step"

# The JSON `to_json` writes carries its own version, so a list saved today can be read back
# after the shape changes rather than failing to load with no explanation.
SCHEMA_VERSION = 1


def new_id() -> str:
    """A short stable id. Used in widget keys, so it must survive reruns unchanged.

    Its own copy rather than an import of `dashboard.model.new_id` or `checks.model.new_id`,
    on the grounds `checks.model` already gives: a recipe package should not depend on a
    sibling for a uuid.
    """
    return uuid.uuid4().hex[:12]


def _now() -> str:
    """A sortable timestamp for a saved run. Seconds are enough — this labels a run in the
    UI, it does not order concurrent writes."""
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class SavedRun:
    """One run of a report item's SQL, locked in when the user pins it.

    Attributes:
        sql: the statement that produced it — the part worth persisting, because it is what
            could be run again against a later file.
        frame: a *copy* of the rows. Session-only; never written to disk.
        ran_at: when the run happened, shown so a stale result is recognisable as one.
    """

    sql: str = ""
    frame: pd.DataFrame | None = None
    ran_at: str = field(default_factory=_now)

    @property
    def row_count(self) -> int:
        return 0 if self.frame is None else len(self.frame)


def freeze_run(sql: str, frame: pd.DataFrame) -> SavedRun:
    """Turns a result into a saved run. The frame is copied here rather than by the caller,
    so there is exactly one place the rule can be broken."""
    return SavedRun(sql=sql, frame=frame.copy())


@dataclass
class ReportItem:
    """One entry in the ordered list — either a report item or a column step.

    One dataclass rather than two, because the two kinds are *positions in one sequence*
    and every operation on the list (order, find, remove, serialize) treats them alike.
    `kind` says which, and the fields each kind ignores simply stay at their defaults; the
    view is what draws the two differently.

    Attributes:
        item_id: stable across reruns; forms the widget keys for this item's controls,
            which is why it is generated once and never derived from the heading.
        kind: one of `KINDS`.
        heading: what the report calls this item. Both kinds carry one — a column step's is
            the label in the list, since it never reaches a report.
        request: the rule, or the column change, in the user's own words. This is the thing
            that gets sent to the model; everything else is a hint or a result.
        hint_tables / hint_columns: the user's guess at what the item touches. A hint, not a
            constraint — the model also receives the real schema and may correct them.
        sql: the current statement, whether or not it has been pinned. On a column step this
            is the statements actually executed, joined — see `statements`.
        last_error: why the last attempt failed, or None. Shown beside the SQL and fed back
            into the next generation, which is what makes Regenerate a refine rather than a
            retry.
        saved_run: the run that is currently in the report, or None.
        comment: the written note under the output, drafted by `analyst/commentary.py` and
            the user's to edit from then on.
        chart / chart_style: how to draw this item's result, or None for no chart. Two
            values rather than a drawn figure, for the same reason `sql` is stored rather
            than the rows it returned.
        statements: column steps only — the exact `ALTER`/`UPDATE` statements the engine
            executed, in order. This is what requirement 8.2 step 2 replays.
        applied: column steps only. True once the change has been made to the loaded tables.
            A step is applied at most once per session; re-applying an `ALTER TABLE ADD` on
            a column that already exists is an error, not an idempotent no-op.
        summary: column steps only — the engine's own one-line account of what it did
            ("Added `tax` to `salary`"), kept because it is the honest record of the change
            rather than a restatement of what was asked for.
        description: column steps only — the model's one sentence on what the new column
            *means*, which `summary` deliberately does not say. Stored beside it rather than
            folded into it: one is the engine's record of what happened, the other is the
            AI's account of why, and a later stage may want to trust them differently.

    There is deliberately no `pinned_item_id`. Which report item this owns is answered by
    `PinnedItem.source_id` (see `source_id_for`), looked up fresh each time — and two
    records of the same ownership is one that can go stale.
    """

    item_id: str = field(default_factory=new_id)
    kind: str = KIND_REPORT
    heading: str = ""
    request: str = ""
    hint_tables: list[str] = field(default_factory=list)
    hint_columns: list[str] = field(default_factory=list)
    sql: str | None = None
    last_error: str | None = None
    saved_run: SavedRun | None = None
    comment: str = ""
    chart: ChartChoices | None = None
    chart_style: ChartStyle | None = None
    statements: list[str] = field(default_factory=list)
    applied: bool = False
    summary: str = ""
    description: str = ""

    def display_heading(self) -> str:
        """What to label this item. Never empty."""
        default = UNTITLED_COLUMN_STEP if self.kind == KIND_COLUMN else UNTITLED_ITEM
        return (self.heading or "").strip() or default

    def is_column_step(self) -> bool:
        return self.kind == KIND_COLUMN

    def is_saved(self) -> bool:
        """Whether this item currently has a run in the report. Never true of a column step:
        it has nothing to report, which is the whole distinction between the two kinds."""
        return self.kind == KIND_REPORT and self.saved_run is not None


def source_id_for(item: ReportItem) -> str:
    """The `PinnedItem.source_id` this item owns.

    Prefixed rather than the bare id so it can never collide with `checks/`'s — both pin
    into the same report, and `pin_result` is idempotent on exactly this string.
    """
    return f"item:{item.item_id}"


# --------------------------------------------------------------------------------------
# Operations on the list
# --------------------------------------------------------------------------------------


def add_item(items: list[ReportItem], kind: str = KIND_REPORT, heading: str = "") -> ReportItem:
    """Appends an item of the given kind and returns it.

    Appends rather than inserts, always: an item's position is what decides which column
    steps have run before it, so there is no such thing as putting one "somewhere in the
    middle" without changing what it sees.
    """
    if kind not in KINDS:
        logger.warning("Unknown report item kind '%s' — treating it as a report item.", kind)
        kind = KIND_REPORT
    item = ReportItem(kind=kind, heading=(heading or "").strip())
    items.append(item)
    return item


def find_item(items: list[ReportItem], item_id: str) -> ReportItem | None:
    return next((item for item in items if item.item_id == item_id), None)


def can_remove(items: list[ReportItem], item_id: str) -> tuple[bool, str]:
    """Whether an item may be deleted, and why not when it may not.

    A report item may always go: nothing below it depends on it. A **column step may only be
    deleted if it is the last item in the list** — everything under it was written against
    the table as that step left it, so removing it in the middle would silently invalidate
    them all. Returning the reason rather than a bare False is what lets the view disable
    the button *and* say why, instead of offering a control that refuses.
    """
    item = find_item(items, item_id)
    if item is None:
        return False, "That item is no longer in the list."

    if not item.is_column_step():
        return True, ""

    if items[-1].item_id == item_id:
        return True, ""

    return False, (
        "Only the last column step can be removed. Everything below it was written against "
        "the columns it added, so removing it here would break them. Remove the items under "
        "it first."
    )


def remove_item(items: list[ReportItem], item_id: str) -> bool:
    """Drops an item, if `can_remove` allows it. Returns whether it went.

    The report item it produced is deliberately left alone here — unpinning is the caller's,
    since only it has the session's report. `session.remove_item` does both.
    """
    allowed, reason = can_remove(items, item_id)
    if not allowed:
        logger.info("Refused to remove report item %s: %s", item_id, reason)
        return False
    items.remove(find_item(items, item_id))
    return True


def items_before(items: list[ReportItem], item_id: str) -> list[ReportItem]:
    """Everything above this item in the list — the state of the world it was written for."""
    for position, item in enumerate(items):
        if item.item_id == item_id:
            return items[:position]
    return list(items)


def column_steps(items: list[ReportItem]) -> list[ReportItem]:
    return [item for item in items if item.is_column_step()]


def saved_items(items: list[ReportItem]) -> list[ReportItem]:
    """The report items with a run in the report. Nothing else reaches it."""
    return [item for item in items if item.is_saved()]


def pending_column_steps(items: list[ReportItem]) -> list[ReportItem]:
    """Column steps that have been written but not yet applied to the loaded tables.

    Worth naming because an unapplied step is the one state in which the list on screen and
    the data underneath it disagree — every item below is reading a table that doesn't have
    the column yet.
    """
    return [item for item in column_steps(items) if not item.applied]


# --------------------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------------------


def _item_to_dict(item: ReportItem) -> dict:
    """One item as a recipe. Note what is absent: the run's *rows*, and the figure.

    The run keeps its SQL and its timestamp so a reloaded item can say what it last did and
    re-run it in one press; storing the frame would describe data that is no longer loaded.
    """
    run = item.saved_run
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "heading": item.heading,
        "request": item.request,
        "hint_tables": list(item.hint_tables),
        "hint_columns": list(item.hint_columns),
        "sql": item.sql,
        "comment": item.comment,
        "last_run": None if run is None else {"sql": run.sql, "ran_at": run.ran_at},
        "chart": choices_to_dict(item.chart),
        "chart_style": style_to_dict(item.chart_style),
        "statements": list(item.statements),
        "summary": item.summary,
        # No `SCHEMA_VERSION` bump: an older payload has no `description` and reads back as
        # an empty one, which is exactly what it had.
        "description": item.description,
    }


def _item_from_dict(raw: dict) -> ReportItem:
    """The reverse.

    A reloaded item has no `saved_run`: its rows were never stored, and rebuilding one with
    an empty frame would put an item claiming zero rows into the report. The stored SQL comes
    back so the user can re-run it in one press against today's data.

    `applied` is likewise **not** restored. A loaded recipe describes a change that has not
    been made to *this* session's tables — reading it back as applied would leave every item
    below it querying a column nothing had added.

    The chart comes back, and is why the style is only rebuilt when there is a chart to wear
    it: a style with nothing to draw would be state with no visible cause.
    """
    kind = str(raw.get("kind") or KIND_REPORT)
    if kind not in KINDS:
        # Read as a report item with a note rather than dropped: losing an item silently is
        # worse than mis-typing it, the same call `meetings.agenda_from_json` makes.
        logger.warning("Report item %s has unknown kind '%s' — reading it as a report item.",
                       raw.get("item_id"), kind)
        kind = KIND_REPORT

    chart = choices_from_dict(raw.get("chart"))
    return ReportItem(
        item_id=str(raw.get("item_id") or new_id()),
        kind=kind,
        heading=str(raw.get("heading") or ""),
        request=str(raw.get("request") or ""),
        hint_tables=[str(name) for name in raw.get("hint_tables") or []],
        hint_columns=[str(name) for name in raw.get("hint_columns") or []],
        sql=raw.get("sql") or None,
        comment=str(raw.get("comment") or ""),
        chart=chart,
        chart_style=style_from_dict(raw.get("chart_style")) if chart is not None else None,
        statements=[str(statement) for statement in raw.get("statements") or []],
        summary=str(raw.get("summary") or ""),
        description=str(raw.get("description") or ""),
    )


def to_json(items: list[ReportItem]) -> str:
    """Serialises the list for storage.

    Shaped as a recipe rather than a snapshot, so requirement 7.6's `task_json` embeds one
    of these unchanged — the same promise `checks.model.to_json` makes and keeps.
    """
    payload = {
        "version": SCHEMA_VERSION,
        "items": [_item_to_dict(item) for item in items],
    }
    return json.dumps(payload, indent=2)


def from_json(text: str) -> list[ReportItem]:
    """Rebuilds the list from stored JSON.

    Raises:
        ReportItemStorageError: if the text isn't the JSON object this module writes. The
            list in front of the user is never replaced by a partial load — the caller only
            swaps it in once this returns.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("A stored report item list could not be parsed.")
        raise ReportItemStorageError(
            "These saved report items couldn't be read — their stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning("A stored report item list was %s, not an object.", type(payload).__name__)
        raise ReportItemStorageError(
            "These saved report items couldn't be read — they aren't in the expected format."
        )

    version = payload.get("version")
    if version is not None and version > SCHEMA_VERSION:
        logger.warning("A report item list was saved by a newer version (%s).", version)
        raise ReportItemStorageError(
            "These report items were saved by a newer version of the app and can't be opened here."
        )

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []

    return [_item_from_dict(raw) for raw in raw_items if isinstance(raw, dict)]
