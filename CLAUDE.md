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

<!-- delet last pase & update this line each time a phase is completed -->
  phase 50 (done) - meeting agenda items can be a **Question** with an Answer type (Text, Number, Date, Yes/No, Choice), a Rule (`between 20000 and 60000`, `future`, `Morning, Evening, Night`...) and Tries: the invitee is asked them first, one at a time ("Question 2 of 5"), the model only converts the reply ("45k" -> 45000) while `meetings/steps.py` decides if it passes, a wrong answer re-asks and uses a try, a side question uses none, running out saves "Not answered" and moves on, and Comparisons gains a Question answers tab; phase 51 (done) - a Question row gains a **Go to** cell (`if > 60000 go to End; if No go to Notice period`, conditions in the Rule words, Yes/No or Choice options, Text can't branch): the first match jumps **forward only** to a later question or End, a Not answered question never branches, date conditions are judged on the day the answer was saved, the path is recomputed from saved answers (`steps.question_path`, no new table), progress counts along the path, and the organiser sees "— skipped" and a status count that leaves skipped questions out; phase 52 (done) - a Question row gains a **For each** cell naming a list (e.g. "Outstanding invoices"): questions sharing it (kept together) are asked once per row of a list uploaded in Overview (optional Match column = only rows with the invitee's name/email), Go to inside a list can say `Next row`, answers are stored per row in `meeting_step_answers` under `steps.answer_key(title, row)` (no new table; `match_column` added to `meeting_agenda_tables`), the bot is given the row, and Comparisons gains a List answers tab with a filled-in Excel download (`meetings/loops.py`); phase 53 (done) - Overview gains a **FAQ** box (upload Excel/CSV with Question | Answer, max 300): the whole FAQ goes into the chat prompt and the bot answers side questions only from it; a question it does not cover is logged via `ChatTurnOutput.unanswered_question` (tables `meeting_faqs`, `meeting_faq_misses`, once per invitee), the organiser answers it in Overview and **Add to FAQ** moves it into the FAQ, the Status tab counts them, and no FAQ means no logging (`meetings/faq.py`); phase 54 (done) - the New meeting window gains **Start from** (5 templates: HR interview, Vendor purchase, Sales quotation, Project review, AR review - `meetings/templates.py`) with Use template, **Describe it in plain English** + Draft with AI (`meetings/drafting_agent.py` writes rows into the grid, still checked by `steps.agenda_problems`), and a **Flow picture** (`meetings/flow.py` DOT via `st.graphviz_chart`: order, blue Go to arrows, For each box with next row, red '?' for a bad Go to) under the grid and in Overview; phase 55 (done) - Invitees & status gains an **Add invitee** form (name, email; new link + code in Share, duplicate/bad email refused) and the Overview FAQ box gains a **Download template** Excel (Question | Answer + 2 sample rows, `faq.template_frame`)

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