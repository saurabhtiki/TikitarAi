"""Name sanitizing and de-duplication.

Two unrelated jobs live here, both of the form "make these strings unique and legal":
Excel worksheet names (hard rules, 31-character limit) and Streamlit tab labels
(no rules beyond uniqueness). They are deliberately separate functions — using the
Excel deduper on tab labels would truncate perfectly good labels to 31 characters.
"""

import logging
import re

logger = logging.getLogger(__name__)

MAX_SHEET_NAME_LENGTH = 31

# The sheet the cleaning log is written to. Reserved so a user-chosen output sheet
# name can never silently collide with it and overwrite the log.
CLEANING_LOG_SHEET_NAME = "_Cleaning log"

# Characters Excel forbids in a worksheet name.
_FORBIDDEN_SHEET_CHARACTERS = re.compile(r"[\[\]:*?/\\]")

# Excel reserves this name and refuses to create a sheet with it.
_RESERVED_SHEET_NAMES = {"history"}


def sanitize_sheet_name(name: str, *, fallback: str = "Sheet") -> str:
    """Returns an Excel-legal worksheet name derived from `name`.

    Replaces Excel's forbidden characters with underscores, collapses whitespace,
    strips surrounding whitespace and apostrophes (Excel rejects a leading or trailing
    apostrophe), and truncates to 31 characters. Falls back to `fallback` when the
    result would be empty or is one of Excel's reserved names.

    Never raises — there is always some legal name to return, which is why the package
    has no sheet-naming exception.
    """
    if name is None:
        return fallback

    cleaned = _FORBIDDEN_SHEET_CHARACTERS.sub("_", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip("'").strip()
    cleaned = cleaned[:MAX_SHEET_NAME_LENGTH]

    if not cleaned or cleaned.lower() in _RESERVED_SHEET_NAMES:
        return fallback
    return cleaned


def deduplicate_sheet_names(names: list[str]) -> list[str]:
    """Makes already-sanitized worksheet names unique, preserving order.

    Excel compares sheet names case-insensitively, so `Sales` and `sales` collide and
    are treated as duplicates here too. The first occurrence keeps its name; later ones
    gain a `_2`, `_3`, … suffix, with the base **re-truncated** so the suffixed name
    still fits within 31 characters.

    `CLEANING_LOG_SHEET_NAME` counts as already taken, so a user-chosen name can never
    displace the cleaning log sheet.
    """
    taken = {CLEANING_LOG_SHEET_NAME.lower()}
    result: list[str] = []

    for name in names:
        candidate = name
        if candidate.lower() not in taken:
            taken.add(candidate.lower())
            result.append(candidate)
            continue

        suffix_number = 2
        while True:
            suffix = f"_{suffix_number}"
            base = name[: MAX_SHEET_NAME_LENGTH - len(suffix)]
            candidate = f"{base}{suffix}"
            if candidate.lower() not in taken:
                break
            suffix_number += 1

        taken.add(candidate.lower())
        result.append(candidate)

    return result


def sanitize_sheet_names(names: list[str]) -> list[str]:
    """Sanitizes then de-duplicates a list of worksheet names. The single entry point
    used by the Data Cleaner page and by `cleaner.export`."""
    return deduplicate_sheet_names([sanitize_sheet_name(name) for name in names])


def deduplicate_labels(labels: list[str]) -> list[str]:
    """Makes UI labels unique by appending ` (2)`, ` (3)`, …, preserving order.

    Used for `st.tabs`, which renders duplicate labels ambiguously — two uploads both
    named `data.csv`, or two workbooks each contributing `Sheet1`, would otherwise be
    indistinguishable. Comparison is case-sensitive and there is no length limit, which
    is why this is separate from the Excel rules above.
    """
    taken: set[str] = set()
    result: list[str] = []

    for label in labels:
        candidate = label
        suffix_number = 2
        while candidate in taken:
            candidate = f"{label} ({suffix_number})"
            suffix_number += 1
        taken.add(candidate)
        result.append(candidate)

    return result
