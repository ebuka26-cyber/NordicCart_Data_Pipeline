"""Shared pytest setup for tests/python.

Makes the package in python/ importable and provides the generated datasets once per
test session. Database tests live in test_master_data_seed_db.py and only run when
NORDICCART_DB_TESTS=1 is set (they create and drop their own databases).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "python"))

from nordiccart.master_data.generate import generate_master_data  # noqa: E402
from nordiccart.settings import load_settings  # noqa: E402


@pytest.fixture(scope="session")
def pilot_settings():
    return load_settings(profile="pilot")


@pytest.fixture(scope="session")
def full_settings():
    return load_settings(profile="full")


@pytest.fixture(scope="session")
def pilot_dataset(pilot_settings):
    return generate_master_data(pilot_settings)


@pytest.fixture(scope="session")
def full_dataset(full_settings):
    return generate_master_data(full_settings)
