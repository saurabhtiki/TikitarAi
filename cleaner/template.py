"""What a cleaning template is, and how one is written down.

A template is the Data Cleaner's answer to Task Builder's Task: the **whole working set**
recorded once — every expected file, the recipe that cleans it, and any Pivot / Group &
total / Unpivot table saved off it — so that next month's files can be cleaned by picking
a name instead of by repeating a dozen dialogs.

Whole set, not one table, because that is how the work actually arrives: "Receivables"
means `billwise_due`, `customer_master` and `sales` together, each with its own steps.

No Streamlit and no database here. `cleaner/session.py` builds one out of what is on
screen, `cleaner/matching.py` measures an upload against one, and `cleaner/db.py` stores
its JSON — the same split `tasks/` uses.

Three things are deliberately **not** stored.

**`table_id` and `file_id`.** Both are built from Streamlit's per-upload UUID
(`session.make_table_id`), which is meaningless in the session that opens the template
next month. A template is matched back onto an upload by the *file's own name*, which is
what `cleaner.matching` does.

**A summary's own steps.** They are empty by construction: `session.effective_steps`
resolves a derived table's recipe live from its parent, so storing them would be storing
a copy that could only ever go stale.

**Any data.** A recipe, never a snapshot — no frames, no previews, no row counts. The one
thing here that names data is `TemplateTable.columns`, and that is a list of column
*names* shown in the "expected files" dialog so a user can see what the template wants.
"""

import json
import logging
from dataclasses import dataclass, field

from cleaner.exceptions import TemplateStorageError
from cleaner.pipeline import CLEANING_RECIPE_VERSION, Step

logger = logging.getLogger(__name__)

UNTITLED_TEMPLATE = "Untitled template"

# Bumped only when a stored template would be read *wrongly* by the current code. The steps
# inside carry `CLEANING_RECIPE_VERSION` separately, exactly as the working set does.
TEMPLATE_VERSION = 1


def source_key(file_name: str, sheet_name: str | None) -> str:
    """The name one expected file is stored and matched under.

    The file's stem plus its sheet — `billwise_due`, or `workbook — Sheet1`. Not the
    editable output sheet name, which the user can rename at any time and which would then
    drift away from the key it is matched on; and not `source_label`, which keeps the
    extension, so re-saving the same data as `.xlsx` instead of `.csv` would stop matching.
    """
    stem = (file_name or "").rsplit(".", 1)[0].strip()
    sheet = (sheet_name or "").strip()
    return f"{stem} — {sheet}" if sheet else stem


def normalise(key: str) -> str:
    """The form two source keys are compared in.

    Case- and spacing-insensitive, on the grounds `chat_types.matching.normalise` gives:
    the name comes from a filename, and re-saving `Sales.xlsx` as `SALES.xlsx` has not
    changed which file it is.
    """
    return " ".join((key or "").split()).casefold()


@dataclass
class TemplateTable:
    """One expected uploaded file (or one sheet of one), and the recipe that cleans it."""

    name: str
    file_name: str = ""
    sheet_name: str | None = None
    output_sheet_name: str = ""
    steps: list[Step] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    recipe_version: int = CLEANING_RECIPE_VERSION

    def key(self) -> str:
        return normalise(self.name)


@dataclass
class TemplateSummary:
    """A derived table — a Group & total, Pivot or Unpivot saved off one of the files.

    `parent` is a `TemplateTable.name`, not an id, for the same reason the tables are keyed
    by name: an id would not survive the session it was made in.
    """

    parent: str
    name: str
    reshape: Step


@dataclass
class CleaningTemplate:
    """One saved working set.

    Attributes:
        template_id: None until it has been saved, exactly as `Task.task_id` is.
        name: what it is called. Unique per account — see `cleaner/db.py`.
        description: the author's note, shown in the picker so a template found six months
            later can be identified without opening it.
        tables: the expected files, each with its own steps.
        summaries: the derived tables to rebuild once their parents have been cleaned.
    """

    template_id: int | None = None
    name: str = ""
    description: str = ""
    tables: list[TemplateTable] = field(default_factory=list)
    summaries: list[TemplateSummary] = field(default_factory=list)
    version: int = TEMPLATE_VERSION

    def display_name(self) -> str:
        return (self.name or "").strip() or UNTITLED_TEMPLATE

    def table_names(self) -> list[str]:
        return [table.name for table in self.tables]

    def table(self, name: str) -> "TemplateTable | None":
        wanted = normalise(name)
        return next((table for table in self.tables if table.key() == wanted), None)

    def summaries_of(self, name: str) -> list[TemplateSummary]:
        wanted = normalise(name)
        return [summary for summary in self.summaries if normalise(summary.parent) == wanted]

    def summary_line(self) -> str:
        """One line for the picker: what this template contains.

        Counted rather than described, because the counts are what tell two similarly named
        templates apart at a glance.
        """
        steps = sum(len(table.steps) for table in self.tables)
        return (
            f"{len(self.tables)} file(s) - {steps} cleaning step(s) - "
            f"{len(self.summaries)} summary table(s)"
        )


def capture(
    name: str,
    *,
    description: str = "",
    tables: list[TemplateTable],
    summaries: list[TemplateSummary],
    template_id: int | None = None,
) -> CleaningTemplate:
    """Assembles a template from what the caller read off the working set.

    Takes the pieces rather than the session's `TableState` objects, on the grounds
    `tasks.model.capture` gives: this module never imports the one part of `cleaner/` that
    pulls in Streamlit, which is what keeps it testable without `AppTest`.

    The lists are **copied**, so a template held in session state cannot be quietly
    rewritten by the user continuing to clean the tables it was captured from.
    """
    return CleaningTemplate(
        template_id=template_id,
        name=(name or "").strip(),
        description=(description or "").strip(),
        tables=[
            TemplateTable(
                name=table.name,
                file_name=table.file_name,
                sheet_name=table.sheet_name,
                output_sheet_name=table.output_sheet_name,
                steps=[dict(step) for step in table.steps],
                columns=list(table.columns),
                recipe_version=table.recipe_version,
            )
            for table in tables
        ],
        summaries=[
            TemplateSummary(parent=summary.parent, name=summary.name, reshape=dict(summary.reshape))
            for summary in summaries
        ],
    )


# --------------------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------------------


def _step_to_dict(step: Step) -> dict:
    return {"action": str(step["action"]), "params": dict(step.get("params") or {})}


def to_json(template: CleaningTemplate) -> str:
    """Serialises a template for storage. The name, description and id live in their own columns.

    Raises:
        TemplateStorageError: if any step holds something JSON can't carry. Reported as one
            failure because from the page's point of view it is one: the recipe didn't survive.
    """
    try:
        payload = {
            "version": TEMPLATE_VERSION,
            "tables": [
                {
                    "name": table.name,
                    "file_name": table.file_name,
                    "sheet_name": table.sheet_name,
                    "output_sheet_name": table.output_sheet_name,
                    "steps": [_step_to_dict(step) for step in table.steps],
                    "columns": list(table.columns),
                    "recipe_version": table.recipe_version,
                }
                for table in template.tables
            ],
            "summaries": [
                {
                    "parent": summary.parent,
                    "name": summary.name,
                    "reshape": _step_to_dict(summary.reshape),
                }
                for summary in template.summaries
            ],
        }
        return json.dumps(payload, indent=2)
    except (TypeError, ValueError, KeyError) as error:
        logger.exception("Could not serialise cleaning template '%s'.", template.display_name())
        raise TemplateStorageError(f"This template couldn't be saved ({error}).") from error


def recipe_fingerprint(template: CleaningTemplate) -> str:
    """A stable string for "is this template the same as the one that was saved?".

    Everything a template holds is part of it — unlike a Task, whose schema is read from the
    live session — so this is `to_json`'s payload with the keys sorted and the name and
    description folded in.

    Raises:
        TemplateStorageError: if it refuses to serialise. The caller treats that as "can't
            tell", which is safer than claiming there is nothing to lose.
    """
    try:
        return json.dumps(
            {
                "name": template.display_name(),
                "description": (template.description or "").strip(),
                "body": json.loads(to_json(template)),
            },
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        logger.exception("Could not fingerprint cleaning template '%s'.", template.display_name())
        raise TemplateStorageError(
            f"This template's contents couldn't be compared ({error})."
        ) from error


def _read_step(raw: object) -> Step | None:
    """Reads one stored step, or None if it isn't one.

    Tolerant rather than strict: a stored recipe is a setting to honour as far as it still
    makes sense. A step whose action is unknown to this version is still returned — the
    replay path (`pipeline.apply_steps_with_report`) already skips and reports what it can't
    run, which is a better place to notice than a refused load.
    """
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if not isinstance(action, str) or not action:
        return None
    params = raw.get("params")
    return {"action": action, "params": dict(params) if isinstance(params, dict) else {}}


def from_json(
    text: str, *, template_id: int | None = None, name: str = "", description: str = ""
) -> CleaningTemplate:
    """Rebuilds a template from stored JSON.

    Raises:
        TemplateStorageError: if the text isn't the JSON object this module writes, or was
            written by a newer version. The template in front of the user is never replaced
            by a partial load — the caller only swaps it in once this returns.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("Stored cleaning template %s could not be parsed.", template_id)
        raise TemplateStorageError(
            "This saved template couldn't be read — its stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning(
            "Stored template %s was %s, not an object.", template_id, type(payload).__name__
        )
        raise TemplateStorageError(
            "This saved template couldn't be read — it isn't in the expected format."
        )

    version = payload.get("version")
    if isinstance(version, int) and version > TEMPLATE_VERSION:
        logger.warning("Cleaning template %s was saved by a newer version (%s).", template_id, version)
        raise TemplateStorageError(
            "This template was saved by a newer version of the app and can't be opened here."
        )

    tables: list[TemplateTable] = []
    for raw in payload.get("tables") or []:
        if not isinstance(raw, dict):
            continue
        table_name = str(raw.get("name") or "").strip()
        if not table_name:
            logger.warning("Dropped an unnamed table from stored template %s.", template_id)
            continue
        sheet_name = raw.get("sheet_name")
        tables.append(
            TemplateTable(
                name=table_name,
                file_name=str(raw.get("file_name") or ""),
                sheet_name=str(sheet_name) if sheet_name else None,
                output_sheet_name=str(raw.get("output_sheet_name") or ""),
                steps=[step for step in (_read_step(item) for item in raw.get("steps") or []) if step],
                columns=[str(column) for column in raw.get("columns") or []],
                recipe_version=int(raw.get("recipe_version") or CLEANING_RECIPE_VERSION),
            )
        )

    known = {table.key() for table in tables}
    summaries: list[TemplateSummary] = []
    for raw in payload.get("summaries") or []:
        if not isinstance(raw, dict):
            continue
        reshape = _read_step(raw.get("reshape"))
        parent = str(raw.get("parent") or "").strip()
        if reshape is None or not parent:
            logger.warning("Dropped a malformed summary from stored template %s.", template_id)
            continue
        if normalise(parent) not in known:
            # An orphan can never be rebuilt — `apply_template` needs the parent's table to
            # hang it off — so it is dropped here rather than carried as a silent no-op.
            logger.warning(
                "Dropped summary '%s' — its parent '%s' isn't in the template.",
                raw.get("name"),
                parent,
            )
            continue
        summaries.append(
            TemplateSummary(parent=parent, name=str(raw.get("name") or "Summary"), reshape=reshape)
        )

    return CleaningTemplate(
        template_id=template_id,
        name=name,
        description=description,
        tables=tables,
        summaries=summaries,
        version=TEMPLATE_VERSION,
    )
