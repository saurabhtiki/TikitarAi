# Phase 11 — Better summaries: rename output columns, per-column functions

Small follow-up phase on the Summarise group (Group & total, Pivot, Unpivot) in the
Data Cleaner. Three changes, built and tested in this order.

## 1. Rename the output columns (all three reshapes)

**Why:** a Group & total gives you `sum_of_Salary`, `count_of_Days` — fine to read, ugly
in the download. Today the only fix is a second cleaning pass.

**What:** a "Rename columns" section in each reshape dialog, just above Save, listing one
text box per output column (blank = keep the name it has). Applied to the preview, saved
with the summary, so it survives Edit and template replay.

**How:** new optional `output_names` param (`{produced column: new name}`) on the three
reshape steps, applied at the end of each — one shared helper, deduped like every other
column rename. Names of columns that no longer exist (a pivot's spread-across columns
change with the data) are ignored silently.

## 2. Group & total: a function list per column

**Why:** today "Total up" × "Using" is a cross — pick Salary + Days and min + max + count
and you get all six columns, including a meaningless `min_of_Days`.

**What:** pick the columns, then pick that column's functions on its own row:

| Total up | Using       |
| -------- | ----------- |
| Salary   | min, max    |
| Days     | count       |

Non-numeric columns are only offered the functions that work on text. Defaults: sum for
a numeric column, count for a text one.

**How:** UI only — the saved params are already `[{column, function}, ...]`, so steps.py
does not change.

## 3. Pivot: several value columns, each with its own function

**Why:** a real pivot is `Region across the top → min salary | max salary`, not one value
column with one function.

**What:** the same per-column function list as (2) for Pivot's values. "Columns (across
the top)" stays required — leaving it blank is just a Group & total, and two ways to do
one thing is one too many.

**How:** pivot params become `index`, `columns`, `aggregations: [{column, function}]`,
`fill_value`. Old saved templates hold `values`/`function` instead, so both shapes are
accepted and normalised on read. The existing `MAX_PIVOT_COLUMNS` guard matters more now
(distinct values × chosen aggregations), and already covers it.

## Tests

Extend `tests/test_cleaner_steps.py` (renames applied, ignored when a column has gone,
deduped; pivot with several aggregations; old pivot params still replay) and
`tests/test_data_cleaner_page.py` (the new dialog widgets, and that the params they build
round-trip through save and Edit).
