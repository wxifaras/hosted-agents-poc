"""Isolated Story6 Responses host; none of the three existing entrypoints change."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStorageNotFoundError
from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    TextResponse,
)
from azure.ai.agentserver.responses.models import ResponseStreamEvent
from azure.ai.projects.aio import AIProjectClient
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from openai import OpenAIError
from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from app.core.app_config_source import env_file
from app.core.logger import Logger, configure_logging
from app.core.settings import Settings
from app.hosting import failed_response
from app.services.foundry_state import Caller, FoundryStore
from app.services.story6_approval import PREFIX, STORE_NAME, Command, Coordinator, EntraRoute, Reply, ToolboxRoute
from app.services.workflows import WorkflowError


class Story6Settings(Settings):
    """Additive settings consumed only by the dedicated coordinator entrypoint."""

    story6_enabled: bool = False
    story6_port: int = Field(default=8091, ge=1, le=65535)
    story6_request_timeout_seconds: float = Field(default=60, ge=1, le=180)
    story6_toolbox_version: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,9}$")
    story6_mcp_url: str | None = None
    story6_mcp_scope: str | None = None

    @field_validator("story6_toolbox_version", "story6_mcp_url", "story6_mcp_scope", mode="before")
    @classmethod
    def blank_public_route(cls, value: object) -> object:
        """A blank optional deployment placeholder selects the original public route."""
        return None if value == "" else value

    @model_validator(mode="after")
    def explicit_route(self) -> Story6Settings:
        """Reject half-configured or competing routes instead of a silent fallback."""
        if (self.story6_mcp_url is None) != (self.story6_mcp_scope is None):
            raise ValueError("STORY6_MCP_URL and STORY6_MCP_SCOPE must be configured together.")
        if self.story6_mcp_url is not None and self.story6_mcp_scope is not None:
            if self.story6_toolbox_version is not None:
                raise ValueError("Select either Entra MCP or toolbox, never both.")
            EntraRoute(self.story6_mcp_url, self.story6_mcp_scope, {}).endpoint()
        return self


@lru_cache(maxsize=1)
def get_story6_settings() -> Story6Settings:
    """Reuse the repository's layered sources without modifying old host settings."""

    class LocalSettings(Story6Settings):
        model_config = SettingsConfigDict(
            extra="ignore", case_sensitive=False, env_file=env_file(), env_file_encoding="utf-8"
        )

    return LocalSettings()


@asynccontextmanager
async def open_approval_store(
    credential: AsyncTokenCredential, caller: Caller, *, create: bool
) -> AsyncIterator[FoundryStore]:
    """Use a separate user-isolated store; never open an old story's registry."""
    store = (
        await FoundryStateStore.get_or_create(
            STORE_NAME, credential=credential, user_isolation=True, item_ttl_seconds=-1
        )
        if create
        else FoundryStateStore(STORE_NAME, credential=credential)
    )
    try:
        try:
            descriptor = await store.get()
        except FoundryStorageNotFoundError as error:
            raise WorkflowError("Story6 approval store is missing; continuation cannot create it.") from error
        if descriptor.user_isolation is not True or descriptor.item_ttl_seconds != -1:
            raise WorkflowError("Story6 requires a user-isolated, non-expiring approval store.")
        yield FoundryStore(store, caller)
    finally:
        await store.aclose()


async def execute(command: Command, context: ResponseContext, settings: Story6Settings) -> Reply:
    """Authorize before credentials/state, then submit at most one operation step."""
    caller = Caller.current()
    if not context.conversation_id:
        raise WorkflowError("Create an explicit Story6 coordinator conversation first.")
    settings.require_model()
    async with (
        DefaultAzureCredential() as credential,
        open_approval_store(credential, caller, create=command.operation == "start") as store,
    ):
        coordinator = Coordinator(store, caller, context.conversation_id, Path.home())
        if command.operation == "inspect":
            return await coordinator.inspect(command.operation_id)
        if command.operation == "start" and await store.read(coordinator.key(command.operation_id)) is not None:
            return await coordinator.inspect(command.operation_id)
        route_version = settings.story6_toolbox_version if command.operation == "start" else None
        mcp_url = settings.story6_mcp_url if command.operation == "start" else None
        mcp_scope = settings.story6_mcp_scope if command.operation == "start" else None
        if command.operation == "decide":
            binding = await coordinator.open(command.operation_id)
            route_version = binding.record.toolbox_version if binding.record.mcp_route == "foundry-toolbox" else None
            if binding.record.mcp_route == "entra-mcp":
                mcp_url, mcp_scope = binding.record.mcp_url, binding.record.mcp_scope
                if mcp_url is None or mcp_scope is None:
                    raise WorkflowError("Original Entra MCP binding is incomplete; do not reroute.")
        if mcp_url is not None and mcp_scope is not None:
            target_mcp = EntraRoute(mcp_url, mcp_scope, {})
            target_mcp.endpoint()
            mcp_token = await credential.get_token(target_mcp.scope)
            coordinator = Coordinator(
                store,
                caller,
                context.conversation_id,
                Path.home(),
                proxy=EntraRoute(mcp_url, mcp_scope, {"Authorization": "Bearer " + mcp_token.token}),
            )
        if route_version is not None:
            target = ToolboxRoute(
                project_endpoint=str(settings.foundry_project_endpoint),
                version=route_version,
                headers={},
            )
            target.endpoint()
            token = await credential.get_token("https://ai.azure.com/.default")
            coordinator = Coordinator(
                store,
                caller,
                context.conversation_id,
                Path.home(),
                proxy=ToolboxRoute(
                    project_endpoint=target.project_endpoint,
                    version=route_version,
                    headers={"Authorization": f"Bearer {token.token}", "x-agent-foundry-call-id": caller.call_id},
                ),
            )
        async with AIProjectClient(
            endpoint=str(settings.foundry_project_endpoint), credential=credential, allow_preview=True
        ) as project:
            async with project.get_openai_client(
                timeout=settings.story6_request_timeout_seconds, max_retries=0
            ) as provider:
                if command.operation == "decide":
                    return await coordinator.decide(command, provider, settings.azure_ai_model_deployment_name)
                async with project.get_openai_client(
                    agent_name="research-agent",
                    timeout=settings.story6_request_timeout_seconds,
                    max_retries=0,
                ) as research:
                    return await coordinator.start(command, research, provider, settings.azure_ai_model_deployment_name)


def build_host(settings: Story6Settings) -> ResponsesAgentServerHost:
    """Expose only the Story6 protocol, not a modified Supervisor/Research host."""

    def observability(**kwargs: object) -> None:
        connection = settings.applicationinsights_connection_string
        platform_connection = kwargs.get("connection_string")
        configure_logging(
            "story6-coordinator",
            level=settings.log_level,
            connection_string=(connection.get_secret_value() if connection else None)
            or (platform_connection if isinstance(platform_connection, str) else None),
            auth_mode=settings.applicationinsights_auth_mode,
        )

    server = ResponsesAgentServerHost(configure_observability=observability)

    @server.response_handler
    async def handle(
        request: CreateResponse, context: ResponseContext, cancellation: asyncio.Event
    ) -> TextResponse | AsyncIterator[ResponseStreamEvent]:
        if cancellation.is_set():
            raise asyncio.CancelledError
        try:
            if not settings.story6_enabled:
                raise WorkflowError("Story6 coordinator commands are disabled.")
            if request.get("background") or context.is_recovery:
                raise WorkflowError("Story6 does not support background execution or automatic recovery.")
            text = await context.get_input_text() or ""
            if not text.startswith(PREFIX):
                raise WorkflowError("The dedicated coordinator accepts only POC_STORY6 commands.")
            command = Command.model_validate_json(text[len(PREFIX) :])
            async with asyncio.timeout(settings.story6_request_timeout_seconds):
                reply = await execute(command, context, settings)
        except (WorkflowError, ValidationError, OpenAIError, AzureError, OSError, TimeoutError) as error:
            reason = str(error) if isinstance(error, WorkflowError) else type(error).__name__
            Logger("story6-coordinator").error("Story6 request stopped", error_type=type(error).__name__, reason=reason)
            return failed_response(
                request,
                context,
                f"Story6 request stopped: {reason}. Inspect the original operation; do not replay.",
                code="story6_request_failed",
            )
        return TextResponse(context, request, text=reply.model_dump_json())

    return server


def run() -> None:
    """Start without model inference; runtime credentials are acquired on commands."""
    settings = get_story6_settings()
    settings.require_model()
    host = build_host(settings)
    host.run(
        host="127.0.0.1" if settings.environment == "development" else "0.0.0.0",
        port=settings.port or settings.story6_port,
    )
