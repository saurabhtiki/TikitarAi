# Phase 71 — "Accept only if" column on agenda rows

**Status: done.**

## What changes

**1. New "Accept only if" column** on Question and Discussion rows. The organiser writes in
plain words what a good answer looks like. Empty = the row works exactly as today.

| Item | Type | Answer | Accept only if |
|---|---|---|---|
| Company Demo | Question | Yes/No | Yes |
| Introduction | Question | Text | describes their company: what it does and where |
| Pricing | Discussion | | invitee has agreed a demo date |

**2. Questions (every answer type).** After the usual Rule check, the AI checks the reply
against "Accept only if".
- Fits → the answer is saved and the meeting moves on (Go to works as usual).
- Doesn't fit → the AI says what is missing and asks again. **No limit**: Tries is ignored
  on that row, the step never ends as "not answered".

**3. Discussions.** The step ends only when "Accept only if" is met. "Nothing more to add" or
"let's move on" doesn't end it, and the 5-reply limit is off for that row.

**4. Close chat stays off** while the invitee is on such a step (the step is still open, so
the existing "steps left" check already holds it). Closing the browser tab and coming back
resumes at the same step.

**5. Organiser results** show the invitee as stuck: "stuck at Introduction" with their last
reply.

**6. Help** (`static/help/meetings.html`) explains the column with these examples.

Code: `meetings/model.py` (`accept_if` on `AgendaItem`, saved in the agenda JSON — no new
table), `meetings/chat_agent.py` (answer/discussion readers judge `accept_if`),
`meetings/steps.py` (no tries limit / no reply limit when set), `app_pages/meetings.py`
(column, results), `app_pages/meeting_invitee.py`.

## Tests (`tests/test_meetings_phase71.py`)

- The column saves and loads; an old agenda without it still reads.
- A question with Accept only if: a refused reply asks again and never runs out of tries.
- A question with Accept only if: an accepted reply is saved and moves on.
- A discussion with Accept only if: not met → stays, past 5 replies; met → moves on.
- Close chat stays off while stuck.
- Organiser results show "stuck at …" with the last reply.
