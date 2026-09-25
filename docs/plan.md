# Phase 45 — Generate Dashboard asks before it builds

**Status: done.** Phase 44's plan is in git history.

## The problem

**Generate Dashboard** builds a whole page in one go. You can't say what you want first,
so you only find out what the AI chose after it has built it, and a wrong guess means
generating again and hoping.

## What changes

Generate becomes two steps: **propose a checklist**, then **build exactly that list**.

1. Press **Generate Dashboard**. The Light Model reads the tables, the links from Setup and
   the column descriptions, and writes a short checklist, one line per visual:

   ```
   Card: Total salary
   Card: Headcount
   Filter: Department
   Chart: Salary by department (bar)
   Chart: Headcount by month (line)
   Table: Salary by department, then employee (drill-down)
   ```

2. A dialog shows the list in a text box. You can edit, delete or add lines.
   - A second box, **Anything else?**, takes wishes that are not a visual
     (for example "show money in INR, focus on cost").
   - A collapsed **Columns you can use** list shows the real column names, so a line you
     add names something that exists.
3. Press **Build this dashboard**. The session's own model (the stronger one) builds
   **exactly** those lines: one visual per line, nothing added, nothing skipped on purpose.
4. When the build is done, any line that could not be built is named with its reason,
   for example: *"Line 4 not built: there is no column called 'Bonus'."*

## Buttons in the dialog

| Button | What it does |
|---|---|
| **Build this dashboard** | Builds the list with the session model. |
| **Draft again** | Asks the Light Model for a fresh list, using what is in *Anything else?* |
| **Skip the list, just build** | The old one-press behaviour, for people who trust the AI. |
| **Cancel** | Closes the dialog. Nothing changes. |

## Kept from before

- **Replace warning:** if the page already has visuals, the dialog says *"This replaces
  everything on the dashboard"* and the build button reads **Replace and build**. This
  takes over from today's two-press confirm, so the user answers only one question.
- **A failed build puts the old page back**, the same as today.
- **Undo this** in "What was asked, and what changed" reverses it. The history shows the
  checklist the page was built from, not "Generate a dashboard from this data".
- **Rounds are unchanged:** Update the dashboard and Edit work exactly as they do now.
- **No new saved field.** The checklist is only kept for this sitting, so an older Task
  opens as it always did.

## Which model does what

- **Checklist:** the Light Model, because it is fast and cheap. With no Light Model set up,
  the session model drafts it instead.
- **Build:** the session model, the same as today.
- **Both:** the model can still only pick from the existing visual catalog and real
  columns. It never writes code.

## How "strict" works

- The build prompt numbers the lines and says: one visual per line, in this order,
  nothing extra.
- Each visual the model proposes carries the line number it came from (a new field on
  the model's reply only, not on the saved `PanelSpec`).
- Any line number with no visual behind it is reported with the reason the checker gave.
  A visual with no line number (something extra) is dropped, with a note.

## Why the column check happens after the build, not before

Earlier we said the dialog would flag bad column names before building. The Light
Model's own draft uses real columns, because it is sent the column list. A line *you*
type is free text, though, and guessing column names from free text gives false alarms:
"Headcount" is a count of rows, not a column. So the dialog shows the column list to
help, and the build names each line it could not make and why. Nothing is silently
dropped.

## Code (roughly)

- `live_dashboard/ai_spec.py`
  - `draft_checklist(profile, tables, notes, wishes)` returns a list of plain lines, or a
    warning. It never raises.
  - `build_from_checklist(...)` is a thin wrapper around the existing first-draft path,
    with the numbered list as the instruction plus the strict rule. It also returns the
    lines that were not built.
- `live_dashboard/session.py`: holds the draft, the wishes and the dialog open/closed state
  for this sitting.
- `app_pages/dashboard_view.py`: the Generate button opens the dialog, and
  `_run_generate` takes the checklist.

## Tests

- `draft_checklist`: parses the model's reply into lines, fails politely when the model is
  down, and falls back to the session model with no Light Model set.
- Strict build: 5 lines in gives 5 visuals out. An extra visual is dropped with a note.
  A line naming a missing column is reported by its line number.
- The dialog (AppTest): an edited list is what gets sent, Cancel changes nothing, and
  Skip behaves like today's Generate.
- On a page that has visuals, the replace warning shows, and a failed build restores the
  old page.
- The round history records the checklist, and Undo brings the old page back.
