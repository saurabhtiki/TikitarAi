# Project

AI-powered data analysis and visualization automation, built with the Agno agent framework (Python) and a Streamlit UI.

# Stack

- Python, Agno (agents), Streamlit (UI), UV (depency management)
- See `docs/requirements.md` for full project scope and requirements
- See `docs/plan.md` for the current phase's plan

# Workflow

Plan → Test → Build, one phase at a time.
- Plan: read `docs/requirements.md` and write/update `docs/plan.md` for the next phase only, before building
- Build: implement only that phase
- Test: write and run tests before moving to the next phase
- End of each phase: update "Current phase" (in 1 line short) below before starting the next

# Current phase

<!-- update this line each time a phase is completed -->
 phase 26 — Transform Data gains the nine clean-up steps it was missing (skip rows, remove empty rows, fix numbers stored as text, round, trim spaces, remove special characters, letter case, find & replace, unpivot), and each uploaded table gets a Fix headers button that opens Skip rows pre-filled; phase 27 (done) — Transform Data steps can be saved as a named pipeline that matches next month's upload by file name, checks the columns its steps actually need before running, replays every step with no AI, and hands the finished table to Chat with Data via an Export to menu; phase 28 (done) — Transform Data gains a Describe a step button beside Add a step: type an instruction in plain English, the Light Model turns it into steps from the existing catalog only (never code), you see them described and previewed before adding, and saved pipelines still replay with no AI; the model is sent each table's columns with their types, and answers in strict-schema-safe name/value pairs so cloud providers fill them in; an "update column X if Y" instruction is built as add/drop/rename so it actually overwrites the column, and its "otherwise" side keeps the column's real value (text included) instead of blanking it; phase 29 (done) — the calculated-column formula box can now test and choose: `amount > 1000` gives a 1/0 flag and `price * 0.9 if quantity > 100 else price` picks between two numbers (tests `> < >= <= = !=`, choices chainable, a blank or unreadable test takes the else side); it stays a printable whitelist with no functions and no text answers, so `add_conditional_column` is still the tool for text branches and the non-maths tests, and the AI is told to use the one-step formula only when both answers are numbers; phase 30 (done) — seven finance helpers join the catalog (36 operations now) with no UI change: Today's date, Add or subtract days, Start or end of month, Take part of a code (characters by position, for codes with no separator), Percentage of total, Drop the minus sign, and Smallest or largest across columns (sideways, per row — not a groupby); blanks rather than errors for unreadable values and a zero total, and all seven reach Describe a step straight from the registry; phase 31 (done) — development/testing and live data are now separate folders: every database and file-storage path goes through `utils/env.py::get_data_dir()`, which returns `data/` only when the setting `APP_ENV` is `"production"` (Streamlit Cloud *Settings -> Secrets*) and `data_testing/` otherwise, so an unconfigured laptop can never write to real data; `data_testing/` is gitignored and no UI changed; phase 32 (done) — Report Builder gains a **Dashboard** tab that builds an interactive HTML dashboard: an editable spec table (the add/delete-users pattern, not `st.data_editor`) of filters, cards, charts (bar, horizontal bar, line, area, pie, scatter) and flat tables, laid out by row number with filters on top or left; the data is flattened once in DuckDB from the relationships already confirmed in Setup (walking `Stock -> SubCategory -> Category` so a hierarchy filter is just a column test), embedded as columns+rows, and drawn by **Vega-Lite** vendored under `live_dashboard/assets/vendor/` (~0.8 MB, not Plotly's 4.9 MB) so grouping, totalling and click-to-cross-filter all happen in the browser with no server; the Streamlit preview and the download are the same string; the spec is saved inside the Task; a visual naming a column no confirmed link reaches is listed with a warning rather than silently joined on a guess; AI generation, templates, drill-down, pivot/map and the Chat with Data entry are phase 33+; phase 33 (done) - the Dashboard tab gains a **Describe a dashboard** button: type the page you want in plain English and the Light Model drafts the spec rows (`live_dashboard/ai_spec.py`), choosing only from the visual types, sub-types and aggregations the page already has and only from columns the flattened data really carries - an invented visual type, an unknown way of totalling, a merely similar column name or a text column asked to be summed is dropped with a sentence rather than drawn; a collapsed **Extra guidance for AI** box holds the user's own standing preferences ("always show currency in INR"), appended to the prompt below the fixed rules and saved inside the Task as `DashboardSpec.ai_guidance`; the draft is shown in words with **Replace the dashboard** or **Add to it**, and row numbers are laid out here when the model puts everything on row 1; once accepted the rows are ordinary rows - previewing, redrawing and downloading never call a model; phase 34 (done) - the dashboard's vocabulary widens so the coming chat mode has something to draw with: 14 ways to total a number (median, count unique, standard deviation, quartiles, first/last, percentage of total, running total) and 13 chart shapes (stacked and grouped bars, donut, histogram, heatmap, box plot, combo), with `live_dashboard/model.py` declaring its own `DASHBOARD_AGGREGATIONS` and `DASHBOARD_CHART_LABELS` rather than changing the five `analyst/charts.py` shares with Chat with Data; a shape we can't draw now falls back to the nearest one we can **and says so** ("asked for a treemap ... so it's a pie instead") instead of dropping the visual silently, while one with no honest stand-in (a map) still refuses; percentage-of-total and running-total are Vega-Lite transforms rather than aggregates, each refused where it would be meaningless (a percentage of nothing on a card, a running total over unordered labels); `format:currency` finally prints a symbol - a whitelisted `currency:INR` through `Intl.NumberFormat`, so the UI's own "always show currency in INR" example works; no change to how the page is used, and `tests/test_live_dashboard_runtime.py` lifts the card maths out of `runtime.js` and runs it in Node rather than grepping its source

# Library docs (Agno, Streamlit)

Agno and Streamlit APIs change frequently — do not rely on training knowledge for either.
- Streamlit: use the official `streamlit skills` install (auto-synced to installed version, no lookup needed)
- Agno: use the official docs MCP (`agno-docs`, https://docs.agno.com/mcp) for current API details.

After writing Agno/Streamlit code, verify it against these sources and flag anything deprecated.

# Coding conventions

- Naming: snake_case for functions/variables, PascalCase for classes, descriptive names (no `df1`, `temp`, `x`)
- Streamlit: every widget must have a unique `key=` and a `help=` tooltip
- streamlit: Use width property instead of old container_width property
- Every function: wrap risky logic (I/O, parsing, API/model calls) in try/except with specific, user-facing error messages — no bare `except:`
- Log errors before raising or displaying them
- After finishing a phase, run `/code-review` or `/simplify` and check output against this list
- when use st.caption use red color

# Answers & Plan
- keep your language for Answers & Plan short & simple as layman can understand, give small examples so user can understand better.