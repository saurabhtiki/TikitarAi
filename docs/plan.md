# Phase 20 — Updating a report's pictures, notes and pasted HTML on a re-run

The last of the five features worked out in plan mode, and the one that was deliberately
left until the blocks it edits existed (phases 17–19 built them).

## What the user does today

**Task Builder** is where a report is authored: sections, report items, and — since phase 17
— blocks the user writes by hand (a text note, an uploaded picture, pasted HTML). That is
saved as a Task, the reusable recipe.

**Automate → Reports** is where it is run: pick the saved report, upload this month's files,
press **Run task**. The run rebuilds the report from the Task's skeleton and fills the
report items with fresh numbers.

The gap: the numbers refresh, the hand-written parts cannot. A picture pasted out of last
month's Excel, a note written about last month, a Power BI embed pointing at last month's
page — all come back exactly as saved, with no way to change them without going back to
Task Builder and re-authoring the whole thing.

## What this phase adds

A third view on the run screen, between the two that are already there:

**Preview · Update · Download**

**Update** lists every placed item in report order and gives each one the controls for the
parts a run cannot produce:

- **any item** — its comment, in the same bold/italic/underline/list editor Task Builder uses
- **a picture block** — upload a new picture, look at it, remove it
- **an HTML block** — the paste box and its frame height

Edits land on this run's report immediately, so Preview and both downloads show them without
another press.

Under the list, one button: **Save these into the task**. It writes the current comments,
pictures and pasted HTML back into the saved report, so next month's run opens with them
already right. Not automatic — a run's edits are often just for this month's copy, and
silently rewriting the saved recipe would be the wrong default.

## Decisions

- **Update, not Build.** `render_report_output` has always refused to offer the Build view,
  because a run rebuilds the arrangement wholesale from the Task and filing items into
  sections here would be undone by the next press of Run. That reasoning does *not* apply to
  a block's own content: a manual block's picture and HTML come straight out of the skeleton
  and nothing in a run produces them, so they are exactly the fields it is safe to edit here.
  The Update view therefore edits content and never structure.

- **Comments are editable here but rewritten by the next run.** A run redrafts each report
  item's comment for this month's numbers unless the *Rewrite the comments* box is cleared.
  So an edit made here is this month's wording, and saving it into the task only sticks for a
  user who runs with rewriting off — which is precisely the hand-written report that wants
  it. Said in the button's tooltip rather than assumed.

- **Matched by `item_id`, not by position.** The run's report is a deep copy of the Task's
  skeleton, so every item carries the same id at both ends. Copying by id means a save is
  exact even if a later Task Builder edit reordered things, and an item that no longer exists
  in the saved report is skipped rather than guessed at.

- **Only the by-hand fields travel.** `copy_authored_content` moves `comment`, `image`,
  `image_mime`, `embed_html` and `embed_height` — and nothing else. It cannot carry a frame
  or a figure into the skeleton even by accident, which is the rule `skeleton.to_dict`
  enforces structurally and this must not undermine.

- **The save button is the caller's.** `report_view` has no business importing `runner` or
  `tasks` — `run_task.py` imports *it*. So the view takes an optional `on_save` callable and
  the Run page supplies the one that writes to SQLite, the same arrangement `EmptyPool`
  already uses for its button.

- **Nothing new is persisted.** `skeleton.py` already stores `comment`, `image`,
  `image_mime`, `embed_html` and `embed_height` (phases 17–19). This phase writes into fields
  that already round-trip, so a report saved before it loads and exports unchanged.

## Files changed

- `dashboard/model.py` — `AUTHORED_FIELDS` and `copy_authored_content(source, target)`,
  pure, no Streamlit.
- `app_pages/report_view.py` — `"Update"` in `OUTPUT_VIEWS`; `_render_update_view`,
  `_render_update_item` and `_render_save_authored`; `render_report_output` gains `on_save`.
- `app_pages/run_task.py` — `_save_authored_into_task`, wired in as `on_save`.

## Verification

- Run a saved report, change a picture block's picture and one item's comment, and check
  Preview and the HTML download both show the new ones.
- Press **Save these into the task**, run the report again, and check the new picture is
  what comes back.
- Leave a block untouched and check it keeps exactly what it had.
- Run a task that has never been saved and check the save button explains itself rather than
  failing.
- Open a report saved before this phase and check it still runs, previews and downloads.
