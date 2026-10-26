"""Story 4 Steps 3-6: Research tool call, identity paths, trace with allowed/denied access, and Data Analysis.

All steps reuse the Story 4 Step 1 baseline read-only; none deploys, grants a role, or changes an agent.
Identities are compared only as salted fingerprints in memory and never written to evidence.
"""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from app.core.redaction import sanitize_text
from app.services.orchestration import DelegateCommand, DelegationReport, IdentityMode
from pydantic import JsonValue

from scripts.common.findings import Finding
from scripts.common.reporting import Status, StepSpec
from scripts.deploy.grant_local_rbac import CliError, object_value
from scripts.run_scenario.story1_data_analysis import (
    OUTPUT_FILES,
    aggregates_match,
    analysis_prompt,
    bearer,
    check_by_category_csv,
    download_files,
    fenced_json,
    mcp_result,
    png_dimensions,
    result_text,
    select_container,
)
from scripts.run_scenario.story4_common import (
    ALLOWED_MCP_TOOL,
    BASELINE_FILE,
    CUSTOM,
    DATA_ANALYSIS,
    EXCLUDED_MCP_TOOL,
    FOUNDRY,
    LEARN_MCP_URL,
    NOT_SUPPORTED,
    RESEARCH,
    RESEARCH_TOOLBOX,
    SDK,
    STORY1_DIR,
    SUPERVISOR,
    TASK_PREFIX,
    Story4Step,
    classify_user,
    identity_candidates,
    impersonation_granted,
    marker,
    role_names,
    token_claims,
)

TOOL_TASK = (
    "Use your Microsoft Learn documentation search tool to find the official Microsoft Learn page that explains "
    "hosted agents in Microsoft Foundry Agent Service. Reply with the page title and its learn.microsoft.com URL, "
    "and end your answer with the exact token {token}."
)
IDENTITY_TASK = "Reply with exactly: {token}"
DOCUMENTED_403_GAP = (
    "Discrepancy with the documentation: Foundry accepted `x-ms-user-identity` from the Supervisor although it holds "
    "no role with UserIdentityImpersonation (documented outcome: HTTP 403), and Research saw no user context. This "
    "matches the earlier nested-path delegated-label findings in ADR-0001; raise it with Foundry support before "
    "relying on either outcome."
)
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def tool_call_ok(report: DelegationReport | None) -> tuple[bool, list[dict[str, object]]]:
    """Research made the allow-listed MCP search call and it succeeded."""
    calls = report.sub_agent.tool_calls if report and report.sub_agent else []
    observed: list[dict[str, object]] = [{"name": call.name, "succeeded": call.succeeded} for call in calls]
    return any(ALLOWED_MCP_TOOL in call.name and call.succeeded for call in calls), observed


def mcp_call(endpoint: str, token: str, identifier: int, method: str, params: dict[str, JsonValue]) -> dict[str, Any]:
    """One JSON-RPC request to a toolbox MCP endpoint with the operator's own token."""
    with httpx.Client(timeout=120, trust_env=False) as client:
        body = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        return mcp_result(client.post(endpoint, headers={**bearer(token), **MCP_HEADERS}, json=body))


def tool_names(result: Mapping[str, JsonValue]) -> list[str]:
    tools = result.get("tools")
    return [str(t.get("name")) for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []


def research_toolbox_endpoint(output_dir: Path) -> str:
    data = json.loads((output_dir / BASELINE_FILE).read_text(encoding="utf-8"))
    return str(data["research_toolbox_endpoint"])


def outcome(report: DelegationReport | None) -> str:
    """Allowed, denied by HTTP status, or another failure; never an inferred success."""
    if report is None:
        return "no report"
    if report.status == "completed":
        return "allowed"
    if report.status == "denied":
        return "denied by Supervisor allow-list"
    failure = report.failure
    if failure and failure.http_status in (401, 403):
        return f"denied (HTTP {failure.http_status}{', ' + failure.code if failure.code else ''})"
    return f"{report.status}: {failure.message[:120] if failure else 'unknown'}"


TRUST_ROWS: list[list[object]] = [
    [
        "Agent-to-agent",
        "Supervisor container (its managed identity via `DefaultAzureCredential`)",
        "Research agent endpoint (Responses protocol)",
        "Entra token for `https://ai.azure.com`; Supervisor identity needs a project role with agent-invoke data "
        "actions (Foundry User); `x-ms-user-identity` additionally needs `UserIdentityImpersonation`",
        "Supervisor -> Foundry endpoint -> Research container; the platform authenticates the Supervisor, not the "
        "end user. Optional `x-agent-foundry-call-id` forwards the original call context",
    ],
    [
        "Agent-to-service",
        "Research container (its agent identity via `FoundryToolbox`)",
        f"Foundry toolbox `{RESEARCH_TOOLBOX}` MCP endpoint",
        "Entra token for `https://ai.azure.com`; Research identity needs Foundry User on the project",
        "Research -> Foundry toolbox service; `FoundryToolbox` also forwards `x-agent-foundry-call-id` so Foundry can "
        "resolve the caller context (SDK behavior; not observable from the client)",
    ],
    [
        "MCP tool call",
        "Foundry toolbox service (on behalf of Research)",
        f"Remote MCP server {LEARN_MCP_URL}",
        "None: public, unauthenticated server; the toolbox `allowed_tools` filter limits callable tools",
        "Foundry -> public internet; results return to Research and its model as untrusted tool output",
    ],
]


class Step3(Story4Step):
    """Research calls its MCP tool through a Foundry toolbox, invoked through the Supervisor."""

    number = 3
    spec = StepSpec(4, number, "research-tool-call")
    title = "Story 4 / Step 3 - Research Agent tool call and trust boundaries"
    task_label = f"{TASK_PREFIX} - 3 Demonstrate Research tool call"
    ac = (
        "Demonstrate a Research Agent tool call. Define the caller, target resource, required permissions, and "
        "trust boundaries for representative agent-to-service, agent-to-agent, and MCP tool calls."
    )
    scope_note = (
        f"Reuses the Step 1 baseline. Research's toolbox `{RESEARCH_TOOLBOX}` exposes one tool "
        f"(`{ALLOWED_MCP_TOOL}`) from the public Microsoft Learn MCP server. Queries contain no customer data."
    )

    def body(self) -> None:
        endpoint = research_toolbox_endpoint(self.args.output_dir)
        self.report.heading("Toolbox definition (read-only)")
        version = endpoint.split("/versions/", 1)[1].split("/", 1)[0]
        versions = [object_value(i.as_dict()) for i in self.project.toolboxes.list_versions(RESEARCH_TOOLBOX)]
        definition = next((item for item in versions if str(item.get("version")) == version), {})
        tools = definition.get("tools")
        summary = [
            {key: tool.get(key) for key in ("type", "server_label", "server_url", "allowed_tools", "require_approval")}
            for tool in (tools if isinstance(tools, list) else [])
            if isinstance(tool, dict)
        ]
        self.check(
            "Research toolbox definition",
            len(summary) == 1 and summary[0].get("server_url") == LEARN_MCP_URL,
            f"One MCP tool for {LEARN_MCP_URL}, allowed_tools [{ALLOWED_MCP_TOOL}]",
            {"version": version, "tools": summary},
        )
        self.report.heading("Direct toolbox probe (operator identity)")
        token = self.token()
        names = tool_names(mcp_call(endpoint, token, 1, "tools/list", {}))
        search = next((name for name in names if ALLOWED_MCP_TOOL in name), None)
        self.check("tools/list", search is not None, f"Lists {ALLOWED_MCP_TOOL}", names)
        if search:
            result = mcp_call(
                endpoint, token, 2, "tools/call", {"name": search, "arguments": {"query": "hosted agents"}}
            )
            text = result_text(result)
            self.check(
                "Toolbox reaches the MCP server",
                "learn.microsoft.com" in text and not result.get("isError"),
                "Search results with learn.microsoft.com URLs",
                {"is_error": result.get("isError"), "excerpt": sanitize_text(text)[:300]},
            )
        self.report.heading("Hosted tool call: user -> Supervisor -> Research -> toolbox -> MCP")
        mark = marker("S4T")
        command = DelegateCommand(task=TOOL_TASK.format(token=mark), correlation_id=uuid4(), target="research")
        self.report.code(command.task, "text")
        exchange, report = self.delegate("AC3 Research tool call via Supervisor", command)
        ok, calls = tool_call_ok(report)
        self.obs["tool_calls"] = calls
        self.check(
            "Research tool call", ok, f"Research called {ALLOWED_MCP_TOOL} successfully", calls or exchange.observed()
        )
        answer = report.sub_agent.answer if report and report.sub_agent else ""
        self.check(
            "Tool result used in the answer",
            "learn.microsoft.com" in answer and report is not None and mark in report.final_answer,
            "Research cites a learn.microsoft.com URL; the Supervisor delivers it with the token",
            {"answer_excerpt": answer[:300]},
        )
        self.report.heading("Caller, target, permissions, and trust boundaries")
        self.report.table(
            ["Call type", "Caller", "Target resource", "Required permissions", "Trust boundary"], TRUST_ROWS
        )

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        if (
            self.statuses.get("Research tool call") == "PASS"
            and self.statuses.get("Tool result used in the answer") == "PASS"
        ):
            return "Met"
        return "Partially met" if self.statuses.get("Toolbox reaches the MCP server") == "PASS" else "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        results = [f"Research tool calls observed: {self.obs.get('tool_calls') or 'none'}."]
        findings = [
            Finding(
                "Research calls an MCP tool through a Foundry toolbox with its own agent identity",
                "Succeeded" if self.statuses.get("Research tool call") == "PASS" else "See checks",
                "Tested",
                f"{FOUNDRY} (toolbox, MCP proxy); {SDK} (FoundryToolbox client)",
                ("toolbox_auth", "agent_identity"),
            ),
            Finding(
                "The toolbox `allowed_tools` filter limits which remote MCP tools are exposed",
                "Applied" if self.statuses.get("tools/list") == "PASS" else "See checks",
                "Tested",
                FOUNDRY,
                ("mcp_tool",),
            ),
            Finding(
                "Remote MCP server reached anonymously; the public Microsoft Learn server needs no authentication",
                "Observed",
                "Tested",
                FOUNDRY,
                ("learn_mcp",),
            ),
            Finding(
                "Agent-to-agent calls authenticate the calling agent, not the end user",
                "See Step 4 and Step 5",
                "Documented",
                FOUNDRY,
                ("agent_identity",),
            ),
        ]
        gaps = [
            "Authenticated MCP servers (key, agent identity, or OAuth passthrough) were not configured.",
            "Tool output is untrusted input to the model; no content filtering beyond the platform's was added.",
        ]
        conclusion = (
            "Research called its Microsoft Learn MCP tool through the Foundry toolbox and cited the result."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


class Step4(Story4Step):
    """Agent-owned, forwarded call context, and delegated-user paths to another agent and a service."""

    number = 4
    spec = StepSpec(4, number, "identity-paths")
    title = "Story 4 / Step 4 - Delegated-user/OBO and agent-owned access"
    task_label = f"{TASK_PREFIX} - 4 Evaluate identity paths"
    ac = (
        "Evaluate delegated-user/OBO and agent-owned access to another service or agent. Demonstrate available "
        "paths and record unavailable or untested combinations, without selecting a final identity architecture."
    )
    scope_note = (
        "Reuses the Step 1 baseline. Grants nothing: the delegated-user expectation follows the Supervisor's "
        "current role assignments, read before the test. Identities appear only as classified fingerprints."
    )

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.paths: dict[str, str] = {}
        self.granted: bool | None = None

    def body(self) -> None:
        supervisor_principal = str(
            object_value(self.records[SUPERVISOR].get("instance_identity") or {}).get("principal_id")
        )
        self.report.heading("Supervisor impersonation permission (read-only)")
        try:
            self.granted, roles = impersonation_granted(self.context, supervisor_principal)
            self.record(
                "Supervisor impersonation permission",
                "PASS",
                "Read the Supervisor's roles that grant UserIdentityImpersonation (no change)",
                {"granted": self.granted, "granting_roles": roles},
            )
        except (CliError, ValueError, KeyError) as error:
            self.record("Supervisor impersonation permission", "BLOCKED", "Readable role assignments", str(error))
        candidates = identity_candidates(token_claims(self.token()), self.records[SUPERVISOR])
        rows: list[list[object]] = []
        modes: tuple[IdentityMode, ...] = ("agent", "call-context", "delegated-user")
        for mode in modes:
            mark = marker("S4I")
            correlation = uuid4()
            command = DelegateCommand(
                task=IDENTITY_TASK.format(token=mark), correlation_id=correlation, target="research", identity=mode
            )
            _, report = self.delegate(f"AC4 Supervisor -> Research ({mode})", command)
            seen = outcome(report)
            self.paths[mode] = seen
            expected = self.expected(mode)
            principal = research_principal(report, correlation, candidates)
            self.check(
                f"Supervisor -> Research ({mode})",
                expected is not None and seen.startswith(expected),
                f"{expected or 'unknown (permission unreadable)'}",
                {"outcome": seen, "research_sees": principal, "headers_sent": headers_of(report)},
            )
            rows.append([mode, headers_of(report), seen, principal])
        self.report.table(
            ["Identity mode", "Headers sent by Supervisor", "Outcome", "User context Research sees"], rows
        )
        self.report.heading("Agent-owned access to a service: Research -> toolbox MCP")
        command = DelegateCommand(
            task=TOOL_TASK.format(token=marker("S4S")), correlation_id=uuid4(), target="research", identity="agent"
        )
        _, report = self.delegate("AC4 agent-owned Research -> toolbox", command)
        ok, calls = tool_call_ok(report)
        self.paths["service"] = "allowed" if ok else outcome(report)
        self.check("Research -> toolbox (agent-owned)", ok, "Tool call succeeds with Research's own identity", calls)
        self.report.heading("Combinations")
        self.report.table(
            ["Target", "Agent-owned identity", "Forwarded call context", "Delegated user label", "OAuth OBO"],
            [
                [
                    "Another agent (Research)",
                    self.paths.get("agent", "-"),
                    self.paths.get("call-context", "-"),
                    self.paths.get("delegated-user", "-"),
                    "Untested: documented for A2A tool targets (`AgentOnBehalfOfUser`, preview); this POC uses "
                    "Responses endpoints, which authenticate the calling agent",
                ],
                [
                    "Another service (toolbox MCP)",
                    self.paths.get("service", "-"),
                    "Automatic: FoundryToolbox forwards `x-agent-foundry-call-id` on every tool call (SDK behavior)",
                    "Not applicable: `x-ms-user-identity` applies to agent endpoints, not toolbox calls",
                    "Untested: Microsoft Entra client user delegation (`UserEntraToken`/`AgentOnBehalfOfUser`) or "
                    "OAuth identity passthrough need a supported MCP target, connection, and consent",
                ],
            ],
        )

    def expected(self, mode: str) -> str | None:
        if mode != "delegated-user":
            return "allowed"
        if self.granted is None:
            return None
        return "allowed" if self.granted else "denied (HTTP 403"

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        names = [f"Supervisor -> Research ({mode})" for mode in ("agent", "call-context", "delegated-user")]
        names.append("Research -> toolbox (agent-owned)")
        passed = [self.statuses.get(name) == "PASS" for name in names]
        return "Met" if all(passed) else ("Partially met" if any(passed) else "Not met")

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        results = [f"{mode}: {seen}" for mode, seen in self.paths.items()]
        findings = [
            Finding(
                "Agent-owned access: Supervisor -> Research and Research -> toolbox with each agent's own identity",
                self.paths.get("agent", "not run"),
                "Tested",
                FOUNDRY,
                ("agent_identity", "foundry_rbac"),
            ),
            Finding(
                "Forwarded original call context (`x-agent-foundry-call-id`)",
                self.paths.get("call-context", "not run"),
                "Tested",
                f"{FOUNDRY} (validation); {CUSTOM} (forwarding)",
                ("hosted_contract",),
            ),
            Finding(
                "Delegated user (`x-ms-user-identity`) requires UserIdentityImpersonation for the calling agent",
                self.paths.get("delegated-user", "not run"),
                "Tested",
                FOUNDRY,
                ("impersonation",),
            ),
            Finding(
                "Agent acting on behalf of the user to another agent (A2A `AgentOnBehalfOfUser`, preview)",
                "Not configured; A2A is not used in this POC",
                "Documented",
                FOUNDRY,
                ("a2a_identity",),
            ),
            Finding(
                "User-delegated access to a downstream service through an MCP tool (Entra client user delegation or "
                "OAuth identity passthrough)",
                "Not configured; the public Learn MCP server needs no user context",
                "Documented",
                FOUNDRY,
                ("mcp_auth_obo", "mcp_auth_passthrough"),
            ),
        ]
        gaps = [
            "No final identity architecture is selected; this step records available and unavailable paths only.",
            "Granting UserIdentityImpersonation (Access: Apply Supervisor impersonation permission) changes the "
            "delegated-user expectation; it was not granted here.",
        ]
        if self.granted is False and self.paths.get("delegated-user") == "allowed":
            gaps.insert(0, DOCUMENTED_403_GAP)
        return "Available identity paths were exercised; unavailable combinations are listed.", results, findings, gaps


def headers_of(report: DelegationReport | None) -> list[str]:
    return list(report.invocation.headers_sent) if report and report.invocation else []


def research_principal(report: DelegationReport | None, correlation: UUID, candidates: Mapping[str, list[str]]) -> str:
    """Classify the user context Research observed against the Supervisor hop, label, and known principals."""
    if report is None or report.sub_agent is None:
        return "not observed"
    return classify_user(
        report.sub_agent.context.user_fingerprint,
        str(correlation),
        candidates,
        {
            "original user (same as Supervisor hop)": report.supervisor.user_fingerprint,
            "delegated application-user label": report.delegated_label_fingerprint,
        },
    )


class Step5(Story4Step):
    """Trace identities and correlation across client, Supervisor, Research, and tool; allowed and denied cases."""

    number = 5
    spec = StepSpec(4, number, "trace-and-access")
    title = "Story 4 / Step 5 - Identity trace and allowed/denied access"
    task_label = f"{TASK_PREFIX} - 5 Trace identity and access"
    ac = (
        "Trace the original user, authenticated principal, and applied permissions across client, supervisor, "
        "sub-agent, and tool; identify custom propagation/correlation. Exercise representative allowed/denied "
        "access and identify the enforcing component and observed outcome."
    )
    scope_note = (
        "Reuses the Step 1 baseline; grants nothing. A client-generated correlation ID travels in the body and the "
        "`x-client-correlation-id` header. Each hop returns only salted identity fingerprints and presence flags."
    )

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.cases: dict[str, bool] = {}

    def roles(self, principal: str | None) -> str:
        if not principal:
            return "unknown principal"
        try:
            return ", ".join(role_names(self.context, principal)) or "none at this project"
        except (CliError, ValueError, KeyError) as error:
            return f"unreadable ({sanitize_text(str(error))[:80]})"

    def body(self) -> None:
        claims = token_claims(self.token())
        candidates = identity_candidates(claims, self.records[SUPERVISOR])

        def principal_of(agent: str) -> str | None:
            identity = self.records[agent].get("instance_identity")
            value = identity.get("principal_id") if isinstance(identity, dict) else None
            return str(value) if value else None

        oid = claims.get("oid")
        operator_roles = self.roles(oid if isinstance(oid, str) else None)
        supervisor_roles = self.roles(principal_of(SUPERVISOR))
        research_roles = self.roles(principal_of(RESEARCH))
        self.report.heading("Allowed, traced request: client -> Supervisor -> Research -> toolbox -> MCP")
        correlation = uuid4()
        mark = marker("S4C")
        command = DelegateCommand(task=TOOL_TASK.format(token=mark), correlation_id=correlation, target="research")
        exchange, report = self.delegate("AC5 traced request (call-context)", command)
        salt = str(correlation)
        sup = report.supervisor if report else None
        sub = report.sub_agent.context if report and report.sub_agent else None
        supervisor_user = classify_user(sup.user_fingerprint if sup else None, salt, candidates)
        research_user = research_principal(report, correlation, candidates)
        tool_ok, calls = tool_call_ok(report)
        self.report.table(
            [
                "Hop",
                "Authenticated caller",
                "Original user visible to the target",
                "Call context",
                "Correlation propagated",
                "Permissions applied",
                "Enforcing component",
                "Outcome",
            ],
            [
                [
                    "Client -> Supervisor",
                    "Operator (Entra user token from Azure CLI)",
                    supervisor_user,
                    "Created by Foundry" if sup and sup.call_id_present else "absent",
                    "header matched" if sup and sup.correlation_header_matches else "no",
                    f"Operator: {operator_roles}",
                    "Foundry agent endpoint (Entra + RBAC)",
                    exchange.status,
                ],
                [
                    "Supervisor -> Research",
                    "Supervisor managed identity",
                    research_user,
                    "forwarded (`x-agent-foundry-call-id`)" if sub and sub.call_id_present else "absent",
                    "header and body matched" if sub and sub.correlation_header_matches else "no",
                    f"Supervisor: {supervisor_roles}",
                    "Supervisor allow-list (custom), then Foundry agent endpoint (Entra + RBAC)",
                    report.invocation.status if report and report.invocation else "-",
                ],
                [
                    "Research -> toolbox",
                    "Research agent identity (FoundryToolbox)",
                    "Resolved by Foundry from the forwarded call ID (documented; not observable here)",
                    "forwarded automatically by FoundryToolbox",
                    "not propagated (no custom header)",
                    f"Research: {research_roles}",
                    "Foundry toolbox (Entra + RBAC, allowed_tools)",
                    "tool call succeeded" if tool_ok else f"tool calls: {calls}",
                ],
                [
                    "Toolbox -> Microsoft Learn MCP",
                    "Foundry toolbox service (anonymous to a public server)",
                    "No",
                    "no",
                    "no",
                    "None required (public server)",
                    "Remote MCP server",
                    "results returned" if tool_ok else "-",
                ],
            ],
        )
        self.cases["Trace complete"] = self.check(
            "Trace: original user and correlation",
            bool(
                sup
                and sup.user_id_present
                and sup.correlation_header_matches
                and sub
                and sub.correlation_header_matches
            ),
            "Supervisor sees the original user; correlation ID matches at Supervisor and Research",
            {"supervisor_user": supervisor_user, "research_user": research_user, "tool_calls": calls},
        )
        self.cases["Allowed request"] = self.check(
            "Allowed: operator -> Supervisor -> Research -> tool",
            bool(report and report.status == "completed" and tool_ok),
            "Completed with a successful tool call",
            {"outcome": outcome(report)},
        )
        self.report.heading("Denied access")
        self.denials()

    def denials(self) -> None:
        rows: list[list[object]] = []
        supervisor_principal = self.records[SUPERVISOR].get("instance_identity")
        principal = supervisor_principal.get("principal_id") if isinstance(supervisor_principal, dict) else None
        try:
            granted: bool | None = impersonation_granted(self.context, str(principal))[0]
        except CliError, ValueError, KeyError:
            granted = None
        command = DelegateCommand(
            task=IDENTITY_TASK.format(token=marker("S4D")),
            correlation_id=uuid4(),
            target="research",
            identity="delegated-user",
        )
        _, report = self.delegate("AC5 delegated-user header (impersonation enforcement)", command)
        seen = outcome(report)
        expected = "allowed" if granted else "denied (HTTP 403"
        status: Status = "BLOCKED" if granted is None else ("PASS" if seen.startswith(expected) else "FAIL")
        self.record(
            "Platform enforcement: delegated user header", status, expected, {"outcome": seen, "granted": granted}
        )
        self.cases["Platform denial"] = status == "PASS"
        self.obs["documented_403_not_observed"] = granted is False and seen == "allowed"
        rows.append(
            [
                "Supervisor sends `x-ms-user-identity`",
                "Supervisor managed identity",
                "Research",
                expected,
                seen,
                "Foundry agent endpoint (RBAC: UserIdentityImpersonation)",
                FOUNDRY,
            ]
        )
        command = DelegateCommand(task="Story 4 denial probe", correlation_id=uuid4(), target="billing-agent")
        _, report = self.delegate("AC5 denied: target outside the allow-list", command)
        ok = report is not None and report.status == "denied" and report.invocation is None
        self.cases["Application denial"] = self.check(
            "Application enforcement: Supervisor allow-list",
            ok,
            "Denied by the Supervisor before any outbound call",
            {"outcome": outcome(report)},
        )
        rows.append(
            [
                "Client asks for `billing-agent`",
                "Operator",
                "Unlisted agent",
                "denied",
                outcome(report),
                "Supervisor application allow-list",
                CUSTOM,
            ]
        )
        endpoint = research_toolbox_endpoint(self.args.output_dir)
        token = self.token()
        names = tool_names(mcp_call(endpoint, token, 1, "tools/list", {}))
        search = next((name for name in names if ALLOWED_MCP_TOOL in name), ALLOWED_MCP_TOOL)
        excluded = search.replace(ALLOWED_MCP_TOOL, EXCLUDED_MCP_TOOL)
        try:
            result = mcp_call(endpoint, token, 2, "tools/call", {"name": excluded, "arguments": {"url": LEARN_MCP_URL}})
            denied = bool(result.get("isError"))
            detail = sanitize_text(result_text(result))[:200]
        except (ValueError, httpx.HTTPError) as error:
            denied, detail = True, sanitize_text(str(error))[:200]
        listed = any(EXCLUDED_MCP_TOOL in name for name in names)
        self.cases["Tool denial"] = self.check(
            "Platform enforcement: toolbox allowed_tools",
            denied and not listed,
            f"{EXCLUDED_MCP_TOOL} is not listed and its call is rejected",
            {"listed": listed, "rejected": denied, "detail": detail},
        )
        rows.append(
            [
                f"Operator calls `{EXCLUDED_MCP_TOOL}`",
                "Operator",
                "Toolbox MCP",
                "denied",
                "denied" if denied else "allowed",
                "Foundry toolbox `allowed_tools`",
                FOUNDRY,
            ]
        )
        rows.append(
            [
                "User B reads user A's state/history",
                "Second Entra user",
                "Research",
                "denied",
                "See Agents: Test native Entra user isolation (#3)",
                "Foundry State Store / Responses",
                FOUNDRY,
            ]
        )
        self.report.table(
            ["Case", "Caller", "Target", "Expected", "Observed", "Enforcing component", "Classification"], rows
        )

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        if all(
            self.cases.get(name) for name in ("Trace complete", "Allowed request", "Application denial", "Tool denial")
        ):
            return "Met" if self.cases.get("Platform denial") else "Partially met"
        return "Partially met" if any(self.cases.values()) else "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        results = [f"{name}: {'yes' if ok else 'no'}" for name, ok in self.cases.items()]
        findings = [
            Finding(
                "The original user is visible to the Supervisor; the downstream hop authenticates the Supervisor",
                "Observed (see trace table)",
                "Tested",
                FOUNDRY,
                ("hosted_contract", "hosted_user_header", "agent_identity"),
            ),
            Finding(
                "Correlation across hops uses a client-generated ID in the body and `x-client-correlation-id`",
                "Propagated" if self.cases.get("Trace complete") else "See checks",
                "Tested",
                CUSTOM,
                ("client_headers",),
            ),
            Finding(
                "Unlisted targets are denied by the Supervisor before any outbound call",
                "Denied" if self.cases.get("Application denial") else "See checks",
                "Tested",
                CUSTOM,
            ),
            Finding(
                "Delegated-user header without UserIdentityImpersonation is rejected by Foundry",
                "Denied" if self.cases.get("Platform denial") else "See checks",
                "Tested",
                FOUNDRY,
                ("impersonation",),
            ),
            Finding(
                "Toolbox `allowed_tools` blocks a non-listed MCP tool",
                "Denied" if self.cases.get("Tool denial") else "See checks",
                "Tested",
                FOUNDRY,
                ("mcp_tool",),
            ),
        ]
        gaps = [
            "W3C trace context and Application Insights correlation across hops were not verified; only Data "
            "Analysis exports telemetry.",
            "Whether Foundry applies the original user's permissions at the toolbox from the forwarded call ID is not "
            "observable with a public, unauthenticated MCP server.",
        ]
        if self.obs.get("documented_403_not_observed"):
            gaps.insert(0, DOCUMENTED_403_GAP)
        return (
            "Identity and correlation were traced across all hops with allowed and denied cases.",
            results,
            findings,
            gaps,
        )


INDEX_LINE = re.compile(
    r"^- (?P<when>.+?) UTC - (?P<label>.+?) - \*\*(?P<verdict>.+?)\*\* - \[(?P<name>[^\]]+)\]\((?P<link>[^)]+)\)"
)


def story1_evidence(index: Path) -> dict[str, dict[str, str]]:
    """Latest Story 1 index entry per step label prefix (``Step 1`` ... ``Step 5``)."""
    latest: dict[str, dict[str, str]] = {}
    if not index.is_file():
        return latest
    for line in index.read_text(encoding="utf-8").splitlines():
        match = INDEX_LINE.match(line.strip())
        if match:
            step = match.group("label").split(" (", 1)[0]
            latest[step] = match.groupdict()
    return latest


class Step6(Story4Step):
    """Data Analysis with managed Code Interpreter through the Supervisor, reusing Story 1 inputs and oracle."""

    number = 6
    spec = StepSpec(4, number, "data-analysis")
    title = "Story 4 / Step 6 - Data Analysis Agent with Code Interpreter (reusing Story 1)"
    task_label = f"{TASK_PREFIX} - 6 Demonstrate Data Analysis via Supervisor"
    ac = (
        "Demonstrate a Data Analysis Agent using Microsoft-managed Code Interpreter, reusing the analysis and "
        "output evidence from #1."
    )
    scope_note = (
        "Reuses the Story 1 toolbox, synthetic 1k-row input, analysis request, and local oracle, and links the "
        "Story 1 evidence. The request goes through the Supervisor (`target: data_analysis`)."
    )

    def body(self) -> None:
        self.report.heading("Story 1 evidence reused")
        story1 = self.args.output_dir.parent / STORY1_DIR
        evidence = story1_evidence(story1 / "index.md")
        rows: list[list[object]] = [
            [
                step,
                item["verdict"],
                item["when"],
                f"[{item['name']}]({Path(os.path.relpath(story1 / item['link'], self.run.path)).as_posix()})",
            ]
            for step, item in sorted(evidence.items())
        ]
        self.report.table(["Story 1 step", "Verdict", "UTC", "Report"], rows or [["-", "-", "-", "No index found"]])
        met = [step for step in ("Step 1", "Step 2") if evidence.get(step, {}).get("verdict") == "Met"]
        self.record(
            "Story 1 evidence reused",
            "PASS" if len(met) == 2 else "BLOCKED",
            "Story 1 Step 1 (Code Interpreter) and Step 2 (analysis and outputs) reports with verdict Met",
            {"found": sorted(evidence), "met": met, "index": str(story1 / "index.md")},
        )
        self.report.heading("Data Analysis wiring (read-only)")
        definition = object_value(object_value(self.records[DATA_ANALYSIS].get("versions") or {}).get("latest") or {})
        environment = object_value(object_value(definition.get("definition") or {}).get("environment_variables") or {})
        wired = environment.get("TOOLBOX_ENDPOINT")
        configured = self.context.get("DATA_ANALYSIS_TOOLBOX_ENDPOINT")
        self.check(
            "Code Interpreter toolbox wired",
            bool(wired) and wired == configured and "/toolboxes/story1-data-analysis-ci/" in str(wired),
            "Active Data Analysis version uses the Story 1 Code Interpreter toolbox",
            {"toolbox_endpoint": wired},
        )
        self.analysis()

    def analysis(self) -> None:
        from scripts.common.synthetic_data import (
            DATASET_SIZES,
            build_dataset,
            expected_aggregates,
            expected_filtered_ids,
            generate_rows,
        )

        dataset = build_dataset(DATASET_SIZES[0])
        rows = list(generate_rows(dataset.rows))
        expected = expected_aggregates(rows)
        ids = expected_filtered_ids(rows)
        self.report.heading("Analysis through the Supervisor (Story 1 Step 2 request)")
        command = DelegateCommand(task=analysis_prompt(dataset), correlation_id=uuid4(), target="data_analysis")
        exchange, report = self.delegate("AC6 Supervisor -> Data Analysis (Code Interpreter)", command)
        sub = report.sub_agent if report else None
        self.check(
            "Supervisor -> Data Analysis delegation",
            bool(report and report.status == "completed" and sub and sub.agent == "data_analysis"),
            "Completed report from the Data Analysis sub-agent",
            {"outcome": outcome(report), **exchange.observed()},
        )
        if not report or not sub:
            return
        self.check(
            "Code Interpreter executed",
            bool(sub.containers) and bool(sub.tool_calls),
            "Tool call made and a sandbox container referenced (from tool results only)",
            {"tool_calls": [c.name for c in sub.tool_calls], "containers": sub.containers},
        )
        printed = fenced_json(report.final_answer)
        values = printed if isinstance(printed, dict) else {}
        self.check(
            "Analysis matches the Story 1 oracle",
            values.get("input_sha256") == dataset.sha256
            and values.get("filtered_rows") == len(ids)
            and aggregates_match(values.get("by_category"), expected),
            "Printed SHA-256, filtered row count, and every category total equal the local calculation",
            {"input_sha256": values.get("input_sha256"), "filtered_rows": values.get("filtered_rows")},
        )
        if not sub.containers:
            return
        token = self.token()
        with httpx.Client(timeout=120, trust_env=False) as client:
            selected = select_container(client, self.endpoint, token, sub.containers, OUTPUT_FILES)
            container, files = selected if selected else (sub.containers[-1], {})
            downloaded = download_files(
                client, self.endpoint, token, container, files, OUTPUT_FILES, self.run.path / "outputs"
            )
        png = downloaded.get("revenue_by_category.png", (b"", None))[0]
        groups = downloaded.get("by_category.csv", (b"", None))[0]
        self.check(
            "Outputs delivered to the user",
            set(OUTPUT_FILES) <= set(downloaded)
            and png_dimensions(png) is not None
            and check_by_category_csv(groups, expected),
            "filtered.csv, by_category.csv, and a valid PNG chart downloaded by the user's own identity",
            {"container": container, "files": {name: len(data) for name, (data, _) in downloaded.items()}},
        )
        self.report.paragraph(
            "Local copies: " + ", ".join(f"[{name}](outputs/{name})" for name in OUTPUT_FILES if name in downloaded)
        )

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        live = (
            "Supervisor -> Data Analysis delegation",
            "Code Interpreter executed",
            "Analysis matches the Story 1 oracle",
            "Outputs delivered to the user",
            "Code Interpreter toolbox wired",
        )
        if all(self.statuses.get(name) == "PASS" for name in live):
            return "Met" if self.statuses.get("Story 1 evidence reused") == "PASS" else "Partially met"
        return "Partially met" if self.statuses.get("Code Interpreter executed") == "PASS" else "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        results = [
            f"Story 1 evidence: {self.statuses.get('Story 1 evidence reused', 'not checked')}.",
            f"Analysis through the Supervisor: {self.statuses.get('Analysis matches the Story 1 oracle', 'not run')}.",
            f"Outputs delivered: {self.statuses.get('Outputs delivered to the user', 'not run')}.",
        ]
        findings = [
            Finding(
                "Data Analysis runs model-written Python in Microsoft-managed Code Interpreter via a Foundry toolbox",
                "Executed" if self.statuses.get("Code Interpreter executed") == "PASS" else "See checks",
                "Tested",
                FOUNDRY,
                ("ci_overview",),
            ),
            Finding(
                "Analysis requested through the Supervisor returns results and container references to the user",
                "Delivered" if self.statuses.get("Outputs delivered to the user") == "PASS" else "See checks",
                "Tested",
                f"{CUSTOM} (delegation report); {FOUNDRY} (container files API)",
                ("ci_files",),
            ),
            Finding(
                "Code Interpreter through a toolbox in a hosted agent has no per-user isolation",
                "Limitation carried over from Story 1",
                "Documented",
                NOT_SUPPORTED,
                ("ci_isolation",),
            ),
        ]
        gaps = [
            "Capacity, sandbox boundaries, and continuity evidence remain in Story 1 Steps 3-5 and are not rerun.",
            "If Story 1 evidence is missing locally, run Story 1 Steps 1-2 for this environment to regenerate it.",
        ]
        return "Code Interpreter analysis was delivered through the Supervisor.", results, findings, gaps
