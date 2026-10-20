"""Compose MAF agents with public Responses protocol handlers."""

from functools import partial

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient, FoundryChatOptions
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential

from app.core.logger import Logger, configure_logging
from app.core.settings import AgentName, Settings, get_settings
from app.hosting import build_host

_INSTRUCTIONS: dict[AgentName, str] = {
    "supervisor": (
        "You are the Supervisor in a Hosted Agents runtime POC. "
        "Plain chat is model-backed. Explicit POC_WORKFLOW commands are handled by the runtime, "
        "which delegates to Research and Data Analysis with distinct persisted conversations. "
        "Never claim that plain chat invoked specialists, created artifacts, or demonstrated crash recovery."
    ),
    "research": (
        "You are the Research specialist in a Hosted Agents runtime POC. "
        "Answer from the supplied context. External research tools are not connected yet; "
        "do not claim to have searched or invoked tools."
    ),
    "data_analysis": (
        "You are the Data Analysis specialist in a Hosted Agents runtime POC. "
        "This foundation has no file analysis tools yet. Discuss analysis of supplied synthetic examples, "
        "but never claim to have executed Python, measured resources, or created artifacts."
    ),
}


def build_agent(name: AgentName, settings: Settings, credential: TokenCredential) -> Agent[FoundryChatOptions]:
    """Create the model-backed agent using verified current MAF APIs."""
    settings.require_model()
    client = FoundryChatClient(
        project_endpoint=str(settings.foundry_project_endpoint),
        model=settings.azure_ai_model_deployment_name,
        credential=credential,
    )
    return Agent(
        client=client,
        id=f"{name}-agent",
        name=f"{name}-agent",
        instructions=_INSTRUCTIONS[name],
        default_options={"store": False},
    )


def run_agent(name: AgentName) -> None:
    """Start one independent Responses host; PORT overrides per-agent local ports."""
    settings = get_settings()
    settings.require_model()
    connection = settings.applicationinsights_connection_string
    configure = partial(
        configure_logging,
        name,
        level=settings.log_level,
        connection_string=connection.get_secret_value() if connection else None,
    )

    # The SDK passes its own telemetry kwargs; our callback owns the single pipeline.
    def configure_host(**_kwargs: object) -> None:
        configure()

    with DefaultAzureCredential() as credential:
        agent = build_agent(name, settings, credential)
        server = build_host(name, settings, agent, configure_host)
        Logger(name).info("Starting model-backed Responses host", agent=name, port=settings.agent_port(name))
        server.run(
            host="127.0.0.1" if settings.environment == "development" else "0.0.0.0", port=settings.agent_port(name)
        )
