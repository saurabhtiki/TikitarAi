"""What a list of transformation steps is, how a step joins it, and how it runs.

Two guarantees matter beyond this phase, because phase 26 saves these step lists and
replays them against next month's upload:

1. **A step is JSON round-trippable** — `json.loads(json.dumps(steps)) == steps`.
2. **The stored order is the execution order.** `apply_steps_with_report` never sorts.

The difference from `cleaner.pipeline`, which does the same job for a single table, is that
a step here names the tables it reads and the table it writes. That is why an executor takes
`frames_by_role` rather than one frame, and why replay threads a whole workspace through
rather than a single DataFrame.

**A failed step stops the run**, which is the other difference. The cleaner skips a broken
step and carries on, correctly: with one table, later steps can still do useful work. Here
step 3 may read a table step 2 was supposed to create, so carrying on would compute an
answer from the wrong data and present it as fine. Stopping and saying which step failed is
the only honest option.
"""

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, TypedDict

import pandas as pd

from transform.exceptions import (
    DuplicateFrameNameError,
    FrameNotFoundError,
    InvalidStepParamsError,
    TransformError,
)
from transform.registry import OperationSpec, get_operation
from transform.workspace import (
    NamedFrame,
    columns_of,
    get_dataframe,
    has_frame,
    name_key,
    normalise_frame_name,
    put_frame,
    suggest_frame_name,
)

logger = logging.getLogger(__name__)

#: Bumped only when a stored step list would be read *wrongly* by the current code.
TRANSFORM_PIPELINE_VERSION = 1

#: The errors an executor may raise that mean "this step can't run on this data", as
#: opposed to a bug. Named explicitly rather than caught as `Exception`, so a genuine
#: programming error still surfaces as a crash in testing instead of a friendly message.
_STEP_FAILURES = (TransformError, ValueError, TypeError, KeyError, re.error)


class OutputSpec(TypedDict):
    """Where a step's answer goes.

    `name` is populated even when `mode` is `in_place`, where it repeats the source table's
    name. One field to read rather than a branch, in the executor, in the step list line
    and in phase 26's serialiser.
    """

    mode: Literal["in_place", "new"]
    name: str


class TransformStep(TypedDict):
    """One step.

    `inputs` maps an `InputRole.name` to a table name — or to a list of them, for a `multi`
    role like `concat`'s. Named rather than positional so that `step["inputs"]["right"]`
    says what it means.
    """

    operation: str
    inputs: dict
    params: dict
    output: OutputSpec


@dataclass(frozen=True)
class StepOutcome:
    """What happened when one step ran, for the step list and the run report."""

    index: int
    operation: str
    label: str
    status: Literal["applied", "warned", "failed"]
    message: str
    input_names: dict
    output_name: str
    rows_before: int
    rows_after: int
    columns_before: int
    columns_after: int

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def make_step(
    operation: str, inputs: dict, params: dict, output_mode: str, output_name: str
) -> TransformStep:
    """Builds a step dict.

    Raises:
        InvalidOperationError: if `operation` isn't in the registry.
    """
    get_operation(operation)
    mode: Literal["in_place", "new"] = "new" if output_mode == "new" else "in_place"
    return {
        "operation": operation,
        "inputs": copy.deepcopy(inputs),
        "params": copy.deepcopy(params),
        "output": {"mode": mode, "name": normalise_frame_name(output_name)},
    }


def input_names(step: TransformStep) -> list[str]:
    """Every table name a step reads, flattened, in role order."""
    names: list[str] = []
    for value in step.get("inputs", {}).values():
        if isinstance(value, list):
            names.extend(str(item) for item in value)
        elif value:
            names.append(str(value))
    return names


def suggested_output_name(
    spec: OperationSpec, inputs: dict, workspace: dict[str, NamedFrame]
) -> str:
    """The pre-filled name for a step that makes a new table.

    `{source}` in the hint stands for the first input's name, so joining `sales` suggests
    `sales_joined`; then `suggest_frame_name` makes it free. Pre-filling a name that is
    already available is what keeps the duplicate-name block a backstop rather than
    something a user meets on the normal path.
    """
    primary = inputs.get(spec.primary_role)
    if isinstance(primary, list):
        primary = primary[0] if primary else ""
    base = spec.output_name_hint.format(source=primary or "table")
    return suggest_frame_name(base, list(workspace))


def _columns_by_role(step: TransformStep, workspace: dict[str, NamedFrame]) -> dict[str, list[str]]:
    """Each role's table's column names, for a validator to check parameters against."""
    columns: dict[str, list[str]] = {}
    for role, value in step.get("inputs", {}).items():
        if isinstance(value, list):
            merged: list[str] = []
            for name in value:
                merged.extend(column for column in columns_of(workspace, str(name)) if column not in merged)
            columns[role] = merged
        else:
            columns[role] = columns_of(workspace, str(value or ""))
    return columns


def validate_step(step: TransformStep, workspace: dict[str, NamedFrame]) -> None:
    """Checks a step against the workspace it will actually run in.

    Called before a step is admitted to a pipeline, which is what makes "a stored pipeline
    is always well-formed" an invariant rather than a hope.

    Raises:
        InvalidOperationError: unknown operation.
        FrameNotFoundError: a named input table isn't in the workspace.
        InvalidStepParamsError: a role has no table, a multi role has fewer than two, or
            the operation's own validator rejected the parameters.
        DuplicateFrameNameError: the step would create a new table under a taken name.
    """
    spec = get_operation(step["operation"])
    inputs = step.get("inputs", {})

    for role in spec.inputs:
        value = inputs.get(role.name)
        if role.multi:
            chosen = [str(name) for name in (value or [])]
            if len(chosen) < 2:
                raise InvalidStepParamsError(f"Choose at least two tables for '{role.label}'.")
            for name in chosen:
                if not has_frame(workspace, name):
                    raise FrameNotFoundError(f"There's no table called '{name}'.")
            continue

        if not value:
            raise InvalidStepParamsError(f"Choose a table for '{role.label}'.")
        if not has_frame(workspace, str(value)):
            raise FrameNotFoundError(f"There's no table called '{value}'.")

    spec.validate(_columns_by_role(step, workspace), step.get("params", {}))

    output = step.get("output", {})
    if output.get("mode") == "new":
        name = normalise_frame_name(output.get("name", ""))
        if not name:
            raise InvalidStepParamsError("Type a name for the new table.")
        if has_frame(workspace, name):
            raise DuplicateFrameNameError(
                f"A table named '{name}' already exists - choose another name."
            )


def append_step(steps: list[TransformStep], step: TransformStep) -> list[TransformStep]:
    """Returns a new step list with `step` on the end.

    Always appends. Unlike the cleaner's `add_step`, no step is ever merged into an earlier
    one or floated to a pinned position: a step here names the table it reads, so moving it
    would change which data it saw.
    """
    return [copy.deepcopy(existing) for existing in steps] + [copy.deepcopy(step)]


def replace_last_step(steps: list[TransformStep], step: TransformStep) -> list[TransformStep]:
    """Returns a new step list with the final step swapped for `step`.

    Raises:
        IndexError: if there are no steps to replace.
    """
    if not steps:
        raise IndexError("There's no step to edit.")
    return [copy.deepcopy(existing) for existing in steps[:-1]] + [copy.deepcopy(step)]


def remove_last_step(steps: list[TransformStep]) -> list[TransformStep]:
    """Returns a new step list without its final step.

    Last-step-only, per section 7 of the requirements document: steps feed each other by
    table name, so removing one from the middle could leave three below it reading a table
    that no longer exists.

    Raises:
        IndexError: if there are no steps to remove.
    """
    if not steps:
        raise IndexError("There's no step to delete.")
    return [copy.deepcopy(existing) for existing in steps[:-1]]


def frames_created_by(steps: list[TransformStep], index: int) -> list[str]:
    """The table names a step brings into being, for the delete confirmation.

    A step whose output mode is `new` creates its output table — unless an earlier step
    already created one by that name, which cannot happen while `validate_step` is the only
    way in, but is cheap to be right about.
    """
    if not 0 <= index < len(steps):
        return []
    step = steps[index]
    if step.get("output", {}).get("mode") != "new":
        return []
    name = step["output"]["name"]
    earlier = {
        name_key(other["output"]["name"])
        for other in steps[:index]
        if other.get("output", {}).get("mode") == "new"
    }
    return [] if name_key(name) in earlier else [name]


def _resolve_inputs(step: TransformStep, workspace: dict[str, NamedFrame]) -> dict:
    """Each role's actual DataFrame(s).

    Raises:
        FrameNotFoundError: if a named table has gone.
    """
    resolved: dict = {}
    for role, value in step.get("inputs", {}).items():
        if isinstance(value, list):
            resolved[role] = [get_dataframe(workspace, str(name)) for name in value]
        else:
            resolved[role] = get_dataframe(workspace, str(value))
    return resolved


def apply_steps_with_report(
    base_workspace: dict[str, NamedFrame], steps: list[TransformStep]
) -> tuple[dict[str, NamedFrame], list[StepOutcome]]:
    """Runs every step in list order against a copy of `base_workspace`.

    Returns the finished workspace and one outcome per step that ran. **Stops at the first
    failure** — see the module docstring — so the outcome list may be shorter than `steps`,
    and its last entry is the one that failed.

    Never raises for a data problem: a step that cannot run is reported, not thrown. Only a
    genuinely unregistered operation raises, because that means the step list itself is not
    something this version can reason about.

    Raises:
        InvalidOperationError: if a step names an operation that isn't registered.
    """
    workspace = dict(base_workspace)
    report: list[StepOutcome] = []

    for index, step in enumerate(steps):
        spec = get_operation(step["operation"])
        output_name = step.get("output", {}).get("name", "")
        names = step.get("inputs", {})

        try:
            frames_by_role = _resolve_inputs(step, workspace)
        except FrameNotFoundError as error:
            logger.warning("Step %s couldn't find its table: %s", index + 1, error)
            report.append(
                _failure(index, spec, str(error), names, output_name)
            )
            break

        primary = frames_by_role.get(spec.primary_role)
        if isinstance(primary, list):
            primary = primary[0] if primary else pd.DataFrame()
        rows_before = len(primary) if isinstance(primary, pd.DataFrame) else 0
        columns_before = len(primary.columns) if isinstance(primary, pd.DataFrame) else 0

        params = dict(step.get("params", {}))
        if any(role.multi for role in spec.inputs):
            # `concat` labels its rows with the table each came from, which means the
            # executor needs the names as well as the frames. Passed as a private param so
            # the executor signature stays `(frames_by_role, params)` for everything.
            params["_frame_names"] = [str(name) for name in input_names(step)]

        try:
            result, warnings_out = spec.apply(frames_by_role, params)
        except _STEP_FAILURES as error:
            logger.exception("Transform step '%s' failed at position %s.", step["operation"], index)
            report.append(_failure(index, spec, str(error), names, output_name))
            break

        try:
            workspace = put_frame(
                workspace,
                NamedFrame(
                    name=output_name,
                    frame=result,
                    origin="step",
                    source_label=f"step {index + 1}: {spec.label}",
                    created_by_step=index,
                ),
                # An in-place step writes back over the table it read, so it must replace.
                # A step making a *new* table must not: at pipeline-definition time
                # `validate_step` already refuses a taken name, but a **saved pipeline
                # replayed against next month's upload** can meet one it has never seen —
                # an extra file the user uploaded that happens to share the name. Silently
                # overwriting it would contradict `transform.matching`, which has just told
                # the user that file was "left as it is".
                replace=step.get("output", {}).get("mode") != "new",
            )
        except DuplicateFrameNameError as error:
            logger.warning("Step %s couldn't create its table: %s", index + 1, error)
            report.append(_failure(index, spec, str(error), names, output_name))
            break

        report.append(
            StepOutcome(
                index=index,
                operation=step["operation"],
                label=spec.label,
                status="warned" if warnings_out else "applied",
                message=" ".join(warnings_out),
                input_names=copy.deepcopy(names),
                output_name=output_name,
                rows_before=rows_before,
                rows_after=len(result),
                columns_before=columns_before,
                columns_after=len(result.columns),
            )
        )

    return workspace, report


def _failure(
    index: int, spec: OperationSpec, message: str, names: dict, output_name: str
) -> StepOutcome:
    """The outcome recorded for a step that couldn't run."""
    return StepOutcome(
        index=index,
        operation=spec.operation,
        label=spec.label,
        status="failed",
        message=message,
        input_names=copy.deepcopy(names),
        output_name=output_name,
        rows_before=0,
        rows_after=0,
        columns_before=0,
        columns_after=0,
    )


def apply_steps(
    base_workspace: dict[str, NamedFrame], steps: list[TransformStep]
) -> dict[str, NamedFrame]:
    """Runs every step and returns just the finished workspace."""
    workspace, _ = apply_steps_with_report(base_workspace, steps)
    return workspace


def describe_step(step: TransformStep) -> str:
    """The step list line for one step.

    Derived, never stored, so improving the wording later improves every saved pipeline's
    step list retroactively — the same reasoning `cleaner.pipeline.describe_step` records.
    """
    try:
        spec = get_operation(step["operation"])
        return spec.describe(step)
    except TransformError:
        logger.exception("Could not describe transform step %s.", step)
        return f"Unknown step: {step.get('operation')}"


def describe_steps(steps: list[TransformStep]) -> list[str]:
    """One line per step, in order."""
    return [describe_step(step) for step in steps]


def step_headline(step: TransformStep, index: int) -> str:
    """`3. Joined sales with customers on customer_id -> merged_data`.

    The one line the step list shows, with the arrow only when the step makes a new table —
    an in-place step's output name is its input's, and repeating it reads like a mistake.
    """
    line = f"{index + 1}. {describe_step(step)}"
    if step.get("output", {}).get("mode") == "new":
        return f"{line} -> {step['output']['name']}"
    return line


def to_json(steps: list[TransformStep]) -> str:
    """Serialises a step list.

    Phase 26 stores this; the round trip is asserted in the tests so that the guarantee is
    checked now rather than discovered later.

    Raises:
        InvalidStepParamsError: if a step holds something JSON can't carry.
    """
    try:
        return json.dumps(
            {"version": TRANSFORM_PIPELINE_VERSION, "steps": steps}, indent=2, default=str
        )
    except (TypeError, ValueError) as error:
        logger.exception("Could not serialise the transform step list.")
        raise InvalidStepParamsError(f"These steps couldn't be saved ({error}).") from error
