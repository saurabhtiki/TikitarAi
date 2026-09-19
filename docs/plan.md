# Phase 27 — save a pipeline, reuse it next month, export to Chat with Data

**Status: built and tested.** Three points came out differently from the plan below; each
is marked **[as built]** where it applies.

Phases 25–26 built every step Transform Data needs (29 of them). What's still missing is
the actual promise of the tool: define the steps once, save them, and next month just
upload the new file and get the same result — no re-clicking. This phase adds that, copying
the pattern Data Cleaner already proved with its **Templates**, and adds one more thing the
user asked for: sending a finished table straight to Chat with Data.

---

## 1. Why this copies Data Cleaner, not something new

Data Cleaner already solved "save my steps, match them to next month's file" with a
**template**: a name, a description, the expected columns per file, and the recipe. A picker
shows saved templates; picking one and dropping in a new file checks it has the right
columns and replays the steps automatically.

Transform Data needs exactly this, table for table:

| Data Cleaner | Transform Data |
|---|---|
| Template | Pipeline |
| One recipe per uploaded file | **[as built]** One ordered step list for the whole pipeline |
| Expected columns per file | Expected columns per named input table |
| Derived summary table (Pivot/Group & total) rebuilt from its parent | Same — a derived table is rebuilt by the steps, never stored |

**[as built] Steps are one list, not a recipe per table.** The plan said each saved table
would carry its own steps. That doesn't survive contact with a join: a `merge` step reads
two tables at once, so "which table's recipe is it in?" has no answer, and splitting the
list would mean re-deriving the execution order on load from something never stored. So a
`PipelineTable` records the file and its columns, and the pipeline holds the steps.

The one real difference: Transform Data steps can span *multiple* tables (merge, lookup,
append), so a saved pipeline stores expected columns per **input table**, not per file. The
save/match/replay mechanics are otherwise identical, so this phase is an adaptation of
`cleaner/template.py` and `cleaner/db.py`, not a new design.

---

## 2. Saving a pipeline

At the bottom of Transform Data, a **Save pipeline** button (name + optional description),
same spot Data Cleaner's "Save template" sits.

What gets saved, per input table the pipeline actually uses:
- Its name
- Its column names (for later validation — dtypes only where a step actually depends on one)
- The steps recorded against it, in order

Plus, for the pipeline as a whole:
- Its name and description
- Which final output table(s) are marked for export/download

Re-saving under a name you already used **updates that pipeline** — same rule as Data
Cleaner, because re-saving after tweaking one more step is the normal case, not an error.

**Storage:** a new SQLite table `transform_pipelines`, same shape and same short-lived
per-call connection pattern as `cleaning_templates` in `cleaner/db.py`. One row per pipeline,
JSON body, scoped to `user_id`.

---

## 3. Reusing a saved pipeline

1. User opens **Load a saved pipeline**, picks one by name (shows "3 table(s) · 12 step(s)",
   same summary line style as Data Cleaner's picker).
2. The page shows what it expects: "This pipeline needs files matching: `sales`,
   `customer_master`" with each one's required columns.
3. User uploads file(s). Each is matched to an expected input table by file name (same
   matching rules `cleaner/matching.py` already has — case/spacing-insensitive).
4. Before running anything, each matched file is checked: does it have every column the
   pipeline's steps actually use? Extra columns and reordered columns are fine and ignored.

   **[as built] The saved schema is what decides "actually use".** A step halfway down may
   read `bonus` — a column an *earlier step created*. Demanding that of next month's file
   would refuse a perfectly good one. So a column is required only if the file had it when
   the pipeline was saved; anything else the steps mention, the steps make. This also
   closes the gap phase 26 left in `required_add_calculated_column`, which returns nothing
   because a formula can't be tokenized without a column list — with the saved schema in
   hand, it can.
5. **Missing column** → stop, show exactly which pipeline table and which column, e.g.
   *"Pipeline expects column 'basic_salary' in 'sales' but the uploaded file has
   'basic salary'."* Nothing runs.
6. **All present** → every saved step replays in order, no AI call, same as a step you'd
   added by hand. Preview shows up to 500 rows; download carries every row (phase 16's rule).

---

## 4. Export to Chat with Data

Data Cleaner already has this: an `EXPORT_DESTINATIONS` button that hands its cleaned tables
to the Data Engine (`engine/session.py`'s `adopt_cleaner_tables`), which Chat with Data then
reads from.

Transform Data gets the same button on its final output table(s): **Send to Chat with
Data**. Under the hood this is a small, generalised version of what already exists:

- `engine/session.py` gets `adopt_transform_tables()`, parallel to `adopt_cleaner_tables()`.
  **[as built] the frames are passed in, not read back.** Transform Data keeps no
  DataFrames in session state — its tables are derived by replaying the steps — so reading
  them from the engine would mean running the whole pipeline a second time, which could
  disagree with what the user is looking at. The page has the answer in hand and hands it
  over.
- The `EngineTable` gets a second provenance flag alongside `from_cleaner` (e.g.
  `from_transform`), so the dictionary rebuild and the "which page owns this table" bookkeeping
  stay correct without the two pages fighting over one flag.
- Clicking the button adopts the table(s), rebuilds the schema dictionary
  (`refresh_dictionary`), and switches to Chat with Data — identical sequence to Data
  Cleaner's button.

Only the tables the user marked as **final output** are offered, not every intermediate
named table — a merge's raw halves aren't useful to chat over, only the result.

---

## 5. What's still not in this phase

Deliberately left for a later phase, per the requirements doc:
- **Plain English step entry** (typing an instruction, AI turns it into a step) — this phase
  is about saving/reusing steps that are already there, not about how they're added.
- **custom_formula** sandboxed fallback for row-level math the catalog doesn't cover.

Both build cleanly on top of a pipeline that can already be saved and replayed, so they come
after this phase, not before it.

---

## 6. Files

- `transform/template.py` — **new**: `SavedPipeline`, `PipelineTable` (name, file, columns),
  `capture`/`to_json`/`from_json`.
- `transform/db.py` — **new**: `transform_pipelines` table, list/load/save/delete, scoped
  to `user_id`, name collision = update.
- `transform/matching.py` — **new**: `required_columns_by_table` and `check_upload`.
- `transform/exceptions.py` — `PipelineStorageError`.
- `transform/session.py` — capture/match/apply, the selection and dialog state.
- `app_pages/transform_data.py` — the pipeline bar, the Run button, the expected-files
  dialog, and the Export to menu.
- `engine/session.py` — `adopt_transform_tables()`, `from_transform` provenance flag.
- `streamlit_app.py` — creates the new table at start-up.

## 7. Tests

All written and passing.

- `tests/test_transform_template.py` (28) — capture, the JSON round trip, a step unknown to
  this version carried through rather than refused, and the storage rules including
  cross-account isolation.
- `tests/test_transform_matching.py` (20) — a column the pipeline creates is *not* demanded
  of the file, formulas resolved against the saved schema, matching by file name, missing
  column named on both sides, extra/reordered columns allowed.
- `tests/test_transform_data_page.py` (+25, 68 total) — the bar, saving, updating, deleting,
  reuse end to end including a renamed table being renamed back, both blocking cases on
  screen, and Export to Chat with Data adopting only the ticked table.

---

## 8. Code review

`/code-review` after the build found five things. Three were fixed and covered by tests;
two were left alone on purpose.

**Fixed**

1. **`app_pages/transform_form.py` — "Add a calculated column" could not be used at all.**
   `_build_expression` never returned the formula the user typed, so the Add button stayed
   permanently greyed out saying "Formula" was still needed. A missing `return value`.
   This was phase 25/26 code, not phase 27's, but a saved pipeline containing a calculated
   column was impossible to create, so the phase's own promise depended on it. The formula
   box had **no test coverage at all**, which is how it shipped broken; it does now.
2. **`transform/pipeline.py` — replay could silently overwrite an uploaded table.** A step
   whose output mode is `new` wrote with `replace=True`, so a saved pipeline replayed
   against next month's upload would quietly clobber an extra file that happened to share
   its output name — directly contradicting `transform.matching`, which had just told the
   user that file was "left as it is". Now `replace=False` for a `new` output (in place
   still replaces, or no step could change a table), and the collision is reported as a
   failed step.
3. **`transform/session.py` — a failed rename left a table called `tfmove0`.** If the
   second pass of `apply_pipeline`'s two-pass rename failed, the user's table was stranded
   under an internal name. It is now put back under the name they know it by.

**Confirmed as intended**

4. **26 captions changed from `:grey[` to `:red[`** across chat_with_data, meetings,
   report_view, run_task and setup_view. Flagged rather than reverted, since these edits
   predated phase 27; the user confirmed captions should be red everywhere. A repo-wide
   search now finds no remaining `:grey[` caption, so nothing further to change.

**Left alone**

5. **`transform/session.py:sync_uploads`** de-duplicates a new table's name only against the
   tables built so far this run**, not the full name map, so in principle a new upload
   could take a name already assigned to a later-iterated table and drop it. Pre-existing
   phase 25/26 code, and it needs the uploader to list a new file *before* an existing one
   — which normal upload order doesn't do. Recorded rather than fixed, to keep this phase's
   diff to its own scope.

## 9. Fixes after real use

Two things the user hit on the first real run with saved pipelines.

**1. Last month's steps hung around after "— New pipeline —".**

What happened: run a saved pipeline, switch the picker back to `— New pipeline —`, upload
a completely different set of files — and the page opens on
`Step 1 (Skip rows) couldn't run… There's no table called 'Stock with difference August 20'`.
A step from a pipeline that is no longer selected, failing on a table that was never in
this month's files.

Why: steps live in `TF_STEPS_KEY` and replay on every rerun. `clear_active_pipeline` was
written to deselect *without* touching steps ("deselecting is not undoing"), which is right
for steps the user built by hand but wrong for steps a pipeline installed and nobody has
since changed.

The fix tells the two apart:

- `TF_STEPS_SOURCE_KEY` records the pipeline that installed the current steps.
- `set_steps` clears it. Every route that changes a step — add, edit, undo, a table rename —
  goes through `set_steps`, so one line keeps the marker honest, and `apply_pipeline`
  re-stamps it straight after.
- `_select_pipeline` calls `discard_pipeline_steps()` when the picker moves off a pipeline
  whose marker is still set, and flashes a line saying so. Uploaded tables stay loaded —
  clearing steps is not Start over.

Steps the user has touched are still never thrown away, exactly as before.

**2. No Column details panel.**

Transform Data's preview shows a handful of rows, so "this column is 40% blank" or "these
numbers are stored as text" was invisible until a step went wrong. Added
`_render_column_details`, the same collapsed `profiling.column_stats` table Data Cleaner
shows, under every table's preview and computed over the whole table rather than the
preview rows. A profile that can't be computed hides the one panel and says so, rather than
taking the preview down with it.

Tests: `TestDeselectingAPipelineTakesItsStepsWithIt` (6) and `TestColumnDetails` (2) in
`tests/test_transform_data_page.py`. The Column details assertions read the profile table
itself rather than the expander around it — AppTest does not surface expanders nested
inside `st.tabs`.
