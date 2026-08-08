"""Dashboard — arrange what was pinned in Chat with Data, then download it.

Requirements 6.3 and 6.4. Open to every logged-in role, so this page follows
`settings.py` and `chat_with_data.py` and carries no `require_role` guard.

Three views on one `st.segmented_control`, the same idiom the chat page uses, because
they are three different jobs rather than three parts of one screen:

- **Build** — the unplaced pool on the left, the section/subsection tree on the right.
  Placing an item is a dropdown and a button, and every row of the tree carries the same
  three reordering controls, so the interaction is learned once and works at all three
  levels requirement 6.3 asks to be reorderable. Each placed item also carries one
  layout switch — "Show in columns with above" — and a run of them becomes a row of
  equal-width columns; `model.group_into_rows` is what both this view's warning and the
  exports read, so a row is never described one way here and built another way there.
- **Preview** — the report read top to bottom, walked through `dashboard.model.walk`,
  which is the same function both exporters use. What is previewed is what downloads.
- **Download** — the CSS preset, the optional hand-edit, the two buttons, and the finished
  HTML shown in an iframe beneath them. The two previews answer different questions:
  Preview says whether the *report* is right, Download says whether the *stylesheet* is.

Nothing here is saved. Requirement 6.3 is explicit that the dashboard exists for the
session only, which is why the page says so plainly rather than leaving the user to find
out by refreshing.
"""

import logging

import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from dashboard import session as dashboard_session
from dashboard.css_presets import (
    DEFAULT_PRESET,
    PRESET_DESCRIPTIONS,
    PRESETS,
    preset_css,
    rule_count,
    validate_css,
)
from dashboard.exceptions import ReportExportError
from dashboard.excel_export import build_report_workbook
from dashboard.html_export import build_html
from dashboard.model import (
    MAX_ROW_COLUMNS,
    PinnedItem,
    Report,
    add_section,
    add_subsection,
    assign_item,
    find_item,
    move,
    numbered_sections,
    numbered_subsections,
    remove_item,
    remove_section,
    remove_subsection,
    subsection_choices,
    unassign_item,
    walk,
    wraps_to_new_row,
)
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

VIEWS = ["Build", "Preview", "Download"]

# The HTML preview scrolls inside this rather than sizing to its content: a report is as
# long as it is, and letting the iframe grow to match would push the download buttons off
# the top of the screen on anything past a couple of items.
HTML_PREVIEW_HEIGHT = 700

# Where pinned items come from. The two pages are one workflow, so an empty Dashboard
# offers the way back rather than just saying there is nothing here.
CHAT_PAGE = "app_pages/chat_with_data.py"

# One icon per output type, so a pool card says what it is before it is opened.
_CHART_ICON = ":material/bar_chart:"
_TABLE_ICON = ":material/table_chart:"
_TEXT_ICON = ":material/notes:"

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


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


def _render_pool(report: Report) -> None:
    """Everything pinned but not yet placed (requirement 6.1 step 4)."""
    st.markdown(f"##### Unplaced ({len(report.pool)})")

    if not report.pool:
        st.info(
            "Nothing pinned yet. Ask a question in Chat with data, then press "
            "**Pin to Dashboard** under any answer.",
            icon=":material/push_pin:",
        )
        # A button and `st.switch_page` rather than `st.page_link`, matching
        # `data_cleaner.py`'s cross-page jump: the link element resolves against the
        # `st.navigation` registry as the page renders, which is one more thing to be
        # broken on a page that is otherwise perfectly usable on its own.
        if st.button(
            "Go to Chat with data",
            key="db_go_to_chat",
            icon=":material/forum:",
            type="primary",
            help="Ask a question, then pin the answer to bring it back here.",
        ):
            st.switch_page(CHAT_PAGE)
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

    for item_index, item in enumerate(subsection.items):
        with st.container(border=True, key=f"db_item_{item.item_id}"):
            _render_item_body(report, subsection, item, item_index)


def _render_item_body(report: Report, subsection, item: PinnedItem, index: int) -> None:
    """One placed item: its heading, its comment, and where it goes next.

    The output itself is not drawn here — a tree of a dozen full-size Plotly charts is
    unusable as a structure editor. The badge and one-line summary say what it is, the
    **Look** button opens it full size, and Preview shows the whole report properly.
    """
    with st.container(horizontal=True, vertical_alignment="bottom", key=f"db_item_bar_{item.item_id}"):
        st.markdown(_item_icon(item))
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
# Preview
# --------------------------------------------------------------------------------------


def _render_preview(report: Report) -> None:
    """The report read top to bottom, in export order.

    Walked with the same `walk(report)` both exporters use, so the order and numbering
    here cannot drift from what downloads. The styling does not carry over — the presets
    are for the exported file, and Streamlit renders in the app's own theme.
    """
    sections = walk(report)
    if not sections:
        st.info(
            "Nothing placed yet. Switch to **Build**, add a section, and place a pinned item into it.",
            icon=":material/preview:",
        )
        return

    st.caption(
        ":grey[Order, numbering and content are exactly what downloads, rendered in the "
        "app's own theme. To see the chosen style, use the preview under **Download**.]"
    )

    for section in sections:
        st.subheader(f"{section.number}. {section.name}", divider="grey")
        for subsection in section.subsections:
            st.markdown(f"##### {subsection.number} {subsection.name}")
            # Grouped through the model's own `rows()`, the same call the HTML export
            # makes, so a row that reads as four columns here is four columns there.
            for row in subsection.rows():
                if len(row) == 1:
                    _render_preview_item(row[0])
                    continue
                for column, item in zip(st.columns(len(row), gap="medium"), row):
                    with column:
                        _render_preview_item(item)


def _render_preview_item(item: PinnedItem) -> None:
    """One item as the report reads it — heading, output, comment."""
    st.markdown(f"**{item.display_heading()}**")
    _render_item_output(item, f"db_preview_{item.item_id}")
    if item.comment.strip():
        st.markdown(f":grey[_{item.comment.strip()}_]")


# --------------------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------------------


def _render_download(report: Report) -> None:
    st.markdown("##### Style")

    stored_preset = dashboard_session.selected_preset(DEFAULT_PRESET)
    preset = st.segmented_control(
        "Style",
        options=list(PRESETS),
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

    css = dashboard_session.accepted_css() or preset_css(preset)
    if dashboard_session.accepted_css():
        st.caption(f":green[Using your edited stylesheet — {rule_count(css)} rule(s).]")

    _render_css_editor(preset, css)

    st.markdown("##### Download")
    if report.is_empty():
        st.info(
            "Place at least one pinned item into a subsection before downloading.",
            icon=":material/download:",
        )
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


DIALOGS = {"preview": _preview_item_dialog}


def _render_pending_dialog() -> None:
    pending = dashboard_session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action not in DIALOGS:
        dashboard_session.close_dialog()
        return
    DIALOGS[action](payload)


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------

if profile is not None:
    render_sidebar(profile)

    st.subheader("📊 Dashboard")
    st.caption(
        ":blue[Arrange what you pinned in Chat with data, then download it as HTML or Excel. "
        "This report lives for this session only — download it before you close the tab.]"
    )

    current_report = dashboard_session.get_report()
    current_report.title = st.text_input(
        "Report title",
        value=current_report.title,
        key="db_report_title",
        placeholder="e.g. Q3 sales review",
        help="Printed at the top of the report and used as the downloaded file's name.",
    )

    view = st.segmented_control(
        "View",
        options=VIEWS,
        default=VIEWS[0],
        required=True,
        key=dashboard_session.DB_VIEW_KEY,
        label_visibility="collapsed",
        # Widget values are dropped when the widget stops being rendered, which a trip to
        # Chat with data does — so without this, coming back to check one more answer lands
        # the user on Build again rather than where they left off.
        persist_state="session",
        help="Build the structure, preview the finished report, then download it.",
    )

    _render_pending_dialog()

    if view == "Preview":
        _render_preview(current_report)
    elif view == "Download":
        _render_download(current_report)
    else:
        pool_column, tree_column = st.columns([2, 3], gap="medium")
        with pool_column:
            _render_pool(current_report)
        with tree_column:
            _render_tree(current_report)
