# Phase 67 — Organiser's photo as the bot's icon, and documents shown in the chat

**Status: done.**

## What changes

**1. The bot's chat icon is the organiser's profile photo.**
The photo from Settings → Profile photo is used for every bot message. No photo (or the file
is missing) → the default bot icon.

**2. The bot can show a reference document in the chat.**
Example: the invitee says "show me your product demo" → bot "Here is our product demo:" and
the video plays right under the reply. It stays there after a reload.
- Picture, video, PDF, HTML page → shown under the reply.
- Word, Excel, anything else → the bot only says "Please open price_list.xlsx under Reference
  documents". No preview.
- The bot gets only each file's **name + description**, never what's inside (no extra tokens).
  It can only pick a file from the list; a made-up name shows nothing.
- An HTML page runs walled off from the app (a `data:` URL). A PDF uses the browser's own viewer.

**2b. Show a file when a step starts.**
Put the file name in an agenda item's note, e.g. `Show plan_layout.png, then ask what they
think`. On starting that step the bot writes one short line ("Here is the plan layout:") and the
picture appears below it. The first step can do it in the greeting. If the bot forgets, the app
still shows a file named in that step's note, once per chat (`show_files.file_for_step`).

**3. Organiser: descriptions and a video limit (Overview → Reference documents).**
- A table **File | Shown in chat | Description** + **Save descriptions**. Example:
  `demo.mp4` → "Product demo video".
- A video over **50 MB** is refused ("demo.mp4 is 80 MB — videos can be at most 50 MB"),
  and nothing from that upload is saved.
- How to use gets "Ask to see a document, e.g. 'Show me the product demo'" when a showable
  file exists. The Help page explains all of this.

Code: `meetings/show_files.py` (new), `meetings/db.py` (`meeting_files.description`,
`meeting_messages.shown_file_id`, `save_file_descriptions`), `meetings/chat_agent.py`
(`ChatTurnOutput.show_file`, file list in the prompt), `meetings/guide.py`,
`app_pages/meeting_invitee.py`, `app_pages/meetings.py`, `static/help/meetings.html`.

## Tests (`tests/test_meetings_phase67.py`)

- File kinds by extension (upper case too); Word/Excel are "other".
- Only videos over 50 MB are refused.
- The prompt file list marks (can show)/(refer only) and is left out when there are no files.
- Picking a file matches case and spaces loosely and refuses invented names.
- An old database gains the two new columns; shown file and descriptions are saved; another
  user can't change the descriptions.
- The invitee page uses the organiser's photo, shows a picture under a reply, and says
  "This file is no longer available." for a deleted file.
