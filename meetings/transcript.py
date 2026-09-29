"""Chat and summary downloads for the organiser (phase 57).

Plain text for one invitee's chat or summary, and one Excel of every invitee's chat. No
Streamlit and no SQL here.
"""

import datetime
import logging
from io import BytesIO

import pandas as pd

from meetings.exceptions import MeetingStorageError
from meetings.model import ChatMessage

logger = logging.getLogger(__name__)

CHAT_COLUMNS = ["Invitee", "Time", "From", "Message"]


def _stamp(created_at: str) -> str:
    """A stored "2026-09-29 10:15:00" as "29-09-2026 10:15"; anything else unchanged."""
    text = str(created_at or "").strip()
    try:
        return datetime.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return text


def _speaker(message: ChatMessage, invitee_name: str) -> str:
    return "AI" if message.is_from_ai() else (invitee_name or "Invitee")


def chat_text(subject: str, invitee_name: str, messages: list[ChatMessage]) -> str:
    """One invitee's chat as plain text, one message per block:

        [29-09-2026 10:15] AI: What salary do you expect?
    """
    lines = [f"Meeting: {subject}", f"Invitee: {invitee_name}", ""]
    for message in messages:
        lines.append(f"[{_stamp(message.created_at)}] {_speaker(message, invitee_name)}: {message.text}")
        lines.append("")
    return "\n".join(lines)


def summary_text(subject: str, invitee_name: str, summary) -> str:
    """One invitee's summary (a `summary_agent.MeetingSummary`) as plain text."""
    lines = [f"Meeting: {subject}", f"Invitee: {invitee_name}", ""]
    for entry in summary.agenda_items:
        lines.append(entry.item)
        lines.append(entry.notes.strip() or "Not discussed.")
        lines.append("")
    if summary.other_extra.strip():
        lines.extend(["Other / Extra", summary.other_extra.strip(), ""])
    if summary.closing_message.strip():
        lines.append(summary.closing_message.strip())
    return "\n".join(lines)


def all_chats_frame(chats: list[tuple[str, list[ChatMessage]]]) -> pd.DataFrame:
    """Every invitee's chat, one row per message. `chats` is (invitee name, messages) pairs."""
    records = [
        {
            "Invitee": name,
            "Time": _stamp(message.created_at),
            "From": _speaker(message, name),
            "Message": message.text,
        }
        for name, messages in chats
        for message in messages
    ]
    return pd.DataFrame(records, columns=CHAT_COLUMNS)


def to_excel_bytes(frame: pd.DataFrame) -> bytes:
    """The chats as an .xlsx file.

    Raises:
        MeetingStorageError: if the file can't be written.
    """
    buffer = BytesIO()
    try:
        frame.to_excel(buffer, index=False, sheet_name="Chats", engine="openpyxl")
    except (ValueError, OSError, ImportError) as error:
        logger.exception("Could not write the chats to Excel.")
        raise MeetingStorageError(f"Could not build the Excel file: {error}") from error
    return buffer.getvalue()
