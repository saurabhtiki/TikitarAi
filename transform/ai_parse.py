"""Plain English in, catalog steps out (requirements document section 5, path 2).

The user types *"add a bonus column that is salary times 12%"* and gets back the same
structured step the picker would have built. Three rules hold this together, and all
three come from section 2 of the requirements document:

1. **The catalog is the only vocabulary.** The model is handed every operation and its
   parameters and told to choose among them. It never writes pandas, and an operation
   name it invents is dropped here rather than reaching the pipeline.
2. **Only real tables and columns.** The current workspace goes into the prompt, and
   every parsed step is run through the registry's own validator against that workspace
   before it is offered to the user - the same check `_commit_step` makes for a step
   built by hand.
3. **Nothing is guessed when nothing fits.** A request the catalog cannot express comes
   back as a `clarification` sentence, not as the nearest wrong step.

AI is used *here only* - at definition time. A saved pipeline replays with no model call,
which is why this module returns ordinary `TransformStep` dicts and stops there.

Like `llm.suggestions`, `parse_instruction` never raises: a model that is down or talking
nonsense is a degraded result the user can work around by using the picker, not a broken
screen.
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field

from llm.client import LLMConnectionError, run_structured
from transform.exceptions import TransformError
from transform.params import ParamKind, collect_params
from transform.pipeline import (
    TransformStep,
    apply_steps_with_report,
    make_step,
    suggested_output_name,
    validate_step,
)
from transform.registry import OPERATION_REGISTRY, get_operation
from transform.workspace import NamedFrame, columns_of, frame_names, get_dataframe, name_key

logger = logging.getLogger(__name__)

#: How many of a table's columns go into the prompt. A 300-column upload would otherwise
#: crowd out the catalog itself, and the columns an instruction names are almost always
#: near the front.
PROMPT_COLUMN_LIMIT = 60

_INSTRUCTIONS = """\
You turn one plain-English data instruction into steps from a fixed catalog.

Hard rules:
- Use ONLY operation names from the catalog you are given. Never invent one.
- Use ONLY table and column names from the schema you are given, spelled exactly as
  shown. The user may abbreviate ("Qty"); you must write the real column ("Quantity").
  Never invent a column, and never guess at one that is merely similar.
- Fill only the parameters the chosen operation lists. Use its exact parameter names.
- `inputs` and `params` are both lists of {"name": ..., "value": ...} pairs, and every
  value is written as text. A parameter that takes several columns is one pair whose
  value is comma separated: {"name": "group_by", "value": "Region, Product"}. A
  true/false parameter is the text "true" or "false". A number is the digits alone.
- If the request needs several catalog operations, return them as an ordered list. A
  later step may read a table an earlier step creates: use that step's output_name.
- output_mode is "new" when the step should make a separate table (grouping, joining,
  appending, pivoting) and "in_place" when it changes the table it read. Give an
  output_name only for "new".
- add_calculated_column's formula can itself choose between two NUMBERS with a test:
  "answer if test else other answer", for example "1 if Amount > 1000 else 0" or
  "Price * 0.9 if Quantity > 100 else Price". Tests use > < >= <= = != only. When BOTH
  answers are plain numbers, prefer this single step over add_conditional_column. When
  either answer is TEXT ("high", "ok"), a formula cannot produce it - use
  add_conditional_column instead.
- add_conditional_column always writes a NEW column - it never overwrites one that
  already exists, even if you give it that column's own name. So "change Remarks to xxx
  if Mrp is under 50" (update an EXISTING column when a condition holds) is three steps,
  not one: (1) add_conditional_column into a throwaway name such as "Remarks_updated"; if
  the user did not say what the other rows should become, set result_if_false to the
  target column's own name (as a formula) so those rows keep what they already had -
  never leave it blank and never invent a value the user didn't ask for; (2)
  drop_columns the original column; (3) rename_column the throwaway name back to the
  original name. Do this whenever the target column already exists in the schema you
  were given; when it doesn't exist yet, a plain add_conditional_column is enough.
- If no combination of catalog operations can do what was asked, return an EMPTY steps
  list and say why in one plain sentence in `clarification`. Never return a step that is
  merely close - a wrong step is worse than an honest no.\
"""

#: Written as text in a parameter value to mean `True`. Anything else is `False`, so a
#: model that answers "no" or leaves the value blank gets the safe reading.
_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "on"})


class ParsedValue(BaseModel):
    """One named value: an input role's table, or a parameter's setting.

    A list of these rather than a dict, and text rather than a typed value, because a
    provider's strict structured-output mode - which every cloud profile asks for - does
    not accept an object whose keys are not declared up front. A dict of twenty-nine
    operations' worth of parameters cannot declare its keys, so it came back empty from
    every request; a list of name/value pairs is an ordinary array the strict schema can
    describe, and it survives.

    Text rather than typed for the same reason: a value that could be a number, a
    true/false or a list of columns is a union, and unions are the other thing strict
    schemas handle badly. `_typed_params` turns the text back into the shape the
    operation's own `ParamSpec` asks for, which is where the real checking already lived.
    """

    name: str = Field(..., description="The input role or parameter name, exactly as listed")
    value: str = Field(
        default="", description="Its value as text; comma separated when several are wanted"
    )


class ParsedStep(BaseModel):
    """One step as the model proposed it, before the registry has had a look at it."""

    operation: str = Field(..., description="An operation key copied exactly from the catalog")
    inputs: list[ParsedValue] = Field(
        default_factory=list, description="Input role name and the table it reads"
    )
    params: list[ParsedValue] = Field(
        default_factory=list, description="The operation's own parameters, by name"
    )
    output_mode: Literal["in_place", "new"] = Field(
        default="in_place", description="Change the table it read, or make a new one"
    )
    output_name: str = Field(
        default="", description="Name for the new table, only when output_mode is 'new'"
    )


class ParsedSteps(BaseModel):
    """What one parse returns: the steps, or an honest reason there are none."""

    steps: list[ParsedStep] = Field(default_factory=list)
    clarification: str = Field(
        default="", description="Why the request can't be done with the catalog, if it can't"
    )


def describe_catalog_for_prompt() -> str:
    """Every operation, its inputs and its parameters, as prompt text.

    Rendered from `OPERATION_REGISTRY` rather than written out by hand, so an operation
    added in a later phase becomes available to plain English the moment it is registered
    - the same "a registry entry and nothing else" promise section 4.2 makes for the form.
    """
    lines: list[str] = []
    for spec in OPERATION_REGISTRY.values():
        lines.append(f"- {spec.operation} ({spec.label}): {spec.summary}")
        roles = ", ".join(
            f"{role.name}{' (list of tables)' if role.multi else ''}" for role in spec.inputs
        )
        lines.append(f"    inputs: {roles or '(none)'}")
        for param in spec.params:
            bits = [f"{param.name}: {param.kind.value}"]
            if not param.required:
                bits.append("optional")
            if param.choices:
                bits.append("one of " + "/".join(param.choices))
            if param.depends_on is not None:
                bits.append(f"only when {param.depends_on} is {param.depends_value}")
            lines.append("    param " + ", ".join(bits))
        lines.append(f"    usually outputs: {spec.default_output}")
    return "\n".join(lines)


def _type_word(dtype) -> str:
    """`number`, `date`, `true/false` or `text` for one column's dtype.

    A step that sums a column or rounds it only makes sense on a number, and the type
    beside the name is what lets the model tell an amount from an invoice number that
    merely looks like one.
    """
    kind = getattr(dtype, "kind", "O")
    if kind in "iuf":
        return "number"
    if kind == "b":
        return "true/false"
    if kind == "M":
        return "date"
    return "text"


def describe_workspace_for_prompt(workspace: dict[str, NamedFrame]) -> str:
    """The tables the user actually has, their columns, and each column's type.

    The column list is what turns the user's shorthand into the real name: they type
    "Qty" and the model can see the column is called "Quantity". Without it the model
    would be guessing, which rule 2 of this module forbids.
    """
    lines: list[str] = []
    for name in frame_names(workspace):
        columns = columns_of(workspace, name)
        types = getattr(get_dataframe(workspace, name), "dtypes", {})
        described = [
            f"{column} ({_type_word(types[column])})" if column in types else str(column)
            for column in columns[:PROMPT_COLUMN_LIMIT]
        ]
        shown = ", ".join(described)
        if len(columns) > PROMPT_COLUMN_LIMIT:
            shown += f", ... ({len(columns) - PROMPT_COLUMN_LIMIT} more)"
        lines.append(f"- {name}: {shown or '(no columns)'}")
    return "\n".join(lines) or "(no tables loaded)"


def build_prompt(instruction: str, workspace: dict[str, NamedFrame]) -> str:
    """The whole ask: the schema, the catalog, then the sentence to turn into steps."""
    return (
        "Tables available, with their columns:\n"
        f"{describe_workspace_for_prompt(workspace)}\n\n"
        "Operation catalog:\n"
        f"{describe_catalog_for_prompt()}\n\n"
        "Instruction to carry out:\n"
        f"{instruction.strip()}"
    )


def _resolve_table(name: object, known: dict[str, str]) -> str:
    """Maps a table name the model wrote onto the one the workspace knows.

    Case and surrounding spaces are forgiven - the model reading `Sales` off the prompt
    and writing `sales` back is not a mistake worth refusing a step over. Anything else is
    passed through unchanged so that `validate_step` reports it by the name the model
    used, which is what makes the warning readable.
    """
    text = str(name or "").strip()
    return known.get(name_key(text), text)


def _split(value: str) -> list[str]:
    """`"Region, Product"` -> `["Region", "Product"]`, dropping the empties."""
    return [piece.strip() for piece in str(value or "").split(",") if piece.strip()]


def _resolve_inputs(parsed: ParsedStep, spec, known: dict[str, str]) -> dict:
    """Every input role's table name, mapped onto the workspace's spelling.

    A role that takes several tables reads its comma-separated list apart; every other
    role is one name. Which is which comes from the operation's own `InputRole.multi`, so
    the model never has to signal it.
    """
    multi_roles = {role.name for role in spec.inputs if role.multi}
    resolved: dict = {}
    for item in parsed.inputs:
        role = item.name.strip()
        if not role:
            continue
        if role in multi_roles:
            resolved[role] = [_resolve_table(piece, known) for piece in _split(item.value)]
        else:
            resolved[role] = _resolve_table(item.value, known)
    return resolved


def _resolve_column(name: str, known: dict[str, str]) -> str:
    """Maps a column name the model wrote onto the table's own spelling.

    Case and spacing only - `quantity` finds `Quantity`, and ` Order ID` finds `Order ID`.
    Nothing fuzzier: rule 2 of this module says a merely *similar* name is a guess, and a
    step that quietly totals the wrong column is exactly the failure that rule exists to
    prevent. An unmatched name is passed through so `validate_step` can name it in the
    warning the user reads.
    """
    text = str(name or "").strip()
    return known.get(text.casefold(), text)


def _typed_params(spec, raw: dict[str, str], columns_by_role: dict[str, list[str]]) -> dict:
    """Turns the model's text values into the shapes each `ParamSpec` expects.

    Everything arrives as text because a strict schema cannot describe a value that is
    sometimes a number, sometimes a list and sometimes a true/false. This is the one place
    that is undone, and it is done from the operation's own declared `ParamKind` rather
    than by guessing at the text - so a column called "12" stays a column name and a
    `decimals` of "2" becomes a number.
    """
    typed: dict = {}
    for param in spec.params:
        if param.name not in raw:
            continue
        text = raw[param.name]
        known_columns = {
            column.casefold(): column for column in (columns_by_role.get(param.from_role) or [])
        }

        if param.kind is ParamKind.BOOLEAN:
            typed[param.name] = str(text).strip().casefold() in _TRUE_WORDS
        elif param.kind is ParamKind.COLUMNS:
            typed[param.name] = [_resolve_column(piece, known_columns) for piece in _split(text)]
        elif param.kind is ParamKind.CHOICES:
            typed[param.name] = _split(text)
        elif param.kind is ParamKind.COLUMN:
            typed[param.name] = _resolve_column(text, known_columns)
        else:
            # TEXT, EXPRESSION, CHOICE, NUMBER and INTEGER all want the text as written.
            # `collect_params` does the numeric conversion, and raises its own readable
            # message when the model wrote something that is not a number.
            typed[param.name] = text
    return typed


def _output_name(parsed: ParsedStep, spec, inputs: dict, workspace: dict[str, NamedFrame]) -> str:
    """Where the step's answer goes.

    An in-place step's output name is its own source table's - the same rule the form
    follows. A new table falls back to the registry's suggested name when the model left
    it blank, which is better than refusing an otherwise good step over a missing label.
    """
    if parsed.output_mode == "new":
        if parsed.output_name and parsed.output_name.strip():
            return parsed.output_name.strip()
        return suggested_output_name(spec, inputs, workspace)

    primary = inputs.get(spec.primary_role, "")
    if isinstance(primary, list):
        primary = primary[0] if primary else ""
    return str(primary or "")


def _build_one(
    parsed: ParsedStep, workspace: dict[str, NamedFrame]
) -> tuple[TransformStep | None, str | None]:
    """Turns one proposed step into a real, validated one, or says why it can't be.

    Returns `(step, warning)` with exactly one of the two set. Everything that can go
    wrong here is the model's fault rather than the user's, so each failure becomes a
    sentence naming the step, not an exception the dialog has to catch.
    """
    try:
        spec = get_operation(parsed.operation)
    except TransformError as error:
        logger.info("AI proposed an unknown operation %s: %s", parsed.operation, error)
        return None, f"Skipped a step: {error}"

    known = {name_key(name): name for name in frame_names(workspace)}
    inputs = _resolve_inputs(parsed, spec, known)
    columns_by_role = {
        role.name: columns_of(workspace, _first_table(inputs.get(role.name)))
        for role in spec.inputs
    }
    raw = {item.name.strip(): item.value for item in parsed.params if item.name.strip()}

    try:
        params = collect_params(spec.params, _typed_params(spec, raw, columns_by_role))
        step = make_step(
            parsed.operation,
            inputs,
            params,
            parsed.output_mode,
            _output_name(parsed, spec, inputs, workspace),
        )
        validate_step(step, workspace)
    except TransformError as error:
        logger.info("AI step %s refused: %s", parsed.operation, error)
        return None, f"Skipped '{spec.label}': {error}"
    except (TypeError, ValueError) as error:
        # A value the model wrote in a shape no `ParamKind` anticipated - a nested object
        # where text was asked for, say. `parse_instruction` promises never to raise, and
        # the dialog that calls it has no error path, so a malformed answer has to end up
        # as a warning here rather than as a traceback on the page.
        logger.info("AI step %s was malformed: %s", parsed.operation, error)
        return None, f"Skipped '{spec.label}': the answer wasn't in a usable shape."

    return step, None


def _first_table(chosen) -> str:
    """The one table a role points at, or the first of several."""
    if isinstance(chosen, list):
        return str(chosen[0]) if chosen else ""
    return str(chosen or "")


def parse_instruction(
    profile: dict,
    instruction: str,
    workspace: dict[str, NamedFrame],
    *,
    key_path=None,
) -> tuple[list[TransformStep], list[str], str | None]:
    """Asks the Light Model to turn `instruction` into catalog steps.

    Each proposed step is validated against the workspace *as it would be when that step
    runs* - an earlier step's new table is put into the running workspace before the next
    step is checked, so "group it, then filter the summary" validates the filter against
    the summary rather than against a table that does not exist yet.

    Returns:
        `(steps, warnings, clarification)`. The warnings name the proposed steps that were
        dropped and why, so the dialog can say "2 of 3 steps understood" instead of
        quietly returning fewer than the sentence asked for.

    Never raises: a model that is unreachable or talking nonsense comes back as an empty
    list and one warning. The structured picker is always there as the fallback.
    """
    if not instruction.strip():
        return [], ["Type what you want to do first."], None

    try:
        response = run_structured(
            profile,
            build_prompt(instruction, workspace),
            ParsedSteps,
            instructions=_INSTRUCTIONS,
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Plain-English parse failed: %s", error)
        return [], [f"We couldn't read that instruction: {error}"], None

    steps: list[TransformStep] = []
    warnings: list[str] = []
    running = dict(workspace)

    for parsed in response.steps:
        step, warning = _build_one(parsed, running)
        if step is None:
            warnings.append(warning or "A step couldn't be understood.")
            continue

        # Run it now so the next step sees the table this one makes. A step that
        # validates but falls over on the real data is caught here rather than after the
        # user has pressed Add.
        after, report = apply_steps_with_report(running, [step])
        if report and report[0].status == "failed":
            logger.info("AI step %s failed on the data: %s", step["operation"], report[0].message)
            warnings.append(f"Skipped '{report[0].label}': {report[0].message}")
            continue

        steps.append(step)
        running = after

    return steps, warnings, response.clarification.strip() or None
