"""Task Builder — record a whole analysis once, so it can be run again (requirement 7).

Everything before this page is session-only: Chat with data loads files, builds a report on
the Dashboard, and when the browser reloads it is all gone. A **Task** is that same pipeline
written down — the expected schema, the links, the column meanings, the calculated columns,
the report items, the criteria and the shape of the report — so next month's files can be run
through it (requirement 8, the next stage).

**One question first.** Until a Task is open the page is a gate and nothing else: open a saved
Task, or start a new one under a name. It used to open onto an empty name box, a Save button
and four views, which left "am I editing something saved, or building something new?" to be
inferred from the controls — and made typing a different name into the box the apparent way to
start a second Task, when `tasks.db.save_task` reads a name already in use as *update that
row*. With the choice made up front, the name becomes a heading, Save means write this Task,
and starting another one is a deliberate trip back through **Switch task**.

Four views on one `st.segmented_control`, in the order the work happens:

- **Setup** — the shared Data Engine steps (`app_pages/setup_view.py`), plus this Task's
  persona and description at the foot.
- **Report-Items** — `app_pages/report_items_view.py`. The ordered list of report items and
  column steps, which is what replaces Chat's conversation: a transcript has no order that
  can be replayed, and a list does.
- **Checks** — `app_pages/checks_view.py`, with its set bar and Actions tab turned off. See
  `render_checks` for why each.
- **Report** — `app_pages/report_view.py`. The same Build / Preview / Download workspace the
  Dashboard page uses, on **this Task's** report.

Three things about the shape are load-bearing and are not obvious from reading the page:

**There is no chat type picker.** A Task *is* the saved setup, so a second saved-setup concept
on the same screen would be two answers to one question. Nothing here calls `chat_types`
except through `tasks.model`, which stores the schema signature in that format because it is
the format `chat_types.matching` already knows how to match an upload against.

**Pinning goes to the Task's report, not the Dashboard.** Said once, at the top of the run,
via `dashboard_session.use_report(tasks_session.TB_REPORT_KEY)` — after which the report items
view and the checks view pin exactly as they always did, with no idea they are doing anything
different.

**The upload widgets are mounted on every view.** `setup_view.mount_upload` must be called on
every run or `sync_tables` drops every loaded table, so off Setup it runs inside a hidden
container. That container is not dead UI: deleting it loses the user's data the first time
they switch views. This is the arrangement `chat_with_data.py` documents at length.

Restricted to admins and superusers (requirement 2.2 grants Task Builder to those two), so
this page carries a `require_role` guard like `user_management.py` — and `streamlit_app.py`
registers it conditionally, so it isn't a locked door in a normal user's sidebar.
"""

import logging

import streamlit as st

from analyst import session as chat_session
from app_pages import report_items_view, report_view, saved_picker, setup_view
from app_pages.checks_view import render_checks
from auth.db import get_user_by_id
from auth.exceptions import AuthDatabaseError
from auth.service import require_role
from checks import session as checks_session
from dashboard import session as dashboard_session
from engine import session as engine_session
from report_items import session as report_items_session
from sidebar import render_sidebar
from tasks import db as tasks_db
from tasks import session as tasks_session
from tasks.exceptions import TaskError, TaskStorageError
from tasks.model import UNTITLED_TASK

logger = logging.getLogger(__name__)

VIEWS = ["Setup", "Report-Items", "Checks", "Report"]

VIEW_KEY = "tb_view"
# A view change asked for by something *inside* a view, queued rather than written: the
# toggle is a widget, and Streamlit forbids writing a widget's own key once it exists this
# run — which it does by the time anything below it can ask.
PENDING_VIEW_KEY = "tb_pending_view"

# The container the upload widgets live in off the Setup view, hidden by this page's one
# stylesheet. Named here because the CSS rule has to spell the same key.
HIDDEN_UPLOAD_MOUNT = "tb_upload_mount"

# Three keyed containers that exist on **every** run, whatever the page is showing. They are
# not styling: Streamlit addresses an element by its position among its parent's children, so
# an element that appears only sometimes — a flash message, the gate — shifts everything below
# it by one and the browser is left holding the previous run's copy at the old position. That
# is what put two "Step 1 · Your data" headers on screen. A fixed slot each keeps every
# position constant: the flash slot is simply empty when there is nothing to say, and the gate
# and the open Task each own a container, so switching between them replaces one whole subtree
# rather than reshuffling a shared one.
FLASH_SLOT = "tb_flash_slot"
GATE_BODY = "tb_gate_body"
TASK_BODY = "tb_task_body"
# Wraps whichever of the two upload mounts a view needs — the Step 1 expander on Setup, the
# hidden container everywhere else — so that swap happens *inside* a block that is always
# there, instead of changing what sits at that position on the page.
UPLOAD_SLOT = "tb_upload_slot"

PERSONA_PLACEHOLDER = (
    "You are a finance controller reviewing monthly salary processing. Bonuses above policy "
    "are escalated to the department head, and every figure is reported in INR."
)


if not require_role("admin", "superuser"):
    st.error("You don't have permission to view this page.", icon=":material/lock:")
    st.stop()

try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------


def _dismiss_dialog() -> None:
    """Clears the flag when a dialog is dismissed by the X, ESC or a click outside.

    Without it the flag survives, and the next unrelated rerun reopens the same dialog.
    """
    tasks_session.close_dialog()


@st.dialog("Rename this task", on_dismiss=_dismiss_dialog)
def _dialog_rename_task(payload: dict) -> None:
    """The only way the open Task's name changes.

    It used to be a text box in the bar, permanently editable — which made typing a different
    name look like the way to start another Task, when `tasks.db.save_task` reads a name
    already in use as "update *that* row". One name, changed deliberately, and refused outright
    when it belongs to a different Task.
    """
    user_id = payload["user_id"]

    typed = st.text_input(
        "Task name",
        value=tasks_session.task_name(),
        key="tb_rename_name",
        placeholder="e.g. Monthly salary review",
        help="What this task is called when you come back to open it.",
    )

    confirm, cancel = st.columns(2)
    with confirm:
        if st.button(
            "Rename",
            key="tb_rename_confirm",
            type="primary",
            width="stretch",
            disabled=not typed.strip(),
            help="Use this name from now on. It takes effect in storage the next time you save.",
        ):
            taken = _name_taken(user_id, typed, tasks_session.task_id())
            if taken:
                st.error(
                    f"You already have a task called “{typed.strip()}”. Pick another name — "
                    "saving under a name in use would overwrite that task.",
                    icon=":material/error:",
                )
                return
            tasks_session.set_task_name(typed)
            tasks_session.close_dialog()
            st.rerun(scope="app")
    with cancel:
        if st.button("Cancel", key="tb_rename_cancel", width="stretch", help="Keep the current name."):
            tasks_session.close_dialog()
            st.rerun(scope="app")


@st.dialog("You have unsaved changes", on_dismiss=_dismiss_dialog)
def _dialog_confirm_switch(payload: dict) -> None:
    """Asked before switching away, because switching is the one press that can lose work.

    Only raised when `has_unsaved_changes()` says so — a task that matches what was saved
    switches straight away, with nothing to click through.
    """
    user_id = payload["user_id"]

    st.warning(
        "The report items, criteria and report on screen have changed since this task was "
        "last saved.",
        icon=":material/warning:",
    )

    save, discard, cancel = st.columns(3)
    with save:
        if st.button(
            "Save and switch",
            key="tb_switch_save",
            type="primary",
            width="stretch",
            help="Write this task, then go back to the task list.",
        ):
            if not _save_task(user_id):
                return
            tasks_session.close_task()
            tasks_session.close_dialog()
            st.rerun(scope="app")
    with discard:
        if st.button(
            "Switch anyway",
            key="tb_switch_discard",
            width="stretch",
            help="Go back to the task list without saving. The changes are lost.",
        ):
            tasks_session.close_task()
            tasks_session.close_dialog()
            st.rerun(scope="app")
    with cancel:
        if st.button("Cancel", key="tb_switch_cancel", width="stretch", help="Stay on this task."):
            tasks_session.close_dialog()
            st.rerun(scope="app")


@st.dialog("Delete this task?", on_dismiss=_dismiss_dialog)
def _dialog_confirm_delete(payload: dict) -> None:
    """Deleting is one click on a card, so it is asked about first.

    The old list was a dropdown in a dialog, where choosing and deleting were two separate
    motions; on a card they are the same motion, next to Open.
    """
    user_id = payload["user_id"]
    task_id = payload["task_id"]
    name = payload["name"]

    st.warning(
        f"“{name}” will be deleted permanently. Anything currently on screen is not affected.",
        icon=":material/warning:",
    )

    confirm, cancel = st.columns(2)
    with confirm:
        if st.button(
            "Delete",
            key="tb_delete_confirm",
            type="primary",
            width="stretch",
            icon=":material/delete:",
            help="Delete this saved task for good.",
        ):
            try:
                tasks_db.delete_task(task_id, user_id)
            except TaskStorageError as error:
                logger.exception("Could not delete task %s.", task_id)
                st.error(str(error), icon=":material/error:")
                return
            tasks_session.queue_flash(f"Deleted “{name}”.")
            tasks_session.close_dialog()
            st.rerun(scope="app")
    with cancel:
        if st.button("Cancel", key="tb_delete_cancel", width="stretch", help="Keep this task."):
            tasks_session.close_dialog()
            st.rerun(scope="app")


DIALOGS = {
    # Steps 2 and 3's three, shared with Chat with data.
    **setup_view.SETUP_DIALOGS,
    "rename_task": _dialog_rename_task,
    "confirm_switch": _dialog_confirm_switch,
    "confirm_delete": _dialog_confirm_delete,
}


def _render_pending_task_dialog() -> None:
    """This page's own dialogs. Also called from the gate, which has no engine dialogs to
    resolve — the setup steps aren't on screen there."""
    pending = tasks_session.pending_dialog()
    if pending is None:
        return
    action, payload = pending
    if action in DIALOGS:
        DIALOGS[action](payload)
    else:
        tasks_session.close_dialog()


def _render_pending_dialog() -> None:
    """The engine's dialogs and this page's, resolved from one flag each.

    Two registries rather than one because they are two owners: `engine.session` opens the
    setup dialogs from inside `setup_view`, and this page opens its own.
    """
    pending = engine_session.pending_dialog()
    if pending is not None:
        action, payload = pending
        if action in DIALOGS:
            DIALOGS[action](payload)
        else:
            engine_session.close_dialog()

    _render_pending_task_dialog()


# --------------------------------------------------------------------------------------
# Saved tasks, and the two shapes the page takes
# --------------------------------------------------------------------------------------


def _saved_tasks(user_id: int) -> list[dict] | None:
    """Every saved Task for this account, or None when the list couldn't be read.

    None rather than `[]`, because "you have no tasks" and "we couldn't look" lead the user to
    two different next actions and the gate words them differently.
    """
    try:
        return tasks_db.list_tasks(user_id)
    except TaskStorageError as error:
        logger.exception("Could not list saved tasks for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return None


def _name_taken(user_id: int, name: str, ignoring: int | None = None) -> bool:
    """Whether another saved Task already answers to this name.

    Worth checking before both Create and Rename: `tasks.db.save_task` treats a name already
    in use as "update that row", so a name collision is not an error later — it is a silent
    overwrite of somebody's other Task.

    A lookup that fails returns False: refusing a name because the database hiccuped would
    block the user for a reason that has nothing to do with them.
    """
    wanted = (name or "").strip().casefold()
    if not wanted:
        return False
    try:
        rows = tasks_db.list_tasks(user_id)
    except TaskStorageError:
        logger.exception("Could not check task names for user %s.", user_id)
        return False
    return any(
        str(row["name"]).strip().casefold() == wanted and row["task_id"] != ignoring for row in rows
    )


def _save_task(user_id: int) -> bool:
    """Captures everything on screen and writes it, or says why it couldn't.

    Returns whether it was written, so the switch dialog's "Save and switch" doesn't switch
    away from work that never reached storage.

    The message is queued rather than drawn: every caller ends in a rerun, and anything
    written just before one never reaches the screen.
    """
    try:
        task = tasks_session.capture_task()
        saved = tasks_db.save_task(user_id, task)
    except TaskError as error:
        logger.exception("Could not save the task for user %s.", user_id)
        st.error(str(error), icon=":material/error:")
        return False

    tasks_session.set_task_id(saved.task_id)
    tasks_session.mark_saved(saved)
    tasks_session.queue_flash(f"Saved “{saved.display_name()}”.")
    return True


def _render_task_gate(user_id: int) -> None:
    """The first and only question, when no Task is open: open one, or start one.

    The page used to open straight onto an empty name box, a Save button and four views, which
    left the user to work out from the controls alone whether they were editing something
    saved or building something new. Now that choice is made once, on a screen with nothing
    else on it, and everything after it is about the Task they picked.
    """
    st.markdown("#### Open a task, or start a new one")

    saved = _saved_tasks(user_id)
    open_column, new_column = st.columns(2, border=True, gap="medium")

    with open_column:
        st.markdown("**Open a saved task**")
        if saved is None:
            st.caption("Your saved tasks couldn't be listed — the message above says why.")
        elif not saved:
            st.caption("You haven't saved any tasks yet. Start one on the right.")
        else:
            st.caption(
                "A task brings back its recipe — the persona, the report items, the criteria "
                "and the report's arrangement — but not their results, which described data "
                "that is no longer loaded."
            )
            _render_saved_task_picker(user_id, saved)

    with new_column:
        st.markdown("**Start a new task**")
        st.caption(
            "A blank recipe. Any files you have already loaded stay loaded — writing a second "
            "task against them is the usual reason to do this."
        )
        typed = st.text_input(
            "Task name",
            key="tb_new_task_name",
            placeholder="e.g. Monthly salary review",
            help="What this task is called when you come back to open it. You can rename it later.",
        )
        if st.button(
            "Create task",
            key="tb_create_task",
            type="primary",
            icon=":material/add:",
            width="stretch",
            disabled=not typed.strip(),
            help="Start building this task. The report items, criteria and report on screen are cleared.",
        ):
            if _name_taken(user_id, typed):
                st.error(
                    f"You already have a task called “{typed.strip()}” — open it from the left, "
                    "or pick another name.",
                    icon=":material/error:",
                )
            else:
                tasks_session.start_new_task(typed)
                tasks_session.queue_flash(f"Started “{typed.strip()}”.")
                st.rerun(scope="app")


def _render_saved_task_picker(user_id: int, saved: list[dict]) -> None:
    """The gate's left half: choose a saved Task, see what it is, open or delete it.

    A select box rather than the scrolling list of cards this used to be. Cards read well at
    five and badly at fifty, and an account is free to save a hundred Tasks — the picker's
    type-to-filter makes the hundredth cost the same as the first. What the cards showed
    beyond the name (the description, the date) moves into the detail panel underneath, so
    choosing is still an informed decision.

    The two buttons are keyed once rather than once per Task: there is one of each now,
    acting on whichever row is selected.
    """
    chosen = saved_picker.select_saved(
        saved,
        key="tb_gate_task_pick",
        id_key="task_id",
        label="Saved tasks",
        placeholder="Choose a task…",
        help="Start typing to filter. Your most recently saved tasks are at the top.",
    )
    if chosen is None:
        st.caption("Pick one to see what it contains.")
        return

    task_id = chosen["task_id"]
    with st.container(border=True):
        saved_picker.render_detail(chosen)

        open_column, delete_column = st.columns([3, 1])
        with open_column:
            if st.button(
                "Open",
                key="tb_gate_open",
                type="primary",
                width="stretch",
                help="Open this task and start working on it.",
            ):
                try:
                    tasks_session.load_task(tasks_db.load_task(task_id, user_id))
                except TaskStorageError as error:
                    logger.exception("Could not load task %s.", task_id)
                    st.error(str(error), icon=":material/error:")
                    return
                tasks_session.queue_flash(f"Opened “{chosen['name']}”.")
                st.rerun(scope="app")
        with delete_column:
            if st.button(
                "",
                key="tb_gate_delete",
                icon=":material/delete:",
                width="stretch",
                help="Permanently delete this saved task.",
            ):
                tasks_session.open_dialog(
                    "confirm_delete",
                    {"user_id": user_id, "task_id": task_id, "name": chosen["name"]},
                )
                st.rerun(scope="app")


def _render_task_header(user_id: int) -> None:
    """Which Task is open, whether it is saved, and the three things you can do to it.

    A heading rather than a text box: the name is chosen at the gate and changed through
    Rename, so Save now means one thing only — write *this* task — instead of "write whatever
    the name box currently says", which quietly overwrote another Task if it said its name.
    """
    unsaved = tasks_session.has_unsaved_changes()
    title_column, save_column, rename_column, switch_column = st.columns(
        [4, 1.2, 1, 1.2], vertical_alignment="bottom"
    )

    with title_column:
        st.markdown(f"#### {tasks_session.task_name().strip() or UNTITLED_TASK}")
        if tasks_session.task_id() is None:
            st.caption(":orange[New — not saved yet]")
        elif unsaved:
            st.caption(":orange[Unsaved changes]")
        else:
            st.caption(":green[Saved]")

    with save_column:
        if st.button(
            "Save task",
            key="tb_save_task",
            icon=":material/save:",
            type="primary" if unsaved else "secondary",
            width="stretch",
            help=(
                "Store this whole setup — schema, links, column meanings, calculated columns, "
                "report items, criteria and the report's arrangement — so it can be run again "
                "against next month's files."
            ),
        ):
            _save_task(user_id)
            st.rerun(scope="app")

    with rename_column:
        if st.button(
            "Rename",
            key="tb_rename_task",
            icon=":material/edit:",
            width="stretch",
            help="Change what this task is called.",
        ):
            tasks_session.open_dialog("rename_task", {"user_id": user_id})
            st.rerun(scope="app")

    with switch_column:
        if st.button(
            "Switch task",
            key="tb_switch_task",
            icon=":material/swap_horiz:",
            width="stretch",
            help="Go back to the task list to open a different task, or start a new one.",
        ):
            if unsaved:
                tasks_session.open_dialog("confirm_switch", {"user_id": user_id})
            else:
                tasks_session.close_task()
            st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------------------


def _render_task_details() -> None:
    """The persona and the description, at the foot of Setup.

    Here rather than in the task bar because they are written once, when the Task is being
    designed, and re-read rarely — while the bar is pressed every time it is saved. And here
    rather than on Report-Items or Checks because **both** of those read the persona
    (requirement 7.2), so it belongs to neither of them.
    """
    st.markdown("#### About this task")

    # `persist_state="session"` on both, for the reason the view control 130 lines up carries
    # it: these widgets are only created on Setup, and Streamlit drops a widget's value on any
    # run where the widget is not rendered. Without it, Save pressed from Checks or Report
    # stores an empty persona, and the values `consume_pending_fields` writes on Open are gone
    # again before Setup ever draws them.
    st.text_area(
        "Persona and context",
        key=tasks_session.TB_PERSONA_KEY,
        height=120,
        placeholder=PERSONA_PLACEHOLDER,
        persist_state="session",
        help=(
            "Who the AI is acting as, plus any background it should apply. Used when it writes "
            "the SQL, the comments and the criteria remarks — set once for the whole task."
        ),
    )

    st.text_input(
        "Description (optional)",
        key=tasks_session.TB_DESCRIPTION_KEY,
        placeholder="e.g. Run on the 1st of every month, after payroll is closed.",
        persist_state="session",
        help="A note to yourself. Shown beside the task's name when you come back to open it.",
    )


def _render_start_over() -> None:
    """Discards this session's data and everything written against it.

    A saved Task in SQLite is untouched: it is a recipe, and the whole point of one is that it
    outlives the data it was written for. It clears the Task's name and persona along with the
    rest, though, so it also closes the Task — the next screen is the gate, where the user
    says which Task they are working on now.
    """
    if st.button(
        "Start over",
        key="tb_start_over_button",
        icon=":material/refresh:",
        help="Discard every loaded table, link, description, report item, criteria and pinned report item.",
    ):
        engine_session.queue_start_over()
        # Cleared here rather than inside `reset_engine`, so `engine/` keeps no dependency on
        # the packages above it. Ordered so the report goes while it is still the active one.
        dashboard_session.reset_dashboard()
        report_items_session.reset_report_items()
        checks_session.reset_checks()
        chat_session.reset_chat()
        tasks_session.reset_task_builder()
        st.rerun(scope="app")


def _go_to_report_items() -> None:
    st.session_state[PENDING_VIEW_KEY] = "Report-Items"


EMPTY_POOL = report_view.EmptyPool(
    message=(
        "Nothing pinned yet. Add a report item, run it, then press **Pin to report** — or "
        "save a criteria's result in Checks."
    ),
    button_label="Go to Report-Items",
    on_click=_go_to_report_items,
    icon=":material/summarize:",
    button_help="Build an item, then pin it to bring it back here.",
)


def _render_no_data(message: str, key: str) -> None:
    """What a view that needs tables says when there are none, with the way back.

    Setup is where the fix is, and with Step 1 off this screen the message has to carry its
    own route there rather than say "scroll up".
    """
    st.info(message, icon=":material/upload_file:")
    if st.button(
        "Go to Setup",
        key=key,
        icon=":material/upload_file:",
        help="Open the Setup view, where you upload files and manage what's loaded.",
    ):
        st.session_state[PENDING_VIEW_KEY] = "Setup"
        st.rerun(scope="app")


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------


if profile is not None:
    render_sidebar(profile)
    user_id = st.session_state["user_id"]

    st.subheader("🧩 Task Builder")
    st.write(
        ":blue[**Record a whole analysis once — the setup, the report items, the checks and "
        "the report — and save it to run again on next month's files.**]"
    )

    # The whole reason `dashboard.session` has an active report key. Said at the top of the
    # run, before anything below can pin, so every producer on this page lands in the Task's
    # report rather than the session Dashboard.
    dashboard_session.use_report(tasks_session.TB_REPORT_KEY)

    # A container that is always here and usually empty. Writing the message straight onto the
    # page would make every element below it sit one position lower on the runs that have
    # something to say, which is how the browser ends up showing both positions at once.
    with st.container(key=FLASH_SLOT):
        flash = tasks_session.consume_flash()
        if flash:
            st.success(flash, icon=":material/check_circle:")

    # Emitted on every run, whatever is showing. The rule only bites when the container it
    # names exists, and a stylesheet that comes and goes is one more element moving the page
    # around underneath itself.
    st.html(f"<style>.st-key-{HIDDEN_UPLOAD_MOUNT} {{ display: none; }}</style>")

    # Applied before any expander or the view toggle is created, since Streamlit forbids
    # writing a widget's own key once it exists this run.
    engine_session.consume_step_state()
    # The name, description and persona a just-opened Task queued. Under the same rule, and
    # before `_render_task_details` creates the boxes they go into.
    tasks_session.consume_pending_fields()
    if PENDING_VIEW_KEY in st.session_state:
        st.session_state[VIEW_KEY] = st.session_state.pop(PENDING_VIEW_KEY)

    if not tasks_session.is_task_open():
        # One container for the whole gate, matching the one the open Task draws into. The two
        # screens then occupy the same single position on the page, so switching replaces that
        # subtree outright instead of leaving the other screen's taller half behind.
        with st.container(key=GATE_BODY):
            # The upload widgets are mounted even here. A widget that stops being rendered
            # stops reporting its value, and `sync_tables` would then drop every loaded table —
            # so a trip back to the gate to start a second task against the same files would
            # arrive with the files gone. Same hidden container the non-Setup views use.
            with st.container(key=HIDDEN_UPLOAD_MOUNT):
                setup_view.mount_upload()
            _render_pending_task_dialog()
            _render_task_gate(user_id)
        st.stop()

    # The open Task's half of the page, in the container the gate matches. Everything
    # below — the header, the view control, Step 1 and whichever view is showing — hangs
    # off this one block, so leaving for the gate takes all of it with it.
    with st.container(key=TASK_BODY):
        loaded_tables = list(engine_session.get_tables().values())
        # Called for the side effect: it puts the statements key in session state from the
        # first run regardless of the view, rather than leaving its first read to wherever a
        # column step happens to occur.
        engine_session.get_statements()

        upload_summary = (
            f"{len(loaded_tables)} table(s) — " + ", ".join(table.table_name for table in loaded_tables)
            if loaded_tables
            else ""
        )
        tables_before = set(engine_session.get_tables())

        _render_task_header(user_id)

        view = st.segmented_control(
            "View",
            options=VIEWS,
            key=VIEW_KEY,
            default=VIEWS[0],
            required=True,
            label_visibility="collapsed",
            # Widget values are dropped when the widget stops being rendered, which leaving the
            # page does — so without this, coming back lands on Setup with the work apparently
            # gone.
            persist_state="session",
            help=(
                "Setup: files, links and column meanings. Report-Items: what the report shows. "
                "Checks: business rules and their exceptions. Report: arrange and download it."
            ),
            width="stretch",
        )

        # Step 1 on Setup, the hidden mount everywhere else — both inside one slot that is on the
        # page either way, so the swap happens among the slot's children rather than changing what
        # occupies this position.
        with st.container(key=UPLOAD_SLOT):
            if view == "Setup":
                # A **constant**, like every `expanded=` in this app: Streamlit re-applies the
                # argument whenever its value changes, so anything dynamic would force the step
                # shut the instant a file loaded and keep it shut.
                with st.expander(
                    setup_view.step_label(1, "Your data", upload_summary, bool(loaded_tables)),
                    key=engine_session.STEP_UPLOAD,
                    on_change="rerun",
                    expanded=True,
                    icon=":material/upload_file:",
                ) as upload_step:
                    # Ungated on `upload_step.open`: a collapsed step must still instantiate the
                    # uploader. Only the per-table previews below are gated — they are the
                    # expensive part, and they hold no state.
                    loaded_tables = setup_view.mount_upload()
                    if upload_step.open:
                        setup_view.render_upload_report(loaded_tables)
            else:
                # Mounted out of sight so the widgets keep reporting their files. Not dead UI —
                # deleting this container drops the user's tables the first time they switch views.
                # There is no native way to mount a widget without showing it, and mounting it is
                # not optional; the page's one stylesheet, emitted above, hides it.
                with st.container(key=HIDDEN_UPLOAD_MOUNT):
                    loaded_tables = setup_view.mount_upload()

        if set(engine_session.get_tables()) != tables_before:
            # The labels above were rendered from the table set as it stood at the top of this
            # run, which the upload has just changed. One rerun brings them back in step.
            if engine_session.get_tables():
                engine_session.collapse_once(engine_session.STEP_UPLOAD)
            st.rerun(scope="app")

        _render_pending_dialog()

        if view == "Setup":
            setup_view.render_setup_steps(user_id, loaded_tables)
            st.divider()
            _render_task_details()
            if loaded_tables:
                st.divider()
                _render_start_over()

        elif view == "Report-Items":
            if not loaded_tables:
                _render_no_data(
                    "Upload your data in Setup first — every report item is written against the "
                    "columns you load.",
                    "tb_items_go_to_setup_button",
                )
            else:
                report_items_view.render_report_items(user_id, tasks_session.persona())

        elif view == "Checks":
            if not loaded_tables:
                _render_no_data(
                    "Upload your data in Setup first — criteria are written against the columns "
                    "you load.",
                    "tb_checks_go_to_setup_button",
                )
            else:
                render_checks(
                    user_id,
                    # A Task *is* the saved setup, so a second Save set / Load set would be two
                    # answers to one question — these criteria are saved with the Task.
                    show_set_bar=False,
                    # A drafted email answers *this month's* exceptions; a Task is a recipe, and a
                    # saved draft about rows that no longer exist is worse than no draft.
                    show_actions=False,
                    # Requirement 7.2: one persona for the whole Task, set on Setup.
                    persona=tasks_session.persona(),
                    report_hint=(
                        "Write your rules here. Saved results go to this task's **Report** view, "
                        "where the report is arranged and downloaded."
                    ),
                )

        elif view == "Report":
            report_view.render_report_workspace(
                dashboard_session.get_report(), empty_pool=EMPTY_POOL
            )
