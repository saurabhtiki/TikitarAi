"""Creating meetings and following what invitees said (requirement 6.7, spec 1 & 8).

Two views in one page — a list and a detail — switched by `meetings.session.open_meeting_id`
rather than by separate `st.Page` entries, because "which meeting" is a selection inside one
workflow, not a different place in the app.

A **table agenda item's data is attached after the meeting exists**, from the Overview tab,
not in the creation dialog. Uploading a sheet and then marking each of its columns locked or
editable is a two-step operation over a file the dialog would have to hold across reruns
while every other field in it stays live — and the item itself is what the grid hangs off, so
it has to exist first either way.
"""

import logging
import os
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st

from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from meetings import (
    access,
    db,
    drafting_agent,
    extraction_agent,
    faq,
    flow,
    listing,
    llm_choice,
    loops,
    matrix,
    session,
    steps,
    storage,
    summary_agent,
    tables,
    templates,
    transcript,
)
from meetings.exceptions import MeetingAgentError, MeetingError
from meetings.model import (
    ANSWER_TEXT,
    DEFAULT_MAX_TRIES,
    DISCUSSION_ITEM,
    MAX_TRIES_LIMIT,
    QUESTION_ITEM,
    STEP_ANSWERED,
    TABLE_ITEM,
    AgendaItem,
    AgendaTable,
    EvaluationField,
    Faq,
    FaqEntry,
    Meeting,
    clamp_tries,
    evaluation_buckets_from_text,
    evaluation_buckets_to_text,
)
from sidebar import render_sidebar
from utils.dates import show_dataframe

logger = logging.getLogger(__name__)

# Where an invitee link points when nothing better is known: no setting, and no browser
# address (a test run with no browser behind it).
FALLBACK_BASE_URL = "http://localhost:8501"
BASE_URL_SETTING = "TIKITARAI_BASE_URL"

AGENDA_ROWS_KEY = "meetings_agenda_rows"
INVITEE_ROWS_KEY = "meetings_invitee_rows"
EVALUATION_ROWS_KEY = "meetings_evaluation_rows"
NEW_NOTICE_KEY = "meetings_new_notice"
NEW_MODEL_KEY = "meetings_new_model"
BLANK_TEMPLATE = "Blank"

# What the creator picks in the agenda grid, and what it means in the stored agenda.
TYPE_DISCUSSION = "Discussion"
TYPE_TABLE = "Table"
TYPE_QUESTION = "Question"
ITEM_TYPE_BY_LABEL = {TYPE_DISCUSSION: DISCUSSION_ITEM, TYPE_TABLE: TABLE_ITEM, TYPE_QUESTION: QUESTION_ITEM}
LABEL_BY_ITEM_TYPE = {value: key for key, value in ITEM_TYPE_BY_LABEL.items()}
SKIPPED_CELL = steps.SKIPPED_CELL
ROW_NUMBER_LABEL = "(row number)"


def _current_user_id() -> int | None:
    return st.session_state.get("user_id")


def _render_profile_sidebar() -> None:
    user_id = _current_user_id()
    if user_id is None:
        return
    try:
        profile = get_user_by_id(user_id)
    except AuthDatabaseError:
        logger.exception("Could not load the profile for user %s.", user_id)
        return
    if profile is not None:
        render_sidebar(profile)


# --------------------------------------------------------------------------------------
# Creating a meeting
# --------------------------------------------------------------------------------------


def _blank_agenda_row() -> dict:
    return {
        "Agenda item": "",
        "Type": TYPE_DISCUSSION,
        "Answer type": steps.ANSWER_TYPE_LABELS[ANSWER_TEXT],
        "Rule": "",
        "Tries": DEFAULT_MAX_TRIES,
        "Go to": "",
        "For each": "",
        "AI note": "",
    }


def _agenda_rows() -> pd.DataFrame:
    """The agenda editor's backing frame, seeded with one blank row."""
    if AGENDA_ROWS_KEY not in st.session_state:
        st.session_state[AGENDA_ROWS_KEY] = pd.DataFrame([_blank_agenda_row()])
    return st.session_state[AGENDA_ROWS_KEY]


def _invitee_rows() -> pd.DataFrame:
    if INVITEE_ROWS_KEY not in st.session_state:
        st.session_state[INVITEE_ROWS_KEY] = pd.DataFrame([{"Name": "", "Email": ""}])
    return st.session_state[INVITEE_ROWS_KEY]


def _evaluation_rows() -> pd.DataFrame:
    if EVALUATION_ROWS_KEY not in st.session_state:
        st.session_state[EVALUATION_ROWS_KEY] = pd.DataFrame([{"Question": "", "Buckets": ""}])
    return st.session_state[EVALUATION_ROWS_KEY]


def _clear_creation_rows() -> None:
    st.session_state.pop(AGENDA_ROWS_KEY, None)
    st.session_state.pop(INVITEE_ROWS_KEY, None)
    st.session_state.pop(EVALUATION_ROWS_KEY, None)
    st.session_state.pop(NEW_NOTICE_KEY, None)


def _replace_new_agenda(agenda: list[AgendaItem]) -> None:
    """Refills the New meeting grid. The editor's own edits are dropped too — they are kept
    by row number, so left in place they would land on the new rows."""
    st.session_state[AGENDA_ROWS_KEY] = _agenda_to_frame(agenda)
    st.session_state.pop("meetings_new_agenda", None)


def _apply_template() -> None:
    """Button callback: copies the chosen template into the New meeting window (phase 54).

    A callback, because it runs before the boxes are drawn — the only point at which a
    widget's value may be set from code.
    """
    template = templates.TEMPLATE_BY_NAME.get(st.session_state.get("meetings_new_template", ""))
    if template is None:
        st.session_state[NEW_NOTICE_KEY] = ("warning", "Pick a template first.")
        return
    st.session_state["meetings_new_context"] = template.meeting_context
    st.session_state["meetings_new_persona"] = template.persona
    st.session_state["meetings_new_sop"] = template.context_sop
    _replace_new_agenda(template.agenda)
    st.session_state[EVALUATION_ROWS_KEY] = _evaluation_to_frame(template.evaluation)
    st.session_state.pop("meetings_new_evaluation", None)
    st.session_state[NEW_NOTICE_KEY] = ("success", f"Filled in from '{template.name}'. Edit anything before creating.")


def _draft_from_description(user_id: int) -> None:
    """Button callback: the AI writes agenda rows from the plain-English box (phase 54)."""
    profile = llm_choice.resolve(
        llm_choice.owned_profiles(user_id), st.session_state.get(NEW_MODEL_KEY)
    ).profile
    if profile is None:
        st.session_state[NEW_NOTICE_KEY] = ("error", "Pick an AI model (or set a default in Settings) before drafting with AI.")
        return
    try:
        agenda = drafting_agent.draft_agenda(profile, st.session_state.get("meetings_new_describe", ""))
    except MeetingError as error:
        logger.exception("Could not draft an agenda for user %s.", user_id)
        st.session_state[NEW_NOTICE_KEY] = ("error", str(error))
        return

    _replace_new_agenda(agenda)
    problems = steps.agenda_problems(agenda)
    if problems:
        st.session_state[NEW_NOTICE_KEY] = (
            "warning",
            f"Drafted {len(agenda)} row(s). Please fix these before creating:\n\n"
            + "\n".join(f"- {problem}" for problem in problems),
        )
    else:
        st.session_state[NEW_NOTICE_KEY] = ("success", f"Drafted {len(agenda)} row(s). Check them in the grid below.")


def _model_picker(profiles: list[dict], current_id: int | None, key: str) -> int | None:
    """The AI model dropdown (phase 56): every Settings profile, light ones included."""
    options = [profile["profile_id"] for profile in profiles]
    labels = {profile["profile_id"]: llm_choice.profile_label(profile) for profile in profiles}
    if current_id not in labels:
        fallback = llm_choice.default_of(profiles)
        current_id = fallback["profile_id"] if fallback else options[0]
    return st.selectbox(
        "AI model",
        options=options,
        index=options.index(current_id),
        format_func=lambda profile_id: labels.get(profile_id, str(profile_id)),
        key=key,
        help="The model this meeting's chat, summaries and answer checks run on. You can change it later "
        "in Overview; invitees already chatting get the new model from their next message.",
    )


def _render_new_notice() -> None:
    notice = st.session_state.pop(NEW_NOTICE_KEY, None)
    if notice is None:
        return
    level, text = notice
    show = {"success": st.success, "warning": st.warning}.get(level, st.error)
    show(text, icon=":material/info:" if level == "success" else ":material/error:")


def _render_flow(agenda: list[AgendaItem], expanded: bool = False) -> None:
    """The flow picture of the questions (phase 54); nothing when there are no questions."""
    dot = flow.flow_dot(agenda)
    if not dot:
        return
    # Phase 57: the Rule is checked before Go to, so point out a Go to that reaches past it.
    for warning in steps.agenda_warnings(agenda):
        st.caption(f":red[Note — {warning}]")
    with st.expander("Flow picture", expanded=expanded):
        st.caption(":red[Arrows show the order. Blue arrows are Go to jumps; a red '?' is a Go to that needs fixing.]")
        st.graphviz_chart(dot, width="stretch")


def _agenda_editor(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """The agenda grid, shared by the creation dialog and the detail page's setup editor.

    `Type` is a fixed-option column rather than free text: it decides which table the item's
    substance is stored in, so a typo would have to become a silent fallback to Discussion —
    an agenda item quietly demoted from a grid to a chat question.
    """
    return st.data_editor(
        frame,
        num_rows="dynamic",
        width="stretch",
        key=key,
        column_config={
            "Type": st.column_config.SelectboxColumn(
                "Type",
                options=list(ITEM_TYPE_BY_LABEL),
                default=TYPE_DISCUSSION,
                required=True,
                help="Discussion items are talked through in the chat. Table items get their own "
                "grid tab. Question items are asked one by one until the answer passes the rule.",
            ),
            "Answer type": st.column_config.SelectboxColumn(
                "Answer type",
                options=list(steps.ANSWER_TYPE_BY_LABEL),
                default=steps.ANSWER_TYPE_LABELS[ANSWER_TEXT],
                help="Question items only: the kind of answer expected, e.g. Number for a salary.",
            ),
            "Rule": st.column_config.TextColumn(
                "Rule",
                help="Question items only. Number: > 0, between 20000 and 60000. Date: future, "
                "past, after 01-10-2026, within 30 days. Text: at least 20 characters. "
                "Choice: Morning, Evening, Night. Leave blank for any answer.",
            ),
            "Tries": st.column_config.NumberColumn(
                "Tries",
                min_value=1,
                max_value=MAX_TRIES_LIMIT,
                step=1,
                default=DEFAULT_MAX_TRIES,
                help="Question items only: wrong answers allowed before it is marked Not answered.",
            ),
            "Go to": st.column_config.TextColumn(
                "Go to",
                help="Question items only, optional: where to jump after this answer, e.g. "
                "'if > 60000 go to End' or 'if No go to Notice period'. Separate several with ';'. "
                "Only forward to a later question, or End. Blank means the next question. "
                "Inside a For each list, 'Next row' skips the rest of that row.",
            ),
            "For each": st.column_config.TextColumn(
                "For each",
                help="Question items only, optional: the name of a list, e.g. 'Outstanding invoices'. "
                "The question is asked once for every row of that list. Give the questions of one "
                "list the same name and keep them together. Upload the list in Overview.",
            ),
        },
    )


def _evaluation_editor(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    """The evaluation-questions grid (spec 3b). Leaving it empty turns the feature off."""
    return st.data_editor(
        frame,
        num_rows="dynamic",
        width="stretch",
        key=key,
        column_config={
            "Buckets": st.column_config.TextColumn(
                "Buckets (optional)",
                help="Comma-separated options to classify the answer into, e.g. Low, Medium, High.",
            ),
        },
    )


@st.dialog("New meeting", width="large")
def _open_new_meeting_dialog(user_id: int) -> None:
    if "meetings_new_persona" not in st.session_state:
        # Seeded through session state rather than `value=`, so Use template can replace it.
        try:
            st.session_state["meetings_new_persona"] = db.get_default_persona(user_id)
        except MeetingError:
            logger.exception("Could not read the default persona for user %s.", user_id)
            st.session_state["meetings_new_persona"] = ""

    left, right = st.columns([3, 1], vertical_alignment="bottom")
    with left:
        chosen = st.selectbox(
            "Start from",
            options=[BLANK_TEMPLATE, *templates.TEMPLATE_BY_NAME],
            key="meetings_new_template",
            help="A ready-made meeting to edit instead of starting from a blank grid.",
        )
    with right:
        st.button(
            "Use template",
            key="meetings_new_template_apply",
            icon=":material/content_copy:",
            on_click=_apply_template,
            disabled=chosen == BLANK_TEMPLATE,
            help="Fills in the context, persona, SOP, agenda and evaluation questions. Replaces what is there.",
        )
    if chosen in templates.TEMPLATE_BY_NAME:
        st.caption(f":red[{templates.TEMPLATE_BY_NAME[chosen].description}]")

    profiles = llm_choice.owned_profiles(user_id)
    if profiles:
        _model_picker(profiles, st.session_state.get(NEW_MODEL_KEY), NEW_MODEL_KEY)
    else:
        st.caption(":red[No AI model yet — add one in Settings before invitees can chat.]")

    with st.expander("Describe it in plain English"):
        st.text_area(
            "What should the bot ask?",
            key="meetings_new_describe",
            placeholder="Ask expected salary, must be a number; if above 1 lakh end the interview. "
            "Then ask notice period in days.",
            help="The AI turns this into agenda rows in the grid below. You can edit them before creating.",
        )
        st.button(
            "Draft with AI",
            key="meetings_new_draft",
            icon=":material/auto_awesome:",
            on_click=_draft_from_description,
            args=(user_id,),
            help="Replaces the agenda grid with rows drafted from your description. Uses the AI model picked above.",
        )
    _render_new_notice()

    subject = st.text_input(
        "Subject",
        key="meetings_new_subject",
        help="What this conversation is about, e.g. 'PO No 123'.",
    )
    meeting_context = st.text_area(
        "Meeting context",
        key="meetings_new_context",
        help="Two to four lines on why this conversation is happening. The AI is told this.",
    )
    persona = st.text_area(
        "Persona",
        key="meetings_new_persona",
        help="Who the AI should be, e.g. 'You are the Purchase Manager...'. Saved with this meeting.",
    )
    context_sop = st.text_area(
        "Context / SOP",
        key="meetings_new_sop",
        help="Rules and background the AI must apply throughout the conversation.",
    )

    st.caption(
        ":red[Agenda — one item per row. The AI note is the ground truth for that item. A Table "
        "item's data is attached after the meeting is created. Question items are asked "
        "first, in order, e.g. 'Expected salary' · Number · between 20000 and 60000. "
        "Go to can skip ahead, e.g. 'if > 60000 go to End'. For each repeats a question for "
        "every row of a list you upload after creating, e.g. 'Outstanding invoices'.]"
    )
    agenda_frame = _agenda_editor(_agenda_rows(), "meetings_new_agenda")
    _render_flow(_agenda_from_frame(agenda_frame))

    st.caption(
        "Evaluation questions (optional) — short questions the AI works into the conversation, "
        "so the answers can be compared across invitees. Leave blank to skip."
    )
    evaluation_frame = _evaluation_editor(_evaluation_rows(), "meetings_new_evaluation")

    st.caption("Invitees — each gets their own private link and access code.")
    invitee_frame = st.data_editor(
        _invitee_rows(),
        num_rows="dynamic",
        width="stretch",
        key="meetings_new_invitees"
    )

    save_default = st.checkbox(
        "Save this persona as my default",
        key="meetings_new_save_default",
        help="Pre-fills the persona on every meeting you create from now on.",
    )

    if st.button("Create meeting", key="meetings_new_save", icon=":material/save:", type="primary"):
        _handle_create_meeting(
            user_id,
            subject,
            meeting_context,
            persona,
            context_sop,
            agenda_frame,
            evaluation_frame,
            invitee_frame,
            save_default,
            st.session_state.get(NEW_MODEL_KEY),
        )


def _agenda_from_frame(frame: pd.DataFrame) -> list[AgendaItem]:
    items = []
    for _, row in frame.iterrows():
        title = str(row.get("Agenda item") or "").strip()
        if not title:
            continue
        label = str(row.get("Type") or TYPE_DISCUSSION).strip()
        answer_label = str(row.get("Answer type") or "").strip()
        rule = row.get("Rule")
        branch = row.get("Go to")
        loop = row.get("For each")
        item_type = ITEM_TYPE_BY_LABEL.get(label, DISCUSSION_ITEM)
        items.append(
            AgendaItem(
                item=title,
                ai_note=str(row.get("AI note") or "").strip(),
                item_type=item_type,
                answer_type=steps.ANSWER_TYPE_BY_LABEL.get(answer_label, ANSWER_TEXT),
                rule="" if pd.isna(rule) else str(rule).strip(),
                max_tries=clamp_tries(row.get("Tries")),
                branch="" if item_type != QUESTION_ITEM or pd.isna(branch) else str(branch).strip(),
                loop="" if pd.isna(loop) else str(loop).strip(),
            )
        )
    return steps.tidy_loop_names(items)


def _agenda_to_frame(agenda: list[AgendaItem]) -> pd.DataFrame:
    """A stored agenda back into the editor's shape, with a blank row to grow into."""
    rows = [
        {
            "Agenda item": item.item,
            "Type": LABEL_BY_ITEM_TYPE.get(item.item_type, TYPE_DISCUSSION),
            "Answer type": steps.ANSWER_TYPE_LABELS.get(item.answer_type, steps.ANSWER_TYPE_LABELS[ANSWER_TEXT]),
            "Rule": item.rule,
            "Tries": item.max_tries,
            "Go to": item.branch,
            "For each": item.loop,
            "AI note": item.ai_note,
        }
        for item in agenda
    ]
    rows.append(_blank_agenda_row())
    return pd.DataFrame(rows)


def _evaluation_from_frame(frame: pd.DataFrame, existing: list[EvaluationField]) -> list[EvaluationField]:
    """The evaluation grid back into fields, keeping ids where the question is unchanged.

    Matching on the question text is what stops an edit elsewhere in the grid from looking
    like a delete-and-recreate to `db.replace_evaluation_fields` — which would take every
    answer already extracted for that question with it.
    """
    by_question = {field_spec.question.strip().lower(): field_spec for field_spec in existing}

    fields = []
    for _, row in frame.iterrows():
        question = str(row.get("Question") or "").strip()
        if not question:
            continue
        previous = by_question.get(question.lower())
        fields.append(
            EvaluationField(
                field_id=previous.field_id if previous is not None else None,
                question=question,
                buckets=evaluation_buckets_from_text(row.get("Buckets")),
            )
        )
    return fields


def _evaluation_to_frame(fields: list[EvaluationField]) -> pd.DataFrame:
    rows = [
        {"Question": field_spec.question, "Buckets": evaluation_buckets_to_text(field_spec.buckets)}
        for field_spec in fields
    ]
    rows.append({"Question": "", "Buckets": ""})
    return pd.DataFrame(rows)


def _invitees_from_frame(frame: pd.DataFrame) -> list[tuple[str, str]]:
    people = []
    for _, row in frame.iterrows():
        email = str(row.get("Email") or "").strip()
        if not email:
            continue
        people.append((str(row.get("Name") or "").strip() or email, email))
    return people


def _handle_create_meeting(
    user_id: int,
    subject: str,
    meeting_context: str,
    persona: str,
    context_sop: str,
    agenda_frame: pd.DataFrame,
    evaluation_frame: pd.DataFrame,
    invitee_frame: pd.DataFrame,
    save_default: bool,
    profile_id: int | None = None,
) -> None:
    agenda = _agenda_from_frame(agenda_frame)
    fields = _evaluation_from_frame(evaluation_frame, [])
    people = _invitees_from_frame(invitee_frame)

    if not subject.strip():
        st.warning("Give this meeting a subject.", icon=":material/error:")
        return
    if not people:
        st.warning("Add at least one invitee — a meeting with nobody to talk to can't start.", icon=":material/error:")
        return
    problems = steps.agenda_problems(agenda)
    if problems:
        st.warning("Please fix the agenda first:\n\n" + "\n".join(f"- {problem}" for problem in problems), icon=":material/error:")
        return

    meeting = Meeting(
        subject=subject,
        meeting_context=meeting_context,
        persona=persona,
        context_sop=context_sop,
        agenda=agenda,
        profile_id=profile_id,
    )

    try:
        saved = db.create_meeting(user_id, meeting)
        if fields:
            db.replace_evaluation_fields(saved.meeting_id, user_id, fields)
        for name, email in people:
            code = access.generate_access_code()
            db.add_invitee(
                saved.meeting_id, user_id, name, email, access.generate_token(), access.encrypt_code(code)
            )
            db.remember_contact(email, name, user_id)
        if save_default:
            db.set_default_persona(user_id, persona)
    except MeetingError as error:
        logger.exception("Could not create a meeting for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return

    _clear_creation_rows()
    st.session_state.pop(NEW_MODEL_KEY, None)
    session.open_meeting(saved.meeting_id)
    if saved.table_items() or steps.loop_names(saved):
        # Said now rather than left to be discovered: an invitee who opens their link before
        # the sheet is attached finds a table tab that can't be filled in.
        session.flash(
            f"Created '{saved.display_subject()}'. Attach the data for each table item and "
            "For each list in Overview before sharing the links."
        )
    else:
        session.flash(f"Created '{saved.display_subject()}'. Share the links below with your invitees.")
    st.rerun()


# --------------------------------------------------------------------------------------
# The meetings list
# --------------------------------------------------------------------------------------


def _render_list(user_id: int) -> None:
    st.subheader("My meetings")

    if st.button("New meeting", key="meetings_new_button", icon=":material/add:", type="primary",
                 help="Set up a subject, persona, agenda and invitees."):
        _open_new_meeting_dialog(user_id)

    try:
        rows = db.list_meetings(user_id)
    except MeetingError as error:
        logger.exception("Could not list meetings for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return

    if not rows:
        st.info("You haven't created any meetings yet.", icon=":material/info:")
        return

    search_column, status_column = st.columns([3, 1])
    with search_column:
        query = st.text_input(
            "Find a meeting",
            key="meetings_search",
            placeholder="Type part of a meeting's subject",
            help="Filters the list below by subject. Leave it empty to see every meeting.",
        )
    with status_column:
        status = st.selectbox(
            "Status",
            options=listing.STATUS_OPTIONS,
            key="meetings_status_filter",
            help="Waiting on invitees: someone hasn't finished. All finished: every invitee closed their chat.",
        )

    matches = listing.filter_meetings(rows, query, status)
    if not matches:
        st.caption(":red[No meeting matches this search.]")
        return
    if len(matches) < len(rows):
        st.caption(f":red[Showing {len(matches)} of {len(rows)} meeting(s).]")

    for row in matches:
        with st.container(border=True):
            left, right = st.columns([4, 1], vertical_alignment="center")
            with left:
                st.markdown(f"**{row['subject']}**")
                st.caption(
                    f"Created {listing.created_on(row['created_at'])} · "
                    f"{row['closed_count']} of {row['invitee_count']} invitee(s) finished"
                )
            with right:
                if st.button(
                    "Open",
                    key=f"meetings_open_{row['meeting_id']}",
                    icon=":material/arrow_forward:",
                    help="View this meeting's invitees, chats and summaries.",
                ):
                    session.open_meeting(row["meeting_id"])
                    st.rerun()


# --------------------------------------------------------------------------------------
# Meeting detail
# --------------------------------------------------------------------------------------


def _render_model_setup(meeting: Meeting, user_id: int) -> None:
    """The meeting's AI model (phase 56): shown, changeable at any time, and a warning if deleted."""
    profiles = llm_choice.owned_profiles(user_id)
    if not profiles:
        st.caption(":red[No AI model yet — add one in Settings before invitees can chat.]")
        return
    choice = llm_choice.resolve(profiles, meeting.profile_id)
    if choice.fell_back:
        using = llm_choice.profile_label(choice.profile) if choice.profile else "none — set a default in Settings"
        st.caption(f":red[The chosen AI model was deleted in Settings — using the default ({using}).]")

    left, right = st.columns([3, 1], vertical_alignment="bottom")
    with left:
        current = choice.profile["profile_id"] if choice.profile else None
        picked = _model_picker(profiles, current, f"meetings_model_{meeting.meeting_id}")
    with right:
        if st.button(
            "Save model",
            key=f"meetings_model_save_{meeting.meeting_id}",
            icon=":material/save:",
            disabled=picked == meeting.profile_id,
            help="Invitees get the new model from their next message.",
        ):
            _handle_save_model(meeting, user_id, picked)


def _handle_save_model(meeting: Meeting, user_id: int, profile_id: int) -> None:
    try:
        db.set_meeting_profile(meeting.meeting_id, user_id, profile_id)
    except MeetingError as error:
        logger.exception("Could not save the model for meeting %s.", meeting.meeting_id)
        st.error(str(error), icon=":material/error:")
        return
    session.flash("AI model saved.")
    st.rerun()


def _render_overview(meeting: Meeting, user_id: int, fields: list[EvaluationField]) -> None:
    _render_model_setup(meeting, user_id)

    st.markdown("**Meeting context**")
    st.write(meeting.meeting_context or "_None given._")

    st.markdown("**Persona**")
    st.write(meeting.persona or "_None given._")

    st.markdown("**Context / SOP**")
    st.write(meeting.context_sop or "_None given._")

    st.markdown("**Agenda**")
    if meeting.agenda:
        for item in meeting.agenda:
            kind = " · table" if item.is_table() else ""
            if item.is_question():
                kind = f" · question · {steps.describe_rule(item)}, {item.max_tries} tries"
                if item.branch:
                    kind += f" · go to: {item.branch}"
                if item.loop:
                    kind += f" · for each row of: {item.loop}"
            st.markdown(f"- **{item.item}**{kind}" + (f" — {item.ai_note}" if item.ai_note else ""))
    else:
        st.write("_No agenda items._")
    _render_flow(meeting.agenda)

    if fields:
        st.markdown("**Evaluation questions**")
        for field_spec in fields:
            buckets = evaluation_buckets_to_text(field_spec.buckets)
            st.markdown(f"- {field_spec.question}" + (f" — _{buckets}_" if buckets else ""))

    _render_setup_editor(meeting, user_id, fields)

    for item in meeting.table_items():
        _render_table_setup(meeting, user_id, item)

    for name in steps.loop_names(meeting):
        _render_list_setup(meeting, user_id, name)

    _render_faq_setup(meeting, user_id)

    st.markdown("**Reference documents**")
    uploaded = st.file_uploader(
        "Add reference documents",
        accept_multiple_files=True,
        key=f"meetings_refdocs_{meeting.meeting_id}",
        help="Shared with every invitee, read-only. Stored for reference, not searched by the AI.",
    )
    if uploaded and st.button(
        "Save documents",
        key=f"meetings_refdocs_save_{meeting.meeting_id}",
        icon=":material/upload:",
        help="Store these against the meeting.",
    ):
        _handle_ref_upload(meeting.meeting_id, uploaded)

    try:
        files = db.list_files(meeting.meeting_id)
    except MeetingError:
        logger.exception("Could not list reference documents for meeting %s.", meeting.meeting_id)
        files = []

    for record in files:
        try:
            payload = storage.read_file(record["filepath"])
        except MeetingError:
            st.caption(f"{record['filename']} — no longer on disk")
            continue
        st.download_button(
            record["filename"],
            data=payload,
            file_name=record["filename"],
            key=f"meetings_refdoc_dl_{record['file_id']}",
            help="Download this reference document.",
        )


def _render_setup_editor(meeting: Meeting, user_id: int, fields: list[EvaluationField]) -> None:
    """Editing the agenda and the evaluation questions after the meeting exists.

    Collapsed by default and separate from the read-only summary above it, because reading
    what a meeting is set up to do is the common visit and rewriting it is the rare one.
    """
    with st.expander("Edit agenda & questions"):
        st.caption("Agenda — changing an item's title detaches any table data attached to it.")
        agenda_frame = _agenda_editor(
            _agenda_to_frame(meeting.agenda), f"meetings_edit_agenda_{meeting.meeting_id}"
        )

        st.caption("Evaluation questions — removing one also removes the answers extracted for it.")
        evaluation_frame = _evaluation_editor(
            _evaluation_to_frame(fields), f"meetings_edit_evaluation_{meeting.meeting_id}"
        )

        if st.button(
            "Save setup",
            key=f"meetings_save_setup_{meeting.meeting_id}",
            icon=":material/save:",
            help="Applies to every invitee's next message, including chats already in progress.",
        ):
            _handle_save_setup(meeting, user_id, agenda_frame, evaluation_frame, fields)


def _handle_save_setup(
    meeting: Meeting,
    user_id: int,
    agenda_frame: pd.DataFrame,
    evaluation_frame: pd.DataFrame,
    fields: list[EvaluationField],
) -> None:
    agenda = _agenda_from_frame(agenda_frame)
    problems = steps.agenda_problems(agenda)
    if problems:
        st.warning("Please fix the agenda first:\n\n" + "\n".join(f"- {problem}" for problem in problems), icon=":material/error:")
        return
    meeting.agenda = agenda
    try:
        db.update_meeting(user_id, meeting)
        db.replace_evaluation_fields(meeting.meeting_id, user_id, _evaluation_from_frame(evaluation_frame, fields))
    except MeetingError as error:
        logger.exception("Could not save the setup for meeting %s.", meeting.meeting_id)
        st.error(str(error), icon=":material/error:")
        return

    session.flash("Setup saved.")
    st.rerun()


def _render_table_setup(meeting: Meeting, user_id: int, item: AgendaItem) -> None:
    """Attaching a sheet to one table agenda item, and marking its columns (spec 3a).

    Opened by default while there is no sheet attached: until there is, the item is a tab
    every invitee can see and none of them can fill in.
    """
    try:
        attached = db.find_agenda_table(meeting.meeting_id, item.item)
    except MeetingError as error:
        logger.exception("Could not read the table for '%s'.", item.item)
        st.error(str(error), icon=":material/error:")
        return

    with st.expander(f"Table data — {item.item}", expanded=attached is None):
        if attached is not None:
            st.caption(
                f"{attached.source_file or 'Uploaded sheet'} · {attached.row_count()} row(s) · "
                f"locked: {', '.join(attached.locked_columns) or 'none'} · "
                f"editable: {', '.join(attached.editable_columns) or 'none'}"
            )
            if st.button(
                "Remove this table",
                key=f"meetings_table_remove_{meeting.meeting_id}_{item.item}",
                icon=":material/delete:",
                help="Deletes the sheet and every answer invitees have filled into it.",
            ):
                _handle_remove_table(meeting.meeting_id, user_id, item.item)
            st.caption("Uploading another sheet replaces this one and clears every answer to it.")

        uploaded = st.file_uploader(
            "Upload the sheet for this item",
            type=["csv", "xlsx", "xls"],
            key=f"meetings_table_upload_{meeting.meeting_id}_{item.item}",
            help="One row per thing the invitee has to respond about.",
        )
        if uploaded is None:
            return

        try:
            frame = tables.read_source(uploaded.getvalue(), uploaded.name)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            return

        show_dataframe(frame.head(5), width="stretch", hide_index=True)

        editable = st.multiselect(
            "Columns the invitee fills in",
            options=list(frame.columns),
            default=list(attached.editable_columns) if attached is not None else [],
            key=f"meetings_table_editable_{meeting.meeting_id}_{item.item}",
            help="Everything else is shown read-only, for reference.",
        )

        if st.button(
            "Attach table",
            key=f"meetings_table_save_{meeting.meeting_id}_{item.item}",
            icon=":material/table:",
            type="primary",
            help="Saves this sheet as the grid every invitee fills in for this item.",
        ):
            _handle_attach_table(meeting.meeting_id, user_id, item.item, uploaded.name, frame, editable)


def _handle_attach_table(
    meeting_id: int,
    user_id: int,
    item_ref: str,
    filename: str,
    frame: pd.DataFrame,
    editable: list[str],
) -> None:
    if not editable:
        st.warning(
            "Mark at least one column as editable — a grid with nothing to fill in is only a document.",
            icon=":material/error:",
        )
        return

    table = AgendaTable(
        meeting_id=meeting_id,
        item_ref=item_ref,
        source_file=filename,
        # Column order is the sheet's own, minus the editable ones — the creator laid the
        # columns out in an order that reads, and re-sorting them would undo that.
        locked_columns=[column for column in frame.columns if column not in editable],
        editable_columns=[column for column in frame.columns if column in editable],
        base_data=tables.base_data_from_frame(frame),
    )

    try:
        db.save_agenda_table(meeting_id, user_id, table)
    except MeetingError as error:
        logger.exception("Could not attach a table to '%s'.", item_ref)
        st.error(str(error), icon=":material/error:")
        return

    session.flash(f"Table attached to '{item_ref}'.")
    st.rerun()


def _handle_remove_table(meeting_id: int, user_id: int, item_ref: str) -> None:
    try:
        db.delete_agenda_table(meeting_id, user_id, item_ref)
    except MeetingError as error:
        logger.exception("Could not remove the table on '%s'.", item_ref)
        st.error(str(error), icon=":material/error:")
        return

    session.flash(f"Table removed from '{item_ref}'.")
    st.rerun()


def _render_list_setup(meeting: Meeting, user_id: int, name: str) -> None:
    """Uploading the list a For each runs over (phase 52), its row name, and who gets which rows.

    Every invitee gets every row unless the Advanced filter is on (phase 57). Opened by
    default while no list is attached: until there is, its questions are skipped.
    """
    try:
        attached = db.find_agenda_table(meeting.meeting_id, name)
    except MeetingError as error:
        logger.exception("Could not read the list '%s'.", name)
        st.error(str(error), icon=":material/error:")
        return

    questions = ", ".join(item.item for item in steps.loop_questions(meeting, name))
    key = f"{meeting.meeting_id}_{name}"
    with st.expander(f"List — {name}", expanded=attached is None):
        st.caption(f":red[Asked for every row: {questions}.]")
        if attached is None:
            st.caption(":red[No list attached yet — these questions are skipped until you upload one.]")
        else:
            _render_attached_list(meeting, user_id, name, attached, key)

        uploaded = st.file_uploader(
            "Upload the list",
            type=["csv", "xlsx", "xls"],
            key=f"meetings_list_upload_{key}",
            help="One row per thing to ask about, e.g. one row per outstanding invoice.",
        )
        if uploaded is None:
            return

        try:
            frame = tables.read_source(uploaded.getvalue(), uploaded.name)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            return

        show_dataframe(frame.head(5), width="stretch", hide_index=True)
        columns = [str(column) for column in frame.columns]
        label_column, match_column = _list_settings_inputs(
            columns,
            attached.label_column if attached is not None else (columns[0] if columns else ""),
            attached.match_column if attached is not None else "",
            f"new_{key}",
        )
        if st.button(
            "Attach list",
            key=f"meetings_list_save_{key}",
            icon=":material/list:",
            type="primary",
            help="Saves this list; its questions are then asked once per row.",
        ):
            _handle_attach_list(meeting, user_id, name, uploaded.name, frame, label_column, match_column)


def _render_attached_list(meeting: Meeting, user_id: int, name: str, attached: AgendaTable, key: str) -> None:
    """What is attached, a warning for invitees who get no rows, and its settings."""
    who = (
        f"only rows matching the invitee in {attached.match_column}"
        if attached.match_column
        else "every invitee gets every row"
    )
    st.caption(
        f":red[{attached.source_file or 'Uploaded list'} · {attached.row_count()} row(s) · "
        f"row name: {attached.label_column or 'row number'} · {who}]"
    )
    try:
        invitees = db.list_invitees(meeting.meeting_id, user_id)
    except MeetingError:
        logger.exception("Could not read the invitees of meeting %s.", meeting.meeting_id)
        invitees = []
    for invitee in loops.zero_row_invitees(attached, invitees):
        st.caption(
            f":red[{invitee['name']} ({invitee['email']}) gets 0 rows — these questions are skipped for them.]"
        )

    label_column, match_column = _list_settings_inputs(
        attached.all_columns(), attached.label_column, attached.match_column, f"saved_{key}"
    )
    if st.button(
        "Save list settings",
        key=f"meetings_list_settings_{key}",
        icon=":material/save:",
        help="Changes the row name and the Advanced filter. Answers already given are kept.",
    ):
        _handle_list_settings(meeting, user_id, name, label_column, match_column)
    if st.button(
        "Remove this list",
        key=f"meetings_list_remove_{key}",
        icon=":material/delete:",
        help="Deletes the list and every answer given about its rows.",
    ):
        _handle_remove_list(meeting, user_id, name)
    st.caption(":red[Uploading another list replaces this one and clears every answer given to it.]")


def _list_settings_inputs(columns: list[str], label_current: str, match_current: str, key: str) -> tuple[str, str]:
    """The Row name picker and the Advanced filter; returns (row name column, match column)."""
    label_options = [ROW_NUMBER_LABEL, *columns]
    label = st.selectbox(
        "Row name column",
        options=label_options,
        index=label_options.index(label_current) if label_current in columns else 0,
        key=f"meetings_list_label_{key}",
        help="The column that names each row, e.g. Invoice No or Task ID. The chat then says "
        "'INV-102' instead of 'row 2'.",
    )
    own_rows = st.toggle(
        "Advanced: only give each invitee their own rows",
        value=bool(match_current and match_current in columns),
        key=f"meetings_list_own_{key}",
        help="Off (usual): every invitee sees the whole list and is asked about every row. "
        "On: each invitee gets only the rows whose value in the column below is their name or email.",
    )
    match = ""
    if own_rows and columns:
        match = st.selectbox(
            "Column holding the invitee's name or email",
            options=columns,
            index=columns.index(match_current) if match_current in columns else 0,
            key=f"meetings_list_match_{key}",
            help="e.g. a Customer email column. Rows whose value here is not an invitee's name or "
            "email are asked to nobody.",
        )
    return ("" if label == ROW_NUMBER_LABEL else label), match


def _list_answer_prefixes(meeting: Meeting, name: str) -> list[str]:
    return [steps.row_key_prefix(item.item) for item in steps.loop_questions(meeting, name)]


def _handle_attach_list(
    meeting: Meeting,
    user_id: int,
    name: str,
    filename: str,
    frame: pd.DataFrame,
    label_column: str,
    match_column: str,
) -> None:
    table = AgendaTable(
        meeting_id=meeting.meeting_id,
        item_ref=name,
        source_file=filename,
        locked_columns=list(frame.columns),
        base_data=tables.base_data_from_frame(frame),
        match_column=match_column,
        label_column=label_column,
    )
    try:
        db.save_agenda_table(meeting.meeting_id, user_id, table)
        # Answers were given about the old rows; row 3 of a new list is a different thing.
        db.delete_row_answers(meeting.meeting_id, user_id, _list_answer_prefixes(meeting, name))
    except MeetingError as error:
        logger.exception("Could not attach the list '%s'.", name)
        st.error(str(error), icon=":material/error:")
        return

    session.flash(f"List attached to '{name}' ({len(frame)} row(s)).")
    st.rerun()


def _handle_list_settings(meeting: Meeting, user_id: int, name: str, label_column: str, match_column: str) -> None:
    try:
        db.update_list_settings(meeting.meeting_id, user_id, name, label_column, match_column)
    except MeetingError as error:
        logger.exception("Could not save the settings of the list '%s'.", name)
        st.error(str(error), icon=":material/error:")
        return

    session.flash(f"List settings saved for '{name}'.")
    st.rerun()


def _handle_remove_list(meeting: Meeting, user_id: int, name: str) -> None:
    try:
        db.delete_agenda_table(meeting.meeting_id, user_id, name)
        db.delete_row_answers(meeting.meeting_id, user_id, _list_answer_prefixes(meeting, name))
    except MeetingError as error:
        logger.exception("Could not remove the list '%s'.", name)
        st.error(str(error), icon=":material/error:")
        return

    session.flash(f"List removed from '{name}'.")
    st.rerun()


def _faq_misses(meeting: Meeting, user_id: int) -> list:
    """The side questions the FAQ couldn't answer, or none if they can't be read."""
    try:
        return db.list_faq_misses(meeting.meeting_id, user_id)
    except MeetingError:
        logger.exception("Could not read the unanswered questions for meeting %s.", meeting.meeting_id)
        return []


def _render_faq_setup(meeting: Meeting, user_id: int) -> None:
    """The FAQ the bot answers side questions from, and what it couldn't answer (phase 53)."""
    meeting_id = meeting.meeting_id
    try:
        current = db.load_faq(meeting_id)
    except MeetingError as error:
        logger.exception("Could not read the FAQ for meeting %s.", meeting_id)
        st.error(str(error), icon=":material/error:")
        return
    misses = _faq_misses(meeting, user_id)

    with st.expander("FAQ — answers the bot can give", expanded=bool(misses)):
        st.caption(
            ":red[Invitees can ask side questions at any time. The bot answers only from this FAQ; "
            "anything it doesn't cover is noted below for you to answer.]"
        )
        if current is None:
            st.caption(":red[No FAQ yet — the bot answers side questions from the Context / SOP only.]")
        else:
            st.caption(f":red[{current.source_file or 'FAQ'} · {len(current.entries)} question(s)]")

        template_column, upload_column = st.columns(2, vertical_alignment="bottom")
        with template_column:
            try:
                template = loops.to_excel_bytes(faq.template_frame(), "FAQ")
            except MeetingError as error:
                st.error(str(error), icon=":material/error:")
            else:
                st.download_button(
                    "Download template",
                    data=template,
                    file_name="FAQ template.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"meetings_faq_template_{meeting_id}",
                    icon=":material/download:",
                    help="An Excel file with the Question and Answer columns and two sample rows. Fill it in and upload it.",
                )
        with upload_column:
            uploaded = st.file_uploader(
                "Upload FAQ",
                type=["csv", "xlsx", "xls"],
                key=f"meetings_faq_upload_{meeting_id}",
                help="An Excel or CSV with a Question column and an Answer column. It replaces the table "
                "below; press Save FAQ to keep it.",
            )
        if uploaded is not None:
            _take_faq_upload(meeting_id, uploaded)

        start_frame = st.session_state.get(_faq_rows_key(meeting_id))
        if start_frame is None:
            start_frame = faq.frame_from_faq(current)
        edited = st.data_editor(
            start_frame,
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            key=_faq_table_key(meeting_id),
            column_config={
                faq.QUESTION_COLUMN: st.column_config.TextColumn(
                    faq.QUESTION_COLUMN, help="A question invitees might ask, e.g. 'What is the notice period?'"
                ),
                faq.ANSWER_COLUMN: st.column_config.TextColumn(
                    faq.ANSWER_COLUMN, help="The only answer the bot may give to it, e.g. '60 days.'"
                ),
            },
        )
        if st.button(
            "Save FAQ",
            key=f"meetings_faq_save_{meeting_id}",
            icon=":material/quiz:",
            type="primary",
            help=f"Saves the table (max {faq.MAX_FAQ_ENTRIES} rows); the bot uses it from the invitee's next "
            "message. Saving an empty table removes the FAQ.",
        ):
            _handle_save_faq(meeting_id, user_id, current, edited)

        _render_faq_misses(meeting_id, user_id, current, misses)


def _faq_rows_key(meeting_id: int) -> str:
    """Where an uploaded FAQ waits in the table until Save FAQ."""
    return f"meetings_faq_rows_{meeting_id}"


def _faq_table_key(meeting_id: int) -> str:
    return f"meetings_faq_table_{meeting_id}"


def _take_faq_upload(meeting_id: int, uploaded) -> None:
    """Puts a newly uploaded FAQ into the table, replacing what it showed. Once per file."""
    seen_key = f"meetings_faq_upload_seen_{meeting_id}"
    if st.session_state.get(seen_key) == uploaded.file_id:
        return
    try:
        frame = faq.frame_from_upload(tables.read_source(uploaded.getvalue(), uploaded.name))
    except MeetingError as error:
        logger.warning("Could not read the FAQ upload %s: %s", uploaded.name, error)
        st.error(str(error), icon=":material/error:")
        return
    st.session_state[seen_key] = uploaded.file_id
    st.session_state[_faq_rows_key(meeting_id)] = frame
    st.session_state[f"meetings_faq_source_{meeting_id}"] = uploaded.name
    # The table's edits are by row position and belong to what it showed before.
    st.session_state.pop(_faq_table_key(meeting_id), None)


def _handle_save_faq(meeting_id: int, user_id: int, current: Faq | None, edited: pd.DataFrame) -> None:
    has_rows = any(
        str(question or "").strip() and str(answer or "").strip()
        for question, answer in zip(edited[faq.QUESTION_COLUMN].fillna(""), edited[faq.ANSWER_COLUMN].fillna(""))
    )
    source = st.session_state.get(f"meetings_faq_source_{meeting_id}") or (
        current.source_file if current is not None else "Added by hand"
    )
    try:
        if has_rows:
            entries = faq.entries_from_frame(edited, faq.QUESTION_COLUMN, faq.ANSWER_COLUMN)
            db.save_faq(meeting_id, user_id, Faq(source_file=source, entries=entries))
        elif current is not None:
            db.delete_faq(meeting_id, user_id)
        else:
            st.warning("Type at least one question and its answer first.", icon=":material/warning:")
            return
    except MeetingError as error:
        logger.exception("Could not save the FAQ for meeting %s.", meeting_id)
        st.error(str(error), icon=":material/error:")
        return

    for key in (_faq_rows_key(meeting_id), _faq_table_key(meeting_id), f"meetings_faq_source_{meeting_id}"):
        st.session_state.pop(key, None)
    session.flash(f"FAQ saved ({len(entries)} question(s))." if has_rows else "FAQ removed.")
    st.rerun()


def _render_faq_misses(meeting_id: int, user_id: int, current: Faq | None, misses: list) -> None:
    st.markdown(f"**Questions the bot couldn't answer ({len(misses)})**")
    if not misses:
        st.caption(":red[None so far.]")
        return

    st.caption(":red[Type an answer next to any question and press Add to FAQ — the bot can then answer it for everyone.]")
    frame = faq.misses_frame(misses)
    edited = st.data_editor(
        frame,
        width="stretch",
        hide_index=True,
        key=f"meetings_faq_misses_{meeting_id}",
        disabled=[column for column in frame.columns if column != faq.ANSWER_COLUMN],
        column_config={
            faq.ANSWER_COLUMN: st.column_config.TextColumn(
                faq.ANSWER_COLUMN, help="The answer the bot should give from now on."
            ),
        },
    )
    if st.button(
        "Add to FAQ",
        key=f"meetings_faq_add_{meeting_id}",
        icon=":material/playlist_add:",
        help="Adds every question you answered here to the FAQ and takes it off this list.",
    ):
        _handle_add_to_faq(meeting_id, user_id, current, misses, edited)

    try:
        payload = loops.to_excel_bytes(frame.drop(columns=[faq.ANSWER_COLUMN]), "Unanswered questions")
    except MeetingError as error:
        st.error(str(error), icon=":material/error:")
        return
    st.download_button(
        "Download Excel",
        data=payload,
        file_name="Unanswered questions.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"meetings_faq_misses_download_{meeting_id}",
        icon=":material/download:",
        help="The questions the bot couldn't answer, with who asked and when.",
    )


def _handle_add_to_faq(meeting_id: int, user_id: int, current: Faq | None, misses: list, edited: pd.DataFrame) -> None:
    answered_ids, new_entries = [], []
    for miss, answer in zip(misses, edited[faq.ANSWER_COLUMN]):
        answer = str(answer or "").strip()
        if answer:
            answered_ids.append(miss.miss_id)
            new_entries.append(FaqEntry(question=miss.question, answer=answer))
    if not new_entries:
        st.warning("Type an answer next to at least one question first.", icon=":material/warning:")
        return

    try:
        db.save_faq(meeting_id, user_id, faq.add_answers(current, new_entries))
        db.delete_faq_misses(meeting_id, user_id, answered_ids)
    except MeetingError as error:
        logger.exception("Could not add answers to the FAQ for meeting %s.", meeting_id)
        st.error(str(error), icon=":material/error:")
        return
    # The grid's edits are by row position; the list is now shorter, so they would land on
    # the wrong questions.
    st.session_state.pop(f"meetings_faq_misses_{meeting_id}", None)
    st.session_state.pop(_faq_rows_key(meeting_id), None)
    st.session_state.pop(_faq_table_key(meeting_id), None)
    session.flash(f"Added {len(new_entries)} answer(s) to the FAQ.")
    st.rerun()


def _handle_ref_upload(meeting_id: int, uploaded) -> None:
    try:
        for upload in uploaded:
            path = storage.save_upload(upload.getvalue(), upload.name, meeting_id)
            db.add_file(meeting_id, upload.name, str(path))
    except MeetingError as error:
        logger.exception("Could not store reference documents for meeting %s.", meeting_id)
        st.error(str(error), icon=":material/error:")
        return
    session.flash("Reference documents saved.")
    st.rerun()


def _render_add_invitee(meeting: Meeting, user_id: int, invitees: list[dict]) -> None:
    """Adds someone the organiser forgot at creation (phase 55)."""
    meeting_id = meeting.meeting_id
    with st.expander("Add invitee", icon=":material/person_add:", expanded=not invitees):
        with st.form(f"meetings_add_invitee_form_{meeting_id}", clear_on_submit=True, border=False):
            name_column, email_column = st.columns(2)
            name = name_column.text_input(
                "Name",
                key=f"meetings_add_invitee_name_{meeting_id}",
                help="Optional. Left blank, the saved contact's name (or the email) is used.",
            )
            email = email_column.text_input(
                "Email",
                key=f"meetings_add_invitee_email_{meeting_id}",
                help="Required, e.g. raj@x.com. Their link and access code appear in the Share tab.",
            )
            submitted = st.form_submit_button(
                "Add invitee",
                key=f"meetings_add_invitee_submit_{meeting_id}",
                icon=":material/person_add:",
                help="Makes a new link and access code for this person.",
            )
        if submitted:
            _handle_add_invitee(meeting_id, user_id, name, email, invitees)


def _handle_add_invitee(meeting_id: int, user_id: int, name: str, email: str, invitees: list[dict]) -> None:
    email = email.strip()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        st.warning("Enter a valid email, e.g. raj@x.com.", icon=":material/error:")
        return
    if any(str(invitee["email"]).casefold() == email.casefold() for invitee in invitees):
        st.warning(f"{email} is already invited.", icon=":material/error:")
        return

    try:
        name = name.strip() or str((db.find_contact(email) or {}).get("name") or "").strip() or email
        db.add_invitee(meeting_id, user_id, name, email, access.generate_token(), access.encrypt_code(access.generate_access_code()))
        db.remember_contact(email, name, user_id)
    except MeetingError as error:
        logger.exception("Could not add invitee %s to meeting %s.", email, meeting_id)
        st.error(str(error), icon=":material/error:")
        return
    session.flash(f"Added {name}. Their link and access code are in the Share tab.")
    st.rerun()


def _render_invitee_status(
    meeting: Meeting, user_id: int, invitees: list[dict], fields: list[EvaluationField]
) -> None:
    _render_add_invitee(meeting, user_id, invitees)
    if not invitees:
        st.info("This meeting has no invitees.", icon=":material/info:")
        return

    discussion_count = len(meeting.discussion_items())
    all_answers = _all_step_answers(meeting, user_id)
    list_sheets = _list_tables(meeting)
    ordinary = [item for item in meeting.question_items() if not item.loop]
    misses_by_invitee: dict[int, int] = {}
    for miss in _faq_misses(meeting, user_id):
        misses_by_invitee[miss.invitee_id] = misses_by_invitee.get(miss.invitee_id, 0) + 1
    if misses_by_invitee:
        st.caption(
            f":red[{sum(misses_by_invitee.values())} question(s) the bot couldn't answer — "
            "see Overview → FAQ]"
        )

    for invitee in invitees:
        with st.container(border=True):
            joined = invitee["last_active_at"] is not None
            closed = bool(invitee["closed"])
            status = "Finished" if closed else ("In progress" if joined else "Not started")

            try:
                messages = db.list_messages_for_creator(meeting.meeting_id, invitee["invitee_id"], user_id)
            except MeetingError:
                logger.exception("Could not read messages for invitee %s.", invitee["invitee_id"])
                messages = []

            covered = summary_agent.coverage(messages, meeting)
            st.markdown(f"**{invitee['name']}** — {invitee['email']}")
            model_used = f" · model: {invitee['model_used']}" if invitee.get("model_used") else ""
            st.caption(
                f"{status} · last active {invitee['last_active_at'] or '—'} · "
                f"{len(covered)} of {discussion_count} agenda item(s) covered{model_used}"
            )

            # Spec 3a tracks a table item by rows filled, so it gets its own line rather
            # than being folded into the agenda count above.
            for item_ref, (filled, total) in _table_progress(meeting, invitee["invitee_id"]).items():
                st.caption(f"{item_ref}: {tables.format_completion(filled, total)}")

            if meeting.question_items():
                answers = all_answers.get(invitee["invitee_id"], {})
                lists = loops.invitee_lists(list_sheets, invitee)
                walk = steps.walk(meeting, answers, lists.rows)
                if ordinary:
                    answered = sum(
                        1 for item in ordinary if item.item in answers and answers[item.item].status == STEP_ANSWERED
                    )
                    # Questions a Go to jumped over are not counted as missing.
                    skipped = sum(1 for step in walk.skipped if step.row is None)
                    st.caption(f":red[{answered} of {len(ordinary) - skipped} question(s) answered]")
                for name in steps.loop_names(meeting):
                    if steps.loop_key(name) not in list_sheets:
                        st.caption(f":red[{name}: no list attached]")
                        continue
                    rows = lists.rows.get(steps.loop_key(name), [])
                    st.caption(f":red[{loops.progress_label(meeting, name, walk, answers, rows)}]")

            unanswered = misses_by_invitee.get(invitee["invitee_id"], 0)
            if unanswered:
                st.caption(f":red[{unanswered} question(s) the bot couldn't answer]")

            _render_invitee_files(meeting.meeting_id, invitee["invitee_id"])

            if not closed and joined:
                if st.button(
                    "Generate status",
                    key=f"meetings_status_{invitee['invitee_id']}",
                    icon=":material/summarize:",
                    help="A provisional summary of this in-progress chat. Overwrites the previous one.",
                ):
                    _handle_generate_status(meeting, user_id, invitee["invitee_id"], messages, fields)

            if closed and fields:
                if st.button(
                    "Re-extract answers",
                    key=f"meetings_extract_{invitee['invitee_id']}",
                    icon=":material/fact_check:",
                    help="Reads the evaluation answers out of this finished chat again.",
                ):
                    _handle_extract(meeting, user_id, invitee["invitee_id"], messages, fields)

            snapshot = summary_agent.from_json(invitee["live_status_json"] or "")
            if snapshot is not None and not closed:
                with st.expander(f"Live status — generated {invitee['live_status_at']}"):
                    _render_summary(snapshot)


def _render_invitee_files(meeting_id: int, invitee_id: int) -> None:
    """The files this invitee attached, each with a download button (phase 57).

    The meeting was already opened through `load_meeting`, which proves it is this
    organiser's, so reading its invitees' uploads here is allowed.
    """
    try:
        files = db.list_files(meeting_id, invitee_id)
    except MeetingError:
        logger.exception("Could not list the files of invitee %s.", invitee_id)
        st.caption(":red[Their files couldn't be read.]")
        return
    if not files:
        return

    st.markdown(f"**Files ({len(files)})**")
    for record in files:
        try:
            payload = storage.read_file(record["filepath"])
        except MeetingError:
            st.caption(f":red[{record['filename']} — no longer on disk]")
            continue
        st.download_button(
            record["filename"],
            data=payload,
            file_name=record["filename"],
            key=f"meetings_invitee_file_dl_{record['file_id']}",
            icon=":material/download:",
            help="Download the file this invitee attached.",
        )


def _all_step_answers(meeting: Meeting, user_id: int) -> dict:
    """Every invitee's question answers, or nothing if there are no questions or they can't be read."""
    if not meeting.question_items():
        return {}
    try:
        return db.list_all_step_answers(meeting.meeting_id, user_id)
    except MeetingError:
        logger.exception("Could not read question answers for meeting %s.", meeting.meeting_id)
        return {}


def _table_progress(meeting: Meeting, invitee_id: int) -> dict[str, tuple[int, int]]:
    """One invitee's row counts per table item, or nothing if they can't be read.

    Only Table items: a For each list is stored the same way but is counted by rows done.
    """
    try:
        progress = db.table_progress(meeting.meeting_id, invitee_id)
    except MeetingError:
        logger.exception("Could not read table progress for invitee %s.", invitee_id)
        return {}
    titles = {item.item for item in meeting.table_items()}
    return {title: counts for title, counts in progress.items() if title in titles}


def _list_tables(meeting: Meeting) -> dict[str, AgendaTable]:
    """The sheets behind the meeting's For each lists, or none if they can't be read."""
    if not steps.loop_names(meeting):
        return {}
    try:
        return loops.list_tables(meeting, db.list_agenda_tables(meeting.meeting_id))
    except MeetingError:
        logger.exception("Could not read the lists for meeting %s.", meeting.meeting_id)
        return {}


def _handle_generate_status(
    meeting: Meeting, user_id: int, invitee_id: int, messages: list, fields: list[EvaluationField]
) -> None:
    """The on-demand snapshot: a provisional MoM, and the evaluation answers alongside it.

    Both, on one press, because spec 3 makes them the same question asked of the same
    conversation — "where has this invitee got to". The extraction failing is reported but
    does not discard the status that was already generated.
    """
    profile = llm_choice.meeting_profile(meeting).profile
    if profile is None:
        st.error("Pick an AI model in Overview (or set a default in Settings) before generating a summary.", icon=":material/error:")
        return

    try:
        summary = summary_agent.generate_summary(
            meeting, profile, messages, table_progress=_table_progress(meeting, invitee_id)
        )
        db.save_live_status(meeting.meeting_id, invitee_id, user_id, summary_agent.to_json(summary))
    except (MeetingAgentError, MeetingError) as error:
        logger.exception("Could not generate a live status for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    if fields:
        try:
            db.save_evaluation_answers(
                invitee_id, extraction_agent.extract_answers(meeting, profile, fields, messages)
            )
        except (MeetingAgentError, MeetingError) as error:
            logger.exception("Could not extract evaluation answers for invitee %s.", invitee_id)
            st.warning(f"Status generated, but the evaluation answers weren't: {error}", icon=":material/error:")

    session.flash("Status generated.")
    st.rerun()


def _handle_extract(
    meeting: Meeting, user_id: int, invitee_id: int, messages: list, fields: list[EvaluationField]
) -> None:
    """Re-reads a finished chat's evaluation answers, leaving its locked MoM alone."""
    profile = llm_choice.meeting_profile(meeting).profile
    if profile is None:
        st.error("Pick an AI model in Overview (or set a default in Settings) before extracting answers.", icon=":material/error:")
        return

    try:
        db.save_evaluation_answers(
            invitee_id, extraction_agent.extract_answers(meeting, profile, fields, messages)
        )
    except (MeetingAgentError, MeetingError) as error:
        logger.exception("Could not extract evaluation answers for invitee %s.", invitee_id)
        st.error(str(error), icon=":material/error:")
        return

    session.flash("Evaluation answers extracted.")
    st.rerun()


def _render_transcript(messages: list) -> None:
    """One invitee's conversation, read-only.

    Shared shape with the invitee's own view on purpose — a creator reviewing a chat should
    be reading the same thing the invitee saw, not a reformatted approximation of it.
    """
    if not messages:
        st.info("This invitee hasn't started their chat yet.", icon=":material/info:")
        return
    for message in messages:
        with st.chat_message("assistant" if message.is_from_ai() else "user"):
            st.write(message.text)


def _render_chat_tab(meeting: Meeting, user_id: int, invitees: list[dict]) -> None:
    """One invitee's chat, with Download chat (.txt) and Download all chats (Excel) (phase 57)."""
    chosen = _pick_invitee(invitees, "meetings_chat_pick")
    if chosen is None:
        st.info("This meeting has no invitees.", icon=":material/info:")
        return
    try:
        messages = db.list_messages_for_creator(meeting.meeting_id, chosen["invitee_id"], user_id)
    except MeetingError as error:
        st.error(str(error), icon=":material/error:")
        return

    subject = meeting.display_subject()
    left, right = st.columns(2)
    with left:
        st.download_button(
            "Download chat",
            data=transcript.chat_text(subject, chosen["name"], messages),
            file_name=f"{subject} - {chosen['name']} - chat.txt",
            mime="text/plain",
            key="meetings_chat_download",
            icon=":material/download:",
            disabled=not messages,
            help="This invitee's chat as a text file.",
        )
    with right:
        _render_all_chats_download(meeting, user_id, invitees)
    _render_transcript(messages)


def _render_all_chats_download(meeting: Meeting, user_id: int, invitees: list[dict]) -> None:
    chats = []
    try:
        for invitee in invitees:
            chats.append(
                (invitee["name"], db.list_messages_for_creator(meeting.meeting_id, invitee["invitee_id"], user_id))
            )
        payload = transcript.to_excel_bytes(transcript.all_chats_frame(chats))
    except MeetingError as error:
        logger.exception("Could not build the all-chats Excel for meeting %s.", meeting.meeting_id)
        st.caption(f":red[{error}]")
        return
    st.download_button(
        "Download all chats (Excel)",
        data=payload,
        file_name=f"{meeting.display_subject()} - all chats.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="meetings_all_chats_download",
        icon=":material/table:",
        help="Every invitee's chat in one Excel sheet: Invitee, Time, From, Message.",
    )


def _render_summary_tab(meeting: Meeting, invitees: list[dict]) -> None:
    """One finished invitee's summary, with Download summary (.txt) (phase 57)."""
    chosen = _pick_invitee(invitees, "meetings_summary_pick", only_closed=True)
    if chosen is None:
        st.info("No invitee has finished their chat yet.", icon=":material/info:")
        return
    parsed = summary_agent.from_json(chosen["summary_json"] or "")
    if parsed is None:
        st.warning("This invitee's summary couldn't be read.", icon=":material/error:")
        return

    subject = meeting.display_subject()
    st.download_button(
        "Download summary",
        data=transcript.summary_text(subject, chosen["name"], parsed),
        file_name=f"{subject} - {chosen['name']} - summary.txt",
        mime="text/plain",
        key="meetings_summary_download",
        icon=":material/download:",
        help="This invitee's summary as a text file.",
    )
    _render_summary(parsed)


def _pick_invitee(invitees: list[dict], key: str, only_closed: bool = False) -> dict | None:
    options = [person for person in invitees if not only_closed or person["closed"]]
    if not options:
        return None
    labels = {person["invitee_id"]: f"{person['name']} ({person['email']})" for person in options}
    chosen = st.selectbox(
        "Invitee",
        options=list(labels),
        format_func=lambda value: labels[value],
        key=key,
        help="Whose conversation to show.",
    )
    return next(person for person in options if person["invitee_id"] == chosen)


def _render_summary(summary) -> None:
    for entry in summary.agenda_items:
        if entry.is_table:
            # A table item is never "discussed" — its line is a row count, so a tick beside a
            # half-filled grid would be claiming something the number underneath contradicts.
            st.markdown(f":red[:material/table:] **{entry.item}**")
        else:
            icon = ":material/check_circle:" if entry.discussed else ":material/radio_button_unchecked:"
            st.markdown(f":{'green' if entry.discussed else 'red'}[{icon}] **{entry.item}**")
        st.write(entry.notes or "_Not discussed._")

    if summary.other_extra.strip():
        st.markdown("**Other / Extra**")
        st.write(summary.other_extra)

    if summary.closing_message.strip():
        st.caption(summary.closing_message)


def _render_comparisons(
    meeting: Meeting, user_id: int, invitees: list[dict], fields: list[EvaluationField]
) -> None:
    """Spec 8's three cross-invitee matrices, as sub-tabs.

    None of them makes a provider call: every cell was written when a chat closed, when a
    status was generated, or when an invitee pressed Save progress.
    """
    if not invitees:
        st.info("This meeting has no invitees to compare.", icon=":material/info:")
        return

    questions, list_answers, consolidated, evaluation, table_comparison = st.tabs(
        ["Question answers", "List answers", "Consolidated MoM", "Evaluation answers", "Table comparisons"]
    )

    with questions:
        _render_question_answers(meeting, user_id, invitees)

    with list_answers:
        _render_list_answers(meeting, user_id, invitees)

    with consolidated:
        _render_consolidated(meeting, invitees)

    with evaluation:
        _render_evaluation_matrix(meeting, invitees, fields)

    with table_comparison:
        _render_table_comparisons(meeting, invitees)


def _render_question_answers(meeting: Meeting, user_id: int, invitees: list[dict]) -> None:
    """One row per invitee, one column per question item (phase 50). For each questions are
    per row, so they are shown under List answers instead."""
    questions = [item for item in meeting.question_items() if not item.loop]
    if not questions:
        st.info(
            "This meeting has no question items. Set an agenda item's Type to Question in "
            "Overview to ask it step by step.",
            icon=":material/info:",
        )
        return

    all_answers = _all_step_answers(meeting, user_id)
    list_sheets = _list_tables(meeting)
    rows = []
    for invitee in invitees:
        answers = all_answers.get(invitee["invitee_id"], {})
        skipped = set(steps.skipped_questions(meeting, answers, loops.invitee_lists(list_sheets, invitee).rows))
        row = {"Invitee": invitee["name"]}
        for item in questions:
            row[item.item] = SKIPPED_CELL if item.item in skipped else steps.display_answer(answers.get(item.item))
        rows.append(row)

    st.caption(
        ":red[Saved as each answer passed its rule. ⚠ means the tries ran out; "
        "skipped means a Go to jumped over it.]"
    )
    show_dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_list_answers(meeting: Meeting, user_id: int, invitees: list[dict]) -> None:
    """Each For each list with its answers written back, and a filled-in Excel (phase 52)."""
    names = steps.loop_names(meeting)
    if not names:
        st.info(
            "This meeting has no For each lists. Give Question items a For each name in Overview "
            "to ask them once per row of an uploaded list.",
            icon=":material/info:",
        )
        return

    all_answers = _all_step_answers(meeting, user_id)
    list_sheets = _list_tables(meeting)
    for name in names:
        st.markdown(f"**{name}**")
        if steps.loop_key(name) not in list_sheets:
            st.caption(":red[No list attached yet — upload it in Overview.]")
            continue
        frame = loops.results_frame(meeting, name, list_sheets, invitees, all_answers)
        st.caption(
            ":red[One row per invitee per list row. ⚠ means the tries ran out; skipped means a Go to "
            "jumped over it; blank means not reached yet.]"
        )
        show_dataframe(frame, width="stretch", hide_index=True)
        try:
            payload = loops.to_excel_bytes(frame, name)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            continue
        st.download_button(
            "Download Excel",
            data=payload,
            file_name=f"{name}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"meetings_list_download_{meeting.meeting_id}_{name}",
            icon=":material/download:",
            help="The list with every invitee's answers added as columns.",
        )


def _render_consolidated(meeting: Meeting, invitees: list[dict]) -> None:
    if not meeting.agenda:
        st.info("This meeting has no agenda items to compare.", icon=":material/info:")
        return

    summaries = {}
    for invitee in invitees:
        # The final MoM where there is one, the live snapshot otherwise — spec 8's
        # "whichever is available". A closed chat's record always wins over a provisional one.
        stored = invitee["summary_json"] or invitee["live_status_json"] or ""
        parsed = summary_agent.from_json(stored)
        if parsed is not None:
            summaries[invitee["invitee_id"]] = parsed

    st.caption("Rows are agenda items, columns are invitees. Nothing here calls the model.")
    show_dataframe(
        matrix.consolidated_frame(meeting, invitees, summaries),
        width="stretch",
        hide_index=True,
    )


def _render_evaluation_matrix(meeting: Meeting, invitees: list[dict], fields: list[EvaluationField]) -> None:
    if not fields:
        st.info(
            "This meeting has no evaluation questions. Add them in Overview to compare short "
            "answers across invitees.",
            icon=":material/info:",
        )
        return

    try:
        answers = db.list_evaluation_answers(meeting.meeting_id)
    except MeetingError as error:
        logger.exception("Could not read evaluation answers for meeting %s.", meeting.meeting_id)
        st.error(str(error), icon=":material/error:")
        return

    st.caption("Extracted when each chat closed. Use Generate status or Re-extract to refresh.")
    show_dataframe(
        matrix.evaluation_frame(fields, invitees, answers),
        width="stretch",
        hide_index=True,
    )


def _render_table_comparisons(meeting: Meeting, invitees: list[dict]) -> None:
    table_items = meeting.table_items()
    if not table_items:
        st.info("This meeting has no table agenda items.", icon=":material/info:")
        return

    for item in table_items:
        st.markdown(f"**{item.item}**")
        try:
            table = db.find_agenda_table(meeting.meeting_id, item.item)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            continue

        if table is None:
            st.caption("No data attached to this item yet.")
            continue
        if not table.editable_columns:
            st.caption("This table has no columns for invitees to fill in.")
            continue

        column = st.selectbox(
            "Column to compare",
            options=table.editable_columns,
            key=f"meetings_compare_column_{table.table_id}",
            help="One column at a time — every column at once is too wide to read down.",
        )

        try:
            responses = db.load_all_table_responses(table.table_id)
        except MeetingError as error:
            st.error(str(error), icon=":material/error:")
            continue

        show_dataframe(
            matrix.table_comparison_frame(table, column, invitees, responses),
            width="stretch",
            hide_index=True,
        )


def _invite_base_url() -> str:
    """The web address an invitee link starts with.

    A `TIKITARAI_BASE_URL` setting (Streamlit secrets, then environment) wins, for a host
    behind a proxy or a sub-path. Otherwise the address the organiser has open right now —
    on Streamlit Cloud that is `https://yourapp.streamlit.app`, so the link works wherever
    the app is hosted without anyone configuring it. Only the scheme and host are kept: the
    organiser is on the Meetings page, but the invitee lands on the app's front door.
    """
    try:
        configured = str(st.secrets.get(BASE_URL_SETTING) or "").strip()
    except Exception as error:  # no secrets file at all raises rather than returning None
        logger.debug("Could not read %s from Streamlit secrets: %s", BASE_URL_SETTING, error)
        configured = ""
    configured = configured or os.environ.get(BASE_URL_SETTING, "").strip()
    if configured:
        return configured.rstrip("/")

    try:
        browser_url = st.context.url or ""
    except Exception as error:  # no script-run context, e.g. called outside a page run
        logger.warning("Could not read the browser address for invitee links: %s", error)
        browser_url = ""
    parts = urlsplit(browser_url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return FALLBACK_BASE_URL


def _render_share(meeting: Meeting, invitees: list[dict]) -> None:
    base_url = _invite_base_url()
    st.info(
        "Copy each invitee's link and access code and send them yourself — this app doesn't "
        "send email.",
        icon=":material/info:",
    )
    for invitee in invitees:
        with st.container(border=True):
            st.markdown(f"**{invitee['name']}** — {invitee['email']}")
            st.caption("Link")
            st.code(f"{base_url}/?m={meeting.meeting_id}&t={invitee['token']}", language=None)
            st.caption("Access code")
            try:
                st.code(access.decrypt_code(invitee["access_code_enc"]), language=None)
            except MeetingError:
                logger.exception("Could not decrypt the access code for invitee %s.", invitee["invitee_id"])
                st.error("This invitee's access code can't be read.", icon=":material/error:")


def _render_detail(user_id: int, meeting_id: int) -> None:
    try:
        meeting = db.load_meeting(meeting_id, user_id)
        invitees = db.list_invitees(meeting_id, user_id)
        fields = db.list_evaluation_fields(meeting_id)
    except MeetingError as error:
        logger.exception("Could not open meeting %s for user %s.", meeting_id, user_id)
        st.error(str(error), icon=":material/error:")
        if st.button("Back to meetings", key="meetings_back_error", icon=":material/arrow_back:"):
            session.open_meeting(None)
            st.rerun()
        return

    if st.button("Back to meetings", key="meetings_back", icon=":material/arrow_back:",
                 help="Return to the list."):
        session.open_meeting(None)
        st.rerun()

    st.subheader(meeting.display_subject())

    overview, status, chat, summary, comparisons, share = st.tabs(
        ["Overview", "Invitees & status", "View chat", "View summary", "Comparisons", "Share"]
    )

    with overview:
        _render_overview(meeting, user_id, fields)

    with status:
        _render_invitee_status(meeting, user_id, invitees, fields)

    with chat:
        _render_chat_tab(meeting, user_id, invitees)

    with summary:
        _render_summary_tab(meeting, invitees)

    with comparisons:
        _render_comparisons(meeting, user_id, invitees, fields)

    with share:
        _render_share(meeting, invitees)


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


_render_profile_sidebar()

st.title("Meetings")

_flash = session.take_flash()
if _flash:
    st.success(_flash, icon=":material/check_circle:")

_user_id = _current_user_id()
if _user_id is None:
    st.error("Please log in again.", icon=":material/lock:")
    st.stop()

_open_id = session.open_meeting_id()
if _open_id is None:
    _render_list(_user_id)
else:
    _render_detail(_user_id, _open_id)
