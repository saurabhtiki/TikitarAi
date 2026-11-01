# Phase 68 — Reference documents can be links (YouTube, Drive, web pages)

**Status: done.**

## What changes

**1. Organiser: Add a link (Overview → Reference documents).**
Next to uploading, a small form: **Name** + **Link**. Example: `Product demo` →
`https://youtu.be/abc123`. Nothing is uploaded, so big videos cost no storage or upload time.
- The link must start with `http://` or `https://`; the name is required and can't repeat a
  name already in the list ("Product demo is already in the list").
- It appears in the same list and **Description** table as uploaded files, with
  "Shown in chat" = "Yes — video" / "Yes — picture" / "Yes — opens link".

**2. Works the same way as an uploaded file.**
- The bot sees the name + description, marked (can show). "Show me the product demo" →
  the video plays under the reply.
- A YouTube link or a link ending in .mp4/.webm/.mov → video player.
- A link ending in .png/.jpg/.gif/.webp → picture.
- Anything else (Google Drive, a PDF on a website, a web page) → an **Open Product demo ↗**
  button under the reply (most sites refuse to be shown inside another page).
- An agenda note naming the link ("Show Product demo, then ask...") shows it when the step starts.
- Invitee's Reference documents box: a link is an **Open** button instead of Download.

Code: `meetings/show_files.py` (`link_kind`, `record_kind`, `shows`, `link_problems`),
`meetings/db.py` (`meeting_files.url`, `add_link`), `meetings/guide.py`,
`app_pages/meeting_invitee.py`, `app_pages/meetings.py`, `static/help/meetings.html`.

## Tests (`tests/test_meetings_phase68.py`)

- Link kinds: YouTube / youtu.be / .mp4 → video, .png → picture, Drive / web page → link.
- A bad link (`ftp://`, `javascript:`, no host), empty name or repeated name is refused.
- The prompt marks a link (can show); How to use mentions showing documents.
- An agenda note naming a link shows it once.
- An old database gains `url`; `add_link` saves it; another user can't add a link.
- The invitee page shows a YouTube link as a video and a web page as an Open button.
