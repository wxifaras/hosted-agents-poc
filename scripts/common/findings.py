"""Reader-first report summaries: verdict, outcomes, findings, and supporting Microsoft documentation.

Every documentation link here was opened and checked against the quoted text. A finding with no
official page says so explicitly instead of pointing at a page that does not support it.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from os.path import relpath
from pathlib import Path
from typing import Literal

from scripts.common.reporting import markdown_cell


@dataclass(frozen=True)
class Doc:
    """An official Microsoft Learn page, a short verbatim excerpt, and what it means for this POC."""

    title: str
    url: str
    quote: str
    meaning: str

    def link(self) -> str:
        return f"[{self.title}]({self.url})"


NO_DOC = "No official Microsoft documentation found"

CI = "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/code-interpreter"
CI_TITLE = "Use Code Interpreter with Microsoft Foundry agents"
HOSTED = "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents"
CONTRACT = "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract"
AGENT_IDENTITY = "https://learn.microsoft.com/azure/foundry/agents/concepts/agent-identity"
MCP_AUTH = "https://learn.microsoft.com/azure/foundry/agents/how-to/mcp-authentication"

# Links, anchors, and quotes re-verified on learn.microsoft.com on 2026-10-26. Quotes are verbatim
# excerpts; "..." marks omitted text and a trailing "(... table/row)" note marks a summarized table row.
DOCS: dict[str, Doc] = {
    "ci_overview": Doc(
        CI_TITLE,
        CI,
        "Code Interpreter enables a Microsoft Foundry agent to run Python code in a sandboxed execution environment.",
        "The agent's Python runs in a Microsoft-managed sandbox, not inside the agent's own container.",
    ),
    "ci_sandbox": Doc(
        f"{CI_TITLE}: Sandboxed execution environment",
        f"{CI}#sandboxed-execution-environment",
        "The sandbox is designed for running untrusted code and uses dynamic sessions (code interpreter sessions) in "
        "Azure Container Apps. Each session is isolated by a Hyper-V boundary.",
        "Microsoft runs each sandbox as its own Hyper-V-isolated Azure Container Apps session. You can't see or "
        "configure that infrastructure from your subscription.",
    ),
    "ci_network": Doc(
        f"{CI_TITLE}: Sandboxed execution environment",
        f"{CI}#sandboxed-execution-environment",
        "The sandbox doesn't inherit your agent subnet configuration, and dynamic sessions can't make outbound "
        "network requests.",
        "Code in the sandbox can't reach the internet, your network, or your storage. Inputs must be mounted as "
        "files, and outputs must be downloaded by a client.",
    ),
    "ci_sessions": Doc(
        f"{CI_TITLE}: Sandboxed execution environment",
        f"{CI}#sandboxed-execution-environment",
        "If your agent invokes Code Interpreter concurrently in different conversations, separate sessions are "
        "created.",
        "Different conversations get different sandboxes, so don't expect sandbox state to be shared between them.",
    ),
    "ci_lifetime": Doc(
        CI_TITLE,
        CI,
        "Each session is active by default for one hour with an idle timeout of 30 minutes.",
        "Sandboxes are short-lived. Anything left in one is lost after the idle timeout.",
    ),
    "ci_isolation": Doc(
        CI_TITLE,
        CI,
        "When Code Interpreter is used through a toolbox in a hosted agent, user isolation isn't supported. All "
        "users in the same project share the same container context.",
        "Any user of the project can reach the same sandbox files. Don't put one user's sensitive data where "
        "another user could read it.",
    ),
    "ci_files": Doc(
        CI_TITLE,
        CI,
        "The response includes container_file_citation annotations with the generated file details. Save the "
        "container_id and file_id values from the annotation.",
        "Generated files stay in the sandbox. A client downloads them later by container ID and file ID.",
    ),
    "ci_packages": Doc(
        CI_TITLE,
        CI,
        "Code Interpreter includes common data science packages. For custom packages, use Custom code interpreter.",
        "Only the preinstalled packages are available. Anything else needs a custom code interpreter.",
    ),
    "responses_containers": Doc(
        "Use the Azure OpenAI Responses API",
        "https://learn.microsoft.com/azure/foundry/openai/how-to/responses",
        "This container expires if it is not used for 20 minutes.",
        "A sandbox container and its files expire after 20 idle minutes. Copy anything you need to keep.",
    ),
    "aca_isolation": Doc(
        "Serverless code interpreter sessions in Azure Container Apps",
        "https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter",
        "Each code interpreter session is fully isolated by a Hyper-V boundary and is designed to run untrusted code.",
        "Hyper-V isolation per session is what makes the sandbox suitable for model-written code.",
    ),
    "aca_runtime": Doc(
        "Serverless code interpreter sessions in Azure Container Apps",
        "https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter",
        "Each code execution is limited to a maximum runtime of 220 seconds.",
        "One code execution can run for at most 220 seconds. Longer analyses must be split into steps.",
    ),
    "custom_ci": Doc(
        "Configure a custom code interpreter for agents",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/custom-code-interpreter",
        "A custom code interpreter gives you full control over the runtime environment for agent-generated Python "
        "code. You can configure custom Python packages, compute resources, and Azure Container Apps environment "
        "settings.",
        "If you need your own packages, compute, network, or session key, run the code interpreter on your own "
        "Azure Container Apps session pool (preview).",
    ),
    "sdk_memory": Doc(
        "AutoCodeInterpreterToolParam class (Azure AI Projects SDK)",
        "https://learn.microsoft.com/python/api/azure-ai-projects/azure.ai.projects.models.autocodeinterpreterToolparam",
        'memory_limit: Known values are: "1g", "4g", "16g", and "64g".',
        "The SDK accepts larger sandbox memory sizes. Whether they take effect in your project must be tested "
        "(Story 1 Step 4 checks this).",
    ),
    "sdk_network": Doc(
        "ContainerNetworkPolicyParam class (Azure AI Projects SDK)",
        "https://learn.microsoft.com/python/api/azure-ai-projects/azure.ai.projects.models.containernetworkpolicyparam",
        'Known values are: "disabled" and "allowlist".',
        "The SDK offers a network allowlist. Organization policy can still block it (Story 1 Step 4 checks this).",
    ),
    "toolbox_overview": Doc(
        "What is Toolbox in Microsoft Foundry?",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/toolbox-overview",
        "... expose them through a single MCP-compatible endpoint.",
        "A toolbox bundles tools behind one MCP endpoint. Data Analysis reaches Code Interpreter this way.",
    ),
    "toolbox_network": Doc(
        "Network isolation for a toolbox in Microsoft Foundry",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox-network-isolation",
        "Code interpreter: Supported (Microsoft backbone network). (tool support table)",
        "Code Interpreter traffic from a toolbox stays on the Microsoft network; it doesn't enter your network.",
    ),
    "files_upload": Doc(
        "Files - Upload (Azure OpenAI REST API)",
        "https://learn.microsoft.com/rest/api/azureopenai/files/upload",
        "assistants: This file contains data to be used in assistants.",
        "Input files are uploaded with purpose `assistants` before a toolbox can mount them in the sandbox.",
    ),
    "hosted_sizes": Doc(
        "Hosted agents in Foundry Agent Service: Sandbox sizes",
        f"{HOSTED}#sandbox-sizes",
        "Hosted agent sandboxes support the following CPU and memory combinations: 0.5 vCPU 1 GiB, 1 vCPU 2 GiB, "
        "2 vCPU 4 GiB. (sizes table)",
        "These sizes apply to the hosted agent's own container. The Code Interpreter sandbox has separate limits.",
    ),
    "hosted_conversations": Doc(
        "Hosted agents in Foundry Agent Service",
        HOSTED,
        "A conversation ID is a durable record of conversation history (messages, tool calls, and responses) stored "
        "in Foundry.",
        "Foundry stores conversation history, so requests that reuse a conversation ID see the earlier messages.",
    ),
    "hosted_sessions": Doc(
        "Manage hosted agent sessions",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions",
        "Set idle_timeout_seconds from 120 through 14,400 seconds (2 minutes through 4 hours). If you omit the "
        "setting, the server default is 900 seconds (15 minutes).",
        "The hosted agent's session idles out on its own timer, independent of the Code Interpreter sandbox.",
    ),
    "isolate_sessions": Doc(
        "Isolate hosted agent sessions per user",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user",
        "The service sends the end user's stable identifier in the x-ms-user-identity header. The platform treats "
        "the value as an opaque string and scopes the session to it.",
        "A trusted service can scope sessions to its own end users with `x-ms-user-identity`. It is a label, not an "
        "access token, and Microsoft documents no automatic propagation across nested agent calls.",
    ),
    "state_store": Doc(
        "Durable state store for Microsoft Foundry hosted agents (preview)",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/agent-state-store",
        "During preview, the state store is available only to hosted agents. ... The state store removes the need "
        "to provision and secure that storage yourself.",
        "Foundry provides and manages the state store (preview). You don't provision it, and you can't place it in "
        "your own storage account.",
    ),
    "standard_files": Doc(
        "Set up standard agent resources for Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/standard-agent-setup",
        "Azure Storage (BYO File Storage): Files uploaded by developers and end-users. (resource table)",
        "With the Standard agent setup, uploaded files can be kept in your own Storage account.",
    ),
    "standard_threads": Doc(
        "Set up standard agent resources for Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/standard-agent-setup",
        "Azure Cosmos DB (BYO Thread Storage): Messages, conversation history, and agent metadata. (resource table)",
        "With the Standard agent setup, conversation history can be kept in your own Cosmos DB account.",
    ),
    "storage_private_endpoints": Doc(
        "Use private endpoints for Azure Storage",
        "https://learn.microsoft.com/azure/storage/common/storage-private-endpoints",
        "... it provides secure connectivity between clients on your virtual network and your storage ...",
        "Private endpoints keep storage traffic on your virtual network. This POC does not test them.",
    ),
    "nsp": Doc(
        "What is a network security perimeter?",
        "https://learn.microsoft.com/azure/private-link/network-security-perimeter-concepts",
        "By default, network security perimeter restricts public access to PaaS resources within the boundary.",
        "A network security perimeter restricts public access to the storage account. This POC uses one.",
    ),
    "blob_rbac": Doc(
        "Assign an Azure role for access to blob data",
        "https://learn.microsoft.com/azure/storage/blobs/assign-azure-role-data-access",
        "The following example assigns the Storage Blob Data Contributor role to a user.",
        "Blob data access is granted with Azure RBAC roles such as Storage Blob Data Contributor, not shared keys.",
    ),
    "hosted_invoke": Doc(
        "Hosted agents in Foundry Agent Service: Agent identity and endpoint",
        f"{HOSTED}#agent-identity-and-endpoint",
        "Every Hosted agent deployed to a Foundry project gets its own dedicated Microsoft Entra ID (agent identity) "
        "and dedicated endpoint—both created automatically at deploy time.",
        "Each hosted agent has its own endpoint and identity, so a client or another agent can call it directly.",
    ),
    "responses_background": Doc(
        "Hosted agents in Foundry Agent Service: Protocols",
        f"{HOSTED}#protocols-responses-invocations-and-invocations-websocket",
        "background: true with platform-managed polling and cancellation. Opt in separately when the handler must "
        "recover after a process interruption. (protocol table, Background / async processing row)",
        "A caller can start a response in the background and poll for its result instead of waiting.",
    ),
    "a2a_protocol": Doc(
        "Hosted agents in Foundry Agent Service: Protocols",
        f"{HOSTED}#protocols-responses-invocations-and-invocations-websocket",
        "The A2A protocol supports agent-to-agent delegation.",
        "Hosted agents can also delegate over the A2A protocol. This POC uses the Responses API instead.",
    ),
    "resilience": Doc(
        "Resilience for long-running Microsoft Foundry hosted agents (preview)",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/long-running-agent-resilience"
        "#background-execution-and-resilience",
        "For the Responses protocol, full crash recovery applies only to stored background responses when the server "
        "opts in to resilient background execution.",
        "Surviving a host crash mid-response needs an explicit opt-in (preview). This POC doesn't enable it.",
    ),
    "a2a": Doc(
        "Connect to an A2A agent endpoint from Foundry Agent Service",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent",
        "The A2A tool enables agent-to-agent communication through a standardized protocol, allowing your Foundry "
        "agent to exchange context and collaborate with external agents.",
        "Foundry offers a managed A2A tool for agent-to-agent calls, an alternative to this POC's custom delegation.",
    ),
    "hosted_contract": Doc(
        "Hosted agent runtime contract: Platform request headers",
        f"{CONTRACT}#platform-request-headers-container-protocol-200",
        "Forward it unchanged on outbound calls to Foundry services (Storage, Toolbox, and other agents); the "
        "platform resolves the caller's identity from it. (x-agent-foundry-call-id)",
        "Forwarding `x-agent-foundry-call-id` unchanged lets Foundry resolve the original caller on the next hop.",
    ),
    "hosted_user_header": Doc(
        "Hosted agent runtime contract: Platform request headers",
        f"{CONTRACT}#platform-request-headers-container-protocol-200",
        "Use it as the primary partition key for per-user data your container stores; it's for your container's own "
        "use and isn't forwarded outbound. (x-agent-user-id)",
        "The resolved user ID partitions the container's own data. It is not forwarded to other agents.",
    ),
    "client_headers": Doc(
        "Hosted agent runtime contract: Forward custom request headers to your container",
        f"{CONTRACT}#forward-custom-request-headers-to-your-container",
        "The platform forwards every header that starts with x-client- unchanged, so you can send values like a "
        "tenant ID or a feature flag without changing the request body ...",
        "Headers that start with `x-client-` reach the agent unchanged. This POC uses one for correlation.",
    ),
    "impersonation": Doc(
        "Isolate hosted agent sessions per user: Isolate sessions for your own users",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user"
        "#isolate-sessions-for-your-own-users",
        "This permission is not included in any built-in role. It was previously covered by the "
        "Microsoft.CognitiveServices/* data action, but that action no longer grants it. ... A caller without it "
        "receives a 403.",
        "Sending `x-ms-user-identity` needs a custom role with the impersonation permission. Without it, Foundry "
        "returns 403.",
    ),
    "agent_identity": Doc(
        "Agent identity concepts in Microsoft Foundry",
        AGENT_IDENTITY,
        "For hosted agents, the token service lets your code obtain tokens for the current agent instance's identity "
        "by default.",
        "Inside a hosted agent, tokens are for the agent's own identity by default, not the end user's.",
    ),
    "a2a_identity": Doc(
        "Agent identity concepts in Microsoft Foundry: Supported tools",
        f"{AGENT_IDENTITY}#supported-tools",
        "Application-only access with the calling agent's identity, or delegated access when the target supports it. "
        "(Agent-to-agent (A2A) row)",
        "Agent-to-agent calls use the calling agent's identity unless the target supports delegated access.",
    ),
    "mcp_tool": Doc(
        "Connect agents to MCP server endpoints",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol",
        "Use an allow list of tools by using allowed_tools.",
        "`allowed_tools` limits which tools of a remote MCP server the agent can call.",
    ),
    "mcp_auth_obo": Doc(
        "Set up MCP server authentication: Supported authentication methods",
        f"{MCP_AUTH}#supported-authentication-methods",
        "Use UserEntraToken or AgentOnBehalfOfUser with a supported MCP tool to access a downstream resource in the "
        "incoming user's context. (Microsoft Entra client user delegation row)",
        "To reach a downstream service as the end user, use UserEntraToken or AgentOnBehalfOfUser on a supported "
        "MCP tool.",
    ),
    "mcp_auth_passthrough": Doc(
        "Set up MCP server authentication: OAuth identity passthrough",
        f"{MCP_AUTH}#oauth-identity-passthrough",
        "When you use OAuth identity passthrough, Agent Service generates a consent link the first time a particular "
        "user needs to authorize access.",
        "OAuth identity passthrough asks each user for consent the first time it's used.",
    ),
    "learn_mcp": Doc(
        "Microsoft Learn MCP Server overview",
        "https://learn.microsoft.com/training/support/mcp",
        "There's no authentication required to access the Microsoft Learn MCP Server.",
        "The public Microsoft Learn MCP server needs no credentials, so it can't demonstrate user-level access.",
    ),
    "toolbox_auth": Doc(
        "What is Toolbox in Microsoft Foundry?",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/toolbox-overview",
        "The toolbox handles credential injection, token refresh, and policy enforcement at runtime by using "
        "Microsoft Entra ID and OAuth identity passthrough, so consuming agents don't manage per-tool credentials.",
        "The toolbox injects and refreshes tool credentials, so agent code never handles them.",
    ),
    "foundry_rbac": Doc(
        "Hosted agent permissions reference: Roles in this article",
        "https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions#roles-in-this-article",
        "Foundry User: Create agents, perform model inference, and interact with agents. (roles table)",
        "Anyone with the Foundry User role on the project can call the project's agents directly.",
    ),
    "maf_function_tools": Doc(
        "Using function tools with an agent (Microsoft Agent Framework)",
        "https://learn.microsoft.com/agent-framework/agents/tools/function-tools",
        "Function tools are just custom code that you want the agent to be able to call when needed.",
        "Function tools are ordinary code the model can choose to call. The Supervisor routes with them.",
    ),
    "maf_orchestration": Doc(
        "Workflow orchestrations in Agent Framework",
        "https://learn.microsoft.com/agent-framework/workflows/orchestrations/",
        "Magentic: A manager agent dynamically coordinates specialized agents. (patterns table)",
        "Agent Framework also offers built-in multi-agent patterns, such as magentic, as alternatives.",
    ),
    # Story 3 sources, verified on learn.microsoft.com on 2026-10-27.
    "history_hydration": Doc(
        "Hosted agent runtime contract: Responses protocol",
        f"{CONTRACT}#responses-protocol",
        "Hydrated automatically by the SDK adapter when `conversation.id` is present. (Responses protocol table, "
        "Conversation history row)",
        "Each agent's SDK adapter loads the history of the conversation ID it is called with, and no other.",
    ),
    "conversation_session": Doc(
        "Hosted agents in Foundry Agent Service: Sessions, conversations, and the state store",
        f"{HOSTED}#sessions-conversations-and-the-state-store",
        "The platform manages conversation history automatically and associates a session ID with each conversation.",
        "Foundry stores each conversation and its session. Linking one agent's conversation to another agent's is "
        "not described, so that mapping is application code.",
    ),
    "state_store_users": Doc(
        "Hosted agents in Foundry Agent Service: Sessions, conversations, and the state store",
        f"{HOSTED}#sessions-conversations-and-the-state-store",
        "A store can also partition its items per end user, so one store name is safe to share across the users of "
        "a multitenant agent.",
        "The Supervisor keeps its conversation mappings in a per-user partition of one store.",
    ),
    "conversation_privacy": Doc(
        "Isolate hosted agent sessions per user: Understand per-user isolation",
        "https://learn.microsoft.com/azure/foundry/agents/how-to/isolate-sessions-per-user"
        "#understand-per-user-isolation",
        "One user can't read or list another user's conversations.",
        "Conversations are private to the caller that owns them. Story 3 tests what that means when the caller is "
        "an agent identity.",
    ),
}

Outcome = Literal["PASS", "FAIL", "PARTIAL", "BLOCKED", "NOT SUPPORTED", "LIMITATION", "DOCUMENTED", "NOT RUN"]

# Icon and plain-language meaning of each finding outcome, in legend order.
OUTCOMES: dict[Outcome, tuple[str, str]] = {
    "PASS": ("✅", "Tested in this run and worked as expected."),
    "FAIL": ("❌", "Tested in this run and did not work as expected. The finding says what differed."),
    "PARTIAL": ("🟡", "Some parts worked and some did not. The finding says which."),
    "BLOCKED": ("⛔", "Could not be tested because a prerequisite or guard stopped it. The finding says why."),
    "NOT SUPPORTED": (
        "🚫",
        "The platform does not provide this. Microsoft documents it, this run confirmed it, or both.",
    ),
    "LIMITATION": ("⚠️", "Works, but with a platform constraint you must design around."),
    "DOCUMENTED": ("📄", "Stated by Microsoft and not tested in this run."),
    "NOT RUN": ("⏭️", "Testable, but not run this time (option turned off or the run stopped earlier)."),
}


def outcome_label(outcome: Outcome) -> str:
    return f"{OUTCOMES[outcome][0]} {outcome}"


def checks_outcome(*statuses: str | None) -> Outcome:
    """Finding outcome from the check statuses (PASS/FAIL/BLOCKED/SKIP) it depends on; missing means not run."""
    if statuses and all(status == "PASS" for status in statuses):
        return "PASS"
    if "PASS" in statuses:
        return "PARTIAL"
    if "FAIL" in statuses:
        return "FAIL"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    return "NOT RUN"


@dataclass(frozen=True)
class Finding:
    """One plain-language finding: its outcome, what was found, evidence basis, ownership, and documentation."""

    finding: str
    result: str
    basis: str
    ownership: str
    docs: tuple[str, ...] = ()
    outcome: Outcome = field(kw_only=True)


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


VERDICTS = (
    "**Met** = the acceptance criterion is satisfied. **Met, with ...** = satisfied, with the caveat stated in the "
    "verdict. **Partially met** = some required checks failed. **Not met** = the criterion is not satisfied, or an "
    "agent changed unexpectedly. **Not evaluated** = the run was blocked before it could produce evidence."
)
EVIDENCE = (
    "**Tested** = verified by this run. **Observed** = read from the platform during this run. **Documented** = "
    "stated by Microsoft, not tested here. **Unverified** = neither tested nor documented. Other text explains "
    "how far it was verified."
)
OWNERSHIP = (
    "**Foundry-managed** = Microsoft operates it. **Agent Framework / SDK-managed** = provided by the SDK or "
    "framework. **Custom implementation (Dev Team)** = code in this repository that you own. **Not supported** = "
    "no platform capability exists."
)


def legend() -> str:
    """How to read the report: verdicts, finding outcomes, evidence, and ownership."""
    rows = [[outcome_label(name), meaning] for name, (_, meaning) in OUTCOMES.items()]
    return "\n\n".join(
        [
            "## How to read this report",
            f"**Acceptance verdict:** {VERDICTS}",
            "**Finding outcome:**",
            _table(["Outcome", "Meaning"], rows),
            f"**Evidence:** {EVIDENCE}",
            f"**Owner:** {OWNERSHIP}",
            "**Microsoft documentation:** every link was checked against the quoted text. "
            f"*{NO_DOC}* means no Microsoft page covers it; the finding then relies on this run's evidence.",
        ]
    )


def outcome_counts(findings: Sequence[Finding]) -> str:
    counts = Counter(item.outcome for item in findings)
    return " · ".join(f"{outcome_label(name)}: {counts[name]}" for name in OUTCOMES if counts[name])


def render_summary(
    verdict: str,
    conclusion: str,
    results: Sequence[str],
    findings: Sequence[Finding],
    gaps: Sequence[str],
) -> str:
    """Key first, then the summary: what was concluded, measured, and meant, and where Microsoft documents it."""
    used = list(dict.fromkeys(key for finding in findings for key in finding.docs))
    parts = [
        legend(),
        "## Summary",
        f"**Acceptance verdict: {verdict}.** {conclusion}".strip(),
        "### Key results",
        "\n".join(f"- {line}" for line in results) or "- No results were recorded.",
        "### Findings",
        f"**Outcomes:** {outcome_counts(findings)}" if findings else "No findings were recorded.",
        _table(
            ["#", "Outcome", "Finding", "What was found", "Evidence", "Owner", "Microsoft documentation"],
            [
                [
                    str(number),
                    outcome_label(item.outcome),
                    item.finding,
                    item.result,
                    item.basis,
                    item.ownership,
                    doc_cell(item.docs),
                ]
                for number, item in enumerate(findings, start=1)
            ],
        ),
    ]
    if gaps:
        parts += ["### Gaps and follow-ups", "\n".join(f"- {gap}" for gap in gaps)]
    if used:
        numbers = {
            key: ", ".join(str(number) for number, item in enumerate(findings, start=1) if key in item.docs)
            for key in used
        }
        parts += [
            "### Microsoft documentation referenced",
            "Each row quotes the Microsoft page, explains what it means for this POC, and lists the numbers of the "
            "findings above that it supports.",
            _table(
                ["Microsoft page", "What Microsoft says", "What it means here", "Supports finding"],
                [[DOCS[key].link(), f'"{DOCS[key].quote}"', DOCS[key].meaning, numbers[key]] for key in used],
            ),
        ]
    parts += ["---", "_Detailed evidence for every check follows below._"]
    return "\n\n".join(parts) + "\n"
