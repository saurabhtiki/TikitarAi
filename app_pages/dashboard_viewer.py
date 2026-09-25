"""Viewing a report's saved dashboard - the part the Reports pages share (phase 46).

Not a page. `app_pages/report_dashboard.py` draws it full width when a Dashboard button is
pressed on **Reports** or **Chat with reports**, and the Reports run screen draws it under
a freshly run report. All of them show the same thing: the refreshed line, any visual that
can't be drawn, the download, and the dashboard.

**View and download only.** There is no edit control here and no model is called. The
dashboard is changed in Report builder, and the numbers change when someone runs the report.

The built page is cached in session state per report, refresh time and dashboard, because
every widget press reruns the script and flattening a month's data on each one would make
the page feel frozen. A new run, or a re-saved dashboard, changes the key and rebuilds it.
"""

import hashlib
import logging
from datetime import datetime

import duckdb
import streamlit as st

from live_dashboard import model as dashboard_model
from live_dashboard import saved_view
from live_dashboard.exceptions import LiveDashboardError
from reports import db as reports_db
from reports import session as reports_session
from reports.exceptions import ReportDataError
from tasks import db as tasks_db
from tasks.exceptions import TaskStorageError

logger = logging.getLogger(__name__)

#: The hidden page the Dashboard buttons open.
VIEWER_PAGE = "app_pages/report_dashboard.py"

#: Which report the viewer page is showing, and the page its Back button returns to.
RD_VIEW_KEY = "rd_view"

#: The last page built, with the key it was built for. One entry: a person looks at one
#: dashboard at a time, and keeping every one they opened would hold each month's rows.
RD_CACHE_KEY = "rd_cache"

#: How tall the dashboard is on screen - the same as Report builder's preview.
PREVIEW_HEIGHT = 720


def open_viewer(task_id: int, name: str, back_page: str) -> None:
    """Opens the full-width viewer page on one report's dashboard."""
    st.session_state[RD_VIEW_KEY] = {
        "task_id": int(task_id),
        "name": str(name or ""),
        "back_page": back_page,
    }
    st.switch_page(VIEWER_PAGE)


def viewing() -> dict | None:
    """What the viewer page was opened on - `{task_id, name, back_page}` - or None."""
    view = st.session_state.get(RD_VIEW_KEY)
    return view if isinstance(view, dict) and "task_id" in view else None


def refreshed_line(info: dict | None) -> str:
    """"Data last refreshed: 25-09-2026 14:05 by Rahul", dated dd-mm-yyyy like the rest of the app."""
    if not info:
        return "This report hasn't been run yet, so there is no data to show."
    who = str(info.get("refreshed_by") or "").strip() or "someone"
    raw = str(info.get("refreshed_at") or "").strip()
    try:
        when = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y %H:%M")
    except ValueError:
        when = raw or "an unknown time"
    return f"Data last refreshed: {when} by {who}"


def render_saved_dashboard(task_id: int, name: str, key_prefix: str) -> None:
    """The report's dashboard over its latest saved data, with a download button.

    Never raises: each failure is said on screen in words the reader can act on.
    """
    try:
        info = reports_db.dataset_info(task_id)
    except ReportDataError as error:
        logger.exception("Could not read when report %s was refreshed.", task_id)
        st.error(str(error), icon=":material/error:")
        return

    st.caption(f":red[{refreshed_line(info)}]")
    if info is None:
        return

    try:
        spec = tasks_db.load_dashboard_spec(task_id)
    except TaskStorageError as error:
        logger.exception("Could not read the dashboard saved with report %s.", task_id)
        st.error(str(error), icon=":material/error:")
        return

    if not spec.panels:
        st.info(
            "This report has no dashboard yet. Build one in **Report builder → Dashboard** "
            "and save the report.",
            icon=":material/dashboard:",
        )
        return

    built = _built_dashboard(task_id, spec, info)
    if built is None:
        return

    if built.problems:
        st.warning(
            f"{len(built.problems)} visual(s) can't be drawn with this data:\n\n"
            + "\n".join(f"- {line}" for line in built.problems),
            icon=":material/error:",
        )
    if built.size_message:
        st.caption(f":red[{built.size_message}]")
    if not built.allowed:
        return

    st.download_button(
        "Download this dashboard",
        data=built.download_html.encode("utf-8"),
        file_name=saved_view.file_name(spec, name),
        mime="text/html",
        key=f"{key_prefix}_download",
        type="primary",
        icon=":material/download:",
        on_click="ignore",
        help="One HTML file with this data inside. It works offline and stays clickable.",
    )
    if built.truncated:
        st.caption(
            f":red[The dashboard below shows the first {saved_view.PREVIEW_ROW_CAP:,} rows of "
            f"each table. The download has all {built.row_count:,}.]"
        )
    st.iframe(built.preview_html, height=PREVIEW_HEIGHT)


def _built_dashboard(task_id: int, spec: dashboard_model.DashboardSpec,
                     info: dict) -> saved_view.SavedDashboard | None:
    """The page for this report, from the cache when nothing it was built from has moved."""
    try:
        fingerprint = hashlib.sha256(dashboard_model.to_json(spec).encode("utf-8")).hexdigest()
    except (LiveDashboardError, TypeError, ValueError) as error:
        logger.exception("Could not read the dashboard saved with report %s.", task_id)
        st.error(str(error), icon=":material/error:")
        return None

    cache_key = (int(task_id), str(info.get("refreshed_at") or ""), fingerprint)
    cached = st.session_state.get(RD_CACHE_KEY)
    if isinstance(cached, dict) and cached.get("key") == cache_key:
        return cached["result"]

    cursor = None
    try:
        setup = reports_db.load_setup(task_id)
        # A cursor of our own off the shared connection, rather than `read_connection`,
        # which would swap out the cursor an open Chat with reports conversation is using.
        cursor = reports_session.store_connection(task_id).cursor()
        with st.spinner("Drawing the dashboard with the latest data…"):
            result = saved_view.build_saved_dashboard(
                cursor, spec, setup.relationships, setup.table_names()
            )
    except (ReportDataError, LiveDashboardError, duckdb.Error) as error:
        logger.exception("Could not build the saved dashboard for report %s.", task_id)
        st.error(
            f"The dashboard couldn't be drawn from this report's saved data ({error}). "
            "Running the report again usually fixes it.",
            icon=":material/error:",
        )
        return None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except duckdb.Error:
                logger.info("A dashboard read handle for report %s was already closed.", task_id)

    st.session_state[RD_CACHE_KEY] = {"key": cache_key, "result": result}
    return result
