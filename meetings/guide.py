"""The "How to use this meeting" steps on the invitee's side panel (phase 56, 57, 59).

The app writes them from the meeting itself (its questions, lists and table tabs). Since
phase 59 the organiser can replace them with their own text (`Meeting.how_to_use`); blank
means the automatic steps. No Streamlit here, so both pages show the same default.
"""

from meetings import steps
from meetings.model import AgendaTable, Meeting
from meetings.show_files import can_show
from meetings.tables import describe_column


def default_steps(
    meeting: Meeting,
    sheets: dict[str, AgendaTable],
    list_sizes: dict[str, int],
    ref_files: list[dict] | None = None,
) -> list[str]:
    """The automatic steps, naming this meeting's own tabs and lists.

    `sheets` are the table grids by title; `list_sizes` the row count of each For each list,
    keyed by `steps.loop_key` (a list with no rows is not mentioned). `ref_files` (phase 67)
    adds a line when a reference document can be shown in the chat.
    """
    lines = [
        "Type your reply in the box at the bottom of the chat and press Enter.",
        "Or tap 🎤 Speak to say your reply — the words go into the box for you to check (your "
        "browser turns speech into text; in Chrome this goes through Google). Replies are read "
        "aloud — turn off 🔊 Read replies aloud above the chat box to stop this; tap 🔊 under a "
        "reply to hear it again.",
    ]
    if meeting.question_items():
        lines.append(
            "Some questions are asked one at a time, e.g. 'Question 2 of 5'. If an answer can't be "
            "accepted you are told why and asked again."
        )
    for name in steps.loop_names(meeting):
        size = list_sizes.get(steps.loop_key(name), 0)
        if size:
            lines.append(
                f"Questions about each row of **{name}** ({size} row(s)) are asked in the chat — "
                "see the list on the left."
            )
    lines.append("You can ask your own question at any time, e.g. 'What is the notice period?'.")
    if any(can_show(record.get("filename", "")) for record in ref_files or []):
        lines.append("Ask to see a document, e.g. 'Show me the product demo' — it opens right in the chat.")
    for item in meeting.table_items():
        sheet = sheets.get(item.item)
        if sheet is None:
            continue
        columns = ", ".join(describe_column(column, sheet.rule_for(column)) for column in sheet.editable_columns)
        fill = f"fill in **{columns}**" if columns else "check the rows"
        lines.append(f"Open the tab **📋 {item.item}** and {fill}, then press Save progress.")
    lines.append("Use Attach a file to share a document with the organiser.")
    if meeting.question_items():
        lines.append("When every question is done, press Close chat to get your summary.")
    else:
        lines.append("When you are done, press Close chat to get your summary.")
    lines.append("You can leave and come back later with the same link — nothing is lost.")
    return lines


def numbered(lines: list[str]) -> str:
    """The steps as a numbered Markdown list."""
    return "\n".join(f"{number}. {line}" for number, line in enumerate(lines, start=1))


def invitee_text(meeting: Meeting, default_lines: list[str]) -> str:
    """What the invitee sees: the organiser's own text if there is one, else the automatic steps."""
    return meeting.how_to_use.strip() or numbered(default_lines)


def text_to_store(edited: str, default_lines: list[str]) -> str:
    """What to save from the organiser's box: blank when it is still the automatic text, so the
    steps keep following later changes to the agenda."""
    text = (edited or "").strip()
    return "" if text == numbered(default_lines).strip() else text
