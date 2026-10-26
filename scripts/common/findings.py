"""Reader-first report summaries: verdict, key results, findings, and supporting Microsoft documentation.

Every documentation link here was opened and checked against the quoted text. A finding with no
official page says so explicitly instead of pointing at a page that does not support it.
"""

from collections.abc import Sequence
from dataclasses import dataclass

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
