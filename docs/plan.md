# Stage 12 — Run a Task (requirement 8, build-order item 10)

Stage 11 built the recorder. This stage builds the player: pick a saved Task, upload this
month's files, and get the finished report out — **with no question typed and, in the happy
path, no provider call at all**, because every report item and every criteria came back from
storage carrying the SQL that produced it.

Its own page, `app_pages/run_task.py`, registered under **Automate** and available to **any
logged-in user** (requirement 8's first line), unlike Task Builder. A user only ever sees
their own Tasks — `tasks.db` scopes every read to `user_id`, and shared ownership is
explicitly out of scope (requirement 9).

## The four screens, in the order requirement 8.1 asks for

1. **Pick** — the saved Tasks as cards. Each has **Run** and **Show schema**, the dialog
   §8.1 step 2 asks for: the expected files, each column's type, the links and the column
   meanings, so the user knows what to upload before they upload it.
2. **Load** — the upload step (`setup_view.mount_upload`, the same widgets every other page
   mounts), then **Check schema**: `chat_types.matching.check_upload` measures the upload
   against `task.schema`, which is a `ChatType` for exactly this reason.
3. **Fix** — on a mismatch, the file/column/expected-vs-actual discrepancy with a **remap**
   beside it (§8.1 step 5), rather than an abort.
4. **Run** — replay, with a live stage-by-stage status (§8.2 step 7), then the summary of
   what succeeded / what needed the LLM fallback / what failed (§8.2 step 5), then the
   report preview and the two downloads (§8.2 step 6).

## The three decisions that shape everything else

**1. Remapping is a rename applied while the file is being read, never after.** The whole
reason `chat_types` applies saved types during the load is that whether a column converts is
a question about the *text in the file*, and a table already in DuckDB has had that text
converted once already. A remap has the same property: renaming a column after the load
leaves it typed by detection, which is the wrong-rows-no-error failure requirement 6.6
exists to prevent. So `engine.session.sync_tables` grows two optional maps —
`table_names` (`{table_id: forced name}`) and `column_renames` (`{table: {found: expected}}`)
— applied before `prepare_declared_table` sees the frame, and changing a mapping goes through
`engine.session.reload_uploaded_tables()` so the files are read again under it. That is the
identical mechanism `chat_types.session.select` already uses.

Table remapping is in scope even though §8.1 step 5 names only columns: a table's name comes
from its **filename**, so "salary_august.xlsx" against a Task recorded on "salary.xlsx" is
the first mismatch every user will hit, and telling them to rename their files would be a
worse answer than the one this stage exists to give.

**2. A run never touches the recipe.** The `Task` loaded from SQLite is held pristine in
session state; the replay works on a **deep copy** of it, so re-running is idempotent and a
second run cannot inherit the first one's rows, charts or rewritten comments. The report is
`skeleton.from_dict`'s tree with the items empty, and filling them by `source_id` is exactly
what `dashboard.session.pin_result`'s idempotency was built for — which is why `runner/`
fills them through `dashboard.model.find_item_by_source` and adds no new mechanism.

**3. The LLM is the fallback, not the path.** A replayed item runs its **stored SQL** first:
no provider call, no cost, no chance of the model answering a slightly different question
than the one the user saved. Only when that statement fails — a column renamed, a type
changed — does `generate_and_run` get a turn, and the step is recorded as `fallback` so the
summary can say so. This is the distinction §8.2 step 5 asks to be reported, and it only
exists because it is real.

## Steps

### 1. `checks/model.py` — the filter suffix comes home

`FILTER_SUFFIXES` and the heading it builds live in `app_pages/checks_view.py`, which imports
Streamlit. The replay needs them to answer "which of All / Failures / Passes was this
criteria pinned as?", recovered from the pinned heading — the `Check` never stored the mode,
and adding a field would migrate every saved Task for something already recorded. So
`report_heading(check, mode)` and `mode_from_heading(check, heading)` move into
`checks/model.py`, and `checks_view` imports the first of them. No behaviour changes.

### 2. `engine/` — renames at load time

`engine.loading.rename_columns(raw, renames)` (pure, one job), and `sync_tables`'s two new
keyword maps threaded through `_prepare_upload`. `engine.session.set_statements` too: a run
replays the **Task's** statement list, and the session's own must end up holding it so a
later rebuild replays the same one. `setup_view.mount_upload` passes both maps through.
`tests/test_engine_loading.py` gains the rename cases.

### 3. `runner/` package

Mirrors `checks/` and `report_items/` in shape — only `session.py` imports Streamlit.

- `exceptions.py` — `TaskRunError`.
- `model.py` — `StepResult` (kind, label, status, detail) and `RunResult` (the steps, the
  filled report, the counts the summary screen reads). Statuses: `ok`, `fallback`, `failed`,
  `skipped`.
- `replay.py` — requirement 8.2, in order, as one function taking an `on_stage` callback so
  the page can narrate it:
  1. links and column meanings restored (`chat_types.session.apply_setup`, which is already
     exactly this),
  2. the recorded calculated-column statements replayed **after** the rebuild, since the
     rebuild recreates the working tables from their base tables and would otherwise discard
     them,
  3. each report item and column step in **list order** (a column step changes what every
     item below it sees, so order is the content of the list), then each criteria,
  4. results written into the report skeleton by `source_id`; commentary and remarks
     rewritten under the Task's persona, falling back to the recorded wording when no
     provider is configured or the call fails,
  5. the criteria-set overview chart rebuilt if the skeleton holds one.
- `session.py` — the opened Task, the mapping, the run result, and `RT_REPORT_KEY`.

An item whose result could not be produced **stays in the report** carrying a note saying so,
rather than being dropped: a designed report with a point missing and no explanation is worse
than one that admits it, and the run summary names it either way.

`tests/test_runner_model.py`, `tests/test_runner_replay.py`.

### 4. `app_pages/report_view.py` — `render_report_output`

Preview and Download without Build. A run's arrangement came from the Task; offering the
structure editor would invite the user to edit a report that is regenerated wholesale on the
next run. Same two renderers, one new toggle, `rt_*` keys so it cannot collide with `db_view`.

### 5. `app_pages/run_task.py`

The four screens above. Follows `task_builder.py`'s hard-won page shape: a fixed top-level
slot count, the flash slot, the gate body / run body containers, and `UPLOAD_SLOT` holding
either the visible upload step or the hidden mount — the uploader must be instantiated on
**every** run or `sync_tables` drops every loaded table. `dashboard_session.use_report` is
declared at the top of the run, as every page that touches a report now does.

`tests/test_run_task_page.py`.

### 6. Registration and docs

`streamlit_app.py` adds the page to **Automate** for every logged-in user (the section then
exists for everyone; Task builder stays conditional). `app_pages/home.py`, and `CLAUDE.md`'s
Current phase.

## Verification

- `uv run pytest` — the whole suite. Steps 1 and 2 are additive, so the existing
  `test_checks_page.py`, `test_chat_with_data_page.py` and `test_engine_*` passing unchanged
  is the check on them. Two known pre-existing failures in `test_chat_with_data_page.py` are
  recorded in `CLAUDE.md`.
- Three tests worth naming, because they are where this stage can go quietly wrong: a replay
  with every stored statement working makes **no provider call at all**; a replay run twice
  produces the same report rather than a doubled one; and a failing statement is recorded as
  `fallback` when the regenerated one works and `failed` when it doesn't.
- End to end: log in, open Run task, pick the Task built in Stage 11, press Show schema,
  upload a file whose columns match but whose name doesn't, remap it, check, run, and
  download both formats.
- Verify Streamlit API use against the `developing-with-streamlit` skill after building.
