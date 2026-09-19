"""The clean-up operations, every one of them delegating to `cleaner.steps`.

Phase 25 shipped this page with only the two clean-up actions a join creates work for
(`fill_missing`, `drop_duplicates`, both in `ops_rows`). The rest stayed in Data Cleaner,
which meant a file with three junk rows above its headers had to be cleaned on the other
page, downloaded and uploaded again. These nine close that gap.

Not one of them reimplements anything. `cleaner.steps` executors already take
`(frame, params) -> (new_frame, warnings)`, which is this package's contract minus the role
lookup, so each function here is a rename of parameters and a call. The behaviour, and the
wording of every warning, therefore stays identical across the two pages — which matters
more than it sounds, because a user who has learned what "Skip rows" does on one page must
not find it doing something subtly different on the other.

The simplification these make, consistently: where the cleaner takes a *per-column* setting
(`change_case` can upper-case one column and Title-case another), this page takes a set of
columns and one setting for all of them. A per-column mapping needs a bespoke grid widget;
a set of columns plus a dropdown is two ordinary widgets the generic form renderer already
draws. Two different settings is two steps. `fill_missing` made the same trade in phase 25.
"""

import logging
import re

import pandas as pd

from cleaner.steps import (
    DEFAULT_KEEP_PATTERN,
    DEFAULT_VALUE_NAME,
    DEFAULT_VARIABLE_NAME,
    STEP_REGISTRY,
)
from transform.exceptions import InvalidStepParamsError
from transform.ops_common import source_frame

logger = logging.getLogger(__name__)

#: How the letter-case choices read on screen, mapped to what `cleaner.steps` calls them.
#: Shown as the result rather than the verb ("UPPERCASE", not "upper") so the dropdown is
#: its own example.
CASE_CHOICES = {"UPPERCASE": "upper", "lowercase": "lower", "Title Case": "title"}

#: Which way `round_numbers` goes. "nearest" is the one people mean by "round".
ROUNDING_CHOICES = {
    "the nearest value": "nearest",
    "up": "up",
    "down": "down",
}

#: The character that separates whole numbers from decimals. Both are in real use; a file
#: exported from a European system writes 1.234,56 where an Indian one writes 1,234.56.
DECIMAL_SEPARATORS = (".", ",")

MAX_ROUNDING_DECIMALS = 10


def _ensure_new_frame(result: pd.DataFrame, original: pd.DataFrame) -> pd.DataFrame:
    """Guarantees the executor contract's "always a new frame".

    Two of the cleaner executors return their input unchanged in the do-nothing case (an
    unpivot with no columns left to stack, for instance). That is harmless there, because
    the cleaner holds one frame; here the returned frame is stored in the workspace under a
    possibly different name, and sharing an object between two named tables would make an
    edit to one of them appear in the other.
    """
    return result.copy() if result is original else result


# --------------------------------------------------------------------------------------
# skip_rows
# --------------------------------------------------------------------------------------


def apply_skip_rows(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Drops rows off the top or bottom, optionally promoting the next row to headers.

    The one step that usually has to come first. A file with a title and a blank line above
    its real headers is read with those as the header row, so every column is called
    `Unnamed: 1` and every dropdown on this page is unusable until this step has run.
    """
    frame = source_frame(frames_by_role)
    result, warnings_out = STEP_REGISTRY["skip_rows"].apply(
        frame,
        {
            "top": int(params.get("top", 0) or 0),
            "bottom": int(params.get("bottom", 0) or 0),
            "promote_header": bool(params.get("promote_header", False)),
        },
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_skip_rows(step: dict) -> str:
    params = step.get("params", {})
    parts = []
    if params.get("top"):
        parts.append(f"{params['top']} row(s) from the top")
    if params.get("bottom"):
        parts.append(f"{params['bottom']} row(s) from the bottom")
    skipped = f"Skipped {' and '.join(parts)}" if parts else "Kept every row"
    if params.get("promote_header"):
        return f"{skipped}, then used the next row as the column headers"
    return skipped


def validate_skip_rows(columns_by_role: dict, params: dict) -> None:
    top = int(params.get("top", 0) or 0)
    bottom = int(params.get("bottom", 0) or 0)
    if top < 0 or bottom < 0:
        raise InvalidStepParamsError("The number of rows to skip can't be negative.")
    if not top and not bottom and not params.get("promote_header"):
        raise InvalidStepParamsError(
            "This step would do nothing. Skip at least one row, or tick the header box."
        )


def required_skip_rows(params: dict) -> dict[str, list[str]]:
    # Deliberately empty: this step runs *before* the columns have their real names, so
    # naming any of them as required would be checking against the junk header row.
    return {"source": []}


# --------------------------------------------------------------------------------------
# remove_empty_rows
# --------------------------------------------------------------------------------------


def apply_remove_empty_rows(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Drops rows that are blank, across every column or only the chosen ones."""
    frame = source_frame(frames_by_role)
    before = len(frame)
    result, warnings_out = STEP_REGISTRY["remove_empty_rows"].apply(
        frame,
        {
            "columns": [str(column) for column in params.get("columns", [])],
            "blank_strings_count_as_empty": bool(
                params.get("blank_strings_count_as_empty", True)
            ),
        },
    )
    removed = before - len(result)
    if removed:
        warnings_out = list(warnings_out) + [f"Removed {removed:,} empty row(s)."]
    return _ensure_new_frame(result, frame), warnings_out


def describe_remove_empty_rows(step: dict) -> str:
    columns = step.get("params", {}).get("columns") or []
    scope = f" where {', '.join(columns)} are blank" if columns else " where every column is blank"
    return f"Removed rows{scope}"


def validate_remove_empty_rows(columns_by_role: dict, params: dict) -> None:
    return None


def required_remove_empty_rows(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# fix_numeric_text
# --------------------------------------------------------------------------------------


def apply_fix_numeric_text(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Stores a text column as real numbers, reading 1,250.00 and (500) correctly.

    Much of this page reads those shapes on the fly when it totals or compares a column, so
    this step is for when the column itself has to *be* a number — before rounding it, or
    before a download where Excel should treat it as one.
    """
    frame = source_frame(frames_by_role)
    result, warnings_out = STEP_REGISTRY["fix_numeric_text"].apply(
        frame,
        {
            "columns": [str(column) for column in params.get("columns", [])],
            "decimal_separator": params.get("decimal_separator", "."),
            "parentheses_are_negative": bool(params.get("parentheses_are_negative", True)),
        },
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_fix_numeric_text(step: dict) -> str:
    columns = step.get("params", {}).get("columns") or []
    return f"Stored as numbers: {', '.join(columns)}"


def validate_fix_numeric_text(columns_by_role: dict, params: dict) -> None:
    if not params.get("columns"):
        raise InvalidStepParamsError("Choose at least one column to store as numbers.")
    if params.get("decimal_separator", ".") not in DECIMAL_SEPARATORS:
        raise InvalidStepParamsError("The decimal separator must be '.' or ','.")


def required_fix_numeric_text(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# round_numbers
# --------------------------------------------------------------------------------------


def apply_round_numbers(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Rounds numeric columns, warning by name about any that are still text.

    The warning matters here more than in Data Cleaner: everything on this page arrives as
    text, so rounding a column nobody has typed yet is the likely mistake, and the cleaner's
    message already says which step to run first.
    """
    frame = source_frame(frames_by_role)
    result, warnings_out = STEP_REGISTRY["round_numbers"].apply(
        frame,
        {
            "columns": [str(column) for column in params.get("columns", [])],
            "decimals": int(params.get("decimals", 0) or 0),
            "direction": ROUNDING_CHOICES.get(params.get("direction", ""), "nearest"),
        },
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_round_numbers(step: dict) -> str:
    params = step.get("params", {})
    columns = params.get("columns") or []
    direction = params.get("direction", "the nearest value")
    return f"Rounded {direction} to {params.get('decimals', 0)} decimal place(s): {', '.join(columns)}"


def validate_round_numbers(columns_by_role: dict, params: dict) -> None:
    if not params.get("columns"):
        raise InvalidStepParamsError("Choose at least one column to round.")
    decimals = int(params.get("decimals", 0) or 0)
    if decimals < 0 or decimals > MAX_ROUNDING_DECIMALS:
        raise InvalidStepParamsError(
            f"Decimal places must be between 0 and {MAX_ROUNDING_DECIMALS}."
        )
    if params.get("direction") not in ROUNDING_CHOICES:
        raise InvalidStepParamsError("Pick whether to round up, down, or to the nearest value.")


def required_round_numbers(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# trim_whitespace
# --------------------------------------------------------------------------------------


def apply_trim_whitespace(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Strips spaces from around every text value, and optionally inside it.

    Applies to every text column rather than chosen ones, matching Data Cleaner. Stray
    spaces are the classic reason a join finds no matches — `"ACME "` and `"ACME"` are
    different keys — so narrowing it to a selection would mostly be a way to miss one.
    """
    frame = source_frame(frames_by_role)
    result, warnings_out = STEP_REGISTRY["trim_whitespace"].apply(
        frame, {"collapse_internal": bool(params.get("collapse_internal", True))}
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_trim_whitespace(step: dict) -> str:
    suffix = (
        " and collapsed repeated spaces"
        if step.get("params", {}).get("collapse_internal", True)
        else ""
    )
    return f"Trimmed spaces from every text column{suffix}"


def validate_trim_whitespace(columns_by_role: dict, params: dict) -> None:
    return None


def required_trim_whitespace(params: dict) -> dict[str, list[str]]:
    return {"source": []}


# --------------------------------------------------------------------------------------
# remove_special_characters
# --------------------------------------------------------------------------------------


def apply_remove_special_characters(
    frames_by_role: dict, params: dict
) -> tuple[pd.DataFrame, list[str]]:
    """Keeps only the characters listed, across every text column."""
    frame = source_frame(frames_by_role)
    result, warnings_out = STEP_REGISTRY["remove_special_characters"].apply(
        frame,
        {
            "keep_pattern": params.get("keep_pattern") or DEFAULT_KEEP_PATTERN,
            "replacement": str(params.get("replacement", "")),
        },
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_remove_special_characters(step: dict) -> str:
    replacement = step.get("params", {}).get("replacement", "")
    action = f"replaced with '{replacement}'" if replacement else "removed"
    return f"Special characters {action} in every text column"


def validate_remove_special_characters(columns_by_role: dict, params: dict) -> None:
    keep_pattern = params.get("keep_pattern") or DEFAULT_KEEP_PATTERN
    try:
        re.compile(f"[^{keep_pattern}]")
    except re.error as error:
        raise InvalidStepParamsError(
            f"'{keep_pattern}' isn't a usable set of characters to keep: {error}."
        ) from error


def required_remove_special_characters(params: dict) -> dict[str, list[str]]:
    return {"source": []}


# --------------------------------------------------------------------------------------
# change_case
# --------------------------------------------------------------------------------------


def apply_change_case(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Puts the chosen text columns into one letter case."""
    frame = source_frame(frames_by_role)
    choice = CASE_CHOICES.get(params.get("case", ""), "upper")
    columns = [str(column) for column in params.get("columns", [])]

    result, warnings_out = STEP_REGISTRY["change_case"].apply(
        frame, {"by_column": {column: choice for column in columns}}
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_change_case(step: dict) -> str:
    params = step.get("params", {})
    columns = params.get("columns") or []
    return f"Changed {', '.join(columns)} to {params.get('case', '')}"


def validate_change_case(columns_by_role: dict, params: dict) -> None:
    if not params.get("columns"):
        raise InvalidStepParamsError("Choose at least one column to change the case of.")
    if params.get("case") not in CASE_CHOICES:
        raise InvalidStepParamsError("Pick UPPERCASE, lowercase or Title Case.")


def required_change_case(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# find_replace
# --------------------------------------------------------------------------------------


def apply_find_replace(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Swaps text inside the chosen columns."""
    frame = source_frame(frames_by_role)
    result, warnings_out = STEP_REGISTRY["find_replace"].apply(
        frame,
        {
            "columns": [str(column) for column in params.get("columns", [])],
            "find": str(params.get("find", "")),
            "replace": str(params.get("replace", "")),
            "regex": bool(params.get("regex", False)),
            "case_sensitive": bool(params.get("case_sensitive", True)),
        },
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_find_replace(step: dict) -> str:
    params = step.get("params", {})
    columns = params.get("columns") or []
    replacement = params.get("replace", "")
    target = f"'{replacement}'" if replacement else "nothing"
    return f"Replaced '{params.get('find', '')}' with {target} in {', '.join(columns)}"


def validate_find_replace(columns_by_role: dict, params: dict) -> None:
    if not params.get("columns"):
        raise InvalidStepParamsError("Choose at least one column to search in.")
    if not str(params.get("find", "")):
        raise InvalidStepParamsError("Type the text to look for.")
    if params.get("regex"):
        # Compiled here rather than left to the executor so a bad pattern is refused at the
        # dialog, where the box that holds it is still on screen to be corrected.
        try:
            re.compile(str(params["find"]))
        except re.error as error:
            raise InvalidStepParamsError(
                f"'{params['find']}' isn't a valid pattern: {error}. Untick the pattern box "
                "to search for it as plain text."
            ) from error


def required_find_replace(params: dict) -> dict[str, list[str]]:
    return {"source": [str(column) for column in params.get("columns", [])]}


# --------------------------------------------------------------------------------------
# unpivot
# --------------------------------------------------------------------------------------


def apply_unpivot(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Stacks several columns into two: one naming the old column, one holding its value.

    Twelve month columns become a `Month` column and a `Value` column, which is the shape
    every later step on this page wants — you cannot group by month while month is spread
    across twelve headings.
    """
    frame = source_frame(frames_by_role)
    result, warnings_out = STEP_REGISTRY["unpivot"].apply(
        frame,
        {
            "id_columns": [str(column) for column in params.get("id_columns", [])],
            "value_columns": [str(column) for column in params.get("value_columns", [])],
            "variable_name": str(params.get("variable_name") or DEFAULT_VARIABLE_NAME),
            "value_name": str(params.get("value_name") or DEFAULT_VALUE_NAME),
        },
    )
    return _ensure_new_frame(result, frame), warnings_out


def describe_unpivot(step: dict) -> str:
    params = step.get("params", {})
    kept = params.get("id_columns") or []
    stacked = params.get("value_columns") or []
    scope = f"{len(stacked)} column(s)" if stacked else "every other column"
    return f"Stacked {scope} into {params.get('variable_name') or DEFAULT_VARIABLE_NAME}, keeping {', '.join(kept)}"


def validate_unpivot(columns_by_role: dict, params: dict) -> None:
    id_columns = [str(column) for column in params.get("id_columns", [])]
    value_columns = [str(column) for column in params.get("value_columns", [])]
    if not id_columns:
        raise InvalidStepParamsError("Choose at least one column to keep as it is.")

    overlap = sorted(set(id_columns) & set(value_columns))
    if overlap:
        raise InvalidStepParamsError(
            f"{', '.join(overlap)} can't be both kept as-is and stacked up - pick one."
        )

    variable_name = str(params.get("variable_name") or DEFAULT_VARIABLE_NAME)
    value_name = str(params.get("value_name") or DEFAULT_VALUE_NAME)
    if variable_name == value_name:
        raise InvalidStepParamsError("The two new columns need different names.")
    clashing = sorted({variable_name, value_name} & set(id_columns))
    if clashing:
        raise InvalidStepParamsError(
            f"'{clashing[0]}' is already a column you're keeping - pick another name."
        )


def required_unpivot(params: dict) -> dict[str, list[str]]:
    return {
        "source": [str(column) for column in params.get("id_columns", [])]
        + [str(column) for column in params.get("value_columns", [])]
    }
