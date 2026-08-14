"""Where a meeting's files live on disk (requirement 6.7, Phase 1, spec 7).

```
data/meeting_storage/
  <meeting_id>/
    ref_docs/                  ← creator-uploaded, shared, read-only to invitees
    invitee_<invitee_id>/      ← that invitee's own uploads, private
```

Rooted under `data/` to match `auth/photos.py` rather than at the repo root as spec 7
sketches, so everything the app writes stays in one directory that is already understood to
be app state.

Invitee folders are named by **id, not email** — spec 7 suggests a sanitized email, but an
email sanitizes to something that can collide (`a.b@x.com` and `a+b@x.com` both flatten),
and a collision here would put one supplier's private upload in another's folder. The id is
already unique and already the thing the database joins on.

The database is the source of truth for what exists (`meeting_files`); this module only
decides paths and writes bytes.
"""

import logging
import re
from pathlib import Path

from meetings.exceptions import MeetingStorageError

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_ROOT = Path("data") / "meeting_storage"

REF_DOCS_FOLDER = "ref_docs"

# What a stored filename is allowed to contain. Anything else becomes an underscore, which
# is what stops a name like `../../config` from being a path at all.
_UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")

MAX_FILENAME_LENGTH = 120


def sanitize_filename(filename: str) -> str:
    """A user-supplied filename reduced to something safe to join onto a path.

    Strips the directory part entirely before sanitising, rather than only escaping
    separators: an uploaded file's name is never meant to carry a location, so the safest
    reading of one that does is that the location is not wanted.
    """
    name = Path(str(filename or "")).name
    cleaned = _UNSAFE_CHARACTERS.sub("_", name).lstrip(".")
    cleaned = cleaned[:MAX_FILENAME_LENGTH]
    return cleaned or "upload"


def meeting_folder(meeting_id: int, root: Path | str = DEFAULT_STORAGE_ROOT) -> Path:
    """Everything belonging to one meeting."""
    return Path(root) / str(meeting_id)


def ref_docs_folder(meeting_id: int, root: Path | str = DEFAULT_STORAGE_ROOT) -> Path:
    """The meeting's shared reference documents."""
    return meeting_folder(meeting_id, root) / REF_DOCS_FOLDER


def invitee_folder(meeting_id: int, invitee_id: int, root: Path | str = DEFAULT_STORAGE_ROOT) -> Path:
    """One invitee's private uploads for this meeting."""
    return meeting_folder(meeting_id, root) / f"invitee_{invitee_id}"


def save_upload(
    data: bytes,
    filename: str,
    meeting_id: int,
    invitee_id: int | None = None,
    root: Path | str = DEFAULT_STORAGE_ROOT,
) -> Path:
    """Writes an uploaded file and returns where it landed.

    `invitee_id=None` stores it as a shared reference document. A name already taken is
    suffixed rather than overwritten — two invitees both uploading `scan.pdf` is ordinary,
    and losing the first one silently is not an acceptable way to handle it.

    Raises:
        MeetingStorageError: if the file can't be written.
    """
    folder = (
        ref_docs_folder(meeting_id, root)
        if invitee_id is None
        else invitee_folder(meeting_id, invitee_id, root)
    )
    safe_name = sanitize_filename(filename)

    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_name
        stem, suffix = target.stem, target.suffix
        counter = 2
        while target.exists():
            target = folder / f"{stem}_{counter}{suffix}"
            counter += 1
        target.write_bytes(data)
    except OSError as error:
        logger.exception("Failed to store '%s' for meeting %s.", filename, meeting_id)
        raise MeetingStorageError(f"Could not save '{filename}'.") from error

    return target


def read_file(filepath: str | Path) -> bytes:
    """The bytes of a stored file, for a download button.

    Raises:
        MeetingStorageError: if it can't be read — most likely because it was removed from
            disk while its database row survived.
    """
    try:
        return Path(filepath).read_bytes()
    except OSError as error:
        logger.exception("Failed to read the stored file %s.", filepath)
        raise MeetingStorageError("This file is no longer available on disk.") from error
