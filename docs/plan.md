# Stage 9 — Chat Types (requirement 6.6, build-order item 8)

A chat type is a saved Steps 1–3 setup for Chat with Data: the expected shape of the
files, how they link, and what the columns mean. Select one, upload this month's files,
and the setup is already there — or the load is refused with a reason.

Deliberately shaped like Stage 8's criteria sets, because it is the same problem: a recipe
that has to outlive the session so the same work can be repeated next month. Where that
stage stored *rules*, this stores *setup*.

## What is not in it

- **Calculated-column statements.** They are requirement 7.5's Task recipe, and replaying
  a statement whose column the new file did not bring would leave the engine half-applied
  — a state with no good error message.
- **Rows and transcripts.** Same rule as `checks.model.to_json`: a recipe describes data
  that is not loaded yet.

## The one design decision worth stating

**A type mismatch is fixed or refused, never tolerated.** A semantic type is not a label:
`engine.loading.prepare_raw_frame` converts the column, so `date` is a real DuckDB DATE and
`text` is VARCHAR. A date column that loads as text turns `joining_date < '2024-04-01'`
into a *string* comparison — wrong rows, no error, and in Stage 8 a wrong `criteria_met`
that goes straight onto the Dashboard as a report. So the saved type is re-applied as a
**declared** type (the same override a Data Cleaner recipe uses, which is why
`prepare_raw_frame` already takes one), and if the values will not convert the load is
refused with the offending values named.

## Steps

### 1. `chat_types/model.py` + `tests/test_chat_type_model.py`

`ChatType` — `chat_type_id`, `name`, `tables: dict[str, list[ExpectedColumn]]`,
`relationships: list[Relationship]`, `dictionary: list[ColumnEntry]` — plus
`to_json` / `from_json` carrying `SCHEMA_VERSION`, and `capture(...)` building one from the
live engine state. No Streamlit, no SQL, so it tests without `AppTest`.

### 2. `chat_types/db.py` + `tests/test_chat_type_db.py`

`chat_types (chat_type_id, user_id, name, config_json, created_at, updated_at)` with a
unique index on `(user_id, name COLLATE NOCASE)`. Follows `checks/db.py` exactly: a
short-lived connection per call, every read and write scoped to `user_id`, and `save`
matching on name so Save-over updates rather than colliding. `list_types`, `load_type`,
`save_type`, `delete_type`.

### 3. `chat_types/matching.py` + `tests/test_chat_type_matching.py`

Pure. Expected schema × what was uploaded → a `MatchReport`:

- `missing_tables`, `missing_columns` → blocking
- `retyped` → columns the saved type was applied to (a note)
- `refused` → columns whose values would not convert, with examples (blocking)
- `extra_tables`, `extra_columns` → notes; the caller drops them

Table names are compared after `duckdb_session.slugify_table_name`, so `Salary Master.xlsx`
and `salary_master.csv` are the same expected table.

### 4. `chat_types/session.py` and the engine helpers it needs

Session state for the active chat type and its applied-once flag. `engine/duckdb_session.py`
gains `drop_column`; the retyping path reuses `engine.loading.prepare_raw_frame`'s existing
`declared` argument rather than adding a second type-application implementation.

### 5. The page — `app_pages/chat_with_data.py` + `tests/test_chat_type_page.py`

A chat-type bar above the Setup / Chat / Checks control (picker, name box, Save, Delete),
and a match panel inside Step 1 that reports green or lists exactly what is wrong.
On a clean match: apply the dictionary through `session.set_dictionary` +
`refresh_dictionary` (whose `existing=` merge already carries descriptions by
`(table, column)`), then the relationships through `relationships.enforce`.

### 6. Criteria sets scoped to the chat type

`check_sets` gains a nullable `chat_type_id`. There are live rows in `data/tikitarai.db`, so
`init_check_sets_table` adds it with a `PRAGMA table_info`-guarded `ALTER TABLE`, and the
unique index becomes `(user_id, COALESCE(chat_type_id, 0), name)` — SQLite treats NULLs as
distinct, so a plain three-column index would stop de-duplicating unscoped sets. Deleting a
chat type sets the column back to NULL rather than cascading. The Load dialog filters to the
active chat type with a "show all my sets" escape hatch.

### 7. Close out

Full test suite, then `/code-review`, then update CLAUDE.md's Current phase.
