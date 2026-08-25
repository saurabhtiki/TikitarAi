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

import base64
import binascii
import json
import logging

from dashboard.exceptions import ReportSkeletonError
from dashboard.model import (
    DEFAULT_EMBED_HEIGHT,
    KIND_RESULT,
    MAX_ITEM_IMAGE_BYTES,
    MAX_LOGO_BYTES,
    PinnedItem,
    Report,
    Section,
    Subsection,
    set_embed_height,
    set_item_link,
    set_logo_height,
    set_logo_position,
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
        "kind": item.kind,
        # The second picture a skeleton carries, and no more an exception to the "no data"
        # rule than the logo is: nothing produced it and no run can put it back, so a Task
        # that dropped it would come back next month missing the Excel chart the user
        # pasted in. `model.set_item_image` caps it before it is ever stored.
        "image": base64.b64encode(item.image).decode("ascii") if item.has_image() else "",
        "image_mime": item.image_mime,
        "embed_html": item.embed_html,
        "embed_height": item.embed_height,
        "link_url": item.link_url,
        "link_text": item.link_text,
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
        # Absent from anything saved before blocks existed, which reads back as
        # `KIND_RESULT` — exactly what those items are.
        kind=str(raw.get("kind") or KIND_RESULT),
        embed_html=str(raw.get("embed_html") or ""),
    )
    # Through the setter, not straight onto the field: the number came out of a file,
    # and it is interpolated into the export's markup. A missing one is the default.
    set_embed_height(item, raw.get("embed_height", DEFAULT_EMBED_HEIGHT))
    # Through the setter too, and for a sharper reason than the height: this string ends up
    # in an `href`. A file written by hand, or by a version of this app that screened it
    # differently, is refused here rather than printed into the export.
    set_item_link(item, raw.get("link_url") or "", raw.get("link_text") or "")
    _restore_item_image(item, raw)
    # Assigned after construction rather than passed in, so a skeleton written by an older
    # version with no id still gets the fresh one the dataclass generated.
    if raw.get("item_id"):
        item.item_id = str(raw["item_id"])
    return item


def _restore_item_image(item: PinnedItem, raw: dict) -> None:
    """Puts a stored block picture back, or leaves the block without one.

    Forgiving in exactly the way `_restore_logo` is, and for the same reason: a picture
    that can no longer be read costs the picture, never the report it was one item of.
    """
    encoded = raw.get("image")
    mime = str(raw.get("image_mime") or "")
    if not encoded or not mime:
        return

    try:
        data = base64.b64decode(str(encoded), validate=True)
    except (binascii.Error, ValueError):
        logger.warning("A saved block's picture couldn't be decoded; the block loads without it.")
        return

    if not data or len(data) > MAX_ITEM_IMAGE_BYTES:
        logger.warning(
            "A saved block's picture is %s bytes, outside the %s byte limit; the block loads without it.",
            len(data),
            MAX_ITEM_IMAGE_BYTES,
        )
        return

    item.image = data
    item.image_mime = mime


def to_dict(report: Report) -> dict:
    """The report's structure as plain data.

    The **pool is not saved.** An unplaced item is by definition not in the report — the
    exports walk the section tree only — and a Task that restored a pool would hand the user
    a list of leftovers from a session they don't remember.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "title": report.title,
        # The logo is the one picture a skeleton carries, and it is not an exception to the
        # "no data" rule: it is a setting, like the title, and it is what makes a saved Task
        # produce the same branded report on every run. It is capped at `MAX_LOGO_BYTES`
        # before it is ever stored, so the base64 here is header-sized.
        "logo": base64.b64encode(report.logo).decode("ascii") if report.has_logo() else "",
        "logo_mime": report.logo_mime,
        "logo_height": report.logo_height,
        "logo_position": report.logo_position,
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
    _restore_logo(report, raw)

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


def _restore_logo(report: Report, raw: dict) -> None:
    """Puts a stored logo back on the report, or leaves it without one.

    Anything wrong with the stored value — unreadable base64, no mime type, a picture larger
    than the current cap — leaves the report logo-less rather than raising. A saved Task
    whose logo can no longer be read is still a Task worth running, and a report without a
    logo is a report; a load that failed outright would cost the user everything else in it.
    """
    encoded = raw.get("logo")
    mime = str(raw.get("logo_mime") or "")
    set_logo_height(report, raw.get("logo_height"))
    set_logo_position(report, str(raw.get("logo_position") or ""))

    if not encoded or not mime:
        return

    try:
        data = base64.b64decode(str(encoded), validate=True)
    except (binascii.Error, ValueError):
        logger.warning("A saved report's logo couldn't be decoded; the report loads without it.")
        return

    if not data or len(data) > MAX_LOGO_BYTES:
        logger.warning(
            "A saved report's logo is %s bytes, outside the %s byte limit; the report loads without it.",
            len(data),
            MAX_LOGO_BYTES,
        )
        return

    report.logo = data
    report.logo_mime = mime


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
