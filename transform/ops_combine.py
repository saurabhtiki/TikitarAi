"""Operations that read more than one table: join, lookup, append.

These are the reason this page exists. Nothing anywhere else in the app puts two uploaded
files together, and the requirements document's worked example (section 3) is a join
followed by a calculated column followed by a group total.

`lookup` is a deliberate second face on the same machinery as `merge`. A left join that
brings back three named columns is what people mean by VLOOKUP, and asking them to pick a
join type and then drop eleven unwanted columns afterwards is the kind of friction that
sends someone back to Excel. The requirements document names it for that reason.

Both warn loudly about the two things that silently ruin a join: keys of different types
that therefore match nothing, and a many-to-many key that multiplies rows.
"""

import logging

import pandas as pd

from transform.exceptions import InvalidStepParamsError
from transform.ops_common import missing_warning, present_columns, require_columns, to_numeric

logger = logging.getLogger(__name__)

JOIN_TYPES = [
    "keep all rows from the left table",
    "keep only rows that match",
    "keep all rows from the right table",
    "keep all rows from both",
]

#: This page's wording -> the pandas `how=` name.
_JOIN_TYPES = {
    "keep all rows from the left table": "left",
    "keep only rows that match": "inner",
    "keep all rows from the right table": "right",
    "keep all rows from both": "outer",
}


def _matchable(
    left: pd.DataFrame, right: pd.DataFrame, left_keys: list[str], right_keys: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Makes two tables' join keys comparable, warning when they weren't.

    This is the single most common reason a join returns nothing in this app: uploads are
    read as text, so a `customer_id` that has been converted to a number on one side and
    left as text on the other matches not one row. Rather than hand back an empty table and
    let the user guess, both sides are compared as trimmed text and the mismatch is named.

    Keys are only *compared* as text; neither table's own columns are changed.
    """
    left_working = left.copy()
    right_working = right.copy()
    warnings_out: list[str] = []

    for left_key, right_key in zip(left_keys, right_keys, strict=True):
        left_series = left_working[left_key]
        right_series = right_working[right_key]
        if left_series.dtype == right_series.dtype:
            continue

        warnings_out.append(
            f"'{left_key}' and '{right_key}' hold different kinds of value "
            f"({left_series.dtype} and {right_series.dtype}), so they were matched as text."
        )
        left_working[left_key] = _as_key_text(left_series)
        right_working[right_key] = _as_key_text(right_series)

    return left_working, right_working, warnings_out


def _as_key_text(series: pd.Series) -> pd.Series:
    """A join key as trimmed text, with a whole number losing its trailing `.0`.

    `pd.to_numeric` turns the text `1001` into the float `1001.0`, whose string form is
    `"1001.0"` — which would then match nothing on the text side. Normalising both to
    `"1001"` is what makes the text comparison actually work.
    """
    if pd.api.types.is_numeric_dtype(series):
        whole = series.dropna().map(lambda value: float(value).is_integer()).all()
        if whole:
            return series.astype("Int64").astype("string").str.strip()
    return series.astype("string").str.strip()


def _row_growth_warning(before: int, after: int, operation: str) -> list[str]:
    """Names a join that multiplied rows, which is almost always a surprise."""
    if after <= before:
        return []
    return [
        f"{operation} turned {before:,} row(s) into {after:,} - the matching column has "
        f"repeats in the other table, so some rows were matched more than once."
    ]


# --------------------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------------------


def apply_merge(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Joins two tables on one or more matching columns."""
    left = frames_by_role.get("left")
    right = frames_by_role.get("right")
    if not isinstance(left, pd.DataFrame) or not isinstance(right, pd.DataFrame):
        raise InvalidStepParamsError("Join needs two tables.")

    left_keys = [str(column) for column in params.get("left_on", [])]
    right_keys = [str(column) for column in params.get("right_on", [])] or left_keys

    require_columns(left, left_keys, "Join")
    require_columns(right, right_keys, "Join")
    if len(left_keys) != len(right_keys):
        raise InvalidStepParamsError(
            f"Join needs the same number of matching columns on each side - got "
            f"{len(left_keys)} and {len(right_keys)}."
        )

    left_working, right_working, warnings_out = _matchable(left, right, left_keys, right_keys)
    how = _JOIN_TYPES.get(params.get("join_type", JOIN_TYPES[0]), "left")

    merged = left_working.merge(
        right_working,
        left_on=left_keys,
        right_on=right_keys,
        how=how,
        suffixes=("", params.get("suffix") or "_from_right"),
    )

    warnings_out.extend(_row_growth_warning(len(left), len(merged), "The join"))
    unmatched = int(merged[left_keys[0]].isna().sum()) if how in ("right", "outer") else 0
    if how == "left":
        # A left join keeps every left row; the ones that found nothing show as blanks in
        # the columns that came from the right table.
        brought = [column for column in right_working.columns if column not in left_keys]
        if brought:
            unmatched = int(merged[brought[0]].isna().sum())
    if unmatched:
        warnings_out.append(f"{unmatched:,} row(s) found no match in the other table.")

    return merged, warnings_out


def describe_merge(step: dict) -> str:
    params = step.get("params", {})
    inputs = step.get("inputs", {})
    keys = ", ".join(params.get("left_on", []))
    return f"Joined {inputs.get('left')} with {inputs.get('right')} on {keys}"


def validate_merge(columns_by_role: dict, params: dict) -> None:
    if not params.get("left_on"):
        raise InvalidStepParamsError("Choose the matching column(s) in the first table.")
    right_keys = params.get("right_on") or params.get("left_on")
    if len(params["left_on"]) != len(right_keys):
        raise InvalidStepParamsError("Pick the same number of matching columns on each side.")


def required_merge(params: dict) -> dict[str, list[str]]:
    return {
        "left": [str(column) for column in params.get("left_on", [])],
        "right": [str(column) for column in (params.get("right_on") or params.get("left_on") or [])],
    }


# --------------------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------------------


def apply_lookup(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Brings chosen columns across from another table, matched on a key. VLOOKUP.

    Always a left join, so the table being added to never loses a row — the property that
    makes this safe to reach for, and the reason it is a separate operation from `merge`
    rather than a preset of it.
    """
    source = frames_by_role.get("source")
    other = frames_by_role.get("lookup_table")
    if not isinstance(source, pd.DataFrame) or not isinstance(other, pd.DataFrame):
        raise InvalidStepParamsError("Lookup needs two tables.")

    source_key = str(params.get("source_key", ""))
    other_key = str(params.get("lookup_key", "") or source_key)
    wanted = [str(column) for column in params.get("bring_columns", [])]

    require_columns(source, [source_key], "Lookup")
    require_columns(other, [other_key], "Lookup")

    existing, missing = present_columns(other, wanted)
    warnings_out = missing_warning(missing)
    if not existing:
        raise InvalidStepParamsError(
            "Choose at least one column to bring across from the other table."
        )

    # One row per key, so a lookup can never multiply the rows of the table it is adding
    # to. That is the difference between a lookup and a join, and the user is told when it
    # mattered.
    slim = other[[other_key, *existing]].copy()
    duplicates = int(slim[other_key].duplicated().sum())
    if duplicates:
        slim = slim.drop_duplicates(subset=[other_key], keep="first")
        warnings_out.append(
            f"'{other_key}' repeats {duplicates:,} time(s) in the other table; the first "
            f"match for each was used."
        )

    source_working, slim_working, key_warnings = _matchable(source, slim, [source_key], [other_key])
    warnings_out.extend(key_warnings)

    if other_key != source_key:
        slim_working = slim_working.rename(columns={other_key: source_key})

    merged = source_working.merge(slim_working, on=source_key, how="left", suffixes=("", "_looked_up"))

    unmatched = int(merged[existing[0]].isna().sum())
    if unmatched:
        warnings_out.append(
            f"{unmatched:,} row(s) had no matching '{source_key}' in the other table, so "
            f"their new columns are blank."
        )
    return merged, warnings_out


def describe_lookup(step: dict) -> str:
    params = step.get("params", {})
    brought = ", ".join(params.get("bring_columns", []))
    return (
        f"Brought {brought} from {step.get('inputs', {}).get('lookup_table')}, matched on "
        f"{params.get('source_key')}"
    )


def validate_lookup(columns_by_role: dict, params: dict) -> None:
    if not str(params.get("source_key", "")).strip():
        raise InvalidStepParamsError("Choose the matching column in this table.")
    if not params.get("bring_columns"):
        raise InvalidStepParamsError("Choose at least one column to bring across.")


def required_lookup(params: dict) -> dict[str, list[str]]:
    return {
        "source": [str(params.get("source_key", ""))],
        "lookup_table": [
            str(params.get("lookup_key") or params.get("source_key", "")),
            *[str(column) for column in params.get("bring_columns", [])],
        ],
    }


# --------------------------------------------------------------------------------------
# concat
# --------------------------------------------------------------------------------------


def apply_concat(frames_by_role: dict, params: dict) -> tuple[pd.DataFrame, list[str]]:
    """Stacks two or more tables on top of each other, lining up matching column names.

    A column only some of the tables have is kept and left blank in the others, and the
    tables that were missing it are named. Dropping it instead would quietly lose data;
    failing would be useless for the common case of twelve monthly files where one month
    gained a column.
    """
    frames = frames_by_role.get("frames")
    names = params.get("_frame_names") or []
    if not isinstance(frames, list) or len(frames) < 2:
        raise InvalidStepParamsError("Choose at least two tables to append.")

    warnings_out: list[str] = []
    shared = set(frames[0].columns)
    everything: set[str] = set()
    for frame in frames:
        shared &= set(frame.columns)
        everything |= set(frame.columns)

    if not shared:
        raise InvalidStepParamsError(
            "These tables have no column names in common, so there is nothing to line up. "
            "Rename the columns to match first."
        )

    partial = everything - shared
    if partial:
        listed = ", ".join(sorted(str(column) for column in partial)[:8])
        warnings_out.append(
            f"{len(partial)} column(s) aren't in every table and are blank where they were "
            f"missing: {listed}."
        )

    if params.get("add_source_column"):
        labelled = []
        for position, frame in enumerate(frames):
            tagged = frame.copy()
            tagged["source table"] = names[position] if position < len(names) else f"table {position + 1}"
            labelled.append(tagged)
        frames = labelled

    combined = pd.concat(frames, ignore_index=True, sort=False)
    warnings_out.append(
        f"Stacked {len(frames)} table(s) into {len(combined):,} row(s)."
    )
    return combined, warnings_out


def describe_concat(step: dict) -> str:
    names = step.get("inputs", {}).get("frames") or []
    return f"Appended {', '.join(str(name) for name in names)}"


def validate_concat(columns_by_role: dict, params: dict) -> None:
    """Nothing to check beyond the table count, which the pipeline enforces for a multi role."""


def required_concat(params: dict) -> dict[str, list[str]]:
    return {"frames": []}
