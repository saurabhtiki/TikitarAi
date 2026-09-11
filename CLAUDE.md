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
All 10 stages of requirements.md done; phase 13 (a run saves the report's current data, chat with any report); phase 14 — report comments have a bold/italic/underline/list toolbar, carried into the HTML and Excel exports; phase 15 — one upload pins every dataset, pivot table and chart in an Excel workbook, and re-importing refreshes the numbers without touching the user's titles and comments; phase 16 — the report pins tables that are already loaded (no second upload), uploads wait for a Load button, and a report shows 500 rows while the Excel download keeps every one; phase 17 — a report can hold blocks the user writes: a text note, an uploaded picture, or pasted HTML that prints as it looks; phase 18 — a pasted-HTML block renders in a sandboxed iframe at a height the user sets, so a whole styled page arrives intact instead of flattened; phase 19 — that frame's sandbox allows scripts and same-origin access, so a live embed (Power BI and the like) runs instead of showing "enable JavaScript"; phase 20 — a finished run gets an Update view where each item's note, picture and pasted HTML can be changed, and one button saves them back into the report for next month; phase 21 — two long-standing Chat with Data bugs fixed: a collapsed setup step stays collapsed, and confirming a link with mismatched rows now says so on screen; phase 22 — the report gets an External Link block (a button that opens a page in a new tab), folds every section and subsection shut by default with an Expand all / Collapse all pair, and boxes each item and subsection with pinned table headers and print rules that undo it all; phase 23 — a "summary"/"insights" question now shows its table as well as the sentence, and a chart that can't be saved as a picture says why, on screen at pin time and in the report

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

# Answers & Plan
- keep your language for Answers & Plan short & simple as layman can understand, give small examples so user can understand better.