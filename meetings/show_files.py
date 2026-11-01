"""Reference documents the chat bot can show inside the chat (phase 67).

The bot only ever sees each file's **name and description**, never its contents — so a
Word or Excel file costs no tokens, and the bot can only point the invitee to it. Images,
videos, PDFs and HTML pages can be shown under the bot's reply; everything else is
"refer only".
"""

import re

IMAGE_KIND = "image"
VIDEO_KIND = "video"
PDF_KIND = "pdf"
HTML_KIND = "html"
OTHER_KIND = "other"

_KIND_BY_SUFFIX = {
    "png": IMAGE_KIND, "jpg": IMAGE_KIND, "jpeg": IMAGE_KIND, "gif": IMAGE_KIND, "webp": IMAGE_KIND,
    "mp4": VIDEO_KIND, "webm": VIDEO_KIND, "mov": VIDEO_KIND,
    "pdf": PDF_KIND,
    "html": HTML_KIND, "htm": HTML_KIND,
}

VIDEO_FORMAT_BY_SUFFIX = {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime"}

# A big video makes the invitee's page slow to load; 50 MB is a few minutes of good video.
MAX_VIDEO_MB = 50
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024


def _suffix(filename: str) -> str:
    name = str(filename or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def file_kind(filename: str) -> str:
    """'image', 'video', 'pdf', 'html', or 'other' (only pointed to, never shown)."""
    return _KIND_BY_SUFFIX.get(_suffix(filename), OTHER_KIND)


def can_show(filename: str) -> bool:
    return file_kind(filename) != OTHER_KIND


def video_format(filename: str) -> str:
    return VIDEO_FORMAT_BY_SUFFIX.get(_suffix(filename), "video/mp4")


def upload_problems(filename: str, size: int) -> list[str]:
    """Why an upload is refused; empty when it is fine. Only videos have a size limit."""
    if file_kind(filename) == VIDEO_KIND and size > MAX_VIDEO_BYTES:
        return [f"{filename} is {size / (1024 * 1024):.0f} MB — videos can be at most {MAX_VIDEO_MB} MB."]
    return []


def files_block(files: list[dict]) -> str:
    """The file list for the chat prompt, one line each: name, (can show)/(refer only), description."""
    lines = []
    for record in files:
        name = str(record.get("filename") or "").strip()
        if not name:
            continue
        mark = "can show" if can_show(name) else "refer only"
        description = " ".join(str(record.get("description") or "").split())
        lines.append(f"- {name} ({mark})" + (f" — {description}" if description else ""))
    return "\n".join(lines)


def _plain(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def pick_file(name: str, files: list[dict]) -> dict | None:
    """The record the bot named, matched loosely on case and spaces; None for an invented name."""
    wanted = _plain(name)
    if not wanted:
        return None
    return next((record for record in files if _plain(record.get("filename")) == wanted), None)


def file_for_step(note: str, files: list[dict], shown_ids: set[int]) -> dict | None:
    """The showable file an agenda note names ("Show demo.mp4, then discuss"), if not shown yet.

    A safety net for when the bot forgets to set show_file on starting that step; it shows
    each file once, so the reply after the start doesn't show it again.
    """
    text = _plain(note)
    if not text:
        return None
    return next(
        (
            record
            for record in files
            if record.get("file_id") not in shown_ids
            and can_show(record.get("filename"))
            # Whole name only: "plan.png" must not match inside "floorplan.png".
            and re.search(rf"(?<![\w.]){re.escape(_plain(record.get('filename')))}(?!\w)", text)
        ),
        None,
    )
