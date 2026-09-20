"""Which data folder the app picks, based on the APP_ENV setting.

Production must be asked for explicitly. Anything else — including a machine that sets
nothing at all — lands on the testing folder, so real data is never touched by accident.
"""

from pathlib import Path

import pytest

from utils.env import get_app_env, get_data_dir


@pytest.fixture(autouse=True)
def clear_app_env(monkeypatch):
    """Start every test with no APP_ENV set, whatever the machine has configured."""
    monkeypatch.delenv("APP_ENV", raising=False)


def test_unset_app_env_uses_testing_folder():
    assert get_data_dir() == Path("data_testing")


def test_production_uses_real_data_folder(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert get_data_dir() == Path("data")


def test_development_uses_testing_folder(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    assert get_data_dir() == Path("data_testing")


def test_production_is_read_case_and_space_insensitively(monkeypatch):
    monkeypatch.setenv("APP_ENV", "  Production  ")
    assert get_data_dir() == Path("data")


def test_blank_app_env_uses_testing_folder(monkeypatch):
    monkeypatch.setenv("APP_ENV", "   ")
    assert get_app_env() == ""
    assert get_data_dir() == Path("data_testing")
