"""Keep unit tests isolated from developer configuration and remote services."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.app_config_source import reset_app_config_cache
from app.core.settings import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Never read a real dotenv file or enable Azure integrations from test environment."""
    for name in tuple(os.environ):
        if name.startswith(
            ("APP_CONFIG_", "FOUNDRY_", "AZURE_AI_", "APPLICATIONINSIGHTS_", "STORY1_NATIVE_")
        ) or name in {
            "PORT",
            "ENVIRONMENT",
            "SUPERVISOR_PORT",
            "RESEARCH_PORT",
            "DATA_ANALYSIS_PORT",
            "TOOLBOX_ENDPOINT",
            "POC_APPLICATIONINSIGHTS_CONNECTION_STRING",
            "POC_APPLICATIONINSIGHTS_AUTH_MODE",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("POC_ENV_FILE", str(tmp_path / ".env"))
    get_settings.cache_clear()
    reset_app_config_cache()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()
    reset_app_config_cache()
