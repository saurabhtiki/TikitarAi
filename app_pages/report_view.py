"""The report workspace — arrange pinned items, preview the report, download it.

Requirements 6.3 and 6.4, rendered. Extracted from `app_pages/dashboard.py` so Task Builder
(requirement 7.3 step 5) can put the same two views over a Task's own report. Not a
`st.Page`: a page script's body executes on import, so importing one is not an option — this
follows `checks_view.py` and `setup_view.py` in being a plain module a page calls.

Two views on one `st.segmented_control`, because they are two different jobs rather than
two parts of one screen:

- **Build** — the unplaced pool on the left, the section/subsection tree on the right.
  Placing an item is a dropdown and a button, and every row of the tree carries the same
  three reordering controls, so the interaction is learned once and works at all three
  levels requirement 6.3 asks to be reorderable. Each placed item also carries one
  layout switch — "Show in columns with above" — and a run of them becomes a row of
  equal-width columns; `model.group_into_rows` is what both this view's warning and the
  exports read, so a row is never described one way here and built another way there.
- **Preview & Download** — the report read top to bottom (the same walk the exporters use,
  so what is previewed is what downloads), the CSS preset, the optional hand-edit, the two
  download buttons, and the finished HTML shown in an iframe beneath them. One view now
  answers both "is the report right" and "is the stylesheet right", since the second
  question can't be answered without the first already being visible on the same screen.

Every widget key is `db_*`, kept byte-identical to what `dashboard.py` used: the pages that
call this never render in the same run, and renaming them would have broken every saved
session and every page test for nothing.

Which report is worked on is the caller's — the `Report` is passed in. What an *empty* pool
should say is the caller's too, since the way to fill one differs per page: the Dashboard
sends the user to Chat with data, Task Builder to its own Report-Items view.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

import pandas as pd
import streamlit as st

from dashboard import custom_style
from dashboard import session as dashboard_session
from dashboard import theme_db
from dashboard.css_presets import (
    CUSTOM_PRESET,
    DEFAULT_PRESET,
    PRESET_DESCRIPTIONS,
    PRESET_OPTIONS,
    preset_css,
    rule_count,
    validate_css,
)
from dashboard.exceptions import ReportExportError
from dashboard.excel_export import build_report_workbook
from dashboard.html_export import build_html
from dashboard.model import (
    LOGO_FILE_TYPES,
    LOGO_POSITIONS,
    MAX_LOGO_HEIGHT,
    MAX_ROW_COLUMNS,
    MIN_LOGO_HEIGHT,
    PinnedItem,
    Report,
    add_section,
    add_subsection,
    assign_item,
    clear_logo,
    find_item,
    move,
    numbered_items,
    numbered_sections,
    numbered_subsections,
    remove_item,
    remove_section,
    remove_subsection,
    set_logo,
    set_logo_height,
    set_logo_position,
    subsection_choices,
    unassign_item,
    walk,
    wraps_to_new_row,
)
from dashboard.theme_db import ThemeStorageError

logger = logging.getLogger(__name__)

VIEWS = ["Build", "Preview & Download"]

# The HTML preview scrolls inside this rather than sizing to its content: a report is as
# long as it is, and letting the iframe grow to match would push the download buttons off
# the top of the screen on anything past a couple of items.
HTML_PREVIEW_HEIGHT = 700

# One icon per output type, so a pool card says what it is before it is opened.
_CHART_ICON = ":material/bar_chart:"
_TABLE_ICON = ":material/table_chart:"
_TEXT_ICON = ":material/notes:"


# --------------------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------------------


def _item_icon(item: PinnedItem) -> str:
    """The badge for a pinned item, named for the most specific thing it carries."""
    if item.has_chart():
        return _CHART_ICON
    if item.has_table():
        return _TABLE_ICON
    return _TEXT_ICON


def _item_summary(item: PinnedItem) -> str:
    """A one-line description of what a pinned item holds."""
    parts = []
    if item.has_chart():
        parts.append("chart")
    if item.has_table():
        rows, columns = item.frame.shape
        parts.append(f"{rows:,} row(s) × {columns} column(s)")
    if item.comment.strip():
        parts.append("comment")
    return " · ".join(parts) or "written answer"


def _render_item_output(item: PinnedItem, key_prefix: str) -> None:
    """The chart and table of a pinned item, in the order the export puts them."""
    if item.has_chart():
        st.plotly_chart(item.figure, key=f"{key_prefix}_chart", width="stretch")
    if item.has_table():
        st.dataframe(item.frame, key=f"{key_prefix}_frame", width="stretch", hide_index=True)


def _reorder_controls(siblings: list, index: int, key_prefix: str, what: str) -> bool:
    """Up, down and a position-jump box for one row (requirement 6.3).

    The same control set at all three levels — `move` does not care whether it is
    reordering sections, subsections or items — so learning it once is enough. The
    position box only appears once there are enough siblings for it to beat pressing
    ▲ repeatedly, and its key carries the current index so that moving a row by button
    leaves the box showing the row's new position rather than the one it typed last.

    Returns True when something moved, which the caller turns into a rerun.
    """
    moved = False
    with st.container(horizontal=True, vertical_alignment="center", key=f"{key_prefix}_reorder"):
        if st.button(
            "Up",
            key=f"{key_prefix}_up",
            icon=":material/arrow_upward:",
            disabled=index == 0,
            help=f"Move this {what} one place earlier. Disabled — it's already first."
            if index == 0
            else f"Move this {what} one place earlier.",
        ):
            moved = move(siblings, index, index - 1)

        last = len(siblings) - 1
        if st.button(
            "Down",
            key=f"{key_prefix}_down",
            icon=":material/arrow_downward:",
            disabled=index == last,
            help=f"Move this {what} one place later. Disabled — it's already last."
            if index == last
            else f"Move this {what} one place later.",
        ):
            moved = move(siblings, index, index + 1)

        if len(siblings) > 2:
            chosen = st.number_input(
                "Position",
                min_value=1,
                max_value=len(siblings),
                value=index + 1,
                step=1,
                key=f"{key_prefix}_position_{index}",
                label_visibility="collapsed",
                width=110,
                help=f"Type a position to send this {what} straight there.",
            )
            if int(chosen) != index + 1:
                moved = move(siblings, index, int(chosen) - 1)

    return moved


# --------------------------------------------------------------------------------------
# Build — the unplaced pool
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmptyPool:
    """What an empty pool says, and the one button under it.

    The message and the way to fill it differ per page, and both belong to the caller: this
    module has no business knowing that the Dashboard is fed by Chat with data. `on_click`
    is called when the button is pressed, because a page switch and a view switch are not
    the same move.
    """

    message: str
    button_label: str
    on_click: Callable[[], None]
    icon: str = ":material/forum:"
    button_help: str = "Go to where items are made, then pin one to bring it back here."


def _render_pool(report: Report, empty_pool: EmptyPool | None = None) -> None:
    """Everything pinned but not yet placed (requirement 6.1 step 4)."""
    st.markdown(f"##### Unplaced ({len(report.pool)})")

    if not report.pool:
        st.info(
            empty_pool.message if empty_pool is not None else "Nothing pinned yet.",
            icon=":material/push_pin:",
        )
        # A button and a callback rather than `st.page_link`, matching `data_cleaner.py`'s
        # cross-page jump: the link element resolves against the `st.navigation` registry as
        # the page renders, which is one more thing to be broken on a page that is otherwise
        # perfectly usable on its own.
        if empty_pool is not None and st.button(
            empty_pool.button_label,
            key="db_go_to_chat",
            icon=empty_pool.icon,
            type="primary",
            help=empty_pool.button_help,
        ):
            empty_pool.on_click()
        return

    choices = subsection_choices(report)
    if not choices:
        st.warning(
            "Add a section on the right first — pinned items are placed into subsections.",
            icon=":material/info:",
        )

    labels = dict(choices)
    for item in list(report.pool):
        with st.container(border=True, key=f"db_pool_{item.item_id}"):
            st.markdown(f"{_item_icon(item)} **{item.display_heading()}**")
            st.caption(_item_summary(item))

            target = None
            if choices:
                target = st.selectbox(
                    "Place in",
                    options=[node_id for node_id, _ in choices],
                    format_func=lambda node_id: labels[node_id],
                    key=f"db_place_target_{item.item_id}",
                    help="Which subsection this item should go into. You can move it again later.",
                )

            with st.container(horizontal=True, key=f"db_pool_actions_{item.item_id}"):
                if choices and st.button(
                    "Place",
                    key=f"db_place_{item.item_id}",
                    icon=":material/playlist_add:",
                    type="primary",
                    help="Add this item to the chosen subsection.",
                ):
                    assign_item(report, item.item_id, target)
                    st.rerun(scope="app")

                if st.button(
                    "Look",
                    key=f"db_pool_preview_{item.item_id}",
                    icon=":material/visibility:",
                    help="Open this item full size to check it's the one you meant.",
                ):
                    dashboard_session.open_dialog("preview", {"item_id": item.item_id})
                    st.rerun(scope="app")

                # No Discard for an item a producer owns. `source_id` is set only by things
                # that re-save their item — a criteria in `checks/` — and those keep their
                # own Remove beside the Save that created it, where the rule it belongs to
                # is on screen. Discarding it here would leave that page still showing
                # "Saved to report" for something no longer in the report, and the next
                # refine would silently pin a second copy. An unplaced item is not in the
                # report anyway: the exports walk the section tree only, so leaving one in
                # the pool costs nothing.
                if item.source_id is None and st.button(
                    "Discard",
                    key=f"db_pool_discard_{item.item_id}",
                    icon=":material/delete:",
                    help="Throw this item away. It can't be recovered — pin the answer again to get it back.",
                ):
                    remove_item(report, item.item_id)
                    st.rerun(scope="app")

            if item.source_id is not None:
                st.caption(
                    ":grey[Leave it unplaced to keep it out of the report, or remove it from "
                    "the Checks tab that made it.]"
                )


# --------------------------------------------------------------------------------------
# Build — the report structure
# --------------------------------------------------------------------------------------


def _render_tree(report: Report) -> None:
    with st.container(horizontal=True, vertical_alignment="center", key="db_tree_header"):
        st.markdown("##### Report structure")
        if st.button(
            "Add section",
            key="db_add_section",
            icon=":material/add:",
            type="primary",
            help="Add a numbered section. It starts with one subsection, so you can place items into it right away.",
        ):
            add_section(report)
            st.rerun(scope="app")

    if not report.sections:
        st.info(
            "No sections yet. Add one to start building the report — sections and "
            "subsections are numbered automatically from their order.",
            icon=":material/account_tree:",
        )
        return

    for index, (number, section) in enumerate(numbered_sections(report)):
        placed = sum(len(subsection.items) for subsection in section.subsections)
        with st.expander(
            f"{number}. {section.name}  ·  {placed} item(s)",
            expanded=True,
            icon=":material/folder:",
        ):
            _render_section_body(report, section, number, index)


def _render_section_body(report: Report, section, number: str, index: int) -> None:
    with st.container(horizontal=True, vertical_alignment="bottom", key=f"db_section_bar_{section.node_id}"):
        section.name = st.text_input(
            "Section name",
            value=section.name,
            key=f"db_section_name_{section.node_id}",
            label_visibility="collapsed",
            help="What this section is called in the report. The number comes from its position.",
        )
        if _reorder_controls(report.sections, index, f"db_section_{section.node_id}", "section"):
            st.rerun(scope="app")
        if st.button(
            "Delete",
            key=f"db_section_delete_{section.node_id}",
            icon=":material/delete:",
            help="Remove this section. Any items inside it go back to the unplaced pool rather than being lost.",
        ):
            rescued = remove_section(report, section.node_id)
            if rescued:
                st.toast(f"{rescued} item(s) returned to Unplaced.", icon=":material/undo:")
            st.rerun(scope="app")

    if st.button(
        "Add subsection",
        key=f"db_add_subsection_{section.node_id}",
        icon=":material/add:",
        help="Add another subsection to this section. Each subsection becomes its own sheet in the Excel export.",
    ):
        add_subsection(section)
        st.rerun(scope="app")

    for sub_index, (sub_number, subsection) in enumerate(numbered_subsections(section, number)):
        with st.container(border=True, key=f"db_subsection_{subsection.node_id}"):
            _render_subsection_body(report, section, subsection, sub_number, sub_index)


def _render_subsection_body(report: Report, section, subsection, number: str, index: int) -> None:
    with st.container(horizontal=True, vertical_alignment="bottom", key=f"db_sub_bar_{subsection.node_id}"):
        st.markdown(f"**{number}**")
        subsection.name = st.text_input(
            "Subsection name",
            value=subsection.name,
            key=f"db_sub_name_{subsection.node_id}",
            label_visibility="collapsed",
            help="What this subsection is called. It also names its sheet in the Excel export.",
        )
        if _reorder_controls(section.subsections, index, f"db_sub_{subsection.node_id}", "subsection"):
            st.rerun(scope="app")
        if st.button(
            "Delete",
            key=f"db_sub_delete_{subsection.node_id}",
            icon=":material/delete:",
            help="Remove this subsection. Any items inside it go back to the unplaced pool rather than being lost.",
        ):
            rescued = remove_subsection(report, subsection.node_id)
            if rescued:
                st.toast(f"{rescued} item(s) returned to Unplaced.", icon=":material/undo:")
            st.rerun(scope="app")

    if not subsection.items:
        st.caption(":grey[Empty — place a pinned item here from the Unplaced list.]")
        return

    # Numbered here too, from the same `numbered_items`, so the number beside an item while
    # it is being arranged is the number it will carry in the download.
    for item_index, (item_number, item) in enumerate(numbered_items(subsection.items, number)):
        with st.container(border=True, key=f"db_item_{item.item_id}"):
            _render_item_body(report, subsection, item, item_index, item_number)


def _render_item_body(report: Report, subsection, item: PinnedItem, index: int, number: str) -> None:
    """One placed item: its heading, its comment, and where it goes next.

    The output itself is not drawn here — a tree of a dozen full-size Plotly charts is
    unusable as a structure editor. The badge and one-line summary say what it is, the
    **Look** button opens it full size, and Preview shows the whole report properly.
    """
    with st.container(horizontal=True, vertical_alignment="bottom", key=f"db_item_bar_{item.item_id}"):
        st.markdown(f"{_item_icon(item)} **{number}**")
        item.heading = st.text_input(
            "Heading",
            value=item.heading or item.question,
            key=f"db_item_heading_{item.item_id}",
            label_visibility="collapsed",
            help="The heading printed above this item in the report. It starts as the question you asked.",
        )
        if _reorder_controls(subsection.items, index, f"db_item_{item.item_id}", "item"):
            st.rerun(scope="app")

    st.caption(_item_summary(item))

    item.comment = st.text_area(
        "Comment",
        value=item.comment,
        key=f"db_item_comment_{item.item_id}",
        height=90,
        label_visibility="collapsed",
        placeholder="A note printed under this item.",
        help="Printed under the chart and table in both exports. It starts as the answer's own commentary from the chat — edit it freely.",
    )

    _render_column_toggle(subsection, item, index)

    with st.container(horizontal=True, key=f"db_item_actions_{item.item_id}"):
        choices = [pair for pair in subsection_choices(report) if pair[0] != subsection.node_id]
        if choices:
            labels = dict(choices)
            destination = st.selectbox(
                "Move to Follwoing Subsection",
                options=[node_id for node_id, _ in choices],
                format_func=lambda node_id: labels[node_id],
                key=f"db_item_move_target_{item.item_id}",
                label_visibility="visible",
                help="Click on Move to send this item to the chosen subsection.",
            )
            if st.button(
                "Move",
                key=f"db_item_move_{item.item_id}",
                icon=":material/drive_file_move:",
                help="Move this item to the subsection chosen on the left.",
            ):
                assign_item(report, item.item_id, destination)
                st.rerun(scope="app")

        if st.button(
            "Look",
            key=f"db_item_preview_{item.item_id}",
            icon=":material/visibility:",
            help="Open this item full size.",
        ):
            dashboard_session.open_dialog("preview", {"item_id": item.item_id})
            st.rerun(scope="app")

        if st.button(
            "Unplace",
            key=f"db_item_unplace_{item.item_id}",
            icon=":material/undo:",
            help="Send this item back to the unplaced pool without deleting it.",
        ):
            unassign_item(report, item.item_id)
            st.rerun(scope="app")


def _render_column_toggle(subsection, item: PinnedItem, index: int) -> None:
    """The one control behind side-by-side layout: "show in columns with the item above".

    Off by default, so an untouched report reads exactly as it always did — one item per
    row. Turning it on for a run of items puts that run in one row of equal columns, up to
    `MAX_ROW_COLUMNS`.

    The flag stays with the item when it is reordered or moved to another subsection,
    which is deliberate: it says something about the item ("I'm happy to share a row"),
    not about a position. What it *cannot* say is which item it will end up beside, so the
    two cases where it is asked for and not honoured — nothing above to join, or a full row
    above — say so under the switch rather than silently doing nothing.
    """
    first_in_subsection = index == 0

    item.column_with_previous = st.toggle(
        "Show in columns with above",
        value=item.column_with_previous and not first_in_subsection,
        key=f"db_item_columns_{item.item_id}",
        disabled=first_in_subsection,
        help="Nothing above this item to sit beside — it always starts a row."
        if first_in_subsection
        else "Show this item beside the one above it, in equal-width columns, instead of "
        f"underneath it. Up to {MAX_ROW_COLUMNS} items can share a row.",
    )

    if wraps_to_new_row(subsection.items, index):
        st.caption(
            f":orange[The row above already holds {MAX_ROW_COLUMNS} items, so this one "
            "starts a new row.]"
        )


# --------------------------------------------------------------------------------------
# The header logo
# --------------------------------------------------------------------------------------


# Which upload has already been stored on the report. `st.file_uploader` hands back the same
# file on every rerun until it is cleared, so without this the bytes would be re-read and
# re-validated on every slider drag and every button press on the page.
LOGO_APPLIED_KEY = "db_logo_applied_id"

# How wide the on-page thumbnail is drawn. The export uses `report.logo_height`; this is
# just big enough to see what was uploaded.
LOGO_THUMBNAIL_WIDTH = 160


def _absorb_logo_upload(report: Report, upload) -> None:
    """Stores a newly uploaded picture on the report, once.

    Reading the file is I/O and the picture may be anything the user picked, so a failure
    here is shown and swallowed: an unreadable upload costs the logo, never the report.
    """
    if upload is None:
        st.session_state.pop(LOGO_APPLIED_KEY, None)
        return

    if st.session_state.get(LOGO_APPLIED_KEY) == upload.file_id:
        return

    try:
        data = upload.getvalue()
    except OSError as error:
        logger.exception("Could not read the uploaded logo '%s'.", upload.name)
        st.error(f"That picture couldn't be read ({error}).", icon=":material/error:")
        return

    problems = set_logo(report, data, upload.name)
    # Recorded either way, so a refused picture says why once instead of on every rerun.
    st.session_state[LOGO_APPLIED_KEY] = upload.file_id

    if problems:
        st.error("That picture wasn't used as the logo:", icon=":material/error:")
        for problem in problems:
            st.markdown(f"- {problem}")
        return

    st.rerun(scope="app")


def _render_logo_thumbnail(report: Report) -> None:
    """The stored logo, shown small.

    Drawing it means decoding it, and the bytes are a file the user picked — `set_logo`
    checks the extension and the size, not that the contents are really a picture. A file
    named `.png` that isn't one must cost the thumbnail and say so, not take the page and
    the arrangement on it down.
    """
    try:
        st.image(report.logo, width=LOGO_THUMBNAIL_WIDTH)
    except Exception:
        logger.exception("Could not display the stored report logo.")
        st.warning(
            "That file couldn't be read as a picture, so it won't show in the report "
            "either. Remove it and upload a PNG or JPG.",
            icon=":material/broken_image:",
        )


def _render_logo_controls(report: Report) -> None:
    """Upload a logo, then say how big it is and where it sits.

    Under the title box rather than in the Style panel, because a logo is part of the report
    header the way the title is — it is saved with the Task and it shows in every style,
    where a preset is a look chosen at download time.
    """
    with st.expander("Logo", icon=":material/image:", expanded=report.has_logo()):
        upload = st.file_uploader(
            "Logo picture",
            type=list(LOGO_FILE_TYPES),
            key="db_logo_upload",
            help="A small picture printed beside the report title. It is embedded in the download, so the file still shows it offline.",
        )
        _absorb_logo_upload(report, upload)

        if not report.has_logo():
            st.caption(":grey[No logo yet. The report prints its title on its own.]")
            return

        _render_logo_thumbnail(report)

        with st.container(horizontal=True, vertical_alignment="bottom", key="db_logo_controls"):
            set_logo_position(
                report,
                st.segmented_control(
                    "Position",
                    options=list(LOGO_POSITIONS),
                    default=report.logo_position,
                    required=True,
                    key="db_logo_position",
                    format_func=str.capitalize,
                    help="Where the logo sits relative to the title.",
                ),
            )
            set_logo_height(
                report,
                st.slider(
                    "Height",
                    min_value=MIN_LOGO_HEIGHT,
                    max_value=MAX_LOGO_HEIGHT,
                    value=report.logo_height,
                    key="db_logo_height",
                    format="%dpx",
                    help="How tall the logo prints in the report. The width follows automatically.",
                ),
            )
            if st.button(
                "Remove",
                key="db_logo_remove",
                icon=":material/delete:",
                help="Print the report without a logo.",
            ):
                clear_logo(report)
                st.session_state.pop(LOGO_APPLIED_KEY, None)
                st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------------------


EMPTY_PREVIEW = "Nothing placed yet. Switch to **Build**, add a section, and place a pinned item into it."
EMPTY_DOWNLOAD = "Place at least one pinned item into a subsection before downloading."


def _render_preview(report: Report, empty_message: str = EMPTY_PREVIEW) -> None:
    """The report read top to bottom, in export order.

    Walked with the same `walk(report)` both exporters use, so the order and numbering
    here cannot drift from what downloads. The styling does not carry over — the presets
    are for the exported file, and Streamlit renders in the app's own theme.

    `empty_message` is the caller's, because "place an item" is the answer on the Dashboard
    and Task Builder and is not the answer on a screen with no Build view: a run's report is
    empty because the run produced nothing, and telling the user to go and arrange something
    would send them looking for a control that isn't there.
    """
    sections = walk(report)
    if not sections:
        st.info(empty_message, icon=":material/preview:")
        return

    st.caption(
        ":grey[Order, numbering and content are exactly what downloads, rendered in the "
        "app's own theme. To see the chosen style, use the preview under **Download**.]"
    )

    if report.has_logo():
        _render_logo_thumbnail(report)

    for section in sections:
        st.subheader(f"{section.number}. {section.name}", divider="grey")
        for subsection in section.subsections:
            st.markdown(f"##### {subsection.number} {subsection.name}")
            # Grouped and numbered through the model's own `numbered_rows()`, the same call
            # the HTML export makes, so a row that reads as four columns here is four
            # columns there and "2.1.3" names the same item in both.
            for row in subsection.numbered_rows():
                if len(row) == 1:
                    _render_preview_item(*row[0])
                    continue
                for column, (number, item) in zip(st.columns(len(row), gap="medium"), row):
                    with column:
                        _render_preview_item(number, item)


def _render_preview_item(number: str, item: PinnedItem) -> None:
    """One item as the report reads it — number, heading, output, comment."""
    st.markdown(f"**{number} {item.display_heading()}**")
    _render_item_output(item, f"db_preview_{item.item_id}")
    if item.comment.strip():
        st.markdown(f":grey[_{item.comment.strip()}_]")


# --------------------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------------------


def style_css(preset: str) -> str:
    """The stylesheet the export should use, given the chosen preset.

    One place decides it, because three now ask: the Download view, the Custom editor's live
    preview, and the CSS editor that opens pre-filled with it. **Custom** is generated from
    the user's own settings rather than looked up, which is the only thing that separates it
    from the three built-in presets.
    """
    if preset == CUSTOM_PRESET:
        return custom_style.build_css(dashboard_session.custom_style())
    return preset_css(preset)


def _render_download(report: Report, empty_message: str = EMPTY_DOWNLOAD) -> None:
    st.markdown("##### Style")

    stored_preset = dashboard_session.selected_preset(DEFAULT_PRESET)
    preset = st.segmented_control(
        "Style",
        options=PRESET_OPTIONS,
        default=stored_preset,
        required=True,
        key="db_preset_picker",
        label_visibility="collapsed",
        help="How the downloaded HTML looks. The Excel export always uses Excel's own formatting.",
    )
    if preset and preset != stored_preset:
        dashboard_session.set_selected_preset(preset)
        st.rerun(scope="app")

    preset = preset or stored_preset
    st.caption(PRESET_DESCRIPTIONS.get(preset, ""))

    if preset == CUSTOM_PRESET:
        _render_customize_button()
        if _style_panel_open():
            _render_style_editor(report)

    css = dashboard_session.accepted_css() or style_css(preset)
    if dashboard_session.accepted_css():
        st.caption(f":green[Using your edited stylesheet — {rule_count(css)} rule(s).]")

    _render_css_editor(preset, css)

    st.markdown("##### Download")
    if report.is_empty():
        st.info(empty_message, icon=":material/download:")
        return

    html, workbook = _build_exports(report, css)
    _render_download_buttons(report, html, workbook)
    _render_html_preview(html)


def _render_css_editor(preset: str, css: str) -> None:
    """The optional hand-edit, accepted only after validation (requirement 6.4)."""
    with st.expander("Edit the stylesheet", icon=":material/code:"):
        # Keyed on the preset so switching preset reloads the box with that preset's CSS
        # instead of leaving the previous one's rules in an editor labelled otherwise.
        edited = st.text_area(
            "Stylesheet",
            value=css,
            key=f"db_css_editor_{preset}",
            height=320,
            label_visibility="collapsed",
            help="Plain CSS, embedded in the downloaded HTML. It must stay self-contained — no @import and no web fonts.",
        )

        with st.container(horizontal=True, vertical_alignment="center", key="db_css_actions"):
            check = st.button(
                "Check and apply",
                key="db_css_apply",
                icon=":material/check:",
                type="primary",
                help="Validate this stylesheet and use it for the HTML download.",
            )
            if st.button(
                "Back to the preset",
                key="db_css_reset",
                icon=":material/restart_alt:",
                disabled=dashboard_session.accepted_css() is None,
                help="Discard your edits and go back to the preset's own stylesheet.",
            ):
                dashboard_session.set_accepted_css(None)
                st.rerun(scope="app")

        if check:
            problems = validate_css(edited)
            if problems:
                # Rejecting keeps the previously accepted stylesheet in force, so a broken
                # edit costs the change and never the download.
                st.error(
                    "This stylesheet wasn't applied, so the previous one is still in use:",
                    icon=":material/error:",
                )
                for problem in problems:
                    st.markdown(f"- {problem}")
                return
            dashboard_session.set_accepted_css(edited)
            st.success(f"Applied — {rule_count(edited)} rule(s).", icon=":material/check_circle:")


def _build_exports(report: Report, css: str) -> tuple[str | None, bytes | None]:
    """Both exports, built before anything that needs them.

    `st.download_button` needs its bytes up front, so the work happens here rather than in
    a callback — the same shape `data_cleaner.py` uses — and the HTML string is handed back
    so the preview below the buttons shows the very file that downloads rather than a
    second rendering of it. Charts are rasterized once and cached on the item, so the
    second format and every later rerun are cheap.

    Either half can come back None. One export failing is not a reason to withhold the
    other, so each is caught on its own and its button simply isn't drawn.
    """
    with st.spinner("Building your report…"):
        try:
            html = build_html(report, css)
        except ReportExportError as error:
            logger.exception("Could not build the HTML report.")
            st.error(str(error), icon=":material/error:")
            html = None

        try:
            workbook = build_report_workbook(report)
        except ReportExportError as error:
            logger.exception("Could not build the report workbook.")
            st.error(str(error), icon=":material/error:")
            workbook = None

    return html, workbook


def _render_html_preview(html: str | None) -> None:
    """The finished HTML, shown as itself (requirement 6.4's presets are the point of it).

    An iframe rather than `st.html`: the report carries a whole page's stylesheet — `body`,
    `table`, `h1` — which injected into the app would restyle the app itself. An iframe
    gives it its own document, which is exactly the context it was written for, so what is
    shown here is the downloaded file rendering rather than an impression of it.

    `st.iframe` warns against untrusted HTML, and this is not untrusted: every user string
    in it is escaped by the template, and the one value that isn't — the stylesheet — is
    the string `validate_css` exists to screen. Nothing else reaches the page unescaped.
    """
    if html is None:
        return

    with st.expander("Preview the HTML", icon=":material/preview:", expanded=True):
        st.caption(
            ":grey[This is the file itself, styling and all. Scroll it here, then press "
            "Download HTML above to keep it.]"
        )
        st.iframe(html, height=HTML_PREVIEW_HEIGHT)


def _render_download_buttons(report: Report, html: str | None, workbook: bytes | None) -> None:
    """The two download buttons, named after the report."""
    file_stem = (report.title or "dashboard").strip() or "dashboard"

    with st.container(horizontal=True, key="db_download_buttons"):
        if html is not None:
            st.download_button(
                "Download HTML",
                data=html.encode("utf-8"),
                file_name=f"{file_stem}.html",
                mime="text/html",
                key="db_download_html",
                icon=":material/download:",
                type="primary",
                on_click="ignore",
                help="One self-contained file — charts and styling included, nothing loaded from the internet.",
            )
        if workbook is not None:
            st.download_button(
                "Download Excel",
                data=workbook,
                file_name=f"{file_stem}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="db_download_excel",
                icon=":material/download:",
                type="primary",
                on_click="ignore",
                help="One sheet per subsection, with charts as pictures and the full data — no row limit.",
            )


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def _dismiss_dialog() -> None:
    """Clears the open-dialog flag when a dialog is closed without our Close button.

    An `st.dialog` can be dismissed by clicking outside it, pressing its "X" or hitting
    `ESC`, none of which run the Close button's code. Left unhandled, the flag stays set
    and the *next* rerun for any reason — pressing Place, switching view, editing a
    heading — reopens the same preview, because `on_dismiss` defaults to "ignore" and
    never reruns at all. The chat page's `_dismiss_dialog` exists for the same reason.
    """
    dashboard_session.close_dialog()


@st.dialog("Pinned item", width="large", on_dismiss=_dismiss_dialog)
def _preview_item_dialog(payload: dict) -> None:
    item = find_item(dashboard_session.get_report(), payload.get("item_id", ""))
    if item is None:
        st.warning("That item is no longer in the report.", icon=":material/info:")
    else:
        st.markdown(f"**{item.display_heading()}**")
        st.caption(f"Asked: {item.question}" if item.question else "")
        _render_item_output(item, f"db_dialog_{item.item_id}")
        if item.comment.strip():
            st.markdown(f":grey[_{item.comment.strip()}_]")
        if item.sql:
            with st.expander("SQL that ran", icon=":material/code:"):
                st.code(item.sql, language="sql")

    if st.button("Close", key="db_dialog_close", icon=":material/close:", help="Close this preview."):
        dashboard_session.close_dialog()
        st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# The Custom style editor
# --------------------------------------------------------------------------------------


# The settings being edited *in the editor*, kept apart from the ones in force. The page
# reruns on every slider drag, and an editor that wrote straight through would restyle the
# report behind it while the user was still deciding — and leave a half-made style in force
# if they navigated away.
STYLE_DRAFT_KEY = "db_style_draft"

# Whether the style editor is showing. It is a panel on the page rather than a dialog: a
# colour picker opens its own panel, and inside a dialog that panel could be seen but not
# used, so no colour could be chosen.
STYLE_PANEL_KEY = "db_style_panel"

# Tall enough to show the header, an item and a table without scrolling, short enough to
# sit beside the controls.
STYLE_PREVIEW_HEIGHT = 460


def _copy_settings(settings: custom_style.StyleSettings) -> custom_style.StyleSettings:
    """A detached copy, made by round-tripping through the stored form.

    Round-tripped rather than deep-copied so the copy is exactly what a saved theme would
    reload as — if a value can't survive storage, the editor shows that straight away rather
    than after the user has saved it and come back.
    """
    return custom_style.from_dict(custom_style.to_dict(settings))


def _style_draft() -> custom_style.StyleSettings:
    """The settings the editor is editing, seeded from the ones in force."""
    draft = st.session_state.get(STYLE_DRAFT_KEY)
    if not isinstance(draft, custom_style.StyleSettings):
        draft = _copy_settings(dashboard_session.custom_style())
        st.session_state[STYLE_DRAFT_KEY] = draft
    return draft


def _set_style_draft(draft: custom_style.StyleSettings) -> None:
    st.session_state[STYLE_DRAFT_KEY] = draft


def _clear_style_widgets() -> None:
    """Forgets every widget the editor owns, so the next draw starts from the draft.

    Streamlit keeps a widget's value under its key, and that stored value beats the `value=`
    passed to it. Without this, loading a saved theme would redraw the sliders at the
    *previous* theme's numbers.
    """
    for key in [key for key in st.session_state if str(key).startswith("db_style_")]:
        # Everything the panel draws shares this prefix — and so do the draft and the
        # open/closed flag, which must survive: the draft is what the redrawn widgets take
        # their values from, and clearing the flag would shut the panel mid-edit.
        if key not in (STYLE_DRAFT_KEY, STYLE_PANEL_KEY):
            st.session_state.pop(key, None)


def _style_panel_open() -> bool:
    return bool(st.session_state.get(STYLE_PANEL_KEY))


def _render_customize_button() -> None:
    """Shows or hides the style editor, seeding the draft when it opens."""
    open_now = _style_panel_open()
    if st.button(
        "Hide the style editor" if open_now else "Customize style",
        key="db_style_open",
        icon=":material/palette:",
        help="Set the fonts, sizes, colours and borders the downloaded report uses.",
    ):
        if open_now:
            st.session_state[STYLE_PANEL_KEY] = False
        else:
            _set_style_draft(_copy_settings(dashboard_session.custom_style()))
            _clear_style_widgets()
            st.session_state[STYLE_PANEL_KEY] = True
        st.rerun(scope="app")


def _render_page_controls(draft: custom_style.StyleSettings) -> custom_style.StyleSettings:
    """The five settings that apply to the whole page."""
    st.markdown("**Page**")

    fonts = list(custom_style.FONT_STACKS)
    font = st.selectbox(
        "Font",
        options=fonts,
        index=fonts.index(draft.font) if draft.font in fonts else 0,
        key="db_style_font",
        help="Only fonts already on the reader's machine — the report has to open offline.",
    )
    base_font_size = st.slider(
        "Base text size",
        min_value=custom_style.BASE_FONT_RANGE[0],
        max_value=custom_style.BASE_FONT_RANGE[1],
        value=draft.base_font_size,
        key="db_style_base_size",
        format="%dpx",
        help="Everything else is sized from this, so it moves the whole report at once.",
    )
    content_width = st.slider(
        "Page width",
        min_value=custom_style.CONTENT_WIDTH_RANGE[0],
        max_value=custom_style.CONTENT_WIDTH_RANGE[1],
        value=draft.content_width,
        step=20,
        key="db_style_width",
        format="%dpx",
        help="How wide the printed area is. Wider fits more table columns; narrower reads better.",
    )
    # Columns rather than a horizontal container: a colour picker opens a panel that a
    # flex row clips, which leaves the swatch showing but the colour impossible to choose.
    behind_column, page_column = st.columns(2, vertical_alignment="bottom")
    with behind_column:
        page_background = st.color_picker(
            "Behind the page",
            value=draft.page_background,
            key="db_style_page_bg",
            help="The colour around the report area.",
        )
    with page_column:
        content_background = st.color_picker(
            "The page itself",
            value=draft.content_background,
            key="db_style_content_bg",
            help="The colour the report is printed on.",
        )

    return replace(
        draft,
        font=font,
        base_font_size=base_font_size,
        content_width=content_width,
        page_background=page_background,
        content_background=content_background,
    )


def _render_element_controls(draft: custom_style.StyleSettings) -> custom_style.StyleSettings:
    """One element at a time, chosen from a dropdown.

    One at a time rather than six panels stacked, because every element takes the same four
    or seven controls: showing them all at once is a wall of near-identical sliders, and the
    live preview beside them is what says which element is being changed.
    """
    st.markdown("**Element**")

    specs = {spec.label: spec for spec in custom_style.ELEMENT_SPECS}
    label = st.selectbox(
        "What to change",
        options=list(specs),
        key="db_style_element",
        help="Pick a part of the report, then set how it looks.",
    )
    spec = specs[label]
    element = draft.element(spec.key)
    st.caption(f":grey[{spec.hint}]")

    font_size = st.slider(
        "Text size",
        min_value=custom_style.ELEMENT_FONT_RANGE[0],
        max_value=custom_style.ELEMENT_FONT_RANGE[1],
        value=float(element.font_size),
        step=0.05,
        key=f"db_style_{spec.key}_size",
        format="%.2fx",
        help="A multiple of the base text size, so it stays in proportion if you change that.",
    )

    # See `_render_page_controls`: colour pickers need a column, not a flex row.
    text_column, fill_column, background_column = st.columns(
        [3, 2, 3], vertical_alignment="bottom"
    )
    with text_column:
        text_colour = st.color_picker(
            "Text colour",
            value=element.text_colour,
            key=f"db_style_{spec.key}_text",
            help="The colour of the words themselves.",
        )
    with fill_column:
        use_background = st.checkbox(
            "Fill",
            value=bool(element.background_colour),
            key=f"db_style_{spec.key}_use_bg",
            help="Paint a colour behind this element. Off leaves the page showing through.",
        )
    with background_column:
        background_colour = st.color_picker(
            spec.background_label,
            value=element.background_colour or "#eef1f5",
            key=f"db_style_{spec.key}_bg",
            disabled=not use_background,
            help="The colour behind this element. Padding is added for you, so the text is never cramped.",
        )

    border_width = element.border_width
    border_colour = element.border_colour
    border_radius = element.border_radius
    if spec.bordered:
        width_column, colour_column = st.columns([3, 2], vertical_alignment="bottom")
        with width_column:
            border_width = st.slider(
                "Border",
                min_value=custom_style.BORDER_WIDTH_RANGE[0],
                max_value=custom_style.BORDER_WIDTH_RANGE[1],
                value=int(element.border_width),
                key=f"db_style_{spec.key}_border_width",
                format="%dpx",
                help="0 means no border.",
            )
        with colour_column:
            border_colour = st.color_picker(
                "Border colour",
                value=element.border_colour,
                key=f"db_style_{spec.key}_border_colour",
                disabled=border_width == 0,
                help="The colour of the border line.",
            )
        border_radius = st.slider(
            "Rounded corners",
            min_value=custom_style.BORDER_RADIUS_RANGE[0],
            max_value=custom_style.BORDER_RADIUS_RANGE[1],
            value=int(element.border_radius),
            key=f"db_style_{spec.key}_radius",
            format="%dpx",
            help="Only applies where the border goes all the way round.",
        )

    return draft.with_element(
        spec.key,
        font_size=font_size,
        text_colour=text_colour,
        background_colour=background_colour if use_background else custom_style.NO_BACKGROUND,
        border_width=border_width,
        border_colour=border_colour,
        border_radius=border_radius,
    )


def _sample_report(report: Report) -> Report:
    """A one-item stand-in report, for the editor's live preview.

    Built rather than borrowed: the real report may be empty, may be a hundred items long,
    and rasterizing its charts on every slider drag would make the editor unusable. The
    title and the logo *are* borrowed, because those are the parts of the header the style
    has to sit around.
    """
    sample = Report(
        title=(report.title or "").strip() or "Report title",
        logo=report.logo,
        logo_mime=report.logo_mime,
        logo_height=report.logo_height,
        logo_position=report.logo_position,
    )
    section = add_section(sample, "Section heading")
    subsection = section.subsections[0]
    subsection.name = "Subsection heading"
    subsection.items = [
        PinnedItem(
            heading="Item heading",
            comment="The note under an item is Paragraph text.",
            frame=pd.DataFrame({"Region": ["North", "South", "East"], "Sales": [1200, 940, 1580]}),
        )
    ]
    return sample


def _render_style_preview(draft: custom_style.StyleSettings, report: Report) -> None:
    """The sample report rendered with the draft stylesheet, and what it warns about.

    The generated CSS is offered underneath rather than instead of the picture: most people
    cannot read a stylesheet, and the ones who can want to copy it into the editor and
    finish it by hand.
    """
    st.markdown("**Preview**")

    css = custom_style.build_css(draft)
    try:
        html = build_html(_sample_report(report), css)
    except ReportExportError as error:
        logger.exception("Could not render the custom style preview.")
        st.error(str(error), icon=":material/error:")
        return

    st.iframe(html, height=STYLE_PREVIEW_HEIGHT)

    for warning in custom_style.contrast_warnings(draft):
        st.warning(warning, icon=":material/contrast:")

    with st.expander("The stylesheet this makes", icon=":material/code:"):
        st.code(css, language="css")


def _saved_themes(user_id: int) -> list[dict]:
    """The account's saved themes, or an empty list if they can't be read.

    An empty list rather than a raise: the shelf is a convenience beside the controls, and a
    database that is briefly unavailable must not take the whole editor with it.
    """
    try:
        return theme_db.list_themes(user_id)
    except ThemeStorageError as error:
        logger.exception("Could not list saved report themes.")
        st.error(str(error), icon=":material/error:")
        return []


def _render_theme_shelf(draft: custom_style.StyleSettings) -> None:
    """Save this style under a name, or load or delete one saved earlier.

    Hidden entirely when there is no signed-in account to own the themes — every row is
    scoped to a `user_id`, and a shelf that cannot be written to is a control that does
    nothing.
    """
    user_id = st.session_state.get("user_id")
    if not user_id:
        return

    with st.expander("Saved themes", icon=":material/bookmark:"):
        with st.container(horizontal=True, vertical_alignment="bottom", key="db_style_save_row"):
            name = st.text_input(
                "Theme name",
                key="db_style_theme_name",
                placeholder="e.g. Company blue",
                help="Save these settings under a name and reuse them on the next report.",
            )
            if st.button(
                "Save",
                key="db_style_theme_save",
                icon=":material/save:",
                help="Save the settings as they are now. A theme with this name is replaced.",
            ):
                try:
                    theme_db.save_theme(user_id, name, draft)
                except ThemeStorageError as error:
                    st.error(str(error), icon=":material/error:")
                else:
                    st.success(f"Saved '{name.strip()}'.", icon=":material/check_circle:")

        themes = _saved_themes(user_id)
        if not themes:
            st.caption(":grey[No saved themes yet.]")
            return

        theme_ids = {theme["name"]: theme["theme_id"] for theme in themes}
        chosen = st.selectbox(
            "Saved theme",
            options=list(theme_ids),
            key="db_style_theme_pick",
            help="Pick a theme, then load it into the controls or delete it.",
        )

        with st.container(horizontal=True, key="db_style_theme_actions"):
            if st.button(
                "Load",
                key="db_style_theme_load",
                icon=":material/download:",
                help="Put this theme into the controls. Nothing changes in the report until you press Apply.",
            ):
                try:
                    loaded = theme_db.load_theme(theme_ids[chosen], user_id)
                except ThemeStorageError as error:
                    st.error(str(error), icon=":material/error:")
                else:
                    _set_style_draft(loaded)
                    # The sliders still hold the previous theme's values under their keys,
                    # and a stored widget value beats the `value=` passed to it — so they
                    # are dropped and redrawn from the loaded draft.
                    _clear_style_widgets()
                    st.rerun(scope="app")

            if st.button(
                "Delete",
                key="db_style_theme_delete",
                icon=":material/delete:",
                help="Remove this saved theme. The style you are editing is not affected.",
            ):
                try:
                    theme_db.delete_theme(theme_ids[chosen], user_id)
                except ThemeStorageError as error:
                    st.error(str(error), icon=":material/error:")
                else:
                    st.rerun(scope="app")


def _apply_style(draft: custom_style.StyleSettings) -> None:
    """Puts the draft into force and closes the editor.

    The CSS editor's text box is dropped on the way out: it is keyed on the preset name, so
    its stored value would go on showing the stylesheet the *previous* settings generated,
    under a box labelled Custom.
    """
    dashboard_session.set_custom_style(draft)
    # This also clears any hand-edited stylesheet, which is what makes the new settings
    # visible at all — an accepted edit outranks the preset.
    dashboard_session.set_selected_preset(CUSTOM_PRESET)
    st.session_state.pop(f"db_css_editor_{CUSTOM_PRESET}", None)
    st.session_state[STYLE_PANEL_KEY] = False
    st.rerun(scope="app")


def _render_style_editor(report: Report) -> None:
    """Requirement 6.4's stylesheet, set with pickers instead of CSS.

    Controls on the left, the sample report on the right, and nothing reaches the report
    until **Apply**. Drawn on the page inside a bordered box rather than in a dialog: the
    colour pickers each open a panel of their own, and a dialog is the one place that panel
    cannot be reached.
    """
    draft = _style_draft()

    with st.container(border=True, key="db_style_panel_box"):
        st.markdown("##### Custom style")

        controls_column, preview_column = st.columns([3, 4], gap="medium")

        with controls_column:
            draft = _render_page_controls(draft)
            st.divider()
            draft = _render_element_controls(draft)

        # Stored before the preview renders, so what is previewed is what Apply would save.
        _set_style_draft(draft)

        with preview_column:
            _render_style_preview(draft, report)

        _render_theme_shelf(draft)

        with st.container(horizontal=True, vertical_alignment="center", key="db_style_actions"):
            if st.button(
                "Apply",
                key="db_style_apply",
                icon=":material/check:",
                type="primary",
                help="Use these settings for the downloaded report.",
            ):
                _apply_style(draft)
            if st.button(
                "Start from the default",
                key="db_style_reset",
                icon=":material/restart_alt:",
                help="Put every setting back to its starting value. Nothing is applied until you press Apply.",
            ):
                _set_style_draft(custom_style.default_settings())
                _clear_style_widgets()
                st.rerun(scope="app")
            if st.button(
                "Close",
                key="db_style_close",
                icon=":material/close:",
                help="Close the editor without applying these settings.",
            ):
                st.session_state[STYLE_PANEL_KEY] = False
                st.rerun(scope="app")


DIALOGS = {"preview": _preview_item_dialog}


def render_pending_dialog() -> None:
    pending = dashboard_session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action not in DIALOGS:
        dashboard_session.close_dialog()
        return
    DIALOGS[action](payload)

# --------------------------------------------------------------------------------------
# The workspace
# --------------------------------------------------------------------------------------


OUTPUT_VIEWS = ["Preview", "Download"]


def render_report_output(report: Report, *, key: str = "rt_output_view") -> None:
    """The finished report, with no structure editor: preview it, then download it.

    Requirement 8.2 steps 5–6. A run's arrangement came from the Task that was run, and every
    run rebuilds it wholesale from that skeleton — so offering the Build view here would
    invite the user to file items into sections that the next press of Run replaces. The two
    views that are left answer the two questions that remain: is the report right, and is the
    stylesheet right.

    The same two renderers the workspace uses, so what previews here is what downloads there.
    The toggle takes its own key — `db_view` holds one of three options, and a segmented
    control whose stored value isn't in its option list is a value it cannot show.
    """
    view = st.segmented_control(
        "View",
        options=OUTPUT_VIEWS,
        default=OUTPUT_VIEWS[0],
        required=True,
        key=key,
        label_visibility="collapsed",
        persist_state="session",
        help="Read the finished report, then download it as HTML or Excel.",
    )

    render_pending_dialog()

    empty = (
        "This run produced nothing to report. The summary above says which steps failed — "
        "fix those and run it again."
    )
    if view == "Download":
        _render_download(report, empty)
    else:
        _render_preview(report, empty)


def render_report_workspace(report: Report, *, empty_pool: EmptyPool | None = None) -> None:
    """The title box, the view toggle and whichever of the two views is selected.

    `empty_pool` is what to show when nothing has been pinned yet — the message and the
    button that leads to wherever items come from on this page. Passing none leaves a plain
    message, which is all a screen with no obvious "somewhere else" needs.
    """
    report.title = st.text_input(
        "Report title",
        value=report.title,
        key="db_report_title",
        placeholder="e.g. Q3 sales review",
        help="Printed at the top of the report and used as the downloaded file's name.",
    )
    _render_logo_controls(report)

    view = st.segmented_control(
        "View",
        options=VIEWS,
        default=VIEWS[0],
        required=True,
        key=dashboard_session.DB_VIEW_KEY,
        label_visibility="collapsed",
        # Widget values are dropped when the widget stops being rendered, which leaving the
        # page does — so without this, coming back to check one more answer lands the user
        # on Build again rather than where they left off.
        persist_state="session",
        help="Build the structure, then preview and download the finished report.",
    )

    render_pending_dialog()

    if view == "Preview & Download":
        _render_download(report)
    else:
        pool_column, tree_column = st.columns([2, 3], gap="medium")
        with pool_column:
            _render_pool(report, empty_pool)
        with tree_column:
            _render_tree(report)
