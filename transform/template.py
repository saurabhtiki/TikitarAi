"""What a saved transform pipeline is, and how one is written down.

A pipeline is Transform Data's answer to the Data Cleaner's **cleaning template**: the
whole working set recorded once — every expected file, the ordered steps that turn them
into the answer, and which tables are the answer — so that next month's files can be
transformed by picking a name instead of by rebuilding a dozen steps.

No Streamlit and no database here. `transform/session.py` builds one out of what is on
screen, `transform/matching.py` measures an upload against one, and `transform/db.py`
stores its JSON — the same split `cleaner/` uses.

**The one real difference from a cleaning template.** A cleaning template hangs its recipe
off each file, because a cleaning step reads exactly one table. A transform step names the
table(s) it reads *and* the table it writes, and a join reads two at once — so the steps
here are **one ordered list belonging to the pipeline**, not a per-table recipe. Splitting
them by table would mean deciding which half of a join a step belongs to, and re-deriving
the execution order on load from something that was never stored.

Three things are deliberately **not** stored, for the reasons `cleaner/template.py` gives:

**Streamlit's per-upload id.** `NamedFrame.upload_file_id` is meaningless in the session
that opens the pipeline next month. A pipeline is matched back onto an upload by the
*file's own name*, which is what `transform.matching` does.

**Derived tables.** Only `origin="upload"` tables are recorded. A step table is rebuilt by
the steps, so storing it would be storing a copy that could only ever go stale.

**Any data.** A recipe, never a snapshot. The one thing here that names data is
`PipelineTable.columns`, a list of column *names* shown in the "expected files" dialog so a
user can see what the pipeline wants.
"""

import json
import logging
from dataclasses import dataclass, field

from transform.exceptions import PipelineStorageError
from transform.pipeline import TRANSFORM_PIPELINE_VERSION, TransformStep
from transform.workspace import NamedFrame, name_key

logger = logging.getLogger(__name__)

UNTITLED_PIPELINE = "Untitled pipeline"

# Bumped only when a stored pipeline would be read *wrongly* by the current code. The steps
# inside carry `TRANSFORM_PIPELINE_VERSION` separately, exactly as the working set does.
SAVED_PIPELINE_VERSION = 1


def source_key(file_name: str, sheet_name: str | None) -> str:
    """The name one expected file is matched under.

    The file's stem plus its sheet — `sales`, or `workbook - Sheet1`. Not the table name,
    which the user can rename at any time and which would then drift away from the key it
    is matched on; and not the file name with its extension, so re-saving the same data as
    `.xlsx` instead of `.csv` doesn't stop it matching.

    The same rule as `cleaner.template.source_key`, deliberately: a user with both pages
    open should not find that one recognises next month's file and the other doesn't.
    """
    stem = (file_name or "").rsplit(".", 1)[0].strip()
    sheet = (sheet_name or "").strip()
    return f"{stem} - {sheet}" if sheet else stem


def normalise(key: str) -> str:
    """The form two source keys are compared in.

    Case- and spacing-insensitive: the name comes from a filename, and re-saving
    `Sales.xlsx` as `SALES.xlsx` has not changed which file it is.
    """
    return " ".join((key or "").split()).casefold()


@dataclass
class PipelineTable:
    """One expected uploaded file (or one sheet of one).

    Attributes:
        name: what the table is called **in the steps**. This is the workspace name, so
            applying a pipeline renames the matched upload to it — otherwise every step
            would be looking for a table that isn't there.
        file_name: the file it was uploaded from, for matching and for the dialog.
        sheet_name: which sheet of it, or None for a CSV.
        columns: the column names the file had when the pipeline was saved. Shown, not
            enforced — what is enforced is `transform.matching.required_columns_by_table`,
            which is the much shorter list the steps actually read.
    """

    name: str
    file_name: str = ""
    sheet_name: str | None = None
    columns: list[str] = field(default_factory=list)

    def key(self) -> str:
        """What this table is matched on: its *file*, not its table name."""
        return normalise(source_key(self.file_name, self.sheet_name) or self.name)

    def source_label(self) -> str:
        """`sales.xlsx - Sheet1`, for the expected-files dialog."""
        if self.file_name and self.sheet_name:
            return f"{self.file_name} - {self.sheet_name}"
        return self.file_name or self.name


@dataclass
class SavedPipeline:
    """One saved working set: the files it expects, the steps, and the answer.

    Attributes:
        pipeline_id: None until it has been saved, exactly as `CleaningTemplate.template_id`.
        name: what it is called. Unique per account — see `transform/db.py`.
        description: the author's note, shown in the picker so a pipeline found six months
            later can be identified without opening it.
        tables: the expected uploaded files.
        steps: every step, in execution order, across all of those tables.
        outputs: the table names marked for download. Rebuilt by the steps like any other
            derived table; stored only so the picker comes back pointing at the answer.
    """

    pipeline_id: int | None = None
    name: str = ""
    description: str = ""
    tables: list[PipelineTable] = field(default_factory=list)
    steps: list[TransformStep] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    version: int = SAVED_PIPELINE_VERSION
    step_version: int = TRANSFORM_PIPELINE_VERSION

    def display_name(self) -> str:
        return (self.name or "").strip() or UNTITLED_PIPELINE

    def table_names(self) -> list[str]:
        return [table.name for table in self.tables]

    def table(self, name: str) -> "PipelineTable | None":
        """The expected file whose **table name** is `name`, compared as a table name is."""
        return next((table for table in self.tables if name_key(table.name) == name_key(name)), None)

    def summary_line(self) -> str:
        """One line for the picker: what this pipeline contains.

        Counted rather than described, because the counts are what tell two similarly named
        pipelines apart at a glance.
        """
        return (
            f"{len(self.tables)} table(s) - {len(self.steps)} step(s) - "
            f"{len(self.outputs)} output(s)"
        )


def capture(
    name: str,
    *,
    description: str = "",
    upload_workspace: dict[str, NamedFrame],
    steps: list[TransformStep],
    outputs: list[str],
    pipeline_id: int | None = None,
) -> SavedPipeline:
    """Assembles a pipeline from the uploaded tables, the step list and the export choice.

    Takes the pieces rather than reading session state, on the grounds
    `cleaner.template.capture` gives: this module never imports the one part of
    `transform/` that pulls in Streamlit, which is what keeps it testable without `AppTest`.

    Only `origin="upload"` tables are recorded — see the module docstring. The lists are
    **copied**, so a pipeline held in session state cannot be quietly rewritten by the user
    carrying on transforming the tables it was captured from.
    """
    tables = [
        PipelineTable(
            name=held.name,
            file_name=file_name_of(held),
            sheet_name=sheet_name_of(held),
            columns=[str(column) for column in held.frame.columns],
        )
        for held in upload_workspace.values()
        if held.origin == "upload"
    ]
    return SavedPipeline(
        pipeline_id=pipeline_id,
        name=(name or "").strip(),
        description=(description or "").strip(),
        tables=tables,
        # Round-tripped through JSON rather than `copy.deepcopy`, so a step holding
        # something unserialisable fails here — while the user is pressing Save and can be
        # told — instead of at `to_json` time with a half-built pipeline in hand.
        steps=[json.loads(json.dumps(step, default=str)) for step in steps],
        # Kept as chosen. An output naming a table this pipeline no longer builds is
        # dropped by the download picker (`session.seed_export_selection`) rather than
        # here, so there is one place that decides what is selectable.
        outputs=[str(output) for output in outputs],
    )


def file_name_of(held: NamedFrame) -> str:
    """The file an uploaded table came from, read back off its source label.

    `NamedFrame` records `source_label` (`sales.xlsx - Sheet1`) rather than the two parts,
    because that label is what the tab caption shows. Splitting it back here keeps
    `NamedFrame` as it is rather than growing two fields for this module's benefit.
    """
    label = held.source_label or held.name
    return label.split(" - ", 1)[0].strip()


def sheet_name_of(held: NamedFrame) -> str | None:
    label = held.source_label or ""
    return label.split(" - ", 1)[1].strip() if " - " in label else None


# --------------------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------------------


def to_json(pipeline: SavedPipeline) -> str:
    """Serialises a pipeline for storage. The name, description and id live in their own columns.

    Raises:
        PipelineStorageError: if any step holds something JSON can't carry. Reported as one
            failure because from the page's point of view it is one: the steps didn't survive.
    """
    try:
        payload = {
            "version": SAVED_PIPELINE_VERSION,
            "step_version": pipeline.step_version,
            "tables": [
                {
                    "name": table.name,
                    "file_name": table.file_name,
                    "sheet_name": table.sheet_name,
                    "columns": list(table.columns),
                }
                for table in pipeline.tables
            ],
            "steps": pipeline.steps,
            "outputs": list(pipeline.outputs),
        }
        return json.dumps(payload, indent=2)
    except (TypeError, ValueError, KeyError) as error:
        logger.exception("Could not serialise transform pipeline '%s'.", pipeline.display_name())
        raise PipelineStorageError(f"This pipeline couldn't be saved ({error}).") from error


def _read_step(raw: object) -> TransformStep | None:
    """Reads one stored step, or None if it isn't one.

    Tolerant rather than strict, for the reason `cleaner.template._read_step` records: a
    step whose operation is unknown to this version is still returned. Refusing the whole
    load would leave the user with no way to see what the pipeline contains, whereas the
    replay path already reports a step it cannot run — and that is where someone is
    looking when they wonder why a pipeline stopped.
    """
    if not isinstance(raw, dict):
        return None
    operation = raw.get("operation")
    if not isinstance(operation, str) or not operation:
        return None

    inputs = raw.get("inputs")
    output = raw.get("output")
    output = output if isinstance(output, dict) else {}
    name = str(output.get("name") or "")
    return {
        "operation": operation,
        "inputs": dict(inputs) if isinstance(inputs, dict) else {},
        "params": dict(raw.get("params")) if isinstance(raw.get("params"), dict) else {},
        "output": {
            "mode": "new" if output.get("mode") == "new" else "in_place",
            "name": name,
        },
    }


def from_json(
    text: str, *, pipeline_id: int | None = None, name: str = "", description: str = ""
) -> SavedPipeline:
    """Rebuilds a pipeline from stored JSON.

    Raises:
        PipelineStorageError: if the text isn't the JSON object this module writes, or was
            written by a newer version. The pipeline in front of the user is never replaced
            by a partial load — the caller only swaps it in once this returns.
    """
    try:
        payload = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("Stored transform pipeline %s could not be parsed.", pipeline_id)
        raise PipelineStorageError(
            "This saved pipeline couldn't be read - its stored contents aren't valid JSON."
        ) from error

    if not isinstance(payload, dict):
        logger.warning(
            "Stored pipeline %s was %s, not an object.", pipeline_id, type(payload).__name__
        )
        raise PipelineStorageError(
            "This saved pipeline couldn't be read - it isn't in the expected format."
        )

    version = payload.get("version")
    if isinstance(version, int) and version > SAVED_PIPELINE_VERSION:
        logger.warning("Transform pipeline %s was saved by a newer version (%s).", pipeline_id, version)
        raise PipelineStorageError(
            "This pipeline was saved by a newer version of the app and can't be opened here."
        )

    tables: list[PipelineTable] = []
    for raw in payload.get("tables") or []:
        if not isinstance(raw, dict):
            continue
        table_name = str(raw.get("name") or "").strip()
        if not table_name:
            logger.warning("Dropped an unnamed table from stored pipeline %s.", pipeline_id)
            continue
        sheet_name = raw.get("sheet_name")
        tables.append(
            PipelineTable(
                name=table_name,
                file_name=str(raw.get("file_name") or ""),
                sheet_name=str(sheet_name) if sheet_name else None,
                columns=[str(column) for column in raw.get("columns") or []],
            )
        )

    steps = [step for step in (_read_step(raw) for raw in payload.get("steps") or []) if step]

    return SavedPipeline(
        pipeline_id=pipeline_id,
        name=name,
        description=description,
        tables=tables,
        steps=steps,
        outputs=[str(output) for output in payload.get("outputs") or []],
        version=SAVED_PIPELINE_VERSION,
        step_version=int(payload.get("step_version") or TRANSFORM_PIPELINE_VERSION),
    )
