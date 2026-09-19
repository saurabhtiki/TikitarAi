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
 phase 26 — Transform Data gains the nine clean-up steps it was missing (skip rows, remove empty rows, fix numbers stored as text, round, trim spaces, remove special characters, letter case, find & replace, unpivot), and each uploaded table gets a Fix headers button that opens Skip rows pre-filled; phase 27 (done) — Transform Data steps can be saved as a named pipeline that matches next month's upload by file name, checks the columns its steps actually need before running, replays every step with no AI, and hands the finished table to Chat with Data via an Export to menu; phase 28 (done) — Transform Data gains a Describe a step button beside Add a step: type an instruction in plain English, the Light Model turns it into steps from the existing catalog only (never code), you see them described and previewed before adding, and saved pipelines still replay with no AI; the model is sent each table's columns with their types, and answers in strict-schema-safe name/value pairs so cloud providers fill them in; an "update column X if Y" instruction is built as add/drop/rename so it actually overwrites the column, and its "otherwise" side keeps the column's real value (text included) instead of blanking it; phase 29 (done) — the calculated-column formula box can now test and choose: `amount > 1000` gives a 1/0 flag and `price * 0.9 if quantity > 100 else price` picks between two numbers (tests `> < >= <= = !=`, choices chainable, a blank or unreadable test takes the else side); it stays a printable whitelist with no functions and no text answers, so `add_conditional_column` is still the tool for text branches and the non-maths tests, and the AI is told to use the one-step formula only when both answers are numbers; phase 30 (done) — seven finance helpers join the catalog (36 operations now) with no UI change: Today's date, Add or subtract days, Start or end of month, Take part of a code (characters by position, for codes with no separator), Percentage of total, Drop the minus sign, and Smallest or largest across columns (sideways, per row — not a groupby); blanks rather than errors for unreadable values and a zero total, and all seven reach Describe a step straight from the registry

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
- when use st.caption use red color

# Answers & Plan
- keep your language for Answers & Plan short & simple as layman can understand, give small examples so user can understand better.