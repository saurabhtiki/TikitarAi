# Phase 44 — Three fixes from using the app

**Status: built.** Phase 42's plan is in git history.

## 1. "Numeric" in Column details, but Round says "not numeric" (Transform Data)

Why: an upload is held as text on purpose, so leading zeros like `007` are kept.
Column details names a column by what its values *look like* (`25, 40` → numeric), while
Round checks how the column is *stored* (text). Both are true, and together they look like a bug.

- Column details now says `numeric (stored as text)` / `date (stored as text)`.
- Below it, a red line names those columns and a one-click button:
  - **Store as numbers** opens Add a step set to *Fix numbers stored as text*, with the
    columns already picked.
  - **Store as dates** opens *Change column type* → date, with the columns picked.
- It is still an ordinary step, so a saved pipeline replays it next month.
- `cleaner/profiling.py::columns_stored_as_text` finds them, and
  `transform_form.preset_source_table` gains optional pre-filled boxes.

## 2. Dark-mode dashboard: data labels were black (invisible)

The dark theme coloured axes, legends and titles but not text marks, so a bar's number
fell back to Vega's default black. Both themes now set `config.text.color`. A test draws
the chart in the vendored Vega and checks the label colour.

## 3. Report Builder: "What the columns mean" not coming back

The descriptions were saved inside the Task but never put back when it was opened.
Now `load_task` hands them to `engine.session.hold_saved_descriptions`: each one lands as
soon as its table is loaded (before or after opening the Task), then is let go, so a
description you later clear stays cleared.

## Tests

- `tests/test_transform_data_page.py::TestStoredAsText`, `tests/test_cleaner_profiling.py`
- `tests/test_live_dashboard_vega_render.py` (label colour, light and dark)
- `tests/test_task_builder_page.py::TestDescriptionsComeBack`
