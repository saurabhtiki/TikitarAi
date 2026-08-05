# Stage 5: Data Engine core + LLM client

## Context

Stages 1–4 delivered login, LLM provider profiles, and the Data Cleaner. Every
remaining component — Chat with Data, Task Builder, Run a Task — sits on top of
the shared **Data Engine** (requirements §5): an in-memory DuckDB that loads
uploaded files, confirms relationships as real foreign keys, carries a column
data dictionary, and supports calculated columns. Nothing after this stage can
start until it exists.

Two things ride along, both because this stage is the first that needs them:

- **An LLM client** (`llm/client.py` + `agno`). §5.3 wants the Light Model to
  auto-suggest column descriptions, which needs a real client. Building it here
  also closes the two items deferred out of Stage 3 — **Test connection** (§3.4)
  and **per-session model selection** (§3.3) — and gives Stage 6's agent a
  working, tested client to start from.
- **A home for the UI.** The engine's setup screens *are* the front half of
  Chat with Data, so they are built as `app_pages/chat_with_data.py` with the
  chat panel showing a "next stage" placeholder. No throwaway UI.

**Out of scope** (Stage 6+): the Agno `DuckDbTools` agent, natural-language →
SQL, output-type logic, pin-to-dashboard, the Dashboard page, exports, Task
Builder, Run a Task.

---

## What the user sees

A new page **🔍 Chat with Data**, open to every logged-in role. Three numbered
setup steps stacked down the page, then the chat area. Each step collapses to a
one-line green summary once it is done, so a returning user sees progress at a
glance rather than a wall of controls.

```
🔍 Chat with Data
──────────────────────────────────────────────────────────────
  🧹 You have 3 cleaned tables in Data Cleaner   [ Use these ]
                       — or —
[ Drag CSV / Excel files here ]        Sheets: [Jan ×][Feb ×]

  ✅ Step 1 · Loaded 3 tables — sales (1,204 rows), customer (88), stock (310)
──────────────────────────────────────────────────────────────
  Step 2 · How the tables link up
  We found 2 likely links. Check them, then press Confirm.

   ● sales.cust_id  →  customer.id        98% match   [Accept][Edit]
   ● sales.sku      →  stock.sku          4 rows don't match  ⚠
                                          [See the 4 rows][Edit][Skip]
                                                     [ + Add a link ]
   ┌──────────┐        ┌───────────┐
   │ customer │◀───────│   sales   │──────▶│  stock  │   ← diagram
   └──────────┘        └───────────┘
                                          [ Confirm links ]
──────────────────────────────────────────────────────────────
  Step 3 · What the columns mean            [✨ Suggest with AI]

   table    column    type         meaning                also called
   sales    qty       numeric      Units sold per line    quantity, units
   sales    cust_id   id           Customer reference     customer id
   …                                            (editable grid)
                                          [ Save dictionary ]
──────────────────────────────────────────────────────────────
💬  Ask a question about your data
    Chat arrives in the next stage.        [+ Add a column]
```

**Step 1 — Upload.** Its own uploader, behaving like the Data Cleaner's:
multiple files, a sheet picker per Excel workbook, files read as text then typed
using the cleaner's existing detection. Each table gets a short SQL-safe name
shown back to the user.

*Plus a handoff from the Data Cleaner.* If the user cleaned tables earlier in
the same session, a **"Use these tables"** button appears above the uploader
listing them by name. See "How this connects to the Data Cleaner" below.

**Step 2 — Links between tables.** Only appears when two or more tables are
loaded. The app guesses the links by matching column names and then actually
checking how well the values overlap, and shows each guess in plain words
("98% of sales.cust_id values exist in customer.id"). Every control here opens
an `st.dialog`:

- **Edit / Add a link** → a dialog with four dropdowns (child table, child
  column, parent table, parent column) and a live verdict underneath.
- **See the rows that don't match** → a dialog listing the offending rows in
  full — every column's value, not a row number — so they are findable in the
  source file (§5.2). With a "Download these rows as CSV" button.
- **Confirm links** rebuilds the tables with real `FOREIGN KEY` constraints. A
  link that fails its check is *not* enforced and stays flagged; the rest are.

**Step 3 — Column dictionary.** One row per column across all tables, in an
editable grid: name, detected type, meaning, and other names it goes by. The
**✨ Suggest with AI** button fills the blank ones using the account's Light
Model (a dialog first shows how many columns will be sent and lets the user
cancel). Everything stays editable afterwards. If no Light Model is configured
the button is disabled with a tooltip pointing at Settings.

**Add a column** (§5.4) opens a dialog: pick a table, type a new column name
and a formula (`basic * 0.10`). The exact SQL that will run is shown before the
user commits, and the column can be removed again from the same dialog.

**Sidebar, on every page.** A model picker (§3.3) — *"Model: [My OpenAI ▾]"* —
plus a **Test connection** button that fires one tiny request and reports the
provider's own error text on failure (§3.4).

---

## How the collapsing steps work

Not `st.empty()` — that replaces content in place and is the wrong tool. In the
installed Streamlit (1.60) `st.expander` is a **stateful widget**, verified from
its signature and docstring:

```python
st.expander(
    label,                       # the one-line summary, e.g. "✅ Step 2 · 2 links confirmed"
    key=f"de_step_{n}",
    on_change="rerun",           # required — without it .open is always None
    icon=":material/check_circle:",
    expanded=not step_is_done,   # initial state only
)
```

- `on_change="rerun"` makes the container's **`.open`** attribute a real
  boolean, so a collapsed step's body never executes — the same gate
  `app_pages/data_cleaner.py:954` already uses on `tab.open`. That is what keeps
  a collapsed dictionary grid from re-running its preview queries.
- With `key=` set, the open/closed state is readable and writable at
  `st.session_state["de_step_links"]`.
- Programmatic opening and closing goes through the **deferred-flag pattern
  already in the codebase** (`cleaner/session.py::queue_start_over` /
  `consume_start_over`), since Streamlit forbids writing a widget's own key once
  that widget exists this run: the confirm action sets `de_pending_step_state`,
  and `engine/session.py::consume_step_state()` applies it at the top of the next
  run, before the expanders are created.

**Two corrections, both found by building it and both load-bearing:**

1. **`expanded=` is not merely an initial value.** Streamlit re-applies that
   argument whenever *its value changes between runs*, overriding the stored
   widget state. A dynamic `expanded=not loaded_tables` therefore force-collapses
   step 1 the instant a file loads — and keeps it collapsed. So every expander on
   the page passes a **constant** `expanded=`, and everything dynamic goes
   through `queue_step_state` / `collapse_once`.
2. **A widget that stops being rendered stops reporting its value.** Gating the
   *uploader* on `.open` meant that once step 1 auto-collapsed,
   `st.file_uploader` returned nothing and `sync_tables` dutifully dropped every
   table the user had loaded. The uploader is therefore instantiated on every
   run; only the per-table previews below it are gated on `.open`. Steps 2 and 3
   can be gated wholesale because all their state lives in `de_*` keys rather
   than in a widget.

The label itself is derived each run from engine state (`"✅ Step 1 · Your data —
3 table(s) — sales, customer, stock"`). Because the upload is processed *inside*
the expander whose label was already rendered, the page reruns once whenever the
table set changes, so no summary line is ever a run out of date.

## How this connects to the Data Cleaner

**Code is shared; state is handed over on request; neither page changes the
other's data.**

1. **Shared code.** `engine/loading.py` imports `cleaner.loaders.read_table`,
   `cleaner.profiling.detect_column_types` and `cleaner.pipeline.apply_steps`.
   There is no second file reader and no second type detector. Chat with Data's
   uploader is its own widget (`de_uploader`), independent of `dc_uploader`.
2. **Opt-in handoff.** Both pages live in one `st.session_state`, so if
   `cleaner.session.get_tables()` is non-empty a button appears offering those
   tables by name. Pressing it calls `cleaner.session.cleaned_table(table, bytes)`
   per table — the *already existing* cached derivation — and registers each
   returned frame into DuckDB. Roughly 30 lines in
   `engine/session.py::adopt_cleaner_tables()`, no new cleaning code.
3. **One-way and by copy.** The frames are snapshotted into DuckDB at the moment
   the button is pressed. Cleaning further afterwards does not retro-change the
   loaded tables; the button simply stays available to re-adopt. Chat with Data
   never writes back into `dc_*` state. This keeps §4's "independent of the
   Chat/Task/Run pipeline" true in substance while removing a pointless
   download-and-re-upload round trip.
4. **Not embedded.** The cleaning command bar is *not* rendered inside Chat with
   Data — §6.1 has it upload straight into the engine, and §7.1 makes
   upload→clean the Task Builder's job.

---

## Architecture: the three DuckDB facts that shape everything

Verified against current DuckDB docs, not assumed:

1. **Foreign keys cannot be added with `ALTER TABLE`.** They must be declared
   inside `CREATE TABLE`. So confirming relationships is not an `ALTER` — it is
   a **rebuild**: drop the working tables, re-create them in parent-before-child
   order with their constraints inline, and `INSERT … SELECT * FROM base_…`.
2. **A foreign key must reference a `PRIMARY KEY` or `UNIQUE` column.** The spec
   only asks for an orphan pre-check; DuckDB forces a second one — the parent
   key must be unique. Both checks run before enforcement, and both report
   offending rows in full.
3. **`ALTER TABLE … ADD COLUMN x AS (expr)` is not supported.** A calculated
   column is therefore two statements: `ADD COLUMN x <type>` (type learned from
   a `SELECT <expr> … LIMIT 0` probe) then `UPDATE t SET x = <expr>`. Both are
   shown to the user, and chained references (`net = basic - tax`) still work
   because `tax` is by then a real column.

### Table layers

| Layer | Name | Purpose |
|---|---|---|
| Base | `base_sales` | Immutable, exactly as loaded (§5.1). Never queried by the agent. |
| Working | `sales` | What everything queries. Rebuilt from base on every relationship change. |

Because a rebuild would otherwise wipe calculated columns, the ordered list of
calculated-column statements is kept in session state and **replayed after every
rebuild**. That list is also precisely what §7.5 requires Task Builder to
persist, so it is built in the shape Stage 7 will serialize.

### Relationship suggestion

For each pair of columns whose names plausibly refer to the same thing, run one
SQL query returning *containment* (share of child distinct values found in the
parent) and *parent uniqueness*. Ranking is by containment × uniqueness. Nothing
is auto-accepted — every candidate is reviewed (§5.2).

Name matching is a token-set comparison, not a single normalized string, because
one column legitimately goes by several names. A bare `id`/`code`/`key` reduces
to *its own table's* name — `customer.id` is "customer", not "id" — which is
what pairs it with `sales.cust_id` and also stops `customer.id` and `stock.id`
from looking like the same column. Abbreviations match by prefix down to three
characters, so §5.2's own `emp_id` → Employee Master works.

**Direction is decided by the data, then by the name.** The unique side is the
parent. On a tie — which is every genuinely one-to-one sample dataset — the side
whose column is a bare generic key wins, since that is a primary key naming its
own table while `cust_id` on `sales` is a reference outward. Without that
tiebreak the suggestion came out backwards as `customer.id → sales.cust_id`.

### SQL guardrails (§5.5)

`engine/guards.py::assert_safe_sql(sql, allowed_tables)` is written now, because
the calculated-column dialog already accepts free-text SQL from a user. It
rejects multiple statements, filesystem/extension access (`read_csv`,
`read_parquet`, `COPY`, `ATTACH`, `INSTALL`, `LOAD`, `EXPORT`), and any
`DROP`/`ALTER`/`CREATE` naming a table outside the session's own working set.
Stage 6's agent reuses it unchanged.

---

## File/module map addition

```
engine/exceptions.py       # DataEngineError hierarchy
engine/loading.py          # uploaded bytes -> typed DataFrame (reuses cleaner/)
engine/duckdb_session.py   # connection, table registration, DESCRIBE, guarded run_query
engine/relationships.py    # candidate detection, pre-checks, FK rebuild, DOT diagram
engine/dictionary.py       # ColumnEntry, dictionary build/merge, schema_context()
engine/columns.py          # calculated / dropped columns against working tables
engine/guards.py           # assert_safe_sql
engine/session.py          # ONLY streamlit importer: de_* keys, cached derivations
llm/client.py              # profile -> Agno OpenAILike; test_connection(); run_structured()
llm/suggestions.py         # Pydantic schema + Light-Model column-description call
llm/session.py             # active-session-model namespace (llm_* keys)
app_pages/chat_with_data.py
```

Same one-Streamlit-importer-per-package convention as `cleaner/`
(`cleaner/session.py`): `engine/session.py` and `llm/session.py` are the only
modules in their packages that import Streamlit, so everything else stays unit
testable without `AppTest`.

### Reuse — no new code for things Stage 4 already solved

| Need | Reuse |
|---|---|
| Read CSV/Excel bytes, sheet listing, encoding sniffing | `cleaner/loaders.py` — `read_table`, `list_sheet_names`, `is_csv` |
| Detect text/categorical/numeric/date/id | `cleaner/profiling.py` — `detect_column_types` |
| Apply that typing to a frame | `cleaner/pipeline.py` — `make_step` + `apply_steps` |
| Per-table session reconciliation on upload | mirror `cleaner/session.py::sync_tables` |
| Cleaned frames for the handoff button | `cleaner/session.py::get_tables`, `cleaned_table` |
| Deferred write to a widget's own key | `cleaner/session.py::queue_start_over` / `consume_start_over` |
| Collapsed-container execution gate | `app_pages/data_cleaner.py:954` (`tab.open`) → `expander.open` |
| Dialog-from-session-state idiom, `_footer` Apply/Cancel | mirror `app_pages/data_cleaner.py` |
| Encrypted API keys, profile CRUD, light-model flag | `llm/db.py`, `llm/crypto.py` — unchanged |
| Sidebar shell | `sidebar.py::render_sidebar` — extended, not replaced |

---

## Function contracts

**`engine/loading.py`**
- `prepare_table(file_bytes, file_name, sheet_name) -> tuple[pd.DataFrame, dict[str, str]]`
  — typed frame plus `{column: semantic_type}`, via the cleaner modules above.
- `prepare_cleaned_frame(frame) -> tuple[pd.DataFrame, dict[str, str]]` — the
  handoff path: a frame the Data Cleaner already typed, so this only reads back
  its semantic types rather than re-detecting.

**`engine/session.py`** (the Streamlit-coupled module)
- `connection() -> DuckDBPyConnection` — lazily created, one per session.
- `sync_tables(uploads, sheet_selection) -> list[EngineTable]` — both-directions
  reconciliation, mirroring `cleaner.session.sync_tables`.
- `adopt_cleaner_tables() -> list[EngineTable]` — the handoff button's action:
  reads `cleaner.session.get_tables()`, derives each via
  `cleaner.session.cleaned_table(...)`, registers it into DuckDB.
- `cleaner_tables_available() -> list[str]` — source labels, for the button's text.
- `queue_step_state(step, expanded)` / `consume_step_state()` — the deferred
  write to the expanders' own keys.
- `reset_engine()` — close the connection and clear every `de_*` key.

**`engine/duckdb_session.py`**
- `open_connection() -> DuckDBPyConnection` — `duckdb.connect(":memory:")`.
- `slugify_table_name(label, taken: set[str]) -> str` — SQL-safe, de-duplicated.
- `register_table(connection, table, frame) -> None` — creates `base_<table>` and working `<table>`.
- `describe_table(connection, table) -> list[tuple[str, str]]` — `(column, sql_type)`.
- `run_query(connection, sql, params=None) -> pd.DataFrame` — the single guarded
  execution point; wraps `duckdb.Error` into `DataEngineError` with the
  provider's own message preserved.

**`engine/relationships.py`**
- `@dataclass(frozen=True) Relationship(child_table, child_column, parent_table, parent_column)`
- `suggest_relationships(connection, tables) -> list[RelationshipCandidate]`
- `check_relationship(connection, rel) -> RelationshipCheck` — `.ok`,
  `.duplicate_parent_keys: pd.DataFrame`, `.orphan_rows: pd.DataFrame`, `.message`
- `enforce(connection, relationships, replay_statements) -> list[str]` —
  topological rebuild; returns the tables rebuilt. Raises `RelationshipError`
  on a cycle or a failed pre-check.
- `to_dot(tables, relationships) -> str` — DOT source for `st.graphviz_chart`.
  *(A plain DOT string renders client-side; the `graphviz` Python package is
  **not** needed — confirmed in the installed Streamlit's `marshall()`.)*

**`engine/dictionary.py`**
- `@dataclass ColumnEntry(table, column, sql_type, semantic_type, description, synonyms: list[str])`
- `build_dictionary(connection, tables, semantic_types) -> list[ColumnEntry]`
- `merge_edits(entries, edited_frame) -> list[ColumnEntry]`
- `sample_values(connection, table, column, limit=5) -> list[str]`
- `schema_context(entries, relationships) -> str` — the block injected into the
  agent prompt in Stage 6 and saved as the schema signature in Stage 7.

**`engine/columns.py`**
- `expression_type(connection, table, expression) -> str` — probe; raises
  `CalculatedColumnError` carrying DuckDB's message.
- `add_calculated_column(connection, table, column, expression) -> list[str]`
- `drop_column(connection, table, column) -> list[str]`
  Both return the exact SQL executed, for display (§5.4) and for the Stage 7 recipe.

**`llm/client.py`**
- `DEFAULT_BASE_URLS: dict[str, str]` — standard OpenAI-compatible endpoint per
  cloud provider, editable in the UI (§3.1).
- `build_model(profile: dict) -> OpenAILike` —
  `OpenAILike(id=profile["default_model"], api_key=…, base_url=…, timeout=…)`,
  key decrypted via `llm.crypto.decrypt_api_key`.
- `test_connection(profile) -> tuple[bool, str]` — one minimal `Agent(...).run()`;
  returns the provider's real failure text (§3.4).
- `run_structured(profile, prompt, output_schema, instructions=None) -> BaseModel | None`

> **API note, verified against the Agno docs MCP today, not from memory:** the
> parameter is `output_schema=` (not `response_model=`), the import is
> `from agno.models.openai.like import OpenAILike`, and `agent.run()` returns a
> `RunOutput` whose `.content` is the Pydantic object. One `OpenAILike` path
> serves local, cloud and custom profiles alike, since §3.1 makes `base_url`
> always editable. `strict_output=False` for `local` profiles — local servers
> frequently reject strict structured outputs.

**`llm/suggestions.py`**
- `class ColumnSuggestion(BaseModel)` — `table, column, description, synonyms: list[str]`
- `suggest_descriptions(profile, entries, samples, chunk_size=25) -> dict[tuple[str, str], ColumnSuggestion]`
  Chunked so a 200-column upload doesn't become one enormous prompt. A chunk
  that fails is logged and skipped; the rest still return.

---

## Session state

`de_*` for working data, `llm_*` for session LLM config — kept in separate
namespaces exactly as §3.3 requires.

```python
de_connection            # DuckDBPyConnection, one per Streamlit session
de_tables                # dict[table_id, EngineTable]  (file_id, sheet, table_name, semantic_types)
de_relationships         # list[Relationship] — confirmed and enforced
de_pending_relationships # list[RelationshipCandidate] — under review in step 2
de_dictionary            # list[ColumnEntry]
de_calculated_statements # list[str] — ordered; replayed after every rebuild
de_uploader              # this page's own uploader, separate from `dc_uploader`
de_step_1 / de_step_2 / de_step_3   # st.expander widget keys (open state)
de_pending_step_state    # deferred collapse/expand, applied before the expanders render
de_dialog                # {table, action} — one key, `dc_open_dialog` idiom
de_rebuild_count         # bumped on every rebuild; part of the preview cache key
llm_active_profile_id    # per-session model choice (§3.3)
```

The connection lives in `st.session_state`, **not** `st.cache_resource` — the
latter is process-wide and would leak one user's tables into another's session,
against §5.1's "scoped to the current session". `logout()`'s existing
`st.session_state.clear()` therefore already tears the engine down.

No DataFrames in session state; previews come from `run_query` behind
`st.cache_data(scope="session")`, keyed on table name plus a rebuild counter.

---

## Failure semantics

| Class | Answer |
|---|---|
| A file can't be read | `TableLoadError`, that file skipped, others load |
| Parent key not unique | Relationship not enforced; duplicate rows shown in full + CSV download |
| Child rows have no parent | Relationship not enforced; offending rows shown in full + CSV download (§5.2) |
| Relationship set contains a cycle | Nothing rebuilt; the cycle named in the message |
| Bad calculated-column expression | Probe fails first; DuckDB's own message shown; nothing altered |
| SQL fails the guard | `DataEngineError` naming what was rejected; never executed |
| Light Model unreachable / bad key | Suggestion skipped, descriptions stay editable, provider's error surfaced |

Enforcement is all-or-nothing per rebuild: if any table fails to create, the
previous working tables are restored from base so the session is never left
half-built.

---

## Wiring

- `pyproject.toml`: add `duckdb>=1.4`, `agno`, `openai`, `pydantic`.
- `streamlit_app.py`: add `st.Page("app_pages/chat_with_data.py", title="Chat with data", icon=":material/forum:")` as a new **"Explore"** section above `Utilities`. No new SQLite tables, so `bootstrap_database()` is unchanged.
- `sidebar.py`: `render_sidebar(profile)` gains the active-model `st.selectbox` + **Test connection** button, so the choice follows the user across pages.
- `app_pages/settings.py`: the existing LLM-profile dialogs gain a **Test connection** button now that a client exists.

---

## Testing checklist

- [ ] `tests/test_engine_loading.py` — CSV/Excel → typed frame; `id` columns keep leading zeros through DuckDB
- [ ] `tests/test_engine_duckdb.py` — register/describe/query; base table survives a working-table rebuild; two connections stay isolated
- [ ] `tests/test_engine_guards.py` — multi-statement, `read_csv`, `ATTACH`, and out-of-session `DROP` all rejected; ordinary `SELECT`/expression accepted
- [ ] `tests/test_engine_relationships.py` — suggestion ranking and direction; duplicate-parent-key detection; orphan rows returned **in full**; successful FK rebuild rejects a later orphan `INSERT`; cycle refused; calculated columns survive a rebuild; `to_dot` shape
- [ ] `tests/test_engine_columns.py` — add / chained add / drop; type probe; bad expression raises with DuckDB's message; returned SQL is exactly what ran
- [ ] `tests/test_engine_dictionary.py` — build, edit-merge, `schema_context` contains table, column, type, description, synonyms and FK lines
- [ ] `tests/test_llm_client.py` — `build_model` maps each provider type and decrypts the key; `test_connection` success/failure paths — **monkeypatched, no network**
- [ ] `tests/test_llm_suggestions.py` — chunking, merge into entries, a failing chunk is skipped not fatal (monkeypatched)
- [ ] `tests/test_chat_with_data_page.py` — `AppTest`: any role reaches the page; upload registers tables; step 2 hidden for one table, shown for two; confirming a clean link enforces it; a link with orphans is flagged, not enforced; a collapsed step's body does not execute (`expander.open` gate); the deferred step-collapse flag is consumed exactly once
- [ ] `tests/test_engine_handoff.py` — with `dc_tables` populated, `cleaner_tables_available()` lists them and `adopt_cleaner_tables()` registers the *cleaned* frames (a recorded rename/type step is visible in DuckDB); with `dc_tables` empty, no button state is produced; adopting twice re-snapshots rather than duplicating tables
- [ ] `uv run pytest` passes
- [ ] Manual `streamlit run`: upload 3 related files, accept a link, hit a deliberate orphan and read the offending rows, run **Suggest with AI** against a real Light Model, add `tax = basic * 0.10` then `net = basic - tax`, and confirm both survive re-confirming the relationships

**Known testing gaps** (same category as Stages 2–4): `st.data_editor` has no
`AppTest` accessor, so the dictionary grid's *editing* is covered at the
`dictionary.merge_edits` layer and click-through is verified manually. Live LLM
calls are never made in tests.

---

## Risks carried into implementation

- **DuckDB FK eager-evaluation.** DuckDB's docs warn that the ART index backing
  a foreign key can evaluate constraints "too eagerly". If a legitimate rebuild
  trips this, the fallback is to keep the pre-checks (which are the real
  correctness guarantee) and downgrade the constraint to metadata for that one
  pair, flagged in the UI rather than silently.
- **Column-name collisions across tables.** `Customer.Name` vs `Stock.Name` is
  called out in §5.3; the dictionary is keyed on `(table, column)` throughout
  and `schema_context` always qualifies names, so this is handled by
  construction — but it is the thing to watch when Stage 6 writes joins.
- **Local models and structured output.** If `output_schema` proves unreliable
  against LM Studio/Ollama, the fallback is a plain-text JSON prompt parsed with
  `json.loads` behind the same `suggest_descriptions` signature.

## Phase completion

Write this plan into `docs/plan.md` (replacing the current empty file), then on
green: update "Current phase" in both `CLAUDE.md` and `AGENTS.md` to
*"Stage 5: Data Engine core + LLM client — complete"*, and run `/code-review`
against the conventions list.
