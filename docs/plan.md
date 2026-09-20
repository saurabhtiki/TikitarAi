# Phase 31 — separate data folder for development/testing vs production

**Status: built.**

Right now every module hardcodes `Path("data")` for its database and file storage (11
files: `auth/db.py`, `auth/photos.py`, `transform/db.py`, `cleaner/db.py`, `checks/db.py`,
`chat_types/db.py`, `llm/db.py`, `llm/crypto.py`, `dashboard/theme_db.py`,
`analyst/session.py`, `meetings/db.py`, `meetings/storage.py`, `reports/db.py`,
`reports/store.py`, `tasks/db.py`). That means local testing writes into the same `data/`
folder used in production — one mistake while testing can corrupt real data.

## What changes

1. **One new function, one new setting.** Add `utils/env.py` with a single function
   `get_data_dir() -> Path`:
   - Reads a setting called `APP_ENV` (checks `st.secrets` first, falls back to the
     `APP_ENV` environment variable).
   - `APP_ENV = "production"` → returns `Path("data")` (the real folder, used today).
   - Anything else, or not set at all → returns `Path("data_testing")` (safe default, so a
     laptop that forgets to set anything can never touch real data by accident).
2. **Every hardcoded `Path("data")` becomes `get_data_dir()`.** Same 11 files above, one
   line changed each — no other logic changes.
3. **`.gitignore`**: add `data_testing/` so test data never gets committed.
4. **No UI changes.** This is invisible to the user — same app, same buttons, just a
   different folder underneath depending on where it runs.

## How you'll use it (no code, just settings)

- **On your laptop:** do nothing — `APP_ENV` is unset, so it automatically uses
  `data_testing/`. Break things freely.
- **On Streamlit Community Cloud (production):** open the app's *Settings → Secrets* and
  add one line: `APP_ENV = "production"`. That flips it to the real `data/` folder. Secrets
  are private to the cloud app, never committed to git.
- **If you ever run a second "staging" cloud app** from the same GitHub repo: just leave
  `APP_ENV` unset (or anything other than `"production"`) on that one, so it stays on
  `data_testing/` too.

## Test plan

- Unit test for `get_data_dir()`: no `APP_ENV` → `data_testing`; `APP_ENV=production` →
  `data`; `APP_ENV=development` → `data_testing`.
- Run the full suite (`uv run pytest -q`) after the 11 files are updated, to confirm nothing
  that touches the database or file storage broke.
