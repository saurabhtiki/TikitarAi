# Project

AI-powered data analysis and visualization automation, built with the Agno agent framework (Python) and a Streamlit UI.

# Stack

- Python, Agno (agents), Streamlit (UI), UV (depency management)
- See `docs/requirements.md` for full project scope and requirements
- See `docs/plan.md` for the current phase's plan

# Workflow

Plan → Confirm Plan → Test → Build, one phase at a time.
- Plan: read `docs/requirements.md`& prepare a draft plan
- Confirm Plan : using skill-"grill-me" on draft plan,ask questions before making final plan, only after plan is confirmed write/update `docs/plan.md` for the next phase only.
- Build: implement only that phase as per plan
- Test: write and run tests before moving to the next phase
- End of each phase: update (remove old) "Current phase" (in 1 line short) below before starting the next

# Current phase

<!-- delet last pase & update this line each time a phase is completed with 2-3 lines in brief-->
 phase 71 (done) - Question/Discussion rows gain **Accept only if** (plain-words condition, `AgendaItem.accept_if` in the agenda JSON): a question's reply that passes the Rule is also judged by the AI (`chat_agent.read_accept`) and asked again with no tries limit; a discussion ends only when the condition is met (no 5-reply limit); such a step holds Close chat (`steps.questions_left`), and Question answers gains a **Stuck at** column (`steps.stuck_at`)

# Library docs (Agno, Streamlit)

Agno and Streamlit APIs change frequently — do not rely on training knowledge for either.
- Streamlit: use the official `streamlit skills` install (auto-synced to installed version, no lookup needed)
- Agno: use the official docs MCP (`agno-docs`, https://docs.agno.com/mcp) for current API details.

After writing Agno/Streamlit code, verify it against these sources and flag anything deprecated.

# Coding conventions

- Naming: snake_case for functions/variables, PascalCase for classes, descriptive names (no `df1`, `temp`, `x`)
- Streamlit: every widget must have a unique `key=` and a `help=` tooltip
- streamlit: Use width property instead of old container_width property
- streamlit: show dates in table or dataframe in dd-mm-yyyy instead of default yyyy-mm-dd
- Every function: wrap risky logic (I/O, parsing, API/model calls) in try/except with specific, user-facing error messages — no bare `except:`
- Log errors before raising or displaying them
- After finishing a phase, run `/code-review` or `/simplify` and check output against this list
- when use st.caption use red color

# Answers & Plan
- keep your language for Answers & Plan short & simple as layman can understand, give small examples so user can understand better.