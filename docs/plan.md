# Phase 39 — Filters that reach every related table, and a proper starting point

**Status: built.** Phase 38's plan is in git history.

All four parts are in. What changed from the plan as written, and why:

- **`propose_dashboard`, `build_prompt` and `_build_one` lost their `main_table` argument
  entirely** rather than keeping an ignored one. A visual that names no table now goes to
  the table that owns the columns it names (`ai_spec._table_for`). A visual naming a table
  we do *not* have is still refused by name - redirecting it to whichever table happens to
  carry columns of those names would be exactly the silent wrong answer this module exists
  to prevent.
- **`flatten.detect_fact_table`, `load_side_table` and `FlattenPlan.side_tables` are gone**,
  along with `payload`'s `main_table` field: with every table flattened, nothing called
  them. `DashboardSpec.main_table` stays in the file format, documented as read by nothing.
- **The range widget is two stacked sliders**, not two on one track: overlaid native inputs
  leave only the top handle grabbable. `Math.min.apply` also went - an argument list of
  90,000 values (which `ROW_LIMIT` allows) throws a RangeError in several browsers.
- **A failed Generate puts the old panels back.** The panels are emptied on the way in so
  the round reads as a first draft, so a provider outage would otherwise clear the page.
- **The per-visual Edit buttons are hidden with no Light Model configured**, since the
  dialog they open is the round box.

Four things found using phase 38 on Employee Master + Salary + Attendance. Build in this
order: 1, 2, 3 (small, independent) then 4 (the big one).

## 1. Range slider with two handles (min and max)

Today a number filter has one slider ("from 30,000"). Make it a min and a max.

- `runtime.js` `buildFilterWidget`, `range` branch: two `input type=range` on one track (or two
  number boxes + two sliders if a native dual slider is fiddly). The rule already supports
  `{kind: "range", min, max}` — `matchesGlobal` needs no change; only the widget sends `max: null`.
- Readout: "30,000 to 60,000". Clear puts both handles back to the column's lowest / highest.
- Guard: min can never pass max (the moving handle pushes the other, or is clamped).
- Tests: `tests/test_live_dashboard_runtime.py` (Node) — a rule with both ends keeps only rows
  between them; a row with no value is dropped.

## 2. Undo per round, like the Data Cleaner

Today there is one stash (`LD_UNDO_KEY`) and the top Undo goes grey after one press.

- Keep a **stack**: each `Round` carries the spec JSON from *before* it (`Round.before_json`).
  This replaces `LD_UNDO_KEY`, `stash_for_undo`, `can_undo`, `discard_undo`.
- In "What was asked, and what changed", each round shows its own **Undo** button; only the
  newest is enabled, older ones are disabled with a help tooltip saying "Undo the newer ones
  first". Pressing it restores that JSON and drops the round (the flash note stays).
- The top Undo button is removed. `MAX_ROUNDS_SHOWN` already caps memory; rounds that fall off
  the end lose their JSON with them.
- A round that changed nothing is simply not recorded (same as `discard_undo` today).
- `remove_visual` records a round with its stash, so Undo covers it unchanged.
- Tests: `test_live_dashboard_page.py` — two rounds give two buttons, only the top enabled;
  undoing the top enables the next; a no-change round leaves no button.

## 3. "Generate Dashboard" is the starting point; it uses the Good model

- New primary button **Generate Dashboard** at the top of the tab, always visible. It builds
  from the *current* data, confirmed relationships and column descriptions (the `_column_notes`
  path already carries the dictionary).
- Uses the **Default model** = `llm_session.active_profile(user_id)` (the session's chosen model,), not `light_profile`. `revise_dashboard` already takes a
  profile dict, so only which profile is passed changes. If none is configured, say so and name
  Settings -> LLM providers.
- If the dashboard already has visuals, pressing it first asks **"This replaces everything,
  including your edits. Continue?"** — the same two-press session-flag idiom as Remove (a flag
  like `LD_CONFIRM_GENERATE_KEY`, Yes / No). Yes = clear spec panels, stash a round (so Undo
  still brings the old one back), then run the round with the Good model.
- **Update the dashboard** stays as it is, on the Light Model, and shows only once panels exist.
  Edit-this-visual also stays Light.
- The old "Build the dashboard" label goes away (that was the same button before panels existed).
- Tests: no model call until Yes when panels exist; the profile passed is the active one, not
  the light one; Undo after Generate restores the old page.

## 4. Filters that reach every related table (the main fix)

**Problem.** `_build_data` flattens only **one** table (`spec.main_table`, or the one that points
at the most others) with the parents it can reach child -> parent. In Employee Master (parent),
Salary and Attendance (children): with Salary as main, Attendance is a sibling the walk never
reaches, so it is embedded on its own with no Department column and a Department filter does
not touch it. (Worse, `matchesGlobal` treats a missing column as "no match", so it can even
empty a table.)

**Design.** Flatten **every table on its own**, each decorated with the parents it can reach
(reuse `join_plan` + `flatten_main_table` once per table; the join direction is unchanged, so
row counts never multiply). No table is ever joined to a sibling.

- `_build_data`: loop over all `table_names`; `tables[name] = flatten_main_table(connection,
  join_plan(name, relationships, table_names))`. The `GRAIN_SAFETY_MARGIN` guard stays per
  table. Signature drops `main_table`. `side_tables` / `load_side_table` become unnecessary for
  linked tables; a table with no links is just a plain one-table plan.
- **One name for a parent's column everywhere**: `Employee Master - Department`. A child gets
  it by the join; the parent table itself also carries its own columns under that prefixed name
  (a duplicate column, cheap) so the same filter narrows Employee Master rows too. Guard against
  a clash with `_safe_frame` as today.
- `runtime.js`:
  - `matchesGlobal(record, tableName)`: **skip** a rule whose column that table doesn't have
    (use `tableMeta(name).columns`), instead of failing the row. This is the line that makes
    "filter Department = HR" reach Salary *and* Attendance while leaving a Budget table alone.
  - The cross-filter (click a bar) gets the same skip.
  - Filter options (`distinctValues`) and the range's min/max are read from the filter's own
    `source_table`, which the AI/Model picks as the parent's prefixed column - unchanged.
- **Main Table picker goes** from the UI (`dashboard_view.py` ~l.628-641). `DashboardSpec.
  main_table` stays in the file format so an older Task opens; it is ignored, and each visual
  keeps its own `source_table` (defaults to the table that owns the column).
- **Charts stay one table each.** A chart on Salary can use `Employee Master - Department`
  (it is on Salary's rows). A single chart mixing Salary's amount with Attendance's days is
  still not possible — `panel_problems` already reports it in words; the AI prompt is told to
  put those in two visuals. Cards on different tables are fine and both react to filters.
- `ai_spec.py`: drop "The main table is X. Prefer it" (l.~696); describe every table and its
  columns, note that parent columns (`Employee Master - ...`) are available on each child table
  and a filter on one applies to all tables that carry it. `_DESIGN_RULES` gets one line to
  that effect, and a test keeps the prompt and the catalog in step as before.
- `help.py`: "What can I ask?" gains one sentence about filters across related tables.
- Data description line (`plan.describe`) becomes one sentence per table, e.g. "Salary: with
  columns from Employee Master. Attendance: with columns from Employee Master."

**Risks and honest limits**
- Two parents feeding one child through two paths (diamond) — first path wins, as today.
- A many-to-many link (no clean parent) is still not flattened; the filter just doesn't reach it
  and a warning above the preview says which table was left out.
- Payload gets bigger: every table is embedded once, each with its parent columns. Watch size on
  large tables; keep the existing size warning.

**Tests**
- `test_live_dashboard_flatten.py`: star, 3-level hierarchy, one parent with two children, a
  child with no links — row counts equal the raw table's, and each child carries the parent's
  prefixed columns.
- `test_live_dashboard_runtime.py` (Node): a Department rule narrows Salary *and* Attendance,
  leaves a table without that column untouched, and a click cross-filter does the same.
- End to end with Employee Master 2 rows / Salary / Attendance: Department = HR gives HR's
  salary total and HR's attendance total together.

## Done when
`uv run pytest` is green, and by hand: Generate -> filter Department -> both salary and
attendance charts move; two rounds -> two Undo buttons, only the newest live.
