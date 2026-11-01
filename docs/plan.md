# Phase 70 — Show document and Fill table columns on agenda rows

**Status: done.**

## What changes

**1. New "Show document" column.** On a Question or Discussion row, pick one of the meeting's
Reference documents. When the chat reaches that step, the document appears under the bot's
reply (once), and the bot introduces it in one short line. The AI note is just instructions now.

| Item | Type | Show document | Note |
|---|---|---|---|
| Company Introduction | Discussion | Company Info | Ask what they think of us |

- In the setup editor (after creating) it is a dropdown of the Reference documents. In New
  meeting there are no documents yet, so you type the name; add the document in Overview after.
- A name that is not in Reference documents gets a red note (e.g. after a rename or delete).
- Old notes that name a file ("Show Company Info, then…") keep working.

**2. New "Fill table" column (mandatory).** On a Question or Discussion row, name a Table row
of the same agenda. When the chat reaches that step:

1. The bot asks the invitee to fill in that tab and press **Save progress**. A red line above
   the chat box says the same. Nothing moves on until it is filled — the bot only answers side
   questions.
2. Saving the filled table posts "I have filled in the Price list table." in the chat, and the
   step starts: the question is asked, or the discussion begins. The bot sees what was filled
   in (up to 50 rows), so it can say "Item 3's rate went up — why?".
3. **Filled** = every row has a value in every **Required** column (if no column is Required,
   in every column the invitee fills in). A value already in the uploaded sheet counts.
4. **Close chat** also waits for any table still to be filled on the invitee's path.

| Item | Type | Fill table | Note |
|---|---|---|---|
| Price list | Table | | |
| Rates | Discussion | Price list | Ask about any rate that changed a lot |

- Fill table must name a Table row; it can't be used on a For each question.
- A Table row with no data uploaded yet doesn't block anyone (red note for the organiser).

**2b. A Show document belongs to its row.** A document picked in a row's Show document cell is
shown only at that row — never at another step because its note names it or the bot picks it.
Example: About Us asks "Like to see video for company Intro?" (Yes → Company Video, No → go to
Introduction); the video plays only at Company Video. The bot is told which step shows it.

**3. Flow picture** shows "shows Company Info" / "fill Price list" in the step's box.

**4. Help** (`static/help/meetings.html`) explains both columns with these examples.

Code: `meetings/model.py` (`show_file`, `fill_table` on `AgendaItem`, saved in the agenda JSON —
no new table), `meetings/steps.py` (checks, `tables_left`), `meetings/tables.py` (`is_filled`,
`filled_text`), `meetings/chat_agent.py` (`fill_table_guidance`), `meetings/show_files.py`
(`missing_documents`), `meetings/flow.py`, `app_pages/meetings.py`, `app_pages/meeting_invitee.py`.

## Tests (`tests/test_meetings_phase70.py`)

- The two columns save and load (agenda JSON), and an old agenda without them still reads.
- Fill table naming a non-Table row, or on a For each question, is refused.
- `is_filled`: Required columns only; sheet values count; no Required = all fill-in columns.
- The step's Show document is shown when the step starts, once; a missing name warns.
- Invitee page: an unfilled table holds the step (no answer read); saving it starts the step;
  Close chat stays disabled while a table is left.
- Flow picture labels.
