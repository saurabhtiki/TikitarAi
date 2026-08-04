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
