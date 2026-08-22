# Project

AI-powered data analysis and visualization automation, built with the Agno agent framework (Python) and a Streamlit UI.

# Stack

- Python, Agno (agents), Streamlit (UI), UV (depency management)
- See `docs/requirements.md` for full project scope and requirements
- See `docs/plan.md` for the current phase's plan

# Workflow

Plan → Test → Build, one phase at a time.
- Plan: read `docs/requirements.md` and write/update `docs/plan.md` for the next phase only, before building
- Build: implement only that phase
- Test: write and run tests before moving to the next phase
- End of each phase: update "Current phase" (in 1 line short) below before starting the next

# Current phase

<!-- update this line each time a phase is completed -->
All 10 stages of requirements.md done; plus phase 12 — reports carry a logo, number items as 2.1.1, and offer a Custom style set from a dialog with saveable themes

# Library docs (Agno, Streamlit)

Agno and Streamlit APIs change frequently — do not rely on training knowledge for either.
- Streamlit: use the official `streamlit skills` install (auto-synced to installed version, no lookup needed)
- Agno: use the official docs MCP (`agno-docs`, https://docs.agno.com/mcp) for current API details.

After writing Agno/Streamlit code, verify it against these sources and flag anything deprecated.

# Coding conventions

- Naming: snake_case for functions/variables, PascalCase for classes, descriptive names (no `df1`, `temp`, `x`)
- Streamlit: every widget must have a unique `key=` and a `help=` tooltip
- streamlit: Use width property instead of old container_width property
- Every function: wrap risky logic (I/O, parsing, API/model calls) in try/except with specific, user-facing error messages — no bare `except:`
- Log errors before raising or displaying them
- After finishing a phase, run `/code-review` or `/simplify` and check output against this list

# Answers & Plan
- keep your language for Answers & Plan short & simple as layman can understand, give small examples so user can understand better.