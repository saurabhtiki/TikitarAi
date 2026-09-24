# Phase 42 — Combine columns, and dates shown as dd-mm-yyyy everywhere

**Status: built.** Phase 41's plan is in git history.

Two asks from using Transform Data:

1. *"Join Name and Surname into one column with a space or a dash."* Split a column
   exists; its opposite doesn't.
2. *"My file says 03-04-2025 but the app shows 2025-04-03."* Checked: an **Excel** date
   cell is read in as the text `2025-04-03 00:00:00` (pandas' own way of writing a date),
   so it is wrong from the moment of upload. A column changed to a real date then shows
   in Streamlit's default `yyyy-mm-dd`, and downloads follow the same default.

## 1. A "Combine columns" step (Transform Data → Columns)

- `combine_columns` in `transform/ops_columns.py` + one registry entry. No UI code:
  the form, Describe a step (AI) and saved pipelines all read the registry.
- Asks for: the columns (in the order picked), the joiner (space, dash, comma, slash,
  underscore, nothing, or your own), and the new column's name (optional).
- Example: Name `Ravi` + Surname `Kumar` with a space → `Ravi Kumar`; with a dash →
  `Ravi-Kumar`.
- A blank piece is skipped, so `Ravi` + blank gives `Ravi`, not `Ravi ` or `Ravi-`.
  All blank gives blank. Whole numbers print as `5`, not `5.0`; dates as `03-04-2025`.

## 2. dd-mm-yyyy wherever a date is read, shown or saved

One small helper module, `utils/dates.py`, so every page uses the same rule:
date only → `03-04-2025`; date with a time → `03-04-2025 10:30:00`.

- **Reading Excel**: a date cell becomes `03-04-2025` text — what the file shows — instead
  of `2025-04-03 00:00:00`. CSVs are already read exactly as typed.
- **Reading a date without a stated format** (`cleaner.profiling.parse_datetime_series`
  with no format, used by Chat with Data's auto-detect): ISO first, then **day-first**,
  the rule Transform's own date parser already follows. Without this, `03-04-2025` would
  be read as 4 March, and `13-04-2025` not at all.
- **Showing**: every `st.dataframe` goes through `show_dataframe`, which adds a date
  format for each date column and otherwise passes everything straight through. Sorting
  still sorts by real date (it is a display format, not text).
- **Excel downloads**: real dates stay real dates (Excel can still sort and filter them)
  but carry the `dd-mm-yyyy` cell format.
- **CSV downloads**: dates written as `dd-mm-yyyy` text.
- Known limit: the small download icon on Streamlit's own table toolbar writes dates in
  its own `yyyy-mm-dd` form; we can't change that. The app's download buttons are fixed.

## Tests

- Combine: space/dash/custom joiner, blanks skipped, numbers and dates printed nicely,
  name clash, fewer than two columns refused, registry + AI catalog carry it.
- Dates: Excel date cell → `03-04-2025`; `03-04-2025` auto-parses as 3 April and
  `13-04-2025` parses; column config and Excel/CSV formatting of date-only and
  date-with-time columns; full test suite still passes.
