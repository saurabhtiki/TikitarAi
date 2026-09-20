"""The Dashboard view of Report Builder (phase 32).

Where a user composes an interactive dashboard and downloads it as one offline HTML file.
Shaped like `app_pages/report_items_view.py` - a `render_dashboard` entry called from
`task_builder`'s view switch, with `@st.dialog` forms for add, edit and delete.

The spec table follows the "add or delete users" pattern the requirement names: a
`st.dataframe` with single-row selection and buttons underneath, **not** `st.data_editor`.
That is deliberate rather than incidental. A panel has nine settings, several of which depend
on each other (which sub-types a visual type allows, which columns are numbers), and an
in-place grid editor cannot express a dependency without letting the user type a combination
that has no meaning. A dialog can offer only what fits.

Preview and download are built from **one** string. The same HTML is put in the iframe and
handed to the download button, so what the user checks is exactly what they get - which is
the whole reason the renderer had to be the browser's rather than Python's.
"""

import logging

import pandas as pd
import streamlit as st

from analyst.charts import (
    AGGREGATION_LABELS,
    CHART_LABELS,
    PALETTE_LABELS,
    SORT_LABELS,
)
from engine import session as engine_session
from engine.exceptions import DataEngineError
from live_dashboard import flatten
from live_dashboard import html_export
from live_dashboard import model
from live_dashboard import payload as payload_module
from live_dashboard import session as dashboard_session
from live_dashboard.exceptions import DashboardDataError, DashboardExportError, LiveDashboardError

logger = logging.getLogger(__name__)

#: How tall the inline preview is. Enough to see a row of visuals without scrolling the
#: Streamlit page to find them.
PREVIEW_HEIGHT = 720

#: The most rows the preview is built from. The download always carries everything - this is
#: only about not making the editing loop wait on a 90,000-row document every rerun.
PREVIEW_ROW_CAP = 5_000

_LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif"}

#: A logo bigger than this is a mistake, and it would be carried in every exported file.
MAX_LOGO_BYTES = 2 * 1024 * 1024


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------


def _build_data(spec: model.DashboardSpec) -> dashboard_session.BuiltData | None:
    """Flattens the tables behind the dashboard, reusing the last build when nothing moved.

    The signature is the main table plus the engine's rebuild token, so adding a calculated
    column or confirming a new relationship rebuilds this by itself rather than leaving the
    dashboard drawing yesterday's columns.

    Returns None when there is nothing to build from, having already said so on screen.
    """
    table_names = engine_session.table_names()
    if not table_names:
        return None

    relationships = engine_session.get_relationships()
    main_table = spec.main_table or flatten.detect_fact_table(
        relationships, table_names, engine_session.connection()
    )
    signature = (main_table, tuple(table_names), engine_session.rebuild_count())

    cached = dashboard_session.get_built_data()
    if cached is not None and cached.signature == signature:
        return cached

    try:
        connection = engine_session.connection()
        plan = flatten.join_plan(main_table, relationships, table_names)
        tables = {main_table: flatten.flatten_main_table(connection, plan)}
        for name in plan.side_tables:
            tables[name] = flatten.load_side_table(connection, name)
        description = plan.describe()
    except (DashboardDataError, DataEngineError) as error:
        logger.exception("Could not build the dashboard's data from '%s'.", main_table)
        st.error(str(error))
        return None

    data = dashboard_session.BuiltData(
        tables=tables, plan_description=description, signature=signature
    )
    dashboard_session.store_built_data(data)
    return data


def _column_options(data: dashboard_session.BuiltData, table: str) -> list[str]:
    return data.available_columns().get(table, [])


def _source_label(column: str) -> str:
    """One column as the picker shows it, tagged Fact or Master/Lookup.

    The tag is read off the name rather than tracked separately: `flatten` names a joined
    column `Customer - CustName` and leaves a fact column as it found it, so the separator
    *is* the distinction. That keeps the requirement's Fact/Master labelling honest with no
    extra state that could disagree with the data.
    """
    return f"{column}  -  Master/Lookup" if flatten.COLUMN_SEPARATOR in column else f"{column}  -  Fact"


def _numeric_columns(data: dashboard_session.BuiltData, table: str) -> list[str]:
    frame = data.tables.get(table)
    if frame is None:
        return []
    return [str(name) for name in frame.columns if pd.api.types.is_numeric_dtype(frame[name])]


# --------------------------------------------------------------------------------------
# The spec table
# --------------------------------------------------------------------------------------


def _spec_frame(spec: model.DashboardSpec,
                available: dict[str, list[str]]) -> tuple[pd.DataFrame, list[str]]:
    """The spec as rows for display, and the panel ids in the same order.

    Filters come first under a heading row of their own, then the visuals grouped by row
    number with the number blanked on repeats - so a shared row number reads as one block and
    the table looks like the page it describes.
    """
    records: list[dict] = []
    ids: list[str] = []

    for panel in spec.filters():
        problem = model.panel_problems(panel, available)
        records.append({
            "Row": f"Filters ({spec.filter_position})",
            "Visual": model.VISUAL_LABELS[panel.visual_type],
            "Type": model.FILTER_LABELS.get(panel.sub_type, panel.sub_type),
            "Data source": panel.filter_column() or "-",
            "What it shows": "Narrows every visual on this table",
            "Title": panel.display_title(),
            "Depends on": "-",
            "Status": f"! {problem}" if problem else "OK",
        })
        ids.append(panel.panel_id)

    for row in model.group_into_rows(spec.panels):
        for position, panel in enumerate(row):
            problem = model.panel_problems(panel, available)
            depends = model.depends_on_filters(spec, panel)
            records.append({
                "Row": str(panel.row_number) if position == 0 else "",
                "Visual": model.VISUAL_LABELS[panel.visual_type],
                "Type": CHART_LABELS.get(panel.sub_type, panel.sub_type.replace("_", " ").title()),
                "Data source": _panel_source(panel),
                "What it shows": _panel_logic(panel),
                "Title": panel.display_title(),
                "Depends on": ", ".join(item.display_title() for item in depends) or "-",
                "Status": f"! {problem}" if problem else "OK",
            })
            ids.append(panel.panel_id)

    return pd.DataFrame.from_records(records) if records else pd.DataFrame(), ids


def _panel_source(panel: model.PanelSpec) -> str:
    if panel.visual_type == model.VISUAL_TABLE:
        return ", ".join(panel.source_columns) or "-"
    parts = [part for part in (panel.measure_column, panel.group_by, panel.colour_by) if part]
    return ", ".join(parts) or "-"


def _panel_logic(panel: model.PanelSpec) -> str:
    """Column 5 in words - what this visual actually computes."""
    if panel.visual_type == model.VISUAL_TABLE:
        return f"{len(panel.source_columns)} column(s)"
    measure = AGGREGATION_LABELS.get(panel.aggregation, panel.aggregation)
    if panel.aggregation == "count":
        shown = "Count of rows"
    else:
        shown = f"{measure} of {panel.measure_column or '?'}"
    if panel.group_by:
        shown += f" by {panel.group_by}"
    if panel.colour_by:
        shown += f", split by {panel.colour_by}"
    if panel.top_n:
        shown += f" (top {panel.top_n})"
    return shown


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def _panel_form(panel: model.PanelSpec, data: dashboard_session.BuiltData,
                spec: model.DashboardSpec, key_prefix: str) -> None:
    """The add/edit form's body, shared by both dialogs so they cannot drift apart."""
    tables = sorted(data.tables)

    visual_type = st.selectbox(
        "What kind of visual",
        options=model.VISUAL_TYPES,
        index=model.VISUAL_TYPES.index(panel.visual_type),
        format_func=lambda value: model.VISUAL_LABELS[value],
        key=f"{key_prefix}_visual_type",
        help="Filter narrows the whole page. Card shows one number. Chart draws a picture. "
             "Table lists the rows.",
    )
    if visual_type != panel.visual_type:
        panel.visual_type = visual_type
        panel.sub_type = model.default_sub_type(visual_type)

    sub_types = model.sub_types_for(visual_type)
    labels = model.FILTER_LABELS if visual_type == model.VISUAL_FILTER else CHART_LABELS
    panel.sub_type = st.selectbox(
        "Which style",
        options=sub_types,
        index=sub_types.index(panel.sub_type) if panel.sub_type in sub_types else 0,
        format_func=lambda value: labels.get(value, value.replace("_", " ").title()),
        key=f"{key_prefix}_sub_type",
        help="The styles offered here depend on the kind of visual chosen above.",
    )

    panel.source_table = st.selectbox(
        "Which table",
        options=tables,
        index=tables.index(panel.source_table) if panel.source_table in tables else 0,
        key=f"{key_prefix}_table",
        help="The main table already has the linked master columns joined onto it.",
    )

    columns = _column_options(data, panel.source_table)
    numeric = _numeric_columns(data, panel.source_table)

    if visual_type == model.VISUAL_FILTER:
        current = panel.filter_column()
        panel.source_columns = [st.selectbox(
            "Column to filter on",
            options=columns,
            index=columns.index(current) if current in columns else 0,
            format_func=_source_label,
            key=f"{key_prefix}_filter_column",
            help="Master/Lookup columns filter the fact rows through the link you confirmed "
                 "in Setup.",
        )] if columns else []

    elif visual_type == model.VISUAL_TABLE:
        panel.source_columns = st.multiselect(
            "Columns to show",
            options=columns,
            default=[name for name in panel.source_columns if name in columns],
            format_func=_source_label,
            key=f"{key_prefix}_table_columns",
            help="Shown left to right in the order picked. Clicking a row filters the other "
                 "visuals by its first column.",
        )

    else:
        aggregations = list(AGGREGATION_LABELS)
        panel.aggregation = st.selectbox(
            "What to do with the number",
            options=aggregations,
            index=aggregations.index(panel.aggregation) if panel.aggregation in aggregations else 0,
            format_func=lambda value: AGGREGATION_LABELS[value],
            key=f"{key_prefix}_aggregation",
            help="Count needs no column - it counts the rows that survive the filters.",
        )
        if panel.aggregation != "count":
            panel.measure_column = st.selectbox(
                "Which number",
                options=numeric or columns,
                index=(numeric or columns).index(panel.measure_column)
                if panel.measure_column in (numeric or columns) else 0,
                format_func=_source_label,
                key=f"{key_prefix}_measure",
                help="Only number columns can be totalled.",
            ) if (numeric or columns) else ""

        if visual_type == model.VISUAL_CHART:
            options = [""] + columns
            panel.group_by = st.selectbox(
                "Break it down by",
                options=options,
                index=options.index(panel.group_by) if panel.group_by in options else 0,
                format_func=lambda value: "Pick a column" if not value else _source_label(value),
                key=f"{key_prefix}_group_by",
                help="The category axis, or the slices of a pie. Clicking one filters every "
                     "other visual.",
            )
            panel.colour_by = st.selectbox(
                "Split into a legend by (optional)",
                options=options,
                index=options.index(panel.colour_by) if panel.colour_by in options else 0,
                format_func=lambda value: "No split" if not value else _source_label(value),
                key=f"{key_prefix}_colour_by",
                help="Leave as No split for a single series.",
            )
            sorts = list(SORT_LABELS)
            panel.sort = st.selectbox(
                "Order the categories",
                options=sorts,
                index=sorts.index(panel.sort) if panel.sort in sorts else 0,
                format_func=lambda value: SORT_LABELS[value],
                key=f"{key_prefix}_sort",
                help="Automatic keeps dates in order and sorts everything else biggest first.",
            )
            panel.top_n = int(st.number_input(
                "Keep only the top N (0 shows all)",
                min_value=0, max_value=100, step=1, value=int(panel.top_n),
                key=f"{key_prefix}_top_n",
                help="Applies to this visual only - the other visuals still see every row.",
            ))

    panel.title = st.text_input(
        "Title",
        value=panel.title,
        key=f"{key_prefix}_title",
        help="Shown above the visual in the exported page.",
    )

    if visual_type != model.VISUAL_FILTER:
        panel.row_number = int(st.number_input(
            "Row on the page",
            min_value=1, max_value=50, step=1, value=int(panel.row_number),
            key=f"{key_prefix}_row",
            help="Visuals sharing a row number sit side by side.",
        ))

    panel.properties = model.clean_properties(st.text_input(
        "Properties (optional)",
        value=model.properties_text(panel.properties),
        key=f"{key_prefix}_properties",
        help="For example: border:yes, radius:0.5, format:currency, height:420. Anything "
             "else is ignored.",
    ))


@st.dialog("Add a visual", width="large")
def _add_panel_dialog(data: dashboard_session.BuiltData) -> None:
    spec = dashboard_session.get_spec()
    draft = st.session_state.get("ld_draft_panel")
    if not isinstance(draft, model.PanelSpec):
        draft = model.PanelSpec(
            source_table=spec.main_table,
            row_number=1 + max((panel.row_number for panel in spec.visuals()), default=0),
        )
        st.session_state["ld_draft_panel"] = draft

    _panel_form(draft, data, spec, "ld_add")

    problem = model.panel_problems(draft, data.available_columns())
    if problem:
        st.caption(f":red[{problem}]")

    add, cancel = st.columns(2)
    with add:
        if st.button("Add visual", key="ld_add_confirm", type="primary", width="stretch",
                     help="Put this visual on the dashboard.", disabled=bool(problem)):
            spec.panels.append(draft)
            st.session_state.pop("ld_draft_panel", None)
            dashboard_session.close_dialog()
            dashboard_session.queue_table_reset()
            st.rerun(scope="app")
    with cancel:
        if st.button("Cancel", key="ld_add_cancel", width="stretch",
                     help="Close without adding anything."):
            st.session_state.pop("ld_draft_panel", None)
            dashboard_session.close_dialog()
            st.rerun(scope="app")


@st.dialog("Edit this visual", width="large")
def _edit_panel_dialog(data: dashboard_session.BuiltData, panel_id: str) -> None:
    spec = dashboard_session.get_spec()
    panel = model.find_panel(spec, panel_id)
    if panel is None:
        st.warning("That visual is no longer on the dashboard.")
        return

    _panel_form(panel, data, spec, f"ld_edit_{panel_id}")

    problem = model.panel_problems(panel, data.available_columns())
    if problem:
        st.caption(f":red[{problem}]")

    done, = st.columns(1)
    with done:
        if st.button("Done", key=f"ld_edit_done_{panel_id}", type="primary", width="stretch",
                     help="Close this form. Changes are already applied."):
            dashboard_session.close_dialog()
            dashboard_session.queue_table_reset()
            st.rerun(scope="app")


@st.dialog("Remove this visual")
def _delete_panel_dialog(panel_id: str) -> None:
    spec = dashboard_session.get_spec()
    panel = model.find_panel(spec, panel_id)
    if panel is None:
        st.warning("That visual is no longer on the dashboard.")
        return

    st.write(f"Remove **{panel.display_title()}** from this dashboard?")
    st.caption(":red[This cannot be undone, but you can add it again.]")

    remove, cancel = st.columns(2)
    with remove:
        if st.button("Remove", key=f"ld_delete_confirm_{panel_id}", type="primary",
                     width="stretch", help="Delete this visual."):
            dashboard_session.delete_panel(panel_id)
            dashboard_session.close_dialog()
            st.rerun(scope="app")
    with cancel:
        if st.button("Keep it", key=f"ld_delete_cancel_{panel_id}", width="stretch",
                     help="Close without removing anything."):
            dashboard_session.close_dialog()
            st.rerun(scope="app")


def _render_pending_dialog(data: dashboard_session.BuiltData) -> None:
    pending = dashboard_session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action == "add":
        _add_panel_dialog(data)
    elif action == "edit":
        _edit_panel_dialog(data, payload.get("panel_id", ""))
    elif action == "delete":
        _delete_panel_dialog(payload.get("panel_id", ""))


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


def _render_settings(spec: model.DashboardSpec) -> None:
    with st.expander("Dashboard look and title", icon=":material/palette:"):
        spec.title = st.text_input(
            "Title", value=spec.title, key="ld_title",
            help="Shown at the top of the exported page.")
        spec.subtitle = st.text_input(
            "Subtitle", value=spec.subtitle, key="ld_subtitle",
            help="A line under the title. Leave blank for none.")

        upload = st.file_uploader(
            "Logo", type=list(_LOGO_TYPES), key="ld_logo",
            help="A picture for the header. It is carried inside the exported file.")
        if upload is not None:
            raw = upload.getvalue()
            if len(raw) > MAX_LOGO_BYTES:
                st.caption(":red[That logo is larger than 2 MB. Please use a smaller one.]")
            else:
                spec.logo_bytes = raw
                spec.logo_mime = _LOGO_TYPES.get(upload.name.rsplit(".", 1)[-1].lower(), "image/png")

        theme, position, palette = st.columns(3)
        with theme:
            spec.theme = st.segmented_control(
                "Opens in", options=model.THEMES, default=spec.theme, required=True,
                format_func=lambda value: value.title(), key="ld_theme",
                help="The reader can still switch with the toggle on the page.")
        with position:
            spec.filter_position = st.segmented_control(
                "Filters sit", options=model.FILTER_POSITIONS, default=spec.filter_position,
                required=True, format_func=lambda value: value.title(), key="ld_filter_position",
                help="Across the top, or down the left like a sidebar.")
        with palette:
            palettes = list(PALETTE_LABELS)
            spec.palette = st.selectbox(
                "Colours", options=palettes,
                index=palettes.index(spec.palette) if spec.palette in palettes else 0,
                format_func=lambda value: PALETTE_LABELS[value], key="ld_palette",
                help="The same palettes the charts inside this app use.")


# --------------------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------------------


def _render_export(spec: model.DashboardSpec, data: dashboard_session.BuiltData) -> None:
    """The preview and the download, built from one string."""
    rows = data.main_row_count(spec.main_table)
    allowed, message = payload_module.row_guard(rows)
    if message:
        st.caption(f":red[{message}]")
    if not allowed:
        st.info(
            "Reduce the data before downloading: add a date filter to the source table, or "
            "summarise it in Transform Data first."
        )
        return

    preview_tables = {
        name: frame.head(PREVIEW_ROW_CAP) for name, frame in data.tables.items()
    }
    truncated = any(len(frame) > PREVIEW_ROW_CAP for frame in data.tables.values())

    try:
        preview_html = html_export.build_dashboard_html(spec, preview_tables)
        download_html = (
            preview_html if not truncated else html_export.build_dashboard_html(spec, data.tables)
        )
    except DashboardExportError as error:
        logger.exception("Could not build the dashboard page.")
        st.error(str(error))
        return

    st.download_button(
        "Download this dashboard",
        data=download_html.encode("utf-8"),
        file_name=f"{spec.display_title().replace(' ', '_').lower()}.html",
        mime="text/html",
        key="ld_download_html",
        type="primary",
        icon=":material/download:",
        help="One file, works offline. Everything on it stays clickable.",
    )

    size_mb = len(download_html.encode("utf-8")) / (1024 * 1024)
    if truncated:
        st.caption(
            f":red[The preview below shows the first {PREVIEW_ROW_CAP:,} rows. The download "
            f"has all {rows:,} and is about {size_mb:.1f} MB.]"
        )
    else:
        st.caption(f":red[About {size_mb:.1f} MB, with all {rows:,} row(s) embedded.]")

    st.iframe(preview_html, height=PREVIEW_HEIGHT)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def render_dashboard(user_id: int) -> None:
    """Draws the Dashboard view. Called by `task_builder` when its segment is chosen.

    Never raises: every failure below is caught and shown beside the control that caused it,
    so a dashboard that will not build leaves the spec the user typed on screen to be
    corrected rather than taking the Task with it.
    """
    st.subheader("Interactive dashboard")
    st.caption(
        ":red[Build it here, preview it, then download one HTML file that works offline "
        "and stays clickable.]"
    )

    try:
        spec = dashboard_session.get_spec()
        data = _build_data(spec)
    except LiveDashboardError as error:
        logger.exception("The dashboard view could not start.")
        st.error(str(error))
        return

    if data is None:
        return

    table_names = sorted(data.tables)
    if not spec.main_table or spec.main_table not in table_names:
        spec.main_table = table_names[0] if table_names else ""

    chosen = st.selectbox(
        "Main table",
        options=engine_session.table_names(),
        index=(engine_session.table_names().index(spec.main_table)
               if spec.main_table in engine_session.table_names() else 0),
        key="ld_main_table",
        help="The table whose rows the dashboard is built from. Linked master columns are "
             "joined onto it automatically.",
    )
    if chosen != spec.main_table:
        spec.main_table = chosen
        dashboard_session.clear_built_data()
        st.rerun(scope="app")

    st.caption(f":red[Using {data.plan_description}]")

    _render_settings(spec)
    st.divider()

    if st.button("Add a visual", key="ld_add_panel_button", type="primary",
                 icon=":material/add_chart:",
                 help="Add a filter, card, chart or table to this dashboard."):
        dashboard_session.open_dialog("add")
        st.rerun(scope="app")

    available = data.available_columns()
    frame, panel_ids = _spec_frame(spec, available)

    dashboard_session.consume_table_reset()

    if frame.empty:
        st.info("No visuals yet. Add a filter, a card or a chart to get started.")
    else:
        st.caption(":red[Select a row to edit, copy or remove that visual.]")
        selection = st.dataframe(
            frame,
            key=dashboard_session.LD_TABLE_KEY,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "Row": st.column_config.TextColumn(
                    "Row", help="Visuals sharing a row number sit side by side."),
                "Visual": st.column_config.TextColumn("Visual", help="What kind of visual."),
                "Type": st.column_config.TextColumn("Type", help="Its style."),
                "Data source": st.column_config.TextColumn(
                    "Data source", help="The columns this visual reads."),
                "What it shows": st.column_config.TextColumn(
                    "What it shows", help="The number it computes and how it is broken down."),
                "Title": st.column_config.TextColumn("Title", help="Its heading on the page."),
                "Depends on": st.column_config.TextColumn(
                    "Depends on", help="Which filters narrow this visual."),
                "Status": st.column_config.TextColumn(
                    "Status", help="OK, or what needs fixing before it can be drawn."),
            },
        )

        selected = [row for row in selection["selection"]["rows"] if row < len(panel_ids)]
        if selected:
            panel_id = panel_ids[selected[0]]
            edit, copy, delete = st.columns(3)
            with edit:
                if st.button("Edit", key="ld_edit_button", type="primary", width="stretch",
                             icon=":material/edit:", help="Change this visual's settings."):
                    dashboard_session.open_dialog("edit", {"panel_id": panel_id})
                    st.rerun(scope="app")
            with copy:
                if st.button("Duplicate", key="ld_duplicate_button", width="stretch",
                             icon=":material/content_copy:",
                             help="Make a copy of this visual to adjust."):
                    model.duplicate_panel(spec, panel_id)
                    dashboard_session.queue_table_reset()
                    st.rerun(scope="app")
            with delete:
                if st.button("Remove", key="ld_delete_button", width="stretch",
                             icon=":material/delete:", help="Take this visual off the dashboard."):
                    dashboard_session.open_dialog("delete", {"panel_id": panel_id})
                    st.rerun(scope="app")

    _render_pending_dialog(data)

    if spec.panels:
        st.divider()
        _render_export(spec, data)
