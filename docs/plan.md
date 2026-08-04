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
