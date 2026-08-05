"""Chat with Data — upload files, confirm how they link up, describe the columns.

This page is the front half of requirements section 6, built on the shared Data Engine
(section 5). The chat panel itself arrives in the next stage; everything below it —
loading into DuckDB, confirming relationships as real foreign keys, and the column data
dictionary — is what every later component depends on.

Open to every logged-in role (requirement 2.2 grants Chat with Data to all three), so
this page follows `settings.py` and carries no `require_role` guard.

Layout: three numbered steps as `st.expander`s. `on_change="rerun"` makes each
container's `.open` a real boolean, so a collapsed step's body never executes — the same
gate `data_cleaner.py` uses on `tab.open`, and the reason a collapsed dictionary doesn't
re-run its preview queries. Actions that need input open an `st.dialog`, driven from a
session-state flag rather than a button's return value, which breaks the moment a dialog
holds widgets.

A `st.segmented_control` (`de_view`) switches between "Setup" (steps 2 & 3, plus the
column-editing actions) and "Chat" (the chat panel). Step 1 is the one exception — it
stays visible on both, because its `st.file_uploader` must be instantiated on *every*
rerun to keep reporting its files; the moment a run skips creating it (which switching
tabs would do, same as a collapsed expander skipping it would), it comes back empty on
the next run and silently drops every loaded table. Steps 2 and 3 don't have that
problem — their state lives in plain `session_state` dicts, not a raw widget — so they
can be hidden outright.
"""

import logging

import pandas as pd
import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from engine import columns as engine_columns
from engine import dictionary, duckdb_session, relationships, session
from engine.exceptions import CalculatedColumnError, DataEngineError, UnsafeSqlError
from engine.relationships import Relationship
from llm import session as llm_session
from llm import suggestions as llm_suggestions
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

# Above this many tables, the relationship list stops being scannable and the diagram
# earns its place (requirement 5.2 asks for it "once more than two or three tables").
DIAGRAM_MIN_TABLES = 3

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

    _render_cleaner_handoff()

    uploads = st.file_uploader(
        "Upload CSV or Excel files",
        type=["csv", "txt", "tsv", "xlsx", "xlsm"],
        accept_multiple_files=True,
        key=session.DE_UPLOADER_KEY,
        max_upload_size=session.MAX_UPLOAD_SIZE_MB,
        help="Every cell is read as text first, so leading zeros in IDs and account numbers survive.",
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
    st.write(f"**{relationship.label}**")
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
            st.markdown(f"**{relationship.label}**")
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
                text_column.markdown(f"{icon} {relationship.label}")
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
# Calculated columns
# --------------------------------------------------------------------------------------


@st.dialog("Add a calculated column", width="large", on_dismiss=_dismiss_dialog)
def _dialog_add_column(payload: dict) -> None:
    tables = session.table_names()
    if not tables:
        st.info("Load a table first.", icon=":material/info:")
        return

    table = st.selectbox(
        "Table", options=tables, key="de_calc_table", help="Which table the new column is added to."
    )
    new_column = st.text_input(
        "New column name", key="de_calc_name", help="What to call it, e.g. tax or net_salary."
    )
    expression = st.text_input(
        "Formula",
        key="de_calc_expression",
        help="A SQL expression over this table's columns, e.g. basic * 0.10 or basic - tax.",
    )
    st.caption(f"Columns available: {', '.join(_columns_of(table))}")

    statements: list[str] = []
    if new_column.strip() and expression.strip():
        try:
            column_type = engine_columns.expression_type(session.connection(), table, expression)
            statements = [
                f'ALTER TABLE "{table}" ADD COLUMN "{new_column.strip()}" {column_type}',
                f'UPDATE "{table}" SET "{new_column.strip()}" = ({expression.strip()})',
            ]
            st.markdown("**This will run:**")
            st.code(";\n".join(statements), language="sql")
        except (CalculatedColumnError, UnsafeSqlError) as error:
            st.warning(str(error), icon=":material/error:")

    add_column_button, cancel_column = st.columns(2)
    with add_column_button:
        if st.button(
            "Add column",
            key="de_calc_add_button",
            icon=":material/add:",
            type="primary",
            width="stretch",
            disabled=not statements,
            help="Add it to the table for the rest of this session.",
        ):
            try:
                executed = engine_columns.add_calculated_column(
                    session.connection(), table, new_column.strip(), expression
                )
            except (CalculatedColumnError, UnsafeSqlError) as error:
                st.error(str(error))
                return
            session.add_statements(executed)
            session.bump_rebuild()
            session.refresh_dictionary()
            session.close_dialog()
            st.rerun(scope="app")
    with cancel_column:
        if st.button("Cancel", key="de_calc_cancel_button", width="stretch", help="Close without adding anything."):
            session.close_dialog()
            st.rerun(scope="app")


@st.dialog("Remove a column", width="large", on_dismiss=_dismiss_dialog)
def _dialog_remove_column(payload: dict) -> None:
    tables = session.table_names()
    table = st.selectbox(
        "Table", options=tables, key="de_drop_table", help="Which table to remove a column from."
    )
    options = _columns_of(table)
    column = st.selectbox(
        "Column to remove", options=options, key="de_drop_column", help="This applies for the rest of the session."
    )

    remove_column_button, cancel_column = st.columns(2)
    with remove_column_button:
        if st.button(
            "Remove column",
            key="de_drop_go_button",
            icon=":material/delete:",
            type="primary",
            width="stretch",
            disabled=not column,
            help="Drop the column from this table.",
        ):
            try:
                executed = engine_columns.drop_column(session.connection(), table, column)
            except CalculatedColumnError as error:
                st.error(str(error))
                return
            session.add_statements(executed)
            session.bump_rebuild()
            session.refresh_dictionary()
            session.close_dialog()
            st.rerun(scope="app")
    with cancel_column:
        if st.button("Cancel", key="de_drop_cancel_button", width="stretch", help="Close without removing anything."):
            session.close_dialog()
            st.rerun(scope="app")


DIALOGS = {
    "edit_link": _dialog_edit_link,
    "offending": _dialog_offending_rows,
    "suggest": _dialog_suggest,
    "add_column": _dialog_add_column,
    "remove_column": _dialog_remove_column,
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
    # Read here, not only inside the Chat-tab block below, so the key exists in
    # session_state from the first run regardless of which tab is active.
    statements = session.get_statements()

    upload_summary = (
        f"{len(loaded_tables)} table(s) — " + ", ".join(table.table_name for table in loaded_tables)
        if loaded_tables
        else ""
    )
    tables_before = set(session.get_tables())

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
        # The uploader is instantiated on **every** run, open or collapsed. Gating it on
        # `.open` looks like the same optimization applied to steps 2 and 3 and is not:
        # a widget that stops being rendered stops reporting its value, so the moment
        # this step auto-collapsed `st.file_uploader` returned nothing and `sync_tables`
        # dutifully dropped every table the user had loaded. Only the per-table previews
        # below are gated — they are the expensive part, and they hold no state.
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

    view = st.segmented_control(
        "View",
        options=["Setup", "Chat"],
        key="de_view",
        default="Setup",
        required=True,
        label_visibility="collapsed",
        help="Setup: links and column descriptions. Chat: ask questions about your data.",
    )

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
                expanded=False,
                icon=":material/menu_book:",
            ) as dictionary_step:
                if dictionary_step.open:
                    _render_dictionary_step(user_id)

        if loaded_tables:
            if st.button(
                "Start over",
                key="de_start_over_button",
                icon=":material/refresh:",
                help="Discard every loaded table, link and description.",
            ):
                session.queue_start_over()
                st.rerun(scope="app")

    _render_pending_dialog()

    st.divider()

    if loaded_tables and view == "Chat":
        # No `help=` here: `st.chat_input` is the one widget in this app that has no
        # tooltip parameter, so the guidance goes in the caption below it instead.
        st.chat_input(
            "Ask a question about your data",
            key="de_chat_input",
            disabled=True,
        )
        st.caption("💬 Chat arrives in the next stage — your tables, links and descriptions are ready for it.")

        add_column, remove_column = st.columns(2)
        with add_column:
            if st.button(
                "Add a column",
                key="de_add_column_button",
                icon=":material/add:",
                width="stretch",
                help="Add a calculated column, e.g. tax = basic * 0.10.",
            ):
                session.open_dialog("add_column", {})
                st.rerun(scope="app")
        with remove_column:
            if st.button(
                "Remove a column",
                key="de_remove_column_button",
                icon=":material/delete:",
                width="stretch",
                help="Drop a column from one of your tables.",
            ):
                session.open_dialog("remove_column", {})
                st.rerun(scope="app")

        if statements:
            with st.expander("Column changes so far", icon=":material/history:"):
                st.code(";\n".join(engine_columns.describe_statements(statements)), language="sql")
