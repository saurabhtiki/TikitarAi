# Phase 40 — Choose the model a round runs on, and a drill-down table

**Status: built.** Phase 39's plan is in git history.

Two things the dashboard was missing, both small and both additive. What changed from the
plan as written, and why:

- **The toggle names the model in the caption, not in its own label.** "Use my model instead
  of the Light Model" stays the same sentence whichever provider is configured, and the
  caption right below it already had the job of saying who reads the change.
- **A drill-down needed no new `PanelSpec` field and so no migration.** `source_columns` is
  read as the *levels*, `measure_column` + `aggregation` as the number totalled on each.
- **The drill-down's arithmetic is three plain functions** (`drilldownGroups`,
  `drilldownRows`, `toggleDrilldownRow`) rather than one renderer, so the totals a reader
  would check by hand are run in Node by pytest instead of being asserted as source text.
- **A drill-down has no search box.** It shows totals per group, not the rows a search would
  look through, so the template leaves the input out rather than drawing a control that
  finds nothing.

## 1. A toggle to pick the round's model

Rounds have been on the Light Model since phase 35, which is right for "make it horizontal"
and wrong for "redesign the bottom half". Generate Dashboard already used the session's own
model; this gives rounds the same choice without taking the cheap default away.

- `app_pages/dashboard_view.py::_round_model` draws `st.toggle` beside the caption above
  **Update the dashboard**, off by default, and returns the profile the round should use.
- Shown only when there is a second model to switch to. `llm_session.session_profiles`
  already excludes the Light Model, so a user whose only provider is the light one has no
  active profile and sees no toggle — a switch with one position is a control that lies.
- The chosen profile flows into `_render_open_dialog`, `_dialog_ask`, `_dialog_edit_visual`
  and `_run_round`, so **both** dialogs move together: the per-visual Edit box is the same
  round with "which one do you mean" already answered, and a toggle that moved only one of
  them would be a setting that half applies.
- `revise_dashboard` already takes any profile dict, so nothing in `ai_spec` changed.
- Not saved into the Task (`session.LD_USE_ACTIVE_MODEL_KEY` is a plain widget key): which
  model answered is a choice for this sitting, not a property of the dashboard.

## 2. Drill-down tables

A second table style: rows grouped into collapsible layers with a total on each, the way an
Excel pivot table's row grouping works — Category, then SubCategory, then Item.

- `model.py`: `TABLE_DRILLDOWN`, `TABLE_SUB_TYPES`, `TABLE_LABELS`, `MIN_DRILLDOWN_LEVELS`
  (2) and `MAX_DRILLDOWN_LEVELS` (4). `panel_problems` gains a drill-down branch: at least
  two levels, at most four, every level a real column, and the same measure/aggregation
  check a card gets — plus a refusal of `percent_of_total` and `running_total`, which are
  measured against a chart's other bars and have no meaning over a tree of groups.
- `ai_spec.py`: one `_CORE_RULES` bullet (`columns` are the levels, outermost first), one
  `_DESIGN_RULES` line (drill rather than list where the data has a hierarchy),
  `_FIELDS_BY_KIND` lets a table carry `measure_column` and `aggregation` so a round can see
  its own drill-down, `describe_panel` reads *"drill-down table of Sum of Amount by Category
  -> SubCategory"*, and the "that column isn't a number" guard now covers drill-downs too.
- `runtime.js`: `renderTable` branches on `sub_type` so it stays the one entry point every
  caller already uses. `drilldownGroups` builds the tree with the existing `aggregate`;
  `drilldownRows` flattens it into printing order with each row's depth and parent;
  `renderDrilldownTable` builds the rows once and `toggleDrilldownRow` opens and closes them
  by CSS class rather than redrawing. Top level open, everything under it closed. Clicking a
  leaf cross-filters on its own level, like a flat table's row.
- `html_export.py` carries `measure_label` and `sub_type`; the template drops the search box
  for a drill-down; `dashboard.css` adds the indent, the arrow and `.is-hidden`.
- `help.py` lists the two table styles and says plainly that a drill-down has no search or
  sort.

**Known limits (v1)**
- No search and no sorting on a drill-down; a flat table keeps both.
- Groups are ordered by label, not by size. A pivot table does the same: the reader is
  looking a category up, not reading a leaderboard.
- `DRILLDOWN_ROW_LIMIT` caps the page at 2,000 group rows, and the note under the table says
  when the cap bit.

## Done when
`uv run pytest` is green, and by hand: turn the toggle on and the caption and spinner name
your own model; ask for *"a drill-down table of sales by category and subcategory"* and it
appears, opens and closes, and its totals match the same numbers on a card.
