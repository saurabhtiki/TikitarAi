"""What one operation's inputs and parameters look like, and how a filled-in form is read back.

This module is what makes the requirement in section 4.2 of the requirements document
true — "adding a new operation should require only a new registry entry plus a small
execution function, not new UI code". An `OperationSpec` declares its parameters as
`ParamSpec` objects; `app_pages/transform_form.py` loops over them and builds a widget per
`ParamKind`. Nothing here imports Streamlit, so every decision the renderer makes —
which options to offer, what to pre-fill, how to coerce what came back, whether a
parameter is visible at all — is tested without `AppTest`.

The split is deliberate: the renderer owns *drawing*, this module owns *deciding*.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from transform.exceptions import InvalidStepParamsError

logger = logging.getLogger(__name__)


class ParamKind(StrEnum):
    """The widget families the form renderer knows how to draw.

    Kept small on purpose. A new operation should almost always be expressible in these;
    a genuinely new kind is a deliberate decision to teach the renderer something, not an
    accident of one operation's needs.
    """

    COLUMN = "column"
    COLUMNS = "columns"
    TEXT = "text"
    NUMBER = "number"
    INTEGER = "integer"
    CHOICE = "choice"
    CHOICES = "choices"
    BOOLEAN = "boolean"
    # Composites. Each maps to one purpose-built widget in the renderer, because the
    # alternative — three coupled simple widgets the user has to keep in sync — is how
    # forms become unusable.
    COLUMN_AGGREGATIONS = "column_aggregations"
    COLUMN_ORDER = "column_order"
    EXPRESSION = "expression"
    BINS = "bins"


#: Kinds whose value is a list. Used by `collect_params` to tell "the user chose nothing"
#: from "the user hasn't got to this box yet", which matter differently for `required`.
_LIST_KINDS = frozenset(
    {ParamKind.COLUMNS, ParamKind.CHOICES, ParamKind.COLUMN_ORDER, ParamKind.BINS}
)

#: Kinds that name columns of an input table, and so need `from_role` to point at a real
#: input role. `test_transform_registry` asserts exactly this across the whole registry.
COLUMN_BACKED_KINDS = frozenset(
    {
        ParamKind.COLUMN,
        ParamKind.COLUMNS,
        ParamKind.COLUMN_AGGREGATIONS,
        ParamKind.COLUMN_ORDER,
        ParamKind.EXPRESSION,
    }
)


@dataclass(frozen=True)
class InputRole:
    """One table an operation reads.

    Named rather than positional. `merge` has a left and a right and they are not
    interchangeable; with a positional list, `step["inputs"][1]` would be the only way to
    say "the right-hand table", which is unreadable in a log and silently wrong the day
    an operation gains a third input.
    """

    name: str
    label: str
    help: str
    multi: bool = False


@dataclass(frozen=True)
class ParamSpec:
    """One box in an operation's form.

    Attributes:
        name: the key this value is stored under in `step["params"]`.
        label: what the box is called on screen.
        kind: which widget family draws it.
        help: the tooltip. Required and non-empty — the project convention is that every
            widget carries one, and making it a positional field means a new operation
            cannot forget it rather than being reminded in review.
        required: whether the step can be added without a value.
        default: what an empty form starts with.
        choices: the fixed options, for CHOICE and CHOICES.
        from_role: which input table supplies the column list, for the column-backed kinds.
        min_value / max_value: numeric bounds, passed straight to the widget.
        placeholder: greyed-out example text.
        depends_on / depends_value: render this box only while another parameter holds a
            particular value — how one operation offers "number of decimals" only after
            "round" has been picked, without needing two near-identical operations.
    """

    name: str
    label: str
    kind: ParamKind
    help: str
    required: bool = True
    default: object = None
    choices: tuple[str, ...] = ()
    from_role: str = "source"
    min_value: float | None = None
    max_value: float | None = None
    placeholder: str = ""
    depends_on: str | None = None
    depends_value: object = None

    def __post_init__(self) -> None:
        """Rejects a malformed spec at import time.

        A registry entry is written once and read forever, so the cheapest place to catch
        a missing tooltip or an empty choice list is the moment the module loads — not a
        blank dropdown in front of a user.

        Raises:
            ValueError: if the spec could not render a usable widget.
        """
        if not self.name.strip():
            raise ValueError("A parameter needs a name.")
        if not self.label.strip():
            raise ValueError(f"Parameter '{self.name}' needs a label.")
        if not self.help.strip():
            raise ValueError(f"Parameter '{self.name}' needs a help tooltip.")
        if self.kind in (ParamKind.CHOICE, ParamKind.CHOICES) and not self.choices:
            raise ValueError(f"Parameter '{self.name}' is a choice but lists no choices.")


def is_visible(spec: ParamSpec, values: dict) -> bool:
    """Whether `spec`'s box should be drawn, given what the form holds so far.

    A spec with no dependency is always visible. One with a dependency is visible only
    while that other parameter equals `depends_value` — compared by equality rather than
    truthiness so that depending on `False` works.
    """
    if spec.depends_on is None:
        return True
    return values.get(spec.depends_on) == spec.depends_value


def visible_params(specs: Sequence[ParamSpec], values: dict) -> list[ParamSpec]:
    """The subset of `specs` to draw, in declared order."""
    return [spec for spec in specs if is_visible(spec, values)]


def option_columns(spec: ParamSpec, columns_by_role: dict[str, list[str]]) -> list[str]:
    """The column names a column-backed box offers.

    Args:
        spec: the parameter being drawn.
        columns_by_role: role name -> that input table's columns, as the caller read them
            off the workspace.

    Returns an empty list — never raises — when the role has no table chosen yet. That is
    the ordinary state of a half-filled form: the user picks the table first and the
    column list fills in underneath.
    """
    if spec.kind not in COLUMN_BACKED_KINDS:
        return []
    return list(columns_by_role.get(spec.from_role) or [])


def default_value(spec: ParamSpec, existing: dict | None = None) -> object:
    """What a box starts out holding.

    `existing` is a previously saved `params` dict. When one is given its value wins, which
    is the whole of what makes "edit the last step" work: the same form, re-opened with
    every box already filled.

    A list-valued kind always falls back to a new empty list rather than a shared one, so
    two forms drawn in the same run cannot append into each other's default.
    """
    if existing is not None and spec.name in existing:
        return existing[spec.name]
    if spec.default is not None:
        return spec.default
    if spec.kind in _LIST_KINDS:
        return []
    if spec.kind is ParamKind.BOOLEAN:
        return False
    if spec.kind is ParamKind.CHOICE:
        return spec.choices[0] if spec.choices else None
    return None


def _coerce(spec: ParamSpec, value: object) -> object:
    """Turns one widget's return value into the JSON-safe form the step stores.

    Numbers arrive as text from a text box and as `numpy` scalars from a number box;
    neither survives `json.dumps` unchanged, and a pipeline that cannot be serialised is
    useless to phase 26. Coercing here means no executor has to guess.

    Raises:
        InvalidStepParamsError: if a numeric box holds something that isn't a number.
    """
    if value is None:
        return None
    if spec.kind is ParamKind.INTEGER:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as error:
            raise InvalidStepParamsError(
                f"'{spec.label}' needs a whole number, but got '{value}'."
            ) from error
    if spec.kind is ParamKind.NUMBER:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError) as error:
            raise InvalidStepParamsError(
                f"'{spec.label}' needs a number, but got '{value}'."
            ) from error
    if spec.kind is ParamKind.BOOLEAN:
        return bool(value)
    if spec.kind in _LIST_KINDS:
        return list(value)
    if spec.kind is ParamKind.TEXT:
        return str(value)
    return value


def _is_blank(spec: ParamSpec, value: object) -> bool:
    """Whether a required box counts as unfilled."""
    if value is None:
        return True
    if spec.kind in _LIST_KINDS or spec.kind is ParamKind.COLUMN_AGGREGATIONS:
        return len(value) == 0
    if spec.kind in (ParamKind.TEXT, ParamKind.EXPRESSION, ParamKind.COLUMN, ParamKind.CHOICE):
        return not str(value).strip()
    return False


def collect_params(specs: Sequence[ParamSpec], raw: dict) -> dict:
    """Reads a filled-in form back into the dict a step stores.

    Only visible parameters are collected: a box hidden by `depends_on` may still hold a
    stale value from before the user changed the box it depends on, and storing that would
    make two identical-looking steps compare unequal.

    Raises:
        InvalidStepParamsError: if a required visible box is empty, or a numeric box holds
            something that isn't a number. Reported one box at a time, naming the label the
            user can actually see.
    """
    collected: dict = {}
    for spec in visible_params(specs, raw):
        value = _coerce(spec, raw.get(spec.name))
        if spec.required and _is_blank(spec, value):
            raise InvalidStepParamsError(f"'{spec.label}' is needed to add this step.")
        if value is not None:
            collected[spec.name] = value
    return collected


def missing_required(specs: Sequence[ParamSpec], values: dict) -> list[str]:
    """The labels of the required visible boxes that are still empty.

    The non-raising counterpart to `collect_params`, for greying out the Add button while
    a form is half-filled — a disabled button is a better answer than an error the user
    has to trigger to discover.
    """
    return [
        spec.label
        for spec in visible_params(specs, values)
        if spec.required and _is_blank(spec, values.get(spec.name))
    ]
