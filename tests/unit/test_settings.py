"""Characterize adapted configuration precedence, bootstrap, and failure behavior."""

import os
from pathlib import Path

import pytest
from app.core import app_config_source as source
from app.core.settings import Settings, get_settings
from pydantic import Field, ValidationError
from pydantic_settings import SettingsConfigDict


def test_defaults_need_no_azure() -> None:
    settings = get_settings()
    assert settings.foundry_project_endpoint is None
    assert settings.agent_port("research") == 8089
    with pytest.raises(ValueError, match="Set FOUNDRY"):
        settings.require_model()


def test_dotenv_bootstrap_and_complex_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("APP_CONFIG_ENDPOINT=https://example.azconfig.io\nITEMS=[1]\nVALUE=dotenv\n", encoding="utf-8")
    seen: list[source.Bootstrap] = []

    def load(config: source.Bootstrap) -> dict[str, object]:
        seen.append(config)
        return {"items": "[2,3]", "value": "remote"}

    monkeypatch.setattr(source, "load_app_config", load)

    class LayeredSettings(source.AppConfigAwareSettings):
        model_config = SettingsConfigDict(env_file=path, extra="ignore")
        items: list[int] = Field(default_factory=list)
        value: str = "default"

    assert LayeredSettings().items == [2, 3]
    assert LayeredSettings().value == "remote"
    monkeypatch.setenv("VALUE", "environment")
    assert LayeredSettings().value == "environment"
    assert LayeredSettings(value="constructor").value == "constructor"
    assert seen[0].endpoint == "https://example.azconfig.io"
    assert "APP_CONFIG_ENDPOINT" not in os.environ


def test_process_bootstrap_overrides_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_CONFIG_ENDPOINT=https://local.azconfig.io\n", encoding="utf-8")
    monkeypatch.setenv("APP_CONFIG_ENDPOINT", "https://override.azconfig.io")
    assert source.bootstrap().endpoint == "https://override.azconfig.io"


def test_configured_source_failure_is_not_a_successful_default(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_config: source.Bootstrap) -> dict[str, object]:
        raise RuntimeError("Configured provider is unavailable")

    monkeypatch.setattr(source, "load_app_config", fail)
    with pytest.raises(RuntimeError, match="provider is unavailable"):
        get_settings()


def test_dotenv_works_after_changing_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("RESEARCH_PORT=9001\nFOUNDRY_PROJECT_ENDPOINT=\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert get_settings().agent_port("research") == 9001
    assert get_settings().foundry_project_endpoint is None


def test_blank_example_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "")
    assert get_settings().applicationinsights_connection_string is None
    assert get_settings().foundry_project_endpoint is None


def test_port_and_model_validation() -> None:
    with pytest.raises(ValidationError):
        Settings(port=65536)
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(foundry_project_endpoint="http://example.com", azure_ai_model_deployment_name="model").require_model()


def test_platform_port_overrides_local_port() -> None:
    assert Settings(port=8080).agent_port("data_analysis") == 8080
