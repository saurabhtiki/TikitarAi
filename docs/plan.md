# Stage 7 — Pin to Dashboard, the Dashboard page, HTML + Excel export

Covers requirements §6.1 steps 3–4 (pinning into an unplaced pool), §6.3 (the Dashboard
page) and §6.4 (HTML and Excel export). Build-order item 6, and the half of it Stage 6
deliberately left undone.

Stage 6 finished the agent: questions became SQL, SQL became a frame, and the frame became
a chart, a table or written commentary. What it left was that **the outputs had nowhere to
go** — the pin button shipped visible but disabled so the chat layout wouldn't move this
stage. This phase gives it a destination.

**Out of scope:** Task Builder (§7), PDF/Word export (§6.4 rules both out), and any
database persistence of a dashboard. Per §6.3 the report dies with the session; the page
says so rather than leaving the user to find out by refreshing.

---

## The constraint everything else follows from

`analyst/session.py` caps the transcript at `FULL_PAYLOAD_MESSAGES = 12` and
`_trim_payloads` nulls the `frame`, `figure`, `choices` and `style` of anything older. A
pinned item that referenced a `ChatMessage` would therefore quietly empty itself a dozen
questions later — the user would arrange a report and find half of it blank.

So `dashboard.session.pin` **copies** the frame into a new `PinnedItem` and never looks at
the transcript again. `test_chat_with_data_page.py::test_a_pinned_frame_is_a_copy_not_the_transcripts_own`
pins an answer, releases the message's payload, and asserts the pinned frame is still
there.

The second thing that changed on the chat page: the pin button used to be gated on
`frame is not None or figure is not None`, which put commentary-only answers out of reach.
§6.1 step 3 says *every* chat output, and commentary is one of §6.2's three types, so the
gate is now "any assistant message that isn't an error".

---

## New package `dashboard/`

Mirrors `auth/` · `llm/` · `cleaner/` · `engine/` · `analyst/`: one `session.py` holding
all Streamlit state, one `exceptions.py`, everything else pure and unit-testable.

| File | Contents |
|---|---|
| `exceptions.py` | `DashboardError` → `ReportExportError`, `StyleValidationError`. |
| `model.py` | The tree dataclasses and every pure operation on it. No Streamlit. |
| `session.py` | The `db_*` namespace: the report, the CSS, the dialog flag. The only Streamlit-coupled module. |
| `images.py` | `figure_to_png` — Plotly → PNG via kaleido, cached per item, never raises. |
| `css_presets.py` | The three presets of §6.4 plus `validate_css`. |
| `html_export.py` | `build_html(report, css) -> str`. |
| `excel_export.py` | `build_report_workbook(report) -> bytes`. |
| `templates/report.html.j2` | The single Jinja2 template. |

### Three decisions in `model.py`

- **Items live in subsections only.** `add_section` auto-creates a `"General"` subsection,
  so the user is never blocked on inventing a second name and §6.4's one-sheet-per-
  subsection mapping is a fact rather than a judgement call.
- **Numbering is derived, never stored.** `numbered_sections` computes `"1"` / `"1.1"`
  from list position, so a reorder renumbers for free and no two nodes can claim `2.1`.
  §7.1 needs the same rule and will reuse it.
- **One `move(items, index, target)` serves all three levels.** Reordering sections,
  subsections and items is the same operation on a list. The Up/Down buttons call it with
  `index ± 1`; the position-jump box calls it with the number typed; the target is clamped,
  so Up on the first row is a no-op rather than a wrap-around. `assign_item` handles the
  cross-parent case, covering both "place from the pool" and "move to a different
  subsection" with one function.

---

## UI/UX

### Pinning stays one click — and only one

No dialog, no prompt — §6.1 step 3 is explicit that the chat flow must not be interrupted.
The heading defaults to the question, so a pinned tile is already labelled before the user
opens the Dashboard.

Once pressed, the button reads **Pinned to Dashboard** and is disabled, and `pin()` returns
the existing item rather than making a second copy. A live button after a successful press
reads as "press me again", and every extra press used to put another identical tile in the
pool. The state is `ChatMessage.pinned_item_id` *plus* the item still being in the report,
so discarding the copy on the Dashboard genuinely un-pins the answer. It is wired through
`on_click` rather than the button's return value, so the label flips on the same rerun the
click causes rather than on whatever the user presses next.

### `app_pages/dashboard.py`

Registered in the existing **"Explore"** group directly after Chat with data, with no
`require_role` guard (§2.2 grants it to every role, as `chat_with_data.py` already does).
Title box at the top, then three views on one `st.segmented_control`:

**① Build** — a two-column split, the layout that makes assignment cheapest.

*Left, the pool.* One bordered container per unplaced item: a type icon (chart / table /
text), the heading, a one-line summary, and a `st.selectbox` listing every subsection as
`"1.1 By region"`. **Two clicks place an item** — pick, press Place. **Look** opens it full
size in a dialog, **Discard** throws it away. Empty state names the exact gesture that
fills it and offers a button straight to the chat.

*Right, the tree.* Sections are expanders, subsections bordered containers, items bordered
containers inside those. Names are inline `st.text_input`s rather than a rename dialog —
one gesture instead of three. Every row carries the same reordering controls, so the
interaction is learned once and works at all three levels §6.3 asks to be reorderable:

- **Up** / **Down**, disabled at the ends so the boundary is visible rather than silent
- a **position-jump** box, appearing only once there are more than two siblings and it
  starts to beat pressing Up repeatedly
- **Delete**, which returns any items inside to the pool rather than destroying them, and
  says how many

Per item: a heading box, a comment box, **Move to** a different subsection, **Look**, and
**Unplace**. The outputs themselves are *not* drawn in the tree — a dozen full-size Plotly
charts is unusable as a structure editor.

**② Preview** — the report top to bottom, walked through the same `model.walk(report)`
both exporters use, so what is previewed cannot drift from what downloads.

**③ Download** — the preset picker (**Clean** / **Corporate** / **Compact**), an optional
hand-edit behind an expander, the two buttons, and the finished HTML in an iframe beneath
them. The two previews answer different questions: ② says whether the *report* is right,
③ says whether the *stylesheet* is, which is the only thing the presets are for. An iframe
rather than `st.html` because the report carries a whole page's stylesheet — `body`,
`table`, `h1` — which injected into the app would restyle the app itself.

### The comment is the chat's own, and the user's to edit

The comment box opens holding the written answer the chat already produced for that
question, and nothing regenerates it. A **Generate comment** button was built and then
removed: `analyst.commentary` had already written a comment for every answer by the time it
was pinned, so the button offered a second model call to replace text the user had in front
of them. **The Dashboard now makes no model calls at all** — it arranges what the chat
produced — which is why the page has no LLM profile lookup and its tests need no model
seam.

---

## Export

### HTML

One self-contained file. Jinja2 walks the tree, the stylesheet goes in a single `<style>`
block, charts are base64 `data:` images, frames are real `<table>`s with **no row cap**
(§7.3: on-screen output may be capped, exports never are).

Autoescaping is on and exactly two values reach the page unescaped, both accounted for:
the stylesheet, which `validate_css` screens, and the table markup, which pandas generates
with `escape=True` so every cell and column name inside it is already escaped. Verified:
the smoke report contains no `http` substring at all.

### Charts to PNG

`kaleido` joins `pyproject.toml`. `figure_to_png` returns `None` on any failure and every
caller treats `None` as "no image" — the HTML emits the item's table plus a plain notice,
the workbook writes the table without the picture. **An export never fails because a chart
couldn't be rasterized.** This is the one place `except Exception` is right: the failure
surface is a subprocess launching a headless browser, and the exception types kaleido
raises are neither documented nor stable across versions, so narrowing would reintroduce
exactly the failure this function exists to prevent. PNG bytes are cached on the item, so
downloading both formats rasterizes each chart once.

A chart is re-laid-out before it is rendered, on a copy so the one on screen is untouched.
`analyst.charts` draws with `margin={"l": 10, "r": 10, "t": 55, "b": 10}`, which is right
in a browser that grows those margins to fit whatever the axes need; kaleido renders once
at a fixed size and gives the labels no such room, so an exported chart ran its y tick
values off the left edge and printed the x axis title through the category names.
`_prepared_for_export` sets floor margins, turns `automargin` on so long labels still push
them out, and pins the background white with dark text — the export is a white page in
both formats, and a chart drawn under a dark theme otherwise carried pale axis text onto
it.

### Excel

One sheet per subsection, plus a leading **Contents** sheet listing the title and every
section/subsection against its sheet name — a tab strip loses the numbering past a handful
of sheets. Within a sheet each item reads heading → chart → table → comment.

Sheet names are the numbered subsection labels put through
`cleaner.naming.sanitize_sheet_names`, and Excel's limits and column widths come from
`cleaner.export`. Those three helpers lost their underscore prefix to be importable:
two implementations of "what Excel accepts" is one more than can be kept in agreement.

### Stylesheet validation (§6.4's "accept only after validation")

`validate_css` is deliberately not a CSS parser — a misspelled property renders fine with
that rule ignored, so rejecting it would be worse than accepting it. It catches the two
things that actually break the export:

- **structural damage** — unbalanced braces, unterminated comment or string, each of which
  swallows the rest of an inlined stylesheet and the report with it
- **anything reaching off the page** — `@import`, `url(http…)`, `<script`, `expression(`,
  `javascript:`, `@charset`, plus a size cap

A rejection lists every problem in plain English and **leaves the previous stylesheet in
force**, so a broken edit costs the change and never the download.

---

## Files touched

| File | Change |
|---|---|
| `dashboard/` (new: 7 modules + template) | as tabled above |
| `app_pages/dashboard.py` (new) | the page |
| `app_pages/chat_with_data.py` | pin button enabled, gate widened, pins once; per-table Remove; uploader detach; Start over clears the report too |
| `analyst/session.py` | `ChatMessage.pinned_item_id`, an opaque string that stops a second pin |
| `engine/session.py` | `EngineTable.uploader_managed`, `detach_uploader_tables`, `remove_table`, the dismissed-id set |
| `streamlit_app.py` | page registered in the "Explore" group |
| `cleaner/export.py` | `check_limits` / `column_width` / `write_sheet` made public for reuse |
| `pyproject.toml` | `jinja2` declared directly, `kaleido` added |

`analyst/session.py` gained a field but no import: clearing the dashboard on Start over
still happens at the page, the same arrangement `chat_session.reset_chat()` already uses,
so no package below the page layer knows `dashboard/` exists.

## Surviving a page switch

Requirement 6.3 gives the Dashboard its own page, which makes leaving Chat with Data and
coming back an ordinary thing to do — and it destroyed the session. Streamlit drops a keyed
widget's value the moment that widget stops being rendered, and `st.file_uploader` is the
one widget with no `persist_state` to opt out. So the uploader came back empty,
`sync_tables` read that as "the user removed every file", and the tables, the links, the
column descriptions and the whole transcript went with them.

`detach_uploader_tables` runs before the widget is built and takes the loaded tables out of
the uploader's reconciliation whenever the uploader's key is missing from session state —
which is exactly the signal that its value was dropped, and on a genuinely new session is a
no-op because nothing is loaded. Detached tables stay loaded and queryable; the uploader is
purely additive from then on. Removing one is then the per-table **Remove** button, which
also works for tables adopted from the Data Cleaner (they never had an uploader entry) and
records the table_id so the still-populated widget cannot reconcile it straight back in.

The two `st.segmented_control`s that pick a view — `de_view` and `db_view` — take
`persist_state="session"` for the same underlying reason, so returning to a page lands
where it was left rather than back at step one.

## Reused, not rebuilt

`analyst.charts`' figures · `cleaner.naming.sanitize_sheet_names` · `cleaner.export`'s
limits and column widths · `engine.session._drop_removed` for the per-table Remove · the
`open_dialog`/`close_dialog`/`pending_dialog` idiom and the `on_dismiss` handler from
`engine/session.py` and `chat_with_data.py` · the download-button shape from
`data_cleaner.py` · `st.switch_page` for the cross-page jump.

---

## Verification

`uv run pytest` — **825 passed**, plus one pre-existing failure carried in from Stage 6
(`test_a_collapsed_step_does_not_run_its_body`), confirmed independent of this work by
re-running it against the unmodified page.

- `test_dashboard_model.py` (26) — move at each level and at both ends, position clamping,
  assign from the pool, move between subsections, deleting a container returns its items,
  numbering after a reorder.
- `test_dashboard_css.py` (23) — every preset validates; braces, comments, quotes,
  `@import`, `url(http…)`, `<script`, `expression(`, `javascript:`, `@charset` and an
  oversized blob each rejected; braces inside a comment don't count; a `data:` URI is still
  allowed.
- `test_dashboard_html_export.py` (14) — one `<style>` block, a base64 `<img>`, every row
  of a 250-row frame, escaped headings and cell values, no `http` anywhere, a `None` PNG
  substituting the notice, and one rasterization across two builds.
- `test_dashboard_excel_export.py` (14) — one sheet per subsection plus Contents, names
  sanitized/truncated/de-duplicated, a subsection called "Contents" not displacing it,
  500 rows written, a picture embedded, a row-limit breach refused before writing.
- `test_dashboard_page.py` — every role reaches it; empty states; place, unplace, discard;
  rename; delete returning items; reorder disabled at the ends; the position box appearing
  only when it earns its space; the comment box arriving with the chat's own answer and no
  Generate button anywhere; the Look dialog opening, closing, and not rearming itself;
  both downloads and the HTML preview absent while empty, present once placed.
- `test_chat_with_data_page.py` (extended) — the pin button live, present on a
  commentary-only answer, absent on an error and on the user's own message; one click
  adding exactly one pool entry; two clicks still adding one; the button reading "Pinned"
  and going disabled; discarding the copy offering it again; the pinned frame surviving
  `release_payload`; pinning not re-answering; Start over clearing the report. Plus
  `TestSurvivingNavigation` and `TestRemoveTable` for the page-switch fix — a page switch
  simulated by deleting the uploader's session-state key, which is precisely what Streamlit
  does.

Rasterization is stubbed in every test — it launches a headless browser — and was
exercised by hand instead: a real Plotly figure produced an 80 KB PNG, a 109 KB
self-contained HTML with **no** `http` substring anywhere in it, and a 57 KB workbook. The
before/after of the margin fix was checked by looking at both renders.

### Known gaps

- `test_a_collapsed_step_does_not_run_its_body` (Stage 6) fails on the installed Streamlit:
  a collapsed `st.expander`'s `.open` reports True, so a collapsed step still runs its
  body. A performance question, not a correctness one, and untouched by this phase.
- `AppTest` can't read the bytes behind a download button (recorded in
  `test_data_cleaner_page.py`), so what the exports *contain* is asserted against the pure
  builders and the page tests only check the buttons appear. `st.iframe` likewise has no
  typed `AppTest` element, so the HTML preview is asserted through its caption.
- `AppTest` has no way to dismiss an `st.dialog` natively (clicking outside it, its X,
  `ESC`), so the `on_dismiss` handler that clears the flag on those paths is not directly
  covered — only the Close button path is.

---

# Addendum — Summarise, Pivot and Unpivot in the Data Cleaner

Added after Stage 7 at the user's request, ahead of Stage 8. Not a phase of its own: it
extends an existing page rather than opening a requirement.

Every cleaning action so far rewrote a table in place and kept its grain. These three
change the grain, so they **save a new table** instead of recording a step. The saved
summary becomes its own tab, its own worksheet in the download, and its own queryable
table in Chat with Data.

## The one decision everything follows from

A summary is a plain `TableState` with `derived_from` set, not a type of its own. That is
what lets `build_download`, `naming.sanitize_sheet_names`, `tab_labels` and — the big one —
`engine.session.adopt_cleaner_tables` carry it with essentially no code of their own. The
whole engine-side cost was one line: adoption resolves declared types through
`cleaner_session.effective_steps` rather than `.steps`.

`effective_steps(table)` is where the linkage lives: a summary's recipe is its **parent's
current steps** plus its one reshape, resolved on every call rather than frozen at save
time. Because `_cached_cleaned_table` is keyed on that resolved list, cleaning the parent
invalidates the summary's cache entry for free — there is no invalidation code.

`sync_tables` rebuilds the working set from the uploader each rerun, so summaries are
carried over explicitly, each placed straight after its parent. A summary whose parent is
gone is simply not carried over, which is the cascade delete the page relies on.

## Shape of the three actions

- `group_summarise` — group keys plus (columns × functions). No group keys is legal and
  gives one totals row; it runs through the same `groupby` on a constant so the two paths
  can't diverge. `sum`/`mean`/`median` on a text column warns and skips that one
  aggregation rather than costing the user the whole summary.
- `pivot` — column labels are flattened to unique strings, which is load-bearing rather
  than cosmetic: `duckdb_session.register_table` rejects a frame with non-unique labels, so
  a tuple label would break the Chat with Data handoff. Width is capped in the dialog, not
  in `validate`, because `validate` has no frame and so can't know the cardinality.
- `unpivot` — `melt`, with a mixed-type value column cast to text, for the same DuckDB
  reason.

## Notes and known gaps

- A summary tab is preview-only: metrics, preview, column details, sheet name, Edit and
  Delete. Giving it a cleaning bar of its own would make "reset this table" and the
  per-step undo ambiguous about which half of the recipe they act on.
- Renaming a summary happens only in the "Output sheet name" box on its tab. The edit
  dialog deliberately has no name field: that box holds the pre-edit text in its own
  widget state and would write it straight back over anything typed in the dialog.
- `st.selectbox` hands back a stored value its options no longer contain, unlike
  `st.multiselect` which filters stale selections itself. The pivot dialog's pickers narrow
  each other, so its two selectboxes are re-checked against their own options.
- Upload type detection reads a month-name column (`Jan`, `Feb`) as a **date**, so pivoting
  across it produces timestamp headings. Pre-existing detection behaviour, not introduced
  here, but it is the first place a user is likely to meet it — set the column back to text
  first. `test_saving_a_pivot_produces_a_column_per_distinct_value` pivots across `region`
  for exactly this reason.
- In `AppTest`, `st.tabs` is a block rather than a widget, so no tab selection is sent back
  and the open tab has to be re-set in session state before *every* run. `_select_tab` in
  `test_data_cleaner_page.py` does that; without it a summary's own widgets never render.
