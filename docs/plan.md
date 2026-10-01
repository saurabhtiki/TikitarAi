# Phase 61 — Meetings: templates carry their own FAQ

**Status: done.**

## What changes

**1. Every ready-made template has an FAQ.**
- Example: HR interview comes with "Is the job remote?" → "Hybrid: 3 days in the office."
- Kept in `meetings/templates.py` next to the agenda (`faq=[FaqEntry(...), ...]`).

**2. The New meeting window gets an "FAQ (optional)" table** (Question | Answer).
- Use template fills it from the template, like the agenda.
- Create meeting saves it as the meeting's FAQ (empty = no FAQ). More than 300 rows is refused
  before anything is created.

**3. Save as template keeps the FAQ.**
- From New meeting: the FAQ table. From Overview: the meeting's saved FAQ.
- Stored in a new `faq_json` column on `meeting_templates` (added to older databases on start).
- "Start from" text for your own template says how many FAQ rows it has.

Code: `meetings/templates.py`, `meetings/db.py`, `app_pages/meetings.py`.

## Tests (`tests/test_meetings_phase61.py`)

- Every built-in template has FAQ rows with both a question and an answer.
- A saved template's FAQ comes back from `list_templates`; an old table gains `faq_json`.
- Use template fills the FAQ table; Create meeting stores it as the meeting's FAQ.
- An empty FAQ table creates a meeting with no FAQ.
- Save as template from Overview keeps the meeting's FAQ.
