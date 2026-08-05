"""The recipe: what a cleaning step list is, how steps enter it, and how it executes.

This is the module Stage 7 (Task Builder) serializes and Stage 8 (Run a Task) replays,
so its two guarantees matter beyond this stage:

1. A step is JSON round-trippable — `json.loads(json.dumps(steps)) == steps`.
2. The stored order *is* the execution order. `apply_steps` never sorts; all ordering
   decisions happen when a step is added, so the human-readable log can never disagree
   with what actually ran.
"""

import copy
import logging
import re
from dataclasses import dataclass
from typing import Literal, TypedDict

import pandas as pd

from cleaner.exceptions import InvalidStepError
from cleaner.steps import RecordPolicy, get_spec

logger = logging.getLogger(__name__)

CLEANING_RECIPE_VERSION = 1


class Step(TypedDict):
    action: str
    params: dict


@dataclass(frozen=True)
class StepOutcome:
    """What happened when one step ran, for the log and for Stage 8's run report."""

    index: int
    action: str
    status: Literal["applied", "skipped", "warned"]
    message: str
    rows_before: int
    rows_after: int
    columns_before: int
    columns_after: int


def make_step(action: str, params: dict) -> Step:
    """Builds a step dict.

    Raises:
        InvalidStepError: if `action` isn't a registered cleaning action.
    """
    get_spec(action)
    return {"action": action, "params": params}


def validate_step(step: Step, current_columns: list[str]) -> None:
    """Checks a step against the columns it will actually see.

    Called before a step is admitted to a recipe, which is what makes "a stored recipe
    is always well-formed" an invariant rather than a hope.

    Raises:
        InvalidStepError: unknown action, malformed parameters, a rename that would
            produce duplicate column names, or a regex that won't compile.
    """
    spec = get_spec(step["action"])
    spec.validate(step.get("params", {}), list(current_columns))


def add_step(steps: list[Step], step: Step) -> list[Step]:
    """Returns a new step list with `step` recorded according to its RecordPolicy.

    ADD appends. REPLACE overwrites the existing entry **at its current index** — if an
    edited `skip_rows` moved to the end it would re-run row-skipping after type
    coercion and silently change the results. UPDATE_PER_COLUMN merges the new
    `by_column` entries into the existing step, so re-applying a per-column panel
    updates only the columns named this time; a column mapped to None is removed, and
    an emptied mapping removes the step entirely.

    Pinned steps are then floated to a sorted prefix, so `skip_rows` always runs before
    `set_column_types`, which always runs before everything else.
    """
    spec = get_spec(step["action"])
    result = copy.deepcopy(steps)
    position = next((index for index, existing in enumerate(result) if existing["action"] == step["action"]), None)

    if spec.record is RecordPolicy.ADD or position is None:
        result.append(copy.deepcopy(step))
    elif spec.record is RecordPolicy.REPLACE:
        result[position] = copy.deepcopy(step)
    else:
        merged = dict(result[position]["params"].get("by_column", {}))
        for column, value in step["params"].get("by_column", {}).items():
            if value is None:
                merged.pop(column, None)
            else:
                merged[column] = value
        if not merged:
            result.pop(position)
        else:
            result[position] = {"action": step["action"], "params": {"by_column": merged}}

    return _float_pinned(result)


def _float_pinned(steps: list[Step]) -> list[Step]:
    pinned = [step for step in steps if get_spec(step["action"]).pin_rank is not None]
    unpinned = [step for step in steps if get_spec(step["action"]).pin_rank is None]
    pinned.sort(key=lambda step: get_spec(step["action"]).pin_rank)
    return pinned + unpinned


def remove_step(steps: list[Step], index: int) -> list[Step]:
    """Returns a new step list without the step at `index`.

    Raises:
        IndexError: if `index` isn't a valid position in `steps`.
    """
    if not 0 <= index < len(steps):
        raise IndexError(f"No cleaning step at position {index}.")
    return [copy.deepcopy(step) for position, step in enumerate(steps) if position != index]


def apply_steps_with_report(frame: pd.DataFrame, steps: list[Step]) -> tuple[pd.DataFrame, list[StepOutcome]]:
    """Runs every step in list order, returning the cleaned frame and a per-step report.

    A step whose target columns have all disappeared is skipped and flagged while the
    rest of the sequence continues — this is the tolerance Stage 8's replay requires
    (requirements 8.2), and it also happens here whenever an earlier step is undone.
    Data problems never raise; only an unregistered action does.

    Raises:
        InvalidStepError: if a step names an action that isn't registered.
    """
    result = frame
    report: list[StepOutcome] = []

    for index, step in enumerate(steps):
        spec = get_spec(step["action"])
        params = step.get("params", {})
        rows_before, columns_before = len(result), len(result.columns)

        required = spec.required_columns(params)
        if required and not any(column in result.columns for column in required):
            report.append(
                StepOutcome(
                    index=index,
                    action=step["action"],
                    status="skipped",
                    message=(
                        f"Skipped — none of its columns are in the table any more "
                        f"({', '.join(required)})."
                    ),
                    rows_before=rows_before,
                    rows_after=rows_before,
                    columns_before=columns_before,
                    columns_after=columns_before,
                )
            )
            continue

        try:
            result, warnings_out = spec.apply(result, params)
        except (ValueError, TypeError, KeyError, re.error) as error:
            logger.exception("Cleaning step '%s' failed at position %s.", step["action"], index)
            report.append(
                StepOutcome(
                    index=index,
                    action=step["action"],
                    status="skipped",
                    message=f"Skipped — this step couldn't be applied ({error}).",
                    rows_before=rows_before,
                    rows_after=rows_before,
                    columns_before=columns_before,
                    columns_after=columns_before,
                )
            )
            continue

        report.append(
            StepOutcome(
                index=index,
                action=step["action"],
                status="warned" if warnings_out else "applied",
                message=" ".join(warnings_out),
                rows_before=rows_before,
                rows_after=len(result),
                columns_before=columns_before,
                columns_after=len(result.columns),
            )
        )

    return result, report


def apply_steps(frame: pd.DataFrame, steps: list[Step]) -> pd.DataFrame:
    """Runs every step in order and returns just the cleaned frame."""
    cleaned, _ = apply_steps_with_report(frame, steps)
    return cleaned


def declared_column_types(steps: list[Step]) -> dict[str, str]:
    """The column types the recipe explicitly sets, by column name.

    Read back by the UI's column panel so it reports the type the user *chose*. pandas
    stores text, categorical and id identically, so detection alone would keep re-guessing
    `numeric` for an id column of plain digits however many times the user set it.

    A column renamed after its type was set falls out of this mapping and the panel goes
    back to detecting it — a weaker answer, never a wrong one.
    """
    declared: dict[str, str] = {}
    for step in steps:
        if step.get("action") != "set_column_types":
            continue
        for column, settings in step.get("params", {}).get("by_column", {}).items():
            if isinstance(settings, dict) and settings.get("target_type"):
                declared[str(column)] = settings["target_type"]
    return declared


def describe_step(step: Step) -> str:
    """Returns the human-readable log line for one step.

    The line is derived, never stored, so improving the wording later improves every
    saved Task's log retroactively.
    """
    try:
        spec = get_spec(step["action"])
        return spec.describe(step.get("params", {}))
    except InvalidStepError:
        logger.exception("Could not describe step %s.", step)
        return f"Unknown step: {step.get('action')}"


def describe_steps(steps: list[Step]) -> list[str]:
    """Returns one human-readable log line per step, in order."""
    return [describe_step(step) for step in steps]
