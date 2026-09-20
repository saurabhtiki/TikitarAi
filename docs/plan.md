# Phase 34 — A wider vocabulary, so the chat has something to draw with

**Status: built. Tests written and run; see Verification below.**

Two things were found by building it rather than planning it, and both are fixed:

- **"Count unique customers" was refused for being text.** The old check read "anything but a
  count needs a number", which was right when there were five aggregations and wrong the
  moment `distinct` existed - it would have refused the most obvious use of the whole thing.
  `TEXT_FRIENDLY_AGGREGATIONS` now names the two that read a cell rather than a number.
- **A histogram described itself as "Sum of Amount"**, a chart it isn't drawing. It now says
  "how Amount is spread" in both the spec table and the AI's own summary.

One extra test file was added that the plan did not call for: `tests/test_live_dashboard_runtime.py`
lifts the card-maths functions out of `runtime.js` and **runs them in Node**. Asserting the
source text of a median only proves it was typed. Skipped where Node isn't installed.

## Context

The Dashboard tab today is driven by an **editable spec table** (phase 32) with a
**Describe a dashboard** button bolted on (phase 33). Reviewing it, the user changed
direction:

> *"Let's simplify — just keep AI chatbot mode, NL instructions go on updating the
> dashboard, plus some basic input parameters like title that we already have. We will add
> the Table stage in next version… shall we give some kind of Skill just like a Claude skill
> for a good professional dashboard?"*

So the destination is: **type → see it → say what to change → see it again → download**,
with a **design skill** (a written set of professional-dashboard rules sent with every
request) making the output look designed rather than dumped. The table stays on screen but
becomes **read-only** — a glance at what the AI built, not the thing you drive.

**That is phase 35.** This phase is the thing that has to happen first, and the reason is
concrete:

| | Where it happens | Can the vocabulary be free? |
|---|---|---|
| **The number** (sum, median, % of total) | Vega-Lite / `runtime.js` compute it from a named operation | **Yes** — phase 33's five was an arbitrarily small list |
| **The shape** (bar, heatmap, treemap) | Drawing code that must be **inside the exported HTML file** | **No** — a shape with no drawing code is a blank box in the reader's inbox, found *after* the file is emailed |

A design skill that says *"use a stacked bar, sort biggest-first, KPI cards on top"* writes
cheques the exported file can't cash — we have no stacked bar today. So: widen the
vocabulary now, chat and skill next.

**No change to how the page is used in this phase.** Everything below is testable with no
browser, no `AppTest` and no provider — which is what makes the split worth taking.

One small bug is folded in because it sits in the files this phase opens anyway: the UI's own
guidance example is *"always show currency in INR"*, yet `format:currency` prints `1,234.00`
with no symbol.

**Outcome:** a median, a count of unique customers, a percentage of total, a running total, a
stacked bar, a donut, a histogram, a heatmap, a box plot or a combo chart — all drawable.
Asking for a treemap gets a pie **plus a sentence saying why**, instead of silence.

---

## 1. Aggregations — `live_dashboard/model.py` gains its own superset

`analyst/charts.py`'s five (`AGG_SUM/COUNT/AVERAGE/MINIMUM/MAXIMUM`) are shared with Chat with
Data and report items and **must not change**. The dashboard declares its own list that starts
with those five and adds the rest, so the two stay compatible where they overlap:

```python
# live_dashboard/model.py
AGG_MEDIAN = "median"; AGG_DISTINCT = "distinct"; AGG_STDEV = "stdev"
AGG_Q1 = "q1"; AGG_Q3 = "q3"; AGG_FIRST = "first"; AGG_LAST = "last"
AGG_PERCENT_OF_TOTAL = "percent_of_total"; AGG_RUNNING_TOTAL = "running_total"

DASHBOARD_AGGREGATIONS: dict[str, str] = {**AGGREGATION_LABELS, AGG_MEDIAN: "Median", ...}
```

Two are **transforms, not aggregates**, and carry their own rules:

- **Percentage of total** — `joinaggregate` + `calculate`, so the bars add to 100%. Meaningless
  on a card with nothing to be a percentage *of*, so `panel_problems` refuses it there with a
  sentence.
- **Running total** — a `window` transform sorted by the group-by. Only when the group-by is a
  date; a running total over unordered categories is nonsense, and refusing it is cheaper than
  explaining a wrong chart later.

**Files:** `live_dashboard/model.py` (constants, `panel_problems` rules),
`live_dashboard/vega_spec.py` (`_VEGA_AGGREGATES`, `measure_title`, `_measure_field`, plus a new
`_transform_aggregation` for the two above), `live_dashboard/assets/runtime.js` (`aggregate()`
gains median / distinct / stdev / q1 / q3 / first / last for the **card** path),
`app_pages/dashboard_view.py::_panel_form` (the aggregation selectbox reads the new dict),
`live_dashboard/ai_spec.py` (import the wider dict; extend `_AGGREGATION_SYNONYMS` with
"unique", "distinct", "middle", "share", "% of total", "cumulative").

`vega_aggregate`'s existing contract — unknown falls back to `sum` with a log line — is kept.

---

## 2. Chart shapes — the half Vega-Lite gives nearly free

Added to `CHART_SUB_TYPES` in `live_dashboard/model.py`, encoded in `vega_spec.py::_encode_chart`:

| New sub-type | How it is built | Note |
|---|---|---|
| Stacked bar | existing bar + `colour_by` on `color` | today `colour_by` only tints |
| Grouped bar | the same plus `xOffset` on the colour field | the one-line difference from stacked |
| Donut | `arc` with a non-zero `innerRadius` (the code already sets `innerRadius: 0`) | |
| Histogram | `bin: true` on the measure, `count` on the other channel | **the first chart with no `group_by`** |
| Heatmap | `rect`, category on `x`, `colour_by` on `y`, measure on `color` | needs two categorical columns |
| Box plot | `mark: "boxplot"` | raw rows, like scatter — no aggregation |
| Combo | a two-`layer` spec: bar for the measure, line for a second | needs `measure_column_2` on `PanelSpec` |

Three break assumptions currently hard-coded; each needs its rule stated in `panel_problems`
rather than discovered as a blank panel:

- **Histogram has no group-by** — `panel_problems` must stop requiring one for this sub-type.
- **Heatmap and combo need a second column** — heatmap reuses `colour_by`; combo needs one new
  field on `PanelSpec` (`measure_column_2: str = ""`, serialised in `_panel_to_dict` /
  `_panel_from_dict`, absent from older JSON so it reads back as `""`).
- **Box plot and scatter are un-aggregated** — a sibling of `_ORDERED_KINDS`:
  `_RAW_ROW_KINDS = {CHART_SCATTER, CHART_BOXPLOT}`, so `_measure_field` skips the aggregate.

`SCHEMA_VERSION` stays **1**: every addition is a new field with a default, or a new value of an
existing string field — which `from_json` already tolerates.

**Deferred with the reason, so this phase has an edge:** pivot table and drill-down are new
runtime widgets, not Vega specs; treemap and map need Vega (not Vega-Lite) or geography files.
All four are real new payload work.

---

## 3. Falling back out loud — `live_dashboard/ai_spec.py`

Today `_resolve_sub_type` **drops** a row whose sub-type isn't in the catalog. Correct for a
shape with no honest equivalent; for most it throws away a good idea. One module constant:

```python
#: A shape we can't draw mapped onto the nearest we can. Every use is reported, never silent.
_SHAPE_FALLBACKS = {"treemap": CHART_PIE, "funnel": CHART_BAR_HORIZONTAL,
                    "bubble": CHART_SCATTER, "waterfall": CHART_BAR,
                    "sunburst": CHART_PIE, "radar": CHART_BAR}
```

`_resolve_sub_type` returns `(sub_type, note)` where a note is **informational, not a refusal** —
the panel is still built. `propose_dashboard` keeps these notes in the same `warnings` list the
dialog already renders, worded as a swap rather than a loss:

> *"'Share by product' asked for a treemap. This dashboard can't draw one yet, so it's a pie
> chart — change it in Edit if you'd rather have a bar."*

A shape with **no** sensible stand-in (`map`) still refuses and says why. No UI change needed:
`_render_ai_result` already lists warnings.

---

## 4. The currency symbol — `runtime.js` and the properties whitelist

`clean_properties` gains one key beside `format`:

```
format:currency, currency:INR
```

`currency` is validated against a short printable list (`INR USD GBP EUR AED JPY AUD CAD SGD`)
and dropped with a log line otherwise — the same discipline every other property follows, so
there is still no path from that cell to anything executable. `properties_text` round-trips it.

`runtime.js::formatNumber` uses `Intl.NumberFormat(undefined, {style: "currency", currency: code})`
inside a `try/catch` falling back to today's plain formatting — an invalid code throws
`RangeError`, and a card showing a bare number beats a card showing nothing. With `INR` this
gives `₹12,34,567.00`, grouped the reader's way.

`_panel_form` gains a currency selectbox shown only when the format is `currency` (unique `key=`,
`help=`). It survives phase 35 because the table stays on screen, read-only.

---

## 5. Verification

**`tests/test_live_dashboard_vega.py`** (extended)
- One test per new shape: stacked vs grouped bar differ only by `xOffset`; donut carries a
  non-zero `innerRadius`; histogram emits `bin` and no group-by channel; heatmap has two
  categorical channels with the measure on `color`; boxplot carries no `aggregate`; combo is a
  two-layer spec naming both measures.
- `percent_of_total` emits `joinaggregate` + `calculate`; `running_total` emits a `window`
  sorted by the group-by.
- A loop asserting **every** key of `DASHBOARD_AGGREGATIONS` maps to a Vega operation or a
  transform — so adding a tenth aggregation without wiring it fails here, not in a user's file.

**`tests/test_live_dashboard_model.py`** (extended)
- `percent_of_total` on a card refused with a sentence; `running_total` over a text group-by
  refused; a histogram with no group-by **accepted**.
- `clean_properties` keeps `currency:INR`, drops `currency:BITCOIN`; `properties_text`
  round-trips both halves.
- `measure_column_2` round-trips through JSON; a panel saved before this phase loads with
  `measure_column_2 == ""`.

**`tests/test_live_dashboard_ai_spec.py`** (extended)
- A `treemap` proposal becomes a **pie plus a note naming the swap** — the headline behaviour,
  and the one most likely to regress back into a silent drop.
- A `map` still refuses.
- The new aggregation words survive `_resolve_aggregation`, synonyms included.
- The prompt's catalog contains every new shape and aggregation, still rendered from the
  constants.

**`tests/test_live_dashboard_html_export.py`** (extended)
- One dashboard containing every new shape and aggregation exports without raising, and the
  payload names them.

**Run:** `uv run pytest tests/ -k "live_dashboard"`, then the full `uv run pytest tests/`.

**By hand, end to end:** Report Builder → sample tables → Dashboard tab → *"sales by category as
a stacked bar coloured by region, a median order value card in INR, a monthly running total, and
a treemap of products"* → confirm the treemap arrives as a pie with the sentence explaining it,
the card reads `₹…`, the running total climbs → download and open the file **with the network
disabled**.

---

## Where this leads — phase 35, agreed now, built next

Recorded so nothing here makes it harder. Not built in this phase.

- **Chat rounds.** Type, or leave the box blank and press Generate. Each round the AI edits the
  dashboard rather than replacing it. Download available at **any** round.
- **The dashboard is the AI's only memory.** `llm.client.run_structured` is deliberately
  stateless, so each round sends the current spec described in words (a new
  `describe_spec_for_prompt`, built on the existing `describe_panel`). A chat history would drift
  from the page; the page can't disagree with itself.
- **The design skill** — a written rules block appended to every prompt: KPI cards first, at most
  four; one trend full width; sort bars biggest-first; no pie over five slices; money in ₹;
  titles that name the number. This is where "professional" comes from, and it costs nothing but
  prose — provided the shapes in §2 exist.
- **Column descriptions reach the prompt.** `engine.session.schema_context()` (built by
  `engine/dictionary.py::schema_context`) already renders every column's description and synonyms
  for Chat with Data. `live_dashboard/ai_spec.py` does not use it today. Wiring it in is worth
  more than any table: *"Amount"* is a guess, *"Amount — invoice value after discount, INR"* is an
  instruction.
- **The table goes read-only.** Still on screen so you can see what was built at a glance; editing
  happens by talking. Editable again in a later version.

---

## Carried into phase 35 — found by the cleanup review, deliberately not done here

Three findings were real but too big to fold into a cleanup pass. Each is cheapest to do at
the start of phase 35, because phase 35 is what adds the consumers that justify them.

1. **One record per chart shape.** Shape knowledge sits in ~13 declarations across four files
   (`model.py`'s five sets, `vega_spec.py`'s six, `ai_spec.py`'s fallbacks and prompt prose,
   the form). Shape #14 means editing four files. A `ChartShape` record - label, vega mark,
   needs_colour, needs_second_measure, takes_group_by, raw_rows, clickable, stands_in_for -
   with every set derived from it makes that one row. The cost is already visible: this pass
   had to fix the form asking a histogram for a breakdown it ignores. Phase 35's design skill
   is a fifth reader of the same facts, and prose in `_INSTRUCTIONS` that no test ties to the
   sets is exactly what `describe_catalog_for_prompt` was written to avoid.
2. **Separate refusals from notes.** `_build_one` returns `(panel, warning)` where a warning
   means "dropped" or "changed, FYI" depending on whether the panel is None, and
   `propose_dashboard` flattens both into one list. A one-shot Generate can live with that; a
   chat round reporting "changed 2 things, dropped 1" cannot, and every round after the first
   inherits whatever shape that list has. A small `PanelOutcome(panel, refusal, notes)` before
   the rounds are built is much cheaper than after.
3. **One type map instead of two projections.** `available_columns()` throws each column's
   type away and `date_columns()` recovers one bit of it. `payload.column_type` already knows
   the whole answer. A single `{table: {column: type}}` built once would replace both
   arguments, remove `panel_problems`' optional "maybe you know the dates" parameter, and give
   `ai_spec._numeric_columns` - the same knowledge in a fourth place - somewhere to live.

---

## Judgement calls to flag during the build

1. **Combo adds a field to `PanelSpec` for one chart type.** Acceptable — but if a second
   multi-measure chart appears, `measure_column_2` should become a list before it becomes
   `measure_column_3`.
2. **A wider catalog is a longer prompt.** Measure it. If the catalog crowds out the schema on a
   small Light Model, send sub-type names only and keep the labels for the UI.
3. **`Intl.NumberFormat` currency support varies by browser** — exactly why the `try/catch` is
   mandatory rather than defensive decoration.
4. **`gauge` is deliberately not in the fallback table.** A gauge flattened to a card loses the
   dial entirely; refusing honestly reads better than a silent downgrade.
