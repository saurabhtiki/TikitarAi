# Phase 30 — finance helpers: today's date, date shifting, codes, and row maths

**Status: built and tested.** All seven steps are in the catalog (36 operations in total),
`uv run pytest -q` is green (3203 passed, 1 skipped), and no UI file changed — the form,
the step list and Describe a step all picked them up from the registry.

The user asked for a list of finance-typical functions. Checking against the existing
29-operation catalog, three were already there: **Running total**, **Days between two
dates**, and **Age in days/months/years** (`date_difference`, different `unit` choices), and
**Extract Year/Month/Quarter** (`extract_date_part`). This phase adds the seven that are
genuinely missing.

---

## 1. What gets added (seven new steps, "Dates" and "Calculated columns" categories)

| Step | What it does | Example |
|---|---|---|
| **Today's date** | New column, same value every row | `Loaded_On` = 19/09/2026 for every row |
| **Add/subtract days** | Shift a date column by N days | `Due Date = Invoice Date + 30` |
| **Start/end of month** | The 1st or last day of a date's month | `31/03/2026` for a date in March |
| **Extract part of a code** | Pull characters by position, not by separator | first 3 of `INV000123` → `INV` |
| **Percentage of total** | Each row ÷ the column's total × 100 | row's share of total sales |
| **Absolute value** | Drop the minus sign | `-500` → `500` |
| **Row min / max across columns** | Per row, compare 2+ columns | higher of `Budget` and `Actual` per row |

Each is one ordinary catalog step: pick it, fill a small form, see a preview, add it. Each
also reaches **Describe a step** automatically, the same way every operation does — the AI
prompt is generated from the registry, not hand-maintained.

---

## 2. Where each one lives

- **`transform/ops_derive.py`** (dates + row maths — it already owns `extract_date_part`
  and `date_difference`):
  - `add_today_date` — no source column needed; writes `pd.Timestamp.today().normalize()`
    into every row of a new column.
  - `shift_date` — date column + a number of days (can be negative) + new column name.
  - `month_edge` — date column + choice of "start of month" / "end of month".
  - `row_percentage_of_total` — a numeric column; each row becomes its share of that
    column's sum, as a percentage. Zero-total column gives blank for every row (matches the
    existing divide-by-zero-is-blank rule from phase 29) rather than an error.
  - `absolute_value` — a numeric column, in place or as a new column; same "not a number ->
    blank" rule as `change_dtype`.
  - `row_min_max` — two or more numeric columns + choice of "smallest" / "largest"; result
    is a new column, one answer per row (this is *not* the existing `groupby_aggregate`,
    which totals a column down the table — this compares sideways, across columns, on one
    row at a time).
- **`transform/ops_columns.py`** (text/columns — it already owns `split_column`):
  - `extract_by_position` — a text/code column + start position + length (both 1-based, so
    "start at 1, length 3" reads as "the first 3 characters") + new column name. Kept
    separate from `split_column` because a code like `INV000123` has no separator to split
    on.
- **`transform/registry.py`** — one `OperationSpec` entry per new operation, each following
  the existing pattern exactly (label, category, summary, params, apply/describe/validate/
  required_columns). No changes to the registry's shape or to `ParamKind`.
- **`transform/ai_parse.py`** — no mechanical change; `describe_catalog_for_prompt()` already
  builds the model's catalog straight from the registry, so these seven reach **Describe a
  step** with zero extra plumbing, the same as every past phase.

---

## 3. Rules carried over from earlier phases (so behaviour stays predictable)

- A value that can't be read as a number, or a date that can't be read as a date, becomes
  blank rather than an error — same as `change_dtype` and the phase-29 formula.
- Dividing by zero (percentage of total when the column sums to zero) gives a blank, not an
  error or infinity — same rule as the calculated-column formula.
- Every new operation still only reads columns and produces a new whole table — none of them
  edit cells in place, for the same replay/undo reasons already explained to the user.

---

## 4. Tests

- **`tests/test_transform_operations.py`** — one `TestXxx` class per new operation: a normal
  case, an edge case (zero total for percentage; length past the end of a short code string;
  a non-numeric column for absolute value / min-max), and `validate_` catching bad params
  before the step is added.
- **`tests/test_transform_ai_parse.py`** — one instruction-parse test confirming a sentence
  like "add a column with today's date" or "flag which of Budget or Actual is higher"
  proposes the right new operation.
- Full suite: `uv run pytest -q` green before calling this phase done.

---

## 4a. Three small calls made while building

- **Extract part of a code sits under "Columns", not "Dates"/"Calculated columns".** It lives
  in the picker right beside **Split a column**, which is the step a user will have tried
  first and found had no separator to work with.
- **Drop the minus sign has a "Write the answer" choice.** The plan said "in place or as a
  new column"; that is a two-option dropdown, with the new column's name only appearing once
  "add a new column" is picked.
- **Percentage of total has an optional "Decimal places", default 2.** A raw share is
  33.33333333333333, which nobody wants in a report.

## 5. Explicitly out of scope for this phase

- **Format as currency**, **pad codes to a fixed width**, and **group & sum** (the "bigger,
  pivot-like" one) — mentioned earlier by the user as extra ideas but not picked in the
  latest list. Can be their own later phase.
- Any change to the existing `add_calculated_column` formula grammar, `date_difference`, or
  `extract_date_part` — they already cover what they cover.
