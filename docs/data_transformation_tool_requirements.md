# Data Transformation Tool — Requirements Document

## 1. Overview

A Streamlit web application that lets users upload Excel/CSV datasets, define data
transformation steps (via a structured UI and/or plain English), save those
steps as a reusable **pipeline**, and re-run the saved pipeline automatically on future
uploads to produce the same desired output without redefining steps each time.

---

## 2. Core Design Principles

1. **Structured operations are the source of truth.** A fixed, extensible catalog of
   pandas-based operations (drop_column, add_calculated_column, groupby_aggregate, etc.)
   drives everything. No arbitrary/free-form code execution.
2. **Plain English is a convenience input**, not a separate mechanism. Natural-language
   instructions are parsed by AI into the same structured operation format (constrained
   to real column/dataframe names and the fixed operation catalog), then shown to the
   user for confirmation before being added to the pipeline.
3. **AI is only used at pipeline-definition time.** Saved pipelines are deterministic
   JSON specs that replay directly on new uploads — no AI/LLM call needed at run-time.
4. **Multi-step decomposition for complex requests.** A single complex instruction
   (e.g., "average holding period per stock item") is decomposed by the AI into an
   ordered sequence of catalog operations, where each step's output can feed the next.
   The decomposed steps are shown to the user as editable/confirmable steps before saving.
5. **Custom formula/code is a sandboxed fallback only**, used when no catalog operation
   fits. Must run in a restricted evaluator (no `exec`/`eval` on arbitrary code, no
   file/network/import access) and is clearly flagged as "Advanced."
6. **Preview before commit.** Show a live data preview after each step and after the
   full pipeline runs, both during pipeline creation and during pipeline reuse.
7. **Schema metadata is saved with every pipeline** and validated against new uploads
   before execution (see Section 6).
8. **Editing is last-step-only (v1 decision).** To avoid dependency-tracking complexity,
   only the most recently added step in a pipeline can be edited or deleted. Editing an
   earlier step requires deleting back to it and re-adding subsequent steps. (Documented
   trade-off — may be revisited in a future version with in-place step editing and
   downstream dependency checks.)

---

## 3. Multi-Dataframe Support

The tool must support **more than one named dataframe** in a workspace, not just a
single dataframe flowing linearly through steps. This is required for summary tables,
merges, and appends.

- **Upload step** supports multiple files; each file becomes a **named dataframe**
  (auto-named from filename, user-editable). If two files share the same base name,
  auto-suggest `n1`, `n2`, `n3`, etc. as the dataframe name (user-editable).
- **Output dataframe names must be unique.** If a user tries to save a new named
  dataframe with a name that already exists, block it and show a message asking the
  user to choose a different name.
- **Every operation** operates on a selected source dataframe (or dataframes, for
  merge/concat) and has an **"Output as"** setting:
  - Update the source dataframe in place (default for column-level ops: drop, rename,
    fillna, calculated column, etc.), or
  - Save as a **new named dataframe** (default for groupby_aggregate, merge, concat,
    pivot).
- Later steps can reference **any** named dataframe created so far in the pipeline, not
  just the most recent result.
- At the end of the pipeline, the user selects which named dataframe(s) are the final
  desired output(s) for export/download.

### Example flow
1. Upload `sales_data.xlsx` and `customer_master.xlsx`
2. `merge`: sales_data + customer_master on `customer_id` → output `merged_data`
3. `add_calculated_column` on `merged_data`: `bonus = basic * 0.12` (in place)
4. `groupby_aggregate` on `merged_data`: group by `customer`, sum(amount), avg(bonus)
   → output `customer_summary`
5. Final export: user selects `customer_summary`

---

## 4. Operation Catalog

### 4.1 Initial scope (v1 — broad catalog, so most requests are covered without
falling back to custom_formula)

| Category | Operations |
|---|---|
| Column Operations | drop_column, rename_column, reorder_columns, change_dtype |
| Clean Data | fillna, dropna, drop_duplicates, strip_whitespace, replace_value |
| Filter & Sort | filter_rows, sort_rows |
| Calculated Fields | add_calculated_column (arithmetic + multi-column + basic conditional/if-else logic) |
| Aggregate & Summarize | groupby_aggregate (min/max/mean/sum/count), pivot |
| Combine Data | merge/lookup (join, incl. a friendlier "lookup" wrapper), concat (append) |
| Reshape | melt/unpivot, transpose |
| Date Handling | extract date part, date difference, add days/months, fiscal year/quarter, age in years |
| Ranking & Running Values | rank within group, running/cumulative total, row number within group, top-N per group |
| Text Operations | uppercase/lowercase, trim, split column, contains/starts-with filter, find & replace |
| Number Formatting | round to nearest X, round up/down, percentage of total |
| Grouping/Binning | bucket a numeric column into ranges (e.g. price bands, age groups) |
| Advanced | custom_formula (sandboxed fallback — see 4.3) |

Additional operations can be added later based on real usage — architecture must support
this without redesign (see 4.2).

### 4.2 Architecture: registry-driven, not hand-coded per operation

- **Operation registry**: SQLite database table mapping operation name →
  {required parameters, parameter types/UI widget hints, category, execution function}.
- **Generic executor**: one engine that reads a pipeline step (operation name +
  parameters) and looks up/runs it via the registry — a single code path for all
  operations, not per-operation branching logic.
- **Generic form renderer (Streamlit UI)**: one reusable form-rendering component that
  reads an operation's parameter definitions from the registry and automatically renders
  the right widgets (column-picker dropdowns populated from actual uploaded schema, text
  inputs, fixed-choice dropdowns, dataframe selectors, etc.). Adding a new operation
  should require only a new registry entry + a small execution function — not new UI code.
- **Operation picker UI**: grouped by category (as in table above) rather than one flat
  list of 70+ items, to keep the UI approachable as the catalog grows.

### 4.3 custom_formula: what it is and what it is not

- **AI is restricted to the catalog only.** AI never writes free-form pandas/Python
  code. It only selects and arranges existing catalog blocks (possibly several in
  sequence, per Section 2.4) to satisfy a request. This applies even to requests that
  sound complex — e.g. "weighted average rate per stock item" decomposes into
  add_calculated_column (value = rate × qty) → groupby_aggregate (sum value) →
  groupby_aggregate (sum qty) → add_calculated_column (divide) — no custom code needed.
  Cross-dataframe lookups (e.g. "fetch standard cost per stock item from another
  sheet and deduct it") likewise decompose into merge/lookup → add_calculated_column,
  not custom_formula.
- **custom_formula is only for simple, row-level math/logic that doesn't fit any
  catalog block** — e.g. "round the invoice amount up to the nearest 100." It behaves
  like a single Excel cell formula: it can only use values from columns in that same
  row, plus a small allowed set of operations (numbers, +−×÷, round, and similar).
  It must run in a restricted evaluator (e.g. `asteval` or an equivalent whitelist
  parser) — no `exec`/`eval` on arbitrary code, no loops, no file/network/import
  access, no access to other rows or other dataframes. Clearly flagged as "Advanced"
  in the UI.
- **If a request needs a genuinely new type of operation** (not row-level math, and
  not an existing catalog block — e.g. a reshape or grouping operation the catalog
  doesn't have yet), the app must **not** attempt it via custom_formula or ad-hoc AI
  code. Instead, show a clear message (e.g. "This operation isn't available yet —
  please try a supported operation, or contact support to request it.") and stop.
  Since the catalog is registry-driven and extensible (4.2), such gaps get closed by
  adding a new, properly tested catalog block later — not by generating one-off code
  at run-time.

---

## 5. Instruction Input Methods

Two ways to add a step, both converging to the same structured, saved format:

1. **Structured picker (primary path)**: user selects operation from the categorized
   catalog, selects source dataframe(s)/column(s) from real schema via dropdowns, fills
   operation-specific parameters via the generic form renderer, sets output behavior
   (in place vs. new named dataframe), previews result, confirms.
2. **Plain English (convenience path)**: user types a natural-language instruction,
   optionally referencing dataframe/column names explicitly for higher accuracy (e.g.,
   "add calculated column bonus = salary * 0.12"). AI parses this into:
   - a single structured operation, or
   - if complex, a decomposed ordered sequence of structured operations
   
   constrained to the real schema and fixed operation catalog. Parsed result is
   displayed back in the structured/editable form for user confirmation before being
   added to the pipeline. AI must not reference or invent columns/dataframes that do
   not exist in the current schema.

---

## 6. Schema Metadata & Validation

- When a pipeline is saved, store metadata **per named input dataframe**:
  - Expected file/dataframe name
  - Expected column names and dtypes
  - Any relevant constraints (e.g., non-null requirements) used by the pipeline
- **Validation rule: required columns present, not exact match.** The uploaded file
  must contain every column the pipeline's steps actually use (right name, compatible
  dtype). Extra columns not used by the pipeline, and columns in a different order,
  are allowed and ignored — only a genuinely **missing required column** blocks
  execution.
- On future use of a saved pipeline:
  1. User selects the saved pipeline
  2. User uploads new file(s)
  3. App validates each uploaded file has all required columns (per above) against the
     corresponding saved metadata
  4. If a required column is missing → block execution, show a clear error (e.g.,
     "Pipeline expects column 'basic_salary' but uploaded file has 'basic salary'")
  5. If valid → proceed to execute all saved steps in order, deterministically, without
     any AI call

---

## 7. Pipeline Editing Rules (v1)

- Steps are shown as a numbered, ordered list.
- Only the **last step** in the list can be **edited** or **deleted**.
- Editing the last step reopens its structured form pre-filled for modification.
- Deleting the last step removes it; the new last step (previously second-to-last)
  becomes editable/deletable.
- To change an earlier step, user must delete back to that step (removing all
  subsequent steps) and re-add steps from there.
- No automatic dependency tracking or downstream-break detection is required in v1.

---

## 8. End-to-End Flow

### A. Pipeline Creation
1. User uploads one or more Excel/CSV files → each becomes a named dataframe
2. App displays schema (columns, dtypes, sample rows) for each dataframe
3. User adds a step via structured picker or plain English
4. If complex/multi-step, AI decomposes into multiple ordered operations
5. Parsed/selected step(s) shown in structured, editable form for confirmation
6. User confirms → step(s) appended to pipeline (subject to last-step-only edit rule)
7. App shows data preview after the step
8. Repeat steps 3–7 as needed
9. User selects final output dataframe(s) for export
10. User saves the pipeline: name + ordered steps + expected schema metadata per input,
    stored in a SQLite table (same storage pattern already used for report-saving
    elsewhere in the app)

**Preview row limits:** data previews during pipeline creation/reuse show up to 500
rows (same pattern as phase 16's report tables); the full dataset is always used for
execution and for the final export/download.

### B. Pipeline Reuse
1. User selects a saved pipeline
2. User uploads new file(s)
3. App validates uploaded file(s) against saved schema metadata
   - Mismatch → error, block processing
   - Valid → proceed
4. App executes saved steps in order, deterministically (no AI call)
5. App shows final output(s); user downloads result
