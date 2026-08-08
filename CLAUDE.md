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
Stage 8 (previous): Criteria-Based Exceptional Reporting (requirement 6.5, written up from
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

Stage 9: Chat Types (requirement 6.6, new) — complete. Build-order item 8, which pushed
Task Builder to item 9. The new `chat_types/` package saves a whole Steps 1–3 setup — the
expected tables with each column's semantic type, the links, the column descriptions — and
a picker above the Setup / Chat / Checks control selects one. Pick "Salary processing",
upload this month's files, and the setup is already there. `— New chat type —` is the
default and is every earlier stage's behaviour, unchanged.

The load-bearing decision: **a type mismatch is fixed or refused, never tolerated.** A
semantic type decides the real DuckDB column type, so a date column that quietly loads as
text turns `joining_date < '2024-04-01'` into a string comparison returning wrong rows with
no error — and in Checks, a wrong Yes/No straight onto the Dashboard. So the saved types go
in *with* the files (`engine.loading.prepare_declared_table`, called from `sync_tables`)
and overrule detection; a column whose values won't convert refuses the load with the
offending values named. Application is all-or-nothing per table: a missing column or a
refusal applies nothing and falls back to detection, and the Chat and Checks views stay
shut until the upload matches. Extra columns and extra files are dropped and reported.
Calculated-column statements are deliberately **not** part of a chat type — they are §7.5's
Task recipe. Start over keeps the chat type selected, because uploading next month's files
against the same setup is the normal way to use one.

The **Delete button is commented out** in `_render_chat_type_bar` — deleting a chat type
isn't offered on the page. `_delete_chat_type`, `chat_types.db.delete_type` and its
un-scoping of criteria sets all remain, and the page test for it is skipped with that
reason, so un-commenting the button is all it takes to bring the feature back.

The match report lives **inside the Step 1 expander**, not in a panel of its own: once the
upload matches, collapsing Step 1 takes the green banner and its notes off the screen, so
Chat and Checks are not topped by a box repeating setup feedback. The step's header carries
`report.status_word()` — `matched` / `needs attention` — because that is all that shows
while it is shut. A blocking problem, or a discarded file, re-opens the step once
(`_open_step_one_on_problems`, keyed on the problems so a *different* one re-opens again but
a user who deliberately collapsed it is left alone). Acting on the check is deliberately not
gated on the step being open — a collapsed step must still discard extras and apply the
saved setup. `_sync_selection` moved inside Step 1 too, after `_render_upload`: it has to
run before the check, or a switching run would measure the upload against the chat type
being switched away from and discard tables on its say-so.

Three subtleties worth not rediscovering. **Selecting a chat type re-reads the uploader's
files** (`engine.session.reload_uploaded_tables`) — a table already in DuckDB was typed by
detection and the text is gone, so picking a setup after uploading would otherwise report a
clean match over a table it never touched. **The picker's selection is acted on after Step 1
has rendered**, never in the bar itself: a rerun that ends before `st.file_uploader` is
created counts as not rendering it, and Streamlit then drops the uploaded files. **A table
the declared load never saw** (Data Cleaner handoff, or detached from the uploader) can't be
re-typed, so `matching._verify_as_loaded` checks its types as they stand and blocks on a
mismatch instead.

`check_sets` gained a nullable `chat_type_id` (migrated in with a `PRAGMA table_info` guard;
the unique index is now `(user_id, COALESCE(chat_type_id, 0), name)` because SQLite treats
NULLs as distinct). A criteria set takes the scope it is saved under, Load set filters to
the active chat type with a "show all my sets" escape hatch, and deleting a chat type
**un-scopes** its sets rather than deleting them — renaming any whose name is already taken
among the unscoped ones, since the index would otherwise roll the whole delete back.

Out of band, earlier: the Data Cleaner gained Summarise / Pivot / Unpivot, which save a
derived table rather than recording a step.

Known pre-existing failures, not from this stage (they fail with Stage 9's page changes
stashed): `test_chat_with_data_page.py::TestSteps::test_a_collapsed_step_does_not_run_its_body`
and `TestRelationships::test_confirming_a_bad_link_keeps_it_declared_but_unenforced`.

Next: Stage 10 — Task Builder (requirement 7), build-order item 9. It should reuse
`checks.model`'s JSON, already shaped as a recipe, for §7.5's `task_json`, and
`chat_types.model` for the schema signature §7.4 asks a Task to capture.

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