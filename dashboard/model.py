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
"""

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


def new_id() -> str:
    """A short stable id. Used in widget keys, so it must survive reruns unchanged —
    which is why nodes carry one rather than being identified by list position."""
    return uuid.uuid4().hex[:12]


@dataclass
class PinnedItem:
    """One pinned chat output.

    Attributes:
        item_id: stable across reruns; forms the widget keys for this item's controls.
        question: what was asked to produce it, kept so "Generate comment" can be re-run
            later without the transcript still being around.
        heading: what the report calls it. Defaults to the question, so a pinned item is
            already labelled before the user opens the Dashboard.
        comment: the written note under the output — the chat's own commentary at pin
            time, then whatever the user edits or regenerates it into.
        sql: the statement behind it, carried for the commentary call's context.
        frame: a *copy* of the rows, or None.
        figure: the Plotly figure, or None.
        outputs: which of requirement 6.2's output types this item renders.
        png: the rasterized chart, cached after the first export so HTML and Excel
            downloads of the same report don't rasterize it twice.
        comment_revision: bumped whenever `comment` is replaced by something other than
            the user typing. The page folds it into the comment box's widget key, so a
            regenerated comment actually appears: a text area keyed on the item alone
            would keep showing what was in it before, since Streamlit widgets remember
            their own value rather than re-reading the `value=` they were given.
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
    comment_revision: int = 0

    def set_comment(self, comment: str) -> None:
        """Replaces the comment from outside the editor — currently only "Generate
        comment" — and marks it so the editor picks the new text up."""
        self.comment = comment
        self.comment_revision += 1

    def display_heading(self) -> str:
        """What to print above this item. Never empty."""
        return (self.heading or self.question or UNTITLED_ITEM).strip() or UNTITLED_ITEM

    def has_chart(self) -> bool:
        return self.figure is not None

    def has_table(self) -> bool:
        return self.frame is not None and not self.frame.empty


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

    def is_empty(self) -> bool:
        """True when there is nothing to export — no placed item anywhere."""
        return not any(subsection.items for section in self.sections for subsection in section.subsections)


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


def find_item(report: Report, item_id: str) -> PinnedItem | None:
    """The item with this id, wherever it lives — pool or any subsection."""
    for item in report.pool:
        if item.item_id == item_id:
            return item
    for section in report.sections:
        for subsection in section.subsections:
            for item in subsection.items:
                if item.item_id == item_id:
                    return item
    return None


def _detach_item(report: Report, item_id: str) -> PinnedItem | None:
    """Removes an item from wherever it currently is and returns it."""
    for position, item in enumerate(report.pool):
        if item.item_id == item_id:
            return report.pool.pop(position)
    for section in report.sections:
        for subsection in section.subsections:
            for position, item in enumerate(subsection.items):
                if item.item_id == item_id:
                    return subsection.items.pop(position)
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
