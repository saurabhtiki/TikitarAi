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

A `st.segmented_control` (`de_view`) switches between "Setup" (steps 1, 2 & 3) and "Chat"
(the transcript, the chat input, and the Actions menu), rendered *above* them so it reads
as the page's primary navigation rather than something below a full upload panel.

Setup is the only view that **shows** Step 1: Chat and Checks are for working, not for
managing files, and a step header repeated above every question is setup chrome on a
screen that has no use for it. But Step 1's `st.file_uploader` must still be
*instantiated* on every rerun regardless of the view, to keep reporting its files — the
moment a run skips creating it (which gating it on the view would do, same as a collapsed
expander skipping it would), it comes back empty on the next run and `sync_tables`
dutifully drops every loaded table. So `_mount_upload` runs in all three views, and off
Setup it runs inside `HIDDEN_UPLOAD_MOUNT`, a container hidden by this page's one
stylesheet. That container is not dead UI: removing it loses the user's data the first
time they switch views. What the check found is not lost with it — anything blocking is
restated by the views' own gates, which name the problems rather than pointing at a step
that isn't on screen. Steps 2 and 3 don't have that problem — their state lives in plain
`session_state` dicts, not a raw widget — so they are simply not rendered off Setup.

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
from app_pages import chart_controls, setup_view
from app_pages.checks_view import render_checks
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from chat_types import db as chat_type_db
from chat_types import matching
from chat_types import model as chat_type_model
from chat_types import session as chat_type_session
from chat_types.exceptions import ChatTypeStorageError
from checks import session as checks_session
from dashboard import session as dashboard_session
from engine import columns as engine_columns
from engine import session
from engine.exceptions import CalculatedColumnError, DataEngineError
from llm import session as llm_session
from sidebar import render_sidebar

logger = logging.getLogger(__name__)

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
#
# The uploader, the loaded-table summary and the previews all live in `setup_view`, shared
# with Task Builder (requirement 7.3 step 2). What stays here is the chat-type layer over
# them, which is this page's alone.


# --------------------------------------------------------------------------------------
# Chat types (requirement 6.6)
# --------------------------------------------------------------------------------------
#
# A saved Steps 1–3 setup, selected above everything else on the page because it decides
# how the upload underneath it is read. Picking "new chat type" is every earlier stage's
# behaviour, unchanged — which is why that is the default and why nothing here is required.


def _saved_chat_types(user_id: int) -> list[dict]:
    """The user's saved setups, or an empty list with the reason on screen."""
    try:
        return chat_type_db.list_types(user_id)
    except ChatTypeStorageError as error:
        logger.exception("Could not list saved chat types for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return []


def _render_chat_type_bar(user_id: int, loaded_tables: list[session.EngineTable]) -> tuple[str, list[dict]]:
    """The picker, the name box and Save/Delete, on one row above everything else.

    Draws the widgets and nothing else. Acting on a changed selection is `_sync_selection`,
    which the page calls **after** Step 1 has rendered — see the note there. Returns what
    the picker says and the rows behind it, so that call needs no second database read.
    """
    saved = _saved_chat_types(user_id)
    names = [row["name"] for row in saved]
    options = [chat_type_session.NEW_CHAT_TYPE, *names]

    with st.container(horizontal=True, vertical_alignment="bottom", border=True, key="ct_bar"):
        chosen = st.selectbox(
            "Chat type",
            options=options,
            key=chat_type_session.CT_PICKER_KEY,
            # Dropped when the widget stops being rendered, and a trip to the Dashboard
            # does exactly that — without this, coming back would silently put the session
            # onto the ad-hoc path while its tables were loaded under a chat type.
            persist_state="session",
            help="A saved setup: which files to expect, how they link, and what the columns mean. "
            "Pick one and just upload Current files.",
        )

        name = st.text_input(
            "Name",
            value=chat_type_session.active_name(),
            placeholder="e.g. Salary processing",
            key="ct_name_input",
            help="Saving under a name you already have updates that chat type.",
        )

        active = chat_type_session.active()
        st.button(
            "Update chat type" if active is not None else "Save chat type",
            key="ct_save_button",
            icon=":material/save:",
            type="primary",
            disabled=not loaded_tables or not name.strip(),
            on_click=_save_chat_type,
            args=(user_id, name),
            help="Store the tables, links and column descriptions as they stand now, so next "
            "time you only have to upload the files.",
        )

        st.button(
            "Show schema",
            key="ct_schema_button",
            icon=":material/schema:",
            # Disabled rather than hidden, so the bar keeps its shape as the picker changes.
            disabled=active is None,
            on_click=_open_schema_dialog,
            help="What this chat type expects: the tables, each column's type, the links "
            "between them and what the columns mean.",
        )

        # if active is not None and active.chat_type_id is not None:
        #     st.button(
        #         #"Delete",
        #         key="ct_delete_button",
        #         icon=":material/delete:",
        #         on_click=_delete_chat_type,
        #         args=(user_id, active),
        #         help="Permanently delete this chat type. Your loaded tables stay as they are, and "
        #         "any criteria sets saved under it survive without one.",
        #     )
            

    if not loaded_tables and chat_type_session.active() is None:
        st.write(
            "No chat type selected — upload anything and set it up by hand, then save it "
            "as a chat type to skip the setup next time."
        )

    return chosen, saved


def _sync_selection(user_id: int, chosen: str, saved: list[dict]) -> None:
    """Loads the picked chat type when the picker has moved off the active one.

    Called from **after Step 1**, never from the bar itself, even though the bar is what
    draws the picker. Selecting reruns, and a run that ends before `st.file_uploader` has
    been created is a run in which that widget wasn't rendered — Streamlit then drops its
    value, taking the files with it (the hazard this module's docstring opens with). By the
    time this runs the uploader exists, so the rerun is free.

    Guarded on the selection actually changing, because this reads the database: doing it
    on every rerun would put a SQLite round trip behind every keystroke in the chat box.
    """
    if chosen == chat_type_session.NEW_CHAT_TYPE:
        if chat_type_session.active() is not None:
            chat_type_session.select(None)
            st.rerun(scope="app")
        return

    if chosen == chat_type_session.active_name():
        return

    row = next((item for item in saved if item["name"] == chosen), None)
    if row is None:
        return

    try:
        chat_type_session.select(chat_type_db.load_type(row["chat_type_id"], user_id))
    except ChatTypeStorageError as error:
        logger.exception("Could not load chat type %s.", row["chat_type_id"])
        st.error(str(error), icon=":material/error:")
        return
    st.rerun(scope="app")


def _save_chat_type(user_id: int, name: str) -> None:
    """Captures the setup as it stands and stores it.

    A callback rather than acting on the button's return value, so the run that saves is
    also the run that repaints the bar with the new name in it.
    """
    captured = chat_type_model.capture(
        name,
        session.semantic_types_by_table(),
        session.get_relationships(),
        session.get_dictionary(),
        chat_type_id=chat_type_session.active_id(),
    )
    try:
        saved = chat_type_db.save_type(user_id, captured)
    except ChatTypeStorageError as error:
        logger.exception("Could not save chat type '%s' for user %s.", name, user_id)
        st.toast(str(error), icon=":material/error:")
        return

    chat_type_session.select(saved)
    # Selecting cleared the applied marker, and the setup on screen *is* what was just
    # saved — so mark it applied rather than letting the next run re-apply it over the
    # user's own edits.
    chat_type_session.apply_setup(saved, session.table_names())
    st.session_state[chat_type_session.CT_PICKER_KEY] = saved.name
    st.toast(f"Saved “{saved.display_name()}”.", icon=":material/check_circle:")


def _open_schema_dialog() -> None:
    """Asks for the schema dialog on the next run.

    Routed through the session flag like every other dialog on this page rather than
    calling the dialog here: the bar is drawn *above* `st.file_uploader`, and a run that
    ends before that widget is created loses the uploaded files.
    """
    session.open_dialog("chat_type_schema", {})


def _delete_chat_type(user_id: int, chat_type: chat_type_model.ChatType) -> None:
    try:
        orphaned = chat_type_db.delete_type(chat_type.chat_type_id, user_id)
    except ChatTypeStorageError as error:
        logger.exception("Could not delete chat type %s.", chat_type.chat_type_id)
        st.toast(str(error), icon=":material/error:")
        return

    chat_type_session.select(None)
    st.session_state[chat_type_session.CT_PICKER_KEY] = chat_type_session.NEW_CHAT_TYPE
    kept = f" {orphaned} criteria set(s) kept, without a chat type." if orphaned else ""
    st.toast(f"Deleted “{chat_type.display_name()}”.{kept}", icon=":material/delete:")


def _match_report(loaded_tables: list[session.EngineTable]) -> matching.MatchReport | None:
    """This upload checked against the active chat type, or None on the ad-hoc path.

    Pure — no rendering, no side effects — so it is safe to call twice on one run: once
    before Step 1 for its header, once inside it for the panel and the views' gate.
    """
    chat_type = chat_type_session.active()
    if chat_type is None or not chat_type.tables or not loaded_tables:
        return None
    return matching.check_upload(
        chat_type, session.load_outcomes(), session.semantic_types_by_table()
    )


def _match_status(loaded_tables: list[session.EngineTable]) -> str:
    """A word for Step 1's header, so a collapsed step still says how the check went."""
    report = _match_report(loaded_tables)
    return f" · {report.status_word()}" if report is not None else ""


def _check_upload(loaded_tables: list[session.EngineTable]) -> matching.MatchReport | None:
    """Acts on the check: drops what the chat type doesn't expect, applies what it saved.

    Called from `_mount_upload`, so it runs in every view and is deliberately **not** gated
    on Step 1 being open or even shown — a collapsed step, and a view that hides it, must
    still discard extra tables and apply the saved links, because the views below are only
    safe once it has.

    Returns the report, for `_render_match_notes` and for the gate on Chat and Checks.
    """
    report = _match_report(loaded_tables)
    if report is None:
        return None

    if report.extra_tables:
        _discard_extra_tables(report.extra_tables)
        st.rerun(scope="app")

    chat_type = chat_type_session.active()
    if report.ok and chat_type_session.needs_applying(chat_type, session.table_names()):
        chat_type_session.apply_setup(chat_type, session.table_names())
        st.rerun(scope="app")

    _open_step_one_on_problems(report)
    return report


def _open_step_one_on_problems(report: matching.MatchReport | None) -> None:
    """Re-opens Step 1 when a new problem appears, since the check lives inside it.

    Step 1 auto-collapses once files are loaded, so a blocking problem would arrive already
    hidden. Keyed on the problems themselves rather than run unconditionally: forcing the
    step open on every run would fight a user who deliberately collapsed it, while a
    *different* problem is worth showing again.

    Off Setup this only prepares that view for when the user arrives — it deliberately does
    **not** switch views. A non-blocking note (a file discarded for not belonging to the
    chat type) must not yank someone out of a chat mid-question, and what does block is
    already restated in full by `_render_mismatch_gate`.
    """
    problems = [
        *(report.problems() if report is not None and not report.ok else []),
        # A file that vanished on upload has to be said out loud even though nothing is
        # blocked by it — silence there reads as the app losing the upload.
        *_discarded_note(),
    ]
    signature = "|".join(problems)
    if signature == chat_type_session.problem_signature():
        return

    chat_type_session.note_problem_signature(signature)
    if problems:
        session.queue_step_state(session.STEP_UPLOAD, True)
        st.rerun(scope="app")


def _render_match_notes(report: matching.MatchReport | None) -> None:
    """What the check found, inside Step 1 rather than in a container of its own.

    Nothing is drawn on the ad-hoc path, and nothing outside this step: once the upload
    matches, collapsing Step 1 takes the whole report off the screen, which is the point —
    Chat and Checks are for working, not for re-reading a green banner. What must not be
    missed stays visible because `_open_step_one_on_problems` opens the step for it.
    """
    if report is None:
        # No report and still something to say: every uploaded file may have been discarded
        # for not belonging to this chat type, leaving no tables to check.
        for note in _discarded_note():
            st.caption(f":grey[{note}]")
        return

    if report.ok:
        st.success(report.summary(), icon=":material/check_circle:")
    else:
        st.error(report.summary(), icon=":material/error:")
        for problem in report.problems():
            st.markdown(f"- {problem}")
        st.caption(
            "Fix these in your file and upload it again, or switch to "
            f"**{chat_type_session.NEW_CHAT_TYPE}** to carry on without this chat type."
        )

    # Read back rather than taken from `apply_setup` above: applying ends in a rerun, which
    # destroys anything that run painted.
    for warning in chat_type_session.apply_warnings():
        st.warning(warning, icon=":material/error:")

    for note in [*report.notes(), *_discarded_note()]:
        st.caption(f":grey[{note}]")


def _discard_extra_tables(extra_tables: list[str]) -> None:
    """Drops tables the chat type doesn't expect, so nothing unexpected reaches the agent.

    `session.remove_table` also marks them dismissed, which is what stops the uploader —
    still holding the file — reconciling them straight back in on the next run.
    """
    by_name = {table.table_name: table_id for table_id, table in session.get_tables().items()}
    removed = [name for name in extra_tables if session.remove_table(by_name.get(name, "")) is not None]
    if removed:
        chat_type_session.note_discarded(removed)
        session.refresh_dictionary()


def _discarded_note() -> list[str]:
    discarded = chat_type_session.discarded()
    if not discarded:
        return []
    return [
        f"{len(discarded)} file(s) not imported — this chat type doesn't expect them: "
        f"{', '.join(f'**{name}**' for name in discarded)}. Switch to "
        f"**{chat_type_session.NEW_CHAT_TYPE}** and upload again to use them."
    ]


# --------------------------------------------------------------------------------------
# Dialogs this page owns
# --------------------------------------------------------------------------------------
#
# Steps 2 and 3 and their three dialogs live in `setup_view`; what follows is only what is
# specific to Chat with Data.


def _dismiss_dialog() -> None:
    """Clears the open-dialog flag when a dialog is dismissed natively.

    A dismissible `st.dialog` can be closed by clicking outside it, its "X", or `ESC`
    -- none of which run our own Cancel/Close button code. Left unhandled, `de_open_dialog`
    stays set and the very next unrelated rerun (switching tabs, opening a step) reopens
    the same dialog, since `on_dismiss` defaults to "ignore" and never reruns at all.
    """
    session.close_dialog()


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


@st.dialog("Chat type schema", width="large", on_dismiss=_dismiss_dialog)
def _dialog_chat_type_schema(payload: dict) -> None:
    """What the selected chat type expects, in full.

    This is what the upload is measured against, so it is worth being able to read before
    uploading anything — which a one-line list of table names above the uploader was not.
    Reads the chat type out of session state: it is already the whole definition, so there
    is no database call here and nothing that can fail.
    """
    chat_type = chat_type_session.active()
    if chat_type is None:
        st.info("No chat type is selected.", icon=":material/info:")
        return

    st.caption(
        f"**{chat_type.display_name()}** expects {len(chat_type.tables)} table(s) and "
        f"{chat_type_model.column_count(chat_type)} column(s). Upload Current files and "
        "we'll check them against this."
    )

    if not chat_type.tables:
        st.warning("This chat type has no tables saved against it.", icon=":material/warning:")

    for position, table in enumerate(chat_type.tables):
        st.markdown(f"**{table.table_name}**")
        st.dataframe(
            pd.DataFrame(
                {
                    "Column": table.column_names,
                    "Type": [matching.type_label(column.semantic_type) for column in table.columns],
                }
            ),
            key=f"ct_schema_table_{position}",
            width="stretch",
            hide_index=True,
        )

    if chat_type.relationships:
        st.markdown("**Links**")
        for link in chat_type.relationships:
            st.markdown(
                f"- `{link.child_table}.{link.child_column}` → "
                f"`{link.parent_table}.{link.parent_column}`"
            )

    described = [
        saved for saved in chat_type.descriptions if saved.description.strip() or saved.synonyms
    ]
    if described:
        st.markdown("**Column meanings**")
        st.dataframe(
            pd.DataFrame(
                {
                    "Table": [saved.table for saved in described],
                    "Column": [saved.column for saved in described],
                    "Description": [saved.description for saved in described],
                    "Also called": [", ".join(saved.synonyms) for saved in described],
                }
            ),
            key="ct_schema_descriptions",
            width="stretch",
            hide_index=True,
        )

    if st.button(
        "Close",
        key="ct_schema_close_button",
        width="stretch",
        help="Back to the page.",
    ):
        session.close_dialog()
        st.rerun(scope="app")


DIALOGS = {
    # Steps 2 and 3's three, shared with Task Builder.
    **setup_view.SETUP_DIALOGS,
    "show_data": _dialog_show_data,
    "chat_type_schema": _dialog_chat_type_schema,
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
            _render_generate_chart_button(message, index)

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


def _chart_keys(index: int) -> chart_controls.ChartKeys:
    """The widget namespace one answer's chart panel owns, keyed by transcript position."""
    return chart_controls.ChartKeys(prefix="an_chart", suffix=f"_{index}")


def _render_generate_chart_button(message: ChatMessage, index: int) -> None:
    """Offers a chart for an answer that came back as a table.

    `routing.classify_output` decides whether a question wanted a chart, and a question that
    didn't ask for one gets a table — as does a question that did ask and couldn't be drawn,
    which `analyst.pipeline` deliberately downgrades rather than showing nothing. Both leave
    rows on screen that the user may well want to see plotted, and until this button their
    only recourse was to ask the question again in different words.

    Only offered while the message still holds its frame: `analyst.session` releases those
    from older turns, and a button that can't draw anything would be a dead end.
    """
    if message.figure is not None or message.frame is None:
        return
    if not charts.available_chart_types(message.frame):
        return

    if not st.button(
        "Generate chart",
        key=f"an_chart_new_{index}",
        icon=":material/insert_chart:",
        help="Plot these rows. Nothing is re-run and nothing is asked of the AI — the chart is built from the table above, and you can change what it plots.",
    ):
        return

    choices, warnings = chart_controls.seed_choices(message.frame, message.question)
    figure, draw_warnings = (
        charts.render_chart(message.frame, choices) if choices is not None else (None, [])
    )
    if figure is None:
        st.caption(f":orange[{' '.join(warnings + draw_warnings) or 'These rows cannot be charted.'}]")
        return

    message.figure = figure
    message.choices = choices
    message.style = charts.ChartStyle()
    message.chart_warnings = warnings + draw_warnings
    # The answer's own record of what it produced, so a later pin and the export agree with
    # what is on screen.
    message.outputs.add(routing.OUTPUT_CHART)
    st.rerun(scope="app")


def _render_chart_controls(message: ChatMessage, index: int) -> None:
    """Lets the user redraw the chart from the rows already in hand.

    The panel itself lives in `chart_controls`, shared with the Checks page; this is the
    part that is the chat's own — which message is being redrawn, and what "reset" means
    here, which is the chart this question produced on its own.

    Nothing here calls the model or re-runs any SQL: the frame behind this message is still
    in session state, so changing a dropdown is a redraw of data that has already been
    fetched. The controls open pre-filled with what was drawn automatically, so opening the
    panel and closing it again changes nothing.

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
    keys = _chart_keys(index)

    # `key` + `on_change` is what makes the open/closed state a widget rather than a fresh
    # `expanded=False` on every run. Every control in here ends in `st.rerun`, so without it
    # the panel shut itself the moment anything was changed in it.
    with st.expander(
        "Customize chart",
        icon=":material/tune:",
        key=f"an_chart_panel_{index}",
        on_change="rerun",
    ):
        data_tab, style_tab = st.tabs(["Data", "Style"], key=f"an_chart_tabs_{index}")
        with data_tab:
            chosen = chart_controls.render_data_controls(frame, current, kinds, keys)
        with style_tab:
            # `chosen or current` because the style controls only need to know the shape of
            # the chart to decide what to offer, and an empty Values box isn't a reason to
            # empty the Style tab too.
            chosen_style = chart_controls.render_style_controls(
                frame,
                chosen or current,
                current_style,
                keys,
                title_placeholder=chart_controls.figure_title(message.figure),
            )
            if chart_controls.render_reset_button(
                keys,
                help_text="Discards these customizations and rebuilds the chart this question produced on its own.",
            ):
                _reset_chart(message)

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


def _reset_chart(message: ChatMessage) -> None:
    """Puts the chart back to the one this question produced on its own.

    Both halves have to go: the widgets are cleared by `render_reset_button` before this is
    called, and clearing them without rebuilding the stored choices would leave the panel
    showing defaults over a chart that still reflects the old picks.
    """
    automatic, warnings = chart_controls.seed_choices(message.frame, message.question)
    if automatic is not None:
        figure, draw_warnings = charts.render_chart(message.frame, automatic)
        if figure is not None:
            message.figure = figure
            message.choices = automatic
            message.style = charts.ChartStyle()
            message.chart_warnings = warnings + draw_warnings
    st.rerun(scope="app")


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

    # Read before the answer is appended, so it is genuinely the *previous* result.
    previous_frame, previous_sql = chat_session.last_result()

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
                # Rescues "now show that as a pie chart" when the model answers it in
                # prose instead of re-running the query the rows came from.
                previous_frame=previous_frame,
                previous_sql=previous_sql,
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


# The container Step 1's widgets live in on Chat and Checks, hidden by the one stylesheet
# on this page. Named here because the CSS rule has to spell the same key.
HIDDEN_UPLOAD_MOUNT = "de_upload_mount"

# Holds whichever of the two the view calls for — the Step 1 expander on Setup, the hidden
# mount on Chat and Checks. Streamlit addresses an element by its position among its parent's
# children, so swapping one for the other directly on the page changes what sits there; a run
# that ends by asking for another leaves the previous occupant on screen, which is how two
# "Step 1 · Your data" headers ended up showing at once on the Task Builder page. One slot that
# is always present keeps the swap among its own children.
UPLOAD_SLOT = "de_upload_slot"

VIEW_KEY = "de_view"
# A view change asked for by something *inside* a view, queued rather than written: the
# toggle is a widget, and Streamlit forbids writing a widget's own key once it exists this
# run — which it does by the time anything below it can ask. Same deferral as
# `session.queue_step_state` uses for the steps.
PENDING_VIEW_KEY = "de_pending_view"


def _mount_upload(
    user_id: int, chosen_chat_type: str, saved_chat_types: list[dict]
) -> tuple[list[session.EngineTable], matching.MatchReport | None]:
    """Step 1's machinery, without any of the chrome that shows it.

    Every view calls this, because `st.file_uploader` must be instantiated on **every** run
    — a widget that stops being rendered stops reporting its value, so the moment a run
    skipped it, `sync_tables` returned nothing and dutifully dropped every table the user
    had loaded. Only Setup wraps the call in an expander and reports what came back; Chat
    and Checks mount it out of sight.

    The order of the three calls is load-bearing and is the reason they sit together here.
    """
    # The chat type's saved column types go in *with* the files rather than being applied
    # afterwards: whether a column can be read as a date is a question about the text in the
    # file, and by the time a table is in DuckDB that text has already been converted once.
    loaded_tables = setup_view.mount_upload(chat_type_session.declared_types())

    # Acted on here rather than where the picker is drawn: this reruns, and a rerun before
    # `st.file_uploader` has been created is a run in which that widget wasn't rendered —
    # Streamlit then drops its value, taking the files with it. It also has to come before
    # the check below, which would otherwise measure this upload against the chat type being
    # switched away from, and discard tables on its say-so.
    _sync_selection(user_id, chosen_chat_type, saved_chat_types)

    # Ungated on purpose: a view that never shows the report must still act on the check.
    # Only the *reporting* of it belongs to Setup.
    match_report = _check_upload(loaded_tables)
    return loaded_tables, match_report


def _render_go_to_setup(key: str) -> None:
    """Sends the user to the one view that can fix what the message above just described.

    Step 1 isn't on this screen any more, so a message about the upload has to carry its own
    way back rather than say "scroll up".
    """
    if st.button(
        "Go to Setup",
        key=key,
        icon=":material/upload_file:",
        help="Open the Setup view, where you upload files and manage what's loaded.",
    ):
        st.session_state[PENDING_VIEW_KEY] = "Setup"
        st.rerun(scope="app")


def _render_mismatch_gate(report: matching.MatchReport | None, reason: str, key: str) -> None:
    """The blocked-view message: what's wrong, why it blocks, and where to fix it.

    The problems are listed here rather than pointed at, because the step that lists them is
    only on Setup now — "fix the problems in Step 1" would name something off screen.
    """
    st.error(f"{report.summary() if report is not None else 'This upload has problems.'} {reason}",
             icon=":material/error:")
    for problem in report.problems() if report is not None else []:
        st.markdown(f"- {problem}")
    _render_go_to_setup(key)


if profile is not None:
    render_sidebar(profile)
    user_id = st.session_state["user_id"]
    with st.container(key="de_chat_with_data",horizontal=True):
        st.subheader("🔍 Chat with Data")
        st.write(":blue[**Upload your files, tell us how they connect, then ask questions.**]")

    # Pinning from here — from an answer or from the Checks view — goes to the session
    # Dashboard. Said even though it is the default: the choice outlives the run that made
    # it, so staying silent would pin into a Task's report after a visit to Task Builder.
    dashboard_session.use_report()

    # Applied before any expander is created, since Streamlit forbids writing a widget's
    # own key once it exists this run.
    session.consume_step_state()
    # The view toggle is under the same rule, and "Go to Setup" is drawn well below it.
    if PENDING_VIEW_KEY in st.session_state:
        st.session_state[VIEW_KEY] = st.session_state.pop(PENDING_VIEW_KEY)

    loaded_tables = list(session.get_tables().values())
    # Called here for the side effect, not the value: it puts the statements key in
    # session_state from the very first run, regardless of which tab is active, rather
    # than leaving its first read to wherever a column change happens to occur.
    session.get_statements()

    upload_summary = (
        f"{len(loaded_tables)} table(s) — "
        + ", ".join(table.table_name for table in loaded_tables)
        # So a collapsed Step 1 still reports the chat type check that now lives inside it.
        + _match_status(loaded_tables)
        if loaded_tables
        else ""
    )
    tables_before = set(session.get_tables())

    # Above the view toggle, because a chat type decides how the files underneath it are
    # read — it is the first choice on the page, not one of the things inside a view.
    chosen_chat_type, saved_chat_types = _render_chat_type_bar(user_id, loaded_tables)

    # Rendered ahead of Step 1 so it reads as the page's primary navigation rather than
    # something found after scrolling past the upload block. On a brand-new session this
    # is a toggle with nothing loaded to switch to yet — a small price for Chat no longer
    # sitting below a full upload panel on every later visit.
    view = st.segmented_control(
        "View",
        options=["Setup", "Chat", "Checks"],
        key=VIEW_KEY,
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

    # One slot, both arrangements — see UPLOAD_SLOT.
    with st.container(key=UPLOAD_SLOT):
        if view == "Setup":
            # Every `expanded=` on this page is a **constant**. Streamlit re-applies that
            # argument whenever its value changes, overriding the stored open state — so a
            # dynamic `expanded=not loaded_tables` would force this step shut the instant a
            # file loaded and keep it shut, putting the uploader permanently out of reach.
            # Anything dynamic goes through `session.queue_step_state` instead.
            with st.expander(
                setup_view.step_label(1, "Your data", upload_summary, bool(loaded_tables)),
                key=session.STEP_UPLOAD,
                on_change="rerun",
                expanded=True,
                icon=":material/upload_file:",
            ) as upload_step:
                # Ungated on `upload_step.open` for the reason `_mount_upload` explains: a
                # collapsed step must still instantiate the uploader and act on the check.
                # Only the per-table previews below are gated — they are the expensive part,
                # and they hold no state.
                loaded_tables, match_report = _mount_upload(
                    user_id, chosen_chat_type, saved_chat_types
                )

                if upload_step.open:
                    _render_match_notes(match_report)
                    setup_view.render_upload_report(loaded_tables)
        else:
            # Chat and Checks are for working, not for managing files, so Step 1 is off these
            # screens entirely — no expander, no header line, no report. Its widgets are still
            # *mounted*, hidden, because they must be created on every run or the upload is
            # lost; this container is not dead UI and deleting it drops the user's tables the
            # first time they switch views. What the check found is not silently swallowed:
            # anything blocking is said by the gates below, and Setup still shows the rest.
            with st.container(key=HIDDEN_UPLOAD_MOUNT):
                loaded_tables, match_report = _mount_upload(
                    user_id, chosen_chat_type, saved_chat_types
                )

    # The app's only stylesheet, and deliberately not a styling choice: there is no native way
    # to mount a widget without showing it, and mounting it is not optional. Emitted on every
    # run — the rule only bites when the container it names exists, and a stylesheet that comes
    # and goes is one more element moving the page around underneath itself. `key=` on a
    # container is what Streamlit documents as producing `.st-key-<key>`.
    st.html(f"<style>.st-key-{HIDDEN_UPLOAD_MOUNT} {{ display: none; }}</style>")

    if set(session.get_tables()) != tables_before:
        # The labels above were rendered from the table set as it stood at the top of
        # this run, which the upload has just changed. One rerun brings every summary
        # line back in step with what is actually loaded.
        if session.get_tables():
            session.collapse_once(session.STEP_UPLOAD)
        st.rerun(scope="app")

    # None on the ad-hoc path, which is what leaves every view open below.
    upload_matches = match_report is None or match_report.ok

    if view == "Setup":
        setup_view.render_setup_steps(user_id, loaded_tables)

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
                # Only what was learned about *these* files. The chat type itself stays
                # selected: discarding this month's data to upload next month's against
                # the same setup is the normal way to use one.
                chat_type_session.forget_upload()
                st.rerun(scope="app")

    _render_pending_dialog()

    if view == "Chat":
        if not loaded_tables:
            # Said out loud rather than left blank: with Step 1 off this screen there is
            # nothing else here, and an empty view reads as the page having failed.
            st.info(
                "Upload your data in Setup to start asking questions.",
                icon=":material/upload_file:",
            )
            _render_go_to_setup("de_chat_go_to_setup_button")
        elif upload_matches:
            with st.container(border=True, key="an_chat_container1",):
                _render_chat(user_id)
        else:
            # Answering questions against a half-matched load is the one thing requirement
            # 6.6 exists to prevent — a text date column returns wrong rows, not an error.
            _render_mismatch_gate(
                match_report,
                "Fix these before asking questions — the answers wouldn't be reliable "
                "until then.",
                "de_chat_mismatch_setup_button",
            )

    if view == "Checks":
        # Gated on tables for the same reason Chat is: every criteria is written against
        # the loaded schema, and an empty session has nothing to write a rule about.
        if not loaded_tables:
            st.info(
                "Upload your data first — criteria are written against the columns you load.",
                icon=":material/upload_file:",
            )
            _render_go_to_setup("de_checks_go_to_setup_button")
        elif not upload_matches:
            _render_mismatch_gate(
                match_report,
                "Fix these before running criteria — a wrong Yes/No goes straight onto "
                "your Dashboard as a report.",
                "de_checks_mismatch_setup_button",
            )
        else:
            render_checks(user_id)
