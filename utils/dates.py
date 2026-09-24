"""One rule for how a date looks, wherever the app reads, shows or saves one: dd-mm-yyyy.

This app's users write `03-04-2025` for the 3rd of April. Left to their defaults, pandas
writes that date as `2025-04-03 00:00:00`, Streamlit shows it as `2025-04-03` and a
download keeps whichever of those it was given - so a date the user typed one way came back
another. Every page routes through here instead, so the rule lives in one place:

- a date alone reads `03-04-2025`
- a date with a time of day reads `03-04-2025 10:30:00`

The dates themselves stay real dates wherever they already were. Only how they are
written changes, so sorting a table or filtering an Excel column still works by date.
"""

import datetime
import logging

import pandas as pd
import streamlit as st
from pandas.api.types import is_datetime64_any_dtype

logger = logging.getLogger(__name__)

#: For Python's `strftime`.
DATE_TEXT_FORMAT = "%d-%m-%Y"
DATETIME_TEXT_FORMAT = "%d-%m-%Y %H:%M:%S"

#: For Streamlit's column config, which uses momentJS codes.
DATE_DISPLAY_FORMAT = "DD-MM-YYYY"
DATETIME_DISPLAY_FORMAT = "DD-MM-YYYY HH:mm:ss"

#: For `pd.ExcelWriter(**EXCEL_WRITER_DATE_FORMATS)`. `date_format` applies to plain `date`
#: values and `datetime_format` to ones carrying a time, which is why `excel_ready` turns a
#: column with no times in it into plain dates first.
EXCEL_WRITER_DATE_FORMATS = {
    "date_format": "dd-mm-yyyy",
    "datetime_format": "dd-mm-yyyy hh:mm:ss",
}


def has_time_of_day(series: pd.Series) -> bool:
    """True if any date in the column is at a time other than midnight."""
    try:
        present = series.dropna()
        if present.empty:
            return False
        return bool((present != present.dt.normalize()).any())
    except (AttributeError, TypeError, ValueError) as error:
        logger.debug("Could not check '%s' for times of day: %s", series.name, error)
        return True


def date_columns(frame: pd.DataFrame) -> list:
    """The columns of a table that hold real dates."""
    return [column for column in frame.columns if is_datetime64_any_dtype(frame[column])]


def format_date_value(value: datetime.date) -> str:
    """One date as text: `03-04-2025`, or `03-04-2025 10:30:00` when it has a time."""
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0, 0):
            return value.strftime(DATE_TEXT_FORMAT)
        return value.strftime(DATETIME_TEXT_FORMAT)
    return value.strftime(DATE_TEXT_FORMAT)


def date_column_config(frame: pd.DataFrame, column_config: dict | None = None) -> dict:
    """Streamlit column settings that show each date column as dd-mm-yyyy.

    A column the caller has already configured is left exactly as the caller set it.
    """
    config = dict(column_config or {})
    for column in date_columns(frame):
        if column in config:
            continue
        display_format = DATETIME_DISPLAY_FORMAT if has_time_of_day(frame[column]) else DATE_DISPLAY_FORMAT
        config[column] = st.column_config.DatetimeColumn(format=display_format)
    return config


def show_dataframe(data, **kwargs):
    """`st.dataframe`, with every date column shown as dd-mm-yyyy.

    Everything else is passed straight through, and whatever `st.dataframe` returns (a
    selection, when `on_select` is set) is returned unchanged.
    """
    frame = data if isinstance(data, pd.DataFrame) else getattr(data, "data", None)
    if isinstance(frame, pd.DataFrame) and date_columns(frame):
        kwargs["column_config"] = date_column_config(frame, kwargs.get("column_config"))
    return st.dataframe(data, **kwargs)


def dates_as_text(frame: pd.DataFrame) -> pd.DataFrame:
    """A copy with every date column written as dd-mm-yyyy text, for a CSV download."""
    columns = date_columns(frame)
    if not columns:
        return frame
    result = frame.copy()
    for column in columns:
        text_format = DATETIME_TEXT_FORMAT if has_time_of_day(result[column]) else DATE_TEXT_FORMAT
        result[column] = result[column].dt.strftime(text_format)
    return result


def excel_ready(frame: pd.DataFrame) -> pd.DataFrame:
    """A copy ready for `to_excel` under `EXCEL_WRITER_DATE_FORMATS`.

    A date column with no times in it becomes plain dates, so Excel shows `03-04-2025`
    rather than `03-04-2025 00:00:00`. Excel refuses time zones outright, so those are
    dropped (the clock time is kept as it read). Either way the cells stay real dates.
    """
    columns = date_columns(frame)
    if not columns:
        return frame
    result = frame.copy()
    for column in columns:
        series = result[column]
        if getattr(series.dt, "tz", None) is not None:
            series = series.dt.tz_localize(None)
        result[column] = series if has_time_of_day(series) else series.dt.date
    return result
