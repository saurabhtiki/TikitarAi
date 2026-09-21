# Phase 37 — Dashboard chat, made easier to drive

**Status: planned, not built.**

Seven small requests from using phase 36 on real data. None change how a dashboard is
described to the AI or how a round works - all seven are UI, one default-layout nudge to the
design rules, and one real bug fix (item 5).

## 1. Layout defaults: filters left, titles centered, dropdowns collapsed

Add three rules to `_DESIGN_RULES` in `live_dashboard/ai_spec.py` (the text already sent with
every round, tested against the catalog): filters default to the left column rather than the
top row, every panel title is center-aligned, and any dropdown/select filter renders
collapsed rather than expanded. Rendering side: `vega_spec.py` / the dashboard layout code in
`dashboard_view.py` needs to honor a left-column filter placement and center-aligned titles
(a CSS/Vega config change, not a new spec field) and the filter widgets need to default to
collapsed. No spec schema change - this is a default, not something the user can already ask
for differently per visual, so it applies globally.

## 2. Clear one filter, not just all

Find the existing "Clear all" control (filters live in `spec.filters()`, rendered in
`dashboard_view.py`/`vega_spec.py`/`runtime.js`). Add a small reset control per filter
alongside it, scoped to that one filter's current selection. Likely a per-filter "x" next to
the widget rather than a new button row.

## 3. Light vs. a stronger model

No change planned. Confirmed with the user: Describe/Revise only ever pick from a fixed
catalog of chart types, columns, and aggregations - a constrained, structured task the Light
Model already handles reliably, per the existing schema-safe name/value contract from phase
28. Revisit only if real use shows the Light Model misreading instructions.

## 4. Dashboard reachable from the Chat with Data tab

Flagged as a later item back in phase 32/33 ("AI generation, templates, drill-down,
pivot/map, and the Chat with Data entry are phase 33+"). Add an entry point - likely a tab or
button inside Chat with Data that renders the same saved `DashboardSpec` / preview already
built for Report Builder, reusing `dashboard_session` and `vega_spec` rather than a second
implementation.

## 5. Bug: Undo doesn't update "What was asked, and what changed"

Real bug, not a request. `session.undo_last_round()` (`live_dashboard/session.py:197`) pops
`LD_UNDO_KEY` and restores the spec, but never touches `LD_ROUNDS_KEY` - so the round that was
just undone is still sitting at the top of `rounds()`, and the expander in
`_render_rounds()` (`app_pages/dashboard_view.py:272`) keeps describing a change that no
longer exists on the page.

Fix: `undo_last_round` should also drop the round it's undoing from history (`rounds()[0]`),
the same way `record_round` prepends it. Rather than inventing a new "Undone" entry, popping
the entry keeps the history honest: it describes only rounds that are still reflected in the
dashboard on screen.

**And:** match Data Cleaner's step pattern (`app_pages/transform_data.py:317-389`) - each
step in a list with a **Remove** button next to it, "remove this step; to change an earlier
step, delete back to it." Reusing this pattern for dashboard rounds means each entry in
`_render_rounds` gets a "Cancel this round" affordance the same shape as Data Cleaner's,
rather than a single Undo button restoring only the very last round. This is a bigger change
than the bug fix (needs a per-round stash, not just the one-step `LD_UNDO_KEY`), so it should
land as a distinct, clearly-labelled step within this phase - worth confirming that scope
before building rather than assuming a one-step undo becomes a full history stack.

## 6. Text box → dialog on "Update the dashboard"

Currently `st.text_area` + button run in place (`app_pages/dashboard_view.py:144-183`). Move
the instruction box into an `st.dialog`, opened by the "Update the dashboard" / "Build the
dashboard" button, matching the dialog pattern already used elsewhere in this file (e.g. the
Apply/Cancel dialogs in `data_cleaner.py:331`). Same instruction text and help copy, just
collected in a dialog instead of inline.

## 7. Per-visual Edit button → dialog scoped to that one visual

Each panel in the preview gets a small Edit control. Clicking it opens a dialog pre-scoped to
that panel (title shown, instruction box for "what should change about *this* visual"), which
calls `ai_spec.revise_dashboard` with a `target` naming that panel's `panel_id` directly -
same mechanism phase 35 built for round edits (`action`/`target`), just invoked from a
specific visual instead of free text mentioning it by title. Reduces to "one more entry point
into revise_dashboard with the target pre-filled," not a new AI path.

## Open questions to settle before building

- Item 5's "Cancel this round" - confirm we want a full per-round undo stack (harder, more
  session state) vs. keeping one-step Undo but fixing the history-sync bug and adding a
  "removed" note. Recommend: fix the bug first (cheap, correct), then decide separately
  whether multi-step history is worth the extra state.
- Item 1's layout rules are global defaults - confirm no existing dashboards rely on top-row
  filters or left-aligned titles (a saved Task's spec doesn't encode layout today, so this
  should be safe, but worth checking `model.py` for any layout field before assuming so).

## Plan for Build/Test (once confirmed)

Build in the order above (1 → 7), each with its own test addition in the matching
`tests/test_live_dashboard_*.py` file, then `uv run pytest tests/ -k "live_dashboard"`.
Item 5's bug fix should land first and alone, since it's the one regression a user could hit
today.
