# Phase 65 — Read the bot's replies aloud automatically

**Status: done.**

The invitee no longer has to tap 🔊. Each new bot reply, the first greeting included,
is read out by the browser as soon as it appears. A switch turns this off.

## What changes

**1. A switch above the chat box: "🔊 Read replies aloud" (on by default).**
- On: the newest bot reply is read out once, as soon as it appears.
  Example: the invitee says "I can join Monday" and the bot's answer is read straight away.
- Off: nothing is read by itself. Turning it off while the bot is talking stops it.
- The 🔊 button under every reply stays, so any reply can be heard again.

**2. The first greeting is read too.**
- Browsers only let a page talk after the person has clicked or typed on it. The invitee
  has just typed the access code and clicked **Continue**, so the greeting can be read.
- If the browser still blocks it (for example, the page was reloaded), the reply is read
  at the invitee's first tap or key press anywhere on the page.

**3. No repeats, no talking over the mic.**
- Each reply is read only once, even when the page refreshes itself (Save progress, a tab
  change, the switch). A full page reload reads the newest reply once more.
- Tapping 🎤 Speak stops the bot talking first, so the mic doesn't hear the bot.

**4. How to use and the help page mention it.**
"Replies are read aloud — turn off 🔊 Read replies aloud above the chat box to stop this."

Code: `app_pages/voice_controls.py` (`listen_button(..., auto=True)`, `auto_read_switch`),
`app_pages/meeting_invitee.py` (switch + only the newest bot reply gets `auto`),
`meetings/guide.py`, `static/help/meetings.html`.

## Tests (`tests/test_meetings_phase65.py`)

- The switch is shown and on by default.
- Only the newest message, and only if it is from the bot, is marked to be read aloud.
- Switch off → nothing is marked.
- A closed chat reads nothing by itself.
- How to use and the help page mention the switch.
