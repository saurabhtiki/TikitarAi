# Phase 13 — Chat with Reports

Save each report's current data so anyone can chat with it later, without re-uploading.

## Goal

After a report runs, its data is written to a file. Any logged-in user can open
**Chat with reports**, pick a report, and ask questions of that saved data.

## Decisions already taken

- **Shared, not per-user.** One dataset per report. Any user may refresh it, and any
  user may chat with it. This deliberately relaxes the per-`user_id` scoping in
  `tasks/db.py` for *data* only — the recipe itself stays owned.
- **Latest only.** A refresh replaces the data. No versions, no history.
- **One process, so no write-lock problem.** One shared connection per report file.
- **A "last refreshed" line** in the chat, naming who and when.
- **No chat history stored**, and **no Pin to Dashboard**. This is on-the-spot Q&A.
- **Schema and relationships are given to the agent as context** — the same
  `engine.dictionary.schema_context()` block Chat with data uses, rebuilt from storage.
- **Read-only.** A report's shared data must never be altered from this chat, so the
  conversational column-change path (requirement 5.4) is refused here.

## Where things live

| Thing | Where |
|---|---|
| Rows | `data/reports/report_<task_id>.duckdb`, one file per report |
| Tables, columns, types, descriptions, synonyms, links | `report_datasets.setup_json` in `data/tikitarai.db` |
| Who refreshed, when | `report_datasets.refreshed_by` / `refreshed_at` |

## Steps

1. **`reports/exceptions.py`** — `ReportDataError`.
2. **`reports/model.py`** — `ReportSetup` (tables, dictionary entries, relationships)
   with `to_json` / `from_json`, so what the agent needs survives a restart.
3. **`reports/store.py`** — the DuckDB file: `dataset_path`, `open_store`,
   `write_tables` (replace wholesale), `stored_tables`, `delete_dataset`.
4. **`reports/db.py`** — the `report_datasets` SQLite table, plus
   `list_reports_with_data()`, which is **not** scoped by user, by design.
5. **`reports/session.py`** — Streamlit only: one cached store connection per report
   (`st.cache_resource`, shared on purpose), and the chat transcript in `cr_*` keys.
6. **Save after a run** — `runner.session.execute_run` writes the data and setup once the
   replay succeeds. A save failure is a warning on the run, never a failed run.
7. **`app_pages/chat_with_reports.py`** — pick a report, see when it was last refreshed,
   ask questions. Answers render text, table, chart and the SQL behind them.
8. **Navigation** — add "Chat with reports"; rename "Run a task" to "Reports" in the
   menu only.
9. **Delete cleans up** — deleting a task deletes its dataset row and its file.

## Tests

- Save a run's tables, reopen the file cold: tables, links and column meanings all return.
- Refresh twice: only the newest rows exist, and a dropped table is gone from the store.
- The rebuilt schema context names every table, its columns and the joins.
- A column-change message is refused instead of altering shared data.
- Deleting a report deletes its file.
