# Phase 62 — Utilities: prebuilt "Reports" (your own HTML dashboards)

**Status: done.**

## What changes

**1. A folder for your HTML dashboards: `static/reports/`.**
- Copy each self-contained HTML file there, e.g. `sales_report.html`.
- The file name becomes the report name: `sales_report.html` → "Sales Report",
  `CashFlowDashboard.html` → "Cash Flow Dashboard".
- No list to edit, no expected columns (skipped for now).

**2. A new menu item: Utilities → Reports** (every logged-in user).
- Pick a report from a dropdown → **Open report** opens it in a **new browser tab**.
- The user uploads their xlsx in that tab, as before.
- Empty folder → a short note saying where to put the files.
- Works because static serving is on (`.streamlit/config.toml`: `enableStaticServing = true`),
  which serves the file at `app/static/reports/<file>` as a normal web page.

Code: `utils/prebuilt_dashboards.py` (find, name and link the files),
`app_pages/prebuilt_reports.py`, `streamlit_app.py` (menu), `.streamlit/config.toml`.

## Tests (`tests/test_prebuilt_reports.py`)

- Files are listed by readable name, sorted; non-HTML files are ignored.
- A missing folder gives an empty list (no crash).
- The link is `app/static/reports/<file>` (spaces made safe).
- The page shows the dropdown and Open report when a file exists.
- The page shows the "no reports yet" note when the folder is empty.
