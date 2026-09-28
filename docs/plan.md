# Phase 54 — Meetings: templates, plain-English drafting and a flow picture

**Status: done.**

## The problem

Setting up a structured meeting means filling a grid with Rule, Go to and For each cells.
A new organiser starts from a blank row and has to learn the rule words first. And once
there are jumps ("if > 60000 go to End"), it is hard to see the path an invitee will take.

This is step 5 of 5 in the structured-meeting build order.

## What changes

**1. Templates (New meeting window).** A **Start from** box at the top with five ready-made
meetings, and a **Use template** button:

| Template | What it fills in |
|---|---|
| HR interview | Expected salary (Number, between 10000 and 500000, `if > 150000 go to End`), notice period, joining date (future), relocation (Yes/No) |
| Vendor purchase | Price, delivery date, payment terms (Choice), warranty |
| Sales quotation | Quantity, target price, delivery needed by, decision date |
| Project review | Task complete? (Yes/No, `if Yes go to Next update`), why late, new date, who is responsible |
| AR review | For each **Outstanding invoices**: will it be paid on time? (`if Yes go to Next row`), expected payment date, reason for delay |

It fills the meeting context, persona, Context / SOP, agenda and evaluation questions. The
subject and invitees stay yours. Everything can be edited before pressing Create.

**2. Describe it in plain English (New meeting window).** A text box and a **Draft with AI**
button. Example: *"Ask expected salary, must be a number; if above 1 lakh end the interview.
Then ask notice period in days."* The AI writes the agenda rows into the grid. Any row the
app can't use is listed in a warning so it can be fixed before Create. Needs a default
model (Settings), like the other AI buttons.

**3. Flow picture.** A diagram of the questions: Start → each question in order → End,
with the Go to jumps as labelled arrows (e.g. "> 150000") and a For each list drawn as a box
with a "next row" arrow back to its first question. Shown:
- in the New meeting window, under the grid (updates as you type), and
- in Overview, under the agenda.

A Go to the app can't read is drawn as a red arrow labelled "?" so it stands out.
Meetings with no Question rows show no picture.

**Not in this phase:** saving your own meetings as templates, Excel download/upload of the
agenda, and a "try it as the invitee" run.

## How it's built

- `meetings/templates.py` (new): the five templates as data (`MeetingTemplate` with context,
  persona, SOP, agenda, evaluation questions). Each must pass `steps.agenda_problems`.
- `meetings/drafting_agent.py` (new): `draft_agenda(profile, description)` via
  `run_structured`, returning `AgendaItem`s; unknown type words fall back to Discussion/Text.
- `meetings/flow.py` (new): `flow_dot(agenda)` builds a Graphviz DOT text; the page shows it
  with `st.graphviz_chart` (no new package).
- `app_pages/meetings.py`: Start from + Use template and Draft with AI in the dialog (they
  refill the grid and the text boxes through button callbacks); flow picture in the dialog
  and in Overview.

## Tests

- Templates: all five pass the agenda check and name only valid rules and jumps.
- Flow: order arrows, a labelled Go to arrow, End, a For each box with "next row", a red "?"
  for a bad Go to, quotes escaped, and no picture without questions.
- Drafting: the prompt carries the description and rule words; rows come back as agenda
  items; odd types fall back; a provider failure becomes a clear message.
- Page: Use template fills the grid and text boxes; Draft with AI fills the grid (model
  stubbed); Overview shows the flow picture for a meeting with questions.

---

# Phase 53 — Meetings: FAQ side questions

**Status: done.**

## The problem

Invitees ask side questions in the middle of a meeting ("What is the leave policy?", "Who
approves credit notes?"). Today the bot answers from the Context / SOP box or says it
doesn't know, and the organiser never learns what was asked. Organisers already keep these
answers in a spreadsheet.

This is step 4 of 5 in the structured-meeting build order (templates come last).

## What changes

**For the organiser — Overview** gets a **FAQ** box. Upload an Excel or CSV with a Question
column and an Answer column (usually 50–70 rows):

| Question | Answer |
|---|---|
| What is the notice period? | 60 days for managers, 30 days for everyone else. |
| Who approves credit notes? | The regional finance manager. |

- The columns named Question and Answer are picked automatically; the organiser can choose
  others. Blank rows are dropped. At most 300 questions.
- Uploading again replaces the FAQ. A Remove button deletes it.

**For the invitee:** they can ask a side question at any time, even in the middle of a set
question.
- If the FAQ has the answer, the bot answers **only from it**, then goes back to the
  question it was asking. Asking uses no try, as before.
- If the FAQ doesn't have it, the bot doesn't guess. It says *"I don't have that — I've
  noted it for the organiser, who will get back to you"*, logs the question, and carries on.

**For the organiser (results):**
- The FAQ box lists **Questions the bot couldn't answer**: who asked, what, and when
  (dd-mm-yyyy). The organiser can type an answer next to any of them and press **Add to
  FAQ**: those questions join the FAQ (so the bot can answer them for the next invitee) and
  leave the list. A Download Excel button gives the list as a file.
- The Status tab adds a red line: *"2 question(s) the bot couldn't answer — see Overview →
  FAQ"*.
- The same question from the same invitee is logged once.

**Without a FAQ** nothing changes: the bot behaves as today and nothing is logged.

**Not in this phase:** telling the invitee when their question has been answered, and
searching the FAQ instead of putting it all in the prompt (not needed at 50–300 rows).

## How it's built

- `meetings/model.py`: `FaqEntry(question, answer)`, `Faq(source_file, entries)`,
  `FaqMiss(miss_id, invitee_id, invitee_name, question, agenda_tag, created_at)`.
- `meetings/db.py`: two new tables — `meeting_faqs` (one row per meeting: file name and the
  entries as JSON) and `meeting_faq_misses` (one row per logged question). `save_faq`,
  `load_faq`, `delete_faq`, `add_faq_miss` (skips a repeat from the same invitee),
  `list_faq_misses`, `delete_faq_misses`.
- `meetings/faq.py` (new): reading the upload into entries, guessing the two columns, the FAQ
  text for the prompt, adding answered misses to the FAQ, and the misses table.
- `meetings/chat_agent.py`: `ChatTurnOutput` gains `unanswered_question`. With a FAQ, the
  instructions carry it and say: answer the invitee's own questions only from it; if it isn't
  there, say you will check, fill `unanswered_question`, and return to the current step.
- Invitee page: loads the FAQ, passes it to the bot, and logs `unanswered_question`. A
  failure to log is logged and never costs the invitee the reply.
- Organiser page: FAQ box in Overview, the unanswered list with Add to FAQ, the Status line.

## Tests

- Reading the upload: columns guessed, blank rows dropped, missing columns and too many rows
  refused.
- Prompt: FAQ entries and the "only from the FAQ" rule are present with a FAQ, absent without.
- Storage: save/replace/delete the FAQ, log a miss once per invitee, list with the invitee's
  name, delete; tables created on an old database.
- Invitee page: an unanswered question is logged; no FAQ means nothing is logged.
- Organiser page: upload saves the FAQ; Add to FAQ moves an answered question into it; the
  Status line counts the misses.

# Phase 52 — Meetings: repeat questions for each row of a list

**Status: done.**

## The problem

Some meetings go through a long list with the same questions for every line. In a weekly
receivables review, every outstanding invoice needs "Expected payment date" and "Reason for
delay". Typing one Question row per invoice is not practical, and the answers should end up
back in the list, as a filled-in Excel.

This is step 3 of 5 in the structured-meeting build order (FAQ and templates come later).

## What changes

**For the organiser — the agenda grid** gets one more column, **For each**, for Question rows
only. Question rows with the same For each name are asked once per row of that list:

| Agenda item | Type | Answer type | Rule | Go to | For each |
|---|---|---|---|---|---|
| Customer name confirmed? | Question | Yes/No | | | |
| Is it paid already? | Question | Yes/No | | if Yes go to Next row | Outstanding invoices |
| Expected payment date | Question | Date | future | | Outstanding invoices |
| Reason for delay | Question | Text | | | Outstanding invoices |
| Anything else? | Discussion | | | | |

**The list itself** is uploaded in Overview, in a new "List — Outstanding invoices" box (the
same way a Table item's sheet is attached today). Optional **Match column**: each invitee then
sees only their own rows — rows whose value in that column equals their name or email.
Example: Match column `Customer`, invitee "ABC Traders" gets only ABC Traders' invoices.

**Rules:**
- The questions of one list must sit together (one after another).
- The list name can't be the same as another agenda item's title.
- Go to inside a list can jump to a later question **of the same list** (same row),
  to `Next row` (skip the rest of this row), or `End`.
- Go to from an ordinary question can jump to the **first** question of a list, not the middle.
- If no list is attached yet, or an invitee has no matching rows, those questions are skipped.
- Uploading a new list replaces the old one and clears the answers given to it.
- Mistakes block **Create meeting** / **Save setup** with a sentence, as before.

**For the invitee:** the bot says which row it is on and asks the questions for it, e.g.
*"Invoice 1001 for 5,000, due 01-08-2026 — when do you expect to pay it?"*. The line above
the reply box reads *"Outstanding invoices: row 2 of 12, question 1 of 3"*. They can stop and
come back later; rows already answered are not asked again.

**For the organiser (results):**
- Comparisons gets a **List answers** tab: the list's own columns, plus one column per
  question, one row per invitee per list row, with a **Download Excel** button.
- A question jumped over shows `— skipped`, as before.
- The status line adds *"Outstanding invoices: 3 of 12 row(s) done"*.

**Not in this phase:** the "same answer for all 12 invoices?" shortcut, and using `{Bill No}`
inside the question title (the bot is given the whole row instead, so it can say it itself).

## How it's built

- `meetings/model.py`: `AgendaItem.loop` (the For each name, written to the JSON only for
  questions that have one). `AgendaTable.match_column`.
- `meetings/db.py`: `match_column` added to `meeting_agenda_tables` (guarded `ALTER TABLE`);
  `delete_row_answers` clears a list's answers when it is replaced. **No new table:** a row's
  answer is stored in `meeting_step_answers` under a key made of the question title and the
  row number.
- `meetings/steps.py`: the path is now a list of **steps** (question + row). `walk()` goes
  through them following Go to (now also `Next row`) and records which were skipped.
  `next_question`, `progress_text` and `skipped_questions` use it; loop checks are added to
  `agenda_problems`.
- `meetings/loops.py` (new): which rows each invitee gets, the row's text for the bot, the
  per-list progress, and the results table for the organiser.
- `meetings/chat_agent.py`: the question guidance and the answer reader are given the row.
- Pages: For each column; list upload box in Overview; List answers tab with Excel download;
  status line; the invitee page passes the rows through.

## Tests

- Loop checks: questions not together, list name clashing with a title, Go to rules inside
  and outside a list, `Next row` outside a list.
- Walking: every row gets every question, answers keyed per row, `Next row` skips the rest of
  a row, End inside a list, no rows means skipped, matching rows by name or email.
- Progress text and results table (answers, skipped, not reached).
- Pages: the invitee is asked about row 1 then row 2; the organiser sees the List answers table
  and the status line; the upload box saves the list and match column.
- Full suite still passes.

---

# Phase 51 — Meetings: branching between questions

**Status: done.**

## The problem

Phase 50 asks every question in grid order. The organiser wants the answer to change the path,
for example "if the salary is above budget, skip the rest", or "if they won't relocate, skip
the city question".

This is step 2 of 5 in the structured-meeting build order (loops, FAQ and templates come later).

## What changes

**For the organiser:** the agenda grid gets one more column, **Go to**, for Question rows only.
It holds one or more lines of the form `if <answer> go to <question title>`, separated by `;`.
`End` as the target skips all the remaining questions.

| Agenda item | Type | Answer type | Rule | Go to |
|---|---|---|---|---|
| Expected salary | Question | Number | > 0 | if > 60000 go to End |
| Willing to relocate? | Question | Yes/No | | if No go to Notice period |
| Preferred city | Question | Choice | Pune, Delhi | |
| Notice period | Question | Number | <= 90 | |
| Preferred shift | Question | Choice | Morning, Evening, Night | if Night go to Night allowance; if Morning, Evening go to End |

**What `<answer>` can be**, by answer type:
- **Number:** as in Rule: `> 60000`, `<= 30`, `between 1 and 5`
- **Date:** as in Rule: `after 01-12-2026`, `before 31-12-2026`, `future`, `past`, `within 30 days`
  (judged on the day the answer was given, so the path never changes later)
- **Yes/No:** `Yes` or `No`
- **Choice:** one or more of the question's options, with commas: `Morning, Evening`
- **Text:** can't branch

**Rules for Go to:**
- The first line that matches wins. No match (or a Not answered question) goes to the next
  question as before.
- It can only jump **forward**, to a later Question row or `End`. This stops endless loops.
- Mistakes block **Create meeting** / **Save setup** with a sentence, for example:
  *"Willing to relocate?: 'Notice' isn't a later question. Go to can only jump forward to a Question row, or End."*

**For the invitee:** skipped questions are simply never asked. The line above the reply box
counts the path, e.g. "Question 3 of 4". The "of" number can drop after a jump.

**For the organiser (results):** a skipped question shows `— skipped` in the Question answers
table, and the status line counts only questions on that invitee's path
("2 of 3 question(s) answered"). The Overview shows each question's Go to lines.

## How it's built

- `meetings/model.py`: `AgendaItem.branch` (text, written to the JSON only for questions that
  have one, so older agendas read back unchanged).
- `meetings/steps.py`:
  - `parse_branches(item, agenda)` reads the Go to text into `(condition, target)` pairs, or
    raises `RuleError`. `branch_problem` turns that into a sentence and `agenda_problems`
    includes it.
  - `branch_target(item, answer)` returns the target title, `End`, or `None` (go on in order).
  - `question_path(meeting, answers)` walks from the first question, following branches, and
    returns the questions asked so far plus the open one. `next_question`, `progress_text` and
    the new `skipped_questions` are built on it.
- `app_pages/meetings.py`: the **Go to** column (with `help=`), Overview line, `— skipped`
  cells, and the status count.
- `app_pages/meeting_invitee.py`: `progress_text` now takes the answers.
- No database change: the path is worked out from the saved answers every time.

## Tests

- `tests/test_meetings_branching.py`: parsing each answer type, forward-only and unknown-target
  errors, text refused, first match wins, End, Not answered doesn't branch, date judged on the
  answer day, progress, skipped list, JSON round trip.
- Page tests: an invitee answer that jumps skips a question; the organiser table shows
  `— skipped`.

# Phase 50 — Meetings: question steps with answer type, rule and tries

**Status: done.**

## The problem

Today the meeting AI chats freely: if it asks "What is your expected salary?" and the candidate
says "good money", it may simply move on. The organiser wants **step-by-step questions** where
the bot keeps asking until it gets a proper answer, for example a number within the budget or
a date in the future.

This is step 1 of 5 in the structured-meeting build order. Branching, loops over lists, FAQ
side questions and templates come in later phases.

## What changes

**For the organiser (agenda grid):** the agenda grid gets a third **Type**, `Question`, and
three new columns:

| Agenda item | Type | Answer type | Rule | Tries | AI note |
|---|---|---|---|---|---|
| Expected salary | Question | Number | between 20000 and 60000 | 3 | Monthly, in INR |
| Notice period end | Question | Date | future | 3 | |
| Willing to relocate? | Question | Yes/No | | 2 | |
| Preferred shift | Question | Choice | Morning, Evening, Night | 3 | |
| Tell us about yourself | Question | Text | at least 20 characters | 3 | |

**Answer types and the rules each one understands** (a blank rule means "any answer of that type"):
- **Number:** `> 0`, `>= 1000`, `< 90`, `<= 90`, `between 20000 and 60000`
- **Date** (written as dd-mm-yyyy): `future`, `past`, `after 01-10-2026`, `before 31-12-2026`,
  `within 30 days`
- **Text:** `at least 20 characters`
- **Choice:** the options, separated by commas (a rule is required)
- **Yes/No:** no rule

If a rule can't be read, **Create meeting** or **Save setup** refuses to save and names the row,
for example: *"Expected salary: 'more than 5k' isn't a rule I understand. For a number, write
it like `> 5000` or `between 20000 and 60000`."*
The same check refuses an agenda that uses one title twice, because answers are stored by title.

**For the invitee:** questions are asked first, one at a time and in grid order. A line above
the reply box shows *"Question 2 of 5"*.
- A valid answer is saved and the bot asks the next question.
- A wrong answer: the bot explains the rule in simple words and asks again. That uses up one try.
- The invitee asks something instead of answering ("Is this remote?"): the bot answers and asks
  again. No try is used.
- Out of tries: the question is saved as **Not answered** with the last reply, and the bot
  moves on, so nobody is stuck forever.
- After the last question, any Discussion items are chatted through as they are today. If there
  are none, the bot says thanks and asks the invitee to press **Close chat**.

**For the organiser (results):** *Comparisons* gets a new **Question answers** tab with one row
per invitee and one column per question. For example, Raj's row reads `45000 | 15-11-2026 | Yes`,
and an unanswered question shows `⚠ Not answered (said: "good money")`. *Invitees & status*
shows "3 of 5 questions answered".

## How it's built

- `meetings/model.py`: `QUESTION_ITEM` item type. `AgendaItem` gains `answer_type`, `rule` and
  `max_tries` (written to the JSON only for questions, so older agendas read back unchanged), plus
  `Meeting.question_items()`.
- `meetings/steps.py` (new, no AI and no Streamlit):
  - `rule_problem(item)` returns a plain sentence if the rule can't be read.
  - `check_answer(item, value, today)` returns ok or not, the cleaned value, and the reason.
  - `describe_rule(item)` gives the rule in words for the prompt.
  - `next_question(meeting, answers)` returns the next unfinished question.
- `meetings/chat_agent.py`:
  - `read_answer(profile, item, message, today)`: the model only *converts* the reply, e.g.
    "45k" → `45000` or "next Monday" → `05-10-2026`, or reports that it was a question. Python
    then decides whether the answer passes the rule.
  - `send_turn` / `opening_message` gain `step_guidance`, which tells the model what just
    happened and which question to ask now.
  - Question items are left out of the free agenda list.
- `meetings/db.py`: new table `meeting_step_answers` with columns meeting, invitee, question
  title, value, status (`pending`/`answered`/`not_answered`), tries, last reply and time. It
  gets `load_step_answers`, `save_step_answer` and `list_all_step_answers` (the next state comes
  from `steps.record_attempt`, which is pure). Changing a
  question's title detaches its answers, the same way tables do today.
- `app_pages/meeting_invitee.py`: while a question is pending, each turn runs read → check →
  record → reply, with a progress caption.
- `app_pages/meetings.py`: the new grid columns, the save-time rule check, the Overview line
  (e.g. "Expected salary · question · Number, between 20000 and 60000"), the status count and
  the Question answers tab.

## Tests

- **Rules:** every example rule above passes a good answer and refuses a bad one. Unreadable
  rules are refused with a sentence. A Choice question with no options is refused. Dates are
  checked against a fixed "today".
- **Model:** a question item round-trips through JSON, and an old agenda without the new keys
  still loads.
- **Storage:** tries count up, the status becomes `not_answered` at the limit, a question
  doesn't use a try, and `next_question` skips finished questions.
- **Chat agent:** `read_answer` and `step_guidance` reach the prompt, and question items are
  not listed as free agenda (the provider is stubbed).
- **AppTest (invitee):**
  - A good answer moves to question 2.
  - A bad answer re-asks and shows "Question 1 of 2".
  - Running out of tries moves on and saves "Not answered".
- **AppTest (organiser):**
  - The status count and the Overview line describe the questions. (The save-time check is
    tested on `steps.agenda_problems` directly, because AppTest cannot type into a data editor.)
  - The Question answers tab shows the saved answers.

## Not in this phase

Branching ("if salary > budget, go to the end"), loops over uploaded lists, FAQ side questions,
templates and the flow diagram come in phases 51–54.

---

# Phase 49 — Everyone can run every Transform Data pipeline

**Status: done.**

## The problem

Transform Data's **Saved pipeline** picker lists only the pipelines *you* saved. Like reports
(phase 48), Ravi should be able to run Anna's "Monthly clean-up" on this month's files.

## What changes

**Everyone can:** see every saved pipeline, with its owner, like *"Monthly clean-up · by Anna —
last saved 2026-09-20 10:15"* (or *"· by you"*), and run it with **Run this pipeline**. Typing
"anna" in the picker finds Anna's pipelines. **Save as pipeline** still works: it saves your
own copy under your name.

**Only the owner can:** **Update pipeline** and **Delete**. For anyone else both are grey, with
the tooltip *"Only Anna can change or delete this pipeline. Save as pipeline keeps your own
copy."*, and the line beside the picker says the same.

**Example:** Ravi picks "Monthly clean-up · by Anna", uploads September's file and presses Run.
He wants one more step, so he adds it and presses **Save as pipeline** → "Ravi's clean-up".
Anna's pipeline is untouched.

## How it's built

- `transform/db.py`: `list_all_pipelines()` (with `owner_name`), `load_pipeline_for_run(id)`
  and `pipeline_owner(id)`, the same shape as phase 48's Task helpers. Every write stays
  owner-scoped.
- `app_pages/transform_data.py`: the picker lists everyone's pipelines (`saved_picker.select_saved`
  gains `row_face` for the "· by" label), selecting loads with `load_pipeline_for_run`, and
  `_change_blocked_reason` greys Update/Delete and is checked again when either dialog's button
  is pressed (a non-owner with a same-named pipeline would otherwise overwrite their own).
- `tests/conftest.py`: loads the `dashboard` package before any page test, since
  `app_pages/dashboard.py` hid it when a page test file ran on its own.

## Tests

- Storage: every account listed with owner names, another account loads to run, owner lookup,
  another account still can't save or delete, a missing pipeline says so.
- AppTest: Anna sees the admin's pipeline "· by" its owner and runs it; Update/Delete are grey
  for her (Save as is not); pressing either dialog's button is refused; the owner's stay live.

---

# Phase 48 — Everyone can run every report

**Status: done.**

## The problem

Reports → **Pick a task to run** lists only the reports *you* built. This is a small internal
team, so Ravi (a normal user) should be able to run Anna's "Monthly sales" report on this
month's file without asking Anna to do it.

## What changes

**Everyone can:**
- **See** every saved report in the Reports list. Each row shows who owns it:
  *"Monthly sales · by Anna"*.
- **Run** any report on new files (Run, Schema, Chat and Dashboard all work for everyone).
- **Send** a cleaned table from Transform Data to any report (Export to → Reports).

**Only the owner can:**
- Change the report's design, in Report Builder (as today, admins and super users only).
- Delete it.
- **Save notes into the report** after a run (the button that writes this run's notes,
  pictures and pasted HTML back into the saved report). For anyone else the button is grey,
  with the tooltip *"Only Anna can save changes into this report."* Their run still works,
  and the notes still show in the report they download.

**Example:** Ravi opens Reports, searches "sales", and sees *"Monthly sales · by Anna"*. He
presses Run, uploads September's file and gets the report, then opens its Chat and Dashboard.
He can't change the report's layout, and he can't delete it.

## Rules

- **Report Builder doesn't change.** Its Open/Delete list still shows only your own reports,
  so nobody can edit or delete someone else's. Normal users still don't see it.
- **Chat with reports already shows everyone's reports.** No change there.
- **Same name, two owners:** if Anna and Ravi both have "Monthly sales", the owner's name
  tells them apart.
- **Search** matches the owner's name too, so typing "anna" lists Anna's reports.
- The report a run produces (and what Chat and Dashboard show) belongs to the report, not
  the person who ran it, the same as today. The newest run is what everyone sees.

## How it's built

- `tasks/db.py`:
  - `list_all_tasks()`: every Task with its owner's name (joined from `users`), no JSON.
  - `load_task_for_run(task_id)`: reads any Task, for running only. `task_owner(task_id)`:
    who owns it. `owner_label(row, user_id)`: "you" or the owner's name. Every write
    (`save_task`, `delete_task`) stays scoped to the owner.
  - `list_tasks` / `load_task` stay as they are for Report Builder.
- `app_pages/run_task.py`: the picker uses `list_all_tasks`, shows "by <owner>", searches the
  owner's name, and Run and Schema use `load_task_for_run`. The save-notes button is disabled
  unless you own the report, and the owner is checked again when it is pressed. (`save_task`
  matches by name within the saver's own account, so a non-owner with a report of the same
  name would overwrite their own one rather than be refused.)
- `app_pages/report_view.py`: `render_report_output(save_blocked=...)` greys the button out
  with that reason.
- `app_pages/transform_data.py`: the Send to a report dialog lists every report, with the
  owner's name.

## Tests

- `list_all_tasks` returns two users' Tasks with owner names. `load_task_for_run` reads
  someone else's Task, while `save_task` and `delete_task` still refuse a non-owner.
- AppTest: a normal user's picker shows another user's report with "by <owner>", Run opens
  it, and the save-notes button is disabled for them but enabled for the owner.
- Search by owner name. Transform Data's dialog lists another user's report.

---

# Phase 47 — Send a cleaned table from Transform Data to a Report

**Status: done.** Phase 45's plan is in git history; phase 46's is below.

## The problem

People clean each month's file in **Transform Data**, then download it and upload it again on
**Reports**. That is an extra step, and it's easy to upload the wrong file.

## What changes

**Export to** gets a second choice: **Reports** (next to Chat with Data).

1. Tick the finished table under **Download**, then press **Export to → Reports**.
2. A dialog asks:
   - **Report:** which of your saved reports.
   - **"merged_data" is which of the report's files?** It is already filled in when the
     name matches or the report expects only one file.
3. Press **Send and open the report**. You land on the Reports run screen with the table
   already loaded as that file. Check Step 2, then press **Run task**.

**Example:** Rahul cleans April's sales in Transform Data (trims spaces, fixes dates). He picks
**Export to → Reports → Monthly sales**, and "sales" is already chosen. He presses Send, and the
Reports page opens with April's data loaded. He presses Run.

## Rules

- **Missing column = no send.** If the report needs `region` and the table hasn't got it,
  the dialog says *"merged_data has no **region**... Add a Rename step, then send again."*
  and the Send button stays grey. (On the Reports page a sent table has no file to remap,
  so the fix belongs here.)
- **Loaded like an upload.** The table is turned back into text and loaded with the
  report's saved column types, exactly as if it were downloaded and uploaded. So the
  Reports page's normal check still catches text in a number column, in the same words.
- **Several files:** if the report expects Sales **and** Customers and you send only Sales,
  the dialog says *"Still to upload on the Reports page: customers."* Upload that one as usual.
- Two tables sent as the same file are refused. **Don't send** leaves a table out.
- Sending again replaces what was sent last time, and any table already loaded under that name.
- Only your own saved reports are listed, the same as the Reports page.

## How it's built

- `transform/report_handoff.py` (no Streamlit): the likely file, the reasons to refuse,
  and the files still to upload.
- `engine/loading.py::raw_from_frame`: a table back to upload-style text (dates
  `yyyy-mm-dd`, an id of 7 stays `7`, not `7.0`).
- `engine/session.py::adopt_for_report`: loads the tables under the report's names and
  saved types, and records what the load found for Step 2.
- `runner/session.py::receive_sent_tables`: opens the report (only if it isn't already
  open, so files you uploaded for it stay), then loads the tables.
- `app_pages/transform_data.py`: the **Reports** menu choice and the **Send to a report** dialog.

## Tests

- `raw_from_frame`: dates, whole numbers, blanks, time of day.
- `report_handoff`: name match, one-and-one, missing column, duplicate, nothing chosen.
- AppTest: Reports opens the dialog, a send loads `sales` under its saved types with a clean
  match report and a message queued for Reports, a missing column disables Send, and Cancel sends nothing.

---

# Phase 46 (done) — Anyone can view and download a report's dashboard

## The problem

A dashboard is designed in **Report builder → Dashboard** and saved with the report. But on
the **Reports** page, the people who upload each month's data can only Run, see the Schema
or Chat. There is no way to see the dashboard with the new numbers.

## What changes

A **Dashboard** button, **view and download only**. There is no editing and no AI.

| Where | What you see |
|---|---|
| **Reports** list | A **Dashboard** button on every report, next to Chat. |
| **Chat with reports** list | The same **Dashboard** button, so anyone can open it, even people who didn't build the report. |
| **Reports** run screen | After **Run task**, a **Show the dashboard** switch under the report draws it right there, with the numbers just uploaded. |

Pressing **Dashboard** opens a full-width page with:
- the report name, and *"Data last refreshed: 25-09-2026 by Rahul"*,
- **Download this dashboard** (one HTML file that works offline and stays clickable),
- the dashboard itself, filters and all,
- **Back**, to return to the list you came from.

**Example:** Rahul runs *Sales* with April's file. Priya opens **Chat with reports**, presses
**Dashboard** on *Sales* and sees April's numbers. She downloads the file and emails it.

## Rules

- **Data:** the report's saved data (the copy every run saves for Chat). So it always shows
  the latest run, and needs no upload.
- **Disabled** until the report has been run once, like Chat is ("Run this report once...").
- **No dashboard saved:** the page says *"This report has no dashboard yet. Build one in
  Report builder → Dashboard."*
- **A visual that can't be drawn** (for example, a column this month's file no longer has) is
  named in a warning above the dashboard, the same way Report builder names it.
- **Too much data:** the same row limit as Report builder. It says so instead of freezing.
- **Only the dashboard is shared.** The rest of the report (its SQL, checks and so on) stays
  private to the person who built it, as before.

## How it's built

- `live_dashboard/saved_view.py` (no Streamlit): flattens the saved tables with the saved
  links and builds the HTML, plus the list of visuals that can't be drawn. Pure, so it can be tested.
- `tasks/db.py::load_dashboard_spec(task_id)`: reads **only** the dashboard part of a
  report, for any user.
- `app_pages/dashboard_viewer.py`: shows it (the refreshed line, problems, download and preview).
  Its result is cached per report, refresh time and dashboard, so reruns don't rebuild it.
- `app_pages/report_dashboard.py`: a hidden page (not in the menu) the Dashboard buttons open.

## Tests

- `saved_view` builds a dashboard from a saved DuckDB file whose filter reaches a child
  table through a saved link, names a visual whose column is gone, and refuses when there is
  too much data.
- `load_dashboard_spec` works for a user who doesn't own the report and fails cleanly for a missing one.
- AppTest: Reports and Chat with reports show the Dashboard button (disabled with no data),
  and the viewer page shows the refreshed line, the download button and the "no dashboard yet" message.

## Not in this phase

- Editing the dashboard from Reports. That stays in Report builder.
- Transform Data → **Export to → Reports** is phase 47.
