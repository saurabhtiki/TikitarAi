# Phase 41 — Several totals on a drill-down table, and a Rows column that can go

**Status: built.** Phase 40's plan is in git history.

A drill-down table drew one number. Asked for *"Sum of Cost, Sum of Quantity, Average,
Minimum and Maximum of Basic Cost by Category → Item → Vendor"*, it came back with the
sum alone and four measures quietly gone — `extra_measures` existed since phase 38 but was
whitelisted to five chart shapes, so a table had nowhere to put them and `clean_measures`
never saw them. The same page also always printed a **Rows** column nobody asked for.

Both are the same complaint: a drill-down shows what it decided to show rather than what
was asked for. This phase gives it the columns.

What changed from the plan as written, and why:

- **The extra-measure check became a shared function rather than a second copy.**
  `_extra_measure_problems` is called by the chart branch and the table branch, so a
  measure over a column no confirmed link reaches gets the same sentence on both.
- **The Rows column decides in Python, not in the browser.** `wants_row_count()` is the one
  place, and the payload carries the answer - so the page and the sentence the user read
  about it cannot disagree.
- **A drill-down's extra totals go through the "isn't a number" guard too.** Its first one
  always did; four more summed text columns would have been four more silent columns of
  zeroes.

## 1. A drill-down can total several numbers

One column per measure, in the order they were asked for, the panel's own measure first.

- `model.py`: the `extra_measures` check moves out of the chart branch into
  `_extra_measure_problems`, called by charts as before and now by tables. A **flat** table
  is refused with a sentence of its own ("a flat table lists the rows themselves"), a
  drill-down accepts up to `MAX_MEASURES_PER_CHART` in all (4, its own included — the same
  cap and the same reason: past four the reader is scanning, not comparing).
- `axis` is a chart word. `clean_measures` keeps the field as it always did, and a table
  simply ignores it: columns sit side by side, so there is no second side for one to go on.
  Nothing new to save, and so no migration — a phase-40 drill-down has an empty list and
  reads exactly as it always did.
- The "that column isn't a number" guard in `ai_spec` already covers a drill-down's own
  measure. It now covers its extra ones too: a summed text column is a silent column of
  zeroes, and four of them is four times the lie.

## 2. The Rows column becomes a setting

- A new `row_count` property (`yes`/`no`), whitelisted in `clean_properties` and offered in
  `describe_properties_for_prompt`, so *"drop the Rows column"* is a round that works.
- Its default is **shown for a single total, hidden once there are several**: one number per
  group leaves room for the count, five do not. The property overrides either way, so
  nothing is unreachable — `wants_row_count(panel)` is the one place that decides.
- On a flat table the setting is meaningless, and `property_problems` says so rather than
  dropping it silently.

## 3. The browser draws the columns

- `runtime.js`: `drilldownGroups(rows, levels, measures)` takes the list and each node carries
  `values` (one per measure) instead of `value`. `drilldownRows` carries the list through.
  `renderDrilldownTable` prints one `<th>` per measure from the labels Python built, and a
  Rows column only when the payload asks for one.
- A count column is printed plain even on a currency panel: `₹4` rows is not a price.
- `html_export.py` carries `measures` (column, aggregation, label, whether it is a count)
  and `show_row_count`, built from `vega_spec.measure_title`/`measure_label` so a card and a
  drill-down of the same number read alike.

## 4. The AI knows it can

- `_CORE_RULES`: the drill-down bullet gains "extra totals go in `more_measures`, one column
  each, and the axis is ignored".
- `_FIELDS_BY_KIND`: `_DRILLDOWN_EXTRA_FIELDS` gains `more_measures`, so a round can see the
  measures the table already has and add to them rather than replacing them.
- `describe_panel` names every total on a drill-down, the way it already names a chart's.
- `help.py`: the "several numbers" row says tables too, and the "not yet" list keeps only
  what is still true.

**Known limits (v1)**
- Still no search and no sorting on a drill-down, and groups are still ordered by label.
- Every measure shares the panel's one number format (counts excepted). Per-column formats
  would need a format per measure, which is a field per measure for a rare want.
- Four totals in all, the panel's own included.

## Done when
`uv run pytest` is green, and by hand: the original request — *"a drill-down table showing
Sum of Cost Amount, Sum of Quantity, Average, Minimum and Maximum of Basic Cost p.u. by
Category → Item Category Code → ItemName → VendorName"* — comes back with five columns, no
Rows column, and every level's totals matching a card over the same filter.
