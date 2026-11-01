# Phase 72 — Remove a reference document

**Status: done.**

## What changes

**1. Remove button** next to every Reference document in meeting setup, uploaded files and
links alike, e.g. `CaCl2_Tech_Grade.pdf` or `Product demo` (YouTube).

**2. Asks first.** Remove opens a small box: "Remove CaCl2_Tech_Grade.pdf? Invitees won't see
it any more." with a red **Yes, remove** button. Nothing happens until that is clicked.

**3. Fully removed.** The database row goes, and an uploaded file is deleted from
`data/meeting_storage/<meeting_id>/ref_docs/` too. A file already missing from disk ("no longer
on disk") still removes cleanly, no error.

**4. Agenda still using it** (Show document = that name): removal is allowed; the existing red
"Note — …" under the agenda shows which step to fix.

**5. Works any time**, even after invitees have started. Old chat messages that showed the
file already say "This file is no longer available."

**6. Not in this phase:** files invitees attached in their chats are left alone.

Code: `meetings/db.py` (`delete_ref_file`, owner-only, reference documents only),
`meetings/storage.py` (`delete_file`, missing file is fine), `app_pages/meetings.py` (button +
confirm popover).

## Tests (`tests/test_meetings_phase72.py`)

- Removing an uploaded file deletes the row and the file on disk.
- Removing a link deletes the row.
- A file already missing on disk still removes without error.
- Another user can't remove it; an invitee's own upload can't be removed this way.
- Organiser page: Remove → Yes, remove takes it off the list.
