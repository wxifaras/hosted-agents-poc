"""Reader-first report summaries: verdict, key results, findings, and supporting Microsoft documentation.

Every documentation link here was opened and checked against the quoted text. A finding with no
official page says so explicitly instead of pointing at a page that does not support it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from os.path import relpath
from pathlib import Path

from scripts.common.reporting import markdown_cell


@dataclass(frozen=True)
class Doc:
    """An official Microsoft Learn page and a short verbatim excerpt that supports a finding."""

    title: str
    url: str
    quote: str

    def link(self) -> str:
        return f"[{self.title}]({self.url})"


NO_DOC = "No official Microsoft documentation found"

CI = "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/code-interpreter"
CI_TITLE = "Use Code Interpreter with Microsoft Foundry agents"
HOSTED = "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents"
CONTRACT = "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract"
AGENT_IDENTITY = "https://learn.microsoft.com/azure/foundry/agents/concepts/agent-identity"
MCP_AUTH = "https://learn.microsoft.com/azure/foundry/agents/how-to/mcp-authentication"

# Verified on learn.microsoft.com on 2026-10-24; quotes are verbatim excerpts (tables are summarized as noted).
DOCS: dict[str, Doc] = {
    "ci_overview": Doc(
        CI_TITLE,
        CI,
        "Code Interpreter enables a Microsoft Foundry agent to run Python code in a sandboxed execution environment.",
    ),
    "ci_sandbox": Doc(
        f"{CI_TITLE}: Sandboxed execution environment",
        f"{CI}#sandboxed-execution-environment",
        "The sandbox is designed for running untrusted code and uses dynamic sessions (code interpreter sessions) in "
        "Azure Container Apps. Each session is isolated by a Hyper-V boundary.",
    ),
    "ci_network": Doc(
        f"{CI_TITLE}: Sandboxed execution environment",
        f"{CI}#sandboxed-execution-environment",
        "The sandbox doesn't inherit your agent subnet configuration, and dynamic sessions can't make outbound "
        "network requests.",
    ),
    "ci_sessions": Doc(
        f"{CI_TITLE}: Sandboxed execution environment",
        f"{CI}#sandboxed-execution-environment",
        "If your agent invokes Code Interpreter concurrently in different conversations, separate sessions are "
        "created.",
    ),
    "ci_lifetime": Doc(
        CI_TITLE,
        CI,
        "Each session is active by default for one hour with an idle timeout of 30 minutes.",
    ),
    "ci_isolation": Doc(
        CI_TITLE,
        CI,
        "When Code Interpreter is used through a toolbox in a hosted agent, user isolation isn't supported. All "
        "users in the same project share the same container context.",
    ),
    "ci_files": Doc(
        CI_TITLE,
        CI,
        "The response includes container_file_citation annotations with the generated file details. Save the "
        "container_id and file_id values from the annotation.",
    ),
    "ci_packages": Doc(
        CI_TITLE,
        CI,
        "Code Interpreter includes common data science packages. For custom packages, use Custom code interpreter.",
    ),
    "responses_containers": Doc(
        "Use the Azure OpenAI Responses API",
        "https://learn.microsoft.com/azure/foundry/openai/how-to/responses",
        "This container expires if it is not used for 20 minutes.",
    ),
    "aca_isolation": Doc(
        "Serverless code interpreter sessions in Azure Container Apps",
        "https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter",
        "Each code interpreter session is fully isolated by a Hyper-V boundary and is designed to run untrusted code.",
    ),
    "aca_runtime": Doc(
        "Serverless code interpreter sessions in Azure Container Apps",
        "https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter",
        "Each code execution is limited to a maximum runtime of 220 seconds.",
    ),
    "custom_ci": Doc(
        "Configure a custom code interpreter for agents",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/custom-code-interpreter",
        "A custom code interpreter gives you full control over the runtime environment ... by using Azure Container "
        "Apps Dynamic Sessions.",
    ),
    "sdk_memory": Doc(
        "AutoCodeInterpreterToolParam class (Azure AI Projects SDK)",
        "https://learn.microsoft.com/python/api/azure-ai-projects/azure.ai.projects.models.autocodeinterpreterToolparam",
        'memory_limit: Known values are: "1g", "4g", "16g", and "64g".',
    ),
    "sdk_network": Doc(
        "ContainerNetworkPolicyParam class (Azure AI Projects SDK)",
        "https://learn.microsoft.com/python/api/azure-ai-projects/azure.ai.projects.models.containernetworkpolicyparam",
        'Known values are: "disabled" and "allowlist".',
    ),
    "toolbox_overview": Doc(
        "What is Toolbox in Microsoft Foundry?",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/toolbox-overview",
        "... expose them through a single MCP-compatible endpoint.",
    ),
    "toolbox_network": Doc(
        "Network isolation for a toolbox in Microsoft Foundry",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox-network-isolation",
        "Code interpreter: Supported (Microsoft backbone network). (tool support table)",
    ),
    "files_upload": Doc(
        "Files - Upload (Azure OpenAI REST API)",
        "https://learn.microsoft.com/rest/api/azureopenai/files/upload",
        "assistants: This file contains data to be used in assistants.",
    ),
    "hosted_sizes": Doc(
        "Hosted agents in Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents",
        "Sandbox sizes include 0.5 vCPU / 1 GiB, 1 vCPU / 2 GiB, and 2 vCPU / 4 GiB. (sizes table)",
    ),
    "hosted_conversations": Doc(
        "Hosted agents in Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents",
        "A conversation ID is a durable record of conversation history (messages, tool calls, and responses) stored "
        "in Foundry.",
    ),
    "hosted_sessions": Doc(
        "Manage hosted agent sessions",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions",
        "Set idle_timeout_seconds from 120 through 14,400 seconds (2 minutes through 4 hours). If you omit the "
        "setting, the server default is 900 seconds (15 minutes).",
    ),
    "isolate_sessions": Doc(
        "Isolate hosted agent sessions per user",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user",
        "The service sends the end user's stable identifier in the x-ms-user-identity header. The platform treats "
        "the value as an opaque string and scopes the session to it.",
    ),
    "state_store": Doc(
        "Durable state store for Microsoft Foundry hosted agents (preview)",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/agent-state-store",
        "During preview, the state store is available only to hosted agents. ... The state store removes the need "
        "to provision and secure that storage yourself.",
    ),
    "standard_files": Doc(
        "Set up standard agent resources for Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/standard-agent-setup",
        "Azure Storage (BYO File Storage): Files uploaded by developers and end-users. (resource table)",
    ),
    "standard_threads": Doc(
        "Set up standard agent resources for Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/standard-agent-setup",
        "Azure Cosmos DB (BYO Thread Storage): Messages, conversation history, and agent metadata. (resource table)",
    ),
    "storage_private_endpoints": Doc(
        "Use private endpoints for Azure Storage",
        "https://learn.microsoft.com/azure/storage/common/storage-private-endpoints",
        "... it provides secure connectivity between clients on your virtual network and your storage ...",
    ),
    "nsp": Doc(
        "What is a network security perimeter?",
        "https://learn.microsoft.com/azure/private-link/network-security-perimeter-concepts",
        "By default, network security perimeter restricts public access to PaaS resources within the boundary.",
    ),
    "blob_rbac": Doc(
        "Assign an Azure role for access to blob data",
        "https://learn.microsoft.com/azure/storage/blobs/assign-azure-role-data-access",
        "The following example assigns the Storage Blob Data Contributor role to a user.",
    ),
    # Story 4 entries, verified on learn.microsoft.com on 2026-10-26.
    "hosted_invoke": Doc(
        "Hosted agents in Foundry Agent Service: Agent identity and endpoint",
        f"{HOSTED}#agent-identity-and-endpoint",
        "Every Hosted agent deployed to a Foundry project gets its own dedicated Microsoft Entra ID (agent identity) "
        "and dedicated endpoint—both created automatically at deploy time.",
    ),
    "responses_background": Doc(
        "Hosted agents in Foundry Agent Service: Protocols",
        f"{HOSTED}#protocols-responses-invocations-and-invocations-websocket",
        "background: true with platform-managed polling and cancellation. Opt in separately when the handler must "
        "recover after a process interruption. (protocol table, Background / async processing row)",
    ),
    "a2a_protocol": Doc(
        "Hosted agents in Foundry Agent Service: Protocols",
        f"{HOSTED}#protocols-responses-invocations-and-invocations-websocket",
        "The A2A protocol supports agent-to-agent delegation.",
    ),
    "resilience": Doc(
        "Resilience for long-running Microsoft Foundry hosted agents (preview)",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience"
        "#background-execution-and-resilience",
        "For the Responses protocol, full crash recovery applies only to stored background responses when the server "
        "opts in to resilient background execution.",
    ),
    "a2a": Doc(
        "Connect to an A2A agent endpoint from Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent",
        "The A2A tool enables agent-to-agent communication through a standardized protocol, allowing your Foundry "
        "agent to exchange context and collaborate with external agents.",
    ),
    "hosted_contract": Doc(
        "Hosted agent runtime contract: Platform request headers",
        f"{CONTRACT}#platform-request-headers-container-protocol-200",
        "Forward it unchanged on outbound calls to Foundry services (Storage, Toolbox, and other agents); the "
        "platform resolves the caller's identity from it. (x-agent-foundry-call-id)",
    ),
    "hosted_user_header": Doc(
        "Hosted agent runtime contract: Platform request headers",
        f"{CONTRACT}#platform-request-headers-container-protocol-200",
        "Use it as the primary partition key for per-user data your container stores; it's for your container's own "
        "use and isn't forwarded outbound. (x-agent-user-id)",
    ),
    "client_headers": Doc(
        "Hosted agent runtime contract: Forward custom request headers to your container",
        f"{CONTRACT}#forward-custom-request-headers-to-your-container",
        "The platform forwards every header that starts with x-client- unchanged, so you can send values like a "
        "tenant ID or a feature flag without changing the request body ...",
    ),
    "impersonation": Doc(
        "Isolate hosted agent sessions per user: Isolate sessions for your own users",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user"
        "#isolate-sessions-for-your-own-users",
        "This permission is not included in any built-in role. It was previously covered by the "
        "Microsoft.CognitiveServices/* data action, but that action no longer grants it. ... A caller without it "
        "receives a 403.",
    ),
    "agent_identity": Doc(
        "Agent identity concepts in Microsoft Foundry",
        AGENT_IDENTITY,
        "For hosted agents, the token service lets your code obtain tokens for the current agent instance's identity "
        "by default.",
    ),
    "a2a_identity": Doc(
        "Agent identity concepts in Microsoft Foundry: Supported tools",
        f"{AGENT_IDENTITY}#supported-tools",
        "Application-only access with the calling agent's identity, or delegated access when the target supports it. "
        "(Agent-to-agent (A2A) row)",
    ),
    "mcp_tool": Doc(
        "Connect agents to MCP server endpoints",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol",
        "Use an allow list of tools by using allowed_tools.",
    ),
    "mcp_auth_obo": Doc(
        "Set up MCP server authentication: Supported authentication methods",
        f"{MCP_AUTH}#supported-authentication-methods",
        "Use UserEntraToken or AgentOnBehalfOfUser with a supported MCP tool to access a downstream resource in the "
        "incoming user's context. (Microsoft Entra client user delegation row)",
    ),
    "mcp_auth_passthrough": Doc(
        "Set up MCP server authentication: OAuth identity passthrough",
        f"{MCP_AUTH}#oauth-identity-passthrough",
        "When you use OAuth identity passthrough, Agent Service generates a consent link the first time a particular "
        "user needs to authorize access.",
    ),
    "learn_mcp": Doc(
        "Microsoft Learn MCP Server overview",
        "https://learn.microsoft.com/training/support/mcp",
        "There's no authentication required to access the Microsoft Learn MCP Server.",
    ),
    "toolbox_auth": Doc(
        "What is Toolbox in Microsoft Foundry?",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/toolbox-overview",
        "The toolbox handles credential injection, token refresh, and policy enforcement at runtime by using "
        "Microsoft Entra ID and OAuth identity passthrough, so consuming agents don't manage per-tool credentials.",
    ),
    "foundry_rbac": Doc(
        "Hosted agent permissions reference: Roles in this article",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions#roles-in-this-article",
        "Foundry User: Create agents, perform model inference, and interact with agents. (roles table)",
    ),
    "maf_function_tools": Doc(
        "Using function tools with an agent (Microsoft Agent Framework)",
        "https://learn.microsoft.com/agent-framework/agents/tools/function-tools",
        "Function tools are just custom code that you want the agent to be able to call when needed.",
    ),
    "maf_orchestration": Doc(
        "Workflow orchestrations in Agent Framework",
        "https://learn.microsoft.com/agent-framework/workflows/orchestrations/",
        "Magentic: A manager agent dynamically coordinates specialized agents. (patterns table)",
    ),
}


@dataclass(frozen=True)
class Finding:
    """One plain-language finding with its result, evidence basis, ownership, and documentation."""

    finding: str
    result: str
    basis: str
    ownership: str
    docs: tuple[str, ...] = ()


def doc_cell(keys: Sequence[str]) -> str:
    """One link per page, even when several quotes from the same page support the finding."""
    links = list(dict.fromkeys(DOCS[key].link() for key in keys))
    return "; ".join(links) if links else NO_DOC


ADR_FILE = Path(__file__).resolve().parents[2] / "docs" / "adr" / "0001-hosted-agents-poc.md"
ISOLATION_OPTIONS_ANCHOR = "per-user-code-interpreter-isolation-options-to-investigate"


def isolation_options_link(report_dir: Path) -> str:
    """Link from a report's folder to the ADR section listing untested per-user isolation options."""
    try:
        target = Path(relpath(ADR_FILE, report_dir)).as_posix()
    except ValueError:
        # Reports on another drive cannot use a relative path.
        target = ADR_FILE.as_posix()
    return f"[ADR-0001: per-user isolation options to investigate]({target}#{ISOLATION_OPTIONS_ANCHOR})"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(markdown_cell(header) for header in headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(markdown_cell(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def render_summary(
    verdict: str,
    conclusion: str,
    results: Sequence[str],
    findings: Sequence[Finding],
    gaps: Sequence[str],
) -> str:
    """Summary first: what was concluded, what was measured, what it means, and where Microsoft documents it."""
    used = list(dict.fromkeys(key for finding in findings for key in finding.docs))
    parts = [
        "## Summary",
        f"**Acceptance verdict: {verdict}.** {conclusion}".strip(),
        "### Key results",
        "\n".join(f"- {line}" for line in results) or "- No results were recorded.",
        "### Findings",
        "Evidence: **Tested** = verified by this run; **Observed** = read from the platform during this run; "
        "**Documented** = stated by Microsoft, not tested here; **Unverified** = neither tested nor documented.",
        _table(
            ["#", "Finding", "Result", "Evidence", "Ownership", "Microsoft documentation"],
            [
                [str(number), item.finding, item.result, item.basis, item.ownership, doc_cell(item.docs)]
                for number, item in enumerate(findings, start=1)
            ],
        ),
    ]
    if gaps:
        parts += ["### Gaps and follow-ups", "\n".join(f"- {gap}" for gap in gaps)]
    if used:
        parts += [
            "### Microsoft documentation referenced",
            _table(["Document", "What it says"], [[DOCS[key].link(), f'"{DOCS[key].quote}"'] for key in used]),
        ]
    parts += ["---", "_Detailed evidence for every check follows below._"]
    return "\n\n".join(parts) + "\n"
