"""Reference documents the chat bot can show inside the chat (phase 67).

The bot only ever sees each file's **name and description**, never its contents — so a
Word or Excel file costs no tokens, and the bot can only point the invitee to it. Images,
videos, PDFs and HTML pages can be shown under the bot's reply; everything else is
"refer only".

Phase 68: a reference document can also be a **link** (`url` set, no file on disk). A YouTube
link or a link to a video/picture file plays or shows like an upload; any other link (Google
Drive, a web page) becomes an "Open" button, since most sites refuse to be framed.
"""

import re
from urllib.parse import urlparse

IMAGE_KIND = "image"
VIDEO_KIND = "video"
PDF_KIND = "pdf"
HTML_KIND = "html"
OTHER_KIND = "other"
LINK_KIND = "link"

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
MAX_LINK_CHARS = 2000

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


def is_link(record: dict) -> bool:
    return bool(str(record.get("url") or "").strip())


def link_kind(url: str) -> str:
    """'video' for YouTube or a video file, 'image' for a picture file, otherwise 'link'."""
    parsed = urlparse(str(url or "").strip())
    if parsed.hostname in _YOUTUBE_HOSTS:
        return VIDEO_KIND
    kind = file_kind(parsed.path)
    return kind if kind in (VIDEO_KIND, IMAGE_KIND) else LINK_KIND


def record_kind(record: dict) -> str:
    """The kind of a reference document record, upload or link."""
    if is_link(record):
        return link_kind(record["url"])
    return file_kind(record.get("filename", ""))


def shows(record: dict) -> bool:
    """True when the record can appear under a bot reply (every link can, as a video, picture or button)."""
    return record_kind(record) != OTHER_KIND


def link_problems(name: str, url: str, files: list[dict]) -> list[str]:
    """Why a link is refused; empty when it is fine. Only http(s) links with a host are allowed."""
    problems = []
    clean_name = " ".join(str(name or "").split())
    clean_url = str(url or "").strip()
    if not clean_name:
        problems.append("Give the link a name, e.g. 'Product demo'.")
    elif pick_file(clean_name, files) is not None:
        problems.append(f"{clean_name} is already in the list — use another name.")
    parsed = urlparse(clean_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        problems.append("The link must start with http:// or https://, e.g. https://youtu.be/abc123.")
    elif len(clean_url) > MAX_LINK_CHARS:
        problems.append(f"The link is too long (at most {MAX_LINK_CHARS} characters).")
    return problems


def video_format(filename: str) -> str:
    return VIDEO_FORMAT_BY_SUFFIX.get(_suffix(filename), "video/mp4")


def upload_problems(filename: str, size: int) -> list[str]:
    """Why an upload is refused; empty when it is fine. Only videos have a size limit."""
    if file_kind(filename) == VIDEO_KIND and size > MAX_VIDEO_BYTES:
        return [f"{filename} is {size / (1024 * 1024):.0f} MB — videos can be at most {MAX_VIDEO_MB} MB."]
    return []


def step_documents(agenda: list) -> dict[str, list[str]]:
    """Each Show document name (plain) -> the rows that show it (phase 70b)."""
    rows: dict[str, list[str]] = {}
    for item in agenda:
        if item.show_file:
            rows.setdefault(_plain(item.show_file), []).append(item.item)
    return rows


def kept_for_other_steps(agenda: list, step_title: str, files: list[dict]) -> set[int]:
    """Ids of documents another row's Show document owns: only that row may show them.

    "Like to see our company video?" on About Us must not play the video meant for the
    Company Video row, whether the bot picks it or the note happens to name it.
    """
    owners = step_documents(agenda)
    return {
        record.get("file_id")
        for record in files
        if (rows := owners.get(_plain(record.get("filename")))) and step_title not in rows
    }


def files_block(files: list[dict], agenda: list | None = None) -> str:
    """The file list for the chat prompt, one line each: name, (can show)/(refer only), description.

    A document a row's Show document owns is marked so: the app shows it at that step itself.
    """
    owners = step_documents(agenda or [])
    lines = []
    for record in files:
        name = str(record.get("filename") or "").strip()
        if not name:
            continue
        mark = "can show" if shows(record) else "refer only"
        if rows := owners.get(_plain(name)):
            steps_text = ", ".join(f'"{row}"' for row in rows)
            mark = f"the app shows it at {steps_text} only — never set show_file to it"
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


def missing_documents(agenda: list, files: list[dict]) -> list[str]:
    """A red-note sentence for each Show document cell naming no Reference document (phase 70)."""
    return [
        f"{item.item}: Show document '{item.show_file}' isn't in Reference documents — add it there, or pick another."
        for item in agenda
        if item.show_file and pick_file(item.show_file, files) is None
    ]


def chosen_file(name: str, files: list[dict], shown_ids: set[int]) -> dict | None:
    """The step's Show document (phase 70), if it exists, can be shown, and isn't shown yet."""
    record = pick_file(name, files)
    if record is None or not shows(record) or record.get("file_id") in shown_ids:
        return None
    return record


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
            and shows(record)
            # Whole name only: "plan.png" must not match inside "floorplan.png".
            and re.search(rf"(?<![\w.]){re.escape(_plain(record.get('filename')))}(?!\w)", text)
        ),
        None,
    )
