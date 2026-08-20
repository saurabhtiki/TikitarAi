"""One select box for choosing something saved, shared by Task Builder and the Data Cleaner.

Both pages ask the same question — "which of your saved recipes?" — and both used to answer
it with a scrolling list of cards. Cards read well at five and badly at fifty: an account
with a hundred Tasks or cleaning templates gets a scroll with no way to search it. A select
box has type-to-filter for nothing, so the hundredth entry costs the same as the first.

A plain module, not a page: `app_pages/checks_view.py` and `app_pages/setup_view.py` are
the precedent. A page module cannot be imported — `st.Page` scripts run on import — so
anything two pages share has to live outside them.

What the picker deliberately does **not** do is act. It returns the chosen row and leaves
Open, Delete and every consequence to the caller, because the two callers differ there:
Task Builder opens a Task onto a screen of its own, while the Data Cleaner has to park the
choice and act on it further down the page, below `st.file_uploader`.
"""

import logging

import streamlit as st

logger = logging.getLogger(__name__)

# The stored timestamp is `datetime('now')` — "2026-08-20 09:41:07". The seconds are noise
# in a dropdown label, so the label keeps the date and the time to the minute.
_TIMESTAMP_LENGTH = 16

# The value behind the optional leading "none of them" entry. A sentinel rather than `None`,
# which `st.selectbox` reserves for *nothing is selected* — as an option value it can be
# offered but never chosen, since selecting it is indistinguishable from selecting nothing.
# Callers that read the widget's session_state key directly have to know this one.
NONE_OPTION = "__saved_picker_none__"


def format_timestamp(value: object) -> str:
    """A stored `updated_at` trimmed to the minute, or "" if it isn't one.

    Presentation only, so an unexpected value degrades to nothing rather than breaking the
    picker that is a user's only route to their own saved work.
    """
    text = str(value or "").strip()
    return text[:_TIMESTAMP_LENGTH] if text else ""


def row_label(row: dict, *, name_key: str = "name") -> str:
    """One saved thing as a single line: what it is called, and when it last changed.

    The date is in the label rather than only in the detail panel because it is what tells
    `Receivables` from `Receivables v2` in a list the user is scrolling past.
    """
    name = str(row.get(name_key) or "Untitled")
    saved = format_timestamp(row.get("updated_at"))
    return f"{name} — last saved {saved}" if saved else name


def select_saved(
    rows: list[dict],
    *,
    key: str,
    id_key: str,
    label: str,
    help: str,
    placeholder: str = "Choose one…",
    index: int | None = None,
    include_none: bool = False,
    none_label: str = "— None —",
    on_change=None,
) -> dict | None:
    """Draws the select box and returns the chosen row, or None when nothing is chosen.

    Args:
        rows: the listing rows, already in the order they should appear (both callers pass
            `list_*`'s output, which is `updated_at DESC`).
        key: the widget's `key=`. Unique per page, since both pages can be in one session.
        id_key: which column identifies a row — `task_id`, `template_id`.
        label: the select box's label.
        help: its tooltip. Required by the project's Streamlit conventions.
        placeholder: shown while nothing is selected.
        index: which option to open on, in the same terms `st.selectbox` uses — counting
            the `include_none` entry when there is one. None opens on the placeholder.
        include_none: adds a leading "none of them" entry, which the Data Cleaner needs and
            Task Builder does not: there, not choosing is simply not pressing Open.
        none_label: what that entry says.
        on_change: passed straight through, for a caller that has to react to the change
            rather than to the returned value.

    Returns None both when nothing is selected and when the "none" entry is — those mean the
    same thing to every caller so far, and a third return value nobody reads would be one
    more thing to get wrong. A caller reading the widget's key out of session_state itself
    sees `NONE_OPTION` there, not None.
    """
    by_id: dict[object, dict] = {}
    options: list[object] = []
    if include_none:
        options.append(NONE_OPTION)
    for row in rows:
        row_id = row.get(id_key)
        if row_id is None:
            logger.warning("Skipped a saved row with no %s in the picker.", id_key)
            continue
        by_id[row_id] = row
        options.append(row_id)

    def _face(option: object) -> str:
        if option == NONE_OPTION:
            return none_label
        return row_label(by_id[option])

    # `index` is only offered on the run that creates the widget. Passing it alongside a
    # value already in session_state sets a default that is then immediately overridden,
    # which Streamlit warns about — and the stored value is the one that should win, since
    # it is the user's own last choice.
    default = {} if key in st.session_state else {"index": index}

    chosen = st.selectbox(
        label,
        options=options,
        key=key,
        format_func=_face,
        placeholder=placeholder,
        help=help,
        on_change=on_change,
        **default,
    )
    return by_id.get(chosen) if chosen not in (None, NONE_OPTION) else None


def option_index(rows: list[dict], *, id_key: str, selected_id: object, include_none: bool) -> int | None:
    """Where `selected_id` sits in the option list `select_saved` will build.

    Kept beside the builder rather than worked out by each caller, because the two have to
    agree about the leading "none" entry — an off-by-one here silently opens the picker on
    the wrong saved recipe.
    """
    offset = 1 if include_none else 0
    if selected_id is None:
        return 0 if include_none else None
    for position, row in enumerate(row for row in rows if row.get(id_key) is not None):
        if row[id_key] == selected_id:
            return position + offset
    return 0 if include_none else None


def render_detail(row: dict, *, name_key: str = "name") -> None:
    """The panel under the picker: the description, and when it was last saved.

    A select box shows one line; the card it replaces showed three. This is where the other
    two go, so choosing is still an informed decision rather than a name-recognition test.
    """
    st.markdown(f"**{row.get(name_key) or 'Untitled'}**")
    description = str(row.get("description") or "").strip()
    st.caption(description or "No description was saved with this one.")
    saved = format_timestamp(row.get("updated_at"))
    if saved:
        st.caption(f"Last saved {saved}")
