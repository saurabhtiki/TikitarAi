"""What a Task is, and how one is written down (requirement 7.5, 7.6).

A Task is the whole pipeline recorded once: the schema it expects, the links, the column
meanings, the calculated columns, the report items, the criteria, and the shape of the report
they fill. Requirement 8 then runs it against next month's files.

**Nothing here is described twice.** Every part of a Task already had a recipe format, written
by the stage that built it, and this module assembles them rather than restating them:

| Part | Comes from |
|---|---|
| expected schema, links, column meanings | `chat_types.model` — the same signature §7.4 asks for |
| calculated columns | `engine.session.get_statements()`, an ordered list of statements |
| report items and column steps | `report_items.model` |
| criteria | `checks.model` — embedded unchanged, as its docstring promised |
| report structure | `dashboard.skeleton` |

The sub-formats are nested as **objects, not as escaped strings**. `json.loads` on their own
output is a round trip through the one serializer that owns each shape, so there is still
exactly one place each format is defined — and the stored Task stays readable, which matters
for a recipe someone may have to debug a year from now.

**A recipe, never a snapshot**, at every level: no rows, no figures, no session state. Each
sub-serializer already enforces that for its own part; this module adds only the persona and
the version, neither of which could carry data.

No Streamlit and no database here — `tasks/session.py` holds one in session state and
`tasks/db.py` stores its JSON.
"""

import json
import logging
from dataclasses import dataclass, field

from chat_types import model as chat_types_model
from chat_types.exceptions import ChatTypeStorageError
from chat_types.model import ChatType
from checks import model as checks_model
from checks.exceptions import ChecksStorageError
from checks.model import CheckSet
from dashboard import skeleton
from dashboard.exceptions import ReportSkeletonError
from dashboard.model import Report
from engine.dictionary import ColumnEntry
from engine.relationships import Relationship
from report_items import model as report_items_model
from report_items.exceptions import ReportItemStorageError
from report_items.model import ReportItem
from tasks.exceptions import TaskStorageError

logger = logging.getLogger(__name__)

UNTITLED_TASK = "Untitled task"

# Bumped only when a stored Task would be read *wrongly* by the current code. Each embedded
# format carries its own version and moves independently of this one, which is the point of
# nesting them rather than flattening them into one payload.
SCHEMA_VERSION = 1


@dataclass
class Task:
    """One saved pipeline.

    Attributes:
        task_id: None until it has been saved, exactly as `CheckSet.set_id` is.
        name: what it is called. Unique per account — see `tasks/db.py`.
        description: the author's note on what this Task is for and when to run it. Shown in
            the picker, so a Task found six months later can be identified without opening it.
        persona: requirement 7.2's persona and context, set once and used by the report items
            and the criteria alike. One field, because one Task has one voice.
        schema: the expected tables, their column types, the links and the column meanings —
            a `ChatType`, which is exactly that and already knows how to match an upload
            against itself (`chat_types.matching`).
        calculated_columns: the `ALTER`/`UPDATE` statements the session executed, in order.
            Requirement 8.2 replays these; the order is the whole content of the field.
        report_items: the ordered list of report items and column steps.
        checks: the criteria set.
        report: the report's structure with no data in it.
    """

    task_id: int | None = None
    name: str = ""
    description: str = ""
    persona: str = ""
    schema: ChatType = field(default_factory=ChatType)
    calculated_columns: list[str] = field(default_factory=list)
    report_items: list[ReportItem] = field(default_factory=list)
    checks: CheckSet = field(default_factory=CheckSet)
    report: Report = field(default_factory=Report)

    def display_name(self) -> str:
        return (self.name or "").strip() or UNTITLED_TASK

    def summary_line(self) -> str:
        """One line for the picker: what this Task contains.

        Counted rather than described, because the counts are what tell two similarly named
        Tasks apart at a glance.
        """
        return (
            f"{len(self.schema.tables)} table(s) · "
            f"{len(self.report_items)} report item(s) · "
            f"{len(self.checks.checks)} criteria"
        )


def capture(
    name: str,
    *,
    description: str = "",
    persona: str = "",
    semantic_types_by_table: dict[str, dict[str, str]],
    relationships: list[Relationship],
    dictionary: list[ColumnEntry],
    statements: list[str],
    report_items: list[ReportItem],
    checks: CheckSet,
    report: Report,
    task_id: int | None = None,
) -> Task:
    """Builds a Task from the session state currently on screen.

    Takes plain lists and dicts rather than `engine.session`'s objects, on the same grounds
    `chat_types.model.capture` gives: this module never imports the one part of `engine/` that
    pulls in Streamlit, which is what keeps it testable without `AppTest`.

    The lists are **copied**, so a Task held in session state cannot be quietly rewritten by
    the user continuing to edit the items it was captured from. Whether that matters is not
    obvious until the day it does: `capture` is called on Save, and the rerun that follows it
    hands the same objects straight back to the view.
    """
    return Task(
        task_id=task_id,
        name=(name or "").strip(),
        description=(description or "").strip(),
        persona=persona or "",
        schema=chat_types_model.capture(
            name, semantic_types_by_table, relationships, dictionary
        ),
        calculated_columns=list(statements),
        report_items=list(report_items),
        checks=checks,
        report=report,
    )


# --------------------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------------------


def to_json(task: Task) -> str:
    """Serialises a Task for storage. The name, description and id live in their own columns.

    Raises:
        TaskStorageError: if any embedded part refuses to serialise. Reported as one failure
            because from the page's point of view it is one: the recipe didn't survive.
    """
    try:
        payload = {
            "version": SCHEMA_VERSION,
            "persona": task.persona,
            "schema": json.loads(chat_types_model.to_json(task.schema)),
            "calculated_columns": list(task.calculated_columns),
            "report_items": json.loads(report_items_model.to_json(task.report_items)),
            "checks": json.loads(checks_model.to_json(task.checks)),
            "report": skeleton.to_dict(task.report),
        }
        return json.dumps(payload, indent=2)
    except (ChecksStorageError, ChatTypeStorageError, ReportItemStorageError, ReportSkeletonError) as error:
        logger.exception("Could not serialise task '%s'.", task.display_name())
        raise TaskStorageError(f"This task couldn't be saved ({error}).") from error
    except (TypeError, ValueError) as error:
        logger.exception("Could not serialise task '%s'.", task.display_name())
        raise TaskStorageError(f"This task couldn't be saved ({error}).") from error


def recipe_fingerprint(task: Task) -> str:
    """A stable string for "is this Task the same as the one that was saved?".

    Deliberately **not** `to_json`. A Task's schema and calculated columns are read from the
    live session, and opening a saved Task restores the recipe without touching the session
    (see `tasks.session.load_task`) — so comparing those parts would report every freshly
    opened Task as edited before the user had touched anything.

    What is compared is exactly what opening a Task puts back: its name, description and
    persona, its report items, its criteria and its report's structure.

    Raises:
        TaskStorageError: if a part refuses to serialise — the caller treats that as "can't
            tell", which is safer than claiming there is nothing to lose.
    """
    try:
        payload = {
            "name": task.display_name(),
            "description": (task.description or "").strip(),
            "persona": task.persona or "",
            "report_items": json.loads(report_items_model.to_json(task.report_items)),
            "checks": json.loads(checks_model.to_json(task.checks)),
            "report": skeleton.to_dict(task.report),
        }
        return json.dumps(payload, sort_keys=True)
    except (ChecksStorageError, ReportItemStorageError, ReportSkeletonError, TypeError, ValueError) as error:
        logger.exception("Could not fingerprint task '%s'.", task.display_name())
        raise TaskStorageError(f"This task's contents couldn't be compared ({error}).") from error


def from_json(text: str, *, task_id: int | None = None, name: str = "", description: str = "") -> Task:
    """Rebuilds a Task from stored JSON.

    Each part is handed back to the module that wrote it, so a format that grows a field
    tolerates its own old payloads without this module knowing anything about it.

    Raises:
        TaskStorageError: if the text isn't the JSON object this module writes, or any
            embedded part refuses to load. The Task in front of the user is never replaced by
            a partial load — the caller only swaps it in once this returns.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("Stored task %s could not be parsed.", task_id)
        raise TaskStorageError(
            "This saved task couldn't be read — its stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning("Stored task %s was %s, not an object.", task_id, type(payload).__name__)
        raise TaskStorageError("This saved task couldn't be read — it isn't in the expected format.")

    version = payload.get("version")
    if version is not None and version > SCHEMA_VERSION:
        logger.warning("Task %s was saved by a newer version (%s).", task_id, version)
        raise TaskStorageError(
            "This task was saved by a newer version of the app and can't be opened here."
        )

    try:
        schema = chat_types_model.from_json(json.dumps(payload.get("schema") or {}), name=name)
        items = report_items_model.from_json(json.dumps(payload.get("report_items") or {}))
        checks = checks_model.from_json(json.dumps(payload.get("checks") or {}), name=name)
        report = skeleton.from_dict(payload.get("report") or {})
    except (ChecksStorageError, ChatTypeStorageError, ReportItemStorageError, ReportSkeletonError) as error:
        logger.exception("Stored task %s has a part that could not be read.", task_id)
        raise TaskStorageError(f"This saved task couldn't be read ({error}).") from error

    statements = [str(statement) for statement in payload.get("calculated_columns") or []]

    return Task(
        task_id=task_id,
        name=name,
        description=description,
        persona=str(payload.get("persona") or ""),
        schema=schema,
        calculated_columns=statements,
        report_items=items,
        checks=checks,
        report=report,
    )
