"""Criteria-Based Exceptional Reporting — the Checks view (requirement 6.5).

A view fragment, not a registered `st.Page`. `st.navigation` only mounts what
`streamlit_app.py` lists, so nothing here auto-registers; `sidebar.py` sets the precedent
for UI that isn't a page. It lives beside the pages rather than inside `checks/` so the
package keeps the same shape as `analyst/` and `dashboard/` — only `session.py` imports
Streamlit — and so `chat_with_data.py`, already 1400 lines, grows by four.

It renders under the Chat page's `de_view` control because it needs precisely what that
page has already loaded: the DuckDB connection, the confirmed relationships and the column
dictionary. A separate page would have to rebuild all three.

**There is no report tab.** `docs/addtion.md` describes one, asking for per-criteria
results, a chart, editable remarks, an HTML preview, an HTML download and a
one-sheet-per-criteria Excel — and `app_pages/dashboard.py` already does every one of those
for a `PinnedItem`, including removing one the user doesn't want. So pressing **Save to
report** pins the result there (`dashboard.session.pin_result`) and the Dashboard page *is*
the report. The second tab is Actions, which is the only part of that description the
Dashboard has no answer for.

Every widget key carries its criteria's `check_id`. This is not cosmetic: Streamlit applies
`value=`/`default=` only when a key is first created, so a key shared between two criteria
keeps showing whichever was created first — the trap already documented in `settings.py`.
"""

import logging
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from analyst import charts
from app_pages import chart_controls
from chat_types import session as chat_type_session
from checks import actions as checks_actions
from checks import db as checks_db
from checks import remarks as checks_remarks
from checks import session as checks_session
from checks import sql_builder
from checks import summary as checks_summary
from checks.actions import KINDS, kind_of
from checks.exceptions import ChecksError, ChecksStorageError, CheckSqlError
from checks.model import (
    ACTION_EMAIL,
    COLUMN_MET,
    FILTER_ALL,
    FILTER_FAILURES,
    FILTER_PASSES,
    MET_YES,
    RESULT_FILTERS,
    ActionDraft,
    Check,
    CheckSet,
    add_check,
    find_check,
    freeze_run,
    met_values,
    remove_action,
    remove_check,
)
from dashboard import session as dashboard_session
from engine import session as engine_session
from llm import session as llm_session

logger = logging.getLogger(__name__)

# The result preview is capped for the same reason every other preview on this page is:
# a criteria over a 200,000-row table is a perfectly normal thing to write, and rendering
# all of it would cost seconds per rerun to show rows nobody scrolls to. The saved run and
# the exported report both keep every row.
PREVIEW_ROWS = 500

# Appended to a criteria's heading when the rows pinned to the report are only part of the
# run. `FILTER_ALL` is absent on purpose — a whole result needs no qualifier.
FILTER_SUFFIXES = {
    FILTER_FAILURES: " — breaches only",
    FILTER_PASSES: " — passing records only",
}

PERSONA_PLACEHOLDER = (
    "You are a finance controller verifying salary processing accuracy. Bonuses are "
    "reviewed monthly and anything above policy is escalated to the department head."
)

CRITERIA_PLACEHOLDER = (
    "Bonus must be at most 5% of basic if department = HR, 10% if department = accounts, "
    "otherwise 8%."
)


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def _dismiss_dialog() -> None:
    """Clears the flag when a dialog is dismissed by the X, ESC or a click outside.

    Without it the flag survives, and the next unrelated rerun reopens the same dialog —
    the behaviour `chat_with_data.py::_dismiss_dialog` documents in full.
    """
    checks_session.close_dialog()


@st.dialog("Add a criteria", on_dismiss=_dismiss_dialog)
def _dialog_add_check(payload: dict) -> None:
    """Asks only for the name (addtion.md step: "Click → prompts for a criteria name").

    The rule itself is written in the expander this creates, where there is room for it —
    a dialog would make the most important field on the page the smallest one.
    """
    name = st.text_input(
        "Criteria name",
        key="ck_new_check_name",
        placeholder="e.g. Bonus within policy",
        help="A short label for this rule. It becomes the heading in the report.",
    )

    confirm, cancel = st.columns(2)
    with confirm:
        if st.button(
            "Add",
            key="ck_new_check_add",
            type="primary",
            width="stretch",
            disabled=not name.strip(),
            help="Create this criteria and open it below.",
        ):
            add_check(checks_session.get_set(), name)
            checks_session.close_dialog()
            st.rerun(scope="app")
    with cancel:
        if st.button("Cancel", key="ck_new_check_cancel", width="stretch", help="Go back without adding."):
            checks_session.close_dialog()
            st.rerun(scope="app")


@st.dialog("Load a saved criteria set", on_dismiss=_dismiss_dialog)
def _dialog_load_set(payload: dict) -> None:
    user_id = payload["user_id"]
    chat_type_id = chat_type_session.active_id()
    chat_type_name = chat_type_session.active_name()

    # Requirement 6.6: with a chat type selected, this offers that setup's sets. The toggle
    # is the escape hatch — a set saved before chat types existed, or under a different
    # setup, must never become unreachable.
    every_scope = st.toggle(
        "Show all my sets",
        key="ck_load_set_all_scopes",
        help="Off shows only the sets saved under the chat type you're using now.",
    )

    try:
        saved = checks_db.list_sets(user_id, chat_type_id, every_scope=every_scope)
    except ChecksStorageError as error:
        logger.exception("Could not list saved criteria sets for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return

    if not saved:
        scope = f" under **{chat_type_name}**" if chat_type_id is not None else " without a chat type"
        st.info(
            f"You haven't saved any criteria sets{'' if every_scope else scope} yet.",
            icon=":material/info:",
        )
        return

    chosen = st.selectbox(
        "Saved sets",
        options=saved,
        key="ck_load_set_picker",
        format_func=lambda row: f"{row['name']} — last saved {row['updated_at']}",
        help="Loading a set replaces the criteria currently on screen.",
    )

    st.caption(
        "A loaded set brings back its rules and their SQL, but not their results — those "
        "described data that is no longer loaded. Press **Run saved SQL** on each criteria to "
        "get this month's numbers; the AI is only involved if you ask it to regenerate."
    )

    load, delete = st.columns(2)
    with load:
        if st.button("Load", key="ck_load_set_confirm", type="primary", width="stretch", help="Open this set."):
            try:
                checks_session.replace_set(checks_db.load_set(chosen["set_id"], user_id))
            except ChecksStorageError as error:
                logger.exception("Could not load criteria set %s.", chosen["set_id"])
                st.error(str(error), icon=":material/error:")
                return
            checks_session.close_dialog()
            st.rerun(scope="app")
    with delete:
        if st.button(
            "Delete",
            key="ck_load_set_delete",
            width="stretch",
            icon=":material/delete:",
            help="Permanently delete this saved set. The criteria on screen are not affected.",
        ):
            try:
                checks_db.delete_set(chosen["set_id"], user_id)
            except ChecksStorageError as error:
                logger.exception("Could not delete criteria set %s.", chosen["set_id"])
                st.error(str(error), icon=":material/error:")
                return
            st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# The set: persona, save, load
# --------------------------------------------------------------------------------------


def _render_set_bar(user_id: int) -> None:
    """Name the set, save it, load another. Requirement 6.5's one persistent piece."""
    check_set = checks_session.get_set()

    name_column, save_column, load_column = st.columns([3, 1, 1], vertical_alignment="bottom")
    with name_column:
        check_set.name = st.text_input(
            "Criteria set",
            value=check_set.name,
            key="ck_set_name",
            placeholder="e.g. Monthly payroll checks",
            help="What to call this set of rules when it's saved.",
        )
    with save_column:
        if st.button(
            "Save set",
            key="ck_save_set",
            icon=":material/save:",
            width="stretch",
            disabled=not check_set.name.strip(),
            help="Store these rules so you can run them again against next month's file. Saved "
            "under the chat type you're using, so it's offered back the next time you pick it.",
        ):
            try:
                # A set always takes the scope it is saved under — see `checks.db.save_set`.
                checks_db.save_set(user_id, check_set, chat_type_session.active_id())
            except ChecksStorageError as error:
                logger.exception("Could not save criteria set for user %s.", user_id)
                st.error(str(error), icon=":material/error:")
            else:
                scope = chat_type_session.active_name()
                where = f" under “{scope}”" if scope else ""
                st.success(f"Saved “{check_set.display_name()}”{where}.", icon=":material/check_circle:")
    with load_column:
        if st.button(
            "Load set",
            key="ck_load_set",
            icon=":material/folder_open:",
            width="stretch",
            help="Open a criteria set you saved earlier.",
        ):
            checks_session.open_dialog("load_set", {"user_id": user_id})
            st.rerun(scope="app")

    check_set.persona = st.text_area(
        "Persona and context",
        value=check_set.persona,
        key="ck_persona",
        height=100,
        placeholder=PERSONA_PLACEHOLDER,
        help=(
            "Who the AI is acting as, plus any background it should apply. Used when it "
            "writes the SQL, the remarks and the follow-up drafts — set once for the whole set."
        ),
    )


# --------------------------------------------------------------------------------------
# One criteria
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaOptions:
    """What the hint pickers offer, built once per run and shared by every criteria.

    Columns come from the dictionary rather than from DuckDB directly, so they carry
    calculated columns and stay in step with what the model is told the schema is. The sets
    exist because the `default=` filters are membership tests, and a list makes those
    O(hints x options) per criteria.
    """

    tables: list[str]
    columns: list[str]
    table_set: frozenset[str]
    column_set: frozenset[str]

    @classmethod
    def current(cls) -> "SchemaOptions":
        tables = engine_session.table_names()
        columns = [entry.qualified_name for entry in engine_session.get_dictionary()]
        return cls(tables, columns, frozenset(tables), frozenset(columns))


def _render_hints(check: Check, schema: SchemaOptions) -> None:
    """Addtion.md step 2 — the user's guess at what the rule touches.

    Explicitly a hint. The model also receives the real schema and is instructed to correct
    a wrong guess rather than fail on it, which is why neither picker is required.

    `schema` is built once per run and passed down: the option lists are identical for every
    criteria and cannot change mid-render, so rebuilding them per criteria walks the whole
    column dictionary again for an answer already in hand.
    """
    table_column, column_column = st.columns(2)
    with table_column:
        check.hint_tables = st.multiselect(
            "Tables involved (optional)",
            options=schema.tables,
            default=[name for name in check.hint_tables if name in schema.table_set],
            key=f"ck_hint_tables_{check.check_id}",
            help="A hint for the AI. It also sees your real schema and will correct a wrong guess.",
        )
    with column_column:
        check.hint_columns = st.multiselect(
            "Columns involved (optional)",
            options=schema.columns,
            default=[name for name in check.hint_columns if name in schema.column_set],
            key=f"ck_hint_columns_{check.check_id}",
            help="A hint for the AI. Leave empty if you're not sure which columns the rule uses.",
        )


def _run_test(check: Check, user_id: int) -> None:
    """Generates, runs and validates this criteria's SQL, recording whatever happened.

    Both outcomes are stored rather than raised: a failure is the input to the refine loop,
    so it has to survive the rerun that follows this press.
    """
    profile = llm_session.active_profile(user_id)
    if profile is None:
        checks_session.record_error(check.check_id, "No AI connection is selected — pick one in the sidebar.")
        return

    try:
        sql, frame = sql_builder.generate_and_run(
            profile,
            checks_session.get_set().persona,
            check,
            engine_session.schema_context(),
            engine_session.connection(),
        )
    except CheckSqlError as error:
        logger.info("Criteria '%s' could not be tested: %s", check.display_name(), error)
        check.last_error = str(error)
        checks_session.record_error(check.check_id, str(error))
        return

    check.sql = sql
    check.last_error = None
    checks_session.record_result(check.check_id, frame)


def _run_saved_sql(check: Check) -> None:
    """Runs the statement this criteria already has, without asking a model for a new one.

    This is what makes a saved set a *recipe*. The SQL is the part `to_json` stores precisely
    because it is what can be run again next month; regenerating it on every re-run would
    spend a provider call per criteria to replace a query the user has already tuned, and
    could come back shaped differently from the one they saved.

    A failure is recorded, not repaired: the message says which table or column is missing,
    and **Regenerate SQL** is right beside it if that is what the user wants. Nothing here
    reaches a provider, so the error costs nothing to read first.
    """
    try:
        frame = sql_builder.run_check(engine_session.connection(), check.sql or "")
    except CheckSqlError as error:
        logger.info("Criteria '%s' could not be re-run: %s", check.display_name(), error)
        # Kept on the check as well as in the runtime: if the user does press Regenerate
        # next, this is what tells the model what went wrong.
        check.last_error = str(error)
        checks_session.record_error(check.check_id, str(error))
        return

    check.last_error = None
    checks_session.record_result(check.check_id, frame)


def _report_heading(check: Check, mode: str) -> str:
    """What the pinned copy is called, saying so when it holds only part of the run.

    A report table that is a subset has to admit it: the remarks printed directly above it
    quote the *whole* run's counts, so "3 of 47 breached" over a table of three rows would
    otherwise read as the entire result.
    """
    suffix = FILTER_SUFFIXES.get(mode, "")
    return f"{check.display_name()}{suffix}"


def _chart_figure(check: Check, shown: pd.DataFrame):
    """The chart to pin beside the table, or None when this criteria has none.

    Drawn fresh from the rows being pinned rather than reusing the figure on screen, so that
    the report gets a chart of exactly what the report is getting — the two are the same
    rows today, and this is what keeps them the same if that ever stops being true.

    A chart that fails to draw costs the chart, never the save: the table, the counts and the
    remarks are the report item, and the picture is the part that can be missing.
    """
    if check.chart is None:
        return None
    figure, warnings = charts.render_chart(shown, check.chart, check.chart_style)
    if figure is None:
        logger.warning(
            "Criteria '%s' was saved without its chart: %s", check.display_name(), " ".join(warnings)
        )
    return figure


def _save_to_report(check: Check, frame: pd.DataFrame, shown: pd.DataFrame, mode: str, user_id: int) -> None:
    """Addtion.md step 6, plus the pin. One press: the run is frozen, the remarks are
    written, and the rows — with this criteria's chart, if it has one — go to the report.

    Two frames, deliberately:

    - `frame` — every row the test returned — is what gets frozen. The saved run drives the
      pass/fail counts, the remarks prompt and every action draft, all of which are drafted
      from the *failures*. Freezing the filtered view instead would mean that saving while
      "Passes" is selected produced remarks reading "0 records breached the rule" and an
      Actions tab with nothing to write about.
    - `shown` — whatever the All / Failures / Passes control is currently showing — is what
      goes into the report, because that is the table the user is looking at when they press
      the button.

    The run is frozen first so the remarks describe exactly what the report will show, and
    the pin happens whether or not the remarks call succeeded — a provider hiccup must not
    cost the user the run they just saved.
    """
    check.saved_run = freeze_run(check.sql or "", frame)

    profile = llm_session.active_profile(user_id)
    if profile is None:
        checks_session.queue_notices(
            check.check_id, ["Saved without remarks — no AI connection is selected in the sidebar."]
        )
    else:
        written, warnings = checks_remarks.write_remarks(profile, checks_session.get_set().persona, check)
        if written:
            check.remarks = written
        # Queued rather than drawn: this runs inside a press that ends in `st.rerun`, which
        # throws away everything drawn during it.
        checks_session.queue_notices(check.check_id, warnings)

    item = dashboard_session.pin_result(
        checks_session.source_id_for(check.check_id),
        heading=_report_heading(check, mode),
        comment=check.remarks,
        sql=check.sql,
        frame=shown,
        figure=_chart_figure(check, shown),
    )
    logger.info("Saved criteria '%s' to the report as item %s.", check.display_name(), item.item_id)


def _unpin_everything(check: Check) -> None:
    """Takes both of a criteria's items out of the report — its results and its follow-ups.

    Called from the two places a criteria stops belonging in the report: **Remove from
    report**, and deleting the criteria outright. It lives at the page layer, next to
    `discard_runtime`, because `checks/` knows nothing about `dashboard/` and this is not
    the module to make it start.
    """
    dashboard_session.unpin_source(checks_session.source_id_for(check.check_id))
    dashboard_session.unpin_source(checks_session.actions_source_id_for(check.check_id))


def _remove_from_report(check: Check) -> None:
    """Undoes a Save: the run is released and both items leave the report.

    The saved run goes as well as the item, because its presence is what puts this criteria
    in the Actions tab — leaving it would offer follow-ups drafted from a result the user
    has just said they don't want reported. The rule, its hints and its SQL are untouched:
    pressing Test then Save again puts everything back.
    """
    check.saved_run = None
    _unpin_everything(check)
    logger.info("Removed criteria '%s' from the report.", check.display_name())


def _chart_keys(check: Check) -> chart_controls.ChartKeys:
    """The widget namespace one criteria's chart panel owns."""
    return chart_controls.ChartKeys(prefix="ck_chart", suffix=f"_{check.check_id}")


def _render_chart(check: Check, shown: pd.DataFrame) -> None:
    """A chart of the rows on screen, built by the user rather than chosen for them.

    No criteria gets a chart automatically — an automatic chart of one rule's rows, repeated
    down the page for every rule, is the thing this tab deliberately doesn't do, and the
    stacked summary at the foot of the tab is the picture that compares rules to each other.
    A chart the user asks for and builds is a different object: "breaches by department" is
    exactly what belongs beside the table in the report.

    Drawn from `shown`, the same rows the table above holds, so a report item headed
    "breaches only" cannot carry a chart that quietly includes the passes.

    The figure is rebuilt on every rerun rather than stored: the frame is already in memory,
    and `Check` holds the recipe so that saving and reloading a criteria set brings the chart
    back — a Plotly figure would neither survive that round trip nor describe itself.
    """
    kinds = charts.available_chart_types(shown)
    if not kinds:
        return

    keys = _chart_keys(check)
    if check.chart is None:
        if st.button(
            "Generate chart",
            key=f"ck_chart_new_{check.check_id}",
            icon=":material/insert_chart:",
            help="Plot the rows above. Nothing is re-run and nothing is asked of the AI — you choose what it plots, and it is saved with this criteria.",
        ):
            chosen, _ = chart_controls.seed_choices(shown, check.criteria_text)
            check.chart = chosen
            check.chart_style = charts.ChartStyle()
            st.rerun(scope="app")
        return

    figure, warnings = charts.render_chart(shown, check.chart, check.chart_style)
    if figure is None:
        st.caption(f":orange[{' '.join(warnings) or 'These rows cannot be charted.'}]")
    else:
        st.plotly_chart(figure, key=f"ck_chart_fig_{check.check_id}", width="stretch")
        for warning in warnings:
            st.caption(f":orange[{warning}]")

    # `key` + `on_change` is what makes the open/closed state a widget rather than a fresh
    # `expanded=False` on every run. Every control in here ends in `st.rerun`, so without it
    # the panel shut itself the moment anything was changed in it.
    with st.expander(
        "Customize chart",
        icon=":material/tune:",
        key=f"ck_chart_panel_{check.check_id}",
        on_change="rerun",
    ):
        data_tab, style_tab = st.tabs(["Data", "Style"], key=f"ck_chart_tabs_{check.check_id}")
        with data_tab:
            chosen = chart_controls.render_data_controls(shown, check.chart, kinds, keys)
        with style_tab:
            # `chosen or check.chart` because the style controls only need the shape of the
            # chart to decide what to offer, and an empty Values box isn't a reason to empty
            # the Style tab too.
            chosen_style = chart_controls.render_style_controls(
                shown,
                chosen or check.chart,
                check.chart_style or charts.ChartStyle(),
                keys,
                title_placeholder=chart_controls.figure_title(figure),
            )
        if st.button(
            "Remove chart",
            key=f"ck_chart_remove_{check.check_id}",
            icon=":material/delete:",
            help="Take the chart off this criteria. Its result table and everything else stay as they are — the report keeps the table on the next save.",
        ):
            check.chart = None
            check.chart_style = None
            chart_controls.clear_controls(keys)
            st.rerun(scope="app")

    if chosen is None:
        st.caption(":orange[Pick at least one value to plot — showing the last chart until you do.]")
        return
    if chosen == check.chart and chosen_style == check.chart_style:
        return

    check.chart = chosen
    check.chart_style = chosen_style
    st.rerun(scope="app")


def _render_results(check: Check, frame: pd.DataFrame, user_id: int) -> None:
    """The result, the All/Failures/Passes filter, the chart, and Save."""
    # One pass over `criteria_met` for the headline and the table both, rather than
    # `filter_rows` twice — that materialized a whole DataFrame of passing rows to take its
    # length, on every rerun, for one integer.
    verdicts = met_values(frame) if COLUMN_MET in frame.columns else None
    passing_mask = None if verdicts is None else verdicts == MET_YES.casefold()
    pass_count = 0 if passing_mask is None else int(passing_mask.sum())
    st.write(f"**{len(frame) - pass_count}** of **{len(frame)}** record(s) breached this rule.")

    mode = st.segmented_control(
        "Show",
        options=RESULT_FILTERS,
        default=FILTER_ALL,
        required=True,
        key=f"ck_filter_{check.check_id}",
        help="Narrow the table to the records that failed, or the ones that passed.",
    )
    if passing_mask is None or mode == FILTER_ALL:
        shown = frame
    else:
        shown = frame[passing_mask if mode == FILTER_PASSES else ~passing_mask]

    if shown.empty:
        st.info("No records to show for that filter.", icon=":material/info:")
    else:
        st.dataframe(shown.head(PREVIEW_ROWS), width="stretch", key=f"ck_result_{check.check_id}")
        if len(shown) > PREVIEW_ROWS:
            st.caption(f"Showing the first {PREVIEW_ROWS} of {len(shown)} rows. The report keeps all of them.")
        # `shown` whole, not the previewed head: a chart of the first 500 of 20,000 breaches
        # would be a different chart from the one the report is about to be given.
        _render_chart(check, shown)

    already_saved = check.is_saved()
    save_column, remove_column = st.columns([1, 1])
    with save_column:
        if st.button(
            "Update in report" if already_saved else "Save to report",
            key=f"ck_save_{check.check_id}",
            type="primary",
            width="stretch",
            icon=":material/push_pin:",
            help=(
                "Lock in the rows shown above — whichever of All, Failures or Passes is "
                "selected — and send them to the Dashboard, where the report is assembled "
                "and downloaded."
            ),
        ):
            _save_to_report(check, frame, shown, mode, user_id)
            st.rerun(scope="app")
    with remove_column:
        # The only way out of the report for a criteria's item: the Dashboard no longer
        # discards anything a producer owns, so removal has to live where the item came from.
        if already_saved and st.button(
            "Remove from report",
            key=f"ck_unsave_{check.check_id}",
            width="stretch",
            icon=":material/delete:",
            help="Take this criteria's results back out of the report. The rule and its SQL stay here.",
        ):
            _remove_from_report(check)
            st.rerun(scope="app")

    if already_saved:
        st.caption(
            f"Saved {check.saved_run.ran_at} · {check.saved_run.fail_count} failure(s). "
            "It's on the **Dashboard** page — arrange, edit the remarks or download it there."
        )


def _render_check(check: Check, schema: SchemaOptions, user_id: int) -> None:
    """One criteria's expander body: the rule, the hints, the SQL, the result."""
    header, delete_column = st.columns([5, 1], vertical_alignment="center")
    with header:
        check.name = st.text_input(
            "Criteria name",
            value=check.name,
            key=f"ck_name_{check.check_id}",
            help="The heading this criteria gets in the report.",
        )
    with delete_column:
        if st.button(
            "Delete",
            key=f"ck_delete_{check.check_id}",
            icon=":material/delete:",
            width="stretch",
            help="Remove this criteria and take its results back out of the report.",
        ):
            # The report items go with it. They used to be left behind deliberately, on the
            # grounds that the Dashboard had its own Remove button for them — it no longer
            # does for anything a producer owns, so leaving them would strand a tile that
            # nothing in the app could delete.
            _unpin_everything(check)
            remove_check(checks_session.get_set(), check.check_id)
            checks_session.discard_runtime(check.check_id)
            st.rerun(scope="app")

    check.criteria_text = st.text_area(
        "The rule, in plain language",
        value=check.criteria_text,
        key=f"ck_text_{check.check_id}",
        height=100,
        placeholder=CRITERIA_PLACEHOLDER,
        help="Write it as you'd explain it to a colleague. The AI turns it into SQL.",
    )

    _render_hints(check, schema)

    # Two buttons once there is SQL, not one. Writing the query and running it are separate
    # jobs, and only the first costs a model call — a criteria loaded from a saved set already
    # has its statement, so re-running last month's rules against this month's file should be
    # free. Regenerating is then something the user asks for when the rule has changed or the
    # query is wrong, which is `addtion.md` step 4's refine loop, unchanged.
    if check.sql:
        run_column, regenerate_column = st.columns(2)
        with run_column:
            if st.button(
                "Run saved SQL",
                key=f"ck_run_{check.check_id}",
                type="primary",
                width="stretch",
                icon=":material/play_arrow:",
                help=(
                    "Run the query below against the data loaded now. No AI call — the rule "
                    "and its SQL are used exactly as they stand."
                ),
            ):
                with st.spinner("Running the query…"):
                    _run_saved_sql(check)
                st.rerun(scope="app")
        with regenerate_column:
            if st.button(
                "Regenerate SQL",
                key=f"ck_test_{check.check_id}",
                width="stretch",
                icon=":material/auto_awesome:",
                disabled=not check.criteria_text.strip(),
                help=(
                    "Have the AI rewrite this query and run it. It starts from the statement "
                    "below and changes as little as it can — use it after editing the rule, "
                    "or when the saved query no longer fits the data."
                ),
            ):
                with st.spinner("Writing and running the query…"):
                    _run_test(check, user_id)
                st.rerun(scope="app")
    elif st.button(
        "Generate & test SQL",
        key=f"ck_test_{check.check_id}",
        icon=":material/play_arrow:",
        disabled=not check.criteria_text.strip(),
        help="Write the SQL for this rule and run it.",
    ):
        with st.spinner("Writing and running the query…"):
            _run_test(check, user_id)
        st.rerun(scope="app")

    if check.sql:
        with st.expander("SQL", key=f"ck_sql_expander_{check.check_id}", icon=":material/code:"):
            st.code(check.sql, language="sql")

    for notice in checks_session.consume_notices(check.check_id):
        st.warning(notice, icon=":material/warning:")

    current = checks_session.runtime(check.check_id)
    if current.error:
        st.error(current.error, icon=":material/error:")
        st.caption(
            "Reword the rule above and press **Regenerate SQL** — the AI keeps this query as "
            "its starting point rather than beginning again."
        )

    if current.frame is not None:
        _render_results(check, current.frame, user_id)


# --------------------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------------------


def _write_set_summary(user_id: int) -> None:
    """Fills the summary box with an AI draft, or says why it couldn't.

    On a button rather than on every run: this is a provider call, and `_render_design`
    re-executes on every keystroke anywhere on the page. Automatic would mean one request per
    character typed into any criteria on screen.
    """
    check_set = checks_session.get_set()
    profile = llm_session.active_profile(user_id)
    if profile is None:
        st.warning(
            "No AI connection is selected — pick one in the sidebar, or write the summary yourself.",
            icon=":material/warning:",
        )
        return

    written, warnings = checks_remarks.write_set_summary(check_set, profile)
    for warning in warnings:
        st.warning(warning, icon=":material/warning:")
    if not written:
        return

    check_set.summary = written
    # Assigned to the widget's own key, not just to the model: a text area that already
    # exists ignores `value=` for the rest of the session, so this is what actually puts the
    # new wording on screen.
    st.session_state[checks_session.CK_SUMMARY_TEXT_KEY] = written


def _save_summary_to_report(check_set: CheckSet, frame: pd.DataFrame) -> None:
    """Pins the overview as its own report item, above the individual criteria.

    The figure is `combined_chart`'s two-panel one rather than either of the two on screen:
    an item holds a single figure, and a report showing only absolute counts would invite
    the comparison the shares panel exists to prevent. It is built here, on the press, rather
    than every run — the Design tab reruns on each keystroke and nothing else needs it.

    The counts table travels with the chart so the Excel export carries the numbers rather
    than a picture of them — the same reason every other pinned item keeps its frame.
    """
    item = dashboard_session.pin_result(
        checks_session.SUMMARY_SOURCE_ID,
        heading=f"{check_set.display_name()} — overview",
        comment=check_set.summary,
        frame=frame,
        figure=checks_summary.combined_chart(frame),
    )
    logger.info("Saved the criteria-set overview to the report as item %s.", item.item_id)


def _render_summary(user_id: int) -> None:
    """The whole-set picture at the foot of the Design tab: one chart, one summary, one Save."""
    st.markdown("#### Summary")

    check_set = checks_session.get_set()
    saved = check_set.saved_checks()
    if not saved:
        st.info(
            "Save a criteria's results above and it joins this summary — it counts only what "
            "is in the report.",
            icon=":material/insights:",
        )
        return

    frame = checks_summary.counts_frame(saved)
    criteria_count, record_count, breach_count = checks_summary.totals(frame)
    st.write(
        f"**{breach_count:,}** breach(es) across **{record_count:,}** record(s), "
        f"from **{criteria_count}** saved criteria."
    )

    count_figure = checks_summary.count_chart(frame)
    percent_figure = checks_summary.percent_chart(frame)
    count_column, percent_column = st.columns(2)
    with count_column:
        st.plotly_chart(count_figure, width="stretch", key="ck_summary_count_chart")
    with percent_column:
        st.plotly_chart(percent_figure, width="stretch", key="ck_summary_percent_chart")

    untested = len(check_set.checks) - len(saved)
    if untested:
        st.caption(
            f"{untested} criteria not counted here — only saved results reach the summary and the report."
        )

    if st.button(
        "Rewrite summary" if check_set.summary else "Write summary",
        key="ck_summary_write",
        icon=":material/auto_awesome:",
        help="Ask the AI to summarise every saved criteria in a few points. Yours to edit afterwards.",
    ):
        with st.spinner("Writing the summary…"):
            _write_set_summary(user_id)

    check_set.summary = st.text_area(
        "Summary",
        value=check_set.summary,
        key=checks_session.CK_SUMMARY_TEXT_KEY,
        height=160,
        placeholder="Write the overview yourself, or press the button above to have it drafted.",
        help="Printed under the summary chart in the report. Edit it freely.",
    )

    save_column, remove_column = st.columns(2)
    with save_column:
        if st.button(
            "Save summary to report",
            key="ck_summary_save",
            type="primary",
            width="stretch",
            icon=":material/push_pin:",
            help=(
                "Send both charts and this summary to the Dashboard as one item, to sit "
                "above the criteria."
            ),
        ):
            _save_summary_to_report(check_set, frame)
            st.rerun(scope="app")
    with remove_column:
        if st.button(
            "Remove from report",
            key="ck_summary_unsave",
            width="stretch",
            icon=":material/delete:",
            help="Take the overview back out of the report. The summary text stays here.",
        ):
            dashboard_session.unpin_source(checks_session.SUMMARY_SOURCE_ID)
            st.rerun(scope="app")


def _render_design(user_id: int) -> None:
    _render_set_bar(user_id)
    st.divider()

    check_set = checks_session.get_set()
    if st.button(
        "Add criteria",
        key="ck_add_check",
        icon=":material/add:",
        type="primary",
        help="Add a new rule to this set.",
    ):
        checks_session.open_dialog("add_check")
        st.rerun(scope="app")

    if not check_set.checks:
        st.info(
            "No criteria yet. Add one, write the rule in plain language, and the AI will "
            "turn it into a query you can test.",
            icon=":material/rule:",
        )
        return

    # Built once for the whole run: the hint pickers offer the same options to every
    # criteria, and nothing can change them mid-render.
    schema = SchemaOptions.current()

    for check in check_set.checks:
        marker = "✅" if check.is_saved() else "•"
        with st.expander(
            f"{marker} Criteria: {check.display_name()}",
            key=f"ck_exp_{check.check_id}",
            on_change="rerun",
            # A **constant**, like every `expanded=` on this page — Streamlit re-applies the
            # argument whenever its value changes, so anything dynamic would force criteria
            # open or shut under the user. True because a criteria that has just been added
            # is one the user is about to write, and opening it by hand first is a step for
            # nothing; collapsing the ones they're done with then sticks.
            expanded=True,
            icon=":material/rule:",
        ) as panel:
            # Gated like every other expander on this page: a collapsed criteria runs no
            # queries and rasterizes no chart.
            if panel.open:
                _render_check(check, schema, user_id)

    st.divider()
    _render_summary(user_id)


def _pin_confirmed_actions(check: Check) -> None:
    """Puts a criteria's confirmed drafts into the report, or takes them out again.

    A second item under the same criteria rather than text appended to the first, so the
    drafts appear in the exported HTML and Excel through the ordinary item path — no export
    code of their own, and the user can move or remove them on the Dashboard like anything
    else.
    """
    source_id = checks_session.actions_source_id_for(check.check_id)
    rows = checks_actions.drafts_frame_rows(check)

    if not rows:
        dashboard_session.unpin_source(source_id)
        return

    dashboard_session.pin_result(
        source_id,
        heading=f"{check.display_name()} — follow-up actions",
        comment="Drafted for review. Nothing has been sent.",
        frame=pd.DataFrame(rows),
    )


@st.dialog("Add a follow-up", width="large", on_dismiss=_dismiss_dialog)
def _dialog_add_action(payload: dict) -> None:
    """Who it's for and when — the drafting happens after, from the failing rows."""
    check = find_check(checks_session.get_set(), payload["check_id"])
    if check is None:
        checks_session.close_dialog()
        return

    kind = st.segmented_control(
        "Type",
        options=list(KINDS),
        format_func=lambda value: KINDS[value].label,
        default=ACTION_EMAIL,
        required=True,
        key=f"ck_new_action_kind_{check.check_id}",
        help="What kind of follow-up this is. It decides what the AI drafts.",
    )

    recipients: dict[str, list[str]] = {}
    for field in KINDS[kind].recipient_fields:
        typed = st.text_input(
            field.title(),
            key=f"ck_new_action_{field}_{check.check_id}",
            placeholder="name@example.com, other@example.com",
            help="Comma-separated. Recorded on the draft — nothing is sent from here.",
        )
        recipients[field] = [person.strip() for person in typed.split(",") if person.strip()]

    when = ""
    if KINDS[kind].when_label:
        when = st.text_input(
            KINDS[kind].when_label,
            key=f"ck_new_action_when_{check.check_id}",
            placeholder="2026-08-14 10:00",
            help=(
                "Written into the draft. Use YYYY-MM-DD HH:MM if you want the downloaded "
                "calendar invite to carry a start time."
            ),
        )

    subject = st.text_input(
        "Subject or title (optional)",
        key=f"ck_new_action_subject_{check.check_id}",
        help="Leave blank and the AI writes one. Anything you type here is kept as-is.",
    )

    add, cancel = st.columns(2)
    with add:
        if st.button(
            "Draft it",
            key=f"ck_new_action_add_{check.check_id}",
            type="primary",
            width="stretch",
            help="Create the draft and let the AI write it from this criteria's failing rows.",
        ):
            draft = ActionDraft(kind=kind, recipients=recipients, when=when, subject=subject.strip())
            check.actions.append(draft)
            # Drafted here rather than on the next run: the dialog is still open, so the
            # spinner sits where the user is looking, and one press produces one draft
            # instead of a draft that appears blank and fills in a moment later.
            with st.spinner("Drafting…"):
                _write_draft(check, draft, payload["user_id"])
            checks_session.close_dialog()
            st.rerun(scope="app")
    with cancel:
        if st.button(
            "Cancel",
            key=f"ck_new_action_cancel_{check.check_id}",
            width="stretch",
            help="Go back without adding.",
        ):
            checks_session.close_dialog()
            st.rerun(scope="app")


def _write_draft(check: Check, draft: ActionDraft, user_id: int) -> None:
    """Fills a draft's wording from the criteria's failures."""
    profile = llm_session.active_profile(user_id)
    if profile is None:
        checks_session.queue_notices(
            check.check_id, ["No AI connection is selected — write the follow-up yourself, or pick one in the sidebar."]
        )
        return

    _, warnings = checks_actions.draft_action(profile, checks_session.get_set().persona, check, draft)
    checks_session.queue_notices(check.check_id, warnings)


def _render_action(check: Check, draft: ActionDraft, user_id: int) -> None:
    """One draft: editable wording, Confirm, download, delete."""
    suffix = f"{check.check_id}_{draft.action_id}"

    kind = kind_of(draft)
    st.write(f"**{kind.label}** · {draft.display_recipients() or 'no recipients'}")
    if draft.when:
        st.caption(f"{kind.when_label or 'When'}: {draft.when}")

    draft.subject = st.text_input(
        "Subject",
        value=draft.subject,
        key=f"ck_action_subject_{suffix}",
        help="The subject line, meeting title or task name.",
    )
    draft.body = st.text_area(
        "Body",
        value=draft.body,
        key=f"ck_action_body_{suffix}",
        height=180,
        help="The AI's draft, yours to edit. Nothing is sent from this app.",
    )

    redraft, confirm, download, delete = st.columns(4)
    with redraft:
        if st.button(
            "Redraft",
            key=f"ck_action_redraft_{suffix}",
            width="stretch",
            icon=":material/refresh:",
            help="Ask the AI to write it again from this criteria's failing rows.",
        ):
            with st.spinner("Drafting…"):
                _write_draft(check, draft, user_id)
            st.rerun(scope="app")
    with confirm:
        if st.button(
            "Unconfirm" if draft.confirmed else "Confirm",
            key=f"ck_action_confirm_{suffix}",
            type="secondary" if draft.confirmed else "primary",
            width="stretch",
            icon=":material/check:",
            help=(
                "Take this draft back out of the report."
                if draft.confirmed
                else "Accept this wording and put the draft into the report. It is still not sent."
            ),
        ):
            draft.confirmed = not draft.confirmed
            _pin_confirmed_actions(check)
            st.rerun(scope="app")
    with download:
        st.download_button(
            "Download",
            # A callable, so the file is built on the click that downloads it rather than on
            # every rerun of the page. Serializing every draft on every keystroke elsewhere
            # is work nobody asked for.
            data=lambda: checks_actions.to_file(check, draft)[0],
            file_name=checks_actions.file_name_for(check, draft),
            key=f"ck_action_download_{suffix}",
            width="stretch",
            icon=":material/download:",
            help=(
                "Open it in your own calendar to send the invite."
                if kind.extension == "ics"
                else "Open it in your own mail client as a pre-filled draft."
            ),
        )
    with delete:
        if st.button(
            "Delete",
            key=f"ck_action_delete_{suffix}",
            width="stretch",
            icon=":material/delete:",
            help="Discard this draft.",
        ):
            remove_action(check, draft.action_id)
            _pin_confirmed_actions(check)
            st.rerun(scope="app")

    if draft.confirmed:
        st.caption("In the report. Still a draft — this app sends nothing.")


def _render_actions_tab(user_id: int) -> None:
    """Addtion.md step 7 and the review half of its Tab 2 — the one part the Dashboard
    has no answer for."""
    saved = checks_session.get_set().saved_checks()
    if not saved:
        st.info(
            "Save a criteria's results in the Design tab first — follow-ups are drafted from "
            "the records that breached a rule.",
            icon=":material/info:",
        )
        return

    st.caption(
        "Drafts only. Confirming one puts it in the report; sending it is a download you "
        "open in your own mail or calendar client."
    )

    for check in saved:
        with st.expander(
            f"Criteria: {check.display_name()}",
            key=f"ck_act_exp_{check.check_id}",
            on_change="rerun",
            expanded=True,
            icon=":material/outgoing_mail:",
        ) as panel:
            if not panel.open:
                continue

            st.caption(f"{check.saved_run.fail_count} record(s) breached this rule.")

            if st.button(
                "Add follow-up",
                key=f"ck_add_action_{check.check_id}",
                icon=":material/add:",
                help="Draft an email, meeting, task or note about these failures.",
            ):
                checks_session.open_dialog("add_action", {"check_id": check.check_id, "user_id": user_id})
                st.rerun(scope="app")

            for notice in checks_session.consume_notices(check.check_id):
                st.warning(notice, icon=":material/warning:")

            if not check.actions:
                st.caption("No follow-ups drafted for this criteria yet.")
                continue

            for draft in check.actions:
                with st.container(border=True, key=f"ck_action_box_{check.check_id}_{draft.action_id}"):
                    _render_action(check, draft, user_id)


# Declared here rather than beside the first dialog, so `_dialog_add_action` — which belongs
# with the Actions tab it opens from — doesn't have to be hoisted above everything else.
DIALOGS = {
    "add_check": _dialog_add_check,
    "add_action": _dialog_add_action,
    "load_set": _dialog_load_set,
}


def _render_pending_dialog() -> None:
    pending = checks_session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action not in DIALOGS:
        checks_session.close_dialog()
        return
    DIALOGS[action](payload)


def render_checks(user_id: int) -> None:
    """The Checks view. Called by `chat_with_data.py` when its `de_view` is on "Checks"."""
    _render_pending_dialog()

    st.caption(
        "Write your rules here. Saved results go to the **Dashboard** page, where the report "
        "is arranged and downloaded as HTML or Excel."
    )

    design_tab, actions_tab = st.tabs(["Design", "Actions"])
    with design_tab:
        try:
            _render_design(user_id)
        except ChecksError as error:
            logger.exception("The Checks design tab failed.")
            st.error(str(error), icon=":material/error:")
    with actions_tab:
        _render_actions_tab(user_id)
