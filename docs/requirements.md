# Data Tools App — Requirements Specification

## 1. Overview

A multi-page Streamlit application with three core components, plus a
library of standalone utilities, sitting behind a role-based login system.

- **🔍 Chat with Data** — ad hoc, session-only data exploration. Upload
  files, confirm relationships, chat in natural language, pin useful
  outputs to a personal dashboard, download as HTML or Excel. Nothing here
  persists beyond the session.
- **🛠️ Task Builder** (admin/superuser only) — an authoring workshop that
  streamlines the full pipeline (upload → clean → link → chat → structured
  report) and saves the entire pipeline as a reusable **Task**.
- **▶️ Run a Task** (any user) — pick a saved Task, upload new files, get
  the same report back automatically.

Both Chat with Data and Task Builder run on a shared, in-memory **DuckDB
data engine** (Section 5) that handles file loading, relationship
management, joins, and calculated columns via real SQL.

An **Agno**-based agent (using the `agno.tools.duckdb.DuckDbTools`
toolkit) is the execution layer that turns natural-language questions into
SQL, runs them against DuckDB, and returns results for display as
dataframes, charts, or written commentary.

---

## 2. Authentication & User Management

### 2.1 Login
- A login gate sits in front of the entire application — no page is
  reachable before authentication.
- User accounts are stored in a SQLite table:
  - `user_id` (surrogate primary key, auto-increment or UUID)
  - `email` (unique)
  - `name`
  - `photo_path` (file path on disk; nullable — photo is optional)
  - `password_hash` (bcrypt or argon2 — passwords are never stored in
    plain text)
  - `role` (`superuser` | `admin` | `normal_user`)
  - `created_at`
- Authentication is a simple hand-rolled SQLite + hashed-password system
  (no external identity provider).
- After login, `st.session_state` holds only the current `user_id`,'email' and
  `role`; full profile details are fetched from SQLite on demand.
- Logging out clears all session state, including any LLM connection
  selected for that session (Section 3).

### 2.2 Roles & Permissions

| Capability | Superuser | Admin | Normal user |
|---|:---:|:---:|:---:|
| Use Chat with Data | ✅ | ✅ | ✅ |
| Use standalone Utilities | ✅ | ✅ | ✅ |
| Manage own password / photo | ✅ | ✅ | ✅ |
| Manage own saved LLM provider profiles | ✅ | ✅ | ✅ |
| Run an existing Task | ✅ | ✅ | ✅ |
| Build/save a new Task (Task Builder access) | ✅ | ✅ | ❌ |
| Add / delete user accounts | ✅ | ❌ | ❌ |
| Change any user's role | ✅ | ❌ | ❌ |
| Be deleted | ❌ (never) | ✅ (by superuser) | ✅ (by superuser) |

- **Normal user:** full use of Chat with Data, Utilities, and any existing
  Task, but cannot create new Tasks.
- **Admin:** everything a normal user can do, plus access to Task Builder.
  No user-management rights.
- **Superuser:** everything above, plus sole authority to add/delete users
  and change roles.

### 2.3 First-Run Seeding
- When the database is created for the first time (no users exist yet),
  the app automatically creates one default account:
  - Email: `admin@admin.com`
  - Password: `nimda`
  - Role: `superuser`
- This account can never be deleted, enforced at the application level
  regardless of who attempts it.
- No forced password change is required — the default password remains
  valid until the user changes it voluntarily via self-service.

### 2.4 User Management Screen
- Visible only to the `superuser` role.
- Lists all users: name, email, photo thumbnail, role.
- Superuser can add a user, delete any user (except the seeded superuser
  account), and change any user's role.
- All users manage their own password and photo from their own profile
  page, not from this screen.

---

## 3. LLM Provider Configuration

Each user manages their own LLM connections, persisted per account so they
don't need to be re-entered every session.

### 3.1 Saved Provider Profiles
- A user can save one or more named connection profiles:
  - `profile_id`, `user_id` (owner), `nickname` (e.g. "My OpenAI," "Office
    LM Studio"), `provider_type` (Local / Cloud / Custom), `base_url`,
    `api_key` (nullable — not required for local providers), `default_model`.
- **API keys are encrypted at rest** using an application-level encryption
  key (not plain text in the database). Saving a key is an explicit,
  opt-in action — the UI clearly states the key is stored encrypted and can
  be removed at any time.
- Provider type determines which fields are shown:
  - **Local** (LM Studio / Ollama): base URL + model name, no key needed.
  - **Cloud** (OpenAI, Anthropic, Google, Groq, etc.): API key (masked
    input) + model name; base URL defaults to the provider's standard
    endpoint but remains editable (for proxies/gateways).
  - **Custom / OpenAI-compatible**: free-text base URL + optional key.
- Model names are free-text input, not a hardcoded dropdown, since
  provider model catalogs change frequently.

### 3.2 Light Model Profile
- In addition to their regular provider profiles, each user configures one
  **Light Model** — a separate, smaller/faster/cheaper model used
  automatically for lightweight, high-frequency auxiliary tasks (e.g.
  auto-suggesting column descriptions in Section 5.3). This uses the same
  profile structure as Section 3.1 (nickname, provider_type, base_url,
  api_key, model) but is flagged as the account's designated light-task
  model.
- The Light Model is applied automatically wherever the app performs a
  small, structured auxiliary task — the user does not need to select it
  per action, only configure it once in their profile settings.

### 3.3 Per-Session Selection
- After login, the user picks which saved profile is the **active session
  model** from a dropdown (plus an "Add new..." option to create one
  on the spot without saving it, if preferred). This choice can be
  changed at any point mid-session.
- The Light Model, once configured, is used automatically in the
  background and does not need reselecting each session.
- Session LLM configuration (which profile is active, any temporarily
  unsaved connection details) lives in its own `st.session_state`
  namespace, separate from working data (uploaded files, chat history,
  dashboard state).

### 3.4 Connection Validation
- A "Test connection" action is available for both the main session model
  and the Light Model — fires one minimal request and reports success or
  the provider's actual failure reason (invalid key, rate limited,
  unreachable host) before the user starts working.

---

## 4. Utilities

A category of standalone, single-purpose tools, open to all logged-in
users, independent of the Chat/Task/Run pipeline.

### 4.1 Data Cleaner

**Flow:** Upload → Clean → Download.

1. **Upload** — one or more CSV/Excel files; a sheet picker appears for
   multi-sheet Excel files.
2. **Clean** (tabbed interface, one tab per uploaded file):
   - Skip junk rows from the top or bottom
   - Remove empty rows (fully blank, or blank across a chosen subset of
     columns)
   - Delete columns
   - Rename columns (duplicate resulting names are blocked)
   - Column type detection and override: `text`, `categorical`, `numeric`,
     `date`, `id`
     - `categorical` is auto-suggested for text columns with low
       cardinality relative to row count (e.g. Department, Status,
       Region) — useful downstream for chart axis/grouping suggestions
   - Fix numbers stored as text (currency symbols, thousands separators)
   - Trim excess whitespace (one click, all text columns)
   - Remove/replace special characters (one click, all text columns)
   - Change letter case (upper/lower/title) per column
   - Custom find & replace, with regex and case-match toggles, scoped to
     one or more chosen columns
   - Per-column missing-value strategy: fill zero, fill mean, fill median,
     fill "Unknown", drop affected rows, or leave as-is
   - Duplicate row detection and removal, with a user-chosen subset of
     columns defining what counts as a duplicate
   - A full, human-readable cleaning log per file, and a "reset to raw"
     action that discards all cleaning steps for that file
   - An editable output sheet name per file, auto-sanitized and
     de-duplicated against Excel sheet-naming rules (31-character limit,
     forbidden characters)
3. **Download** — a single click produces one multi-sheet `.xlsx`
   workbook containing every cleaned table.

*This section is intentionally open-ended — additional standalone
utilities can be added here over time.*

---

## 5. Data Engine

The shared, in-memory DuckDB foundation used by both Chat with Data
(Section 6) and Task Builder (Section 7).

### 5.1 Loading Data
- Every uploaded file is loaded into an in-memory DuckDB instance as a
  base table, scoped to the current session (Chat with Data) or authoring
  session (Task Builder).
- Base tables are immutable. Calculated-column and delete-column actions
  (Section 5.4) are applied to a separate working table, never overwriting
  the original imported data — this preserves a clean path back to the
  original if needed.

### 5.2 Relationship Detection & Confirmation
- Relevant whenever more than one file is uploaded.
- The system auto-suggests candidate relationships using SQL-based checks:
  matching column names plus a value-overlap query between candidate
  columns.
- Suggestions are shown as a reviewable list. The user can **accept**,
  **edit**, or manually **add a new** relationship (choosing left
  table/column and right table/column directly).
- A visual relationship diagram (a box per table, a line per confirmed
  relationship) is shown once more than two or three tables are involved,
  to make multi-table structures easy to read at a glance.
- **A confirmed relationship is always usable for querying, whether or not
  it becomes a database constraint.** What the agent (Section 5.5) actually
  needs is to know which two columns join — not a guarantee that every row
  matches. A relationship the user accepts is recorded and included in the
  schema context either way, so "show attendance by employee" always
  produces the right join, even when a few Attendance rows have no matching
  Employee Master record.
- **The system checks each relationship and shows the match, not a
  pass/fail gate.** Before the user accepts a suggestion, a validation
  query finds every row on the child side whose key value has no match on
  the parent side (e.g. every row in Attendance whose `emp_id` doesn't
  exist in Employee Master), and the result is shown plainly — "100%
  match" or "2 rows don't match". If any don't match:
  - The exact offending rows are shown to the user in full (every column's
    value, not just a row number — this is easier to locate and fix in the
    source file than a bare row index), with a CSV download.
  - The user may fix the source data and re-upload, or simply accept the
    relationship as-is — it's still recorded for querying.
- **A relationship is enforced as a real DuckDB `FOREIGN KEY` only when its
  check comes back completely clean.** This gives financial-data accuracy
  a real database guarantee wherever the data supports it, without
  blocking every other relationship on a full clean-up first. A
  relationship with unmatched rows is used for joins as `LEFT JOIN`
  instead, so downstream questions still work — the unmatched rows just
  don't contribute a match, the same way they wouldn't in Excel.
- Once relationships are confirmed, the system moves directly into chat.
  The agent (Section 5.5) writes the correct SQL `JOIN` for each question
  directly, using the confirmed relationships (foreign keys where enforced,
  `LEFT JOIN` where not) — this covers any number of related tables,
  including a table linking to several others (e.g. Sales linking to both
  Customer and Stock) and multi-level chains (e.g. PO Details → PO Header →
  Inventory Master), with no additional join configuration needed.

### 5.3 Column Descriptions & Aliases
- After relationships are confirmed and before chat begins, the user sees
  an editable data-dictionary table: one row per column across all
  uploaded tables — column name, detected type, and an editable
  description/alias field.
- **Auto-suggestion:** the account's configured Light Model (Section 3.2)
  proposes a plausible description from the column name and a few sample
  values (e.g. `qty` with sample values `5, 12, 3` → "Number of units sold
  per transaction"). The user reviews and edits rather than writing every
  description from scratch.
- A column can carry multiple synonyms, not just one description (e.g.
  `qty` → "quantity, units, units sold, count"), to better match varied
  phrasing in user questions.
- This data dictionary is injected into the schema context given to the
  agent for every query, directly improving SQL-generation accuracy — and
  it also resolves ambiguity between similarly-named columns from
  different joined tables (e.g. `Customer.Name` vs. `Stock.Name`).
- In Chat with Data, descriptions apply only for the current session. In
  Task Builder, descriptions are saved as part of the Task's schema
  signature (Section 7.5) so they don't need re-entering every time the
  Task is reused.

### 5.4 Calculated & Deleted Columns , Update Column values
- The user can add or remove columns conversationally during chat, e.g.:
  - *"Add tax = 10% of basic"* → executes an `ALTER TABLE ... ADD COLUMN
    tax AS basic * 0.10` statement against the working table.
  - *"Add net_salary = basic - tax"* → references `tax` normally, since it
    now exists as a real column from the prior step. Because each
    statement runs sequentially against the same working table, later
    statements can reference anything added earlier — chained calculated
    columns work with no special handling required.
  - *"Delete tax"* → executes `ALTER TABLE ... DROP COLUMN tax`.
  - A formula may also read a column from one other, already-linked table,
    e.g. *"Add performance_bonus = 10% of basic if Department is HR, else
    1%"* where `Department` lives on a related table, not `salary` itself.
    This only works from the "many" side of a confirmed relationship
    (Section 5.2) into the "one" side — `salary` reading `employee_master`,
    not the reverse, since combining several rows into one would need an
    aggregate. The join used is always one already confirmed in Setup,
    never a condition the model supplies.
- The user can update column values conversationally during chat, e.g.:
  - Mark *status* as Over due if Due date is less than Today
- All calculated/delete actions apply to the working table (Section 5.1),
  never the immutable base table.
- The actual SQL statement executed is shown in the chat for transparency.
- These changes persist for the remainder of the session (or authoring
  flow, in Task Builder) and are reflected in every subsequent query and
  in the final report.
- In Task Builder, each calculated/delete-column statement is captured in
  order as part of the Task recipe (Section 7.5) and replayed on reuse.

### 5.5 Agent Execution
- A single Agno **Agent** configured with the `agno.tools.duckdb.DuckDbTools`
  toolkit, which lets the agent write and execute SQL directly against the
  session's DuckDB instance.
- The agent's schema context for every query includes: table and column
  names, the foreign key relationships (Section 5.2), and the
  column descriptions/aliases (Section 5.3).
- Per question, the agent:
  1. Writes the correct SQL (including any necessary joins) against the
     known schema.
  2. Executes it via `DuckDbTools`.
  3. Returns the result set as a dataframe for the output-type logic
     (Section 6.2) to handle.
  4. Handles calculated/delete-column requests (Section 5.4) as DDL/DML
     rather than `SELECT` statements.
  5. Handels update Column values request (section 5.4)
- **Guardrails:** agent-written SQL is scoped strictly to the current
  session's own DuckDB tables — no filesystem access, no
  dropping/renaming tables outside the session's own working tables, and
  nothing is persisted to disk unless the user explicitly exports.
- The agent runs against whichever provider/model is set as the active
  session model (Section 3.3).
- **Session storage:** the agent's own conversation history (questions,
  answers, the SQL it ran) is persisted via Agno's session storage, in its
  own SQLite database separate from the app's user/profile database. This
  is what a future chat-history view is built from. It is the one part of
  Chat with Data written to disk — see Section 6's note on this.

---

## 6. Chat with Data

Open to all users. The uploaded data itself is session-only — nothing about
the tables, links or column dictionary is written to the database; it's a
fast, disposable exploration tool. The chat conversation is the one
exception: questions and answers (not the underlying data) are persisted so
a chat history can be shown to the user, per requirement 5.5's agent
session storage.

### 6.1 Flow
1. Upload one or more files. The Data Engine (Section 5) loads them into
   DuckDB, and — if more than one file was uploaded — the relationship
   confirmation step runs before proceeding.
2. Chat in natural language: ask questions to get dataframes, charts, or
   written commentary; add, remove or update the values of a calculated
   column conversationally.
3. Every chat output has a **"📌 Pin to Dashboard"** button — a single
   click, with no additional prompts, so the chat flow stays uninterrupted.
4. Pinned items accumulate in an unplaced pool held in `st.session_state`,
   visible on the Dashboard page.
5. The agent's own record of the conversation (questions, answers, the SQL
   run) is stored in its session database, separate from the app's own
   SQLite file, so past conversations can be listed back to the user later.
   A stored conversation is a record of what was asked, not something that
   can be re-run — the DuckDB tables it was asked about no longer exist
   once the session ends.

### 6.2 Output Type Logic

| Signal | Output |
|---|---|
| Keyword: chart / graph / plot / visuali[sz]e | Chart |
| Keyword: table / dataframe / list / "show me the data" | Dataframe |
| Keyword: why / explain / summar[yi] / insight | Written commentary |
| Keyword: all / everything / full breakdown | All three |
| No keyword, result has multiple rows | Dataframe with Written commentary(default) |
| No keyword, result is a single value | Written commentary (default) |

- Written commentary is produced via a separate, narrow LLM call, distinct
  from the SQL-generation call — each call has one job, which improves
  reliability.
- When multiple output types are requested together, all three are derived
  from the same computed result, so they never disagree with one another.

### 6.3 Dashboard Page
- Starts blank, aside from an optional title.
- Displays the pool of pinned-but-unplaced items alongside a lightweight
  structure builder: add a title, add sections, add subsections.
- For each pinned item,The user can add Heading & Comments(both optional & added manually)
- The user assigns each pinned item (alongwith Heading & Comments)into a section/subsection.
- Items, subsections, and sections can each be reordered among their
  siblings via up/down controls and a position-jump input; an item can also
  be moved to a different subsection / section.
- This page and its contents exist only for the current session — closing
  or refreshing without downloading loses the dashboard. There is no
  database persistence for this component.

### 6.4 Export
- Two formats are available: **HTML** and **Excel**.
- **HTML** — a single, self-contained file built via a Jinja2 template
  that loops over the section/subsection/item tree; charts are embedded as
  base64 images, tables as real `<table>` elements, and all CSS is
  contained in one `<style>` block within the file.
- **Excel** — one sheet per subsection; charts embedded as images, text
  summaries placed as cells alongside the relevant tables; full data with
  no row limits.
- No PDF or Word export is available for the Dashboard.

---

## 7. Task Builder

Restricted to `admin` and `superuser` roles. This is where a full pipeline
— from raw file upload through a finished, deliberately structured report —
is assembled once and saved as a reusable **Task**.

### 7.1 Flow
1. **Name the task** (e.g. "Salary Processing").
2. **Upload sample files and clean them**, using the same cleaning actions
   available in the Data Cleaner utility (Section 4.1). Every cleaning
   action is recorded with its exact structured parameters (not just a
   display string), in the order applied, so the sequence can be replayed
   later.
3. **Data Engine setup** (Section 5): confirm relationships, review and
   edit column descriptions. Everything configured here is recorded for
   later reuse.
4. **Build the report, structure-first:**
   - Define the report skeleton before chatting: header, footer, title
     page, and a Section → Subsection tree, each subsection with its own
     name. Sections and subsections are auto-numbered based on their
     position in the tree, and renumber automatically if reordered.
   - Ask questions in chat as usual; each output has a **"💾 Save to
     Report"** button that opens a small dialog to choose the target
     Section → Subsection (with an optional title override, defaulting to
     the question asked).
   - Items that aren't assigned to a section at save time go into an
     "Unassigned" holding area rather than being dropped.
   - Items, subsections, and sections can each be reordered among their
     siblings (up/down controls, position-jump input, and — for items only
     — a "move to a different subsection" action).
5. **Attach a knowledge base** (Section 7.2) to guide how AI-generated
   commentary should interpret the data.
6. **Save as Task**, capturing the full recipe (Section 7.5).

### 7.2 Report Knowledge Base
- A free-text field attached to the report (one per report, not per
  section) where the task author writes domain rules or context for the
  AI to apply when generating written commentary — for example: *"This
  report covers accounts receivable. If a customer's outstanding balance
  is overdue more than 90 days, flag it as high risk. If overdue 30–90
  days, note it as a follow-up item."*
- Written in plain English and interpreted directly by the LLM — no
  structured rule-editor is needed.
- This text is passed as additional context into the dedicated
  commentary-generation call (Section 6.2), kept separate from the
  SQL-generation call.
- Because it's attached to the Task's report, it's automatically applied
  every time that report is produced — whether during authoring or during
  a later Task run.

### 7.3 Export Formats
- Uses the same Jinja2-based HTML rendering approach as Chat with Data's
  Dashboard (Section 6.4), plus two additional formats:
  - **HTML** — as described in Section 6.4.
  - **PDF** — generated by converting the same HTML through an
    HTML-to-PDF engine, which handles pagination, page numbers, and footer
    placement.
  - **Word** — generated by converting the same HTML through an
    HTML-to-docx engine, preserving Section/Subsection headings as real
    Word heading styles (enabling a working, auto-generated Table of
    Contents).
  - **Excel** — one sheet per subsection, charts embedded as images, text
    summaries placed as cells; full data, no row limits.
- Row caps apply to large tables in the Word and PDF exports (e.g. top 50
  rows with a note indicating the full data is available in the Excel
  export); this cap is configurable. HTML and Excel exports always contain
  full data.

### 7.4 What Gets Captured in a Task
- **Schema signature per file role** — recorded column names, data types,
  and their descriptions/aliases (Section 5.3).
- **Cleaning action sequence per file role**, with structured parameters,
  in the exact order applied.
- **Relationships** — the confirmed foreign key relationships from Section 5.2.
- **Calculated/deleted column statements**, captured in order.
- **Chat items** — the original questions, their output type, and their
  section/subsection placement.
- **Report skeleton** — header, footer, title page, section/subsection
  structure, and the knowledge base text (Section 7.2).

### 7.5 Storage
- SQLite table: `task_id`, `user_id` (owner), `name`, `description`,
  `created_at`, `task_json`.
- `task_json` holds the full recorded recipe:
  ```
  {
    expected_schemas: { role: { columns: { name: {dtype, description, aliases} } } },
    cleaning_steps: { role: [ ordered action list ] },
    relationships: [ ... ],
    calculated_columns: [ ordered SQL statement list ],
    chat_items: [ ... ],
    report_skeleton: { header, footer, title_page, sections: [...], knowledge_base }
  }
  ```
- Tasks are owned by the user who created them, with rename, duplicate, and
  delete actions available to the owner.

---

## 8. Run a Task

Available to any logged-in user.

### 8.1 Flow
1. Select a saved Task from the list.
2. Download a sample file template, generated automatically from the
   Task's recorded schema — correct column headers plus a handful of
   plausible dummy sample rows appropriate to each column's data type, and
   a short note on any columns that serve as join keys. This is
   regenerated fresh from the schema whenever the Task is saved, so it
   never goes stale.
3. Upload this run's files.
4. **Automatic schema matching** — each uploaded file's column-name and
   dtype signature is compared against each recorded file role's expected
   signature:
   - Column names must match exactly.
   - Data types must match, with reasonable tolerance for compatible
     upgrades (e.g. an `int` column arriving as `float`).
   - Extra columns beyond what's expected are allowed and ignored; missing
     required columns are flagged.
   - If no confident match is found, the user manually assigns which
     uploaded file corresponds to which recorded role.
5. On a mismatch, the exact file/column/expected-vs-actual discrepancy is
   shown, with an option to manually remap the affected column rather than
   aborting the whole run.

### 8.2 Replay Execution
1. Replay the recorded cleaning action sequence for each file. If an
   individual step's target column is missing or renamed, that one step is
   skipped and flagged in the run log — the rest of the sequence continues.
2. Rebuild the relationship metadata and schema context in DuckDB from the
   recorded configuration.
3. Replay the recorded calculated-column statements, in order.
4. Replay each recorded chat item:
   - Attempt the originally stored SQL first.
   - If it errors, automatically fall back to re-asking the LLM with the
     original question against the current schema.
   - If that also fails, skip the item and flag it for the user's
     attention.
5. Assemble all results into the saved report skeleton, applying the
   knowledge base (Section 7.2) wherever commentary is generated.
6. Show a preview screen summarizing what succeeded, what needed the LLM
   fallback, and what failed, before any file is downloaded.
7. Download the finished report in Excel, Word, PDF, or HTML.

---

## 9. Out of Scope

The following are explicitly not part of this build:

- Scheduling or automatically triggered periodic runs.
- Shared or team-level ownership/permissions for Tasks.
- OAuth or external identity-provider login.

---

## 10. Build Order- Stages

1. Login system (SQLite schema, hashed passwords, roles, first-run
   seeding)
2. LLM provider configuration (saved profiles, encrypted key storage,
   Light Model, session selection, test-connection)
3. Data Cleaner utility
4. Data Engine core (DuckDB loading, relationship detection/confirmation
   with metadata table, column type detection including categorical,
   column descriptions/aliases, calculated/delete columns)
5. Agno + DuckDbTools agent integration (SQL generation, execution, output
   type logic)
6. Chat with Data (chat UI, pin-to-dashboard, Dashboard page, HTML + Excel
   export)
7. Task Builder (cleaning-step capture, structure-first report building,
   knowledge base, save/reorder controls, full export formats)
8. Run a Task (schema matching, sample-file generator, replay execution,
   preview screen)
