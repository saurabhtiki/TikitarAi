import logging

import pandas as pd
import streamlit as st

from auth.db import get_user_by_id, update_own_password, update_own_profile
from auth.exceptions import AuthDatabaseError, InvalidPasswordError, PhotoProcessingError
from auth.photos import save_user_photo
from llm.client import LLMConnectionError, test_connection
from llm.db import create_profile, delete_profile, list_profiles, set_light_model, unset_light_model, update_profile
from llm.exceptions import LLMDatabaseError
from sidebar import photo_data_uri, render_sidebar

logger = logging.getLogger(__name__)

PROVIDER_TYPES = ["local", "cloud", "custom"]


def _queue_llm_table_reset() -> None:
    """Marks the LLM profile table's row selection to be cleared on the next run.
    Needed after any create/update/delete, since the table re-sorts by nickname and
    may shrink or reorder — see the same pattern in app_pages/user_management.py.

    The actual session_state["settings_llm_table"] write happens just before that
    widget is instantiated in _render_llm_section, not here -- Streamlit forbids
    writing to a widget's own session_state key in the same run after that widget
    has already been created, and these handlers can run after the dataframe has
    already rendered earlier in the script (e.g. the light-model buttons sit right
    below the dataframe in the same run).
    """
    st.session_state["settings_llm_table_reset_pending"] = True


try:
    profile = get_user_by_id(st.session_state["user_id"])
except AuthDatabaseError:
    logger.exception("Database error while loading profile for user_id %s.", st.session_state.get("user_id"))
    st.error("We couldn't load your profile. Please try logging in again.")
    profile = None


def _handle_save_profile(user_id: int, name: str, uploaded_photo) -> None:
    if not name:
        st.warning("Name is required.", icon=":material/error:")
        return

    photo_path = None
    if uploaded_photo is not None:
        try:
            photo_path = save_user_photo(user_id, uploaded_photo)
        except PhotoProcessingError as error:
            st.error(str(error))
            return

    try:
        update_own_profile(user_id, name, photo_path)
    except AuthDatabaseError:
        logger.exception("Database error while updating profile for user_id %s.", user_id)
        st.error("We couldn't save your profile. Please try again shortly.")
    else:
        st.success("Profile updated.", icon=":material/check_circle:")
        st.rerun()


def _render_profile_section(current_profile: dict) -> None:
    st.write(":blue[**Update your display name and profile photo.**]")

    data_uri = photo_data_uri(current_profile["photo_path"]) if current_profile.get("photo_path") else None
    if data_uri:
        st.html(f'<img src="{data_uri}" style="width:96px;height:96px;border-radius:50%;object-fit:cover;">')

    name = st.text_input(
        "Name",
        value=current_profile["name"],
        key="settings_profile_name",
        help="Your display name, shown in the sidebar and to other users.",
    )
    uploaded_photo = st.file_uploader(
        "Profile photo",
        type=["png", "jpg", "jpeg", "webp"],
        key="settings_profile_photo",
        help="Any image works — it's automatically cropped to a circle and resized.",
    )
    if st.button(
        "Save profile",
        key="settings_save_profile_button",
        icon=":material/save:",
        help="Save your name and photo.",type="primary"
    ):
        _handle_save_profile(current_profile["user_id"], name, uploaded_photo)


def _handle_change_password(user_id: int, current_password: str, new_password: str, confirm_password: str) -> None:
    if not current_password or not new_password or not confirm_password:
        st.warning("All three password fields are required.", icon=":material/error:")
        return
    if new_password != confirm_password:
        st.warning("New password and confirmation don't match.", icon=":material/error:")
        return
    try:
        update_own_password(user_id, current_password, new_password)
    except InvalidPasswordError as error:
        st.error(str(error))
    except ValueError as error:
        st.error(str(error))
    except AuthDatabaseError:
        logger.exception("Database error while changing password for user_id %s.", user_id)
        st.error("We couldn't change your password. Please try again shortly.")
    else:
        st.success("Password changed.", icon=":material/check_circle:")


def _render_password_section(current_profile: dict) -> None:
    st.write(":blue[**Change your own password. This does not affect any other account.**]")
    with st.form("settings_password_form"):
        current_password = st.text_input(
            "Current password",
            type="password",
            key="settings_current_password",
            help="Your existing password.",
        )
        new_password = st.text_input(
            "New password",
            type="password",
            key="settings_new_password",
            help="Choose a new password.",
        )
        confirm_password = st.text_input(
            "Confirm new password",
            type="password",
            key="settings_confirm_password",
            help="Re-enter the new password.",
        )
        submitted = st.form_submit_button(
            "Change password",
            icon=":material/lock_reset:",
            help="Save your new password.",type="primary"
        )
    if submitted:
        _handle_change_password(current_profile["user_id"], current_password, new_password, confirm_password)


@st.dialog("Add LLM provider profile")
def _open_add_profile_dialog(user_id: int) -> None:
    provider_type = st.segmented_control(
        "Provider type",
        options=PROVIDER_TYPES,
        key="settings_add_provider_type",
        default="cloud",
        required=True,
        help="Local providers (LM Studio/Ollama) need no API key.",
    )
    with st.form("settings_add_profile_form"):
        nickname = st.text_input(
            "Nickname", key="settings_add_nickname", help="A name to recognize this profile by, e.g. 'My OpenAI'."
        )
        base_url = st.text_input(
            "Base URL",
            key="settings_add_base_url",
            help="Required for local/custom providers; optional for cloud providers.",
        )
        api_key = None
        if provider_type != "local":
            api_key = st.text_input(
                "API key",
                type="password",
                key="settings_add_api_key",
                help="Stored encrypted. Leave blank if this provider doesn't need one.",
            )
        default_model = st.text_input(
            "Default model", key="settings_add_default_model", help="Free-text model name, e.g. 'gpt-4o-mini'."
        )
        submitted = st.form_submit_button("Save", icon=":material/save:", help="Create this profile.",type="primary")

    if submitted:
        _handle_create_profile(user_id, nickname, provider_type, base_url, api_key, default_model)


def _handle_create_profile(
    user_id: int, nickname: str, provider_type: str, base_url: str, api_key: str | None, default_model: str
) -> None:
    if not nickname or not default_model:
        st.warning("Nickname and default model are required.", icon=":material/error:")
        return
    if provider_type in ("local", "custom") and not base_url:
        st.warning("Base URL is required for local and custom providers.", icon=":material/error:")
        return
    try:
        create_profile(user_id, nickname, provider_type, base_url or None, api_key or None, default_model)
    except LLMDatabaseError:
        logger.exception("Database error while creating an LLM profile for user_id %s.", user_id)
        st.error("We couldn't save this profile. Please try again shortly.")
    else:
        _queue_llm_table_reset()
        st.rerun()


@st.dialog("Edit LLM provider profile")
def _open_edit_profile_dialog(user_id: int, profile_id: int) -> None:
    try:
        current = next((p for p in list_profiles(user_id) if p["profile_id"] == profile_id), None)
    except LLMDatabaseError:
        logger.exception("Database error while loading LLM profile %s for user_id %s.", profile_id, user_id)
        st.error("We couldn't load this profile's details.")
        return
    if current is None:
        st.error("This profile no longer exists.")
        return

    # Widget keys are suffixed with profile_id so each profile's dialog always shows that
    # profile's own values — a shared key would keep showing whichever profile was edited
    # first, since Streamlit only applies value=/default= the first time a key is created.
    provider_type = st.segmented_control(
        "Provider type",
        options=PROVIDER_TYPES,
        key=f"settings_edit_provider_type_{profile_id}",
        default=current["provider_type"],
        required=True,
        help="Local providers (LM Studio/Ollama) need no API key.",
    )
    with st.form("settings_edit_profile_form"):
        nickname = st.text_input(
            "Nickname",
            value=current["nickname"],
            key=f"settings_edit_nickname_{profile_id}",
            help="A name to recognize this profile by.",
        )
        base_url = st.text_input(
            "Base URL",
            value=current["base_url"] or "",
            key=f"settings_edit_base_url_{profile_id}",
            help="Required for local/custom providers; optional for cloud providers.",
        )
        api_key = None
        clear_key = False
        if provider_type != "local":
            api_key = st.text_input(
                "New API key",
                type="password",
                key=f"settings_edit_api_key_{profile_id}",
                help="Leave blank to keep the currently saved key.",
            )
            clear_key = st.checkbox(
                "Remove saved key",
                key=f"settings_edit_clear_key_{profile_id}",
                help="Clears the stored API key for this profile.",
            )
        default_model = st.text_input(
            "Default model",
            value=current["default_model"],
            key=f"settings_edit_default_model_{profile_id}",
            help="Free-text model name.",
        )
        submitted = st.form_submit_button("Save", icon=":material/save:", help="Save changes.",type="primary")

    if submitted:
        api_key_update = "" if clear_key else (api_key or None)
        _handle_update_profile(user_id, profile_id, nickname, provider_type, base_url, api_key_update, default_model)


def _handle_update_profile(
    user_id: int,
    profile_id: int,
    nickname: str,
    provider_type: str,
    base_url: str,
    api_key_update: str | None,
    default_model: str,
) -> None:
    if not nickname or not default_model:
        st.warning("Nickname and default model are required.", icon=":material/error:")
        return
    if provider_type in ("local", "custom") and not base_url:
        st.warning("Base URL is required for local and custom providers.", icon=":material/error:")
        return
    try:
        update_profile(profile_id, user_id, nickname, provider_type, base_url or None, api_key_update, default_model)
    except LLMDatabaseError:
        logger.exception("Database error while updating LLM profile %s for user_id %s.", profile_id, user_id)
        st.error("We couldn't save these changes. Please try again shortly.")
    else:
        _queue_llm_table_reset()
        st.rerun()


@st.dialog("Delete LLM provider profile")
def _open_delete_profile_dialog(user_id: int, profile_id: int) -> None:
    st.warning("Delete this LLM provider profile? This cannot be undone.", icon=":material/warning:")
    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        if st.button(
            "Confirm delete",
            key="settings_confirm_delete_profile_button",
            icon=":material/delete_forever:",
            help="Permanently delete this profile.",type="primary"
        ):
            _handle_delete_profile(user_id, profile_id)
    with cancel_col:
        if st.button("Cancel", key="settings_cancel_delete_profile_button", help="Close without deleting.",type="primary"):
            st.rerun()


def _handle_delete_profile(user_id: int, profile_id: int) -> None:
    try:
        delete_profile(profile_id, user_id)
    except LLMDatabaseError:
        logger.exception("Database error while deleting LLM profile %s for user_id %s.", profile_id, user_id)
        st.error("We couldn't delete this profile. Please try again shortly.")
    else:
        _queue_llm_table_reset()
        st.rerun()


def _handle_set_light_model(user_id: int, profile_id: int) -> None:
    try:
        set_light_model(profile_id, user_id)
    except LLMDatabaseError:
        logger.exception("Database error while setting light model %s for user_id %s.", profile_id, user_id)
        st.error("We couldn't update your light model. Please try again shortly.")
    else:
        _queue_llm_table_reset()
        st.rerun()


def _handle_unset_light_model(user_id: int, profile_id: int) -> None:
    try:
        unset_light_model(profile_id, user_id)
    except LLMDatabaseError:
        logger.exception("Database error while unsetting light model %s for user_id %s.", profile_id, user_id)
        st.error("We couldn't update your light model. Please try again shortly.")
    else:
        _queue_llm_table_reset()
        st.rerun()


def _handle_test_connection(llm_profile: dict) -> None:
    """Fires one minimal request against a saved profile (requirement 3.4).

    Reports the provider's own failure text — "Incorrect API key provided", "Connection
    error" — because that names what to fix, which a generic message never does.
    """
    with st.spinner("Testing…"):
        try:
            succeeded, message = test_connection(llm_profile)
        except LLMConnectionError as error:
            succeeded, message = False, str(error)

    if succeeded:
        st.success(message, icon=":material/check_circle:")
    else:
        st.error(message, icon=":material/error:")


def _render_llm_section(current_profile: dict) -> None:
    user_id = current_profile["user_id"]
    st.write(""":blue[**Manage your saved LLM connection profiles.
        " mark one as your light model — 
        "used automatically for small, fast auxiliary tasks.**]"""
    )

    if st.button(
        "Add new",
        key="settings_add_profile_button",
        icon=":material/add_circle:",
        help="Create a new LLM provider profile.",type="primary"
    ):
        _open_add_profile_dialog(user_id)

    try:
        profiles = list_profiles(user_id)
    except LLMDatabaseError:
        logger.exception("Database error while loading LLM profiles for user_id %s.", user_id)
        st.error("We couldn't load your LLM provider profiles. Please try again shortly.")
        return

    if not profiles:
        st.info("No LLM provider profiles saved yet.", icon=":material/info:")
        return

    profiles_df = pd.DataFrame(profiles)
    profiles_df["light_model_label"] = profiles_df["is_light_model"].map({1: "Yes", 0: ""})

    if st.session_state.pop("settings_llm_table_reset_pending", False):
        st.session_state["settings_selected_profile_id"] = None
        st.session_state["settings_llm_table"] = {"selection": {"rows": [], "columns": []}}

    st.caption("Select a row to edit, delete, or set/unset it as your light model.")
    selection = st.dataframe(
        profiles_df,
        key="settings_llm_table",
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "profile_id": None,
            "user_id": None,
            "api_key_encrypted": None,
            "is_light_model": None,
            "created_at": None,
            "nickname": "Nickname",
            "provider_type": st.column_config.TextColumn("Type", help="local / cloud / custom"),
            "base_url": "Base URL",
            "default_model": "Model",
            "light_model_label": st.column_config.TextColumn("Light model", help="Your designated light model."),
        },
    )

    selected_rows = [row for row in selection["selection"]["rows"] if row < len(profiles_df)]
    if selected_rows:
        selected_row = profiles_df.iloc[selected_rows[0]]
        selected_profile_id = int(selected_row["profile_id"])
        st.session_state["settings_selected_profile_id"] = selected_profile_id

        edit_col, delete_col, light_col, test_col = st.columns(4)
        with test_col:
            if st.button(
                "Test connection",
                key="settings_test_connection_button",
                icon=":material/network_check:",
                help="Send one tiny request and report whether the provider answered.",
                type="primary",
            ):
                # Not selected_row.to_dict(): pandas silently turns a NULL
                # api_key_encrypted (normal for local providers) into a float
                # NaN, which then crashes llm.crypto.decrypt_api_key. Re-fetch
                # the real profile dict instead, same as Edit/Delete below.
                selected_profile = next(
                    (row for row in profiles if row["profile_id"] == selected_profile_id), None
                )
                if selected_profile is not None:
                    _handle_test_connection(selected_profile)
        with edit_col:
            if st.button(
                "Edit", key="settings_edit_profile_button", icon=":material/edit:", help="Change this profile's details.",type="primary"
            ):
                _open_edit_profile_dialog(user_id, selected_profile_id)
        with delete_col:
            if st.button(
                "Delete",
                key="settings_delete_profile_button",
                icon=":material/delete:",
                help="Permanently remove this profile.",type="primary"
            ):
                _open_delete_profile_dialog(user_id, selected_profile_id)
        with light_col:
            if selected_row["is_light_model"]:
                if st.button(
                    "Unset light model",
                    key="settings_unset_light_button",
                    icon=":material/star:",
                    help="Stop using this profile as your light model.",type="primary"
                ):
                    _handle_unset_light_model(user_id, selected_profile_id)
            else:
                if st.button(
                    "Set as light model",
                    key="settings_set_light_button",
                    icon=":material/star_border:",
                    help="Use this profile automatically for lightweight auxiliary tasks.",
                    type="primary"
                ):
                    _handle_set_light_model(user_id, selected_profile_id)
    else:
        st.session_state["settings_selected_profile_id"] = None


if profile is not None:
    render_sidebar(profile)

    st.subheader("⚙️Settings")

    section = st.segmented_control(
        "Section",
        options=["Profile", "Password", "LLM providers"],
        key="settings_section",
        default="Profile",
        required=True,
        help="Choose which settings to manage.",
        label_visibility="collapsed",
    )

    if section == "Profile":
        _render_profile_section(profile)
    elif section == "Password":
        _render_password_section(profile)
    elif section == "LLM providers":
        _render_llm_section(profile)
