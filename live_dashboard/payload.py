"""The data as it travels into the exported page (phase 32).

Two jobs, and the second one is a security control rather than a formatting choice.

**Shape.** Every table is written as column names once and rows as plain arrays, rather than
repeating the column names on every row. The requirement asks for one format at every size,
and this is it - for a 50,000-row table it is meaningfully smaller, and for a 50-row lookup
it costs nothing to be consistent.

**Escaping.** The payload is embedded in a `<script type="application/json">` block, and the
one thing that can break out of such a block is the literal text `</script>` appearing inside
it - which a *cell value* or a *column name* can perfectly well contain, since both come from
the user's own spreadsheet. `escape_for_script` makes that impossible. This is the single
place the exported dashboard could have been injectable, so it is deliberately not optional
and not the caller's responsibility: `dumps_for_script` is the only way out of this module.

Dates become ISO strings and numbers become floats here, in Python, so the JavaScript never
has to guess what a value is. That is also what lets a Vega-Lite `temporal` axis work with no
parsing hint (see `vega_spec._category_type`).
"""

import json
import logging

import pandas as pd

logger = logging.getLogger(__name__)

#: Above this many rows the user is offered a choice before the file is built. Not a hard
#: stop - a big file is the user's business - but silently producing a 40 MB download is not.
ROW_WARN = 50_000

#: Above this, embedding every row is refused. Past here the file stops being something that
#: can be emailed or opened comfortably, and pre-aggregating is the honest answer.
ROW_LIMIT = 90_000

#: What `escape_for_script` rewrites. `<` and `>` cover `</script>` and any stray tag; `&`
#: keeps entity decoding from reconstructing either; the two line separators are legal in
#: JSON but not in JavaScript string literals, an old and easily-missed parse break.
_SCRIPT_ESCAPES = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}

#: How a column's values are described to the page. Three words rather than pandas dtypes,
#: because the runtime only needs to know which filter widget fits and whether to right-align.
TYPE_NUMBER = "number"
TYPE_DATE = "date"
TYPE_TEXT = "text"


def column_type(series: pd.Series) -> str:
    """One column's type, as the page understands it."""
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return TYPE_NUMBER
    if pd.api.types.is_datetime64_any_dtype(series):
        return TYPE_DATE
    return TYPE_TEXT


def date_columns(tables: dict[str, pd.DataFrame]) -> frozenset[str]:
    """Every column across the embedded tables that holds dates.

    Names only, not per-table: a panel names one table, and two tables sharing a column name
    almost always share its meaning. The cost of being wrong is a nominal axis where a time
    axis was wanted, which is visible and harmless.

    Here rather than in `html_export` because two callers need the same answer: the export
    decides whether a line is spaced by elapsed time, and `model.panel_problems` decides
    whether a running total has an order to run along.
    """
    found = set()
    for frame in tables.values():
        for name in frame.columns:
            try:
                if column_type(frame[name]) == TYPE_DATE:
                    found.add(str(name))
            except (TypeError, ValueError) as error:
                logger.info("Could not read the type of column %r: %s", name, error)
    return frozenset(found)


def _cell(value, kind: str):
    """One value as something `json.dumps` can write and JavaScript can use directly.

    Missing values become `None` (JSON `null`) rather than the string "nan": a chart must be
    able to tell a missing amount from a zero one, and `NaN` is not valid JSON in the first
    place.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        # Some values (lists, arrays) make `isna` ambiguous; they are not missing.
        pass

    if kind == TYPE_NUMBER:
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.info("An unreadable number reached the payload; writing it as text.")
            return str(value)
    if kind == TYPE_DATE:
        try:
            return pd.Timestamp(value).isoformat()
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def frame_to_table(frame: pd.DataFrame) -> dict:
    """One DataFrame as the columns/types/rows object the page reads.

    Column *order* is the frame's own, and the rows are arrays in that order - so the two
    must never be reordered independently. They are built in one pass here for exactly that
    reason.
    """
    columns = [str(name) for name in frame.columns]
    types = {str(name): column_type(frame[name]) for name in frame.columns}
    kinds = [types[name] for name in columns]

    rows = [
        [_cell(value, kind) for value, kind in zip(record, kinds)]
        for record in frame.itertuples(index=False, name=None)
    ]

    return {"columns": columns, "types": types, "rows": rows}


def escape_for_script(text: str) -> str:
    """Makes a JSON string safe to sit inside a `<script>` element.

    The output is still valid JSON - every replacement is a `\\uXXXX` escape, which JSON
    parsers read back as the original character. What changes is that the *bytes in the HTML*
    no longer contain `</script>`, so the browser cannot be tricked into ending the block
    early by a value someone typed into a spreadsheet cell.

    Applied to the whole serialised document rather than to individual values, so there is no
    way to add a field later and forget it.
    """
    for character, replacement in _SCRIPT_ESCAPES.items():
        text = text.replace(character, replacement)
    return text


def dumps_for_script(payload: dict) -> str:
    """Serialises the payload and makes it safe for a `<script>` block.

    The only supported way to turn a payload into text. Keeping `json.dumps` and
    `escape_for_script` welded together here is what stops a future caller from doing the
    first and forgetting the second.
    """
    return escape_for_script(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def build_payload(tables: dict[str, pd.DataFrame], main_table: str, panels: list[dict],
                  filters: list[dict], settings: dict) -> dict:
    """Everything the exported page needs, as one object.

    Args:
        tables: each embedded table by the name panels refer to it by.
        main_table: which of those is the flattened fact table.
        panels: the visual panels, each already carrying its Vega-Lite spec where it has one.
        filters: the filter widgets, each with the column and widget type it drives.
        settings: theme, filter position, palette, and the page's titles.
    """
    return {
        "tables": {name: frame_to_table(frame) for name, frame in tables.items()},
        "main_table": main_table,
        "panels": panels,
        "filters": filters,
        "settings": settings,
    }


def row_guard(row_count: int) -> tuple[bool, str]:
    """Whether this many rows may be embedded as they are, and what to say about it.

    Returns `(allowed, message)`. The message is empty for a comfortable size, advisory above
    `ROW_WARN`, and refusing above `ROW_LIMIT` - where the caller must offer pre-aggregation
    or a date range instead of a download that would be painful to open.
    """
    if row_count > ROW_LIMIT:
        return False, (
            f"{row_count:,} rows is too many to put in one file. Summarise by month, or "
            "narrow the date range, and try again."
        )
    if row_count > ROW_WARN:
        return True, (
            f"{row_count:,} rows will make a large file that is slow to open and awkward to "
            "email. Summarising by day or month would keep it comfortable."
        )
    return True, ""
