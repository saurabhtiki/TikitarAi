# Phase 57 — Meetings: invitee files for the organiser, Rule vs Go to, clearer instructions, downloads, lists for everyone

**Status: done.**

## What changes

**1. Organiser sees invitee files.** In **Invitees & status**, each invitee's card lists the files
they attached, each with a download button, e.g. "Files (2): quote.pdf · bank.xlsx".

**2. Rule vs Go to.** The Rule is checked first, so a Go to can never let through an answer the
Rule refuses. Example: Rule `between 10000 and 500000`, Go to `if > 100000 go to End` — an
answer of 700000 is refused by the Rule and never reaches the Go to.
- The agenda (New meeting and Overview) now shows a red **note** (it does not block saving),
  e.g. "Expected salary: 'if > 100000' also covers numbers the Rule refuses (it only accepts
  10000 to 500000). Widen the Rule if bigger answers are fine."
- A refused number now says why, e.g. "The answer needs to be a number between 10000 and
  500000 — 700000 is too high."

**3. Clearer How to use + reference documents for the invitee.**
- How to use names each tab and what to fill, e.g. "Open the tab **📋 Price list** and fill in
  **Rate, Delivery days**, then press Save progress."
- A list says "Questions about each row of **Outstanding invoices** are asked in the chat —
  see the list on the left."
- A new **Reference documents** box on the left lets the invitee download the organiser's
  shared documents (read-only).

**4. Organiser downloads.**
- **View chat**: Download chat (.txt) for the chosen invitee, and Download all chats (Excel,
  one row per message: Invitee, Time, From, Message).
- **View summary**: Download summary (.txt).

**5. For each lists — everyone gets every row.**
- One list (pending invoices, pending tasks) is shown to **every invitee**, and its questions
  are asked for each row. Example: 3 invoices × 3 questions = 9 questions, row by row.
- **Row name column** (e.g. Invoice No): the chat says "Outstanding invoices — INV-102: row 2
  of 3, question 1 of 3", and the bot names the invoice.
- The name/email filter moves under **Advanced: only give each invitee their own rows**
  (off by default).
- Row name and the Advanced filter can be changed later with **Save list settings**, without
  re-uploading (answers are kept).
- A red warning if an invitee would get 0 rows, e.g. "s (s@x.com) gets 0 rows — these questions
  are skipped for them."
- The invitee's left panel shows the list as a table, with a **Status** column: ✓ done,
  ▶ now, blank = still to come.
- New column: `meeting_agenda_tables.label_column` (added on start-up).

Code: `meetings/steps.py` (`agenda_warnings`, clearer refusal, `progress_text` row name),
`meetings/loops.py` (`row_label`, `invitee_rows_frame`, `zero_row_invitees`),
`meetings/transcript.py` (new: chat/summary text and Excel), `meetings/db.py`,
`meetings/model.py`, `app_pages/meetings.py`, `app_pages/meeting_invitee.py`.

## Tests

- Organiser status card shows an invitee's file with a download button.
- Warning when a number Go to goes past the Rule; none when inside; refusal says too high/low.
- How to use names the table tab and its editable columns; reference documents show for the
  invitee.
- Chat text / summary text / all-chats Excel are built right; download buttons exist.
- Lists: row name shows in progress and row context; everyone gets every row by default;
  0-row warning; Save list settings keeps answers; left panel table shows ✓ / ▶.
