"""Compose MAF agents with public Responses protocol handlers."""

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient, FoundryChatOptions, FoundryToolbox
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
        "Managed native Code Interpreter analysis is available only through the explicit "
        "POC_STORY1_NATIVE runtime protocol with verified owner/session/conversation authorization. "
        "Plain chat and delegated text turns do not execute Python or access session files. "
        "Never claim they measured resources, analyzed files, or created artifacts."
    ),
}

_RESEARCH_TOOL_INSTRUCTIONS = (
    "You are the Research specialist in a Hosted Agents runtime POC. "
    "You have a Microsoft Learn documentation search tool reached through a Foundry toolbox (MCP). "
    "When a task asks you to search or cite documentation, call the tool and cite only URLs it returned. "
    "Never invent sources or claim a search you did not run. Answer other questions from the supplied context."
)


def data_analysis_tools(
    name: AgentName, settings: Settings, credential: TokenCredential
) -> list[MCPStreamableHTTPTool]:
    """Data Analysis uses only the authorized native protocol, never a toolbox fallback."""
    return []


def research_tools(name: AgentName, settings: Settings, credential: TokenCredential) -> list[MCPStreamableHTTPTool]:
    """Story 4: Research gets its own MCP toolbox (Microsoft Learn search), only when configured."""
    if name != "research" or settings.toolbox_endpoint is None:
        return []
    return [FoundryToolbox(credential, url=settings.toolbox_endpoint)]


def build_agent(name: AgentName, settings: Settings, credential: TokenCredential) -> Agent[FoundryChatOptions]:
    """Create the model-backed agent using verified current MAF APIs."""
    settings.require_model()
    client = FoundryChatClient(
        project_endpoint=str(settings.foundry_project_endpoint),
        model=settings.azure_ai_model_deployment_name,
        credential=credential,
    )
    tools = data_analysis_tools(name, settings, credential) or research_tools(name, settings, credential)
    instructions = _INSTRUCTIONS[name]
    if tools:
        instructions = _RESEARCH_TOOL_INSTRUCTIONS
    return Agent(
        client=client,
        id=f"{name}-agent",
        name=f"{name}-agent",
        instructions=instructions,
        tools=tools or None,
        default_options={"store": False},
    )


def run_agent(name: AgentName) -> None:
    """Start one independent Responses host; PORT overrides per-agent local ports."""
    settings = get_settings()
    settings.require_model()
    connection = settings.applicationinsights_connection_string

    # Our callback owns the single pipeline; the SDK's platform-resolved connection string is the fallback.
    def configure_host(**kwargs: object) -> None:
        platform_connection = kwargs.get("connection_string")
        configure_logging(
            name,
            level=settings.log_level,
            connection_string=(connection.get_secret_value() if connection else None)
            or (platform_connection if isinstance(platform_connection, str) else None),
            auth_mode=settings.applicationinsights_auth_mode,
        )

    with DefaultAzureCredential() as credential:
        agent = build_agent(name, settings, credential)
        server = build_host(name, settings, agent, configure_host)
        Logger(name).info("Starting model-backed Responses host", agent=name, port=settings.agent_port(name))
        server.run(
            host="127.0.0.1" if settings.environment == "development" else "0.0.0.0", port=settings.agent_port(name)
        )
