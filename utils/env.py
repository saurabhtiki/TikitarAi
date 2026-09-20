"""Chooses which data folder the app writes to.

Production uses the real ``data/`` folder. Everything else (your laptop, a staging
copy of the app) uses ``data_testing/``, so a machine that forgets to configure
anything can never touch real data by accident.

Set ``APP_ENV = "production"`` in Streamlit Cloud's *Settings -> Secrets*, or as an
environment variable, to point the app at the real folder.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PRODUCTION_DATA_DIR = Path("data")
TESTING_DATA_DIR = Path("data_testing")
PRODUCTION_ENV_NAME = "production"


def get_app_env() -> str:
    """Return the configured environment name, lowercased.

    Looks in Streamlit secrets first, then the ``APP_ENV`` environment variable.
    Returns an empty string when nothing is set.
    """
    try:
        import streamlit as st

        secret_value = st.secrets.get("APP_ENV")
        if secret_value:
            return str(secret_value).strip().lower()
    except ImportError:
        logger.debug("Streamlit is not installed; reading APP_ENV from the environment.")
    except Exception as error:  # no secrets file, or secrets could not be read
        logger.debug("Could not read APP_ENV from Streamlit secrets: %s", error)

    return os.environ.get("APP_ENV", "").strip().lower()


def get_data_dir() -> Path:
    """Return the folder that holds the databases and uploaded files."""
    if get_app_env() == PRODUCTION_ENV_NAME:
        return PRODUCTION_DATA_DIR
    return TESTING_DATA_DIR
