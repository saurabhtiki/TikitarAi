"""Chat with Data — upload files, confirm how they link up, describe the columns, then ask.

This page is requirements section 6 built on the shared Data Engine (section 5): loading
into DuckDB, confirming relationships as real foreign keys, the column data dictionary,
and the chat panel where the agent (section 5.5) answers questions against all of it.

Every answer carries a "Pin to Dashboard" button (requirement 6.1 step 3). It copies the
answer into `dashboard.session`'s pool and returns immediately — no dialog, no question
about where it should go — because pinning happens mid-conversation and anything that
interrupts the chat to ask defeats the point. Arranging happens on the Dashboard page.
The button then shows as done and stops accepting presses, so an answer can only reach the
report once however many times it is clicked.

Open to every logged-in role (requirement 2.2 grants Chat with Data to all three), so
this page follows `settings.py` and carries no `require_role` guard.

Layout: three numbered steps as `st.expander`s. `on_change="rerun"` makes each
container's `.open` a real boolean, so a collapsed step's body never executes — the same
gate `data_cleaner.py` uses on `tab.open`, and the reason a collapsed dictionary doesn't
re-run its preview queries. Actions that need input open an `st.dialog`, driven from a
session-state flag rather than a button's return value, which breaks the moment a dialog
holds widgets.

A `st.segmented_control` (`de_view`) switches between "Setup" (steps 2 & 3) and "Chat"
(the transcript, the chat input, and the Actions menu), rendered *above* Step 1 so it
reads as the page's primary navigation rather than something below a full upload panel.
Step 1 is the one exception to the toggle hiding a step outright — its
`st.file_uploader` must be instantiated on *every* rerun regardless of which view is
selected, to keep reporting its files; the moment a run skips creating it (which
switching views would do, same as a collapsed expander skipping it would), it comes back
empty on the next run. Once files are loaded it auto-collapses to one summary line, so in
practice it costs a single line under the toggle rather than a panel. Steps 2 and 3 don't
have that problem — their state lives in plain `session_state` dicts, not a raw widget —
so they can be hidden outright.

Rendering it every run is not enough on its own, because leaving the page entirely still
drops the widget's value and `st.file_uploader` is the one widget with no `persist_state`
to prevent that. So `session.detach_uploader_tables` runs first and takes the loaded
tables out of the uploader's reconciliation the moment its state is gone: after a trip to
the Dashboard the box looks empty, and the tables, links, descriptions and transcript are
all still there. Removing one of those tables is then the per-table **Remove** button
rather than the uploader's own "x".
"""

import logging

import pandas as pd
import streamlit as st

from analyst import charts, pipeline, routing
from analyst import session as chat_session
from analyst.exceptions import ChatStorageError
from analyst.session import ChatMessage
from app_pages.checks_view import render_checks
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from checks import session as checks_session
from dashboard import session as dashboard_session
from engine import columns as engine_columns
from engine import dictionary, duckdb_session, relationships, session
from engine.exceptions import CalculatedColumnError, DataEngineError
from engine.relationships import Relationship
from llm import session as llm_session
from llm import suggestions as llm_suggestions
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

# Above this many tables, the relationship list stops being scannable and the diagram
# earns its place (requirement 5.2 asks for it "once more than two or three tables").
DIAGRAM_MIN_TABLES = 3

# What the Actions menu offers. None of requirement 5.4's column changes are here: adding,
# updating and deleting all work by asking in chat, and a menu entry that duplicates a
# sentence the user can already type is a second thing to maintain for no new capability.
#
# What belongs here is anything chat cannot do. "Show current data" is the first of those
# — after a few conversational column changes, the question "what do my tables look like
# now?" has no natural answer in a transcript. Send mail, add a reminder and add a task
# will join it. Keys map a label to its entry in `DIALOGS`.
MENU_ACTIONS = {
    "Show current data": "show_data",
}

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


# --------------------------------------------------------------------------------------
# Step 1 — Upload
# --------------------------------------------------------------------------------------


def _render_cleaner_handoff() -> None:
    """Offers the tables the user just cleaned, instead of a download/re-upload trip.

    Both pages share one session, so this is a direct read. The frames are snapshotted
    on click: cleaning further afterwards doesn't retroactively change what is loaded,
    and pressing the button again simply re-adopts.
    """
    available = session.cleaner_tables_available()
    if not available:
        return

    with st.container(border=True):
        st.markdown(
            f"🧹 **You have {len(available)} cleaned table(s) in the Data Cleaner** — "
            f"{', '.join(available)}"
        )
        if st.button(
            "Use these tables",
            key="de_adopt_cleaner_button",
            icon=":material/move_down:",
            type="primary",
            help="Load the cleaned tables straight in, exactly as they look in the Data Cleaner.",
        ):
            adopted, warnings = session.adopt_cleaner_tables()
            for warning in warnings:
                st.warning(warning, icon=":material/error:")
            if adopted:
                session.refresh_dictionary()
                st.rerun(scope="app")
        st.caption("Or upload files below.")


def _render_upload() -> list[session.EngineTable]:
    # Consumed before the uploader exists: Streamlit won't allow a widget's own
    # session_state key to be written once that widget has been created this run.
    session.consume_start_over()

    # Also before the uploader exists, and for a related reason: this reads whether the
    # widget's value survived the last run, which stops being answerable the moment the
    # widget is rebuilt. See `session.detach_uploader_tables` — it is what keeps a visit to
    # the Dashboard from wiping every loaded table on the way back.
    detached = session.detach_uploader_tables()

    _render_cleaner_handoff()

    uploads = st.file_uploader(
        "Upload CSV or Excel files",
        type=["csv", "txt", "tsv", "xlsx", "xlsm"],
        accept_multiple_files=True,
        key=session.DE_UPLOADER_KEY,
        max_upload_size=session.MAX_UPLOAD_SIZE_MB,
        help="Every cell is read as text first, so leading zeros in IDs and account numbers survive.",
    )

    if detached:
        # Said once, on the run that detaches, rather than standing permanently: the box
        # above being empty while tables are listed below is confusing exactly once.
        st.caption(
            f":grey[The box above forgets its files when you leave this page, so it looks empty — "
            f"your {detached} loaded table(s) are still here and still queryable. Use **Remove** "
            "beside a table to drop it.]"
        )

    sheet_selection: dict[str, list[str]] = {}
    for upload in uploads or []:
        from cleaner import loaders

        if loaders.is_csv(upload.name):
            continue
        try:
            available = loaders.list_sheet_names(upload.getvalue(), upload.name)
        except Exception as error:  # noqa: BLE001 — cleaner raises its own hierarchy
            st.error(f"{upload.name}: {error}")
            continue
        sheet_selection[upload.file_id] = st.multiselect(
            f"Sheets to load from {upload.name}",
            options=available,
            default=available,
            key=f"de_sheets_{upload.file_id}",
            help="Each selected sheet becomes its own table you can query.",
        )

    return session.sync_tables(uploads, sheet_selection)


def _render_loaded_tables(tables: list[session.EngineTable]) -> None:
    summary = pd.DataFrame.from_records(
        [
            {
                "Table": table.table_name,
                "From": table.source_label,
                "Rows": table.row_count,
                "Columns": len(table.semantic_types),
            }
            for table in tables
        ],
        columns=["Table", "From", "Rows", "Columns"],
    )
    st.dataframe(summary, key="de_tables_summary", width="stretch", hide_index=True)
    st.caption("The names under **Table** are what you'll refer to when you ask questions.")

    _render_remove_buttons(tables)

    for table in tables:
        with st.expander(f"Preview {table.table_name}", icon=":material/table:"):
            try:
                st.dataframe(
                    session.preview(table.table_name),
                    key=f"de_preview_{table.table_id}",
                    width="stretch",
                    hide_index=True,
                )
            except DataEngineError as error:
                st.error(str(error))


def _render_remove_buttons(tables: list[session.EngineTable]) -> None:
    """One Remove per loaded table, under the summary that names them.

    The uploader's own "x" can only take back a file it still holds, which is nothing at
    all for a table adopted from the Data Cleaner or detached after a trip to another page
    (see `session.detach_uploader_tables`). Without these, dropping one of those meant
    Start over — discarding every other table, every link and the whole chat to get rid of
    one file.
    """
    with st.container(horizontal=True, key="de_remove_table_bar"):
        for table in tables:
            if st.button(
                f"Remove {table.table_name}",
                key=f"de_remove_table_{table.table_id}",
                icon=":material/delete:",
                help=f"Drop {table.table_name} and any links that use it. Every other table stays as "
                "it is. To bring it back, upload the file again.",
            ):
                removed = session.remove_table(table.table_id)
                if removed:
                    session.refresh_dictionary()
                    st.toast(f"Removed {removed}.", icon=":material/delete:")
                st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Step 2 — Relationships
# --------------------------------------------------------------------------------------


def _relationship_from_inputs(child_table, child_column, parent_table, parent_column) -> Relationship | None:
    if not all([child_table, child_column, parent_table, parent_column]):
        return None
    if child_table == parent_table:
        return None
    return Relationship(child_table, child_column, parent_table, parent_column)


def _columns_of(table_name: str) -> list[str]:
    try:
        return [column for column, _ in duckdb_session.describe_table(session.connection(), table_name)]
    except DataEngineError:
        return []


def _dismiss_dialog() -> None:
    """Clears the open-dialog flag when a dialog is dismissed natively.

    A dismissible `st.dialog` can be closed by clicking outside it, its "X", or `ESC`
    -- none of which run our own Cancel/Close button code. Left unhandled, `de_open_dialog`
    stays set and the very next unrelated rerun (switching tabs, opening a step) reopens
    the same dialog, since `on_dismiss` defaults to "ignore" and never reruns at all.
    """
    session.close_dialog()


@st.dialog("Add or edit a link", width="large", on_dismiss=_dismiss_dialog)
def _dialog_edit_link(payload: dict) -> None:
    """Four dropdowns and a live verdict.

    The verdict runs the real pre-checks against the real data, so what the user sees
    here is exactly what Confirm will do — there is no separate estimate to drift.
    """
    tables = session.table_names()
    if len(tables) < 2:
        st.info("Load at least two tables first.", icon=":material/info:")
        return

    existing: Relationship | None = payload.get("relationship")
    replacing: Relationship | None = payload.get("replacing")

    st.caption(
        "The **child** column points at the **parent** column. Every child value must "
        "exist in the parent."
    )

    child_column_widget, parent_column_widget = st.columns(2)
    with child_column_widget:
        child_table = st.selectbox(
            "Child table",
            options=tables,
            index=tables.index(existing.child_table) if existing and existing.child_table in tables else 0,
            key="de_link_child_table",
            help="The table that refers to another — for example Sales.",
        )
        child_options = _columns_of(child_table)
        child_column = st.selectbox(
            "Child column",
            options=child_options,
            index=(
                child_options.index(existing.child_column)
                if existing and existing.child_column in child_options
                else 0
            )
            if child_options
            else None,
            key="de_link_child_column",
            help="The column holding the reference — for example cust_id.",
        )

    with parent_column_widget:
        parent_choices = [table for table in tables if table != child_table]
        parent_table = st.selectbox(
            "Parent table",
            options=parent_choices,
            index=(
                parent_choices.index(existing.parent_table)
                if existing and existing.parent_table in parent_choices
                else 0
            )
            if parent_choices
            else None,
            key="de_link_parent_table",
            help="The table being referred to — for example Customer.",
        )
        parent_options = _columns_of(parent_table) if parent_table else []
        parent_column = st.selectbox(
            "Parent column",
            options=parent_options,
            index=(
                parent_options.index(existing.parent_column)
                if existing and existing.parent_column in parent_options
                else 0
            )
            if parent_options
            else None,
            key="de_link_parent_column",
            help="The column that identifies a row there — for example id.",
        )

    relationship = _relationship_from_inputs(child_table, child_column, parent_table, parent_column)
    check = None
    if relationship is not None:
        check = relationships.check_relationship(session.connection(), relationship)
        if check.ok:
            st.success(check.message, icon=":material/check_circle:")
        else:
            st.warning(check.message, icon=":material/error:")
            _render_offending_rows(check)

    add_column, cancel_column = st.columns(2)
    with add_column:
        if st.button(
            "Add this link",
            key="de_link_add_button",
            icon=":material/check:",
            type="primary",
            width="stretch",
            disabled=relationship is None or (check is not None and not check.ok),
            help="Record this link, then press Confirm links to enforce it.",
        ):
            current = [item for item in session.get_relationships() if item != replacing]
            if relationship not in current:
                current.append(relationship)
            session.set_relationships(current)
            session.close_dialog()
            st.rerun(scope="app")
    with cancel_column:
        if st.button("Cancel", key="de_link_cancel_button", width="stretch", help="Close without changing anything."):
            session.close_dialog()
            st.rerun(scope="app")


def _render_offending_rows(check) -> None:
    """Shows the rows behind a failed check, in full.

    Requirement 5.2 is explicit that these are shown with every column's value rather
    than a row number, because that is what makes them findable in the source file. The
    CSV download exists for the same reason — the user is going back to Excel to fix it.
    """
    frame = check.duplicate_parent_keys if not check.duplicate_parent_keys.empty else check.orphan_rows
    if frame.empty:
        return

    label = "repeated" if not check.duplicate_parent_keys.empty else "unmatched"
    if check.orphan_count > len(frame):
        st.caption(f"Showing the first {len(frame):,} of {check.orphan_count:,} {label} rows.")

    st.dataframe(frame, key=f"de_offending_{check.relationship.label}", width="stretch", hide_index=True)
    st.download_button(
        "Download these rows as CSV",
        data=frame.to_csv(index=False).encode("utf-8"),
        file_name="rows_to_fix.csv",
        mime="text/csv",
        key=f"de_offending_download_{check.relationship.label}",
        icon=":material/download:",
        on_click="ignore",
        help="Open this alongside your source file to find and fix the rows.",
    )


@st.dialog("Rows that don't match", width="large", on_dismiss=_dismiss_dialog)
def _dialog_offending_rows(payload: dict) -> None:
    relationship = payload["relationship"]
    check = relationships.check_relationship(session.connection(), relationship)
    st.write(f"**{relationship.explained_label}**")
    st.warning(check.message, icon=":material/error:")
    _render_offending_rows(check)
    if st.button("Close", key="de_offending_close_button", width="stretch", help="Go back to the list of links."):
        session.close_dialog()
        st.rerun(scope="app")


def _render_candidates() -> None:
    candidates = session.get_candidates()
    confirmed = session.get_relationships()

    if not candidates and not confirmed:
        st.caption("No links suggested. Add one yourself if these tables are related.")

    for candidate in candidates:
        relationship = candidate.relationship
        if relationship in confirmed:
            continue

        check = relationships.check_relationship(session.connection(), relationship)
        text_column, action_column = st.columns([3, 2], vertical_alignment="center")
        with text_column:
            st.markdown(f"**{relationship.explained_label}**")
            st.caption(f"✅ {check.message}" if check.ok else f"⚠️ {check.message}")
        with action_column:
            if st.button(
                "Accept",
                key=f"de_accept_{relationship.label}",
                icon=":material/add_link:",
                help=(
                    "Add this link, ready to confirm."
                    if check.ok
                    else "Add this link anyway — it'll still be used for queries, just not "
                    "enforced as a database constraint."
                ),
            ):
                session.set_relationships([*confirmed, relationship])
                st.rerun(scope="app")
            if not check.ok and st.button(
                f"See the {check.orphan_count or len(check.duplicate_parent_keys):,} rows",
                key=f"de_inspect_{relationship.label}",
                icon=":material/error:",
                help="Show the exact rows behind the mismatch, so you can fix the source file.",
            ):
                session.open_dialog("offending", {"relationship": relationship})
                st.rerun(scope="app")

    if confirmed:
        st.markdown("**Links you've added**")
        for relationship in confirmed:
            check = relationships.check_relationship(session.connection(), relationship)
            text_column, action_column = st.columns([3, 2], vertical_alignment="center")
            with text_column:
                icon = "✅" if check.ok else "⚠️"
                text_column.markdown(f"{icon} {relationship.explained_label}")
                if not check.ok:
                    text_column.caption(f"{check.message} Used for queries, not enforced.")
            if action_column.button(
                "Remove",
                key=f"de_remove_{relationship.label}",
                icon=":material/link_off:",
                help="Drop this link.",
            ):
                session.set_relationships([item for item in confirmed if item != relationship])
                st.rerun(scope="app")


def _confirm_links() -> None:
    """Rebuilds the tables, enforcing every confirmed link that's clean enough to.

    A link with unmatched or duplicate-parent rows stays confirmed for querying (it's
    still in `schema_context`, joined with `LEFT JOIN`) — it just doesn't become a
    database constraint, since DuckDB would refuse the `CREATE TABLE` outright.
    """
    confirmed = session.get_relationships()
    connection = session.connection()

    checks = {item: relationships.check_relationship(connection, item) for item in confirmed}
    flagged = [item for item, check in checks.items() if not check.ok]
    clean = [item for item in confirmed if item not in flagged]

    if flagged:
        st.info(
            f"{len(flagged)} link(s) have rows that don't match and won't be enforced as a "
            "database constraint — they'll still be used for queries.",
            icon=":material/info:",
        )

    try:
        relationships.enforce(connection, session.table_names(), clean, session.get_statements())
    except DataEngineError as error:
        logger.exception("Could not enforce the confirmed relationships.")
        st.error(str(error))
        return

    session.bump_rebuild()
    session.refresh_dictionary()
    session.queue_step_state(session.STEP_LINKS, False)
    session.queue_step_state(session.STEP_DICTIONARY, True)
    st.rerun(scope="app")


def _render_links_step(tables: list[session.EngineTable]) -> None:
    st.caption(
        "We compared the column names and then checked how well the actual values overlap. "
        "Accept the ones that look right, then press Confirm."
    )

    if not session.get_candidates() and not session.get_relationships():
        try:
            session.set_candidates(relationships.suggest_relationships(session.connection(), session.table_names()))
        except DataEngineError as error:
            st.error(str(error))

    _render_candidates()

    if len(tables) >= DIAGRAM_MIN_TABLES and session.get_relationships():
        st.graphviz_chart(
            relationships.to_dot(session.table_names(), session.get_relationships()),
            width="stretch",
        )

    add_column, confirm_column = st.columns(2)
    with add_column:
        if st.button(
            "Add a link",
            key="de_add_link_button",
            icon=":material/add:",
            width="stretch",
            help="Link two tables yourself if we didn't suggest it.",
        ):
            session.open_dialog("edit_link", {})
            st.rerun(scope="app")
    with confirm_column:
        if st.button(
            "Confirm links",
            key="de_confirm_links_button",
            icon=":material/verified:",
            type="primary",
            width="stretch",
            disabled=not session.get_relationships(),
            help="Apply these links as real database constraints, so every query joins cleanly.",
        ):
            _confirm_links()


# --------------------------------------------------------------------------------------
# Step 3 — Column dictionary
# --------------------------------------------------------------------------------------


@st.dialog("Describe columns with AI", on_dismiss=_dismiss_dialog)
def _dialog_suggest(payload: dict) -> None:
    user_id = payload["user_id"]
    light = llm_session.light_profile(user_id)
    entries = session.get_dictionary()
    blank = [entry for entry in entries if not entry.description.strip()]

    if light is None:
        st.warning(
            "No Light Model is configured. Set one in Settings → LLM providers first.",
            icon=":material/error:",
        )
        if st.button("Close", key="de_suggest_close_button", width="stretch", help="Go back."):
            session.close_dialog()
            st.rerun(scope="app")
        return

    overwrite = st.toggle(
        "Also replace descriptions I've already written",
        key="de_suggest_overwrite",
        help="Off by default, so anything you've typed is kept.",
    )
    targets = entries if overwrite else blank

    st.write(
        f"**{len(targets)} column(s)** will be sent to **{light['nickname']}** "
        f"({light['default_model']}) — column names, types and a few sample values."
    )
    st.caption("You can edit every suggestion afterwards. Nothing is saved to the database.")

    go_column, cancel_column = st.columns(2)
    with go_column:
        if st.button(
            "Describe them",
            key="de_suggest_go_button",
            icon=":material/auto_awesome:",
            type="primary",
            width="stretch",
            disabled=not targets,
            help="Ask the Light Model for a description and synonyms for each column.",
        ):
            _run_suggestions(light, targets, overwrite)
    with cancel_column:
        if st.button("Cancel", key="de_suggest_cancel_button", width="stretch", help="Close without asking."):
            session.close_dialog()
            st.rerun(scope="app")


def _run_suggestions(light: dict, targets: list, overwrite: bool) -> None:
    connection = session.connection()
    samples = {
        entry.key: dictionary.sample_values(connection, entry.table, entry.column) for entry in targets
    }

    with st.spinner(f"Asking {light['default_model']}…"):
        results, warnings = llm_suggestions.suggest_descriptions(light, targets, samples)

    session.set_dictionary(dictionary.apply_suggestions(session.get_dictionary(), results, overwrite=overwrite))

    for warning in warnings:
        st.warning(warning, icon=":material/error:")
    if not results:
        st.error("No descriptions came back. Check the Light Model with Test connection in the sidebar.")
        return

    session.close_dialog()
    st.rerun(scope="app")


def _render_dictionary_step(user_id: int) -> None:
    entries = session.refresh_dictionary()
    if not entries:
        st.info("Load a table first.", icon=":material/info:")
        return

    st.caption(
        "Descriptions help the AI pick the right column when you ask a question. "
        "**Also known as** is a comma-separated list of other words you might use."
    )

    if st.button(
        "Suggest with AI",
        key="de_suggest_button",
        icon=":material/auto_awesome:",
        help="Fill in the blank descriptions using your Light Model.",
        disabled=llm_session.light_profile(user_id) is None,
    ):
        session.open_dialog("suggest", {"user_id": user_id})
        st.rerun(scope="app")

    if llm_session.light_profile(user_id) is None:
        st.caption("Set a Light Model in Settings → LLM providers to enable AI suggestions.")

    edited = st.data_editor(
        dictionary.to_grid(entries),
        key="de_dictionary_editor",
        width="stretch",
        hide_index=True,
        num_rows="fixed",
        column_config={
            "table": st.column_config.TextColumn("Table", disabled=True),
            "column": st.column_config.TextColumn("Column", disabled=True),
            "type": st.column_config.TextColumn("Type", disabled=True),
            "description": st.column_config.TextColumn(
                "What it means", help="One plain sentence. This is sent to the AI with every question.", width="large"
            ),
            "also_known_as": st.column_config.TextColumn(
                "Also known as", help="Comma-separated synonyms, e.g. quantity, units, units sold."
            ),
        },
    )

    described, total = dictionary.describe_progress(entries)
    save_column, progress_column = st.columns([1, 2], vertical_alignment="center")
    with save_column:
        if st.button(
            "Save descriptions",
            key="de_save_dictionary_button",
            icon=":material/save:",
            type="primary",
            width="stretch",
            help="Keep these descriptions for the rest of this session.",
        ):
            session.set_dictionary(dictionary.merge_edits(entries, edited))
            session.queue_step_state(session.STEP_DICTIONARY, False)
            st.rerun(scope="app")
    progress_column.caption(f"{described} of {total} column(s) described.")


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------
#
# Requirement 5.4's three column changes are all conversational — the path in
# `analyst/column_intent.py` reaches the same `engine/columns.py` functions a dialog would,
# so a guided form for each was a second way to say the same sentence. What lives here
# instead is what a sentence cannot do.


@st.dialog("Current data", width="large", on_dismiss=_dismiss_dialog)
def _dialog_show_data(payload: dict) -> None:
    """The tables as they stand right now, after every change made so far.

    A transcript records what was asked, not what the data became. Once a few columns have
    been added and updated conversationally, "what do my tables look like now?" is a
    reasonable question with no answer anywhere in the chat — this is that answer.
    """
    tables = session.table_names()
    if not tables:
        st.info("No tables are loaded yet.", icon=":material/info:")
        return

    table = st.selectbox(
        "Table",
        options=tables,
        key="an_show_data_table",
        help="Which loaded table to look at. Every change made this session is already in it.",
    )

    try:
        # Counted live rather than read from `EngineTable.row_count`, which is the count
        # at load time. Passing no condition makes this a plain `count(*)`.
        rows = engine_columns.affected_row_count(session.connection(), table)
        frame = session.preview(table)
    except (CalculatedColumnError, DataEngineError) as error:
        logger.exception("Could not preview '%s'.", table)
        st.error(f"'{table}' couldn't be read ({error}).")
        return

    shown = f" — first {len(frame)} shown" if len(frame) < rows else ""
    st.caption(f"{rows} row(s) x {len(frame.columns)} column(s){shown}.")
    st.dataframe(frame, key="an_show_data_frame", width="stretch", hide_index=True)

    if st.button(
        "Close", key="an_show_data_close_button", width="stretch", help="Back to the chat."
    ):
        session.close_dialog()
        st.rerun(scope="app")


DIALOGS = {
    "edit_link": _dialog_edit_link,
    "offending": _dialog_offending_rows,
    "suggest": _dialog_suggest,
    "show_data": _dialog_show_data,
}


def _render_pending_dialog() -> None:
    pending = session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action not in DIALOGS:
        session.close_dialog()
        return
    DIALOGS[action](payload)


# --------------------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------------------


def _render_actions_bar() -> None:
    """The Actions menu and Clear chat, on one row under the chat input.

    A horizontal `st.container` rather than `st.columns`: the two sit together as one
    toolbar and size to their own content, where fixed column widths would leave the menu
    button stranded in an over-wide cell. Placed below the input so the controls stay put
    as the transcript grows — in columns above it they drift further from the chat with
    every answer.

    The per-statement change log that used to sit alongside these was dropped: every
    change already shows up in its own chat message (and, since Actions → Show current
    data, so does the data it produced), so a second running log of the same statements
    was redundant.
    """
    with st.container(horizontal=True, vertical_alignment="center", border=True, key="an_actions_bar"):
        chosen = st.menu_button(
            "Actions",
            options=list(MENU_ACTIONS),
            key="an_actions_menu",
            icon=":material/build:",
            help="Actions that need a form rather than a sentence. Adding, updating and deleting columns is done by asking in chat.",
        )
        if chosen is not None:
            session.open_dialog(MENU_ACTIONS[chosen], {})
            st.rerun(scope="app")

        if st.button(
            "Clear chat",
            key="an_clear_chat_button",
            icon=":material/delete_sweep:",
            disabled=not chat_session.get_messages(),
            help="Empty the transcript and the agent's memory of it. Your tables, links and column changes stay as they are.",
        ):
            chat_session.clear_messages()
            st.rerun(scope="app")


def _render_message(message: ChatMessage, index: int) -> None:
    """Paints one turn of the transcript.

    Everything rendered here comes from what was stored when the question was answered —
    no SQL is re-run on a rerun, which is what keeps scrolling the chat free.
    """
    with st.chat_message(message.role):
        if message.role == chat_session.ROLE_USER:
            st.markdown(message.text)
            return

        if message.is_error:
            st.error(message.text, icon=":material/error:")
            return

        if message.sql:
            # Requirement 5.4 and 5.5: the statement that actually ran is always available,
            # collapsed so it informs without dominating the answer.
            with st.expander("SQL that ran", icon=":material/code:"):
                st.code(message.sql, language="sql")

        if message.figure is not None:
            st.plotly_chart(message.figure, key=f"an_chart_{index}", width="stretch")
            for warning in message.chart_warnings:
                st.caption(f":orange[{warning}]")
            _render_chart_controls(message, index)

        if routing.OUTPUT_DATAFRAME in message.outputs and message.frame is not None:
            st.dataframe(message.frame, key=f"an_frame_{index}", width="stretch", hide_index=True)

        if message.text:
            st.markdown(message.text)

        for warning in message.warnings:
            st.caption(f":orange[{warning}]")

        _render_pin_button(message, index)


def _render_pin_button(message: ChatMessage, index: int) -> None:
    """The one-way trip from an answer to the Dashboard (requirement 6.1 step 3).

    Offered on *every* answer rather than only those carrying a chart or a table — written
    commentary is one of requirement 6.2's three output types and belongs in a report as
    much as the other two.

    Once pressed it shows as done and stops accepting presses, because a button that stays
    live reads as "press me again" and every press used to add another identical tile to
    the Dashboard. The state is the pinned copy still being in the report, so discarding it
    there brings this button back rather than stranding the answer.
    """
    if dashboard_session.pinned_item(message) is not None:
        st.button(
            "Pinned to Dashboard",
            key=f"an_pin_{index}",
            icon=":material/check:",
            disabled=True,
            help=f"Already on your Dashboard — {dashboard_session.pool_count()} item(s) waiting to be placed. "
            "Discard it there if you want to pin this answer again.",
        )
        return

    # `on_click` rather than acting on the return value: a callback runs *before* the rerun
    # the click triggers, so the button comes back painted as "Pinned" on that same rerun.
    # Acting on the return value would leave it reading "Pin to Dashboard" until the user
    # clicked something else, which is what invited the second press in the first place.
    st.button(
        "Pin to Dashboard",
        key=f"an_pin_{index}",
        icon=":material/push_pin:",
        on_click=dashboard_session.pin,
        args=(message,),
        help="Copy this answer to your Dashboard. Nothing is asked for here — you title and arrange it on the Dashboard page.",
    )


# Every widget suffix the customize panel owns. "Reset to automatic" clears them all, and
# keeping the list in one place stops it drifting out of step with the controls themselves.
CHART_CONTROL_SUFFIXES = (
    "kind",
    "x",
    "y",
    "colour",
    "title",
    "palette",
    "series",
    "values",
    "legend",
    "sort",
    "top",
    "height",
)


def _render_chart_controls(message: ChatMessage, index: int) -> None:
    """Lets the user redraw the chart from the rows already in hand.

    Nothing here calls the model or re-runs any SQL: the frame behind this message is still
    in session state, so changing a dropdown is a redraw of data that has already been
    fetched. The controls open pre-filled with what was drawn automatically, so opening the
    panel and closing it again changes nothing.

    Split across two tabs because the two halves answer different questions — **Data** is
    what the chart is of, **Style** is what it looks like — and they are stored as two
    separate values for the same reason, so changing the chart type keeps the title and
    colours the user picked.

    Only offered while the message still holds its frame — `analyst.session` releases those
    from older turns, and controls that can't redraw anything would be a dead end.
    """
    frame = message.frame
    if frame is None or message.choices is None:
        return

    kinds = charts.available_chart_types(frame)
    if not kinds:
        return

    current = message.choices
    current_style = message.style or charts.ChartStyle()

    with st.expander("Customize chart", icon=":material/tune:"):
        data_tab, style_tab = st.tabs(["Data", "Style"], key=f"an_chart_tabs_{index}")
        with data_tab:
            chosen = _chart_data_controls(frame, current, kinds, index)
        with style_tab:
            # `chosen or current` because the style controls only need to know the shape of
            # the chart to decide what to offer, and an empty Values box isn't a reason to
            # empty the Style tab too.
            chosen_style = _chart_style_controls(
                message, frame, chosen or current, current_style, index
            )

    if chosen is None:
        st.caption(":orange[Pick at least one value to plot — showing the last chart until you do.]")
        return
    if chosen == current and chosen_style == current_style:
        return

    figure, warnings = charts.render_chart(frame, chosen, chosen_style)
    if figure is None:
        # The previous chart stays on screen: a failed redraw should cost the change, not
        # the chart the user already had.
        st.caption(f":orange[{' '.join(warnings) or 'That combination cannot be charted.'}]")
        return

    message.figure = figure
    message.choices = chosen
    message.style = chosen_style
    message.chart_warnings = warnings
    st.rerun(scope="app")


def _chart_data_controls(
    frame: pd.DataFrame, current: charts.ChartChoices, kinds: list[str], index: int
) -> charts.ChartChoices | None:
    """The **Data** tab: what the chart is of. None when nothing is selected to plot."""
    columns = list(frame.columns)
    numeric = [name for name in columns if pd.api.types.is_numeric_dtype(frame[name])]

    # Read from the widgets rather than from `current`, which is a run behind them: moving
    # the x axis onto the column currently used as the legend would otherwise leave the
    # legend holding a value its own option list no longer offers.
    live_x = st.session_state.get(f"an_chart_x_{index}", current.x)
    live_measures = st.session_state.get(f"an_chart_y_{index}", current.measures)
    # A measure can't also be the thing it's split by, and neither can the x axis.
    splits = ["None", *(name for name in columns if name != live_x and name not in live_measures)]

    chosen_kind = st.selectbox(
        "Chart type",
        options=kinds,
        index=kinds.index(current.kind) if current.kind in kinds else 0,
        format_func=lambda kind: charts.CHART_LABELS.get(kind, kind),
        key=f"an_chart_kind_{index}",
        help="Only the types that suit this result are listed — a pie needs a few non-negative parts of one whole, a scatter needs two numeric columns.",
    )
    axis_column, value_column = st.columns(2)
    with axis_column:
        chosen_x = st.selectbox(
            "X axis" if chosen_kind != charts.CHART_PIE else "Slices",
            options=columns,
            index=columns.index(current.x) if current.x in columns else 0,
            key=f"an_chart_x_{index}",
            help="The category or date the values are grouped along. On a horizontal bar this runs up the side instead.",
        )
    with value_column:
        chosen_measures = st.multiselect(
            "Values",
            options=numeric,
            default=[name for name in current.measures if name in numeric],
            key=f"an_chart_y_{index}",
            help="The numbers to plot. Pick several to compare them side by side — types that can only draw one will use the first.",
        )

    _drop_stale_selection(f"an_chart_colour_{index}", splits)
    chosen_split = st.selectbox(
        "Colour / legend",
        options=splits,
        index=splits.index(current.colour) if current.colour in splits else 0,
        key=f"an_chart_colour_{index}",
        help="Splits each value into one coloured series per value of this column — e.g. status, giving a legend of statuses.",
    )

    if not chosen_measures:
        return None
    return charts.ChartChoices(
        kind=chosen_kind,
        x=chosen_x,
        measures=list(chosen_measures),
        colour=None if chosen_split == "None" else chosen_split,
    )


def _chart_style_controls(
    message: ChatMessage,
    frame: pd.DataFrame,
    chosen: charts.ChartChoices,
    current: charts.ChartStyle,
    index: int,
) -> charts.ChartStyle:
    """The **Style** tab: what the chart looks like.

    Controls that this particular chart couldn't honour are left out rather than shown
    doing nothing — there is no stacking to choose with one series, and no order to choose
    along a time axis. What is left out keeps whatever it was, so switching a bar to a line
    and back doesn't quietly discard the stacking that was set.
    """
    # An empty box means "use the title derived from the columns", so the automatic title
    # keeps up as the data controls change. Showing the current one as a placeholder says
    # what will be used without pinning it in place.
    title = st.text_input(
        "Title",
        value=current.title or "",
        placeholder=_current_chart_title(message),
        key=f"an_chart_title_{index}",
        help="Leave blank to keep the title built from the column names.",
    )

    palettes = charts.available_palettes(frame, chosen)
    _drop_stale_selection(f"an_chart_palette_{index}", palettes)
    left, right = st.columns(2)
    with left:
        palette = st.selectbox(
            "Colours",
            options=palettes,
            index=palettes.index(current.palette) if current.palette in palettes else 0,
            format_func=lambda name: charts.PALETTE_LABELS.get(name, name),
            key=f"an_chart_palette_{index}",
            help="The colour sequence the series are drawn in. 'Colour-blind safe' is the one to pick for a chart other people will read.",
        )
    with right:
        series = current.series
        if charts.supports_series_layout(frame, chosen):
            layouts = list(charts.SERIES_LABELS)
            series = st.selectbox(
                "Series",
                options=layouts,
                index=layouts.index(current.series) if current.series in layouts else 0,
                format_func=lambda name: charts.SERIES_LABELS.get(name, name),
                key=f"an_chart_series_{index}",
                help="How the series share the space: side by side to compare them, stacked for the total, 100% stacked to compare their shares.",
            )

    show_values = st.checkbox(
        "Show values on the chart",
        value=current.show_values,
        key=f"an_chart_values_{index}",
        help="Prints each point's own number on the chart. Best kept off when there are many bars, where the labels start to overlap.",
    )
    show_legend = st.checkbox(
        "Show legend",
        value=current.show_legend,
        key=f"an_chart_legend_{index}",
        help="Turn off when there is only one series and the legend is just naming it again.",
    )

    sort = current.sort
    top_n = current.top_n
    ceiling = charts.top_n_ceiling(frame, chosen)
    order_column, top_column = st.columns(2)
    with order_column:
        if charts.supports_sorting(frame, chosen):
            orders = list(charts.SORT_LABELS)
            sort = st.selectbox(
                "Sort",
                options=orders,
                index=orders.index(current.sort) if current.sort in orders else 0,
                format_func=lambda name: charts.SORT_LABELS.get(name, name),
                key=f"an_chart_sort_{index}",
                help="'Automatic' is biggest-first for a single run of bars and chronological over time.",
            )
    with top_column:
        if ceiling:
            automatic = charts.default_top_n(frame, chosen)
            picked = st.slider(
                "Show top",
                min_value=1,
                max_value=ceiling,
                value=min(current.top_n or automatic, ceiling),
                key=f"an_chart_top_{index}",
                help="How many categories to draw. The rest stay in the table below.",
            )
            # Left as None while it matches the automatic cap, so the cap can keep moving
            # with the chart type instead of being frozen the moment this panel is opened.
            top_n = None if picked == automatic else picked

    height = st.slider(
        "Height",
        min_value=charts.MIN_CHART_HEIGHT,
        max_value=charts.MAX_CHART_HEIGHT,
        value=current.height,
        step=30,
        key=f"an_chart_height_{index}",
        help="Taller is easier to read when there are many categories, especially on a horizontal bar.",
    )

    _render_chart_reset(message, index)

    return charts.ChartStyle(
        title=title.strip() or None,
        palette=palette,
        series=series,
        show_values=show_values,
        show_legend=show_legend,
        sort=sort,
        top_n=top_n,
        height=height,
    )


def _current_chart_title(message: ChatMessage) -> str:
    """The title on the chart right now, used as the Title box's placeholder."""
    try:
        return str(message.figure.layout.title.text or "")
    except AttributeError:
        # A figure without a title layout is not worth losing the whole panel over.
        logger.debug("Chart figure had no readable title.", exc_info=True)
        return ""


def _render_chart_reset(message: ChatMessage, index: int) -> None:
    """Puts the chart back to the one this question produced on its own.

    Both halves have to go: clearing the stored choices without clearing the widgets would
    leave the panel showing selections the chart no longer reflects.
    """
    if not st.button(
        "Reset to automatic",
        key=f"an_chart_reset_{index}",
        icon=":material/restart_alt:",
        help="Discards these customizations and rebuilds the chart this question produced on its own.",
    ):
        return

    for suffix in CHART_CONTROL_SUFFIXES:
        st.session_state.pop(f"an_chart_{suffix}_{index}", None)

    automatic, warnings = charts.choose_chart(message.frame, message.question)
    if automatic is not None:
        figure, draw_warnings = charts.render_chart(message.frame, automatic)
        if figure is not None:
            message.figure = figure
            message.choices = automatic
            message.style = charts.ChartStyle()
            message.chart_warnings = warnings + draw_warnings
    st.rerun(scope="app")


def _drop_stale_selection(key: str, options: list[str]) -> None:
    """Forgets a stored selection the options no longer contain.

    The panel's option lists depend on each other — the legend can't offer the column now
    on the x axis, and the single-colour palette disappears the moment there are two series
    — so a selection made a run ago can stop being offered. Dropping it lets the widget
    fall back to its default instead of holding a value it can't show.
    """
    if key in st.session_state and st.session_state[key] not in options:
        del st.session_state[key]


def _render_transcript() -> None:
    for index, message in enumerate(chat_session.get_messages()):
        _render_message(message, index)


def _answer_pending_question(active_profile: dict) -> None:
    """Answers the question captured on the previous run, then reruns to paint it.

    Split across two runs on purpose: the run that captures the input appends the user's
    message and reruns immediately, so the question appears the instant it is sent and the
    spinner sits underneath it — rather than the whole page hanging on a model call before
    anything is drawn.
    """
    question = chat_session.pending_question()
    if question is None:
        return

    # The agent's own record of this conversation, so "now break that down by month"
    # resolves against the query it follows — and so the conversation can be listed back
    # later. A store that won't open costs the history, never the answer.
    #
    # The failure travels with the answer rather than being written to the page here: this
    # function always ends in a rerun, so anything painted directly is gone a moment later.
    storage_warning: str | None = None
    try:
        chat_store = chat_session.agent_db()
    except ChatStorageError as error:
        chat_store = None
        storage_warning = str(error)

    with st.chat_message(chat_session.ROLE_ASSISTANT):
        with st.spinner("Working through your data…"):
            answer = pipeline.answer(
                active_profile,
                session.connection(),
                session.schema_context(),
                question,
                # Lets a conversational "add" reach across a confirmed link into another
                # table — see engine.columns.add_calculated_column's child-to-parent join.
                relationships=session.get_relationships(),
                db=chat_store,
                session_id=chat_session.agent_session_id(),
            )

    chat_session.clear_pending_question()

    if answer.statements:
        # A conversational column change (requirement 5.4) is recorded in the same ordered
        # list the dialogs write to, so the Task recipe can't tell which door was used.
        session.add_statements(answer.statements)
        session.bump_rebuild()
        session.refresh_dictionary()

    warnings = list(answer.warnings)
    if storage_warning is not None:
        warnings.append(storage_warning)

    chat_session.append_message(
        ChatMessage(
            role=chat_session.ROLE_ASSISTANT,
            text=answer.text,
            question=answer.question,
            sql=answer.sql,
            frame=answer.frame,
            figure=answer.figure,
            choices=answer.choices,
            style=answer.style,
            outputs=answer.outputs,
            warnings=warnings,
            chart_warnings=answer.chart_warnings,
            is_error=answer.is_error,
        )
    )
    st.rerun(scope="app")


def _render_chat(user_id: int) -> None:
    """The whole chat panel: transcript, input, then the toolbar underneath both."""
    _render_transcript()

    active_profile = llm_session.active_profile(user_id)
    if active_profile is None:
        # Same gate the AI column-description button uses: disable the control and say
        # exactly what is missing, rather than failing on the first question.
        st.chat_input("Ask a question about your data", key="de_chat_input", disabled=True)
        st.caption(
            ":orange[Pick a session model in the sidebar first — that's the model that answers your questions.]"
        )
        _render_actions_bar()
        return

    # Ends in a rerun whenever there was a question to answer, so everything below this
    # line only ever runs with the transcript already up to date.
    _answer_pending_question(active_profile)

    # No `help=` here: `st.chat_input` is the one widget in this app that has no tooltip
    # parameter, so the guidance goes in the caption below it instead.
    question = st.chat_input("Ask a question about your data", key="de_chat_input")
    st.caption(
        "Ask for a chart, a table or an explanation — or change a column, e.g. "
        "*Add tax = 10% of basic*."
    )

    if question:
        chat_session.append_message(ChatMessage(role=chat_session.ROLE_USER, text=question))
        chat_session.set_pending_question(question)
        st.rerun(scope="app")

    # Last, so it always describes a settled transcript. Rendered before the line above, it
    # would paint "Clear chat" as disabled on the run that received the very first
    # question — the message is appended a moment later, and that run never gets repainted.
    _render_actions_bar()


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


def _step_label(number: int, title: str, summary: str, done: bool) -> str:
    marker = "✅" if done else f"{number}."
    return f"{marker} Step {number} · {title}{f' — {summary}' if summary else ''}"


if profile is not None:
    render_sidebar(profile)
    user_id = st.session_state["user_id"]

    st.subheader("🔍 Chat with Data")
    st.write(":blue[**Upload your files, tell us how they connect, then ask questions.**]")

    # Applied before any expander is created, since Streamlit forbids writing a widget's
    # own key once it exists this run.
    session.consume_step_state()

    loaded_tables = list(session.get_tables().values())
    relationship_count = len(session.get_relationships())
    described, total_columns = dictionary.describe_progress(session.get_dictionary())
    # Called here for the side effect, not the value: it puts the statements key in
    # session_state from the very first run, regardless of which tab is active, rather
    # than leaving its first read to wherever a column change happens to occur.
    session.get_statements()

    upload_summary = (
        f"{len(loaded_tables)} table(s) — " + ", ".join(table.table_name for table in loaded_tables)
        if loaded_tables
        else ""
    )
    tables_before = set(session.get_tables())

    # Rendered ahead of Step 1 so it reads as the page's primary navigation rather than
    # something found after scrolling past the upload block. On a brand-new session this
    # is a toggle with nothing loaded to switch to yet — a small price for Chat no longer
    # sitting below a full upload panel on every later visit.
    view = st.segmented_control(
        "View",
        options=["Setup", "Chat", "Checks"],
        key="de_view",
        default="Setup",
        required=True,
        label_visibility="collapsed",
        # Widget values are dropped when the widget stops being rendered, and a visit to
        # the Dashboard does that — so without this, coming back to ask one more question
        # lands on Setup with the chat apparently gone.
        persist_state="session",
        help=(
            "Setup: links and column descriptions. Chat: ask questions about your data. "
            "Checks: test business rules and report the exceptions."
        ),
        width="stretch",
    )

    # Every `expanded=` on this page is a **constant**. Streamlit re-applies that
    # argument whenever its value changes, overriding the stored open state — so a
    # dynamic `expanded=not loaded_tables` would force this step shut the instant a file
    # loaded and keep it shut, putting the uploader permanently out of reach. Anything
    # dynamic goes through `session.queue_step_state` instead.
    with st.expander(
        _step_label(1, "Your data", upload_summary, bool(loaded_tables)),
        key=session.STEP_UPLOAD,
        on_change="rerun",
        expanded=True,
        icon=":material/upload_file:",
    ) as upload_step:
        # The uploader is instantiated on **every** run, open or collapsed, regardless of
        # `view` — gating it on either looks like the same optimization applied to steps 2
        # and 3 and is not: a widget that stops being rendered stops reporting its value,
        # so the moment this step auto-collapsed `st.file_uploader` returned nothing and
        # `sync_tables` dutifully dropped every table the user had loaded. Only the
        # per-table previews below are gated — they are the expensive part, and they hold
        # no state.
        loaded_tables = _render_upload()
        if upload_step.open:
            if loaded_tables:
                _render_loaded_tables(loaded_tables)
            else:
                st.info("Upload a CSV or Excel file to get started.", icon=":material/upload_file:")

    if set(session.get_tables()) != tables_before:
        # The labels above were rendered from the table set as it stood at the top of
        # this run, which the upload has just changed. One rerun brings every summary
        # line back in step with what is actually loaded.
        if session.get_tables():
            session.collapse_once(session.STEP_UPLOAD)
        st.rerun(scope="app")

    if view == "Setup":
        if len(loaded_tables) >= 2:
            link_summary = f"{relationship_count} link(s) confirmed" if relationship_count else "not set up yet"
            with st.expander(
                _step_label(2, "How the tables link up", link_summary, bool(relationship_count)),
                key=session.STEP_LINKS,
                on_change="rerun",
                expanded=True,
                icon=":material/hub:",
            ) as links_step:
                if links_step.open:
                    _render_links_step(loaded_tables)

        if loaded_tables:
            dictionary_summary = f"{described} of {total_columns} column(s) described" if total_columns else ""
            with st.expander(
                _step_label(3, "What the columns mean", dictionary_summary, described > 0),
                key=session.STEP_DICTIONARY,
                on_change="rerun",
                expanded=True,
                icon=":material/menu_book:",
            ) as dictionary_step:
                if dictionary_step.open:
                    _render_dictionary_step(user_id)

        if loaded_tables:
            if st.button(
                "Start over",
                key="de_start_over_button",
                icon=":material/refresh:",
                help="Discard every loaded table, link, description, chat message and pinned dashboard item.",
            ):
                session.queue_start_over()
                # Both cleared here rather than inside `reset_engine`, so `engine/` keeps
                # no dependency on `analyst/` or `dashboard/`. A transcript — or a report —
                # about tables that no longer exist would otherwise read as if it described
                # whatever is loaded next.
                chat_session.reset_chat()
                dashboard_session.reset_dashboard()
                # The criteria in session go with the tables they were written against.
                # Sets saved to SQLite are recipes and deliberately survive: being able to
                # run the same rules against a different file is the point of saving one.
                checks_session.reset_checks()
                st.rerun(scope="app")

    _render_pending_dialog()

    if loaded_tables and view == "Chat":
        with st.container(border=True, key="an_chat_container1",):
            _render_chat(user_id)

    if view == "Checks":
        # Gated on tables for the same reason Chat is: every criteria is written against
        # the loaded schema, and an empty session has nothing to write a rule about.
        if loaded_tables:
            render_checks(user_id)
        else:
            st.info(
                "Upload your data first — criteria are written against the columns you load.",
                icon=":material/upload_file:",
            )
