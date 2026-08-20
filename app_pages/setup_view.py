"""The Data Engine setup steps — upload, relationships, column dictionary.

Requirement 5, rendered. This is the shared body of "Step 1 · Your data", "Step 2 · How the
tables link up" and "Step 3 · What the columns mean", extracted from `chat_with_data.py` so
Task Builder (requirement 7.3 step 2) can put the same three steps on its own Setup view
without duplicating them.

Not a `st.Page`. Like `checks_view.py`, it is a plain module a page calls — a page script's
body executes on import, so importing one is not an option.

The two pages that use this never render in the same run, so every widget key is kept
byte-identical to what `chat_with_data.py` used (`de_*`). Renaming them would have been a
gratuitous break in every saved session and every page test.

**`mount_upload` must be called on every run of whichever page is showing**, in every view,
even when nothing about the upload is on screen. `st.file_uploader` stops reporting its
files the moment a run doesn't create it, and `engine.session.sync_tables` would then
dutifully drop every loaded table. The caller is responsible for mounting it out of sight
when it has no place for it; see `chat_with_data.HIDDEN_UPLOAD_MOUNT`.

Nothing here knows about chat types. The caller passes the declared column types it wants
applied to this load (Chat with Data passes its active chat type's; Task Builder passes
none), and the caller owns any report about how the upload measured up.
"""

import logging

import pandas as pd
import streamlit as st

from engine import dictionary, duckdb_session, relationships, session
from engine.exceptions import DataEngineError
from engine.relationships import Relationship
from llm import session as llm_session
from llm import suggestions as llm_suggestions

logger = logging.getLogger(__name__)

# Above this many tables, the relationship list stops being scannable and the diagram
# earns its place (requirement 5.2 asks for it "once more than two or three tables").
DIAGRAM_MIN_TABLES = 3


def step_label(number: int, title: str, summary: str, done: bool) -> str:
    """The header line of a setup step, with a tick once it has something in it."""
    marker = "✅" if done else f"{number}."
    return f"{marker} Step {number} · {title}{f' — {summary}' if summary else ''}"


def dismiss_dialog() -> None:
    """Clears the open-dialog flag when a dialog is dismissed natively.

    A dismissible `st.dialog` can be closed by clicking outside it, its "X", or `ESC`
    -- none of which run our own Cancel/Close button code. Left unhandled, `de_open_dialog`
    stays set and the very next unrelated rerun (switching tabs, opening a step) reopens
    the same dialog, since `on_dismiss` defaults to "ignore" and never reruns at all.
    """
    session.close_dialog()


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


def mount_upload(
    declared_types: dict | None = None,
    *,
    table_names: dict[str, str] | None = None,
    column_renames: dict[str, dict[str, str]] | None = None,
) -> list[session.EngineTable]:
    """Creates the uploader and reconciles what it holds with what's loaded in DuckDB.

    Call this on **every** run, from every view — see the module docstring. `declared_types`
    is the caller's saved column types, applied as the files are read rather than afterwards:
    whether a column can be read as a date is a question about the text in the file, and by
    the time a table is in DuckDB that text has already been converted once.

    `table_names` and `column_renames` are requirement 8.1 step 5's manual remap, passed
    straight through to `session.sync_tables`, which documents both. Only the Run a Task page
    sends them; every other caller's upload is measured against the recipe as it stands.
    """
    # Consumed before the uploader exists: Streamlit won't allow a widget's own
    # session_state key to be written once that widget has been created this run.
    session.consume_start_over()

    # Also before the uploader exists, and for a related reason: this reads whether the
    # widget's value survived the last run, which stops being answerable the moment the
    # widget is rebuilt. See `session.detach_uploader_tables` — it is what keeps a visit to
    # another page from wiping every loaded table on the way back.
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

    return session.sync_tables(
        uploads,
        sheet_selection,
        declared_types or {},
        table_names=table_names or {},
        column_renames=column_renames or {},
    )


def render_upload_report(tables: list[session.EngineTable]) -> None:
    """What's loaded, or the prompt to load something. Step 1's visible half."""
    if not tables:
        st.info("Upload a CSV or Excel file to get started.", icon=":material/upload_file:")
        return

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


@st.dialog("Add or edit a link", width="large", on_dismiss=dismiss_dialog)
def dialog_edit_link(payload: dict) -> None:
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


@st.dialog("Rows that don't match", width="large", on_dismiss=dismiss_dialog)
def dialog_offending_rows(payload: dict) -> None:
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


def render_links_step(tables: list[session.EngineTable]) -> None:
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


@st.dialog("Describe columns with AI", on_dismiss=dismiss_dialog)
def dialog_suggest(payload: dict) -> None:
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


def render_dictionary_step(user_id: int) -> None:
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
# Steps 2 and 3 together
# --------------------------------------------------------------------------------------


def render_setup_steps(user_id: int, loaded_tables: list[session.EngineTable]) -> None:
    """Steps 2 and 3, each in its own expander. Step 1 is the caller's, since its header
    and body carry whatever the caller checked the upload against.

    Every `expanded=` here is a **constant**. Streamlit re-applies that argument whenever
    its value changes, overriding the stored open state — so a dynamic `expanded=` would
    force a step shut the instant its condition flipped and keep it shut. Anything dynamic
    goes through `session.queue_step_state` instead.
    """
    if len(loaded_tables) >= 2:
        relationship_count = len(session.get_relationships())
        link_summary = f"{relationship_count} link(s) confirmed" if relationship_count else "not set up yet"
        with st.expander(
            step_label(2, "How the tables link up", link_summary, bool(relationship_count)),
            key=session.STEP_LINKS,
            on_change="rerun",
            expanded=True,
            icon=":material/hub:",
        ) as links_step:
            if links_step.open:
                render_links_step(loaded_tables)

    if loaded_tables:
        described, total_columns = dictionary.describe_progress(session.get_dictionary())
        dictionary_summary = f"{described} of {total_columns} column(s) described" if total_columns else ""
        with st.expander(
            step_label(3, "What the columns mean", dictionary_summary, described > 0),
            key=session.STEP_DICTIONARY,
            on_change="rerun",
            expanded=True,
            icon=":material/menu_book:",
        ) as dictionary_step:
            if dictionary_step.open:
                render_dictionary_step(user_id)


# The dialogs these steps open, for a page to merge into its own `DIALOGS` registry. A
# dialog is never called from where its button is drawn: on both pages the buttons can sit
# above `st.file_uploader`, and a run that ends before that widget is created loses the
# uploaded files.
SETUP_DIALOGS = {
    "edit_link": dialog_edit_link,
    "offending": dialog_offending_rows,
    "suggest": dialog_suggest,
}
