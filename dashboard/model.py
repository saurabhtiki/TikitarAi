"""The report tree and every pure operation on it (requirement 6.3).

A report is a title, an ordered list of sections, each an ordered list of subsections,
each an ordered list of pinned items. Nothing here imports Streamlit — `dashboard/session.py`
owns the state, this module owns the shape — so the whole of requirement 6.3's
"reorder items, sections and subsections" is testable without `AppTest`.

Three decisions are worth stating, because the rest of the package leans on them:

- **A pinned item owns copies, never references.** `analyst.session` releases the frame
  and figure of transcript messages older than `FULL_PAYLOAD_MESSAGES`, so an item that
  held on to a `ChatMessage` would quietly empty itself after a dozen more questions.
  `PinnedItem` takes a copy of the frame at pin time and never looks at the transcript
  again.
- **Items live in subsections only.** Creating a section auto-creates a "General"
  subsection, so the user is never blocked on inventing a second name, and the Excel
  export's one-sheet-per-subsection mapping stays unambiguous.
- **Numbering is derived, never stored.** `numbered_sections` computes "1", "1.1" from
  list position, so a reorder renumbers for free and no two nodes can ever claim "2.1".
- **Side-by-side layout is derived too.** An item carries one flag — "sit beside the item
  above" — and `group_into_rows` turns a run of them into a row. Nothing stores a row id,
  so reordering can never leave a row pointing at an item that moved out of it.
"""

import base64
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# The subsection every new section starts with, so an item always has somewhere to land.
DEFAULT_SUBSECTION_NAME = "General"

# Shown wherever a node was created without a name — an empty heading in an exported
# report reads as a bug, and a placeholder the user can rename does not.
UNTITLED_SECTION = "Untitled section"
UNTITLED_ITEM = "Untitled item"
UNTITLED_REPORT = "Untitled report"

# How many items may share one row of the report. Four narrow columns is already the point
# where a table inside one stops being readable, and past that the HTML wraps anyway — so
# the cap is enforced in `group_into_rows` rather than left to the stylesheet.
MAX_ROW_COLUMNS = 4

# What an item *is*, which for everything the app produces is derivable — `has_chart()` and
# `has_table()` read the payload rather than a stored label, and always have. A block the
# user writes cannot work that way: an empty text block and an empty picture block hold
# exactly the same nothing, and only the button they pressed says which editor to show. So
# the kind is stored, and only for the three the user creates.
KIND_RESULT = "result"
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_EMBED = "embed"

MANUAL_KINDS = (KIND_TEXT, KIND_IMAGE, KIND_EMBED)

# What each block is called on the button that makes it and on the card that shows it.
BLOCK_LABELS = {
    KIND_TEXT: "Text",
    KIND_IMAGE: "Picture",
    KIND_EMBED: "HTML",
}

BLOCK_ICONS = {
    KIND_TEXT: ":material/notes:",
    KIND_IMAGE: ":material/image:",
    KIND_EMBED: ":material/code_blocks:",
}

# The heading a new block carries until the user writes one, so it is never nameless in
# the pool.
BLOCK_HEADINGS = {
    KIND_TEXT: "Note",
    KIND_IMAGE: "Picture",
    KIND_EMBED: "Pasted HTML",
}

# An item's picture is printed the full width of its column rather than in a corner, so it
# is allowed to be bigger than the logo. Still capped: it is embedded in every HTML
# download and stored inside the saved Task, exactly like the logo.
MAX_ITEM_IMAGE_BYTES = 2_000_000

# Where the logo sits relative to the title. Three answers cover what a report header can
# reasonably be; anything else is a stylesheet's job, not a picker's.
LOGO_POSITIONS = ("left", "right", "above")
DEFAULT_LOGO_POSITION = "left"

# The logo is embedded in every HTML export and stored inside the saved Task, so it is
# capped at the size a header image actually needs. A 2 MB photograph in a corner of the
# page is a slow download for no visible gain.
MAX_LOGO_BYTES = 512_000

# How tall the logo is printed, in CSS pixels. The width follows the aspect ratio.
MIN_LOGO_HEIGHT = 24
MAX_LOGO_HEIGHT = 160
DEFAULT_LOGO_HEIGHT = 56

# How tall a pasted-HTML block's frame is, in CSS pixels. A frame needs a size and only the
# user knows how tall their page is, so this is a control on the block rather than a guess
# here; content taller than the frame scrolls inside it. The default is roughly a pivot of
# fifteen rows.
MIN_EMBED_HEIGHT = 100
MAX_EMBED_HEIGHT = 3000
DEFAULT_EMBED_HEIGHT = 400


def new_id() -> str:
    """A short stable id. Used in widget keys, so it must survive reruns unchanged —
    which is why nodes carry one rather than being identified by list position."""
    return uuid.uuid4().hex[:12]


@dataclass
class PinnedItem:
    """One pinned chat output.

    Attributes:
        item_id: stable across reruns; forms the widget keys for this item's controls.
        question: what was asked to produce it, shown as context when the item is opened.
        heading: what the report calls it. Defaults to the question, so a pinned item is
            already labelled before the user opens the Dashboard.
        comment: the written note under the output. It arrives as the chat's own commentary
            on this answer and is the user's to edit from then on. Held as a small piece of
            HTML — the comment box has a bold/italic/underline/list toolbar — kept to an
            allow-list by `dashboard.rich_text`. A comment written before that toolbar
            existed is plain text with no tags in it, which that module handles as-is.
        sql: the statement behind it, shown when the item is opened.
        frame: a *copy* of the rows, or None.
        figure: the Plotly figure, or None.
        outputs: which of requirement 6.2's output types this item renders.
        png: the rasterized chart, cached after the first export so HTML and Excel
            downloads of the same report don't rasterize it twice.
        source_id: what produced this item, for things that own their report item and
            re-save it — a criteria in `checks/` (requirement 6.5). None for anything
            pinned from the chat, where each pin is its own one-off snapshot.
        kind: which of `MANUAL_KINDS` the user chose when they made this block, or
            `KIND_RESULT` for everything the app produced. Stored rather than derived —
            see the constants.
        image: a picture the user uploaded onto this block, printed above the comment.
            Distinct from `png`, which is a cache of a rasterized chart.
        image_mime: what `image` is, so it can be written as a `data:` URI.
        embed_html: markup the user pasted in, already reduced to what
            `dashboard.embed_html` allows.
        embed_height: how tall to draw that block's frame, in CSS pixels.
        column_with_previous: render this item beside the one above it rather than under
            it. The flag belongs to the item, not to a position, so moving an item carries
            its answer to "do I sit beside my neighbour" with it — and an item that lands
            first in a subsection simply starts a row, because `group_into_rows` has
            nothing above it to join.
    """

    item_id: str = field(default_factory=new_id)
    question: str = ""
    heading: str = ""
    comment: str = ""
    sql: str | None = None
    frame: pd.DataFrame | None = None
    figure: Any = None
    outputs: set[str] = field(default_factory=set)
    png: bytes | None = None
    source_id: str | None = None
    column_with_previous: bool = False
    kind: str = KIND_RESULT
    image: bytes | None = None
    image_mime: str = ""
    embed_html: str = ""
    embed_height: int = DEFAULT_EMBED_HEIGHT

    def display_heading(self) -> str:
        """What to print above this item. Never empty."""
        return (self.heading or self.question or UNTITLED_ITEM).strip() or UNTITLED_ITEM

    def has_chart(self) -> bool:
        return self.figure is not None

    def has_table(self) -> bool:
        return self.frame is not None and not self.frame.empty

    def has_image(self) -> bool:
        """A picture the user put here — never the rasterized chart, which is `png`."""
        return bool(self.image) and bool(self.image_mime)

    def has_embed(self) -> bool:
        return bool(self.embed_html.strip())

    def is_manual_block(self) -> bool:
        """Whether the user made this block rather than the app producing it.

        What the report views ask before offering a picture uploader or an HTML box: those
        controls belong on a block someone chose to write, not under every pinned answer.
        """
        return self.kind in MANUAL_KINDS

    def image_data_uri(self) -> str:
        """The picture as a `data:` URI, or "" when there is none.

        The same arrangement `Report.logo_data_uri` has, for the same reason: the Preview
        view and the HTML export both want this string, and encoding the bytes in two
        places is two places to get the mime type wrong.
        """
        if not self.has_image():
            return ""
        return f"data:{self.image_mime};base64," + base64.b64encode(self.image).decode("ascii")


@dataclass
class Subsection:
    node_id: str = field(default_factory=new_id)
    name: str = DEFAULT_SUBSECTION_NAME
    items: list[PinnedItem] = field(default_factory=list)


@dataclass
class Section:
    node_id: str = field(default_factory=new_id)
    name: str = UNTITLED_SECTION
    subsections: list[Subsection] = field(default_factory=list)


@dataclass
class Report:
    """The whole dashboard.

    `pool` holds items that have been pinned but not yet placed — requirement 6.1 step 3
    is explicit that pinning must not interrupt the chat to ask where something goes, so
    everything lands here first and is arranged later.
    """

    title: str = ""
    sections: list[Section] = field(default_factory=list)
    pool: list[PinnedItem] = field(default_factory=list)

    # The header logo, printed beside the title in the HTML export. Bytes rather than a
    # path, for the same reason the exports embed charts as base64: the downloaded file has
    # to open on a machine that has never seen this app, so it cannot point at a file.
    logo: bytes | None = None
    logo_mime: str = ""
    logo_height: int = DEFAULT_LOGO_HEIGHT
    logo_position: str = DEFAULT_LOGO_POSITION

    def has_logo(self) -> bool:
        return bool(self.logo) and bool(self.logo_mime)

    def logo_data_uri(self) -> str:
        """The logo as a `data:` URI, or "" when there is none.

        Built here rather than in the HTML exporter because the Preview view wants the same
        string, and two places encoding the same bytes is two places to get the mime type
        wrong.
        """
        if not self.has_logo():
            return ""
        return f"data:{self.logo_mime};base64," + base64.b64encode(self.logo).decode("ascii")

    def is_empty(self) -> bool:
        """True when there is nothing to export — no placed item anywhere."""
        return not any(subsection.items for section in self.sections for subsection in section.subsections)


# --------------------------------------------------------------------------------------
# The header logo
# --------------------------------------------------------------------------------------

# What an `<img src="data:…">` in a self-contained page can actually show. Shared by the
# header logo and by a picture block, which face the same constraint. SVG is left out
# on purpose: it is a document, not a picture, and one embedded in the export would be the
# second string in the file that isn't escaped.
PICTURE_MIME_BY_SUFFIX = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

PICTURE_FILE_TYPES = ("png", "jpg", "jpeg", "gif", "webp")


def picture_problems(data: bytes | None, filename: str, *, limit: int, what: str) -> list[str]:
    """Everything wrong with a would-be picture, in plain English. Empty means accept.

    Same shape as `css_presets.validate_css` and for the same reason: the page needs
    something to *show* the user, not an exception to catch. Nothing is stored on the report
    until this comes back empty.

    One function for the header logo and for a picture block, because the two face the same
    constraint — both are base64'd into every HTML download and into the saved Task — and
    differ only in how big that makes it reasonable to be. `what` names the picture in the
    message so the user is told which one was refused.
    """
    problems: list[str] = []

    if not data:
        return ["That file is empty, so there is nothing to show."]

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in PICTURE_MIME_BY_SUFFIX:
        problems.append(
            f"The {what} has to be a PNG, JPG, GIF or WEBP file — those are what an offline "
            "HTML page can carry."
        )

    if len(data) > limit:
        problems.append(
            f"The {what} is {len(data) / 1024:,.0f} KB, over the {limit // 1024} KB limit. "
            "It goes inside every download, so a smaller picture is all that is needed."
        )

    return problems


def logo_problems(data: bytes | None, filename: str) -> list[str]:
    """`picture_problems` at the header logo's size. A logo sits in a corner of the page,
    so it is held to a far smaller cap than a picture block printed full width."""
    return picture_problems(data, filename, limit=MAX_LOGO_BYTES, what="logo")


def set_logo(report: Report, data: bytes, filename: str) -> list[str]:
    """Stores a logo on the report, or returns why it was refused.

    Refusing leaves whatever logo was already there in place, on the same grounds the
    stylesheet editor keeps the previous stylesheet: a rejected change costs the change and
    never the report.
    """
    problems = logo_problems(data, filename)
    if problems:
        logger.info("Refused a logo for report '%s': %s", report.title, "; ".join(problems))
        return problems

    suffix = filename.rsplit(".", 1)[-1].lower()
    report.logo = bytes(data)
    report.logo_mime = PICTURE_MIME_BY_SUFFIX[suffix]
    return []


def clear_logo(report: Report) -> None:
    report.logo = None
    report.logo_mime = ""


def set_logo_height(report: Report, height) -> int:
    """Stores the printed logo height, clamped to what the slider offers.

    Clamped here rather than trusted from the widget, because the same value comes back out
    of a saved Task, where it was written by whatever version of this app saved it — and it
    is interpolated straight into the export's markup.
    """
    try:
        clamped = max(MIN_LOGO_HEIGHT, min(int(height), MAX_LOGO_HEIGHT))
    except (TypeError, ValueError):
        clamped = DEFAULT_LOGO_HEIGHT
    report.logo_height = clamped
    return clamped


def set_embed_height(item, height) -> int:
    """Stores a pasted block's frame height, clamped to what the control offers.

    Clamped here rather than trusted from the widget, for the same reason
    `set_logo_height` is: the same value comes back out of a saved Task, where it was
    written by whatever version of this app saved it — and it is interpolated straight
    into the export's markup.
    """
    try:
        clamped = max(MIN_EMBED_HEIGHT, min(int(height), MAX_EMBED_HEIGHT))
    except (TypeError, ValueError):
        clamped = DEFAULT_EMBED_HEIGHT
    item.embed_height = clamped
    return clamped


def set_logo_position(report: Report, position: str) -> str:
    """Stores where the logo sits, falling back to the default for an unknown value.

    The value becomes a CSS class in the exported page, so an unrecognized one is replaced
    rather than passed through.
    """
    report.logo_position = position if position in LOGO_POSITIONS else DEFAULT_LOGO_POSITION
    return report.logo_position


# --------------------------------------------------------------------------------------
# Blocks the user writes
# --------------------------------------------------------------------------------------


def new_block(kind: str) -> PinnedItem:
    """An empty block of one of `MANUAL_KINDS`, ready for the pool.

    It carries a placeholder heading rather than none, so it is findable in a pool of a
    dozen items before the user has got round to naming it. An unrecognized kind becomes a
    text block: the least presumptuous of the three, and the one that loses nothing if the
    guess is wrong.
    """
    if kind not in MANUAL_KINDS:
        logger.info("Asked for an unknown block kind %r; making a text block instead.", kind)
        kind = KIND_TEXT
    return PinnedItem(kind=kind, heading=BLOCK_HEADINGS[kind])


def set_item_image(item: PinnedItem, data: bytes, filename: str) -> list[str]:
    """Puts a picture on a block, or returns why it was refused.

    Refusing leaves whatever picture was already there, on the same grounds `set_logo`
    gives: a rejected change costs the change and never the item.
    """
    problems = picture_problems(data, filename, limit=MAX_ITEM_IMAGE_BYTES, what="picture")
    if problems:
        logger.info("Refused a picture for item %s: %s", item.item_id, "; ".join(problems))
        return problems

    suffix = filename.rsplit(".", 1)[-1].lower()
    item.image = bytes(data)
    item.image_mime = PICTURE_MIME_BY_SUFFIX[suffix]
    return []


def clear_item_image(item: PinnedItem) -> None:
    item.image = None
    item.image_mime = ""


# The fields on an item that a person writes and no run can produce. A report item's rows
# and chart come back fresh every run; its note, its picture and its pasted markup do not —
# they sit in the saved skeleton exactly as they were typed. So these are the five fields
# that are worth carrying from a finished run back into the recipe it came from, and
# `frame`/`figure`/`png` are deliberately absent: `skeleton.to_dict` refuses to store them,
# and a copier that could put one there would be a way round that rule.
#
# **Every name here must also be stored by `skeleton._item_to_dict`.** Copying a field the
# skeleton drops would give the user a Save that reports success and loses the value on the
# next load — the quietest way this could break. `TestAuthoredFields` in
# `tests/test_dashboard_blocks.py` fails when a sixth field is added here and not there.
AUTHORED_FIELDS = ("comment", "image", "image_mime", "embed_html", "embed_height")


def copy_authored_content(source: Report, target: Report) -> int:
    """Copies the by-hand fields of every item in `source` onto its twin in `target`.

    Matched on `item_id`, never on position: a run's report is a deep copy of the Task's
    skeleton, so the ids line up at both ends, and matching by id stays exact even if the
    Task has since been reordered in Task Builder. An item with no twin is skipped rather
    than appended — it was deleted from the saved report, and putting it back would undo
    that silently.

    Returns how many items were written to, so the page can say so.
    """
    twins = {item.item_id: item for _, _, item in iter_items(target)}
    written = 0

    for _, _, item in iter_items(source):
        twin = twins.get(item.item_id)
        if twin is None:
            logger.info("Item %s has no twin in the target report; leaving it out.", item.item_id)
            continue
        for name in AUTHORED_FIELDS:
            setattr(twin, name, getattr(item, name))
        written += 1

    return written


# --------------------------------------------------------------------------------------
# Numbering
# --------------------------------------------------------------------------------------


def numbered_sections(report: Report) -> list[tuple[str, Section]]:
    """Sections as `("1", section)`, in order."""
    return [(str(position), section) for position, section in enumerate(report.sections, start=1)]


def numbered_subsections(section: Section, section_number: str) -> list[tuple[str, Subsection]]:
    """A section's subsections as `("1.1", subsection)`, in order."""
    return [
        (f"{section_number}.{position}", subsection)
        for position, subsection in enumerate(section.subsections, start=1)
    ]


def numbered_items(items: list[PinnedItem], subsection_number: str) -> list[tuple[str, PinnedItem]]:
    """A subsection's items as `("2.1.1", item)`, in order.

    The third level of the same derived numbering `numbered_sections` and
    `numbered_subsections` already do: position in the list, never a stored value, so a
    reorder renumbers for free. It reads as `section.subsection.point`, which is the shape
    every numbered document uses, so "2.1.3" is unambiguous the first time it is seen.
    """
    return [
        (f"{subsection_number}.{position}", item)
        for position, item in enumerate(items, start=1)
    ]


def group_into_rows(items: list[PinnedItem]) -> list[list[PinnedItem]]:
    """A subsection's items split into rows of side-by-side columns.

    A run of items whose `column_with_previous` is set joins the row the first of them
    started, so turning the flag on for items 2, 3 and 4 puts items 1–4 in one row of four
    equal columns. Everything else is a row of its own, which is what an untouched report
    is: every flag off, one item per row, exactly the old behaviour.

    Two things the flag can ask for and not get, both resolved by starting a new row rather
    than by refusing:

    - the **first** item of a subsection has nothing above it to join;
    - a row already holding `MAX_ROW_COLUMNS` items is full, so the next one wraps.

    The Preview view, the HTML export and the Build view's own warning all group through
    this one function, so what the page says about a row is what the download does with it.
    """
    rows: list[list[PinnedItem]] = []
    for item in items:
        if rows and item.column_with_previous and len(rows[-1]) < MAX_ROW_COLUMNS:
            rows[-1].append(item)
        else:
            rows.append([item])
    return rows


def wraps_to_new_row(items: list[PinnedItem], index: int) -> bool:
    """True when the item at `index` asked to sit beside the one above and couldn't.

    Only ever True for an item whose flag is on, and only because it is first in its
    subsection or because the row above is full. The Build view turns it into a note under
    the toggle — a switch that is on while the export ignores it needs to say so.
    """
    if not 0 <= index < len(items):
        return False
    item = items[index]
    if not item.column_with_previous:
        return False
    return any(row[0] is item for row in group_into_rows(items))


def subsection_choices(report: Report) -> list[tuple[str, str]]:
    """Every subsection as `(node_id, "1.1 By region")`, for the place/move dropdowns.

    One flat list rather than a nested picker: choosing a destination is one decision,
    and the number prefix already says which section it belongs to.
    """
    choices: list[tuple[str, str]] = []
    for section_number, section in numbered_sections(report):
        for number, subsection in numbered_subsections(section, section_number):
            choices.append((subsection.node_id, f"{number} {subsection.name}"))
    return choices


# --------------------------------------------------------------------------------------
# Building the tree
# --------------------------------------------------------------------------------------


def add_section(report: Report, name: str = "") -> Section:
    """Appends a section, already carrying a "General" subsection.

    The auto-created subsection is what makes "add a section, pin something into it" a
    two-step job instead of three — and it is why `assign_item` can always find a home.
    """
    section = Section(name=(name or "").strip() or UNTITLED_SECTION)
    section.subsections.append(Subsection())
    report.sections.append(section)
    return section


def add_subsection(section: Section, name: str = "") -> Subsection:
    subsection = Subsection(name=(name or "").strip() or DEFAULT_SUBSECTION_NAME)
    section.subsections.append(subsection)
    return subsection


def find_subsection(report: Report, node_id: str) -> Subsection | None:
    for section in report.sections:
        for subsection in section.subsections:
            if subsection.node_id == node_id:
                return subsection
    return None


def find_section(report: Report, node_id: str) -> Section | None:
    for section in report.sections:
        if section.node_id == node_id:
            return section
    return None


def iter_items(report: Report):
    """Every item in the report as `(container, position, item)`, pool first.

    One traversal for every lookup there is. `container` and `position` come along because
    removing an item needs them, and a finder that walked the tree a second way to support
    that is how three near-identical loops appeared here in the first place.
    """
    for position, item in enumerate(report.pool):
        yield report.pool, position, item
    for section in report.sections:
        for subsection in section.subsections:
            for position, item in enumerate(subsection.items):
                yield subsection.items, position, item


def find_item(report: Report, item_id: str) -> PinnedItem | None:
    """The item with this id, wherever it lives — pool or any subsection."""
    return next((item for _, _, item in iter_items(report) if item.item_id == item_id), None)


def find_item_by_source(report: Report, source_id: str) -> PinnedItem | None:
    """The item a given producer owns, wherever it lives — pool or any subsection.

    Searched by `source_id` rather than by a remembered `item_id` for the same reason
    `session.pinned_item` re-looks-up rather than trusting what it stored: the user may have
    removed the item on this page, and an owner that keeps updating a deleted item would be
    writing into nothing.
    """
    if not source_id:
        return None
    return next((item for _, _, item in iter_items(report) if item.source_id == source_id), None)


def _detach_item(report: Report, item_id: str) -> PinnedItem | None:
    """Removes an item from wherever it currently is and returns it."""
    for container, position, item in iter_items(report):
        if item.item_id == item_id:
            return container.pop(position)
    return None


def assign_item(report: Report, item_id: str, subsection_id: str) -> bool:
    """Moves an item into a subsection, from the pool or from another subsection.

    Both halves of requirement 6.3 — "assign a pinned item to a subsection" and "move it
    to a different one" — are the same operation, so they are one function: detach from
    wherever it is, append where it was asked for. Returns False when either id is
    unknown, leaving the report untouched.
    """
    target = find_subsection(report, subsection_id)
    if target is None:
        logger.info("Ignoring assignment of item %s to unknown subsection %s.", item_id, subsection_id)
        return False

    item = _detach_item(report, item_id)
    if item is None:
        logger.info("Ignoring assignment of unknown item %s.", item_id)
        return False

    target.items.append(item)
    return True


def unassign_item(report: Report, item_id: str) -> bool:
    """Sends a placed item back to the unplaced pool."""
    item = _detach_item(report, item_id)
    if item is None:
        return False
    report.pool.append(item)
    return True


# --------------------------------------------------------------------------------------
# Removing
# --------------------------------------------------------------------------------------


def remove_item(report: Report, item_id: str) -> bool:
    """Discards an item outright, from the pool or from a subsection."""
    return _detach_item(report, item_id) is not None


def remove_subsection(report: Report, subsection_id: str) -> int:
    """Deletes a subsection, returning any items it held to the pool.

    Returns the number of items rescued, so the page can say so. Deleting a container
    should never destroy the answers inside it — those cost a model call each, and the
    transcript they came from may have released its copy by now.
    """
    for section in report.sections:
        for position, subsection in enumerate(section.subsections):
            if subsection.node_id != subsection_id:
                continue
            rescued = section.subsections.pop(position).items
            report.pool.extend(rescued)
            return len(rescued)
    return 0


def remove_section(report: Report, section_id: str) -> int:
    """Deletes a section and every subsection under it, returning their items to the pool."""
    for position, section in enumerate(report.sections):
        if section.node_id != section_id:
            continue
        removed = report.sections.pop(position)
        rescued = [item for subsection in removed.subsections for item in subsection.items]
        report.pool.extend(rescued)
        return len(rescued)
    return 0


# --------------------------------------------------------------------------------------
# Reordering
# --------------------------------------------------------------------------------------


def move(items: list, index: int, target: int) -> bool:
    """Moves one entry to a new position among its siblings.

    Reordering sections, subsections and items is the same operation on a list, so it is
    one function used at all three levels: the ▲/▼ buttons call it with `index ± 1`, the
    position box calls it with the number typed. `target` is clamped to the list, so a
    ▲ on the first row is a no-op rather than a wrap-around to the bottom.

    Returns True when something actually moved.
    """
    if not 0 <= index < len(items):
        return False

    clamped = max(0, min(target, len(items) - 1))
    if clamped == index:
        return False

    items.insert(clamped, items.pop(index))
    return True


# --------------------------------------------------------------------------------------
# Walking, for the preview and the exporters
# --------------------------------------------------------------------------------------


@dataclass
class RenderedSubsection:
    number: str
    name: str
    items: list[PinnedItem]

    def rows(self) -> list[list[PinnedItem]]:
        """The same items grouped into side-by-side rows, for renderers that lay out in
        two dimensions. The Excel export ignores this and reads `items`: a worksheet puts
        one block under the next, and a "column" there would mean something else entirely.
        """
        return group_into_rows(self.items)

    def numbered(self) -> list[tuple[str, PinnedItem]]:
        """The items as `("2.1.1", item)`. What the Excel export writes above each block."""
        return numbered_items(self.items, self.number)

    def numbered_rows(self) -> list[list[tuple[str, PinnedItem]]]:
        """`rows()`, with each item carrying its number.

        Numbers are handed out in **reading order**, not per row, so three items side by
        side are 2.1.1, 2.1.2 and 2.1.3 across — which is how they are read. The grouping
        is still `group_into_rows`'s, drawn on rather than repeated, so layout is decided
        in exactly one place.
        """
        numbers = iter(number for number, _ in self.numbered())
        return [[(next(numbers), item) for item in row] for row in self.rows()]


@dataclass
class RenderedSection:
    number: str
    name: str
    subsections: list[RenderedSubsection]


def walk(report: Report, *, skip_empty: bool = True) -> list[RenderedSection]:
    """The report flattened into numbered, ready-to-render nodes.

    The Preview view and both exporters walk the tree through this one function, which is
    what makes "what you see is what you download" true by construction rather than by
    three renderers agreeing with each other.

    Args:
        skip_empty: drops subsections with no items, and sections left with no subsections.
            On by default — an empty heading in a downloaded report is noise. The Build
            view passes False, because that is exactly where empty containers must show.
    """
    rendered: list[RenderedSection] = []

    for section_number, section in numbered_sections(report):
        subsections = [
            RenderedSubsection(number=number, name=subsection.name, items=list(subsection.items))
            for number, subsection in numbered_subsections(section, section_number)
            if subsection.items or not skip_empty
        ]
        if not subsections and skip_empty:
            continue
        rendered.append(
            RenderedSection(number=section_number, name=section.name, subsections=subsections)
        )

    return rendered
