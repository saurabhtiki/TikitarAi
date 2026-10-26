"""Utilities → Reports: open one of the prebuilt HTML dashboards in a new tab (phase 62).

Open to every logged-in role, like the other Utilities. The file is served by Streamlit's
static serving, and the user uploads their xlsx in that tab, so no data passes through here.
"""

import streamlit as st

from utils import prebuilt_dashboards

st.subheader("Reports")

dashboards = prebuilt_dashboards.list_dashboards(prebuilt_dashboards.DASHBOARDS_DIR)
if not dashboards:
    st.info(
        "No reports yet. Copy your HTML dashboards into the `static/reports` folder "
        "(e.g. `sales_report.html`) and they will appear here."
    )
    st.stop()

with st.container(horizontal=True, vertical_alignment="bottom"):
    report_name = st.selectbox(
        "Report",
        list(dashboards),
        key="prebuilt_report_choice",
        help="Pick a report, then open it and upload your Excel file there.",
    )
    st.link_button(
        "Open report",
        prebuilt_dashboards.dashboard_url(dashboards[report_name]),
        type="primary",
        icon=":material/open_in_new:",
        key="prebuilt_report_open",
        help="Opens the dashboard in a new browser tab.",
    )

st.caption(":red[The report opens in a new tab. Upload your Excel file there. It needs internet for its charts.]")
