"""Checking an upload against the saved pipeline it is about to be run through.

Pure: no Streamlit, no database. `transform/session.py` reads the workspace and hands the
plain `(file name, sheet name, columns)` triples in; this module says which of the
pipeline's expected files are here, which are missing, and which required columns aren't.

`cleaner/matching.py` is the model, and two of its three rules carry over unchanged.

**An extra file is left alone, never discarded.** A pipeline is applied to files the user
chose to upload; throwing one away because a saved recipe doesn't mention it would destroy
work. So an extra table simply takes part in no step, and is mentioned.

**A report, not an exception.** A user with three missing files should see three, not
discover them one refusal at a time.

The third rule is where this parts company with the cleaner, and it is section 6 of the
requirements document: **columns are checked here, before anything runs.** The cleaner can
skip a step whose column has gone and carry on, because with one table the later steps
still do useful work. Here step 3 may read a table step 2 was supposed to build, so a
missing column at step 2 means every number after it is computed from the wrong data. The
requirement is explicit — block execution and name the column.

**Required columns, not every column.** Also section 6: "required columns present, not
exact match". Extra columns and reordered columns are fine and ignored; what must be there
is what the steps actually read, which every registry entry reports through
`OperationSpec.required_columns`.
"""

import logging
from dataclasses import dataclass, field

from transform.exceptions import TransformError
from transform.expressions import referenced_columns
from transform.params import ParamKind
from transform.pipeline import TransformStep
from transform.registry import get_operation
from transform.template import PipelineTable, SavedPipeline, normalise, source_key
from transform.workspace import name_key

logger = logging.getLogger(__name__)


def required_columns_by_table(
    steps: list[TransformStep], known_columns: dict[str, list[str]] | None = None
) -> dict[str, list[str]]:
    """Which columns of which **uploaded** table the steps depend on.

    Walks the steps in execution order, asking each operation's `required_columns` what it
    reads, and attributes each answer to the table that role names.

    **The saved schema decides what counts.** A step reading `sales` halfway down the
    pipeline may be reading `bonus`, a column an *earlier step created* — demanding that of
    next month's upload would refuse a perfectly good file for not already containing
    something the pipeline itself produces. `known_columns` is what tells the two apart: a
    column is required of the file only if the file had it when the pipeline was saved.
    Anything else the steps mention, the steps made. This needs no per-operation knowledge
    of which steps add columns, which is why it is done here rather than in the registry.

    Passing no `known_columns` reports everything the steps read, unfiltered — useful for
    describing a step list, but not for validating an upload.

    **Formulas are resolved here too.** `required_add_calculated_column` returns an empty
    list by design: without a column list a formula can't be tokenized. With the saved
    schema in hand it can, which is what phase 26 left this note for.

    Returns `{table name: [column, ...]}` in first-seen order, duplicates removed. A table
    the steps read nothing from (a `concat`'s inputs, say) still appears, with an empty
    list — it is expected, it just has no column requirement.

    Never raises for an unknown operation: a pipeline saved by a newer version is reported
    by the replay, and refusing to describe it here would leave the user unable to see what
    the pipeline wants at all.
    """
    schema = {name_key(name): list(columns) for name, columns in (known_columns or {}).items()}
    required: dict[str, list[str]] = {}

    def note(table: str, columns: list[str]) -> None:
        if not table:
            return
        if schema and name_key(table) not in schema:
            # A table the pipeline builds, not one it expects to be uploaded. There is no
            # file to check it against.
            return
        allowed = schema.get(name_key(table))
        held = required.setdefault(table, [])
        for column in columns:
            if not column or column in held:
                continue
            if allowed is not None and column not in allowed:
                continue
            held.append(column)

    for position, step in enumerate(steps):
        try:
            spec = get_operation(step.get("operation", ""))
        except TransformError:
            logger.warning(
                "Step %s names operation '%s', which this version doesn't know - its "
                "columns can't be checked.",
                position + 1,
                step.get("operation"),
            )
            continue

        params = dict(step.get("params") or {})
        inputs = step.get("inputs") or {}
        try:
            wanted = spec.required_columns(params)
        except (KeyError, TypeError, ValueError):
            logger.exception("Could not read the required columns of step %s.", position + 1)
            wanted = {}

        for role, columns in _expression_columns(spec, params, inputs, schema).items():
            wanted[role] = [*(wanted.get(role) or []), *columns]

        for role, value in inputs.items():
            columns = [str(column) for column in (wanted.get(role) or [])]
            if isinstance(value, list):
                # Every table of a multi-role gets the same requirement: `concat` stacks
                # them, so a column named there has to be in each one.
                for entry in value:
                    note(str(entry), columns)
            else:
                note(str(value or ""), columns)

    return required


def _expression_columns(
    spec, params: dict, inputs: dict, schema: dict[str, list[str]]
) -> dict[str, list[str]]:
    """The columns any formula box in this step reads, by role.

    Generic rather than a special case for `add_calculated_column`: every `EXPRESSION`
    parameter in the registry is resolved the same way, so an operation that grows one
    later is covered without touching this.

    A formula that won't tokenize contributes nothing. That is not a silent pass — the step
    itself will refuse it at replay, with a message about the formula, which is far clearer
    than "a column is missing".
    """
    found: dict[str, list[str]] = {}
    for param in spec.params:
        if param.kind is not ParamKind.EXPRESSION:
            continue
        text = str(params.get(param.name) or "").strip()
        table = inputs.get(param.from_role)
        if not text or isinstance(table, list) or not table:
            continue
        columns = schema.get(name_key(str(table)))
        if not columns:
            continue
        try:
            found.setdefault(param.from_role, []).extend(referenced_columns(text, columns))
        except TransformError:
            logger.info("Formula '%s' didn't tokenize while checking required columns.", text)
    return found


@dataclass
class PipelineMatch:
    """Which of a pipeline's expected files the current upload has, and whether they fit.

    Attributes:
        matched: the pipeline's table name -> the workspace name it was found under. What
            `session.apply_pipeline` walks to know which table to rename to what.
        missing: expected tables with nothing uploaded under their file name.
        extra: uploaded tables the pipeline says nothing about. Never discarded.
        missing_columns: the pipeline's table name -> the required columns its matched
            upload doesn't have. Blocking, per section 6 of the requirements document.
        uploaded_columns: the pipeline's table name -> what its matched upload does have,
            kept so the error can say what was found instead of what was wanted.
    """

    matched: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    uploaded_columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether the pipeline can run: every file here, and every required column in it.

        Unlike a cleaning template, a partial match is not "run what you can" — see the
        module docstring.
        """
        return not self.missing and not self.missing_columns

    @property
    def has_notes(self) -> bool:
        return bool(self.extra)

    def status_word(self) -> str:
        """Two words for the bar, which is all there is room for beside the picker."""
        return "ready" if self.ok else "needs attention"

    def problems(self) -> list[str]:
        """Everything blocking the run, as sentences naming the file and the column.

        The column wording is section 6.4's, near enough word for word: naming both what
        was expected and what arrived is what turns "it didn't work" into "ah, a space
        instead of an underscore".
        """
        lines = [
            f"**{name}** - nothing uploaded matches this file, so the pipeline can't run."
            for name in self.missing
        ]
        for table_name, columns in self.missing_columns.items():
            found = self.uploaded_columns.get(table_name) or []
            listed = ", ".join(f"'{column}'" for column in columns)
            available = ", ".join(f"'{column}'" for column in found[:12]) or "no columns at all"
            if len(found) > 12:
                available += ", ..."
            lines.append(
                f"**{table_name}** - this pipeline expects the column(s) {listed}, but the "
                f"uploaded file has {available}."
            )
        return lines

    def notes(self) -> list[str]:
        """The differences worth mentioning that stopped nothing."""
        if not self.extra:
            return []
        listed = ", ".join(f"**{name}**" for name in self.extra)
        return [
            f"{len(self.extra)} uploaded table(s) aren't part of this pipeline and were left "
            f"as they are: {listed}."
        ]

    def summary(self) -> str:
        """The one line the bar puts beside the picker."""
        expected = len(self.matched) + len(self.missing)
        if self.missing:
            return (
                f"{len(self.matched)} of {expected} expected file(s) matched - "
                f"{len(self.missing)} missing."
            )
        if self.missing_columns:
            short = sum(len(columns) for columns in self.missing_columns.values())
            return (
                f"All {expected} expected file(s) matched, but {short} required column(s) "
                f"are missing."
            )
        if self.extra:
            return (
                f"{len(self.matched)} expected file(s) matched, plus {len(self.extra)} not "
                f"in this pipeline."
            )
        return f"{len(self.matched)} expected file(s) matched this pipeline exactly."


def check_upload(
    pipeline: SavedPipeline, uploaded: dict[str, tuple[str, str | None, list[str]]]
) -> PipelineMatch:
    """Builds the whole-upload report.

    Args:
        pipeline: the saved working set the upload is being checked against.
        uploaded: every **uploaded** table currently in the workspace, as
            `table name -> (file_name, sheet_name, columns)`. Step tables are the caller's
            to leave out — they have no file of their own, and the steps rebuild them.

    Never raises. See the module docstring.
    """
    by_key: dict[str, str] = {}
    duplicates: list[str] = []
    for table_name, (file_name, sheet_name, _columns) in uploaded.items():
        key = normalise(source_key(file_name, sheet_name) or table_name)
        if key in by_key:
            # First upload wins, on `cleaner.matching`'s grounds: two files reduce to one
            # key only when the same name was uploaded twice, and silently preferring the
            # later one would move the steps between two tables that look identical on
            # screen. The loser is still listed as extra rather than passed over.
            duplicates.append(table_name)
            continue
        by_key[key] = table_name

    match = PipelineMatch()
    required = required_columns_by_table(pipeline.steps, _saved_schema(pipeline))

    for table in pipeline.tables:
        found = by_key.get(table.key())
        if found is None:
            match.missing.append(table.name)
            continue
        match.matched[table.name] = found
        columns = list(uploaded[found][2])
        match.uploaded_columns[table.name] = columns
        short = _missing_columns(required.get(table.name) or [], columns)
        if short:
            match.missing_columns[table.name] = short

    expected_keys = {table.key() for table in pipeline.tables}
    match.extra = [
        table_name for key, table_name in by_key.items() if key not in expected_keys
    ] + duplicates

    if not match.ok:
        logger.info(
            "Upload didn't match transform pipeline '%s': %d file(s) missing, %d table(s) "
            "short of a required column.",
            pipeline.display_name(),
            len(match.missing),
            len(match.missing_columns),
        )
    return match


def _missing_columns(required: list[str], present: list[str]) -> list[str]:
    """The required columns `present` doesn't have, in the order they were asked for.

    Compared exactly, not case-insensitively. A pandas column named `Amount` is a different
    column from `amount` — every step would refuse it — so treating them as the same here
    would let a file through only for the first step to fail on it, which is worse than
    being told up front.
    """
    have = set(present)
    return [column for column in required if column not in have]


def _saved_schema(pipeline: SavedPipeline) -> dict[str, list[str]]:
    """What each expected file had when the pipeline was saved."""
    return {table.name: list(table.columns) for table in pipeline.tables}


def expected_columns(pipeline: SavedPipeline, table: PipelineTable) -> list[str]:
    """The columns of `table` the pipeline's steps actually read, for the dialog."""
    return required_columns_by_table(pipeline.steps, _saved_schema(pipeline)).get(table.name) or []
