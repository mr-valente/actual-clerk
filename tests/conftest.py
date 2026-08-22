from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest

from actual_clerk.config import Settings, SettingsManager
from actual_clerk.db import Database

TODAY = datetime.date(2026, 8, 21)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No test may be influenced by the developer's own container variables."""
    for name in list(os.environ):
        if name.startswith(("CLERK_", "ACTUAL_")) or name == "TZ":
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def database(data_dir: Path) -> Database:
    database = Database(data_dir / "clerk.db")
    database.initialize()
    return database


@pytest.fixture
def settings_manager(database: Database) -> SettingsManager:
    return SettingsManager(database)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        actual_url="http://actual:5006",
        actual_password="secret",
        actual_budget_id="budget-1",
        openai_base_url="http://model:11434/v1",
        model="test-model",
        timezone="UTC",
    )


@pytest.fixture
def today() -> datetime.date:
    return TODAY
