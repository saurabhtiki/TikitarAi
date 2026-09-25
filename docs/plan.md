# Phase 49 — Everyone can run every Transform Data pipeline

**Status: done.**

## The problem

Transform Data's **Saved pipeline** picker lists only the pipelines *you* saved. Like reports
(phase 48), Ravi should be able to run Anna's "Monthly clean-up" on this month's files.

## What changes

**Everyone can:** see every saved pipeline, with its owner, like *"Monthly clean-up · by Anna —
last saved 2026-09-20 10:15"* (or *"· by you"*), and run it with **Run this pipeline**. Typing
"anna" in the picker finds Anna's pipelines. **Save as pipeline** still works: it saves your
own copy under your name.

**Only the owner can:** **Update pipeline** and **Delete**. For anyone else both are grey, with
the tooltip *"Only Anna can change or delete this pipeline. Save as pipeline keeps your own
copy."*, and the line beside the picker says the same.

**Example:** Ravi picks "Monthly clean-up · by Anna", uploads September's file and presses Run.
He wants one more step, so he adds it and presses **Save as pipeline** → "Ravi's clean-up".
Anna's pipeline is untouched.

## How it's built

- `transform/db.py`: `list_all_pipelines()` (with `owner_name`), `load_pipeline_for_run(id)`
  and `pipeline_owner(id)`, the same shape as phase 48's Task helpers. Every write stays
  owner-scoped.
- `app_pages/transform_data.py`: the picker lists everyone's pipelines (`saved_picker.select_saved`
  gains `row_face` for the "· by" label), selecting loads with `load_pipeline_for_run`, and
  `_change_blocked_reason` greys Update/Delete and is checked again when either dialog's button
  is pressed (a non-owner with a same-named pipeline would otherwise overwrite their own).
- `tests/conftest.py`: loads the `dashboard` package before any page test, since
  `app_pages/dashboard.py` hid it when a page test file ran on its own.

## Tests

- Storage: every account listed with owner names, another account loads to run, owner lookup,
  another account still can't save or delete, a missing pipeline says so.
- AppTest: Anna sees the admin's pipeline "· by" its owner and runs it; Update/Delete are grey
  for her (Save as is not); pressing either dialog's button is refused; the owner's stay live.

---

# Phase 48 — Everyone can run every report

**Status: done.**

## The problem

Reports → **Pick a task to run** lists only the reports *you* built. This is a small internal
team, so Ravi (a normal user) should be able to run Anna's "Monthly sales" report on this
month's file without asking Anna to do it.

## What changes

**Everyone can:**
- **See** every saved report in the Reports list. Each row shows who owns it:
  *"Monthly sales · by Anna"*.
- **Run** any report on new files (Run, Schema, Chat and Dashboard all work for everyone).
- **Send** a cleaned table from Transform Data to any report (Export to → Reports).

**Only the owner can:**
- Change the report's design, in Report Builder (as today, admins and super users only).
- Delete it.
- **Save notes into the report** after a run (the button that writes this run's notes,
  pictures and pasted HTML back into the saved report). For anyone else the button is grey,
  with the tooltip *"Only Anna can save changes into this report."* Their run still works,
  and the notes still show in the report they download.

**Example:** Ravi opens Reports, searches "sales", and sees *"Monthly sales · by Anna"*. He
presses Run, uploads September's file and gets the report, then opens its Chat and Dashboard.
He can't change the report's layout, and he can't delete it.

## Rules

- **Report Builder doesn't change.** Its Open/Delete list still shows only your own reports,
  so nobody can edit or delete someone else's. Normal users still don't see it.
- **Chat with reports already shows everyone's reports.** No change there.
- **Same name, two owners:** if Anna and Ravi both have "Monthly sales", the owner's name
  tells them apart.
- **Search** matches the owner's name too, so typing "anna" lists Anna's reports.
- The report a run produces (and what Chat and Dashboard show) belongs to the report, not
  the person who ran it, the same as today. The newest run is what everyone sees.

## How it's built

- `tasks/db.py`:
  - `list_all_tasks()`: every Task with its owner's name (joined from `users`), no JSON.
  - `load_task_for_run(task_id)`: reads any Task, for running only. `task_owner(task_id)`:
    who owns it. `owner_label(row, user_id)`: "you" or the owner's name. Every write
    (`save_task`, `delete_task`) stays scoped to the owner.
  - `list_tasks` / `load_task` stay as they are for Report Builder.
- `app_pages/run_task.py`: the picker uses `list_all_tasks`, shows "by <owner>", searches the
  owner's name, and Run and Schema use `load_task_for_run`. The save-notes button is disabled
  unless you own the report, and the owner is checked again when it is pressed. (`save_task`
  matches by name within the saver's own account, so a non-owner with a report of the same
  name would overwrite their own one rather than be refused.)
- `app_pages/report_view.py`: `render_report_output(save_blocked=...)` greys the button out
  with that reason.
- `app_pages/transform_data.py`: the Send to a report dialog lists every report, with the
  owner's name.

## Tests

- `list_all_tasks` returns two users' Tasks with owner names. `load_task_for_run` reads
  someone else's Task, while `save_task` and `delete_task` still refuse a non-owner.
- AppTest: a normal user's picker shows another user's report with "by <owner>", Run opens
  it, and the save-notes button is disabled for them but enabled for the owner.
- Search by owner name. Transform Data's dialog lists another user's report.

---

# Phase 47 — Send a cleaned table from Transform Data to a Report

**Status: done.** Phase 45's plan is in git history; phase 46's is below.

## The problem

People clean each month's file in **Transform Data**, then download it and upload it again on
**Reports**. That is an extra step, and it's easy to upload the wrong file.

## What changes

**Export to** gets a second choice: **Reports** (next to Chat with Data).

1. Tick the finished table under **Download**, then press **Export to → Reports**.
2. A dialog asks:
   - **Report:** which of your saved reports.
   - **"merged_data" is which of the report's files?** It is already filled in when the
     name matches or the report expects only one file.
3. Press **Send and open the report**. You land on the Reports run screen with the table
   already loaded as that file. Check Step 2, then press **Run task**.

**Example:** Rahul cleans April's sales in Transform Data (trims spaces, fixes dates). He picks
**Export to → Reports → Monthly sales**, and "sales" is already chosen. He presses Send, and the
Reports page opens with April's data loaded. He presses Run.

## Rules

- **Missing column = no send.** If the report needs `region` and the table hasn't got it,
  the dialog says *"merged_data has no **region**... Add a Rename step, then send again."*
  and the Send button stays grey. (On the Reports page a sent table has no file to remap,
  so the fix belongs here.)
- **Loaded like an upload.** The table is turned back into text and loaded with the
  report's saved column types, exactly as if it were downloaded and uploaded. So the
  Reports page's normal check still catches text in a number column, in the same words.
- **Several files:** if the report expects Sales **and** Customers and you send only Sales,
  the dialog says *"Still to upload on the Reports page: customers."* Upload that one as usual.
- Two tables sent as the same file are refused. **Don't send** leaves a table out.
- Sending again replaces what was sent last time, and any table already loaded under that name.
- Only your own saved reports are listed, the same as the Reports page.

## How it's built

- `transform/report_handoff.py` (no Streamlit): the likely file, the reasons to refuse,
  and the files still to upload.
- `engine/loading.py::raw_from_frame`: a table back to upload-style text (dates
  `yyyy-mm-dd`, an id of 7 stays `7`, not `7.0`).
- `engine/session.py::adopt_for_report`: loads the tables under the report's names and
  saved types, and records what the load found for Step 2.
- `runner/session.py::receive_sent_tables`: opens the report (only if it isn't already
  open, so files you uploaded for it stay), then loads the tables.
- `app_pages/transform_data.py`: the **Reports** menu choice and the **Send to a report** dialog.

## Tests

- `raw_from_frame`: dates, whole numbers, blanks, time of day.
- `report_handoff`: name match, one-and-one, missing column, duplicate, nothing chosen.
- AppTest: Reports opens the dialog, a send loads `sales` under its saved types with a clean
  match report and a message queued for Reports, a missing column disables Send, and Cancel sends nothing.

---

# Phase 46 (done) — Anyone can view and download a report's dashboard

## The problem

A dashboard is designed in **Report builder → Dashboard** and saved with the report. But on
the **Reports** page, the people who upload each month's data can only Run, see the Schema
or Chat. There is no way to see the dashboard with the new numbers.

## What changes

A **Dashboard** button, **view and download only**. There is no editing and no AI.

| Where | What you see |
|---|---|
| **Reports** list | A **Dashboard** button on every report, next to Chat. |
| **Chat with reports** list | The same **Dashboard** button, so anyone can open it, even people who didn't build the report. |
| **Reports** run screen | After **Run task**, a **Show the dashboard** switch under the report draws it right there, with the numbers just uploaded. |

Pressing **Dashboard** opens a full-width page with:
- the report name, and *"Data last refreshed: 25-09-2026 by Rahul"*,
- **Download this dashboard** (one HTML file that works offline and stays clickable),
- the dashboard itself, filters and all,
- **Back**, to return to the list you came from.

**Example:** Rahul runs *Sales* with April's file. Priya opens **Chat with reports**, presses
**Dashboard** on *Sales* and sees April's numbers. She downloads the file and emails it.

## Rules

- **Data:** the report's saved data (the copy every run saves for Chat). So it always shows
  the latest run, and needs no upload.
- **Disabled** until the report has been run once, like Chat is ("Run this report once...").
- **No dashboard saved:** the page says *"This report has no dashboard yet. Build one in
  Report builder → Dashboard."*
- **A visual that can't be drawn** (for example, a column this month's file no longer has) is
  named in a warning above the dashboard, the same way Report builder names it.
- **Too much data:** the same row limit as Report builder. It says so instead of freezing.
- **Only the dashboard is shared.** The rest of the report (its SQL, checks and so on) stays
  private to the person who built it, as before.

## How it's built

- `live_dashboard/saved_view.py` (no Streamlit): flattens the saved tables with the saved
  links and builds the HTML, plus the list of visuals that can't be drawn. Pure, so it can be tested.
- `tasks/db.py::load_dashboard_spec(task_id)`: reads **only** the dashboard part of a
  report, for any user.
- `app_pages/dashboard_viewer.py`: shows it (the refreshed line, problems, download and preview).
  Its result is cached per report, refresh time and dashboard, so reruns don't rebuild it.
- `app_pages/report_dashboard.py`: a hidden page (not in the menu) the Dashboard buttons open.

## Tests

- `saved_view` builds a dashboard from a saved DuckDB file whose filter reaches a child
  table through a saved link, names a visual whose column is gone, and refuses when there is
  too much data.
- `load_dashboard_spec` works for a user who doesn't own the report and fails cleanly for a missing one.
- AppTest: Reports and Chat with reports show the Dashboard button (disabled with no data),
  and the viewer page shows the refreshed line, the download button and the "no dashboard yet" message.

## Not in this phase

- Editing the dashboard from Reports. That stays in Report builder.
- Transform Data → **Export to → Reports** is phase 47.
