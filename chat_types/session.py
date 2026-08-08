"""Session state for chat types — the only module in `chat_types/` that imports Streamlit.

Same convention as `checks/session.py` and `engine/session.py`: everything else in the
package stays pure and testable without `AppTest`.

Two things live here. The **selection** — which chat type the user picked, held across a
trip to the Dashboard and back — and **applying** one, which is the moment the saved links
and descriptions become the live session's links and descriptions.

Applying is deliberately once-per-load rather than every rerun. The saved setup is a
starting point the user is free to edit in Steps 2 and 3, and re-applying it on the next
rerun would quietly undo every edit they just made.
"""

import logging

import streamlit as st

from chat_types.model import ChatType
from engine import dictionary, relationships
from engine import session as engine_session
from engine.exceptions import DataEngineError

logger = logging.getLogger(__name__)

CT_ACTIVE_KEY = "ct_active_chat_type"
CT_APPLIED_KEY = "ct_applied_signature"
CT_DISCARDED_KEY = "ct_discarded_tables"
CT_WARNINGS_KEY = "ct_apply_warnings"
CT_PROBLEM_KEY = "ct_problem_signature"
CT_PICKER_KEY = "ct_picker"

# What the picker calls the ad-hoc path. Selecting it is every earlier stage's behaviour,
# unchanged: upload anything, set it up by hand, save it as a chat type or don't.
NEW_CHAT_TYPE = "— New chat type —"


def active() -> ChatType | None:
    """The chat type currently selected, or None on the ad-hoc path."""
    return st.session_state.get(CT_ACTIVE_KEY)


def active_id() -> int | None:
    """The active chat type's id, for scoping a criteria set to it (requirement 6.6)."""
    chat_type = active()
    return chat_type.chat_type_id if chat_type is not None else None


def active_name() -> str:
    chat_type = active()
    return chat_type.display_name() if chat_type is not None else ""


def declared_types() -> dict[str, dict[str, str]]:
    """The saved column types, in the shape `engine.session.sync_tables` takes.

    Empty on the ad-hoc path, which is what makes that path plain detection.
    """
    chat_type = active()
    if chat_type is None:
        return {}
    return {table.table_name: table.types_by_column() for table in chat_type.tables}


def select(chat_type: ChatType | None) -> None:
    """Makes this the active chat type, and forgets everything about the last one.

    The load outcomes go because they describe files checked against a *different* saved
    shape, and the applied marker goes so the new setup is applied on the next run.
    """
    if chat_type is None:
        st.session_state.pop(CT_ACTIVE_KEY, None)
    else:
        st.session_state[CT_ACTIVE_KEY] = chat_type

    forget_upload()
    # Anything already loaded was typed by detection, or by a *different* chat type, and
    # the raw text it was typed from is gone. Dropping it makes the next run read the files
    # again under the new saved types — without this, picking a chat type after uploading
    # would report a clean match over a table it never actually touched.
    engine_session.reload_uploaded_tables()
    logger.info("Active chat type is now '%s'.", chat_type.display_name() if chat_type else "none")


def forget_upload() -> None:
    """Forgets everything learned about the files currently loaded.

    Called when the chat type changes and when Start over discards the tables. Everything
    dropped here describes one upload checked against one setup: which tables were
    discarded, what the load found, and whether the setup has been applied yet. The
    selection itself survives Start over on purpose — discarding this month's files to
    upload next month's against the same chat type is the normal way to use one.
    """
    for key in (CT_APPLIED_KEY, CT_DISCARDED_KEY, CT_WARNINGS_KEY, CT_PROBLEM_KEY):
        st.session_state.pop(key, None)
    engine_session.clear_load_outcomes()


def problem_signature() -> str:
    """The problems Step 1 was last re-opened for. See the page's `_open_step_one_on_problems`."""
    return st.session_state.get(CT_PROBLEM_KEY, "")


def note_problem_signature(signature: str) -> None:
    st.session_state[CT_PROBLEM_KEY] = signature


def note_discarded(table_names: list[str]) -> None:
    """Remembers tables dropped for not being part of the chat type.

    They are gone from the engine by the time this is read, so the report can no longer
    call them extra — and a file silently vanishing from the upload list is exactly the
    sort of thing that has to be said out loud.
    """
    known = st.session_state.setdefault(CT_DISCARDED_KEY, [])
    known.extend(name for name in table_names if name not in known)


def discarded() -> list[str]:
    return list(st.session_state.get(CT_DISCARDED_KEY, []))


def reset_chat_types() -> None:
    """Clears the selection outright, along with everything about the current upload.

    Not what Start over does — see `forget_upload`. This is the full teardown, for a caller
    that wants no chat type at all. The saved rows in SQLite are untouched either way:
    being able to run the same setup against a different file next month is the entire
    point of saving one.
    """
    st.session_state.pop(CT_ACTIVE_KEY, None)
    forget_upload()


# --------------------------------------------------------------------------------------
# Applying a saved setup
# --------------------------------------------------------------------------------------


def _signature(chat_type: ChatType, table_names: list[str]) -> str:
    """What "already applied" means: this chat type, against these tables.

    Keyed on the tables too, so uploading next month's second file applies the setup to it
    rather than leaving it linked to nothing.
    """
    return f"{chat_type.chat_type_id}:{chat_type.display_name()}:{','.join(sorted(table_names))}"


def needs_applying(chat_type: ChatType, table_names: list[str]) -> bool:
    return st.session_state.get(CT_APPLIED_KEY) != _signature(chat_type, table_names)


def apply_warnings() -> list[str]:
    """Whatever `apply_setup` had to say, still there after the rerun it triggered."""
    return list(st.session_state.get(CT_WARNINGS_KEY, []))


def apply_setup(chat_type: ChatType, table_names: list[str]) -> list[str]:
    """Restores the saved links and column descriptions onto the loaded tables.

    Only called once the upload has matched (`matching.MatchReport.ok`), so every table and
    column named here exists — bar links naming a table the chat type expects but that this
    upload didn't bring, which cannot happen for the same reason.

    Descriptions go through `engine.dictionary.build_dictionary`'s own `existing=` merge
    rather than being written over the grid: that merge already carries a description by
    `(table, column)` across a rebuild, so restoring is the same operation as surviving one,
    and a column the chat type describes but the table no longer has simply drops out.

    Warnings are stored rather than returned, and read back with `apply_warnings`. This
    runs on the way to a rerun, and anything painted on the run that applies the setup is
    destroyed by it — so a warning that the saved joins couldn't be applied would flash
    once and never be seen. Never raises: a setup that half-applies is still better than an
    upload the user can't do anything with, and the parts that failed are named.
    """
    warnings: list[str] = []
    loaded = {name.lower() for name in table_names}

    # Seeded before the rebuild, so the descriptions are already in place when
    # `refresh_dictionary` runs against the rebuilt schema below.
    engine_session.set_dictionary(
        dictionary.build_dictionary(
            engine_session.connection(),
            table_names,
            engine_session.semantic_types_by_table(),
            existing=chat_type.restored_dictionary(),
        )
    )

    links = [
        link
        for link in chat_type.relationships
        if link.child_table.lower() in loaded and link.parent_table.lower() in loaded
    ]
    skipped = len(chat_type.relationships) - len(links)
    if skipped:
        warnings.append(f"{skipped} saved link(s) were skipped — they join a table that isn't loaded.")

    if links:
        engine_session.set_relationships(links)
        try:
            clean = relationships.enforceable(engine_session.connection(), links)
            relationships.enforce(
                engine_session.connection(), table_names, clean, engine_session.get_statements()
            )
            if len(clean) != len(links):
                warnings.append(
                    f"{len(links) - len(clean)} saved link(s) have rows that don't match in this "
                    "file. They're still used for queries, just not enforced as constraints."
                )
        except DataEngineError as error:
            logger.exception("Could not enforce the saved links from chat type '%s'.", chat_type.display_name())
            warnings.append(f"The saved links couldn't be applied ({error}).")

    engine_session.bump_rebuild()
    engine_session.refresh_dictionary()
    st.session_state[CT_APPLIED_KEY] = _signature(chat_type, table_names)
    st.session_state[CT_WARNINGS_KEY] = warnings
    logger.info(
        "Applied chat type '%s': %d link(s), %d description(s).",
        chat_type.display_name(),
        len(links),
        len(chat_type.descriptions),
    )
    return warnings
