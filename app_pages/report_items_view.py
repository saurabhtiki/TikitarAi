"""The Report-Items view of the Task Builder (requirement 7.3 step 3).

A view fragment, not a registered `st.Page` — the same arrangement `checks_view.py` has with
`chat_with_data.py`, and for the same reason: it needs what the page around it has already
loaded (the DuckDB connection, the confirmed links, the column dictionary), and a page of its
own would have to rebuild all three.

**This is where Task Builder differs most from Chat with Data.** Chat has a conversation; a
Task has an ordered list. The distinction is not presentational — a Task is re-run months
later against a different file (requirement 8.2), and a transcript has no order that can be
replayed, while a list does. So the list is the product, and it holds two kinds of thing:

- a **report item**, which asks a question and produces a table, a chart and a comment; and
- a **column step**, which changes the data every item *below* it then sees.

Both are drawn here, from `report_items.model`'s one `ReportItem` with its `kind` — the same
sequence, because their order relative to each other is the whole point.

The persona is a **parameter**, not a box on this screen. One persona belongs to the whole
Task (requirement 7.2) and is set once at the foot of the Setup view; the Checks view of the
same page is handed the identical string. A second persona box here would be a second answer
to one question.

Every widget key carries its item's `item_id`. This is not cosmetic: Streamlit applies
`value=`/`default=` only when a key is first created, so a key shared between two items would
keep showing whichever was created first — the trap `settings.py` documents in full.
"""

import logging

import pandas as pd
import streamlit as st

from analyst import charts, column_intent, commentary
from analyst.exceptions import AnalystError
from app_pages import chart_controls
from app_pages.checks_view import SchemaOptions
from dashboard import session as dashboard_session
from engine import columns as engine_columns
from engine import session as engine_session
from engine.exceptions import CalculatedColumnError, DataEngineError
from llm import session as llm_session
from report_items import session as report_items_session
from report_items import sql_builder
from report_items.exceptions import ReportItemError, ReportItemSqlError
from report_items.model import (
    KIND_COLUMN,
    KIND_REPORT,
    ReportItem,
    add_item,
    freeze_run,
    source_id_for,
)

logger = logging.getLogger(__name__)

# Capped for the reason every other preview in this app is capped: a report item over a
# 200,000-row table is a perfectly normal thing to write, and rendering all of it would cost
# seconds per rerun to show rows nobody scrolls to. The saved run and the export keep every row.
PREVIEW_ROWS = 500

REQUEST_PLACEHOLDER = (
    "Total salary paid per department this month, highest first, with the number of "
    "employees in each."
)

COLUMN_PLACEHOLDER = "Add tax = 10% of basic"

KIND_LABELS = {
    KIND_REPORT: "report item",
    KIND_COLUMN: "column step",
}

KIND_ICONS = {
    KIND_REPORT: ":material/summarize:",
    KIND_COLUMN: ":material/table_edit:",
}


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def _dismiss_dialog() -> None:
    """Clears the flag when a dialog is dismissed by the X, ESC or a click outside.

    Without it the flag survives, and the next unrelated rerun reopens the same dialog —
    the behaviour `chat_with_data.py::dismiss_dialog` documents in full.
    """
    report_items_session.close_dialog()


@st.dialog("Add to the report", on_dismiss=_dismiss_dialog)
def _dialog_add_item(payload: dict) -> None:
    """Asks only for the heading. The request itself is written in the card this creates,
    where there is room for it — a dialog would make the most important field the smallest."""
    kind = payload.get("kind", KIND_REPORT)
    label = KIND_LABELS[kind]

    heading = st.text_input(
        "Heading" if kind == KIND_REPORT else "Step name",
        key="ri_new_item_heading",
        placeholder="e.g. Payroll by department" if kind == KIND_REPORT else "e.g. Add tax column",
        help=(
            "The heading this item gets in the report."
            if kind == KIND_REPORT
            else "A label for this step. A column step never appears in the report — it changes "
            "the data that the items below it read."
        ),
    )

    confirm, cancel = st.columns(2)
    with confirm:
        if st.button(
            "Add",
            key="ri_new_item_add",
            type="primary",
            width="stretch",
            disabled=not heading.strip(),
            help=f"Create this {label} at the end of the list and open it below.",
        ):
            add_item(report_items_session.get_items(), kind, heading)
            report_items_session.close_dialog()
            st.rerun(scope="app")
    with cancel:
        if st.button(
            "Cancel", key="ri_new_item_cancel", width="stretch", help="Go back without adding."
        ):
            report_items_session.close_dialog()
            st.rerun(scope="app")


@st.dialog("Current data", width="large", on_dismiss=_dismiss_dialog)
def _dialog_show_data(payload: dict) -> None:
    """The tables as they stand right now, after every column step applied so far.

    A port of the Chat page's dialog of the same name, and here for a sharper reason: a
    column step's *output is the data*, which lives on the Setup view. Without this, checking
    what a step actually did means leaving the list mid-build.
    """
    tables = engine_session.table_names()
    if not tables:
        st.info("No tables are loaded yet.", icon=":material/info:")
        return

    table = st.selectbox(
        "Table",
        options=tables,
        key="ri_show_data_table",
        help="Which loaded table to look at. Every column step applied so far is already in it.",
    )

    try:
        # Counted live rather than read from `EngineTable.row_count`, which is the count at
        # load time. Passing no condition makes this a plain `count(*)`.
        rows = engine_columns.affected_row_count(engine_session.connection(), table)
        frame = engine_session.preview(table)
    except (CalculatedColumnError, DataEngineError) as error:
        logger.exception("Could not preview '%s'.", table)
        st.error(f"'{table}' couldn't be read ({error}).", icon=":material/error:")
        return

    shown = f" — first {len(frame)} shown" if len(frame) < rows else ""
    st.caption(f"{rows} row(s) x {len(frame.columns)} column(s){shown}.")
    st.dataframe(frame, key="ri_show_data_frame", width="stretch", hide_index=True)

    if st.button(
        "Close",
        key="ri_show_data_close_button",
        width="stretch",
        help="Back to the list of report items.",
    ):
        report_items_session.close_dialog()
        st.rerun(scope="app")


DIALOGS = {"add_item": _dialog_add_item, "show_data": _dialog_show_data}


def _render_pending_dialog() -> None:
    pending = report_items_session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action not in DIALOGS:
        report_items_session.close_dialog()
        return
    DIALOGS[action](payload)


# --------------------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------------------


def _render_heading_row(item: ReportItem) -> None:
    """The heading box and the Delete button, drawn the same for both kinds.

    Delete goes through `session.remove_item`, which enforces the rule that only the *last*
    column step may go: everything under one was written against the columns it added. A
    refusal is reported rather than silently ignored, because a button that appears to do
    nothing is worse than one that explains itself.
    """
    header, delete_column = st.columns([5, 1], vertical_alignment="center")
    with header:
        item.heading = st.text_input(
            "Heading" if not item.is_column_step() else "Step name",
            value=item.heading,
            key=f"ri_heading_{item.item_id}",
            help=(
                "The heading this item gets in the report."
                if not item.is_column_step()
                else "A label for this step. It never appears in the report."
            ),
        )
    with delete_column:
        if st.button(
            "Delete",
            key=f"ri_delete_{item.item_id}",
            icon=":material/delete:",
            width="stretch",
            help="Remove this item from the list and take anything it put in the report back out.",
        ):
            # Unpinned first: `remove_item` may refuse, and unpinning something that stays in
            # the list would leave a card claiming to be in a report it had just left.
            allowed, reason = report_items_session.remove_item(item.item_id)
            if not allowed:
                st.error(reason, icon=":material/error:")
            else:
                dashboard_session.unpin_source(source_id_for(item))
                logger.info("Removed report item '%s'.", item.display_heading())
                st.rerun(scope="app")


def _render_hints(item: ReportItem, schema: SchemaOptions, *, disabled: bool = False) -> None:
    """The user's guess at what this item touches.

    Explicitly a hint. The model also receives the real schema and is instructed to correct a
    wrong guess rather than fail on it, which is why neither picker is required.

    `schema` is built once per run and passed down: the option lists are identical for every
    item and cannot change mid-render, so rebuilding them per item walks the whole column
    dictionary again for an answer already in hand.

    `disabled` freezes the pickers alongside the request they belong to — an applied column
    step is a change already made, and a live picker over one would suggest it could still
    steer something.
    """
    table_column, column_column = st.columns(2)
    with table_column:
        item.hint_tables = st.multiselect(
            "Tables involved (optional)",
            options=schema.tables,
            default=[name for name in item.hint_tables if name in schema.table_set],
            key=f"ri_hint_tables_{item.item_id}",
            disabled=disabled,
            help="A hint for the AI. It also sees your real schema and will correct a wrong guess.",
        )
    with column_column:
        item.hint_columns = st.multiselect(
            "Columns involved (optional)",
            options=schema.columns,
            default=[name for name in item.hint_columns if name in schema.column_set],
            key=f"ri_hint_columns_{item.item_id}",
            disabled=disabled,
            help="A hint for the AI. Leave empty if you're not sure which columns are involved.",
        )


def _hint_line(item: ReportItem) -> str:
    """The hint pickers as the one line the prompt carries, or "" when nothing was picked.

    Shaped like `checks.sql_builder.build_prompt`'s so the two paths read alike to a model.
    """
    parts = []
    if item.hint_tables:
        parts.append(f"tables: {', '.join(item.hint_tables)}")
    if item.hint_columns:
        parts.append(f"columns: {', '.join(item.hint_columns)}")
    return "; ".join(parts)


# --------------------------------------------------------------------------------------
# Report items — SQL
# --------------------------------------------------------------------------------------


def _generate_sql(item: ReportItem, persona: str, user_id: int) -> None:
    """Writes this item's SQL and runs it, recording whatever happened.

    Both outcomes are stored rather than raised: a failure is the input to the refine loop, so
    it has to survive the rerun that follows this press.
    """
    profile = llm_session.active_profile(user_id)
    if profile is None:
        report_items_session.record_error(
            item.item_id, "No AI connection is selected — pick one in the sidebar."
        )
        return

    try:
        sql, frame = sql_builder.generate_and_run(
            profile,
            persona,
            item,
            engine_session.schema_context(),
            engine_session.connection(),
        )
    except ReportItemSqlError as error:
        logger.info("Report item '%s' could not be generated: %s", item.display_heading(), error)
        item.last_error = str(error)
        report_items_session.record_error(item.item_id, str(error))
        return

    item.sql = sql
    item.last_error = None
    report_items_session.record_result(item.item_id, frame)


def _run_saved_sql(item: ReportItem) -> None:
    """Runs the statement this item already has, without asking a model for a new one.

    This is what makes a saved Task a *recipe*. The SQL is the part `to_json` stores precisely
    because it is what can be run again next month; regenerating on every re-run would spend a
    provider call per item to replace a query the user has already tuned, and could come back
    shaped differently — which in a report means a heading that silently changed between one
    month and the next.

    A failure is recorded, not repaired: the message names the missing table or column, and
    **Regenerate SQL** is right beside it. Nothing here reaches a provider, so reading the
    error first costs nothing.
    """
    try:
        frame = sql_builder.run_item(engine_session.connection(), item.sql or "")
    except ReportItemSqlError as error:
        logger.info("Report item '%s' could not be re-run: %s", item.display_heading(), error)
        # Kept on the item as well as in the runtime: if the user presses Regenerate next,
        # this is what tells the model what went wrong.
        item.last_error = str(error)
        report_items_session.record_error(item.item_id, str(error))
        return

    item.last_error = None
    report_items_session.record_result(item.item_id, frame)


def _render_sql_controls(item: ReportItem, persona: str, user_id: int) -> None:
    """Two buttons once there is SQL, not one.

    Writing the query and running it are separate jobs and only the first costs a model call.
    An item loaded from a saved Task already has its statement, so running last month's recipe
    against this month's file should be free; regenerating is then something the user asks for
    when the request has changed or the query is wrong.
    """
    if not item.sql:
        if st.button(
            "Generate SQL & run",
            key=f"ri_generate_{item.item_id}",
            type="primary",
            icon=":material/play_arrow:",
            disabled=not item.request.strip(),
            help="Write the SQL for this request and run it against the data loaded now.",
        ):
            with st.spinner("Writing and running the query…"):
                _generate_sql(item, persona, user_id)
            st.rerun(scope="app")
        return

    run_column, regenerate_column = st.columns(2)
    with run_column:
        if st.button(
            "Run saved SQL",
            key=f"ri_run_{item.item_id}",
            type="primary",
            width="stretch",
            icon=":material/play_arrow:",
            help=(
                "Run the query below against the data loaded now. No AI call — the request "
                "and its SQL are used exactly as they stand."
            ),
        ):
            with st.spinner("Running the query…"):
                _run_saved_sql(item)
            st.rerun(scope="app")
    with regenerate_column:
        if st.button(
            "Regenerate SQL",
            key=f"ri_regenerate_{item.item_id}",
            width="stretch",
            icon=":material/auto_awesome:",
            disabled=not item.request.strip(),
            help=(
                "Have the AI rewrite this query and run it. It starts from the statement "
                "below and changes as little as it can — use it after editing the request, "
                "or when the saved query no longer fits the data."
            ),
        ):
            with st.spinner("Writing and running the query…"):
                _generate_sql(item, persona, user_id)
            st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Report items — chart, comment, report
# --------------------------------------------------------------------------------------


def _chart_keys(item: ReportItem) -> chart_controls.ChartKeys:
    """The widget namespace one item's chart panel owns."""
    return chart_controls.ChartKeys(prefix="ri_chart", suffix=f"_{item.item_id}")


def _render_chart(item: ReportItem, frame: pd.DataFrame) -> None:
    """A chart of the rows on screen, built by the user rather than chosen for them.

    No item gets one automatically. An automatic chart per item, repeated down a page of ten
    items, is a page of pictures nobody asked for; a chart the user builds is a different
    object, and "payroll by department" is exactly what belongs beside the table in a report.

    The figure is rebuilt on every rerun rather than stored: the frame is already in memory,
    and `ReportItem` holds the recipe so that saving and reloading a Task brings the chart back
    — a Plotly figure would neither survive that round trip nor describe itself.
    """
    kinds = charts.available_chart_types(frame)
    if not kinds:
        return

    keys = _chart_keys(item)
    if item.chart is None:
        if st.button(
            "Generate chart",
            key=f"ri_chart_new_{item.item_id}",
            icon=":material/insert_chart:",
            help=(
                "Plot the rows above. Nothing is re-run and nothing is asked of the AI — you "
                "choose what it plots, and it is saved with this item."
            ),
        ):
            chosen, _ = chart_controls.seed_choices(frame, item.request)
            item.chart = chosen
            item.chart_style = charts.ChartStyle()
            st.rerun(scope="app")
        return

    figure, warnings = charts.render_chart(frame, item.chart, item.chart_style)
    if figure is None:
        st.caption(f":orange[{' '.join(warnings) or 'These rows cannot be charted.'}]")
    else:
        st.plotly_chart(figure, key=f"ri_chart_fig_{item.item_id}", width="stretch")
        for warning in warnings:
            st.caption(f":orange[{warning}]")

    # `key` + `on_change` is what makes the open/closed state a widget rather than a fresh
    # `expanded=False` on every run. Every control in here ends in `st.rerun`, so without it
    # the panel shut itself the moment anything was changed in it.
    with st.expander(
        "Customize chart",
        icon=":material/tune:",
        key=f"ri_chart_panel_{item.item_id}",
        on_change="rerun",
    ):
        data_tab, style_tab = st.tabs(["Data", "Style"], key=f"ri_chart_tabs_{item.item_id}")
        with data_tab:
            chosen = chart_controls.render_data_controls(frame, item.chart, kinds, keys)
        with style_tab:
            # `chosen or item.chart` because the style controls only need the shape of the
            # chart to decide what to offer, and an empty Values box isn't a reason to empty
            # the Style tab too.
            chosen_style = chart_controls.render_style_controls(
                frame,
                chosen or item.chart,
                item.chart_style or charts.ChartStyle(),
                keys,
                title_placeholder=chart_controls.figure_title(figure),
            )
        if st.button(
            "Remove chart",
            key=f"ri_chart_remove_{item.item_id}",
            icon=":material/delete:",
            help=(
                "Take the chart off this item. Its table and comment stay as they are — the "
                "report keeps the table on the next save."
            ),
        ):
            item.chart = None
            item.chart_style = None
            chart_controls.clear_controls(keys)
            st.rerun(scope="app")

    if chosen is None:
        st.caption(":orange[Pick at least one value to plot — showing the last chart until you do.]")
        return
    if chosen == item.chart and chosen_style == item.chart_style:
        return

    item.chart = chosen
    item.chart_style = chosen_style
    st.rerun(scope="app")


def _write_comment(item: ReportItem, frame: pd.DataFrame, persona: str, user_id: int) -> None:
    """Fills the comment box with an AI draft, or says why it couldn't.

    On a button rather than on every run: this is a provider call, and the view re-executes on
    every keystroke anywhere on the page. Automatic would mean one request per character typed
    into any item on screen.
    """
    profile = llm_session.active_profile(user_id)
    if profile is None:
        st.warning(
            "No AI connection is selected — pick one in the sidebar, or write the comment yourself.",
            icon=":material/warning:",
        )
        return

    written, warnings = commentary.write_commentary(
        profile,
        item.request or item.display_heading(),
        frame,
        item.sql,
        # Requirement 7.2: the Task's persona and context is the domain-rules slot every
        # generated wording on this page reads from.
        knowledge_base=persona,
    )
    for warning in warnings:
        st.warning(warning, icon=":material/warning:")
    if not written:
        return

    item.comment = written
    # Assigned to the widget's own key, not just to the model: a text area that already exists
    # ignores `value=` for the rest of the session, so this is what actually puts the new
    # wording on screen.
    st.session_state[f"ri_comment_{item.item_id}"] = written


def _chart_figure(item: ReportItem, frame: pd.DataFrame):
    """The chart to pin beside the table, or None when this item has none.

    Drawn fresh from the rows being pinned rather than reusing the figure on screen, so the
    report gets a chart of exactly what the report is getting.

    A chart that fails to draw costs the chart, never the save: the table and the comment are
    the report item, and the picture is the part that can be missing.
    """
    if item.chart is None:
        return None
    figure, warnings = charts.render_chart(frame, item.chart, item.chart_style)
    if figure is None:
        logger.warning(
            "Report item '%s' was saved without its chart: %s",
            item.display_heading(),
            " ".join(warnings),
        )
    return figure


def _save_to_report(item: ReportItem, frame: pd.DataFrame) -> None:
    """Freezes the run and pins it — the Task Builder's "Pin to report".

    `pin_result` is idempotent on `source_id`, so re-running an item and pressing this again
    updates the copy already in the report rather than adding a second one.

    Which report it lands in is the page's business, not this module's: `dashboard.session`
    holds an active-report key that Task Builder sets to its own. That is what keeps this call
    identical to the one the Checks view makes while the two land in different places.
    """
    item.saved_run = freeze_run(item.sql or "", frame)
    pinned = dashboard_session.pin_result(
        source_id_for(item),
        heading=item.display_heading(),
        comment=item.comment,
        sql=item.sql,
        frame=frame,
        figure=_chart_figure(item, frame),
    )
    logger.info("Pinned report item '%s' as %s.", item.display_heading(), pinned.item_id)


def _remove_from_report(item: ReportItem) -> None:
    """Undoes a pin: the saved run is released and the item leaves the report.

    The request, its hints, its SQL and its comment are untouched — pressing Run then Pin
    again puts everything back.
    """
    item.saved_run = None
    dashboard_session.unpin_source(source_id_for(item))
    logger.info("Removed report item '%s' from the report.", item.display_heading())


def _render_results(item: ReportItem, frame: pd.DataFrame, persona: str, user_id: int) -> None:
    """The result table, the chart, the comment, and the pin."""
    st.write(f"**{len(frame):,}** row(s), **{len(frame.columns)}** column(s).")

    if frame.empty:
        # Not an error: "no invoice was overdue this month" is a legitimate answer, and it is
        # one a report should be able to state.
        st.info(
            "The query ran and returned no rows. That may be the answer — pin it if so, or "
            "reword the request above.",
            icon=":material/info:",
        )
    else:
        st.dataframe(frame.head(PREVIEW_ROWS), width="stretch", key=f"ri_result_{item.item_id}")
        if len(frame) > PREVIEW_ROWS:
            st.caption(
                f"Showing the first {PREVIEW_ROWS} of {len(frame):,} rows. The report keeps all of them."
            )
        # The whole frame, not the previewed head: a chart of the first 500 of 20,000 rows
        # would be a different chart from the one the report is about to be given.
        _render_chart(item, frame)

    if st.button(
        "Rewrite comment" if item.comment else "Write comment",
        key=f"ri_comment_write_{item.item_id}",
        icon=":material/auto_awesome:",
        help="Ask the AI to describe this result in a few sentences. Yours to edit afterwards.",
    ):
        with st.spinner("Writing the comment…"):
            _write_comment(item, frame, persona, user_id)

    item.comment = st.text_area(
        "Comment",
        value=item.comment,
        key=f"ri_comment_{item.item_id}",
        height=120,
        placeholder="Write the note yourself, or press the button above to have it drafted.",
        help="Printed under this item in the report. Edit it freely.",
    )

    already_saved = item.is_saved()
    save_column, remove_column = st.columns(2)
    with save_column:
        if st.button(
            "Update in report" if already_saved else "Pin to report",
            key=f"ri_pin_{item.item_id}",
            type="primary",
            width="stretch",
            icon=":material/push_pin:",
            help=(
                "Lock in the rows above and send them, with the chart and comment, to this "
                "Task's **Report** tab where the report is assembled and downloaded."
            ),
        ):
            _save_to_report(item, frame)
            st.rerun(scope="app")
    with remove_column:
        # The only way out of the report for this item: the report screen no longer discards
        # anything a producer owns, so removal has to live where the item came from.
        if already_saved and st.button(
            "Remove from report",
            key=f"ri_unpin_{item.item_id}",
            width="stretch",
            icon=":material/delete:",
            help="Take this item back out of the report. Its request and SQL stay here.",
        ):
            _remove_from_report(item)
            st.rerun(scope="app")

    if already_saved:
        st.caption(
            f"Pinned {item.saved_run.ran_at} · {item.saved_run.row_count:,} row(s). "
            "It's on the **Report** tab — arrange it, edit the comment or download it there."
        )


def _render_report_item(item: ReportItem, schema: SchemaOptions, persona: str, user_id: int) -> None:
    """One report item's card: the request, the hints, the SQL, the result."""
    _render_heading_row(item)

    item.request = st.text_area(
        "What this should show, in plain language",
        value=item.request,
        key=f"ri_request_{item.item_id}",
        height=100,
        placeholder=REQUEST_PLACEHOLDER,
        help="Write it as you'd explain it to a colleague. The AI turns it into SQL.",
    )

    _render_hints(item, schema)
    _render_sql_controls(item, persona, user_id)

    if item.sql:
        with st.expander("SQL", key=f"ri_sql_expander_{item.item_id}", icon=":material/code:"):
            st.code(item.sql, language="sql")

    for notice in report_items_session.consume_notices(item.item_id):
        st.warning(notice, icon=":material/warning:")

    current = report_items_session.runtime(item.item_id)
    if current.error:
        st.error(current.error, icon=":material/error:")
        st.caption(
            "Reword the request above and press **Regenerate SQL** — the AI keeps this query "
            "as its starting point rather than beginning again."
        )

    if current.frame is not None:
        _render_results(item, current.frame, persona, user_id)


# --------------------------------------------------------------------------------------
# Column steps
# --------------------------------------------------------------------------------------


def _apply_column_step(item: ReportItem, user_id: int) -> None:
    """Turns a plain-language column change into `ALTER`/`UPDATE` and applies it.

    Deliberately **not** free-form SQL. `analyst/column_intent.py` has the model fill in
    parameters — which table, which column, what formula — and `engine/columns.py` builds the
    statement from them, probes it, and wraps the write in a transaction. A model that
    hallucinates therefore cannot produce a statement the guards never saw, and the recorded
    statement list comes out identical to the one the Chat page's conversational path writes —
    which is what makes requirement 8.2's replay work for both.

    The statements go into `engine.session`'s ordered list as well as onto the item: that list
    is what requirement 7.5 persists, and keeping the two in step is what lets a re-run apply
    the same changes in the same order.
    """
    profile = llm_session.active_profile(user_id)
    if profile is None:
        report_items_session.record_error(
            item.item_id, "No AI connection is selected — pick one in the sidebar."
        )
        return

    try:
        change = column_intent.handle_message(
            profile,
            engine_session.connection(),
            engine_session.schema_context(),
            item.request,
            column_hint=_hint_line(item),
            relationships=engine_session.get_relationships(),
        )
    except AnalystError as error:
        logger.info("Column step '%s' could not be applied: %s", item.display_heading(), error)
        item.last_error = str(error)
        report_items_session.record_error(item.item_id, str(error))
        return

    item.statements = list(change.statements)
    item.summary = change.summary
    item.description = change.explanation
    item.sql = "\n".join(change.statements)
    item.applied = True
    item.last_error = None
    report_items_session.clear_error(item.item_id)

    # Snapshotted *before* the rebuild, so the diff after it names exactly the columns this
    # step brought into existence — there is no other way to tell a new column from one that
    # was always there.
    known_columns = {entry.key for entry in engine_session.get_dictionary()}

    engine_session.add_statements(change.statements)
    engine_session.bump_rebuild()
    # Rebuilt here rather than on the next question: every item below this one is about to be
    # offered the new column in its hint pickers and to have it described to the model.
    entries = engine_session.refresh_dictionary()
    _describe_new_columns(entries, known_columns, change.explanation)

    report_items_session.queue_notices(item.item_id, change.warnings)
    logger.info("Applied column step '%s': %s", item.display_heading(), change.summary)


def _describe_new_columns(entries: list, known_columns: set, explanation: str) -> None:
    """Writes the model's sentence onto the dictionary entries this step created.

    Never overwrites a description that is already there — the same rule
    `dictionary.apply_suggestions` follows, because typed text is the user's and a generated
    sentence is not worth losing it for. The payoff runs past Setup's grid: `schema_context()`
    carries these descriptions into every prompt below this step.
    """
    text = (explanation or "").strip()
    if not text:
        return
    for entry in entries:
        if entry.key not in known_columns and not (entry.description or "").strip():
            entry.description = text
    engine_session.set_dictionary(entries)


def _render_column_step(item: ReportItem, schema: SchemaOptions, user_id: int) -> None:
    """One column step's card. No result table, no chart, no pin — its output *is* the data.

    A step is applied at most once per session, which is why the button disables afterwards:
    re-running an `ALTER TABLE … ADD COLUMN` on a column that already exists is an error, not
    an idempotent no-op.
    """
    _render_heading_row(item)

    item.request = st.text_area(
        "The column change, in plain language",
        value=item.request,
        key=f"ri_request_{item.item_id}",
        height=80,
        placeholder=COLUMN_PLACEHOLDER,
        disabled=item.applied,
        help=(
            "Add, update or remove a column — e.g. “Add tax = 10% of basic”. Every item below "
            "this one then sees the change."
            if not item.applied
            else "Applied. Delete this step and add it again to change what it does."
        ),
    )

    _render_hints(item, schema, disabled=item.applied)

    if not item.applied:
        if st.button(
            "Apply to the data",
            key=f"ri_apply_{item.item_id}",
            type="primary",
            icon=":material/play_arrow:",
            disabled=not item.request.strip(),
            help=(
                "Make this change to the loaded tables. It is applied once — the items below "
                "will see the result."
            ),
        ):
            with st.spinner("Applying the change…"):
                _apply_column_step(item, user_id)
            if item.applied:
                # Toasts survive the rerun below, which is what lets this be said here rather
                # than stored and replayed on the next run.
                st.toast(
                    "Applied. The updated data is on the **Setup** view.",
                    icon=":material/table:",
                )
            st.rerun(scope="app")
    else:
        st.success(item.summary or "Applied.", icon=":material/check_circle:")
        if item.description:
            st.caption(item.description)
        if st.button(
            "View data",
            key=f"ri_view_data_{item.item_id}",
            icon=":material/table:",
            help="Look at the loaded tables as they stand now, without leaving this list.",
        ):
            report_items_session.open_dialog("show_data", {})
            st.rerun(scope="app")

    if item.statements:
        with st.expander("SQL", key=f"ri_sql_expander_{item.item_id}", icon=":material/code:"):
            st.code(";\n".join(item.statements), language="sql")

    for notice in report_items_session.consume_notices(item.item_id):
        st.warning(notice, icon=":material/warning:")

    current = report_items_session.runtime(item.item_id)
    if current.error:
        st.error(current.error, icon=":material/error:")
        st.caption("Reword the change above and try again.")


# --------------------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------------------


def _render_add_bar() -> None:
    """The two ways to extend the list, said as two buttons rather than one with a picker.

    The kinds are not variants of one thing — one produces a table for the report and the
    other changes the data — so naming both is what makes the distinction visible before the
    user has committed to either.
    """
    item_column, step_column = st.columns(2)
    with item_column:
        if st.button(
            "Add report item",
            key="ri_add_item",
            type="primary",
            width="stretch",
            icon=":material/add:",
            help="Ask a question of the data. Its table, chart and comment go into the report.",
        ):
            report_items_session.open_dialog("add_item", {"kind": KIND_REPORT})
            st.rerun(scope="app")
    with step_column:
        if st.button(
            "Add column step",
            key="ri_add_column_step",
            width="stretch",
            icon=":material/table_edit:",
            help=(
                "Add, update or remove a column. Nothing goes into the report — every item "
                "below this step sees the changed data."
            ),
        ):
            report_items_session.open_dialog("add_item", {"kind": KIND_COLUMN})
            st.rerun(scope="app")


def _card_label(position: int, item: ReportItem) -> str:
    """The expander header: position, kind, name, and whether it has landed anywhere.

    The position is shown because it is load-bearing here in a way it never is in a chat — an
    item's place in the list decides which column steps have run before it.
    """
    if item.is_column_step():
        marker = "✅" if item.applied else "•"
        return f"{marker} {position}. Column step: {item.display_heading()}"
    marker = "📌" if item.is_saved() else "•"
    return f"{marker} {position}. {item.display_heading()}"


def render_report_items(user_id: int, persona: str = "") -> None:
    """The Report-Items view. Called by `task_builder.py` when its view control is on it.

    Args:
        user_id: whose AI connection to use.
        persona: the Task's persona and context (requirement 7.2), set once on the Setup view
            and passed to both this and the Checks view. Empty is fine — every prompt that
            takes it omits the section rather than sending a blank one.
    """
    _render_pending_dialog()

    st.caption(
        "Build the report a piece at a time, top to bottom. Pinned items go to the "
        "**Report** tab, where the report is arranged and downloaded."
    )

    _render_add_bar()

    items = report_items_session.get_items()
    if not items:
        st.info(
            "Nothing here yet. Add a report item, describe what it should show in plain "
            "language, and the AI will turn it into a query you can run and pin.",
            icon=":material/summarize:",
        )
        return

    # Built once for the whole run: the hint pickers offer the same options to every item and
    # nothing can change them mid-render.
    schema = SchemaOptions.current()

    for position, item in enumerate(items, start=1):
        with st.expander(
            _card_label(position, item),
            key=f"ri_exp_{item.item_id}",
            on_change="rerun",
            # A **constant**, like every `expanded=` in this app — Streamlit re-applies the
            # argument whenever its value changes, so anything dynamic would force cards open
            # or shut under the user. True because an item that has just been added is one the
            # user is about to write; collapsing the ones they're done with then sticks.
            expanded=True,
            icon=KIND_ICONS[item.kind],
        ) as panel:
            # Gated like every other expander in this app: a collapsed item runs no queries
            # and rasterizes no chart.
            if not panel.open:
                continue
            try:
                if item.is_column_step():
                    _render_column_step(item, schema, user_id)
                else:
                    _render_report_item(item, schema, persona, user_id)
            except ReportItemError as error:
                logger.exception("Report item '%s' failed to render.", item.display_heading())
                st.error(str(error), icon=":material/error:")
