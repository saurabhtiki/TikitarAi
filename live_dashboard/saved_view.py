"""A report's saved dashboard drawn over its saved data, for anyone to view (phase 46).

Report Builder designs a dashboard over the files loaded in that session. The Reports page
is where each month's files are uploaded, and every run saves them (`reports/session.py`)
so Chat with reports can answer questions without an upload. This module draws the
dashboard over **that** saved copy, so the latest run's numbers can be seen, and
downloaded, by anyone who can log in.

View and download only. Nothing here edits the spec or calls a model: the rows were written
by a run, the links came from Setup, and the HTML is the same `build_dashboard_html` string
Report Builder previews and downloads.

No Streamlit here, so it is tested against a real DuckDB file without `AppTest`.
"""

import logging
from dataclasses import dataclass, field

import duckdb
import pandas as pd

from engine.relationships import Relationship
from live_dashboard import flatten, html_export, payload
from live_dashboard.exceptions import DashboardDataError
from live_dashboard.model import DashboardSpec, panel_problems

logger = logging.getLogger(__name__)

#: The most rows the on-screen preview carries - the same cap Report Builder's preview uses.
#: The download always carries everything.
PREVIEW_ROW_CAP = 5_000


@dataclass
class SavedDashboard:
    """What the viewer shows.

    Attributes:
        preview_html: the page drawn on screen, built from at most `PREVIEW_ROW_CAP` rows
            per table. Empty when `allowed` is False.
        download_html: the page handed to the download button, carrying every row. The same
            string as `preview_html` whenever nothing was cut.
        problems: one line per visual that can't be drawn, named by title - the thing a
            picture of a dashboard cannot show.
        row_count: every embedded row, across every table.
        size_message: the row guard's note, advisory or refusing; empty for a normal size.
        allowed: False when there are too many rows to put in one file.
        truncated: True when the preview shows fewer rows than the download.
    """

    preview_html: str = ""
    download_html: str = ""
    problems: list[str] = field(default_factory=list)
    row_count: int = 0
    size_message: str = ""
    allowed: bool = True
    truncated: bool = False


def build_saved_dashboard(connection: duckdb.DuckDBPyConnection, spec: DashboardSpec,
                          relationships: list[Relationship],
                          table_names: list[str]) -> SavedDashboard:
    """Flattens the saved tables with the saved links and renders the dashboard.

    Args:
        connection: a handle onto the report's saved data. Only read from.
        spec: the dashboard saved with the report.
        relationships: the links saved with the data, the ones the run confirmed.
        table_names: the tables in the saved data.

    Raises:
        DashboardDataError: if the saved tables can't be joined or read.
        DashboardExportError: if the page can't be rendered.
    """
    names = [name for name in table_names if name]
    if not names:
        raise DashboardDataError("This report's saved data has no tables to draw from.")

    tables, _description = flatten.flatten_every_table(connection, relationships, names)
    result = SavedDashboard(problems=describe_problems(spec, tables))

    result.row_count = sum(len(frame) for frame in tables.values())
    result.allowed, result.size_message = payload.row_guard(result.row_count)
    if not result.allowed:
        return result

    preview_tables = {name: frame.head(PREVIEW_ROW_CAP) for name, frame in tables.items()}
    result.truncated = any(len(frame) > PREVIEW_ROW_CAP for frame in tables.values())

    result.preview_html = html_export.build_dashboard_html(spec, preview_tables)
    result.download_html = (
        html_export.build_dashboard_html(spec, tables) if result.truncated else result.preview_html
    )
    return result


def describe_problems(spec: DashboardSpec, tables: dict[str, pd.DataFrame]) -> list[str]:
    """One sentence per visual the saved data can't draw, e.g. a column this month's file lost.

    The same check Report Builder runs above its preview, so a visual missing here is named
    in the same words there.
    """
    available = {name: [str(column) for column in frame.columns] for name, frame in tables.items()}
    dates = payload.date_columns(tables)
    lines = []
    for panel in spec.panels:
        problem = panel_problems(panel, available, dates)
        if problem:
            lines.append(f"**{panel.display_title()}** - {problem}")
    return lines


def file_name(spec: DashboardSpec, fallback: str) -> str:
    """The download's file name: the dashboard's title, or the report's name without one."""
    title = (spec.title or "").strip() or (fallback or "").strip() or "dashboard"
    safe = "".join(character if character.isalnum() else "_" for character in title.lower())
    return f"{safe.strip('_') or 'dashboard'}.html"

