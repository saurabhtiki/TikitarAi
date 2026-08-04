# Stage 1: Login System

## Scope

SQLite user schema, hashed passwords, roles, first-run seeding of a default
superuser, and a login gate that blocks the entire app until authenticated.

**Out of scope for this stage** (deferred to later phases): LLM provider
configuration, the superuser User Management screen, self-service
password/photo editing, Data Cleaner, Data Engine, Chat with Data, Task
Builder, Run a Task.

## Schema

```sql
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    photo_path    TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('superuser', 'admin', 'normal_user')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## File/module map

```
streamlit_app.py          # entry point: page config, DB bootstrap, login gate via st.navigation
branding.py                # LOGO_PATH — absolute path to the logo, independent of process cwd
static/tikitar-logo.webp  # app logo — page icon + login/home branding
app_pages/login.py         # login form (only page that exists pre-auth)
app_pages/home.py          # minimal authenticated placeholder + logout
auth/passwords.py          # hash_password(), verify_password()
auth/db.py                 # DDL, init_db(), seed_default_admin(), get_user_by_email/id()
auth/service.py             # authenticate(), login(), logout(), is_authenticated()
auth/exceptions.py          # AuthDatabaseError
data/tikitarai.db           # runtime SQLite file (gitignored)
tests/                       # pytest suite
```

## Function contracts

- `auth.passwords.hash_password(plain_password: str) -> str`
- `auth.passwords.verify_password(plain_password: str, password_hash: str) -> bool`
- `auth.db.init_db(db_path=DEFAULT_DB_PATH) -> None` — idempotent DDL
- `auth.db.seed_default_admin(db_path=DEFAULT_DB_PATH) -> None` — idempotent, seeds `admin@admin.com` / `nimda` / `superuser` only when the table is empty
- `auth.db.get_user_by_email(email, db_path=DEFAULT_DB_PATH) -> dict | None`
- `auth.db.get_user_by_id(user_id, db_path=DEFAULT_DB_PATH) -> dict | None`
- `auth.service.authenticate(email, password) -> dict | None` — same `None` for unknown email and wrong password
- `auth.service.login(user: dict) -> None` — sets session state to exactly `{user_id, email, role}`
- `auth.service.logout() -> None` — `st.session_state.clear()`, wholesale
- `auth.service.is_authenticated() -> bool`

## UI flow

1. App boots, bootstraps the DB (creates table + seeds admin if empty).
2. Unauthenticated: `st.navigation` is given only the login page. Login form
   (email, password, submit) validates non-empty fields, calls
   `authenticate()`, shows a generic "Incorrect email or password." on
   failure (no user-enumeration leak), calls `login()` + `st.rerun()` on
   success.
3. Authenticated: `st.navigation` is given only the home page — a welcome
   placeholder with the user's name/role and a sidebar logout button that
   calls `logout()`.
4. The gate is structural: the set of `st.Page` objects is rebuilt from
   `is_authenticated()` every rerun, so nothing but the login page is ever
   reachable pre-auth.

## Testing checklist

- [ ] `test_passwords.py` — hashing salts correctly, verify true/false cases
- [ ] `test_db.py` — idempotent init, idempotent seeding (no duplicate on
      restart), unique-email constraint enforced, lookups round-trip
- [ ] `test_service.py` — authenticate correct/wrong/unknown cases, login
      sets exactly the 3 session keys, logout clears wholesale
- [ ] `test_login_page.py` — `AppTest` drives the real form end-to-end
- [ ] `uv run pytest` passes

## Phase completion

Update "Current phase" in `CLAUDE.md` to "Stage 1: Login system — complete"
once the checklist above is green, before starting Stage 2 planning.

---

# Stage 2: User Management Screen + App Theme

## Scope

Superuser-only add/edit/delete of user accounts and role changes (spec
2.4), plus a custom `.streamlit/config.toml` theme.

**Out of scope for this stage** (still deferred): LLM provider
configuration, self-service password/photo editing, Data Cleaner, Data
Engine, Chat with Data, Task Builder, Run a Task.

## New exceptions (`auth/exceptions.py`)

- `DuplicateEmailError(AuthDatabaseError)` — creating/renaming a user would
  collide with an existing email.
- `ProtectedAccountError(AuthDatabaseError)` — operation targets the seeded
  superuser account (deletion or role change away from `superuser`).

The seeded admin is identified by `SEED_ADMIN_USER_ID = 1` (SQLite
`AUTOINCREMENT` never reuses ids, and seeding only fires on an empty
table), not by email — email is editable via this screen, so an
email-based check would stop protecting the account the moment it's
renamed. The seeded admin's role is permanently protected in addition to
its deletion (slightly beyond the literal spec text) so a superuser can
never be reduced to zero — this makes a separate "last superuser" guard
unnecessary, since the invariant holds by construction.

## New `auth.db` function contracts

- `list_users(db_path=DEFAULT_DB_PATH) -> list[dict]`
- `create_user(email, name, password, role, db_path=DEFAULT_DB_PATH) -> dict` — raises `DuplicateEmailError`, `ValueError` (empty password)
- `update_user(user_id, name, email, role, db_path=DEFAULT_DB_PATH) -> dict` — raises `ProtectedAccountError`, `DuplicateEmailError`, `AuthDatabaseError` (missing user)
- `delete_user(user_id, db_path=DEFAULT_DB_PATH) -> None` — raises `ProtectedAccountError`, `AuthDatabaseError` (missing user)

## New `auth.service` contract

- `require_role(*allowed_roles: str) -> bool` — defense-in-depth page guard; the primary gate is `streamlit_app.py` never listing the page for disallowed roles.

## File/module map addition

```
app_pages/user_management.py   # superuser-only CRUD screen for user accounts
.streamlit/config.toml          # light slate/blue theme
```

## UI flow

1. `streamlit_app.py`'s `pages` list appends `user_management.py` only when
   `session_state.role == "superuser"` — first time the app has a 2-page
   nav (Streamlit's page-picker sidebar now appears for superusers).
2. Page guard: `require_role("superuser")`, else `st.error` + `st.stop()`.
3. "Add new" button opens an `st.dialog` form (email, name, role, initial
   password) → `create_user()`.
4. `st.dataframe(..., on_select="rerun", selection_mode="single-row")`
   lists all users; selecting a row reveals Edit/Delete buttons.
5. Edit → `st.dialog` form (name, email, role — no password field, per
   spec's self-service-only rule) → `update_user()`.
6. Delete → `st.dialog` confirmation (warning + Confirm/Cancel) →
   `delete_user()`.
7. `ProtectedAccountError`/`DuplicateEmailError` surface as specific,
   user-facing messages at each call site.

**Known testing limitation:** `st.dialog` content cannot be driven through
`streamlit.testing.v1.AppTest` in the installed Streamlit version. Dialog
business logic is fully covered at the `auth.db` layer (`test_db.py`);
page-level `AppTest` coverage is limited to the role gate, table
rendering, and row-selection revealing the Edit/Delete buttons (selection
injected via the keyed `st.session_state["um_users_table"]`). Full dialog
submission was confirmed via manual `streamlit run` verification instead.

## Testing checklist

- [ ] `test_db.py` additions — `list_users`, `create_user` (success/duplicate/empty-password), `update_user` (success/duplicate/seeded-admin-role-blocked/seeded-admin-name-email-allowed), `delete_user` (success/seeded-admin-blocked/nonexistent)
- [ ] `test_user_management_page.py` — non-superuser blocked, superuser sees the table, row selection reveals Edit/Delete
- [ ] `uv run pytest` passes
- [ ] Manual verification: add/edit/delete/role-change through the real dialogs, seeded-admin protection, non-superuser nav hidden, theme applied

## Phase completion

Update "Current phase" in `CLAUDE.md`/`AGENTS.md` to "Stage 2: User
Management screen — complete" once the checklist above is green.

---

# Stage 3: User Settings — Profile, Password & LLM Provider Configuration

## Scope

A Settings page, open to **every** logged-in role (spec 2.2's "Manage own
password/photo" and "Manage own saved LLM provider profiles" apply to
all three roles, unlike User Management). Covers self-service name/photo
edits, a self-service password change, and full CRUD over a user's own
LLM provider profiles (spec 3.1) plus a single designated Light Model
(spec 3.2). Also redoes the app-wide sidebar: `st.logo` instead of a
manual `st.image`, Log out always first, a round photo + name (no email,
no role caption).

**Deferred** (still out of scope): per-session active-model selection
(spec 3.3 — no consumer until Chat with Data exists), Test connection
(spec 3.4 — needs a real LLM client, better built alongside one), Data
Cleaner, Data Engine, Chat with Data, Task Builder, Run a Task.

## Schema addition

```sql
CREATE TABLE IF NOT EXISTS llm_profiles (
    profile_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    nickname          TEXT NOT NULL,
    provider_type     TEXT NOT NULL CHECK (provider_type IN ('local', 'cloud', 'custom')),
    base_url          TEXT,
    api_key_encrypted TEXT,
    default_model     TEXT NOT NULL,
    is_light_model    INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Every `llm/db.py` read/write filters by `user_id` — ownership is enforced
in the `WHERE` clause itself, not just trusted from the caller.

## New module: `llm/`

- `llm/exceptions.py` — `LLMDatabaseError(Exception)`.
- `llm/crypto.py` — Fernet key at `data/encryption.key` (auto-generated
  on first use, under the already-gitignored `data/` directory):
  `encrypt_api_key(plain_key, key_path=...) -> str`,
  `decrypt_api_key(encrypted_key, key_path=...) -> str`.
- `llm/db.py`: `init_llm_table()`, `list_profiles(user_id)`,
  `create_profile(user_id, nickname, provider_type, base_url, api_key, default_model)`,
  `update_profile(profile_id, user_id, ..., api_key)` — `api_key=None`
  leaves the stored key unchanged, `""` clears it, any other value
  re-encrypts — `delete_profile(profile_id, user_id)`,
  `set_light_model(profile_id, user_id)` / `unset_light_model(profile_id, user_id)`
  — setting one clears the flag from every other profile of that user in
  the same transaction, so at most one light model ever exists per user.

## `auth/` additions

- `auth/db.py`: `update_own_profile(user_id, name, photo_path=None)` —
  `photo_path=None` always means "leave the current photo alone" (no
  remove-photo feature exists); `update_own_password(user_id, current_password, new_password)`
  — verifies the current password first.
- `auth/exceptions.py`: `InvalidPasswordError(AuthDatabaseError)` (wrong
  current password), `PhotoProcessingError(Exception)` (unreadable
  upload — not a DB error, kept separate).
- `auth/photos.py` (new): `save_user_photo(user_id, uploaded_file) -> str`
  — center-crops to square, resizes to 256×256, saves as
  `data/photos/user_{user_id}.png` (fixed filename, so re-upload just
  overwrites).

## Shared sidebar: `sidebar.py`

`render_sidebar(profile)` — `st.logo(...)`, then inside `st.sidebar`:
Log out button first, then a round avatar (base64-inlined photo, or the
user's initial if none) + name. Used by `home.py`,
`app_pages/user_management.py`, and `app_pages/settings.py`, replacing
each page's previously-duplicated inline sidebar block.

## `app_pages/settings.py`

No role guard beyond authentication — every role reaches this page.
`st.segmented_control` switches between three sections:

1. **Profile** — name + photo upload, saved via `save_user_photo()` +
   `update_own_profile()`.
2. **Password** — current/new/confirm, saved via `update_own_password()`.
3. **LLM providers** — `list_profiles()` → dataframe (row-select →
   Edit/Delete/"Set as light model" or "Unset light model", same
   selection-reset pattern as `user_management.py`'s delete-crash fix,
   applied from the start). "Add new" opens an `st.dialog` with
   `st.segmented_control` for `provider_type` (local/cloud/custom) and
   conditionally-shown base_url/api_key/default_model fields. Editing a
   profile never pre-fills the real decrypted API key — a blank field
   with "leave blank to keep the current key" plus a "remove saved key"
   checkbox instead. Edit-dialog widget keys are suffixed with
   `profile_id` so each profile's dialog always shows its own values
   (a shared key would keep showing whichever profile was edited first).

## Wiring

`streamlit_app.py`: `bootstrap_database()` also calls
`llm.db.init_llm_table()`. Authenticated `pages` list always includes
Settings (all roles), User management staying superuser-only.

## Testing checklist

- [ ] `tests/test_llm_crypto.py` — key persists across loads, round-trip
      encrypt/decrypt, wrong key fails to decrypt
- [ ] `tests/test_llm_db.py` — create/list/update/delete, ownership
      isolation, api_key update semantics (None/""/value), light-model
      flag exclusivity
- [ ] `tests/test_db.py` additions — `update_own_profile`,
      `update_own_password` (success/wrong-current/empty-new)
- [ ] `tests/test_settings_page.py` — any role can access, section
      switching via segmented_control, LLM table scoped to the current user
- [ ] `uv run pytest` passes
- [ ] Manual verification: sidebar redesign, photo upload round-trips
      into the sidebar avatar, password change round-trips through a
      real re-login, LLM profile add/edit/delete/light-model-toggle
      through the real dialogs

**Known testing gap** (same category as Stage 2's `st.dialog` gap): full
Add/Edit LLM-profile dialog submission and `st.file_uploader` photo
upload aren't reliably drivable through `AppTest`. Business logic is
fully covered at the `llm/db.py`/`auth/db.py` layers instead; full
click-through is confirmed via manual `streamlit run` verification.

## Phase completion

Update "Current phase" in `CLAUDE.md`/`AGENTS.md` to "Stage 3: User
Settings — complete" once the checklist above is green.

---

# Stage 4: Data Cleaner Utility

## Scope

The first entry in the **Utilities** category (spec 4.1) — a standalone
Upload → Clean → Download tool, open to every logged-in role, independent
of the Chat/Task/Run pipeline. Multi-file CSV/Excel upload with a sheet
picker, the full set of cleaning operations, a human-readable cleaning log
with reset-to-raw, editable/sanitized output sheet names, and a single
multi-sheet `.xlsx` download.

Beyond the literal spec text: per-step undo (not just a wholesale reset),
an "apply to all tables" option on the one-click text actions, an impact
sentence before each commit, a `_Cleaning log` sheet in the downloaded
workbook, and header promotion on `skip_rows` (see below).

**Out of scope** (still deferred): Data Engine/DuckDB, Chat with Data,
Task Builder, Run a Task, plus per-session LLM selection (spec 3.3) and
Test connection (spec 3.4), carried over from Stage 3.

**No schema addition.** This utility is session-only by design — spec 4.1
describes Upload → Clean → Download with no persistence, and `logout()`'s
wholesale `st.session_state.clear()` already tears it down. Stage 7 is what
persists recipes, and it persists them into the `tasks` table.

## Architecture: the step recipe

Cleaning never mutates a DataFrame in place. Each table holds an immutable
raw frame plus an ordered list of step dicts; the cleaned frame is always
derived as `apply_steps(raw_df, steps)`.

The driver is spec 7.1: Task Builder must record every cleaning action
"with its exact structured parameters, in the order applied", and spec
8.2 must tolerate a step whose target column has gone missing. Building
the recipe engine now means Stages 7 and 8 reuse it rather than rewriting
it — and reset-to-raw, per-step undo, and the log all fall out for free.

A step is exactly two keys and only JSON primitives — no id, no timestamp,
no stored display string (the log line is *derived* via `describe_step`,
so improving the wording later improves every saved Task retroactively):

```python
{"action": "skip_rows", "params": {"top": 2, "bottom": 0, "promote_header": True}}
```

`json.loads(json.dumps(steps)) == steps` is asserted in the test suite —
that is the Stage 7 serialization guarantee, locked in now.

One frozen `StepSpec` dataclass per action keeps the executor, log
renderer, validator, and ordering rule side by side, so adding a 13th
action is one dict entry plus its functions:

```python
@dataclass(frozen=True)
class StepSpec:
    action: str
    label: str
    apply: Callable[[pd.DataFrame, dict], tuple[pd.DataFrame, list[str]]]
    describe: Callable[[dict], str]
    validate: Callable[[dict, list[str]], None]      # params, current columns
    required_columns: Callable[[dict], list[str]]    # drives skip-and-flag
    record: RecordPolicy = RecordPolicy.ADD
    pin_rank: int | None = None
```

`STEP_REGISTRY` is an explicit literal dict at the bottom of `steps.py`,
not decorator-registered — a `@register` decorator would let an
import-order slip produce a silently empty registry, and Stage 8's replay
would then skip every step with no error at all.

## Step ordering

`apply_steps` executes the list strictly in order and never sorts, so the
stored order *is* the execution order and the log can never disagree with
the replay. All reordering happens at insert time, via two `StepSpec`
knobs.

`RecordPolicy` governs only how an applied action is written into that
table's own cleaning log. It never combines data — no rows, columns,
files, or tables are ever joined or stacked by this utility. (Combining
files is a separate utility for a later stage.)

- `ADD` — a new log entry at the end.
- `REPLACE` — at most one entry for this action; re-applying overwrites it
  **at its existing index**. Position matters: if an edited `skip_rows`
  jumped to the end it would re-run row-skipping *after* type coercion and
  silently change the results.
- `UPDATE_PER_COLUMN` — one entry holding a per-column dict; re-applying
  updates only the columns named this time. Setting a column back to
  "leave as-is" removes its key; an emptied dict removes the step.

`pin_rank` pins a step to a sorted prefix of the list; unpinned steps
follow in click order.

| action | record | pin |
|---|---|:--:|
| `skip_rows` | REPLACE | 0 |
| `set_column_types` | UPDATE_PER_COLUMN | 1 |
| `trim_whitespace`, `remove_special_characters` | REPLACE | — |
| `change_case`, `fill_missing` | UPDATE_PER_COLUMN | — |
| `remove_empty_rows`, `delete_columns`, `rename_columns`, `fix_numeric_text`, `find_replace`, `drop_duplicates` | ADD | — |

`skip_rows` is pinned first because it decides which rows the table even
has, including which row is the header; `set_column_types` second because
types must settle before `fill_missing`'s mean/median can work.

**Header promotion.** "Skip 3 junk rows from the top" almost never means
"drop 3 data rows" — it means the real header is on row 4. Without
`promote_header`, the columns stay junk strings and every column-targeted
step afterwards is broken. Hence it lives in `skip_rows`' params.

**Seeding.** On registration a table's recipe is seeded with one
`set_column_types` step carrying `detect_column_types(raw_df)`, so typing
is an explicit replayable action rather than an invisible pandas guess
that could differ between the sample file and the Stage 8 run file.
"Reset to raw" clears it too — raw means raw.

## Load as text, type as a step

`read_table` reads every cell as a string. This follows from the spec, not
taste: spec 4.1 lists `id` as a target type, and if pandas has already
parsed `00123` as `123`, no later step can restore the leading zeros —
the most common real-world bug with employee and account numbers. It is
also what makes "fix numbers stored as text" meaningful and "reset to raw"
honest.

## Failure semantics — three classes, three answers

- **Value-level** (a cell won't parse as numeric/date) → coerce to NA, keep
  the step, report an exact count plus sample offending values. Never
  abort, never drop rows. Real financial exports always contain a few
  `N/A` cells; NA is the honest representation and `fill_missing` is the
  user's next move, so the tools compose.
- **Structural** (target column no longer exists) → skip that step, flag
  it, continue the rest. This is spec 8.2's contract, built once here.
- **Contract** (unknown action, malformed params, a rename producing
  duplicate names, an uncompilable regex) → raise `InvalidStepError` in
  `validate_step` *before* the step enters the recipe. This gives the
  invariant "a stored recipe is always well-formed" that Stage 7 relies
  on, and turns spec 4.1's "duplicate resulting names are blocked" into a
  validator rather than a UI-layer check.

## New exceptions (`cleaner/exceptions.py`)

- `DataCleanerError(Exception)` — base.
- `FileParseError(DataCleanerError)` — upload can't be read or decoded.
- `UnsupportedFileTypeError(FileParseError)` — `.xls`, `.parquet`, etc.
- `SheetNotFoundError(FileParseError)` — requested sheet absent.
- `InvalidStepError(DataCleanerError)` — unknown action, bad params,
  duplicate rename target, uncompilable regex.
- `ExportError(DataCleanerError)` — empty workbook, or an Excel row/column
  limit exceeded.

There is deliberately no sheet-*naming* exception: sanitization always
produces some legal name, so it would be dead code. The read-side failure
(`SheetNotFoundError`) is the one that actually needs a type.

## File/module map addition

```
cleaner/exceptions.py      # DataCleanerError hierarchy
cleaner/loaders.py         # bytes -> all-text DataFrames; sheet listing; encoding/delimiter sniffing
cleaner/steps.py           # StepSpec + 12 executor/renderer/validator triples + STEP_REGISTRY literal
cleaner/pipeline.py        # add/remove/apply/describe; step ordering + record rules; StepOutcome report
cleaner/profiling.py       # type detection incl. categorical + id; column stats; the value parsers
cleaner/naming.py          # Excel sheet-name sanitize/dedupe; UI tab-label dedupe
cleaner/export.py          # multi-sheet .xlsx bytes + _Cleaning log sheet
cleaner/session.py         # ONLY streamlit importer: TableState, dc_* keys, cached derivations
app_pages/data_cleaner.py  # the UI
```

`steps.py` and `pipeline.py` are split because they answer different
questions — "what does `trim_whitespace` do to a frame?" versus "what are
the ordering, undo, and reporting rules of a recipe?". `pipeline.py` is
what Stages 7/8 import. The dependency runs one way:
`pipeline -> steps -> pandas`.

`session.py` is the only module in the package that imports Streamlit,
mirroring the existing convention — `auth/` has exactly one such module
(`service.py`) and `llm/` has none. Everything else is plain
pytest-testable with no `AppTest`.

New dependencies: `pandas`, `openpyxl` (reads `.xlsx`), `xlsxwriter`
(writes the multi-sheet output). `pandas` was already imported directly by
`app_pages/settings.py` and `app_pages/user_management.py` while only a
transitive Streamlit dependency; this stage declares it explicitly, since
the pandas 3.x behaviors noted below are load-bearing. `xlrd` is
deliberately not added — legacy `.xls` is rejected with a clear
`UnsupportedFileTypeError` rather than a cryptic pandas error.

## Function contracts

`cleaner/loaders.py`
- `decode_text(file_bytes) -> str` — tries `utf-8-sig`, `cp1252`, `latin-1`, so Windows-Excel CSVs don't arrive as mojibake. Raises `FileParseError` on empty input.
- `sniff_delimiter(text) -> str` — `csv.Sniffer` over the first 64 KB; `","` when ambiguous. Never raises.
- `list_sheet_names(file_bytes, file_name) -> list[str]` — workbook order; `[]` for CSV. Raises `UnsupportedFileTypeError`, `FileParseError`.
- `read_table(file_bytes, file_name, sheet_name=None) -> pd.DataFrame` — one table, every cell as text. Raises `UnsupportedFileTypeError`, `SheetNotFoundError`, `FileParseError`.

`cleaner/steps.py` — `RecordPolicy`, `StepSpec`, `STEP_REGISTRY`, and 12
private triples. Every `_apply_*` shares the signature
`(df, params) -> tuple[pd.DataFrame, list[str]]` and returns a **new** frame.

| action | params |
|---|---|
| `skip_rows` | `{top, bottom, promote_header}` |
| `set_column_types` | `{by_column: {col: {target_type, date_format, decimal_separator}}}` |
| `remove_empty_rows` | `{columns \| None, blank_strings_count_as_empty}` |
| `delete_columns` | `{columns}` |
| `rename_columns` | `{renames: {old: new}}` |
| `fix_numeric_text` | `{columns, decimal_separator, parentheses_are_negative}` |
| `trim_whitespace` | `{collapse_internal}` |
| `remove_special_characters` | `{keep_pattern, replacement}` |
| `change_case` | `{by_column: {col: upper\|lower\|title}}` |
| `find_replace` | `{columns, find, replace, regex, case_sensitive}` |
| `fill_missing` | `{by_column: {col: zero\|mean\|median\|unknown\|drop_rows}}` |
| `drop_duplicates` | `{columns \| None, keep}` |

The value parsers `parse_numeric_series` and `parse_datetime_series` live in
`profiling.py` rather than here, so that "what counts as a number" is
defined once and shared by both type *detection* and the steps that
*apply* a type. `steps.py` imports them; the dependency stays acyclic
(`pipeline -> steps -> profiling -> pandas`).

- `profiling.parse_numeric_series(series, decimal_separator=".", parentheses_are_negative=True) -> tuple[pd.Series, pd.Index]` — strips currency symbols, thousands separators, non-breaking and zero-width characters; converts accounting parentheses and trailing minus to a leading minus; then coerces. The returned index is exactly the values that were non-null before and NA after — the raw material for the coercion warning. Shared by `set_column_types` and `fix_numeric_text`.
- `profiling.parse_datetime_series(series, date_format=None) -> tuple[pd.Series, pd.Index]` — the same contract for dates.

**All regex work compiles the pattern before handing it to pandas.** pandas 3
routes `str.replace(regex=True)` through Arrow's RE2 engine for plain string
patterns — RE2 rejects `\u` escapes and has no lookarounds — but falls back
to Python's `re` whenever it receives a compiled pattern. Compiling makes the
semantics that `validate` checked the semantics that actually run.

`cleaner/pipeline.py` — `CLEANING_RECIPE_VERSION = 1`, a `Step` TypedDict,
and `StepOutcome` (`index`, `action`, `status: applied|skipped|warned`,
`message`, rows/columns before and after).
- `make_step(action, params) -> Step` — raises `InvalidStepError` for an unknown action.
- `validate_step(step, current_columns) -> None` — raises `InvalidStepError`.
- `add_step(steps, step) -> list[Step]` — new list, applying the record/pin rules above.
- `remove_step(steps, index) -> list[Step]`
- `apply_steps_with_report(df, steps) -> tuple[pd.DataFrame, list[StepOutcome]]` — strict list order; a step whose required columns are absent is skipped and flagged while the rest continue. Raises only `InvalidStepError`, never for data problems.
- `apply_steps(df, steps) -> pd.DataFrame` — wrapper discarding the report.
- `describe_step(step) -> str` / `describe_steps(steps) -> list[str]`

`cleaner/profiling.py`
- `numeric_parse_rate(series, decimal_separator=".") -> float` and `date_parse_rate(series, date_format=None) -> float` — `0.0` for an all-null series.
- `detect_column_type(series, *, numeric_threshold=0.95, date_threshold=0.90, categorical_max_unique=50, categorical_max_ratio=0.5, categorical_min_rows=20) -> DetectedType` — in order: near-unique and either non-numeric-alphanumeric or numeric-with-leading-zeros → `id`; at/above the numeric threshold → `numeric`; at/above the date threshold → `date`; low cardinality per spec 4.1 → `categorical`; else `text`. All-null → `text`. Never raises.
- `detect_column_types(df, **kwargs) -> dict[str, DetectedType]`
- `column_stats(df) -> pd.DataFrame` — column, detected type, non-null, missing, missing %, unique, sample values.

`cleaner/naming.py`
- `sanitize_sheet_name(name, *, fallback="Sheet") -> str` — replaces Excel's forbidden `[ ] : * ? / \`, strips surrounding whitespace and apostrophes, truncates to 31, falls back when empty or equal to Excel's reserved `History`. Never raises.
- `deduplicate_sheet_names(names) -> list[str]` — case-insensitive (Excel's own comparison), appending `_2`, `_3`… and **re-truncating the base so the suffixed name still fits within 31 characters**. First occurrence keeps its name; order preserved.
- `sanitize_sheet_names(names) -> list[str]` — the single entry point used by the page and by `export.py`.
- `deduplicate_labels(labels) -> list[str]` — a *different* function with different rules and no length limit, for `st.tabs` labels.

`cleaner/export.py`
- `build_workbook(tables, log) -> bytes` — one sheet per `(sheet_name, DataFrame)` pair in order, plus the `_Cleaning log` sheet. Names must already be sanitized and deduplicated. NaN written as empty cells, header row frozen and auto-filtered, column widths fitted. Raises `ExportError` if empty or if Excel's limits (1,048,576 rows / 16,384 columns / 32,767 characters per cell) are exceeded — checked here rather than letting xlsxwriter fail opaquely mid-write.

## Session state (`dc_*`) and caching

```python
@dataclass
class TableState:
    table_id: str            # f"{file_id}::{sheet_name or ''}"
    file_id: str
    file_name: str
    sheet_name: str | None
    source_label: str        # immutable — the tab label
    output_sheet_name: str   # editable, sanitized on save
    steps: list[Step]
    recipe_version: int = CLEANING_RECIPE_VERSION
```

`dc_tables: dict[str, TableState]` lives under one flat session key,
consistent with the convention (`settings_llm_table`'s value is a dict
too). Widget keys stay flat beside it: `dc_uploader`, `dc_tabs`,
`dc_sheets_{file_id}`, `dc_output_sheet_name_{table_id}`, and so on.

`table_id` derives from `UploadedFile.file_id`, a per-upload UUID that
persists across reruns. Upload *index* is disqualifying — adding a fourth
file could renumber the others and silently reattach recipe #2 to a
different table. Filename+sheet collides when two files are both named
`data.csv`. The consequence, stated plainly: removing and re-adding the
same file yields a new `table_id` and an empty recipe. That is intended —
re-uploading is the user's signal that the data changed.

**No DataFrames live in session state.** Raw and cleaned frames exist only
in `st.cache_data`, always re-derivable from bytes plus steps, so a cache
eviction costs a recompute rather than data loss.

```python
@st.cache_data(show_spinner=False, max_entries=32, scope="session")
def _cached_raw_table(file_id, file_name, sheet_name, _file_bytes): ...

@st.cache_data(show_spinner=False, max_entries=64, scope="session")
def _cached_cleaned_table(table_id, steps, _raw): ...
```

The leading underscore excludes a parameter from the cache key, so the
upload payload is never hashed — `file_id` already determines it uniquely
within the session. `scope="session"` keeps one user's data out of
another's cache slot. The workbook build is cached the same way, since
`st.download_button` must materialize its bytes *before* the click.

The bigger lever than caching is not executing the other tabs at all:
`st.tabs(labels, key="dc_tabs", on_change="rerun")` makes `tab.open` a
real boolean, so only the visible table's pipeline runs. Each tab body is
an `@st.fragment` and multi-widget panels sit inside `st.form`, so a
keystroke costs one panel rather than N pipelines. The preview is capped
at 500 rows, with true counts shown via `st.metric`.

## UI flow

No role guard beyond authentication — spec 2.2 grants Utilities to every
role, so this follows `settings.py`, not `user_management.py`. Standard
page preamble: `get_user_by_id(...)` → `render_sidebar(profile)` →
`st.title`.

1. **Upload** — `st.file_uploader(accept_multiple_files=True,
   type=["csv", "xlsx"], max_upload_size=50)`. Per Excel file, a
   `st.multiselect` of its sheets defaulting to all, so one workbook can
   contribute several tables. `sync_tables(...)` reconciles **in both
   directions** every run: it registers new file/sheet combinations *and
   drops tables whose file or sheet is no longer selected*. Building this
   one-directional is an easy miss that would silently export tables the
   user thought they had removed.
2. **Clean** — `st.tabs`, one per table, labelled with the immutable
   `source_label` and deduped via `naming.deduplicate_labels`. Never the
   editable output sheet name: that is widget-backed, so every keystroke
   would change the tab set's identity and reset the selection mid-edit.

   Inside each tab, a metrics strip (`st.metric` for rows raw→now, columns
   raw→now, missing %, duplicate count — the always-visible proof each
   action did something), then `st.columns([2, 3])`:

   *Left, action groups as `st.expander`s, ordered the way people clean:*
   1. **Structure** — skip N rows top/bottom with header promotion, remove
      empty rows (all columns or a subset), delete columns, rename columns.
   2. **Column types** — a `st.multiselect` of columns plus one
      target-type `st.selectbox` and Apply, inside a form. Deliberately
      **not `st.data_editor`**: it has no `AppTest` accessor, and
      synthesizing steps from a returned-frame diff fights the recipe
      model. The auto-detected types stay reviewable at a glance as a
      read-only `column_stats` table on the right, so the `categorical`
      suggestions remain visible without an editable grid.
   3. **Text cleanup** — trim whitespace, remove/replace special
      characters, letter case per column, and find & replace with
      regex/case toggles scoped to chosen columns. The one-click actions
      carry an "Apply to all tables" checkbox.
   4. **Missing values** — per-column strategy, plus the
      numbers-stored-as-text fix.
   5. **Duplicates** — choose the subset defining a duplicate, see the
      count, remove them.

   Each Apply shows its impact sentence before commit.

   *Right, live feedback:* the cleaned preview (`st.dataframe`, 500 rows,
   `width="stretch"`) and the read-only `column_stats` table; the cleaning
   log beneath it as numbered `describe_step` lines, each with a remove
   button (`key=f"dc_remove_step_{table_id}_{index}"` — index-based keys
   are safe because buttons are stateless and the whole log re-renders
   after any mutation), plus "Reset to raw" behind a confirm dialog;
   `StepOutcome` warnings (coercion counts, skipped steps); and the output
   sheet name `st.text_input`, sanitized on save.
3. **Download** — one `st.download_button` producing the multi-sheet
   `.xlsx` including `_Cleaning log`, plus "Start over".

"Start over" uses the deferred-reset pattern rather than clearing state
directly, for two reasons found in testing. Clearing only `dc_tables` is
undone within the same run, because `sync_tables` immediately re-registers
every table from the still-populated uploader — so the uploader key has to
be cleared too. And Streamlit forbids writing a widget's own session_state
key after that widget exists this run, which the uploader does by the time
the button is reached. So the button queues a flag and
`session.consume_start_over()` applies it at the top of the next run,
before the uploader is instantiated. This is the same shape as the
table-selection reset in `app_pages/settings.py`.

Widget keys are `dc_`-prefixed and suffixed with `table_id` where
per-table, for the same reason `settings.py` suffixes with `profile_id`:
Streamlit applies `value=`/`default=` only on first key creation, so a
shared key would show the first table's values in every tab.

## Wiring

`bootstrap_database()` is unchanged — no new tables. Since this is the
first of a promised library of utilities (spec 4), `streamlit_app.py`'s
authenticated `pages` becomes `st.navigation`'s section mapping rather
than a flat list:

```python
pages = {
    "": [st.Page("app_pages/home.py", title="Home", icon=":material/home:", default=True)],
    "Utilities": [st.Page("app_pages/data_cleaner.py", title="Data cleaner",
                          icon=":material/cleaning_services:")],
    "Account": [st.Page("app_pages/settings.py", title="Settings", icon=":material/settings:")],
}
if st.session_state.get("role") == "superuser":
    pages["Admin"] = [st.Page("app_pages/user_management.py", ...)]
```

## Environment facts this stage depends on

Verified against the installed venv (pandas 3.0.5, Streamlit 1.60.0,
Python 3.12.12). Three of these overturn assumptions carried from Stages
2–3:

- **`st.file_uploader` IS drivable in `AppTest`** — `set_value()` /
  `upload()` exist and uploaded files persist across `at.run()`. Stage 4
  gets far broader page-test coverage than Stages 2–3 assumed.
- **`st.dialog` bodies ARE reachable in `AppTest`.** What actually broke in
  Stages 2–3 is the idiom `if st.button(...): _open_dialog()` — on the next
  rerun the button is `False`, so the dialog closes and its widgets vanish.
  Driving it from a session-state open-flag works end to end, and that is
  the idiom used here. Retrofitting `settings.py` / `user_management.py` to
  close their documented coverage gaps is a worthwhile follow-up, but is
  not part of this stage.
- **`st.cache_data` hashes `list[dict]` fine** (its hasher is not Python's
  `hash()`), and order-sensitively — so the recipe is directly usable as a
  cache key, with no JSON-string keys or tuple-freezing.
- **`st.data_editor` is NOT drivable in `AppTest`** — it has no accessor,
  while `st.dataframe` does. This is load-bearing: no editable grids in
  this stage.
- **`AppTest`'s `download_button.value` is a bool, not the file's bytes** —
  it reports whether the button was clicked. The workbook's contents are
  therefore asserted at the `cleaner.export` layer, and the page test only
  confirms a download is offered under the expected sheet names.
- **`scope="session"` caches can only be read from the app's own execution
  thread.** A test that calls `session.cleaned_table(...)` directly raises
  `StreamlitAPIException`, so page tests derive expected frames through
  `pipeline.apply_steps` instead — same recipe, no cache.
- **`AppTest`'s `session_state` proxy resolves attribute access as a key
  lookup**, so `.get(...)` and `.update(...)` raise. Use bracket access.
- **pandas is 3.0.5, not 2.x.** The default string dtype is `StringDtype`,
  so `is_object_dtype(text_column)` returns **False** — type detection
  written from 2.x memory would classify every text column wrongly and
  fail silently. Use `pandas.api.types.is_string_dtype`. `errors="ignore"`
  has been **removed** from `to_numeric`/`to_datetime`; only `"raise"` and
  `"coerce"` remain. Copy-on-write is mandatory, so every step returns a
  new frame and never uses `inplace=`.

## Testing checklist

Business logic lives in pure modules, which is what keeps it testable
regardless of `AppTest`'s limits — but unlike Stages 2–3, the page test
here can drive the real uploader and the real dialogs.

- [x] `test_cleaner_naming.py` — each forbidden character; truncation at
      exactly 31; empty → `Sheet`; reserved `History`; case-insensitive
      collision; the truncation-plus-suffix boundary (three 31-character
      names must yield three unique names still within 31); order preserved
- [x] `test_cleaner_loaders.py` — CSV round-trip preserves `007` as text;
      semicolon delimiter sniffed; `cp1252` bytes decode; multi-sheet
      `.xlsx` built in memory lists sheets in order; `.xls` →
      `UnsupportedFileTypeError`; missing sheet → `SheetNotFoundError`;
      corrupt bytes → `FileParseError`
- [x] `test_cleaner_steps.py` — one test per action asserting both the
      frame and the warnings, including `skip_rows` + `promote_header`
      giving the right column names; `parse_numeric_series` on
      `["$1,200.50", "(300)", "1 200", "abc"]` → `[1200.5, -300.0, 1200.0,
      NaN]` with one reported failure; `trim_whitespace` stripping
      non-breaking (U+00A0) and zero-width (U+200B) characters, not just
      ASCII; `find_replace` with `case_sensitive=False, regex=True`;
      `fill_missing` mean/median on a text column warning rather than raising
- [x] `test_cleaner_pipeline.py` — ADD grows the list; REPLACE keeps length
      1 **and preserves the original index**; UPDATE_PER_COLUMN updates only
      the named columns and drops the step when emptied; `skip_rows` and
      `set_column_types` land at indices 0 and 1 regardless of click order;
      two `find_replace` steps in opposite orders give different frames; a
      step targeting a deleted column is `skipped` **and later steps still
      apply**; `validate_step` raises for unknown action, duplicate rename
      target, uncompilable regex, negative `top`; and
      `json.loads(json.dumps(steps)) == steps`
- [x] `test_cleaner_profiling.py` — a 5-value/500-row column →
      `categorical`; near-unique zero-padded → `id`; 3% junk → `numeric`
      and 20% junk → `text`; all-null → `text`
- [x] `test_cleaner_export.py` — two tables round-trip back through
      `loaders` with matching content and order; NaN → empty cell;
      `_Cleaning log` contents; empty input → `ExportError`
- [x] `test_data_cleaner_page.py` — any role reaches the page; no exception
      with zero uploads; `at.file_uploader(key="dc_uploader").set_value(...)`
      produces a tab, preview, and correct metrics; two identically-named
      files get deduped tab labels; multi-sheet upload shows the sheet
      picker; setting `dc_tabs` switches tabs; clicking the trim button
      twice leaves **one** log entry (the REPLACE rule through the real
      UI); the reset-to-raw dialog empties `steps`; a download is offered
      with the expected sheet names; plus a regression test mirroring
      `test_set_light_model_button_does_not_crash` for the deferred-reset
      trap, which applies here too
- [x] `uv run pytest` passes
- [x] Manual verification: multi-sheet upload and sheet picker, every
      action through the real UI, per-step undo, reset-to-raw,
      apply-to-all-tables, removing a file dropping its table from the
      download, and the downloaded `.xlsx` opening with correct sheets and log

## Risks carried into implementation

- **Comma-as-decimal** — `1.200` is twelve hundred in de-DE and 1.2 in
  en-US. Never infer it; `decimal_separator` is an explicit parameter
  defaulting to `.`. Guessing silently produces wrong money.
- **Changing `skip_rows.top` later** renames every column and orphans
  previously-recorded steps. The report flags them as skipped; warn in the
  UI before the change.
- **Duplicate source column names** — pandas mangles a second `Amount` to
  `Amount.1`. Detect at load and surface it. Do *not* switch to positional
  column references to "fix" this; positions are far more fragile across
  Stage 8's replay against a different file.
- **User-supplied regex can hang the worker thread.** `re.compile()` at
  validation time catches the syntax cases; cap pattern length. Note also
  that `str.replace(regex=True)` silently no-ops on numeric columns, so
  the action is scoped to text columns and says so.
- **`logout()` clears session state but not `st.cache_data`.** Cleaned data
  stays resident for that browser session until the websocket closes.
  `scope="session"` plus `max_entries` bound the exposure.
- **Collapsed `st.expander` bodies still execute** unless gated the same
  way as tabs — relevant to the column-stats panel.

## Phase completion

Update "Current phase" in `CLAUDE.md`/`AGENTS.md` to "Stage 4: Data
Cleaner utility — complete" once the checklist above is green.
