"""The Dashboard view of Report Builder (phase 32; a conversation since phase 35).

Where a user composes an interactive dashboard and downloads it as one offline HTML file.
The page is read top to bottom as the flow it describes: **say what you want, see it, say
what to change, see it again, download**.

- **Generate Dashboard is the starting point** (phase 39). One press designs a whole page
  from the data, the links confirmed in Setup and the column descriptions - and it is the
  one control here that uses the session's own model rather than the Light Model, because
  laying out a page from nothing is the single judgement call on this screen. Since phase
  45 it first shows a checklist - one line per visual, drafted by the Light Model - that the
  user edits, and then builds exactly that list. Undo brings the old page back.
- **Asking is the only way the dashboard is changed.** Each press is one round: the model
  is shown the page as it stands and returns the changes asked for, and a visual it does not
  mention is left alone. Rounds run on the Light Model unless the toggle above the button
  says otherwise (phase 40). Each round carries the dashboard from before it, so its
  own Undo button sits beside it in the history. Since phase 37 the instruction is typed in
  a dialog rather than in a box sitting on the page: it is one sentence written once and
  then finished with, and an always-open box above the dashboard pushed the dashboard
  itself down the screen.
- **Every table is embedded, each carrying its parents' columns** (phase 39), so one filter
  on `Employee Master - Department` narrows Salary and Attendance together. There is no
  Main table to pick any more.
- **Each visual has its own Edit button** (phase 37), which opens the same dialog scoped to
  that one visual. It is not a second way to change a dashboard - it is the same round with
  the "which one do you mean" part already answered, so "make this horizontal" needs no
  title spelled out and can reach no other chart.
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
from live_dashboard import help as dashboard_help
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


def _build_data() -> dashboard_session.BuiltData | None:
    """Flattens every table behind the dashboard, reusing the last build when nothing moved.

    **Every table, each with its own parents** since phase 39. One flattened table used to
    be the whole dashboard, which meant a sibling of the chosen one - Attendance beside
    Salary, both children of Employee Master - was embedded raw, with no Department column
    on it and so no way for a Department filter to reach it.

    The signature is which tables exist plus the engine's rebuild token, so adding a
    calculated column or confirming a new relationship rebuilds this by itself rather than
    leaving the dashboard drawing yesterday's columns.

    Returns None when there is nothing to build from, having already said so on screen.
    """
    table_names = engine_session.table_names()
    if not table_names:
        return None

    signature = (tuple(table_names), engine_session.rebuild_count())

    cached = dashboard_session.get_built_data()
    if cached is not None and cached.signature == signature:
        return cached

    try:
        tables, description = flatten.flatten_every_table(
            engine_session.connection(), engine_session.get_relationships(), table_names
        )
    except (DashboardDataError, DataEngineError) as error:
        logger.exception("Could not build the dashboard's data.")
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


def _render_generate(spec: model.DashboardSpec, data: dashboard_session.BuiltData,
                     user_id: int) -> None:
    """**Generate Dashboard** - the starting point, and the one control that uses the good model.

    Everything else on this page runs on the Light Model, which is right for "make it
    horizontal" and wrong for "design me a page": laying out a dashboard from nothing is the
    one judgement call here, and it is made once. So this button uses the session's chosen
    model (`llm_session.active_profile`) while Update the dashboard and Edit stay Light.

    Since phase 45 the press opens a dialog rather than building straight away: the Light
    Model drafts a checklist, one line per visual, the user edits it, and only then is the
    page built - exactly that list. The dialog is itself the second step, so on a page that
    already has visuals it says "this replaces everything" there instead of asking in two
    presses as phase 39 did. Undo still brings the old page back either way.
    """
    profile = llm_session.active_profile(user_id)
    if profile is None:
        st.warning(
            "No model is configured yet. Add one in Settings -> LLM providers to have a "
            "dashboard designed for you.",
            icon=":material/error:",
        )
        return

    st.button(
        "Generate Dashboard", key="ld_generate", width="stretch",
        icon=":material/auto_awesome:",
        help="Shows a short list of what the AI plans to build. Edit it, then build. "
             + ("This replaces what is on the page now." if spec.panels
                else "You can change any of it afterwards by asking."),
        on_click=dashboard_session.open_generate,
    )
    st.caption(
        f":red[Designed by **{profile['nickname']}** ({profile['default_model']}) from "
        "your tables, the links confirmed in Setup and your column descriptions.]"
    )

    if dashboard_session.current_dialog() == dashboard_session.GENERATE_DIALOG:
        # The checklist is the quick part, so it goes to the Light Model; with none set up
        # the session model drafts it too.
        draft_profile = llm_session.light_profile(user_id) or profile
        _dialog_generate(draft_profile, profile, spec, data)


@st.dialog("Plan your dashboard", width="large", on_dismiss=dashboard_session.close_dialog)
def _dialog_generate(draft_profile: dict, build_profile: dict, spec: model.DashboardSpec,
                     data: dashboard_session.BuiltData) -> None:
    """The checklist Generate shows before it builds (phase 45).

    One plain line per visual, in a text box the user can edit, delete from or add to. The
    build then makes exactly those lines - `ai_spec.build_from_checklist` drops anything
    extra and names any line it could not make - so what the user approved is what they get.
    """
    if dashboard_session.take_draft_request():
        with st.spinner(f"Asking {draft_profile['default_model']} what to build..."):
            lines, notes = ai_spec.draft_checklist(
                draft_profile, data.tables, notes=_column_notes(),
                wishes=dashboard_session.checklist_wishes(),
            )
        dashboard_session.store_draft(lines, notes)
    dashboard_session.seed_checklist_boxes()

    if spec.panels:
        st.warning("This replaces everything on the dashboard, including your own edits. "
                   "Undo brings it back.", icon=":material/warning:")
    for note in dashboard_session.checklist_notes():
        st.warning(note, icon=":material/error:")

    st.caption(
        f":red[Drafted by **{draft_profile['nickname']}**. One visual per line, starting "
        "with Card:, Filter:, Chart: or Table:. Edit, delete or add lines - the dashboard "
        "will have exactly these.]"
    )
    text = st.text_area(
        "What the dashboard will show",
        key=dashboard_session.LD_CHECKLIST_BOX_KEY,
        height=240,
        placeholder="Card: Total Amount\nChart: Sum of Amount by Customer (bar)",
        help="One line is one visual. Name the columns you want - the list below shows "
             "them. A line the AI can't build is named afterwards, with the reason.",
    )
    wishes = st.text_area(
        "Anything else?",
        key=dashboard_session.LD_WISHES_BOX_KEY,
        height=70,
        placeholder="show money in INR, focus on cost",
        help="Wishes that are not a visual. Used by Build, and by Draft again for a fresh "
             "list.",
    )
    dashboard_session.keep_checklist(text, wishes)

    with st.expander("Columns you can use", expanded=False, icon=":material/view_column:"):
        for table, columns in data.available_columns().items():
            st.markdown(f"**{table}:** " + ", ".join(columns))

    build_column, redraft_column = st.columns(2)
    with build_column:
        build = st.button(
            "Replace and build" if spec.panels else "Build this dashboard",
            key="ld_checklist_build", type="primary", width="stretch",
            icon=":material/auto_awesome:",
            help=f"{build_profile['nickname']} builds exactly the lines above - one visual "
                 "each, nothing extra.",
        )
    with redraft_column:
        redraft = st.button(
            "Draft again", key="ld_checklist_redraft", width="stretch",
            icon=":material/refresh:",
            help="Asks for a fresh list, using what you wrote in Anything else?. Replaces "
                 "the list above.",
        )
    skip_column, cancel_column = st.columns(2)
    with skip_column:
        skip = st.button(
            "Skip the list, just build", key="ld_checklist_skip", width="stretch",
            help="Lets the AI decide what to build, without the list.",
        )
    with cancel_column:
        if st.button("Cancel", key="ld_checklist_cancel", width="stretch",
                     help="Close without changing anything. Your list is kept."):
            dashboard_session.close_dialog()
            st.rerun(scope="app")

    if redraft:
        dashboard_session.request_draft()
        st.rerun(scope="app")
    elif build:
        lines = ai_spec.checklist_lines(text)
        if not lines:
            st.warning("Write at least one line - for example 'Card: Total Amount' - or "
                       "press Skip the list to let the AI decide.", icon=":material/error:")
        else:
            _run_generate(build_profile, spec, data, checklist=lines, wishes=wishes)
    elif skip:
        _run_generate(build_profile, spec, data)


def _render_conversation(spec: model.DashboardSpec, data: dashboard_session.BuiltData,
                         user_id: int) -> None:
    """The buttons the dashboard is built and changed from - the only way it is changed.

    Two of them, with different jobs and different models (phase 39). **Generate Dashboard**
    designs a page from nothing with the session's own model; **Update the dashboard** is
    one round on the Light Model, and appears only once there is something to update.

    The instruction itself is typed in a dialog (phase 37). Phase 35 put it on the page, on
    the grounds that a conversation you have to re-open a modal for is not a conversation;
    real use showed the opposite - an always-open text area pushed the dashboard it
    describes below the fold on every visit, and one sentence is written once, not lived in.

    Since phase 40 a round is not stuck on the Light Model: a toggle beside the caption
    switches it to the session's own model for the change that needs judgement rather than
    speed. Off by default, so nothing changes for anyone who leaves it alone.

    With no Light Model configured the round box says so and shows no button. Generate is
    unaffected, so a user with one provider configured can still have a dashboard designed.
    """
    flash = dashboard_session.consume_flash()
    if flash:
        st.success(flash, icon=":material/undo:")

    _render_generate(spec, data, user_id)

    light = llm_session.light_profile(user_id)
    round_profile = light
    if spec.panels and light is None:
        st.warning(
            "No Light Model is configured. Set one in Settings -> LLM providers to change "
            "this dashboard by describing the change.",
            icon=":material/error:",
        )
    elif spec.panels:
        round_profile = _round_model(light, user_id)
        # There is no separate box for standing preferences since phase 36. "Always show
        # currency in INR" is a sentence, and the dialog already takes sentences - a second
        # box only asked the user to decide which of the two a preference belonged in.
        st.caption(
            f":red[Changes are read by **{round_profile['nickname']}** "
            f"({round_profile['default_model']}). It can only choose from the visuals this "
            "page already has - it never writes code, and it only uses columns your data "
            "really has.]"
        )
        if st.button(
            "Update the dashboard",type="primary", key="ld_ai_open", width="stretch",
            icon=":material/edit:",
            help="Opens a box to say what should change. Anything you don't mention stays "
                 "exactly as it is.",
        ):
            dashboard_session.open_dialog(dashboard_session.ASK_DIALOG)
            st.rerun(scope="app")

    _render_rounds()
    if round_profile is not None:
        _render_open_dialog(round_profile, spec, data)


def _round_model(light: dict, user_id: int) -> dict:
    """Which model this dashboard's rounds run on - the Light Model unless asked otherwise.

    Rounds have been on the Light Model since phase 35, on the grounds that "make it
    horizontal" is a small edit. Some changes are not small ("redesign the bottom half so it
    reads as one story"), and the user owns a better model, so phase 40 makes it a choice -
    a toggle rather than a dropdown, because there are exactly two answers.

    Off by default, and re-read on every run rather than saved into the Task: which model
    answered is a choice for this sitting, not a property of the dashboard.

    Returns the profile the round should use; `light` unchanged when there is no second
    model to choose (`session_profiles` already excludes the Light Model, so the two can
    only be the same profile if the designations overlap).
    """
    active = llm_session.active_profile(user_id)
    if active is None or active.get("profile_id") == light.get("profile_id"):
        return light

    use_active = st.toggle(
        "Use my model instead of the Light Model",
        key=dashboard_session.LD_USE_ACTIVE_MODEL_KEY,
        value=False,
        help=f"Off, changes are read by the Light Model ({light['default_model']}), which "
             f"is quick and cheap. On, they are read by {active['nickname']} "
             f"({active['default_model']}) - slower, but better at a change that needs "
             "judgement. This does not change the dashboard by itself.",
    )
    return active if use_active else light


# --------------------------------------------------------------------------------------
# The dialogs (phase 37)
# --------------------------------------------------------------------------------------


def _render_open_dialog(profile: dict, spec: model.DashboardSpec,
                        data: dashboard_session.BuiltData) -> None:
    """Draws whichever dialog is open, if any.

    Driven from a session flag rather than a button's return value, which is the rule the
    rest of this app already follows: a `st.dialog` holding widgets reruns the script, and by
    the second run the press that opened it has been forgotten.

    `profile` is whichever model the toggle above resolved to, so both dialogs run the round
    on the same model the caption named.
    """
    which = dashboard_session.current_dialog()
    if not which or which == dashboard_session.GENERATE_DIALOG:
        return  # Generate's checklist is drawn by `_render_generate`, beside its button.

    if which == dashboard_session.ASK_DIALOG:
        _dialog_ask(profile, spec, data)
        return

    panel = next((one for one in spec.panels if one.panel_id == which), None)
    if panel is None:
        # The visual it was scoped to is gone - removed by the very round opened from it.
        dashboard_session.close_dialog()
        return
    _dialog_edit_visual(profile, spec, data, panel)


def _render_help(key: str) -> None:
    """"What can I ask?", closed until it is wanted.

    In both dialogs, because the question is the same one wherever it is asked: this page is
    driven by typing a sentence at a model, and a box with no edges is hard to aim at.
    Collapsed by default so it costs no height to the reader who already knows.
    """
    with st.expander("What can I ask?", expanded=False, icon=":material/help:"):
        st.caption(":red[Anything below can be asked for in your own words.]")
        st.markdown(dashboard_help.capabilities_markdown(ai_spec.MAX_PANELS))


def _dialog_footer(run_label: str, run_key: str, cancel_key: str, run_help: str) -> bool:
    """The run/Cancel pair both dialogs end with. True when the user pressed run."""
    run_column, cancel_column = st.columns(2)
    with run_column:
        pressed = st.button(
            run_label, key=run_key, type="primary", width="stretch",
            icon=":material/auto_awesome:", help=run_help,
        )
    with cancel_column:
        if st.button("Cancel", key=cancel_key, width="stretch",
                     help="Close without changing anything."):
            dashboard_session.close_dialog()
            st.rerun(scope="app")
    return pressed


@st.dialog("Describe your dashboard", width="large",
           on_dismiss=dashboard_session.close_dialog)
def _dialog_ask(profile: dict, spec: model.DashboardSpec,
                data: dashboard_session.BuiltData) -> None:
    """One round over the whole dashboard - the box that used to sit on the page.

    In a dialog since phase 37: the instruction is one sentence written once, and a text
    area that is always open pushed the dashboard it describes down the screen.

    Always a *change* to an existing dashboard since phase 39 - the button that opens it
    appears only once there are visuals, because starting from nothing is Generate
    Dashboard's job now, and it does that with a better model than this one.
    """
    st.text_area(
        "What should change?",
        key="ld_ai_instruction",
        height=110,
        placeholder="make the region chart horizontal, and add a monthly running total",
        help="Say what to change and the rest of the dashboard is left alone. Name the "
             "visual by the title shown above it on the dashboard.",
    )
    _render_help("ld_ai_help")
    if _dialog_footer(
        "Update the dashboard", "ld_ai_generate", "ld_ai_cancel",
        "Changes only what you asked for. Everything else stays as it is.",
    ):
        _run_round(profile, spec, data)


@st.dialog("Change this visual", width="large", on_dismiss=dashboard_session.close_dialog)
def _dialog_edit_visual(profile: dict, spec: model.DashboardSpec,
                        data: dashboard_session.BuiltData,
                        panel: model.PanelSpec) -> None:
    """One round confined to the visual whose Edit button was pressed.

    Not a second way to change a dashboard: it is the same round with "which one do you
    mean" already answered - so the instruction can be "make this horizontal" rather than a
    title the user has to copy out, and it can reach no other visual.
    """
    st.markdown(f"**{panel.display_title()}**")
    st.caption(f":red[{ai_spec.describe_panel(panel)}]")

    st.text_area(
        "What should change about this one?",
        key=f"ld_panel_instruction_{panel.panel_id}",
        height=110,
        placeholder="make it horizontal and show only the top 10",
        help="Only this visual is changed. Say 'remove it' to take it off the page.",
    )
    _render_help(f"ld_panel_help_{panel.panel_id}")
    if _dialog_footer(
        "Update this visual", f"ld_panel_generate_{panel.panel_id}",
        f"ld_panel_cancel_{panel.panel_id}",
        "Changes this visual only. Every other one stays as it is.",
    ):
        _run_round(profile, spec, data, panel=panel,
                   instruction_key=f"ld_panel_instruction_{panel.panel_id}")

    _remove_control(spec, panel)


def _remove_control(spec: model.DashboardSpec, panel: model.PanelSpec) -> None:
    """Remove this visual, in two presses so one stray click can't delete it.

    No AI call: the button says what it does. Undo brings the visual back.
    """
    st.divider()
    if not dashboard_session.is_confirming_remove(panel.panel_id):
        st.button(
            "Remove this visual", key=f"ld_panel_remove_{panel.panel_id}",
            icon=":material/delete:", width="stretch",
            help="Takes this visual off the dashboard. You can Undo it afterwards.",
            on_click=dashboard_session.ask_to_remove, args=(panel.panel_id,),
        )
        return

    st.caption(f":red[Remove '{panel.display_title()}' from the dashboard?]")
    yes_column, no_column = st.columns(2)
    with yes_column:
        confirmed = st.button(
            "Yes, remove it", key=f"ld_panel_remove_yes_{panel.panel_id}", type="primary",
            width="stretch", icon=":material/delete:",
            help="Removes this visual now. Use Undo on the dashboard to bring it back.",
        )
    with no_column:
        st.button(
            "No, keep it", key=f"ld_panel_remove_no_{panel.panel_id}", width="stretch",
            help="Keeps this visual and goes back to the edit box.",
            on_click=dashboard_session.cancel_remove,
        )
    if confirmed:
        dashboard_session.remove_visual(spec, panel)
        dashboard_session.close_dialog()
        st.rerun(scope="app")


def _render_visual_buttons(spec: model.DashboardSpec, user_id: int) -> None:
    """An Edit button per visual, above the preview.

    The preview is an iframe of the exported page, so a button cannot be drawn inside it -
    this row is the nearest thing, each button carrying the title it is printed under on the
    dashboard below so the two read against each other.

    Drawn only where there is a Light Model to answer them: the dialog these open is the
    round box, and a button that opens nothing is worse than a button that is not there.
    """
    if not spec.panels or llm_session.light_profile(user_id) is None:
        return

    st.caption(":red[Change one visual on its own:]")
    with st.container(horizontal=True, key="ld_visual_buttons"):
        for panel in spec.panels:
            st.button(
                panel.display_title(),
                key=f"ld_edit_{panel.panel_id}",
                icon=":material/edit:",
                help=f"Say what should change about {panel.display_title()}. Nothing else "
                     "on the dashboard is touched.",
                on_click=dashboard_session.open_dialog,
                args=(panel.panel_id,),
            )


def _run_round(profile: dict, spec: model.DashboardSpec,
               data: dashboard_session.BuiltData, *,
               panel: model.PanelSpec | None = None,
               instruction_key: str = "ld_ai_instruction") -> None:
    """Runs one round and records what it did.

    `panel` is what the Edit button beside a visual passes: the same round, confined to that
    one visual, so nothing else on the page can move. `profile` is the model the round runs
    on - the Light Model unless the toggle on the page says otherwise (phase 40).

    `revise_dashboard` never raises and edits `spec` in place only once it has a change to
    make, so there is no error path here beyond the sentences it returns - a failed call is
    reported as "we couldn't make that change", with the dashboard still on screen.

    A copy of the dashboard is taken *before* the round, since the round mutates it, and
    the round carries that copy as its own Undo. A round that changed nothing keeps no copy
    rather than offering a button that would restore the page to exactly what it already is.
    """
    instruction = str(st.session_state.get(instruction_key, "") or "")
    before = dashboard_session.snapshot(spec)

    with st.spinner(f"Asking {profile['default_model']}..."):
        result = ai_spec.revise_dashboard(
            profile, instruction, data.tables, spec, notes=_column_notes(), focus=panel,
        )

    changed = result.changed()
    details = [ai_spec.describe_panel(panel) for panel in (*result.added, *result.updated)]
    details += [f"Removed **{title}**" for title in result.removed]

    dashboard_session.record_round(dashboard_session.Round(
        instruction=instruction.strip(),
        summary=result.summary(),
        details=details,
        notes=list(result.notes),
        clarification=result.clarification or "",
        failed=result.failed,
        before_json=before if changed else "",
    ))

    if not changed:
        # The dialog stays open on a round that did nothing, so the sentences explaining why
        # are shown here, beside the box the user is about to retype in - behind the dialog
        # `_render_rounds` has already been drawn this run and cannot say them.
        if result.clarification:
            st.info(result.clarification, icon=":material/info:")
        for note in result.notes:
            st.warning(note, icon=":material/error:")
        return

    dashboard_session.close_dialog()
    st.rerun(scope="app")


def _run_generate(profile: dict, spec: model.DashboardSpec,
                  data: dashboard_session.BuiltData, *,
                  checklist: list[str] | None = None, wishes: str = "") -> None:
    """Designs a whole dashboard from the data, with the session's own model.

    The panels are emptied first, deliberately: `revise_dashboard` reads an empty dashboard
    as a first draft, which is exactly what this is - a page designed from the data rather
    than a round of edits to the one already there. The copy taken beforehand is what Undo
    puts back, so replacing a page you liked is still one press from being reversed.

    `checklist` is the list the user approved in the dialog (phase 45), built line for line;
    without one - Skip the list - the model decides, as Generate always did.
    """
    before = dashboard_session.snapshot(spec)
    spec.panels = []

    with st.spinner(f"Asking {profile['default_model']} to design a dashboard..."):
        if checklist:
            result = ai_spec.build_from_checklist(
                profile, checklist, data.tables, spec, notes=_column_notes(), wishes=wishes,
            )
        else:
            result = ai_spec.revise_dashboard(
                profile, "", data.tables, spec, notes=_column_notes(),
            )

    if checklist:
        # The list itself is what was asked, so the history shows it rather than a generic
        # "Generate" - and Undo beside it says which list it would take away.
        instruction = "Build this list:\n\n" + "\n".join(
            f"{number}. {line}" for number, line in enumerate(checklist, start=1)
        )
        if wishes.strip():
            instruction += f"\n\nAlso: {wishes.strip()}"
    else:
        instruction = "Generate a dashboard from this data"

    changed = result.changed()
    dashboard_session.record_round(dashboard_session.Round(
        instruction=instruction,
        summary=result.summary(),
        details=[ai_spec.describe_panel(one) for one in result.added],
        notes=list(result.notes),
        clarification=result.clarification or "",
        failed=result.failed,
        before_json=before if changed else "",
    ))

    if not changed:
        # The panels were emptied on the way in, so a round that produced nothing has to
        # put the old ones back - otherwise a failed Generate silently clears the page.
        try:
            spec.panels = model.from_json(before).panels if before else []
        except LiveDashboardError:
            logger.exception("The dashboard could not be restored after a failed Generate.")
        for note in result.notes:
            st.warning(note, icon=":material/error:")
        if result.clarification:
            st.info(result.clarification, icon=":material/info:")
        return

    dashboard_session.close_dialog()
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
    for panel in spec.panels:  # filters are panels too, so this already covers them
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
    """The conversation so far, newest first, each round with its own Undo.

    Kept because a round is easy to miss: "changed 1" beside a page of eight visuals is the
    only sign that the one you named is the one that moved. Notes are `st.warning` rather
    than captions - a shape swapped for the nearest one we can draw is exactly the thing a
    user discovers three days later in someone else's inbox.

    The single Undo button at the top of the page is gone (phase 39). There was one stash,
    so the second round of an afternoon threw away the only copy that could take you back -
    and a lone greyed-out button never said which round it would have undone. Now the
    button sits beside the round it reverses, the way the Data Cleaner's steps do, and only
    the newest is live: restoring an older copy would silently discard every round after it,
    which is not what Undo means anywhere else in this app.
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
            _render_undo_button(position, entry)
            if position < len(history) - 1:
                st.divider()


def _render_undo_button(position: int, entry: dashboard_session.Round) -> None:
    """One round's Undo. Drawn disabled on every round but the newest, saying why."""
    if not entry.can_be_undone():
        return

    newest = position == 0
    if st.button(
        "Undo this", key=f"ld_undo_round_{position}", icon=":material/undo:",
        disabled=not newest,
        help=("Puts the dashboard back as it was before this change."
              if newest else
              "Undo the newer changes first - this one is further back."),
    ):
        if dashboard_session.undo_round(position):
            st.rerun(scope="app")
        # Only reachable if the saved copy could not be read back, since the button is
        # drawn only for a round that has one - but a dead button with no explanation is
        # worse than the failure it hides.
        st.caption(":red[That change couldn't be undone. The copy saved before it could "
                   "not be read back.]")


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
    rows = data.total_row_count()
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
        data = _build_data()
    except LiveDashboardError as error:
        logger.exception("The dashboard view could not start.")
        st.error(str(error))
        return

    if data is None:
        return

    # No Main table picker since phase 39: every table is embedded, each carrying the
    # columns of the parents it can reach, so there is no longer one table the dashboard is
    # "built from" - and a filter on a parent's column narrows all of them.
    st.caption(f":red[Using {data.plan_description}]")

    _render_settings(spec)
    st.divider()

    _render_conversation(spec, data, user_id)

    if not spec.panels:
        st.info("No visuals yet. Press **Generate Dashboard** above to have one designed "
                "from your data - you can change any of it afterwards by asking.")
        return

    st.divider()
    _render_problems(spec, data)
    _render_visual_buttons(spec, user_id)
    _render_export(spec, data)
