"""A report's structure, without any of its data (requirement 7.5).

What a saved Task records about its report is a **skeleton, never a snapshot**: the title,
the sections and subsections, the headings, the comments, the ordering and the layout flags
— and for each item, the `source_id` of whatever produced it. Never a DataFrame, never a
figure. The rows in a report describe files that are gone when the session ends, so writing
them into a recipe would produce a Task that reports last month's numbers.

That rule is enforced structurally rather than remembered: `to_json` builds fresh dicts of
named scalars, so there is no path by which a frame or a figure could travel. It is the same
shape `checks.model.to_json` already follows.

`from_json` rebuilds the tree with the items **empty** — heading, comment and `source_id`
intact, `frame` and `figure` None. Filling them back in is what a run does (requirement 8.2
step 4): a producer re-runs and calls `dashboard.session.pin_result` with the same
`source_id`, which finds the placed item and updates it *where it is*. So a loaded skeleton
is a report whose arrangement is already right and whose contents are pending.

No Streamlit here. `dashboard/session.py` owns the state; this module owns the serialization.
"""

import json
import logging

from dashboard.exceptions import ReportSkeletonError
from dashboard.model import (
    PinnedItem,
    Report,
    Section,
    Subsection,
)

logger = logging.getLogger(__name__)

# Bumped only when a stored skeleton would be read wrongly by the current code. Adding a
# field that reads back as its default is not that, on the same grounds as `checks.model`.
SCHEMA_VERSION = 1


def _item_to_dict(item: PinnedItem) -> dict:
    """One item, as the handful of scalars worth keeping.

    Note what is absent: `frame`, `figure` and `png`. `outputs` is kept because it records
    what the item is *meant* to render, which a re-run needs in order to produce the same
    shape of output — it is a description, not data.
    """
    return {
        "item_id": item.item_id,
        "question": item.question,
        "heading": item.heading,
        "comment": item.comment,
        "sql": item.sql,
        "outputs": sorted(item.outputs),
        "source_id": item.source_id,
        "column_with_previous": bool(item.column_with_previous),
    }


def _item_from_dict(raw: dict) -> PinnedItem:
    """The reverse, tolerantly: a stored skeleton is a setting to honour as far as it still
    makes sense, not input to validate."""
    item = PinnedItem(
        question=str(raw.get("question") or ""),
        heading=str(raw.get("heading") or ""),
        comment=str(raw.get("comment") or ""),
        sql=raw.get("sql") or None,
        outputs=set(raw.get("outputs") or []),
        source_id=raw.get("source_id") or None,
        column_with_previous=bool(raw.get("column_with_previous")),
    )
    # Assigned after construction rather than passed in, so a skeleton written by an older
    # version with no id still gets the fresh one the dataclass generated.
    if raw.get("item_id"):
        item.item_id = str(raw["item_id"])
    return item


def to_dict(report: Report) -> dict:
    """The report's structure as plain data.

    The **pool is not saved.** An unplaced item is by definition not in the report — the
    exports walk the section tree only — and a Task that restored a pool would hand the user
    a list of leftovers from a session they don't remember.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "title": report.title,
        "sections": [
            {
                "node_id": section.node_id,
                "name": section.name,
                "subsections": [
                    {
                        "node_id": subsection.node_id,
                        "name": subsection.name,
                        "items": [_item_to_dict(item) for item in subsection.items],
                    }
                    for subsection in section.subsections
                ],
            }
            for section in report.sections
        ],
    }


def from_dict(raw: dict) -> Report:
    """A report tree with every item empty of data. See the module docstring."""
    report = Report(title=str(raw.get("title") or ""))

    for raw_section in raw.get("sections") or []:
        section = Section(name=str(raw_section.get("name") or Section().name))
        if raw_section.get("node_id"):
            section.node_id = str(raw_section["node_id"])

        for raw_subsection in raw_section.get("subsections") or []:
            subsection = Subsection(name=str(raw_subsection.get("name") or Subsection().name))
            if raw_subsection.get("node_id"):
                subsection.node_id = str(raw_subsection["node_id"])
            subsection.items = [_item_from_dict(item) for item in raw_subsection.get("items") or []]
            section.subsections.append(subsection)

        report.sections.append(section)

    return report


def to_json(report: Report) -> str:
    try:
        return json.dumps(to_dict(report), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        logger.exception("Could not serialize the report skeleton.")
        raise ReportSkeletonError(f"This report couldn't be saved ({error}).") from error


def from_json(text: str) -> Report:
    try:
        raw = json.loads(text or "{}")
    except (TypeError, ValueError) as error:
        logger.exception("Could not read a stored report skeleton.")
        raise ReportSkeletonError(f"This saved report couldn't be read ({error}).") from error

    if not isinstance(raw, dict):
        logger.error("A stored report skeleton was %s rather than an object.", type(raw).__name__)
        raise ReportSkeletonError("This saved report couldn't be read — it isn't in the expected format.")

    return from_dict(raw)
