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
- End of each phase: update "Current phase" below before starting the next

# Current phase

<!-- update this line each time a phase is completed -->
Stage 8 (previous): Criteria-Based Exceptional Reporting (requirement 6.5, written up from
`docs/addtion.md`) — complete. Build-order item 7. The new `checks/` package plus
`app_pages/checks_view.py` add a third view to the Chat page: business rules in plain
language become guarded SQL, and **saving a result auto-pins it to the Dashboard, which is
the report** — there is no separate report screen. Two deliberate departures from
`addtion.md` step 5: a criteria has **no chart of its own** (one stacked summary chart at the
foot of the Design tab compares every saved criteria instead, with an AI overview under it),
and Save pins **whichever of All / Failures / Passes is on screen** while the saved run keeps
every row — the counts, the remarks and the action drafts all read from the full run. Report
items a criteria owns can only be removed from the Checks tab; the Dashboard no longer
discards anything carrying a `source_id`. A criteria that already has SQL offers **Run saved
SQL** (no provider call — the stored statement is the recipe) beside **Regenerate SQL**, so
re-running a set against next month's file is free; a run that fails reports the error and
leaves regenerating to the user. The overview pins `summary.combined_chart` — counts and
shares as two panels of one figure, because a `PinnedItem` holds only one.
Follow-up emails, meetings and tasks are
drafted and downloadable as `.eml` / `.ics`; **nothing is sent — there is no SMTP anywhere
in this app**.

Stage 9: Chat Types (requirement 6.6, new) — complete. Build-order item 8, which pushed
Task Builder to item 9. The new `chat_types/` package saves a whole Steps 1–3 setup — the
expected tables with each column's semantic type, the links, the column descriptions — and
a picker above the Setup / Chat / Checks control selects one. Pick "Salary processing",
upload this month's files, and the setup is already there. `— New chat type —` is the
default and is every earlier stage's behaviour, unchanged.

The load-bearing decision: **a type mismatch is fixed or refused, never tolerated.** A
semantic type decides the real DuckDB column type, so a date column that quietly loads as
text turns `joining_date < '2024-04-01'` into a string comparison returning wrong rows with
no error — and in Checks, a wrong Yes/No straight onto the Dashboard. So the saved types go
in *with* the files (`engine.loading.prepare_declared_table`, called from `sync_tables`)
and overrule detection; a column whose values won't convert refuses the load with the
offending values named. Application is all-or-nothing per table: a missing column or a
refusal applies nothing and falls back to detection, and the Chat and Checks views stay
shut until the upload matches. Extra columns and extra files are dropped and reported.
Calculated-column statements are deliberately **not** part of a chat type — they are §7.5's
Task recipe. Start over keeps the chat type selected, because uploading next month's files
against the same setup is the normal way to use one.

The **Delete button is commented out** in `_render_chat_type_bar` — deleting a chat type
isn't offered on the page. `_delete_chat_type`, `chat_types.db.delete_type` and its
un-scoping of criteria sets all remain, and the page test for it is skipped with that
reason, so un-commenting the button is all it takes to bring the feature back.

The match report lives **inside the Step 1 expander**, not in a panel of its own: once the
upload matches, collapsing Step 1 takes the green banner and its notes off the screen, so
Chat and Checks are not topped by a box repeating setup feedback. The step's header carries
`report.status_word()` — `matched` / `needs attention` — because that is all that shows
while it is shut. A blocking problem, or a discarded file, re-opens the step once
(`_open_step_one_on_problems`, keyed on the problems so a *different* one re-opens again but
a user who deliberately collapsed it is left alone). Acting on the check is deliberately not
gated on the step being open — a collapsed step must still discard extras and apply the
saved setup. `_sync_selection` moved inside Step 1 too, after `_render_upload`: it has to
run before the check, or a switching run would measure the upload against the chat type
being switched away from and discard tables on its say-so.

Three subtleties worth not rediscovering. **Selecting a chat type re-reads the uploader's
files** (`engine.session.reload_uploaded_tables`) — a table already in DuckDB was typed by
detection and the text is gone, so picking a setup after uploading would otherwise report a
clean match over a table it never touched. **The picker's selection is acted on after Step 1
has rendered**, never in the bar itself: a rerun that ends before `st.file_uploader` is
created counts as not rendering it, and Streamlit then drops the uploaded files. **A table
the declared load never saw** (Data Cleaner handoff, or detached from the uploader) can't be
re-typed, so `matching._verify_as_loaded` checks its types as they stand and blocks on a
mismatch instead.

`check_sets` gained a nullable `chat_type_id` (migrated in with a `PRAGMA table_info` guard;
the unique index is now `(user_id, COALESCE(chat_type_id, 0), name)` because SQLite treats
NULLs as distinct). A criteria set takes the scope it is saved under, Load set filters to
the active chat type with a "show all my sets" escape hatch, and deleting a chat type
**un-scopes** its sets rather than deleting them — renaming any whose name is already taken
among the unscoped ones, since the index would otherwise roll the whole delete back.

Out of band, earlier: the Data Cleaner gained Summarise / Pivot / Unpivot, which save a
derived table rather than recording a step.

Out of band, since: the Dashboard can lay items side by side. A placed item carries one
flag — `PinnedItem.column_with_previous`, shown as **Show in columns with above**, off by
default — and `model.group_into_rows` turns a run of them into a row of up to
`MAX_ROW_COLUMNS` (4) equal columns. **No row id is stored**, deliberately: the flag says
"I'm happy to share a row", so reordering can never leave a row pointing at an item that
moved out of it, and the two cases the flag can't be honoured (nothing above to join, row
above full) start a new row and say so rather than refusing. `RenderedSubsection.rows()`
is what the Preview and the HTML export both read; the Excel export ignores it and stays
on `items`, because a column in a worksheet means something else. A row of one is written
with **no wrapper element**, so a report that never touches the toggle produces byte-for-byte
the markup it always did and every preset and hand-edited stylesheet keeps working.

Out of band, since: **charts a user builds, in Checks as well as Chat.** The Data/Style
panel moved out of `chat_with_data.py` into `app_pages/chart_controls.py`, shared by both
pages and keyed by a `ChartKeys(prefix, suffix)` so the widget names are unchanged
(`an_chart_kind_1`, and now `ck_chart_kind_{check_id}`). `ChartKeys.created` records what it
hands out, because the aggregation pickers are named after columns and a prefix sweep could
not tell message 1's widgets from message 11's.

`analyst/charts.py` gained three things, every default reproducing what it drew before:
**Combo (bar + line)**, the one type Express can't express, so `_draw_combo` builds the
figure by hand with an optional `secondary_y`; **per-column aggregation** (`Aggregation`,
sum/count/average/min/max) applied by `_aggregate` as a pre-step that groups by x — and the
legend column with it, or every series would be handed the same totals — whose derived
columns then become the measures and run through cap → sort → draw untouched; and
**`choices_to_dict`/`choices_from_dict`** plus the style pair, the module's first
serialization, tolerant on the way in because a stored chart is a setting to honour as far as
it still makes sense, not input to validate. Reading it is what §7.5's `task_json` will want
too. Aggregating widens the Values box to *every* column, since counting turns a text column
into a number — which is how a result with no numeric column at all becomes a chart.

A criteria now offers **Generate chart** under its result, and Save pins the figure beside
the table (`_chart_figure`, drawn from `shown` so a "breaches only" item cannot carry a chart
including the passes). This reverses the earlier "no chart of its own" call for the
*user-built* case only — an **automatic** chart per criteria is still not drawn, because that
was one rule's rows repeated down the page. The spec lives on `Check.chart` /
`Check.chart_style` and goes into `checks_json` with no `SCHEMA_VERSION` bump, on the same
grounds as `summary`. The style is only rebuilt when there is a chart to wear it. Chat's
tables gained the same button: `routing.classify_output` judging a question to want a table,
or a wanted chart failing to draw, both left rows on screen with no route to a chart at all.
An already-pinned chat answer keeps its chart-less pinned copy — chat pins are one-shot
snapshots, and discard-then-repin is the existing way out.

Three follow-ups to that, from using it. **Pie is no longer withheld from a result with more
than `MAX_PIE_SLICES` rows** — `_cap_rows` already trims a pie to its top slices and says so,
and the row count was measured *before* aggregating, so the gate hid the type from exactly
the case it suits. A negative value still takes Pie off the list: that one has no honest
reading. **Aggregation starts on**, but in `chart_controls.seed_choices` rather than on
`ChartChoices`, whose default has to stay off — it is what every spec written before the
field reads back as, and what `pipeline`'s automatic chart keeps, whose SQL has already
grouped. Scatter is exempt (grouping a continuous x destroys the relationship). And the
**Customize panel is a stateful expander** (`key=` + `on_change="rerun"`): every control in
it ends in `st.rerun`, so a plain expander re-declared `expanded=False` and shut itself on
every change. `AppTest` can't drive that state — it rebuilds widget state from *widget*
nodes and an expander isn't one — so the test asserts the stamped element id, which only a
stateful expander carries.

Out of band, since: **Settings takes several models at once, and a designated default LLM.**
A profile row is still one provider *and* one model — that is what `client.build_model` and the
picker read — so the Add dialog's Models box fans out instead: `llm/models.py`'s
`parse_model_names` splits it on lines and commas, and `db.create_profiles` saves one row per
model sharing the nickname, URL and key, each named `OpenRouter — gpt-4o`. One model keeps the
nickname exactly as typed, so adding one behaves as it always did. `create_profiles` **returns
`(created, failure)`** rather than raising: there is no transaction across the rows, and a
failure part-way has to report how far it got instead of looking like nothing was saved.
The success message is queued in `settings_llm_flash`, because `st.rerun` closes the dialog and
anything written just before it never reaches the screen.

`is_default_model` copies `is_light_model` exactly — a nullable-free integer flag, at most one
per user enforced procedurally in `set_default_model`, migrated in with a `PRAGMA table_info`
guard. **Setting either flag clears the other**: `session.session_profiles` hides the light
model from the picker, so a light-and-default profile would be a default nothing could select.
`session.active_profile`'s two `profiles[0]` fallbacks now go through `_fallback_profile`, which
prefers the designated default — the sidebar picker reads its index from `active_profile`, so it
opens on the default with no change to `sidebar.py`.

Out of band, since: **the active chat type's schema is read on demand, not announced.** The red
`st.error` above the uploader ("*X* expects: a, b") is gone; a **Show schema** button sits beside
Update chat type (disabled, not hidden, on `— New chat type —` so the bar keeps its shape) and opens
`_dialog_chat_type_schema` with the tables, each column's type, the links and the column meanings.
It goes through the `DIALOGS` registry rather than being called from the bar, because the bar draws
*above* `st.file_uploader` and a run ending before that widget exists drops the uploaded files.
`matching.type_label` is a public standalone-form sibling of `_type_label`, whose "a Number" reads
as a mistake in a table cell.

Known pre-existing failures, not from this stage (they fail with Stage 9's page changes
stashed): `test_chat_with_data_page.py::TestSteps::test_a_collapsed_step_does_not_run_its_body`
and `TestRelationships::test_confirming_a_bad_link_keeps_it_declared_but_unenforced`.

Stage 10 (previous): Meeting Chatbot, Phase 1 (requirement 6.7, written up from
`docs/projectAI ChatDynamic.md`) — complete. Task Builder is deferred; this was taken up
ahead of it at the user's request. The new `meetings/` package plus `app_pages/meetings.py`
(registered) and `app_pages/meeting_invitee.py` (**not** registered — rendered by direct
call, as `checks_view.py` is) add a second, parallel product to the app: an employee creates
a subject-based meeting with a persona, context, SOP and agenda, and each invitee gets a
private token link to chat with that persona and close it into a point-wise MoM.

Phase 1 is **discussion agenda items only**. Table items (spec 3a), evaluation fields (3b)
and every cross-invitee comparison matrix are later phases. `AgendaItem` stores its `type`
regardless, and `agenda_from_json` drops anything that isn't `"discussion"` (Phase 2 reverses
that drop — see below), so those arrive as a new table rather than a migration of every
`agenda_json` already written.

Two load-bearing decisions. **The MoM is built from the full stored transcript, never from
`running_summary`.** That rolling fold exists only to keep a live conversation inside a
context window; it is lossy and compounds, so a permanent record built on it would quietly
degrade the longer the conversation ran. The mechanism is the signature:
`summary_agent.generate_summary` takes a `list[ChatMessage]`, so no argument on it could
carry a summary instead — and `test_meetings_summary_agent.py::TestFullHistoryIsTheSource`
exists to keep it that way. And **there is still no SMTP anywhere.** The Share tab prints
each invitee's link and 6-digit code in `st.code()` blocks for the creator to send
themselves; `checks/actions.py`'s "nothing here sends anything" philosophy is unchanged, and
whether to break it was left open rather than decided by this stage.

The invitee side has **no user_id to scope by** — the token is the whole identity — so
`meetings/db.py` keeps the two families of query textually apart, and `resolve_token`
*returns* the meeting id rather than accepting one: the `m=` in the URL is a readability
convenience, and editing it by hand must not open a different meeting. `streamlit_app.py`
answers an invitee link after `bootstrap_database()` but **before** `is_authenticated()`,
and `invitee_route_params` lives in `meetings/session.py` rather than inline so that routing
decision is testable without booting the app; anything malformed returns None and falls
through to the ordinary login page.

Access codes are **encrypted, not hashed** — the one deliberate departure from how `auth/`
treats a password, because the creator's Share screen has to show the code again. It reuses
`llm.crypto`'s Fernet key via two aliases. The lockout (5 attempts, 15 minutes) is per
*invitee row*, since the 24-byte token is not worth attacking and the real threat is someone
with a forwarded link guessing six digits. An unreadable `locked_until` fails **open**:
failing closed would strand a legitimate invitee with no appeal.

Table names are all `meeting_`-prefixed (`meeting_sessions`, `meeting_messages`,
`meeting_contacts`…) because `sessions`/`messages`/`contacts`/`files` are too generic to
claim in a database four other domains already share. `BASE_URL` reads an env var, not
`st.secrets`, which raises outright when no secrets file exists.

Stage 10 (previous): Meeting Chatbot, Phase 2 — table agenda items (spec 3a), evaluation
fields (3b) and the cross-invitee comparison matrices they feed (spec 8) — complete. Three
new modules (`meetings/tables.py`, `meetings/extraction_agent.py`, `meetings/matrix.py`),
four new tables (`meeting_agenda_tables`, `meeting_table_responses`,
`meeting_evaluation_fields`, `meeting_evaluation_results`), and Phase 1's two pages grown
rather than replaced. `SCHEMA_VERSION` stays 1 and `agenda_json` is not migrated: Phase 1
already wrote `type` on every item, so **Phase 2's change is `agenda_from_json` no longer
dropping `"table"`** — and an unknown type is now read as a discussion item with a warning,
because losing an agenda line silently is worse than mis-typing it.

An agenda table's join key is `item_ref`, the agenda item's **title**, not an id — agenda
items live inside a JSON blob with no stable identity of their own, and the title is what
`agenda_tag`, `coverage()` and the MoM already group on. One `AgendaTable` belongs to the
*meeting*, which is how spec 3a's "every invitee gets the identical template" holds
**structurally**: there is no per-invitee template that could diverge, so nothing needs to
check it at runtime. Replacing a table deletes the responses to the old one — the row
positions they were keyed to no longer mean anything.

Completion is a `COUNT(*)`, made honest by a DB-enforced invariant: **a stored response row
is a filled row.** `save_table_responses` filters blanks itself rather than trusting callers,
so `table_progress` is one query. That figure is then written **over** the model in
`summary_agent._with_every_item` — a table item's MoM entry is the stored count, never
whatever the model guessed, which is how a wrong number gets into a permanent record. The
wording ("12 of 40 row(s) filled (30%)") lives once, in `tables.format_completion`, read by
the status list, the MoM and the matrix alike. `coverage()` now walks
`meeting.discussion_items()` only: a grid is measured in rows filled, so a passing mention of
it must not read as having filled it.

`meetings/tables.py` reads a source file **entirely as text** (`dtype=str,
keep_default_na=False`) — a bill number in a column with one blank row otherwise comes back
as `1001.0`. `replace_evaluation_fields` **updates in place by `field_id`** and deletes only
what was actually removed, because a delete-all-and-reinsert would cascade every extracted
answer away when the creator fixes a typo in a question. Extraction runs **after**
`close_session` and swallows its own failures: the MoM is the thing that must survive a
provider outage, and a missed extraction is re-runnable from the creator's page
("Re-extract answers"). `extract_answers` returns `[]` with **no provider call** when no
fields are defined, so the common case costs nothing. A bucket is normalised to the
creator's spelling and an invented one is dropped while the raw answer survives — the matrix
groups on that string, so "high" beside "High" would silently become two columns of one.

Two page-shape decisions. The invitee page builds `st.tabs` **only when the meeting has table
items**, so a discussion-only meeting keeps Phase 1's bottom-pinned `st.chat_input` byte for
byte (inside tabs Streamlit renders it inline, which is legal but different). And the
matrices distinguish `NOT_STARTED` (`—`) from `NOT_DISCUSSED` — they look alike in a grid and
mean opposite things, and collapsing them would indict someone who simply never opened their
link. The creator page's detail is now six tabs, `Comparisons` holding the three sub-tabs;
`_handle_save_setup` is the first caller of `db.update_meeting`.

Deferred deliberately: nothing in Phase 2 sends anything either, and the invitee-side queries
still take no `user_id`.

Out of band, since: **Step 1 is only on Setup.** The upload step used to render in all three
views of the Chat page — a step header (and, whenever it was open, a whole upload panel) above
every question the user asked. It now renders on Setup alone. What could *not* move is the
`st.file_uploader` itself: a widget that stops being rendered stops reporting its value, and
`sync_tables` would drop every loaded table on the next run. So the three ungated calls came out
of the expander body into `_mount_upload` (uploader → `_sync_selection` → `_check_upload`, an
order that is load-bearing), which every view calls; off Setup it runs inside
`st.container(key=HIDDEN_UPLOAD_MOUNT)` hidden by the page's one `st.html` stylesheet — **that
container is not dead UI**, and deleting it loses the user's data the first time they switch
views. The check still *acts* everywhere; only its reporting is Setup's.

The two blocked views therefore can no longer say "fix the problems listed in Step 1" — it would
name something off screen — so `_render_mismatch_gate` lists `report.problems()` itself, keeping
each view's reason clause. Chat also gained the no-data message it never had (it rendered
*nothing*, a blank screen, where Checks already spoke). All three messages carry one **Go to
Setup** button, which queues `PENDING_VIEW_KEY` rather than writing `de_view`: the toggle is a
widget created far above the button, and Streamlit forbids writing a widget's own key once it
exists — the same deferral `session.queue_step_state` makes for the steps.
`_open_step_one_on_problems` deliberately still does not switch views: a discarded extra file
blocks nothing, and must not yank someone out of a chat mid-question.

Stage 11 (previous): Task Builder (requirement 7, rewritten from the user's own shaping of
§7.1) — build-order item 9 — complete. A new page, `app_pages/task_builder.py`, registered
under an **Automate** section for `admin`/`superuser` only and carrying its own
`require_role` guard. Four views on one control: **Setup | Report-Items | Checks | Report**.
New packages `report_items/` and `tasks/`; two page modules extracted so the new page could
reuse them, `app_pages/setup_view.py` and `app_pages/report_view.py`.

The user's shaping, all of it deliberate: **no chat type picker** (a Task *is* the saved
setup, so a second saved-setup concept on one screen would be two answers to one question);
**no criteria Save set / Load set**; **no Actions tab**; **no chat view** — Report-Items
replaces it, cards rather than a conversation, because a transcript has no order that can be
replayed and requirement 8.2 replays this list top to bottom. **Pin to dashboard becomes Pin
to report.** And §7.4's *"capture the Data Cleaner steps"* was **removed from
`docs/requirements.md`**, not deferred — the user cleans their files and uploads the result,
so there is no cleaning sequence to record or for §8.2 step 1 to replay; that step is gone
and the rest renumbered.

Three decisions carry the stage.

**A page module cannot be imported** — `chat_with_data.py` and `dashboard.py` are `st.Page`
scripts whose body runs on import — so Steps 1–3 became `setup_view.py` and the
Build/Preview/Download workspace became `report_view.py`, both plain modules the pages call,
as `checks_view.py` already related to `chat_with_data.py`. Pure refactors: **every widget
key is byte-identical** (`de_*`, `db_*`), which is why the existing page tests passing
unchanged *is* the check on them. `setup_view` knows nothing about chat types (the caller
passes `declared_types` and owns the match report) and `report_view` knows nothing about
which page feeds it (the caller passes an `EmptyPool`).

**There are two reports now, so the report had to be addressable.** `dashboard/session.py`
gained `DB_ACTIVE_REPORT_KEY`, `use_report(key)` and `active_report_key()`; `get_report` /
`set_report` / `reset_dashboard` read it, and `pin_result` / `unpin_source` /
`find_item_by_source` are untouched — which is exactly what let the Checks view be reused
with no edits to its pinning code. Departing from the plan, **every page states which report
it wants, including the ones that want the default**: the stored key outlives the run that
set it, so a silent Chat or Dashboard page would inherit `tb_report` after a visit to Task
Builder and quietly pin a chat answer into a Task.

**A saved report is a skeleton, never a snapshot** — `dashboard/skeleton.py`, structure only:
title, sections, subsections, headings, comments, ordering, `column_with_previous` and
`source_id`. The rule is structural, not remembered: `to_json` builds fresh dicts of named
scalars, so no frame or figure has a path to travel. `from_json` returns the tree with items
*empty*, which is what §8.2 step 4 fills back in by `source_id`. The pool is deliberately not
saved — an unplaced item is by definition not in the report.

`report_items/` mirrors `checks/` (only `session.py` imports Streamlit). One `ReportItem`
with a **kind**: a `report` item asks a question and produces table/chart/comment, a `column`
step changes the data every item *below* it then sees. One dataclass rather than two because
the two kinds are *positions in one sequence*. **Only the last column step may be deleted** —
everything under one was written against the columns it added, and the failure would surface
next month, in a report, rather than at the click. A column step goes through
`analyst/column_intent.py` + `engine/columns.py`, never free-form SQL, so the recorded
statement list is identical to the one Chat's conversational path writes and §8.2 can replay
either.

`report_items/sql_builder.py` copies `checks/sql_builder.py`'s shape — one narrow
`run_structured` call, one automatic repair, then the error becomes the refine input — with
one deliberate difference: **no output contract.** A criteria must return `criteria_result`
and `criteria_met`; "total payroll cost" is one cell, and demanding a verdict column would
invent a question the user didn't ask. That gap is real and was found here:
`engine.guards.assert_safe_sql` blocks what could escape the session (`ATTACH`, `COPY`,
`DROP TABLE`, filesystem functions) but **not `DELETE FROM salary`** — `checks/` never
noticed because its contract rejects a row count incidentally. So `assert_read_only` refuses
anything that isn't a single `SELECT`/`WITH`, skipping leading comments first, and names the
keyword while pointing at column steps as the fix.

`render_checks` gained three keyword options, all defaulting to the Chat page's behaviour
exactly as it was: `show_set_bar`, `show_actions`, `persona`, plus a `report_hint` because
"goes to the Dashboard" is not true on both pages. With Actions off there is only Design, so
**no `st.tabs` wrapper is built at all**. The caller's persona is **written onto the set**
rather than threaded through every function below it — the Task's persona genuinely *is* that
set's persona, and one field means no second place for the two to disagree.

`tasks/model.py` **assembles rather than restates**: `chat_types.model.capture` for the
schema signature (that is the format `chat_types.matching` already knows how to measure an
upload against), `engine.session.get_statements()` for the calculated columns,
`report_items.model.to_json`, `checks.model.to_json` **unchanged** as its docstring promised,
and `dashboard.skeleton`. The sub-formats nest as **objects, not escaped strings** —
`json.loads` on their own output — so each format still has exactly one owner and the stored
Task stays readable. `tasks/db.py` follows `checks/db.py`; one name per account, and saving
under a taken name updates that row.

Two page-shape subtleties. **`load_task` queues the name, description and persona rather
than writing them** (`TB_PENDING_FIELDS_KEY`, applied by `consume_pending_fields` at the top
of the next run): the Open dialog is drawn below the task bar, and Streamlit forbids writing
a live widget's own key — the same deferral `queue_step_state` and `PENDING_VIEW_KEY` make.
And **loading a Task restores the recipe, not the session**: it does *not* apply the saved
schema to what is loaded or replay the column steps. That is §8's Run a Task, and doing half
of it here would leave the session in a state neither stage owns.

The persona box and the description sit at the **foot of Setup**, not in the task bar and not
in either view — the bar is pressed on every save while these are written once, and *both*
Report-Items and Checks read the persona, so it belongs to neither of them.

Out of band, since (four fixes from using the page): a **column step gets the hint pickers**
every other AI-driven input has, fed to the model through a new `column_hint` keyword on
`analyst.column_intent.handle_message`/`parse_request`, worded as `checks/sql_builder` words
it so a wrong guess is corrected rather than obeyed. `ColumnAction.explanation` **stops being
discarded**: it rides out on `ColumnChange.explanation` into a new round-tripped
`ReportItem.description`, a caption on the card, and — the part that pays off past Setup's
grid — the data dictionary, where `_describe_new_columns` fills only the columns the step
created and never overwrites typed text, so `schema_context()` explains the column to every
item below. A toast says where the data went and **View data** opens Chat's "Current data"
dialog on the card. And a criteria's result gained a **column picker**: `sql_builder` selects
the whole source row now, `GeneratedSql.identity_columns` (returned all along, discarded all
along) seeds `Check.display_columns` so an untouched criteria's screen is unchanged, and only
`st.dataframe` and `pin_result` see the projection — `freeze_run`, the pass/fail headline,
the remarks and the action drafts keep reading the full frame.

Out of band, since: **Task Builder asks which task first.** The page used to open onto an
empty name box, a Save button and four views, leaving "am I editing something saved or
building something new?" to be inferred from the controls — and since `tasks.db.save_task`
reads a name already in use as *update that row*, typing a different name into the box looked
like the way to start a second Task when it was the way to overwrite another one. Now
`tasks_session.is_task_open()` gates the page: until a Task is chosen it draws
`_render_task_gate` and `st.stop()`s — saved Tasks as cards on the left (Open / Delete), a
name and **Create task** on the right — and the `load_task` dialog is gone, replaced by that
list. The **upload widgets are mounted on the gate too**, in the same hidden container the
non-Setup views use, for the same reason: a trip back to the gate to start a second Task
against the same files must not arrive with the files gone.

Delete moved from the old dropdown into a **confirm dialog**: on a card, Open and Delete are
the same motion. It is the one dialog the gate resolves, through `_render_pending_task_dialog`
rather than `_render_pending_dialog` — the setup steps aren't on screen there, so the engine's
registry has nothing to say.

The name is then a **heading, not a widget** — `TB_NAME_KEY` is plain session state, changed
only through the Rename dialog, which refuses a name belonging to another Task (`_name_taken`,
checked on Create as well). Save means write *this* Task. **Switch task** returns to the gate,
asking first when there is unsaved work.

"Unsaved" is `tasks.model.recipe_fingerprint`, deliberately **not** `to_json`: a Task's schema
and calculated columns are read from the live session while opening one restores only the
recipe, so fingerprinting those would report every freshly opened Task as edited. It covers
name, description, persona, report items, criteria and report structure — and
`session._recipe_only_task` assembles exactly those rather than calling `capture_task`, whose
schema build queries DuckDB once a run to word a caption. A fingerprint that won't build is
dropped, so the Task reads as changed, which is the safe way round. Start over closes the
Task as well, since it clears the name and persona anyway.

Out of band, since (a ghost the gate made visible): **the page's top level is four children in
every state, and the ones that vary are held in fixed slots.** Two "Step 1 · Your data" headers
could appear at once — one of them stale — and one survived onto the gate. Streamlit addresses
an element by its position among its parent's children and only clears the previous run's
leftovers when a run finishes *without* asking for another (`FINISHED_EARLY_FOR_RERUN` keeps
them, deliberately, so reruns don't flicker) — and this page both moves things about and reruns
constantly: the flash message is a top-level element on the runs that have something to say,
the gate replaces half the page, and the upload load-check ends its run with
`st.rerun(scope="app")` whenever the table set changed. So `FLASH_SLOT` is a container that is
always there and usually empty, `GATE_BODY` and `TASK_BODY` give the two screens one shared
position, `UPLOAD_SLOT` holds whichever of the Step 1 expander / hidden mount the view calls
for, and the stylesheet is emitted on every run instead of only the runs that hide something.
None of it is styling, and
`TestTheShape::test_the_page_keeps_the_same_top_level_shape_in_every_state` is what says so —
it compares the top-level element types across gate, flash, settled, off-Setup and back-at-gate
and fails if any one of them differs. `chat_with_data.py` got the same `UPLOAD_SLOT` and the
same always-on stylesheet: it swaps expander for hidden container at one position exactly as
this page does, and had the same latent ghost.

Known pre-existing failures, unchanged by this stage:
`test_chat_with_data_page.py::TestSteps::test_a_collapsed_step_does_not_run_its_body` and
`TestRelationships::test_confirming_a_bad_link_keeps_it_declared_but_unenforced`.

Stage 12 (current): Run a Task (requirement 8) — build-order item 10 — complete. A new page,
`app_pages/run_task.py`, in the **Automate** section and open to **any logged-in user** (Task
Builder is the admin half of the pair); the section is now registered for everyone and Task
builder appended to it for `admin`/`superuser`. One new package, `runner/`, mirroring
`checks/` and `report_items/` — only `session.py` imports Streamlit.

**The stored SQL is the path; the model is the fallback.** Every report item and criteria comes
back from storage carrying the statement that produced it, so producing the numbers costs **no
provider call**. Only when a stored statement actually fails does `generate_and_run` get a turn,
and the step is recorded as `fallback` — which is the distinction §8.2 step 5 asks to be
reported, and it only exists because it is real. `tests/test_runner_replay.py`'s autouse fixture
replaces every `run_structured` seam with one that **fails the test if called**, which is what
keeps that claim true. A **column step never falls back**: regenerating a column definition
would silently change every figure below it while the report looked fine.

Three more decisions carry the stage.

**Order is the content of the list.** Report items are replayed in list order with each column
step's own `statements` executed in place, *not* all of `task.calculated_columns` up front.
The two are nearly the same and differ in exactly one case that matters — a step that updates a
column an earlier item already read. `replay.unowned_statements` catches the leftovers (a
statement the engine's session-wide list holds that no column step owns) and applies them before
the list, because a recorded statement silently not replayed is a column every later item is
missing.

**A remap is a rename applied while the file is read, never after** — `loading.rename_columns`,
threaded through two new keyword maps on `engine.session.sync_tables` (`table_names`, keyed by
`table_id` since the slug is what the caller is replacing; `column_renames`, keyed by table
name) and through `setup_view.mount_upload`. Renaming a loaded table's column instead would
leave it typed by detection under a name the recipe declares a type for — §6.6's
wrong-rows-no-error failure. Changing a mapping therefore goes through
`reload_after_mapping` → `engine.session.reload_uploaded_tables`, the mechanism
`chat_types.session.select` already uses. **Table remapping is in scope** though §8.1 step 5's
letter names only columns: a table's name comes from its filename, so a renamed file is the
first mismatch every user meets. A mapping that *works* takes its own dropdown off the screen,
so `_render_active_mapping` shows what is in force with one button to clear it.

**A run never touches the recipe.** The `Task` read from SQLite is held pristine; the replay
works on `copy.deepcopy` of it (a fallback rewrites an item's SQL) and on a deep copy of the
report skeleton, so running twice produces the same report rather than a doubled one. Results
are written into the skeleton by `source_id` through `dashboard.model.find_item_by_source` — no
new mechanism, which is exactly what `pin_result`'s idempotency was built for. **A failed item
keeps its place** carrying "Not produced in this run — …": a designed report with a point
silently missing is worse than one that admits it.

Two things had to move to keep `runner/` free of Streamlit, both pure refactors with no
behaviour change: `SOURCE_PREFIX` / `SUMMARY_SOURCE_ID` / `source_id_for` /
`actions_source_id_for` from `checks/session.py` into `checks/model.py` (re-exported, so every
caller is unchanged), and `FILTER_SUFFIXES` from `app_pages/checks_view.py` into
`checks/model.py` as `report_heading` / **`mode_from_heading`**. That second one is load-bearing:
a `Check` never stored which of All / Failures / Passes was on screen when it was pinned, and the
**heading is the record of it every saved Task already carries** — pinning every row into an item
headed "breaches only" would put the passing records into a report that says it holds the
failures. `checks.model.project_columns` came out of the view at the same time and takes the
picker's list explicitly, because an emptied selection ("verdict columns only") and an unset one
("saved before the picker existed — show everything") mean opposite things.

Redrafting the commentary is the one provider call a run makes **by design** (§8.2 step 4's
persona), so it is a checkbox beside Run rather than an assumption; declining it keeps the
wording saved with the Task. `report_view.render_report_output` is Preview + Download with **no
Build view** — a run's arrangement came from the Task and the next press of Run replaces it, so
there is nothing here to file items into. `engine.session.set_statements` exists for one reason:
`relationships.enforce` replays the statement list on every rebuild, so a second run would apply
the first run's calculated columns twice and fail on an `ALTER TABLE ADD` of a column that
already exists.

Two page-shape notes. `run_task.py` reuses `task_builder.py`'s hard-won arrangement —
`FLASH_SLOT`, `PICKER_BODY` / `RUN_BODY`, `UPLOAD_SLOT`, and the stylesheet emitted on every run
— and **mounts the upload widgets on the picker too**, hidden, so going back to run a second
Task against the same files doesn't arrive with the files gone. And a run **rewrites this
session's setup**: `apply_recorded_setup` clears the relationships and the statement list before
handing off to `chat_types.session.apply_setup`, so the Task's links and column meanings replace
whatever was there. The panel says so above the button.

Known pre-existing failures, unchanged by this stage:
`test_chat_with_data_page.py::TestSteps::test_a_collapsed_step_does_not_run_its_body` and
`TestRelationships::test_confirming_a_bad_link_keeps_it_declared_but_unenforced`.

Next: build-order item 11 onwards. Requirement 7.4's **Excel export already exists**
(`dashboard/excel_export.py`), and requirement 9 keeps scheduling, shared Task ownership and
external identity providers out of scope.

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