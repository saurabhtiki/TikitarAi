# Phase 55 — Meetings: add an invitee later, FAQ template download

**Status: done.**

## The problem

1. Invitees can only be added in the New meeting window. If the organiser forgets someone,
   they have to make the whole meeting again.
2. The FAQ upload expects a Question | Answer sheet, but there is no sample to start from.

## What changes

**1. Add invitee (Invitees & status tab).** A small form at the top: **Name**, **Email**,
**Add invitee** button. It makes a new link and access code (shown in the Share tab), and
remembers the contact, just like the New meeting window.
- Email is required and must look like an email (`raj@x.com`).
- The same email twice on one meeting is refused: "raj@x.com is already invited."
- A blank name uses the saved contact's name, else the email.
- Works even when the meeting has no invitees yet.

**2. Download FAQ template (Overview → FAQ box).** A **Download template** button gives an
Excel file with the two columns the upload expects and two sample rows, e.g.
`What is the notice period? | 60 days.` The organiser fills it in and uploads it back.

No new tables. Code: `meetings/faq.py` (`template_frame`), `app_pages/meetings.py`.

## Tests

- Adding an invitee saves them with a working code; a duplicate email and a bad email are
  refused; a meeting with no invitees still shows the form.
- The template has the Question and Answer columns, reads back through the FAQ upload, and
  the button is shown in the FAQ box.
