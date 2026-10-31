"""Voice in the invitee chat (phase 64): 🎤 Speak and 🔊 Listen; phase 65 reads the newest
reply aloud by itself, behind a "Read replies aloud" switch.

Both use the browser's own voice features (Web Speech API), so nothing runs on the server
and nothing costs money — it works the same on Streamlit Cloud as on a laptop. Browsers
without them (Speak: Firefox) show a short note or no button; typing always works.

Two details worth knowing before changing this:

* Speak never sends a message. What it hears is put into the chat box's state by a callback
  — callbacks run before the script body, so the box is filled before it is drawn — and the
  invitee checks it and presses Enter as usual.
* The components are Custom Components v2 (rendered in the page, not an iframe), which is
  what lets them reach the microphone. They take no `help` argument, so the tooltip is the
  button's own `title`.
"""

import logging
import re
from functools import partial

import streamlit as st
from streamlit.errors import StreamlitAPIException

logger = logging.getLogger(__name__)

UNSUPPORTED_NOTE = "Voice typing isn't available in this browser — please type."

_BUTTON_CSS = """
button {
  font: inherit;
  font-size: 0.875rem;
  color: var(--st-text-color);
  background: var(--st-secondary-background-color);
  border: 1px solid var(--st-border-color, rgba(128, 128, 128, 0.3));
  border-radius: var(--st-base-radius, 0.5rem);
  padding: 0.25rem 0.75rem;
  cursor: pointer;
}
button:hover { border-color: var(--st-primary-color); color: var(--st-primary-color); }
.note { margin-left: 0.5rem; font-size: 0.875rem; color: #d33; }
"""

_SPEAK_JS = """
export default function (component) {
  const { data, parentElement, setTriggerValue } = component
  const button = parentElement.querySelector("#speak")
  const note = parentElement.querySelector("#note")
  if (!button || !note) return

  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition
  if (!Recognition) {
    button.hidden = true
    note.textContent = data.unsupported
    return
  }

  const reset = () => {
    parentElement.recognition = null
    button.textContent = "🎤 Speak"
  }

  button.onclick = () => {
    if (parentElement.recognition) {
      parentElement.recognition.stop()
      return
    }
    // Stop the bot reading aloud, or the mic would hear it.
    if (window.speechSynthesis) window.speechSynthesis.cancel()
    const recognition = new Recognition()
    recognition.lang = navigator.language || "en-US"
    recognition.interimResults = true
    recognition.continuous = false
    let heard = ""

    recognition.onresult = (event) => {
      let interim = ""
      heard = ""
      for (const result of event.results) {
        if (result.isFinal) heard += result[0].transcript
        else interim += result[0].transcript
      }
      note.textContent = (heard + interim) || "Listening…"
    }
    recognition.onerror = (event) => {
      heard = ""
      note.textContent = event.error === "not-allowed" || event.error === "service-not-allowed"
        ? "Microphone is blocked — allow it in your browser's address bar, or type."
        : event.error === "no-speech"
          ? "Didn't hear anything — tap 🎤 Speak and try again."
          : "Voice typing stopped (" + event.error + "). Please try again or type."
    }
    recognition.onend = () => {
      reset()
      if (heard.trim()) {
        note.textContent = "Check the text in the chat box, then press Enter."
        setTriggerValue("heard", heard.trim())
      }
    }

    parentElement.recognition = recognition
    button.textContent = "⏹ Stop"
    note.textContent = "Listening…"
    recognition.start()
  }

  return () => {
    if (parentElement.recognition) parentElement.recognition.abort()
  }
}
"""

_LISTEN_JS = """
// Replies already read out by themselves, by id. Kept on `window` so it outlives Streamlit's
// reruns (the same page) but not a full reload, which reads the newest reply once more.
const spokenReplies = (window.inviteeSpokenReplies ??= new Set())

export default function (component) {
  const { data, parentElement } = component
  const button = parentElement.querySelector("#listen")
  if (!button) return
  if (!window.speechSynthesis) {
    button.hidden = true
    return
  }

  const done = () => {
    parentElement.speaking = false
    parentElement.autoStarted = false
    button.textContent = "🔊"
  }
  const speak = () => {
    window.speechSynthesis.cancel()
    const utterance = new SpeechSynthesisUtterance(data.text)
    utterance.lang = navigator.language || "en-US"
    utterance.onend = done
    utterance.onerror = (event) => {
      done()
      // Blocked until the person has clicked or typed on the page: try again at their first one.
      if (event.error === "not-allowed" && data.auto) waitForTap()
    }
    parentElement.speaking = true
    button.textContent = "⏹"
    window.speechSynthesis.speak(utterance)
  }
  const onFirstTap = (event) => {
    stopWaiting()
    // A first tap on this 🔊 itself is left to its own click, which plays the reply.
    if (data.auto && !event.composedPath().includes(button)) speak()
  }
  const waitForTap = () => {
    document.addEventListener("pointerdown", onFirstTap, { once: true, capture: true })
    document.addEventListener("keydown", onFirstTap, { once: true, capture: true })
  }
  const stopWaiting = () => {
    document.removeEventListener("pointerdown", onFirstTap, { capture: true })
    document.removeEventListener("keydown", onFirstTap, { capture: true })
  }

  button.onclick = () => {
    if (parentElement.speaking) {
      window.speechSynthesis.cancel()
      done()
      return
    }
    speak()
  }

  if (!data.auto && parentElement.speaking && parentElement.autoStarted) {
    // The switch was turned off mid-reply.
    window.speechSynthesis.cancel()
    done()
  }
  if (data.auto && data.id && !spokenReplies.has(data.id)) {
    spokenReplies.add(data.id)
    if (navigator.userActivation && !navigator.userActivation.hasBeenActive) waitForTap()
    else speak()
    parentElement.autoStarted = true
  }

  return () => {
    stopWaiting()
    if (parentElement.speaking) window.speechSynthesis.cancel()
  }
}
"""

_SPEAK_HTML = """
<button id="speak" type="button" title="Say your reply instead of typing it. The words go into the chat box for you to check, then press Enter.">🎤 Speak</button>
<span id="note" class="note"></span>
"""

_LISTEN_HTML = """
<button id="listen" type="button" title="Hear this reply read aloud. Tap again to stop.">🔊</button>
"""


def _speak_component():
    # Registered on every use, not once at import: the registry belongs to the running
    # Streamlit server, and a module imported under an earlier one (tests start a fresh
    # server per AppTest) would otherwise mount a name the new one has never seen.
    # Registering the same definition again is silent and cheap.
    return st.components.v2.component("invitee_voice_speak", html=_SPEAK_HTML, css=_BUTTON_CSS, js=_SPEAK_JS)


def _listen_component():
    return st.components.v2.component("invitee_voice_listen", html=_LISTEN_HTML, css=_BUTTON_CSS, js=_LISTEN_JS)


def heard_text(value) -> str:
    """What Speak heard, tidied; blank when nothing usable came back."""
    return value.strip() if isinstance(value, str) else ""


def spoken_text(text: str) -> str:
    """A reply as it should be read aloud: Markdown marks (`**`, `#`, backticks) dropped."""
    without_links = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
    return re.sub(r"[*_#`>|]+", " ", without_links).strip()


def fill_chat_box(speak_key: str, chat_box_key: str) -> None:
    """Speak's callback: copy what it heard into the chat box, before the box is drawn."""
    text = heard_text(getattr(st.session_state.get(speak_key), "heard", None))
    if text:
        st.session_state[chat_box_key] = text


def speak_button(chat_box_key: str, *, key: str) -> None:
    """🎤 Speak: puts what the invitee says into the chat box `chat_box_key`."""
    try:
        _speak_component()(
            key=key,
            data={"unsupported": UNSUPPORTED_NOTE},
            on_heard_change=partial(fill_chat_box, key, chat_box_key),
        )
    except StreamlitAPIException:
        # Voice is an extra; a broken component must never take the chat down with it.
        logger.exception("Could not show the Speak button.")


def auto_read_switch(*, key: str) -> bool:
    """The "Read replies aloud" switch (phase 65), on unless the invitee turns it off."""
    return st.toggle(
        "🔊 Read replies aloud",
        value=True,
        key=key,
        help="Each new reply is read out by your browser as soon as it appears. "
        "Turn off to read quietly; 🔊 under a reply still plays it.",
    )


def listen_button(text: str, *, key: str, reply_id: str = "", auto: bool = False) -> None:
    """🔊 Listen: reads `text` aloud in the invitee's browser.

    With `auto`, it is also read out by itself, once per `reply_id` (phase 65).
    """
    try:
        _listen_component()(key=key, data={"text": spoken_text(text), "id": reply_id, "auto": auto})
    except StreamlitAPIException:
        logger.exception("Could not show the Listen button.")
