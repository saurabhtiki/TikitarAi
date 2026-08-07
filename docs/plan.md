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

### Pinning stays one click

No dialog, no prompt — §6.1 step 3 is explicit that the chat flow must not be interrupted.
The heading defaults to the question, so a pinned tile is already labelled before the user
opens the Dashboard. Feedback is a `st.toast` carrying the running pool count.

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

Per item: a heading box, a comment box, **Generate comment**, **Move to** a different
subsection, **Look**, and **Unplace**. The outputs themselves are *not* drawn in the tree —
a dozen full-size Plotly charts is unusable as a structure editor.

**② Preview** — the report top to bottom, walked through the same `model.walk(report)`
both exporters use, so what is previewed cannot drift from what downloads.

**③ Download** — the preset picker (**Clean** / **Corporate** / **Compact**), an optional
hand-edit behind an expander, and the two buttons.

### The AI comment is on demand

`analyst.commentary.write_commentary` was already written, already `knowledge_base`-aware
and already never raises, so it is reused verbatim. Generating is a button rather than
something that fires at pin time: pinning is meant to be a click that costs nothing, and a
model call behind every pin would make a burst of pinning slow and expensive for comments
the user may well rewrite. The button is disabled with the reason given when there is no
session model or no rows to comment on — the same gate the chat input uses.

A regenerated comment needs the text area to actually show it, which a widget keyed on the
item alone would not: Streamlit widgets remember their own value rather than re-reading
`value=`. Hence `PinnedItem.comment_revision`, bumped by `set_comment` and folded into the
widget key.

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
| `app_pages/chat_with_data.py` | pin button enabled, gate widened, toast; Start over clears the report too |
| `streamlit_app.py` | page registered in the "Explore" group |
| `cleaner/export.py` | `check_limits` / `column_width` / `write_sheet` made public for reuse |
| `pyproject.toml` | `jinja2` declared directly, `kaleido` added |

`analyst/session.py` was **not** changed: clearing the dashboard on Start over happens at
the page, the same arrangement `chat_session.reset_chat()` already uses, so no package
below the page layer needs to know `dashboard/` exists.

## Reused, not rebuilt

`analyst.commentary.write_commentary` · `analyst.charts`' figures ·
`cleaner.naming.sanitize_sheet_names` · `cleaner.export`'s limits and column widths ·
`llm.session.active_profile` · the `open_dialog`/`close_dialog`/`pending_dialog` idiom from
`engine/session.py` · the download-button shape from `data_cleaner.py` · `st.switch_page`
for the cross-page jump.

---

## Verification

`uv run pytest` — **811 passed**, plus one pre-existing failure carried in from Stage 6
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
- `test_dashboard_page.py` (32) — every role reaches it; empty states; place, unplace,
  discard; rename; delete returning items; reorder disabled at the ends; the position box
  appearing only when it earns its space; Generate comment gated, working, and failing
  safely; a plain rerun never calling the model; both downloads absent while empty.
- `test_chat_with_data_page.py` (extended) — the pin button live, present on a
  commentary-only answer, absent on an error and on the user's own message; one click
  adding exactly one pool entry; the pinned frame surviving `release_payload`; pinning not
  re-answering; Start over clearing the report.

Rasterization is stubbed in every test — it launches a headless browser — and was
exercised by hand instead: a real Plotly figure produced a 78 KB PNG, a 107 KB
self-contained HTML with no network references, and a 56 KB workbook.

### Known gaps

- `test_a_collapsed_step_does_not_run_its_body` (Stage 6) fails on the installed Streamlit:
  a collapsed `st.expander`'s `.open` reports True, so a collapsed step still runs its
  body. A performance question, not a correctness one, and untouched by this phase.
- `AppTest` can't read the bytes behind a download button (recorded in
  `test_data_cleaner_page.py`), so what the exports *contain* is asserted against the pure
  builders and the page tests only check the buttons appear.
