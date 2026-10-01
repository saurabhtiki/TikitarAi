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
  phase 50 (done) - meeting agenda items can be a **Question** with an Answer type (Text, Number, Date, Yes/No, Choice), a Rule (`between 20000 and 60000`, `future`, `Morning, Evening, Night`...) and Tries: the invitee is asked them first, one at a time ("Question 2 of 5"), the model only converts the reply ("45k" -> 45000) while `meetings/steps.py` decides if it passes, a wrong answer re-asks and uses a try, a side question uses none, running out saves "Not answered" and moves on, and Comparisons gains a Question answers tab; phase 51 (done) - a Question row gains a **Go to** cell (`if > 60000 go to End; if No go to Notice period`, conditions in the Rule words, Yes/No or Choice options, Text can't branch): the first match jumps **forward only** to a later question or End, a Not answered question never branches, date conditions are judged on the day the answer was saved, the path is recomputed from saved answers (`steps.question_path`, no new table), progress counts along the path, and the organiser sees "— skipped" and a status count that leaves skipped questions out; phase 52 (done) - a Question row gains a **For each** cell naming a list (e.g. "Outstanding invoices"): questions sharing it (kept together) are asked once per row of a list uploaded in Overview (optional Match column = only rows with the invitee's name/email), Go to inside a list can say `Next row`, answers are stored per row in `meeting_step_answers` under `steps.answer_key(title, row)` (no new table; `match_column` added to `meeting_agenda_tables`), the bot is given the row, and Comparisons gains a List answers tab with a filled-in Excel download (`meetings/loops.py`); phase 53 (done) - Overview gains a **FAQ** box (upload Excel/CSV with Question | Answer, max 300): the whole FAQ goes into the chat prompt and the bot answers side questions only from it; a question it does not cover is logged via `ChatTurnOutput.unanswered_question` (tables `meeting_faqs`, `meeting_faq_misses`, once per invitee), the organiser answers it in Overview and **Add to FAQ** moves it into the FAQ, the Status tab counts them, and no FAQ means no logging (`meetings/faq.py`); phase 54 (done) - the New meeting window gains **Start from** (5 templates: HR interview, Vendor purchase, Sales quotation, Project review, AR review - `meetings/templates.py`) with Use template, **Describe it in plain English** + Draft with AI (`meetings/drafting_agent.py` writes rows into the grid, still checked by `steps.agenda_problems`), and a **Flow picture** (`meetings/flow.py` DOT via `st.graphviz_chart`: order, blue Go to arrows, For each box with next row, red '?' for a bad Go to) under the grid and in Overview; phase 55 (done) - Invitees & status gains an **Add invitee** form (name, email; new link + code in Share, duplicate/bad email refused) and the Overview FAQ box gains a **Download template** Excel (Question | Answer + 2 sample rows, `faq.template_frame`); phase 56 (done) - a meeting has its own **AI model** (any Settings profile incl. light, picked in New meeting, changeable in Overview; a deleted one falls back to the default with a red warning; `meetings/llm_choice.py`, `meetings.profile_id`, `meeting_sessions.model_used` shown in Status), the FAQ is an **editable table** under Download template + Upload (upload replaces the table, Save FAQ stores it, empty = remove), the invitee page gets a **1/4 side panel** (How to use, Agenda, Attach a file), My meetings gets **Find a meeting** + Status filter (`meetings/listing.py`), and **Close chat** stays disabled until every question on the path is done (`steps.questions_left`); phase 57 (done) - Invitees & status shows each invitee's **attached files** (download), a red **note** when a Number Go to reaches past its Rule (`steps.agenda_warnings`; a refusal now says "700000 is too high"), the invitee's How to use names each **table tab + columns** and gets a **Reference documents** box, View chat/summary get **Download chat (.txt) / all chats (Excel) / summary (.txt)** (`meetings/transcript.py`), and For each lists go to **everyone by default** with a **Row name column** (`label_column`, "Invoices — INV-102: row 2 of 3"), the name/email filter under Advanced, **Save list settings** without re-upload, a 0-rows warning, and the list shown on the invitee's left panel with ✓ done / ▶ now; phase 58 (done) - agenda/evaluation grids no longer add a blank row under real rows, invitees are **optional** at creation, **Save as template** (New meeting + Overview popover: name → save, same name → update; built-in names refused; shown as "(mine)" in Start from with Delete; table `meeting_templates`, per user), and the chat bot answers side questions **only** from context/SOP/agenda notes/FAQ — never general knowledge — even with no FAQ; phase 59 (done) - Overview gains **Invitee instructions** (edit the How to use steps, blank/Use default = automatic, `meetings.how_to_use`, `meetings/guide.py`), the bot greets the invitee **by name**, and table columns the invitee fills in get **Type / Rule / Required** (`meeting_agenda_tables.column_rules`; number, calendar date, Yes/No + Choice dropdowns, Text `at most N characters`; checked on Save progress by `tables.check_rows`, Required only for started rows; **Save column rules** without re-upload); phase 60 (done) - the invitee's grid shows a **You can fill in** line and editable columns are **pre-filled from the uploaded sheet** (only changed cells are saved/counted, Required met by a sheet value); phase 61 (done) - templates carry their own **FAQ**: each built-in template has FAQ rows (`MeetingTemplate.faq`), New meeting gains an **FAQ (optional)** table filled by Use template and saved as the meeting's FAQ on Create, and Save as template keeps the FAQ (`meeting_templates.faq_json`)

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