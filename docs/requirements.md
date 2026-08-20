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
- For each pinned item,The user can add Heading & add or update Comments(generated by AI)
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
-  Give 3 options for CSS style, also allow user to change the ccs( accept only after validation )
- **Excel** — one sheet per subsection; charts embedded as images, text
  summaries placed as cells alongside the relevant tables; full data with
  no row limits.
- No PDF or Word export is available for the Dashboard.

### 6.5 Criteria-Based Exceptional Reporting

A third view on the Chat with Data page — **Setup | Chat | Checks** —
sharing the same loaded tables, confirmed relationships and column
dictionary. Where chat answers one question at a time, this tests a set of
standing business rules and reports the exceptions.

- **Persona** — set once per criteria set: who the AI is acting as, plus
  any background it should apply. Passed as the `knowledge_base` argument
  of the commentary-generation call (Section 7.2's slot, used here first).
- **Criteria** — each is a rule written in plain language ("bonus must be
  at most 5% of basic if department = HR"), with optional table/column
  hints. The hints are a suggestion, not a constraint: the AI also
  receives the real schema and corrects a wrong guess rather than failing
  on it.
- **Generated SQL** — one narrow LLM call, distinct from the chat agent,
  guarded by the same read-only rules as Section 5.5. Its result must
  carry, per row: the columns identifying the record, `criteria_result`
  (the value the rule turns on) and `criteria_met` (the literal `Yes` or
  `No`). A result that breaks that contract is repaired automatically once
  before the reason is shown to the user.
- **Refine loop** — editing the rule and re-testing regenerates *from the
  previous statement*, so a query the user has already tuned is adjusted
  rather than rewritten.
- **Review** — the full result with an All / Failures / Passes filter, and
  an optional chart.
- **Save to report** — freezes the run, generates a point-wise remark on
  the failures, and pins the result to the Dashboard (Section 6.3)
  automatically. There is no separate report screen: the Dashboard already
  arranges, renames, re-comments, reorders, removes and exports pinned
  items. Re-saving after a refine updates that same item in place, wherever
  the user has filed it.
- **Follow-up actions** — for a saved criteria, the AI drafts an email,
  meeting agenda, task description or free-form note from the failing rows.
  Every one is a **draft**: it is reviewed and edited, and confirming it
  places it in the report. **Nothing is sent from this app.** A confirmed
  draft can be downloaded as `.eml` or `.ics` for the user's own mail or
  calendar client. Outbound sending (SMTP, calendar APIs) is out of scope.
- **Storage** — unlike the Dashboard, a criteria set persists: SQLite table
  `check_sets` (`set_id`, `user_id`, `name`, `checks_json`, timestamps),
  owned by its creator. What is stored is the recipe — persona, criteria
  text, hints, SQL — never the result rows, which describe data that is
  gone once the session ends. A reloaded set is re-tested against whatever
  is uploaded now.

### 6.6 Chat Types — Saved Setups

Sections 6.1–6.3 are deliberately session-only, and that is right for a
one-off exploration. It is wrong for the work people actually repeat: the
same files, with the same columns, arriving every month. Retyping the links
and the column descriptions each time is the same objection requirement 6.5
raises about retyping criteria, and it has the same answer — save the recipe.

A **chat type** (e.g. "Salary processing") is a saved Steps 1–3 setup. It is
a recipe, never data: the expected shape of the files, how they link, and
what the columns mean. No rows, no chat transcript.

- **Selecting one** — a picker above the Setup / Chat / Checks control offers
  the user's saved chat types plus "new chat type". Choosing "new" is the
  behaviour of every version before this one, so nothing about the ad-hoc
  path changes.
- **What is stored** — per expected table: its name and its columns with each
  column's semantic type; the confirmed relationships; and every column
  description and synonym list. Calculated-column statements are **not**
  part of a chat type; they belong to the Task recipe of Section 7.5.
- **Saving** — offered once tables are loaded, under a name unique per
  account. Saving under an existing name updates it, as with a criteria set.
- **Matching an upload** — with a chat type selected, the user uploads files
  and the load is checked against the saved shape before anything else
  happens. Matching is on the engine's table name, not the raw filename, so
  the same table is recognised whether it arrives as `Salary Master.xlsx`,
  `salary_master.csv`, or a handoff from the Data Cleaner.

  | Difference | Outcome |
  |---|---|
  | Expected table not uploaded | Blocking error, naming the table |
  | Expected column missing | Blocking error, naming column and table |
  | Column's type differs from the saved one | The **saved** type is applied (see below) |
  | Extra column | Dropped from the table, reported as a note |
  | Extra table | Not imported, reported as a note |
  | Column order differs | Ignored |

- **Types are fixed or refused, never tolerated.** A semantic type is not a
  label — it decides the real DuckDB column type, so a date column loaded as
  text turns `joining_date < '2024-04-01'` into a string comparison that
  returns wrong rows with no error at all. Silently wrong output is worse
  than a failed load, and worse still in Section 6.5, where it becomes a
  wrong Yes/No in a report. So on a mismatch the saved type is applied as a
  declared type, overruling detection exactly as a Data Cleaner recipe does.
  If the column converts, it loads correctly and the conversion is reported.
  If it does not, the load is refused with the values that would not convert
  named, so the user can fix the file or clean it first.
- **Pre-filled setup** — once the files match, Step 2's links and Step 3's
  descriptions are restored from the chat type and are fully editable. The
  user may update the chat type with their changes, or leave it untouched
  and simply carry on to Chat.
- **Criteria sets belong to a chat type** — a set saved from Section 6.5
  while a chat type is selected is stored against it, and Load set offers
  that chat type's sets. Sets saved without a chat type stay reachable.
  Deleting a chat type does not delete its criteria sets; they revert to
  unscoped, because a set is expensive to rebuild.
- **Storage** — SQLite table `chat_types` (`chat_type_id`, `user_id`,
  `name`, `config_json`, timestamps), owned by its creator and never visible
  to another account. `check_sets` gains a nullable `chat_type_id`.

---

## 7. Task Builder

Restricted to `admin` and `superuser` roles. This is where a full pipeline
— from raw file upload through a finished, deliberately structured report —
is assembled once and saved as a reusable **Task**.

### 7.1 Shape of the page

Its own page, laid out like Chat with Data but separate from it: one control
switches between four views — **Setup | Report-Items | Checks | Report** —
with a task bar above them holding the task name and Save / Update / Load.

Two things Chat with Data has are deliberately absent:

- **No chat type picker.** A Task *is* a saved setup; offering a second
  saved-setup concept on the same screen would be two answers to one
  question.
- **No criteria Save set / Load set** in the Checks view. The criteria are
  saved with the Task.

### 7.2 Persona and context

Set **once per Task**, at the foot of the Setup view, and used by both
Report-Items and Checks wherever commentary is generated. This is the
knowledge base / domain-rules slot the rest of this document refers to.

### 7.3 Flow

1. **Name the task** (e.g. "Salary Processing").
2. **Setup** — upload sample files (e.g. salary master, employee master,
   attendance data), confirm relationships, review and edit column
   descriptions, exactly as Section 5 describes. Everything configured here
   is recorded for later reuse. The persona and context box sits at the foot
   of this view.
3. **Report-Items** — cards, like Checks, not a conversation. **Add new**
   creates an item of one of two kinds:
   - A **report item**: point heading, the rule in plain language, tables
     involved (optional), columns involved (optional). The user generates
     and regenerates SQL, sees the result as a dataframe, can generate a
     chart for it and a comment, then **Pin to report**, update, or remove
     from the report.
   - A **column step**: add or update column values, in plain language. From
     that step onwards every later item sees the updated table. A column
     step is never pinned — its effect is the changed data.

   Order is the list's order, and **only the last column step may be
   deleted** — deleting one in the middle would leave every later item's SQL
   written against columns that no longer exist.
4. **Checks** — Criteria-Based Exceptional Reporting as described in
   Section 6.5, with two omissions: no criteria set save/load (above), and
   **no Actions view**. Drafted emails, meetings and tasks respond to *this
   month's* exceptions; a Task is a reusable recipe, and a saved draft about
   rows that no longer exist is worse than no draft. Chat with Data keeps
   its Actions view unchanged.
5. **Report** — the same arranging, preview and download screen as the
   Dashboard (Section 6.4), over the Task's own report rather than the
   session Dashboard. Anything pinned from Report-Items or Checks lands
   here.
6. **Save / Update as Task**, capturing the full recipe below so it can be
   reused and later edited.

### 7.4 Export Formats
- Uses the same Jinja2-based HTML rendering approach as Chat with Data's
  Dashboard (Section 6.4), plus two additional formats:
  - **HTML** — as described in Section 6.4.
  - **Excel** — one sheet per subsection, charts embedded as images, text
    summaries placed as cells; full data, no row limits.


### 7.5 What Gets Captured in a Task

The file schemas, the relationships, the column meanings, the ordered
calculated-column statements, the report items and their SQL, the criteria,
the report structure, and the persona and context.

**Data Cleaner steps are not captured.** The user cleans their files
themselves and uploads the result, so a Task has no cleaning sequence to
record or replay.

A saved report is a **skeleton, never a snapshot**: headings, comments,
section and subsection names, ordering and the id of the item each entry
came from — never a dataframe or a figure, which describe data that is gone
when the session ends.

### 7.6 Storage
- SQLite table: `task_id`, `user_id` (owner), `name`, `description`,
  `created_at`, `task_json`.
- `task_json` holds the full recorded recipe:
  ```
  {
    expected_schemas: { role: { columns: { name: {dtype, description, aliases} } } },
    relationships: [ ... ],
    calculated_columns: [ ordered SQL statement list ],
    report_items: [ ... ],
    checks: [ ... ]
    report_skeleton: { header, footer, title_page, sections: [...], Persona and context  }
  }
  ```
- Tasks are owned by the user who created them, the owner can update & save it.

---

## 8. Run a Task

Available to any logged-in user.

### 8.1 Flow
1. Select a saved Task from the list.
2. on click of button in dialog-Show schema to user -like files-columns names etc
3. user Upload all the files & once all files uploded- click to check/ match schema.
4. **Automatic schema matching** — each uploaded file's column-name and
   dtype signature is compared against each recorded file role's expected
   signature:
   - Column names must match exactly.
   - Data types must match, with reasonable tolerance for compatible
     upgrades (e.g. an `int` column arriving as `float`).
   - Extra columns beyond what's expected are allowed and ignored; missing
     required columns are flagged.
5. On a mismatch, the exact file/column/expected-vs-actual discrepancy is
   shown, with an option to manually remap rather than aborting the whole run.
   Two remaps, because two things can be named differently in this month's
   files:
   - **which file is which table** — a table's name comes from its filename,
     so `salary_august.xlsx` against a task recorded on `salary.xlsx` is the
     first mismatch most users meet, and telling them to rename their files
     would be a worse answer than remapping;
   - **which column is which column**, as above.

   A remap is applied **while the file is being read**, never to what is
   already loaded: whether a column can be read as a date is a question about
   the text in the file, and a column renamed after the load would keep the
   type detection gave it rather than the one the task declares.

### 8.2 Replay Execution
1. Rebuild the relationship metadata and schema context in DuckDB from the
   recorded configuration.
2. Replay the recorded calculated-column statements, in order.
3. Replay each recorded report item & Checks, in the order the report items
   were written — a column step changes what every item below it sees. Each
   one runs its **recorded SQL** first, with no LLM call at all; only if that
   statement fails is it regenerated, and the step is reported as having used
   the fallback. A column step is never regenerated: rewriting a column
   definition would silently change every figure below it.
4. Generate report Assemble all results into the saved report skeleton, applying the
   Task's persona and context (Section 7.2) wherever commentary is
   generated. Redrafting the commentary is the one LLM call a run makes by
   design, so it is offered as a choice — declining it keeps the wording
   saved with the Task.
5. Show a preview screen summarizing what succeeded, what needed the LLM
   fallback, and what failed, before any file is downloaded. An item whose
   result could not be produced keeps its place in the report and says so,
   rather than disappearing from a report that was designed to have it.
6. Download the finished report in Excel, HTML.
7. while the process take time- show user spinner or similar the processing stage to keep UX good.

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
7. Criteria-Based Exceptional Reporting (persona, criteria → SQL, refine
   loop, auto-pin to the Dashboard, saved criteria sets, drafted follow-up
   actions)
8. Chat Types (saved Steps 1–3 setups, schema matching on upload, criteria
   sets scoped to a chat type)
9. Task Builder (cleaning-step capture, structure-first report building,
   knowledge base, save/reorder controls, full export formats)
10. Run a Task (schema matching, sample-file generator, replay execution,
    preview screen)
