"""Optional Azure App Configuration source layered above local dotenv defaults."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource

DEFAULT_ENV_FILE = Path(__file__).resolve().parents[4] / ".env"


def env_file() -> Path:
    """Resolve local configuration independently of the process working directory."""
    return Path(os.environ.get("POC_ENV_FILE", str(DEFAULT_ENV_FILE))).expanduser().resolve()


@dataclass(frozen=True)
class Bootstrap:
    """Non-secret location selectors needed before remote settings can load."""

    endpoint: str
    key_filter: str
    label: str | None


def bootstrap() -> Bootstrap:
    """Read process overrides over dotenv without changing the process environment."""
    local = dotenv_values(env_file())

    def read(name: str, default: str = "") -> str:
        return (os.environ.get(name, local.get(name)) or default).strip()

    return Bootstrap(
        endpoint=read("APP_CONFIG_ENDPOINT"),
        key_filter=read("APP_CONFIG_KEY_FILTER", "hosted-agents-poc:*"),
        label=read("APP_CONFIG_LABEL_FILTER") or read("ENVIRONMENT") or None,
    )


@lru_cache(maxsize=8)
def load_app_config(config: Bootstrap) -> dict[str, object]:
    """Load once per selector set; configured failures propagate to the caller."""
    if not config.endpoint:
        return {}
    try:
        from azure.appconfiguration.provider import AzureAppConfigurationKeyVaultOptions, SettingSelector, load
    except ImportError as error:
        raise RuntimeError("App Configuration requires: uv sync --extra appconfig") from error
    from azure.identity import DefaultAzureCredential

    if not config.key_filter.endswith("*") or "*" in config.key_filter[:-1]:
        raise ValueError("APP_CONFIG_KEY_FILTER must end in one wildcard, for example hosted-agents-poc:*")
    selectors = [SettingSelector(key_filter=config.key_filter)]
    if config.label:
        selectors.append(SettingSelector(key_filter=config.key_filter, label_filter=config.label))
    prefix = config.key_filter[:-1]
    with DefaultAzureCredential() as credential:
        with load(
            endpoint=config.endpoint,
            credential=credential,
            selects=selectors,
            trim_prefixes=[prefix] if prefix else [],
            key_vault_options=AzureAppConfigurationKeyVaultOptions(credential=credential),
        ) as provider:
            return {str(key).casefold(): value for key, value in provider.items()}


def reset_app_config_cache() -> None:
    """Discard cached remote settings, primarily for test isolation."""
    load_app_config.cache_clear()


class AzureAppConfigSource(EnvSettingsSource):
    """Reuse Pydantic's complex-value decoding for remote configuration values."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self.values = load_app_config(bootstrap())

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        key = f"{self.env_prefix}{field_name}".casefold()
        return self.values.get(key), field_name, False


class AppConfigAwareSettings(BaseSettings):
    """Use explicit overrides, environment, remote settings, dotenv, then secrets."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, AzureAppConfigSource(settings_cls), dotenv_settings, file_secret_settings
