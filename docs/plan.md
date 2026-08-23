# Phase 16 — Pin loaded tables, and a Load button on uploads

Two small things, decided in discussion:

1. The report's **Import from Excel** panel stops asking for a file. It offers the tables
   **already loaded** on Chat with Data / Task Builder, and pins the ones you tick.
2. Uploading files no longer acts on its own. You upload, pick sheets, then press
   **Load** — the same behaviour on both pages.

## Decisions already taken

- **No more workbook parsing.** The loaded tables are already the data — cleaned, typed
  and the same numbers you've been chatting with. Re-reading the original `.xlsx` would
  give a second, slightly different copy of the same thing.
- **Charts and pivot tables are dropped from the import.** Excel doesn't store a picture
  of a chart, so we could only redraw it, and a redrawn chart doesn't match the workbook.
  A pivot flattened into a table doesn't look like a pivot either. You'll bring both in by
  hand in the next phase, as an image or as Excel's *Save as Web Page* HTML.
- **A sheet is just a sheet.** With pivots no longer detected, nothing gets skipped and
  nothing gets pinned twice.
- **Rows: 500 on screen, all of them in Excel.**
  - The report page and the HTML export show the first 500 rows and say so —
    *"Showing 500 of 12,480 rows."*
  - The Excel download gets every row. That's the file you'd actually work in.
  - The old 5,000-row read cap goes, replaced by a much higher safety ceiling so a
    250,000-row table can't quietly fill the session.
- **Re-pinning still keeps your words.** `pin_imported` is unchanged: pin the same table
  again after a reload and the numbers refresh under the title and comment you wrote.

## Where things live

| Thing | Where |
|---|---|
| The loaded tables | `engine/session.py` → `get_tables()`, `preview(table_name, limit)` |
| Pinning without overwriting titles | `dashboard/session.py` → `pin_imported` (already built) |
| The import panel | `app_pages/report_view.py` → `_render_excel_import` |
| The uploader both pages share | `app_pages/setup_view.py` → `mount_upload` |

## Steps

### A. Pin from what's loaded

1. **`dashboard/excel_objects.py` → gutted and renamed `dashboard/pinned_tables.py`.**
   Everything that read a workbook goes: charts, pivots, cell trimming, chart redrawing.
   What stays is the source-key convention (`is_imported`, `_source_key`) plus a small
   `ROW_CEILING`. The `excel:` prefix becomes `loaded:`.
   *Old reports keep working:* `is_imported` accepts both prefixes.
2. **`app_pages/report_view.py`** — `_render_excel_import` becomes `_render_pin_loaded`:
   - no uploader, no "what to pin" multiselect;
   - a checkbox list of the loaded tables, showing name and row count;
   - a **Pin N tables** button;
   - a plain message when nothing is loaded, with the existing "Go to Setup" style link.
3. Reading a table's rows uses `engine.session.preview(name, limit=ROW_CEILING)`, so the
   pinned frame holds the whole table (capped only by the safety ceiling, which says so
   when it bites).

### B. Row limits

4. **Display cap of 500** wherever a pinned table is shown — the report page and
   `dashboard/html_export.py` / `report.html.j2` — with the *"Showing 500 of N rows"*
   note carried into the HTML.
5. **`dashboard/excel_export.py`** — unchanged, writes the full frame. Add a test that
   proves a 600-row table exports 600 rows while the HTML shows 500.

### C. The Load button

6. **`app_pages/setup_view.py::mount_upload`** — a confirm step in front of
   `session.sync_tables`:
   - remember the set of `file_id`s already confirmed, in session state;
   - files in the uploader but not in that set are *pending*: their sheet pickers still
     draw, so sheets are chosen before loading;
   - a **Load N files** button adds them to the confirmed set;
   - `sync_tables` is still called on **every** run — with the confirmed files only. This
     is the load-bearing part: the uploader must stay mounted or Streamlit drops it and
     every loaded table with it.
   - Removing a file from the uploader still removes its table, with no button needed.
7. Both pages get this for free — Chat with Data and Task Builder both call
   `mount_upload`.

## Tests

- `tests/test_dashboard_pinned_tables.py` — source keys, old `excel:` ids still read as
  imported, the ceiling.
- Page test: the panel lists loaded tables, pins the ticked ones, and says something
  useful when nothing is loaded.
- Row limits: HTML shows 500 with the note, Excel writes all 600.
- Upload gate: pending files aren't loaded until the button is pressed; removing a file
  still drops its table; a rerun with no button press doesn't lose anything.
- Delete `tests/test_dashboard_excel_objects.py` (the workbook reader is gone).

## Verification by hand

- Upload 4–5 files on Chat with Data — nothing happens until **Load**, and picking sheets
  in between doesn't half-load anything.
- Open the report Build view — the loaded tables are listed; tick two, pin them.
- Retitle and comment one, reload the file, pin again — numbers change, words don't.
- Export a long table: the HTML shows 500 rows and says how many there are; the Excel
  download has all of them.
