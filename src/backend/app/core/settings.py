"""Small typed settings schema without unrelated application-service dependencies."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from app.core.app_config_source import AppConfigAwareSettings, env_file

AgentName = Literal["supervisor", "research", "data_analysis"]
AGENT_NAMES: tuple[AgentName, ...] = ("supervisor", "research", "data_analysis")


class Settings(AppConfigAwareSettings):
    """Configuration shared by hosts and validation commands."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    environment: str = "development"
    poc_agent_name: AgentName = "supervisor"
    foundry_project_endpoint: HttpUrl | None = None
    azure_ai_model_deployment_name: str = ""
    applicationinsights_connection_string: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    port: int | None = Field(default=None, ge=1, le=65535)
    supervisor_port: int = Field(default=8088, ge=1, le=65535)
    research_port: int = Field(default=8089, ge=1, le=65535)
    data_analysis_port: int = Field(default=8090, ge=1, le=65535)
    request_timeout_seconds: float = Field(default=60, gt=0, le=3600)

    @field_validator("foundry_project_endpoint", "applicationinsights_connection_string", mode="before")
    @classmethod
    def empty_optional_value(cls, value: object) -> object:
        """Allow the blank optional placeholders shipped in .env.example."""
        return None if value == "" else value

    def require_model(self) -> None:
        """Reject startup without the explicitly selected Foundry model."""
        if self.foundry_project_endpoint is None or not self.azure_ai_model_deployment_name.strip():
            raise ValueError(
                "Set FOUNDRY_PROJECT_ENDPOINT and AZURE_AI_MODEL_DEPLOYMENT_NAME before starting an agent."
            )
        if self.foundry_project_endpoint.scheme != "https":
            raise ValueError("FOUNDRY_PROJECT_ENDPOINT must use HTTPS.")

    def agent_port(self, agent: AgentName) -> int:
        """Respect the platform PORT override, otherwise select a local agent port."""
        return (
            self.port
            or {
                "supervisor": self.supervisor_port,
                "research": self.research_port,
                "data_analysis": self.data_analysis_port,
            }[agent]
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load once on explicit startup, with a stable local dotenv location."""

    class LocalSettings(Settings):
        model_config = SettingsConfigDict(
            extra="ignore", case_sensitive=False, env_file=env_file(), env_file_encoding="utf-8"
        )

    return LocalSettings()
