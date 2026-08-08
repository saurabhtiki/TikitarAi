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
- End of each phase: update "Current phase" below before starting the next

# Current phase

<!-- update this line each time a phase is completed -->
Stage 8: Criteria-Based Exceptional Reporting (requirement 6.5, written up from
`docs/addtion.md`) — complete. Build-order item 7. The new `checks/` package plus
`app_pages/checks_view.py` add a third view to the Chat page: business rules in plain
language become guarded SQL, and **saving a result auto-pins it to the Dashboard, which is
the report** — there is no separate report screen. Two deliberate departures from
`addtion.md` step 5: a criteria has **no chart of its own** (one stacked summary chart at the
foot of the Design tab compares every saved criteria instead, with an AI overview under it),
and Save pins **whichever of All / Failures / Passes is on screen** while the saved run keeps
every row — the counts, the remarks and the action drafts all read from the full run. Report
items a criteria owns can only be removed from the Checks tab; the Dashboard no longer
discards anything carrying a `source_id`. A criteria that already has SQL offers **Run saved
SQL** (no provider call — the stored statement is the recipe) beside **Regenerate SQL**, so
re-running a set against next month's file is free; a run that fails reports the error and
leaves regenerating to the user. The overview pins `summary.combined_chart` — counts and
shares as two panels of one figure, because a `PinnedItem` holds only one.
Follow-up emails, meetings and tasks are
drafted and downloadable as `.eml` / `.ics`; **nothing is sent — there is no SMTP anywhere
in this app**.

Out of band, earlier: the Data Cleaner gained Summarise / Pivot / Unpivot, which save a
derived table rather than recording a step.

Next: Stage 9 — Task Builder (requirement 7), build-order item 8. It should reuse
`checks.model`'s JSON, already shaped as a recipe, for §7.5's `task_json`.

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