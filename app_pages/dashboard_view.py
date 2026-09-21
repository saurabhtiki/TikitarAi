"""The Dashboard view of Report Builder (phase 32; a conversation since phase 35).

Where a user composes an interactive dashboard and downloads it as one offline HTML file.
The page is read top to bottom as the flow it describes: **say what you want, see it, say
what to change, see it again, download**.

- **The box at the top is the only way the dashboard is changed.** Each press is one round:
  the Light Model is shown the page as it stands and returns the changes asked for, and a
  visual it does not mention is left alone. Undo puts back the dashboard from before it.
- **The preview underneath is the dashboard.** Phase 36 took away the spec table and the
  edit form it opened: two ways to change one thing meant two things to learn, and the
  table described in eight columns what the preview shows in full a few inches below it.
- **What the preview cannot show is said in words.** A visual that cannot be drawn - a
  column a confirmed link never reaches, a running total over something with no order - is
  listed above the preview, because the one thing a picture of a dashboard cannot show is
  the visual that is missing from it.

Preview and download are built from **one** string. The same HTML is put in the iframe and
handed to the download button, so what the user checks is exactly what they get - which is
the whole reason the renderer had to be the browser's rather than Python's.
"""

import logging

import streamlit as st

from analyst.charts import PALETTE_LABELS
from engine import session as engine_session
from engine.exceptions import DataEngineError
from live_dashboard import ai_spec
from live_dashboard import flatten
from live_dashboard import html_export
from live_dashboard import model
from live_dashboard import payload as payload_module
from live_dashboard import session as dashboard_session
from live_dashboard.exceptions import DashboardDataError, DashboardExportError, LiveDashboardError
from llm import session as llm_session

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


# --------------------------------------------------------------------------------------
# The conversation (phase 35)
# --------------------------------------------------------------------------------------


def _column_notes() -> dict[str, str]:
    """What the columns mean, from the dictionary the user filled in during Setup.

    Requirement 5.3's descriptions and synonyms are already there and were simply not
    reaching this feature. An empty dictionary is the ordinary case on a fresh session, and
    costs nothing: `describe_tables_for_prompt` then renders exactly what it did before.
    """
    try:
        return ai_spec.column_notes(engine_session.get_dictionary())
    except (AttributeError, TypeError) as error:
        logger.info("Could not read the column dictionary for the prompt: %s", error)
        return {}


def _render_conversation(spec: model.DashboardSpec, data: dashboard_session.BuiltData,
                         user_id: int) -> None:
    """The box the dashboard is built and changed from - the only way it is changed.

    On the page rather than behind a dialog, which was phase 35's change in shape: a
    conversation you have to re-open a modal for is not a conversation.

    With no Light Model configured this says so and shows no box. Since phase 36 that leaves
    nothing to do here but download a dashboard saved earlier, which is the plain cost of
    having one way to change one thing - so the warning names the setting to fix.
    """
    light = llm_session.light_profile(user_id)
    if light is None:
        st.warning(
            "No Light Model is configured. Set one in Settings -> LLM providers to build "
            "this dashboard by describing it. A dashboard built earlier still previews and "
            "downloads below.",
            icon=":material/error:",
        )
        return

    has_panels = bool(spec.panels)

    st.text_area(
        "What should change?" if has_panels else "What should this dashboard show?",
        key="ld_ai_instruction",
        height=100,
        placeholder=(
            "make the region chart horizontal, and add a monthly running total"
            if has_panels else
            "total sales card, sales by customer as a bar chart, monthly trend as a line, "
            "and a customer filter"
        ),
        help=(
            "Say what to change and the rest of the dashboard is left alone. Name the "
            "visual by its title, as it appears in the table below."
            if has_panels else
            "Name the columns as they appear in your data for the best result. Leave this "
            "blank to have one designed for you."
        ),
    )

    # There is no separate box for standing preferences since phase 36. "Always show
    # currency in INR" is a sentence, and the box above already takes sentences - a second
    # one only asked the user to decide which of the two a preference belonged in.
    st.caption(
        f":red[Read by **{light['nickname']}** ({light['default_model']}). It can only "
        "choose from the visuals this page already has - it never writes code, and it only "
        "uses columns your data really has.]"
    )

    run_column, undo_column = st.columns([3, 1])
    with run_column:
        if st.button(
            "Update the dashboard" if has_panels else "Build the dashboard",
            key="ld_ai_generate", type="primary", width="stretch",
            icon=":material/auto_awesome:",
            help=("Changes only what you asked for. Everything else stays as it is."
                  if has_panels else
                  "Turns your description into visuals. You can change any of them "
                  "afterwards, by asking or by hand."),
        ):
            _run_round(light, spec, data)
    with undo_column:
        if st.button(
            "Undo", key="ld_ai_undo", width="stretch", icon=":material/undo:",
            disabled=not dashboard_session.can_undo(),
            help="Puts the dashboard back as it was before the last round.",
        ):
            if dashboard_session.undo_last_round():
                st.rerun(scope="app")
            # Only reachable if the stashed copy could not be read back, since the button is
            # disabled without one - but a dead button with no explanation is worse.
            st.caption(":red[That round couldn't be undone. The saved copy of the "
                       "dashboard could not be read back.]")

    _render_rounds()


def _run_round(light: dict, spec: model.DashboardSpec,
               data: dashboard_session.BuiltData) -> None:
    """Runs one round and records what it did.

    `revise_dashboard` never raises and edits `spec` in place only once it has a change to
    make, so there is no error path here beyond the sentences it returns - a failed call is
    reported as "we couldn't make that change", with the dashboard still on screen.

    The dashboard is stashed for Undo *before* the round, since the round mutates it. A round
    that changed nothing throws the stash away again rather than lighting up a button that
    would restore the page to exactly what it already is.
    """
    instruction = str(st.session_state.get("ld_ai_instruction", "") or "")
    dashboard_session.stash_for_undo(spec)

    with st.spinner(f"Asking {light['default_model']}..."):
        result = ai_spec.revise_dashboard(
            light, instruction, data.tables, spec, notes=_column_notes(),
        )

    details = [ai_spec.describe_panel(panel) for panel in (*result.added, *result.updated)]
    details += [f"Removed **{title}**" for title in result.removed]

    dashboard_session.record_round(dashboard_session.Round(
        instruction=instruction.strip(),
        summary=result.summary(),
        details=details,
        notes=list(result.notes),
        clarification=result.clarification or "",
        failed=result.failed,
    ))

    if not result.changed():
        dashboard_session.discard_undo()
        return

    st.rerun(scope="app")


def _render_problems(spec: model.DashboardSpec,
                     data: dashboard_session.BuiltData) -> None:
    """The one thing the preview below cannot show: a visual that is not in it.

    This is what the removed spec table's `Status` column said. Without it a visual the
    dashboard refuses to draw - a column no confirmed link reaches, a running total over
    labels with no order - simply is not there, and the only clue is the user's own memory
    of having asked for it.
    """
    try:
        available = data.available_columns()
        dates = data.date_columns()
    except (AttributeError, KeyError, TypeError) as error:
        logger.info("Could not check the visuals for problems: %s", error)
        return

    broken = []
    for panel in (*spec.filters(), *spec.panels):
        problem = model.panel_problems(panel, available, dates)
        if problem:
            broken.append(f"**{panel.display_title()}** - {problem}")

    if not broken:
        return

    st.warning(
        f"{len(broken)} visual(s) can't be drawn yet. Tell the AI above what to use "
        "instead, or ask it to remove them:\n\n"
        + "\n".join(f"- {line}" for line in broken),
        icon=":material/error:",
    )


def _render_rounds() -> None:
    """The conversation so far, newest first.

    Kept because a round is easy to miss: "changed 1" beside a page of eight visuals is the
    only sign that the one you named is the one that moved. Notes are `st.warning` rather
    than captions - a shape swapped for the nearest one we can draw is exactly the thing a
    user discovers three days later in someone else's inbox.
    """
    history = dashboard_session.rounds()
    if not history:
        return

    with st.expander(f"What was asked, and what changed ({len(history)})", expanded=True):
        for position, entry in enumerate(history):
            if entry.instruction:
                st.markdown(f"**You asked:** {entry.instruction}")
            if entry.clarification:
                st.info(entry.clarification, icon=":material/info:")
            for note in entry.notes:
                st.warning(note, icon=":material/error:")
            if not entry.failed:
                st.caption(f":red[{entry.summary}]")
                for detail in entry.details:
                    st.write("- " + detail)
            if position < len(history) - 1:
                st.divider()


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
        ":red[Describe it, see it, say what to change - then download one HTML file that "
        "works offline and stays clickable.]"
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

    _render_conversation(spec, data, user_id)

    if not spec.panels:
        st.info("No visuals yet. Describe the dashboard you want in the box above.")
        return

    st.divider()
    _render_problems(spec, data)
    _render_export(spec, data)
