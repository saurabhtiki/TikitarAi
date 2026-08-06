# Stage 6 — Agent execution + working Chat

Covers requirements §5.4 (the unfinished *update column values* half, plus the
conversational entry point), §5.5 (Agno + `DuckDbTools` agent) and §6.2 (output type
logic). Build-order item 5.

**Out of scope this phase:** Pin to Dashboard, the Dashboard page, HTML/Excel export
(build-order item 6). The pin button ships visible-but-disabled so the chat layout does
not move next phase.

---

## Where the previous phase left off

The Data Engine is complete and idle. Tables load into DuckDB as immutable `base_*` plus
mutable working tables, relationships are confirmed and enforced as real foreign keys
wherever the data is clean, the column dictionary is editable and AI-suggestable, and
`engine.session.schema_context()` already emits exactly the schema block an LLM needs.
The chat input on `app_pages/chat_with_data.py` is `disabled=True`.

This phase turns that input on.

---

## Design decisions

**1. `DuckDbTools` is subclassed, not used raw.** §5.5 names the toolkit, but the
upstream `run_query` returns a flat CSV *string*, silently drops everything after the
first `;`, and applies none of `engine/guards.py` — so `read_csv('C:/…')` and
`DROP TABLE users` would both sail through, and §5.5's "returns the result set as a
dataframe" would be unsatisfiable. Overriding one method fixes both.

**2. The agent's tool surface is read-only.** `include_tools` keeps only
`show_tables` / `describe_table` / `run_query`, dropping the eleven filesystem, S3 and
export tools. The override passes `allowed_tables=None` to
`engine.duckdb_session.run_query`, which means *no table may be mutated at all*. Column
changes never travel through the agent's tool; they go through `engine/columns.py` with
its probe-first, transaction-wrapped path.

**3. Output type and chart choice are deterministic, not model decisions.** §6.2 is a
keyword table, so it is implemented as a keyword table. That is what makes the chart, the
dataframe and the commentary all derive from one computed frame and therefore never
disagree — §6.2's own requirement. Commentary remains a separate narrow LLM call.

**4. Two doors, one room.** The Actions menu opens a dialog with dropdowns and a live SQL
preview. Typing the same request in chat reaches the *same* engine functions — the model
only fills in the blanks (table, column, formula, condition) and never writes the DDL/DML
itself. Both paths are equally guarded and equally replayable into a Task recipe (§7.5).

---

## New package `analyst/`

Mirrors the existing `auth/`, `llm/`, `cleaner/`, `engine/` layout: one Streamlit-free
module per concern, one `session.py` holding all Streamlit state, one `exceptions.py`.

| File | Contents |
|---|---|
| `exceptions.py` | `AnalystError` → `AgentRunError`, `ChartError`. |
| `tools.py` | `SessionDuckDbTools(DuckDbTools)`. Overrides `run_query` to route through `engine.duckdb_session.run_query`, record `(sql, frame)` on `.executions`, and return a row-capped text table to the model. Bad SQL comes back as text, not an exception, so the agent can self-correct. |
| `agent.py` | `answer_question(...) -> QueryResult{question, sql, frame, narrative, warnings}`. Builds the Agno `Agent` on `llm.client.build_model`. |
| `routing.py` | `classify_output(question, frame)` — the §6.2 table verbatim. `looks_like_column_action(question)` → `"add"` / `"delete"` / `"update"` / `None`. |
| `column_intent.py` | The conversational §5.4 path: one `llm.client.run_structured` call filling a Pydantic `ColumnAction`, dispatched to the deterministic engine functions. |
| `commentary.py` | `write_commentary(...)` — the separate narrow call of §6.2, with a `knowledge_base` parameter reserved for §7.2. |
| `charts.py` | `build_chart(frame, question)` → Plotly figure or `None`, chosen from frame shape and dtypes. |
| `session.py` | `an_` namespace: transcript, pending question, dialog flag. Bounds history so old frames and figures are dropped. |

## Modified

- **`engine/columns.py`** — new `update_column_values(connection, table, column,
  value_expression, condition=None)`, same probe-first / nothing-altered-on-failure
  contract as `add_calculated_column`. Fix `describe_statements`, which currently skips
  *every* `UPDATE` and would therefore render a standalone value update invisible in the
  change log; it should skip only the `UPDATE` half of a calculated-column pair.
- **`app_pages/chat_with_data.py`** — `st.menu_button` "Actions" replaces the two bare
  column buttons; new `_dialog_update_values` joins the existing `DIALOGS` dispatch; the
  chat input is enabled and the transcript replays through `st.chat_message`; the input is
  gated on an active session model; start-over also clears the transcript.
- **`pyproject.toml`** — add `plotly`. `kaleido` (PNG export) is deferred to the export
  phase.

## Reused, not rebuilt

`engine.session.schema_context` · `engine.dictionary.schema_context` · `engine.guards`
(both functions) · `engine.duckdb_session.run_query` · `llm.client.build_model` and
`run_structured` · `llm.session.active_profile` · the dialog open/close/pending state
pattern from `engine/session.py`.

---

## Tests

`uv run pytest`. No network: every LLM call is monkeypatched at the `llm.client` seam,
the same way `tests/test_llm_suggestions.py` already does it.

- `test_engine_columns.py` (extend) — update with and without `WHERE`, affected-row count,
  bad expression changes nothing, unknown column, smuggled statement refused, statement
  recorded and replayed in order by `relationships.enforce`, `describe_statements` shows a
  standalone `UPDATE` while still collapsing the calculated-column pair.
- `test_analyst_tools.py` — guard enforcement, DataFrame capture, large-result truncation,
  bad SQL returned as text.
- `test_analyst_routing.py` — the §6.2 table parametrised, plus each §5.4 phrasing.
- `test_analyst_charts.py` — chart type per frame shape; unplottable frame → `None`.
- `test_analyst_agent.py` — stub model: `sql` and `frame` land on `QueryResult`; a
  provider failure becomes `AnalystError`.
- `test_chat_with_data_page.py` (extend) — Actions menu, the update-values dialog, the
  no-active-model gate, a stubbed answer rendering, start-over clearing the transcript.

---

## Stage 6 revisions (after the first run-through)

Five changes from using the built page, all within Stage 6's scope.

1. **Agno session storage.** The agent was built fresh per question with no `db`, so it
   met every question with no memory of the last and follow-ups ("now by month instead")
   could not resolve. It now runs against an `agno.db.sqlite.SqliteDb` at
   `data/chat_sessions.db`, cached in `st.session_state` under `an_agent_db` and keyed by
   an `an_agent_session_id`, with `add_history_to_context=True` and `num_history_runs=5`.
   "Clear chat" and start-over rotate the session id rather than deleting anything, so a
   cleared screen still leaves the conversation in the store.

   **This departs from requirement 6**, which says nothing in Chat with Data is written to
   a database. It is deliberate: the stored runs are what a chat-history screen will be
   built from. What is written is questions and answers, never the uploaded data, and a
   stored conversation can be read but not re-run — the DuckDB tables it described are
   gone with the session. `docs/requirements.md` section 6 needs updating to match.
   Requires `sqlalchemy`, which Agno treats as optional and was not installed.
2. **Actions menu holds only what chat cannot do.** All three of requirement 5.4's
   column changes work conversationally, so their dialogs were removed — each was a
   second way to say one sentence. In their place is **Show current data**: a dialog
   listing each table's live rows and shape. A transcript records what was asked, not
   what the data became, so after a few conversational column changes "what do my tables
   look like now?" had no answer anywhere on the page. Send mail, add a reminder and add
   a task will join it.
3. **Every chart carries a title, a border and a legend.** Titles are built from the
   frame's own columns ("Min basic salary and Max basic salary by Department"), the
   border is a paper-referenced rectangle, and the legend sits inside the plot area so
   the border encloses it.
4. **One toolbar under the chat.** Actions, Clear chat and the change-log expander now
   share a horizontal `st.container` below the input instead of three columns above it,
   so they stay reachable as the transcript grows.
5. **Several measures now plot as several series.** `min`/`max`/`avg` by department came
   back as one label column and three numeric ones, and only the first was drawn. Every
   numeric column becomes its own grouped-bar or line series now, capped at
   `MAX_MEASURES`. With more than one measure the SQL's own `ORDER BY` is preserved —
   re-sorting by whichever measure happened to come first would override what was asked.

---

## Stage 6 revisions, round two

Three more changes, all within Stage 6's scope.

1. **Cross-table calculated columns.** *"Add performance_bonus = 10% of basic if
   Department is HR"* used to fail with a raw DuckDB `Binder Error` whenever the named
   column (`Department`) lived on a different table than the one being added to, because
   `engine.columns.add_calculated_column` only ever probed and ran against one table. It
   now accepts an optional `related_table` — the model names it, `engine/columns.py`
   looks up whether a **confirmed relationship** connects the two, and if `table` is the
   relationship's child (the "many" side) and `related_table` its parent (the "one"
   side), builds a real `UPDATE ... FROM ... WHERE` join using the relationship's own
   column names. The join is never taken from the model's own SQL — only a link the user
   already confirmed in Setup is usable, and only in that one direction, because the
   parent key's enforced uniqueness is what guarantees each child row matches exactly
   one parent row. The reverse direction (a parent column combining many child rows)
   would need an aggregate and is refused with a plain explanation rather than attempted.
   `ColumnAction` gained a `related_table` field and the model's instructions now say to
   qualify cross-table columns with the real table name, never an invented alias — the
   invented-alias case (`em.department` instead of `employee_master.department`) is
   exactly what broke originally.
2. **The "N column change(s)" log is gone.** Every change already shows up as its own
   chat message, and (since round one) `Actions → Show current data` shows the tables as
   they stand now — the running log of raw statements was a third view of the same
   information and added clutter rather than clarity.
3. **Setup/Chat toggle moved above Step 1.** It now renders directly under the page
   title, with the upload step underneath — collapsed to one line once files are loaded.
   Chat no longer sits below a full upload panel every time the view is switched.

---

## Stage 6 revisions, round three — charts the user can steer

*"Show bar chart of count by status, use department for legend"* produced a correct SQL
result and a chart that threw half of it away: `analyst/charts.py` read only the **first**
label column, so `department`/`status`/`count` drew as one flat run of same-coloured bars.
That is the case this round fixes, and the general problem behind it — a chart the user
cannot correct is a chart they have to re-ask the question to change.

The automatic chart stays automatic. Nothing is confirmed before drawing, because the
question is unambiguous most of the time and a dialog in front of every chart would be
friction paid on every question to fix a minority of them. The correction happens
**after**, on a chart already on screen, where the user can see what needs changing.

1. **A second label column becomes the legend.** With two label columns and a single
   measure, the second is passed as `color=` — a bar can encode a second category by
   colour *or* several measures by colour, never both, so the measures are trimmed to one
   and the narrowing is reported.
2. **Choosing and drawing are separated.** `choose_chart(frame, question) -> ChartChoices`
   decides; `render_chart(frame, choices, style) -> Figure` draws; `build_chart` is the
   two composed and is still what the pipeline calls. This is what makes the controls
   trustworthy: they open pre-filled with the choices that produced the chart on screen,
   and a hand-picked chart runs through identical drawing code. A test asserts that
   redrawing the automatic choices reproduces the automatic chart.
3. **Six chart types, gated by applicability.** Bar, bar-horizontal, line, area, scatter,
   pie. `available_chart_types(frame)` offers only what would draw something honest —
   scatter needs two numeric columns, pie needs at most `MAX_PIE_SLICES` non-negative
   parts of one whole — so the picker cannot be used to produce a broken chart. A test
   asserts every offered type actually renders.
4. **`ChartStyle`, kept apart from `ChartChoices`.** Title, palette, series layout
   (side-by-side / stacked / 100% stacked), value labels, legend, sort order, top-N and
   height. Two values rather than one because they have different lifetimes: switching a
   bar to a line changes the data mapping and must keep the title and colours the user
   chose. Every default reproduces the chart exactly as it was drawn before any of these
   existed — a test asserts `ChartStyle()` is byte-identical to passing nothing.
5. **Style controls appear only where they would do something.** No stacking control with
   one series, no sort along a time axis, no top-N on a chart that isn't capped, no
   single-colour palette once there are series to tell apart. A control that does nothing
   is worse than no control.
6. **The panel is two tabs inside one expander** — **Data** (type, x, values, colour) and
   **Style** — with a *Reset to automatic* button that clears both the stored values and
   the widgets, since controls still showing selections the chart no longer reflects would
   be worse than not resetting at all. `ChatMessage` gained `question` so the automatic
   chart can be worked out again on reset.
7. **Nothing here calls the model or re-runs SQL.** The frame is already in session state,
   so every redraw is a redraw. Two tests count `pipeline.answer` calls across a change of
   type and a change of style to hold that line.
8. **Failure never costs the chart.** An impossible combination keeps the previous chart
   and captions the reason; an empty Values box keeps the last chart; a legend column with
   too many values is dropped with a warning rather than refused. `render_chart` narrows,
   it does not raise.

**Pinning stays compatible.** A customized chart is a plain Plotly figure on
`message.figure`, so the next stage's *Pin to Dashboard* needs no special case — and
because `choices` and `style` are plain data beside it, a pinned tile can carry
`(sql, choices, style)` and stay re-renderable and re-editable rather than being a frozen
picture.

**Tests.** `test_analyst_charts.py` 47 → 110: two-label legend, type availability,
rendering chosen combinations, choices matching the automatic chart, and one class per
style dimension. `test_chat_with_data_page.py::TestChatPanel` → 26, covering the controls
rendering pre-filled, only supported types offered, redraws that don't re-ask the model,
style surviving a type change, the stacking control's applicability gate, and reset
clearing both halves.
