"""One form renderer for every operation, driven by the registry.

This module is the other half of requirement 4.2. `transform/registry.py` declares what an
operation asks for as a tuple of `ParamSpec`s; this walks that tuple and draws the right
widget for each `ParamKind`. Adding an operation in a later phase is therefore a registry
entry plus an executor, with **no change here at all** — which is what keeps phases 26 to 28
cheap.

It lives in `app_pages/` rather than in `transform/` because the package keeps exactly one
Streamlit-importing module (`transform/session.py`), the convention `cleaner` and `engine`
follow. The precedent for a shared, non-page view module is `app_pages/chart_controls.py`
and `app_pages/saved_picker.py`.

Every decision this makes — which columns to offer, what to pre-fill, how to coerce what
came back — is delegated to the pure `transform.params`, so the decisions are tested
without `AppTest` and only the drawing is left here.
"""

import logging

import streamlit as st

from transform.exceptions import TransformError
from transform.params import (
    InputRole,
    ParamKind,
    ParamSpec,
    collect_params,
    default_value,
    missing_required,
    option_columns,
    visible_params,
)
from transform.pipeline import TransformStep, make_step, suggested_output_name
from transform.registry import OperationSpec
from transform.workspace import NamedFrame, columns_of, frame_names

logger = logging.getLogger(__name__)

#: Every widget this module draws is keyed under this prefix, so `session.close_dialog`
#: can clear the whole half-filled form in one sweep.
FORM_PREFIX = "tf_form_"


def _key(name: str) -> str:
    return f"{FORM_PREFIX}{name}"


def _seed(name: str, value) -> None:
    """Pre-fills a widget's state before it is drawn, without overwriting a later edit.

    Streamlit widgets read their key from session state, so writing the default in first is
    how "edit the last step" arrives with every box already filled. `setdefault` rather than
    assignment, or the user's typing would be undone on every rerun.
    """
    st.session_state.setdefault(_key(name), value)


def _current(name: str, fallback=None):
    """What a box holds right now, for working out visibility and column options."""
    return st.session_state.get(_key(name), fallback)


#: Which operation the form currently on screen was built for. Switching the picker to a
#: different operation clears the old parameter boxes: two operations can both have a
#: parameter called `columns`, and carrying one's answer into the other would pre-fill it
#: with something the new step never asked for.
#:
#: The **table** pickers are deliberately kept. A user who has chosen their table and then
#: changes their mind about what to do with it has not changed their mind about the table.
_SEEDED_FOR_KEY = "tf_form_seeded_for"

#: The prefix the input-role pickers are keyed under, kept across an operation switch.
_INPUT_PREFIX = f"{FORM_PREFIX}input_"


def preset_source_table(table_name: str) -> None:
    """Empties the form and points its `source` picker at one table.

    For the shortcut buttons that open the dialog already aimed at a job — "Fix headers" on
    an uploaded table, and anything like it later. Everything is cleared first, including
    `_SEEDED_FOR_KEY`, so a form left over from a different operation cannot leak a parameter
    that happens to share a name; `seed_form` then fills in that operation's own defaults on
    the run that draws the dialog.
    """
    for key in [key for key in st.session_state if str(key).startswith(FORM_PREFIX)]:
        st.session_state.pop(key, None)
    st.session_state[f"{_INPUT_PREFIX}source"] = table_name


def seed_form(spec: OperationSpec, existing_step: TransformStep | None) -> None:
    """Fills the form's state from an existing step, or from the declared defaults.

    Seeds with `setdefault`, so it runs on every rerun while the dialog is open and still
    leaves the user's typing alone. The one thing it does overwrite is a form left over
    from a different operation — see `_SEEDED_FOR_KEY`.
    """
    if st.session_state.get(_SEEDED_FOR_KEY) != spec.operation:
        stale = [
            key
            for key in st.session_state
            if str(key).startswith(FORM_PREFIX) and not str(key).startswith(_INPUT_PREFIX)
        ]
        for key in stale:
            st.session_state.pop(key, None)
        st.session_state[_SEEDED_FOR_KEY] = spec.operation

    existing_inputs = (existing_step or {}).get("inputs", {})
    existing_params = (existing_step or {}).get("params", {})

    for role in spec.inputs:
        _seed(f"input_{role.name}", existing_inputs.get(role.name, [] if role.multi else None))
    for param in spec.params:
        _seed(param.name, default_value(param, existing_params or None))

    output = (existing_step or {}).get("output", {})
    _seed("output_mode", output.get("mode", spec.default_output))
    _seed("output_name", output.get("name", ""))


def render_step_form(
    spec: OperationSpec, workspace: dict[str, NamedFrame], existing_step: TransformStep | None = None
) -> tuple[TransformStep | None, list[str]]:
    """Draws one operation's whole form and reads it back.

    Returns:
        `(step, missing_labels)`. `step` is None while anything required is still empty,
        and `missing_labels` names those boxes so the caller can disable its Add button and
        say why — a greyed-out button beats an error the user has to trigger to discover.
    """
    available = frame_names(workspace)
    chosen_inputs = _render_inputs(spec, available)
    columns_by_role = _columns_by_role(spec, chosen_inputs, workspace)

    raw_values = _render_params(spec, columns_by_role)
    output_mode, output_name = _render_output(spec, chosen_inputs, workspace, existing_step)

    missing = missing_required(spec.params, raw_values)
    missing += [
        role.label
        for role in spec.inputs
        if not chosen_inputs.get(role.name)
        or (role.multi and len(chosen_inputs.get(role.name) or []) < 2)
    ]
    if output_mode == "new" and not str(output_name).strip():
        missing.append("New table name")

    if missing:
        return None, missing

    try:
        params = collect_params(spec.params, raw_values)
    except TransformError as error:
        logger.debug("Form for '%s' isn't complete yet: %s", spec.operation, error)
        return None, [str(error)]

    return make_step(spec.operation, chosen_inputs, params, output_mode, output_name), []


def _render_inputs(spec: OperationSpec, available: list[str]) -> dict:
    """One table picker per input role."""
    chosen: dict = {}
    columns = st.columns(len(spec.inputs)) if len(spec.inputs) > 1 else [st.container()]

    for position, role in enumerate(spec.inputs):
        with columns[min(position, len(columns) - 1)]:
            chosen[role.name] = _render_one_input(role, available)
    return chosen


def _render_one_input(role: InputRole, available: list[str]) -> str | list[str] | None:
    """One table picker, either a single choice or a multi-select for an appending step."""
    key = _key(f"input_{role.name}")
    if role.multi:
        held = [name for name in (st.session_state.get(key) or []) if name in available]
        st.session_state[key] = held
        return st.multiselect(
            role.label,
            options=available,
            key=key,
            help=role.help,
            placeholder="Choose two or more tables",
        )

    held = st.session_state.get(key)
    index = available.index(held) if held in available else None
    return st.selectbox(
        role.label,
        options=available,
        index=index,
        key=key,
        help=role.help,
        placeholder="Choose a table",
    )


def _columns_by_role(
    spec: OperationSpec, chosen_inputs: dict, workspace: dict[str, NamedFrame]
) -> dict[str, list[str]]:
    """Each role's chosen table's columns, so the column pickers can offer real names."""
    columns: dict[str, list[str]] = {}
    for role in spec.inputs:
        value = chosen_inputs.get(role.name)
        if role.multi:
            merged: list[str] = []
            for name in value or []:
                merged.extend(column for column in columns_of(workspace, str(name)) if column not in merged)
            columns[role.name] = merged
        else:
            columns[role.name] = columns_of(workspace, str(value or ""))
    return columns


def _render_params(spec: OperationSpec, columns_by_role: dict[str, list[str]]) -> dict:
    """Draws every visible parameter and returns what they hold.

    Visibility is re-checked between widgets rather than once up front, so a box that
    appears because of the box above it appears on the same run the user changes it.
    """
    values = {param.name: _current(param.name) for param in spec.params}

    for param in visible_params(spec.params, values):
        values[param.name] = _WIDGET_BUILDERS[param.kind](param, columns_by_role)
        # Re-read so the next parameter's `depends_on` sees this one's new value.
        values = {**values, param.name: values[param.name]}

    return values


# --------------------------------------------------------------------------------------
# One builder per ParamKind
# --------------------------------------------------------------------------------------


def _build_column(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    options = option_columns(param, columns_by_role)
    key = _key(param.name)
    held = st.session_state.get(key)
    index = options.index(held) if held in options else None
    return st.selectbox(
        param.label,
        options=options,
        index=index,
        key=key,
        help=param.help,
        placeholder="Choose a column" if options else "Choose a table first",
        disabled=not options,
    )


def _build_columns(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    options = option_columns(param, columns_by_role)
    key = _key(param.name)
    # Drop anything the chosen table no longer has, or Streamlit refuses the default.
    st.session_state[key] = [name for name in (st.session_state.get(key) or []) if name in options]
    return st.multiselect(
        param.label,
        options=options,
        key=key,
        help=param.help,
        placeholder="Choose column(s)" if options else "Choose a table first",
        disabled=not options,
    )


def _build_text(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    return st.text_input(
        param.label,
        key=_key(param.name),
        help=param.help,
        placeholder=param.placeholder or None,
    )


def _build_expression(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    """A formula box, with the available column names listed underneath.

    The caption is not decoration: the formula has to name columns exactly, and a user who
    cannot see the spelling will guess it wrong. It is also where the square-bracket rule
    for names with spaces gets explained at the moment it is needed.
    """
    value = st.text_input(
        param.label,
        key=_key(param.name),
        help=param.help,
        placeholder=param.placeholder or None,
    )
    options = option_columns(param, columns_by_role)
    if options:
        listed = ", ".join(f"`{column}`" for column in options[:20])
        more = " ..." if len(options) > 20 else ""
        st.caption(f":red[Columns you can use: {listed}{more}]")
    return value


def _build_number(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    """A decimal box. The value comes from the widget's key, never from a `value=`
    argument — passing both is what makes Streamlit complain that a widget was given a
    default and a key at once."""
    return st.number_input(
        param.label,
        key=_key(param.name),
        help=param.help,
        min_value=param.min_value,
        max_value=param.max_value,
        placeholder="Leave empty to skip" if not param.required else None,
    )


def _build_integer(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    minimum = int(param.min_value) if param.min_value is not None else None
    maximum = int(param.max_value) if param.max_value is not None else None
    return st.number_input(
        param.label,
        key=_key(param.name),
        help=param.help,
        min_value=minimum,
        max_value=maximum,
        step=1,
        placeholder="Leave empty to skip" if not param.required else None,
    )


def _build_choice(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    options = list(param.choices)
    key = _key(param.name)
    held = st.session_state.get(key)
    index = options.index(held) if held in options else 0
    return st.selectbox(param.label, options=options, index=index, key=key, help=param.help)


def _build_choices(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    return st.multiselect(param.label, options=list(param.choices), key=_key(param.name), help=param.help)


def _build_boolean(param: ParamSpec, columns_by_role: dict[str, list[str]]):
    return st.toggle(param.label, key=_key(param.name), help=param.help)


#: Kinds the registry uses today. The composites declared in `ParamKind` but not listed here
#: have no operation asking for them yet; adding one means adding its builder, which is the
#: one deliberate exception to "a new operation needs no UI code".
_WIDGET_BUILDERS = {
    ParamKind.COLUMN: _build_column,
    ParamKind.COLUMNS: _build_columns,
    ParamKind.TEXT: _build_text,
    ParamKind.EXPRESSION: _build_expression,
    ParamKind.NUMBER: _build_number,
    ParamKind.INTEGER: _build_integer,
    ParamKind.CHOICE: _build_choice,
    ParamKind.CHOICES: _build_choices,
    ParamKind.BOOLEAN: _build_boolean,
}


# --------------------------------------------------------------------------------------
# Where the answer goes
# --------------------------------------------------------------------------------------


def _render_output(
    spec: OperationSpec,
    chosen_inputs: dict,
    workspace: dict[str, NamedFrame],
    existing_step: TransformStep | None,
) -> tuple[str, str]:
    """The "update this table / save as a new one" block."""
    st.divider()

    primary = chosen_inputs.get(spec.primary_role)
    if isinstance(primary, list):
        primary = primary[0] if primary else ""
    source_label = primary or "the table"

    mode = st.radio(
        "Where should the answer go?",
        options=["in_place", "new"],index=0,
        format_func=lambda value: (
            f"Update **{source_label}**" if value == "in_place" else "Save as a new table"
        ),
        key=_key("output_mode"),
        horizontal=True,
        help=(
            "Updating replaces the table, so later steps see the new version and the old one "
            "is no longer available. Saving as a new table keeps both."
        ),
    )

    if mode == "in_place":
        return "in_place", str(primary or "")

    name_key = _key("output_name")
    if not str(st.session_state.get(name_key) or "").strip() and primary:
        # Pre-fill a name that is already free, so the duplicate-name refusal stays a
        # backstop rather than something met on the ordinary path.
        st.session_state[name_key] = suggested_output_name(spec, chosen_inputs, workspace)

    name = st.text_input(
        "New table name",
        key=name_key,
        help="What the new table is called. It also becomes its sheet name in the download, so it must be different from every other table.",
    )
    return "new", name
