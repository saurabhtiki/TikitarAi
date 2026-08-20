"""Making a frame safe for the Arrow conversion `st.dataframe` does on the way out.

Streamlit serialises a frame to Arrow before sending it to the browser, and Arrow types a
*column*, never a cell. A pandas `object` column holding `45` on one row and `"45D"` on the
next — which is exactly what a spreadsheet column of payment terms looks like — is typed
`int64` from the first values and then fails on the text one:

    ArrowInvalid: Could not convert '45D' with type str: tried to convert to int64

Nothing is wrong with the data, and the cleaning steps are unaffected; only the rendering
fails, and it fails as a red box where the grid should be — on the raw upload, before the
user has had any chance to fix the column. So every place this app puts a raw uploaded
frame on screen sends it through `arrow_safe` first, which shows the columns Arrow cannot
type as text instead.

**The check is the conversion itself, not a guess about the values.** A heuristic ("more
than one Python type in the column") both misses cases that fail — `Decimal`, lists, mixed
timezones — and fires on ones that convert perfectly well, such as ints beside floats,
which would then lose their right-aligned numeric formatting for nothing. Only `object`
columns are tried: a column pandas has already typed cannot be mixed, so the common frame
costs one dtype check per column and no conversion at all.

Display only. Nothing here touches the frame the steps run against, the profiling stats or
the export — a column shown as text is still a column of whatever it holds.
"""

import logging

import pandas as pd
import pyarrow as pa
from pandas.api.types import is_object_dtype

logger = logging.getLogger(__name__)

# Everything `pa.array` raises when it cannot type a column, plus the two builtins pandas
# itself can raise on the way in. Named rather than caught bare: an unexpected failure
# here should surface, not silently turn a column into text.
_CONVERSION_ERRORS = (
    pa.ArrowInvalid,
    pa.ArrowTypeError,
    pa.ArrowNotImplementedError,
    ValueError,
    TypeError,
)


def to_arrow_safe(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """`frame` fit to render, and the names of the columns that had to be shown as text.

    Returns the frame *itself* when there is nothing to fix, which is the usual case — the
    copy is only made once a column has actually failed, so this is cheap to call on every
    render.

    One pass returning both, rather than a converter and a separate "which ones?", because
    the only way to know is to attempt the conversion and a caller that wants to name the
    columns must not pay for it twice.

    Positions are used rather than column names throughout: an uploaded sheet can carry the
    same header twice, and `frame[name]` on a duplicate returns a frame, not a series.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return frame, []

    unconvertible: list[int] = []
    for position, dtype in enumerate(frame.dtypes):
        if not is_object_dtype(dtype):
            continue
        try:
            pa.array(frame.iloc[:, position].to_numpy(), from_pandas=True)
        except _CONVERSION_ERRORS:
            unconvertible.append(position)

    if not unconvertible:
        return frame, []

    safe = frame.copy()
    converted: list[str] = []
    for position in unconvertible:
        name = str(frame.columns[position])
        try:
            safe.isetitem(position, safe.iloc[:, position].astype("string"))
        except (ValueError, TypeError):
            logger.exception("Could not show column %r as text for display.", name)
            continue
        logger.info("Column %r holds mixed values; showing it as text.", name)
        converted.append(name)
    return safe, converted


def arrow_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Just the frame, for the render sites where naming the columns would be noise."""
    return to_arrow_safe(frame)[0]
