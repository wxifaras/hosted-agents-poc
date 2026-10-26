"""Story 4 (#4) demonstrations against the EXISTING hosted Supervisor, Research, and Data Analysis agents.

Step 1 (AC1) prepares the environment once (``--mode prepare-if-missing``: Research MCP toolbox plus
``azd deploy --all`` for the Story 4 protocol) and demonstrates user -> Supervisor -> sub-agent ->
Supervisor -> user delivery with model-driven routing. Steps 2-7 reuse the Step 1 baseline and never
deploy or change an agent: sync/async delegation and failure (2), Research MCP tool call (3), identity
paths (4), trace and allowed/denied access (5), Data Analysis Code Interpreter via the Supervisor reusing
Story 1 (6), and direct client invocation of Research without the Supervisor (7).
Exit codes: 0 all checks passed, 1 failure, 2 blocked prerequisite.
"""

import argparse
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from app.core.redaction import sanitize_text
from app.services.orchestration import (
    ALLOWED_TARGETS,
    SIMULATED_FAILURE,
    DelegateCommand,
    DelegationReport,
    Specialist,
    SubTask,
)
from azure.ai.projects.models import MCPToolboxTool
from azure.core.exceptions import ResourceNotFoundError
from pydantic import JsonValue

from scripts.common.findings import Finding
from scripts.common.reporting import MarkdownReport, ValidationRun, add_output_arguments, update_index
from scripts.deploy.grant_local_rbac import command_prefix, object_value
from scripts.run_scenario.story1_data_analysis import (
    SNAPSHOT_HEADERS,
    AgentSnapshot,
    Blocked,
    snapshot_agents,
    toolbox_endpoint,
)
from scripts.run_scenario.story4_common import (
    ALLOWED_MCP_TOOL,
    BASELINE_FILE,
    CUSTOM,
    DATA_ANALYSIS,
    EXPECTED_ERRORS,
    FOUNDRY,
    INDEX_TITLE,
    LEARN_MCP_URL,
    MCP_SERVER_LABEL,
    NOT_SUPPORTED,
    RESEARCH,
    RESEARCH_TOOLBOX,
    RESEARCH_TOOLBOX_ENV,
    SDK,
    STORY_DIR,
    SUPERVISOR,
    TASK_PREFIX,
    Exchange,
    Story4Step,
    marker,
    write_baseline,
)

Mode = Literal["verify", "prepare-if-missing"]
AC1 = (
    "Demonstrate user -> supervisor -> sub-agent -> supervisor result delivery. Identify the routing decision, "
    "invocation mechanism, and component controlling delegation."
)
ROUTING_CASES: tuple[tuple[Specialist, str], ...] = (
    (
        "research",
        "Research question - no calculation is needed: in two sentences, explain what a conversation ID is used "
        "for with a Microsoft Foundry hosted agent. End your answer with the exact token {token}.",
    ),
    (
        "data_analysis",
        "Calculation task: use Python to compute the sum of the squares of the integers 1 through 20 and report "
        "the number. End your answer with the exact token {token}.",
    ),
)
SUM_OF_SQUARES = "2870"


@dataclass(frozen=True)
class ToolboxRef:
    version: str
    endpoint: str


def research_toolbox_metadata() -> dict[str, str]:
    return {"story": "4", "mcp_server": LEARN_MCP_URL, "allowed_tools": ALLOWED_MCP_TOOL}


def matching_research_toolbox(versions: Sequence[Mapping[str, JsonValue]], project_endpoint: str) -> ToolboxRef | None:
    """Newest toolbox version created by Story 4 for exactly this MCP server and allow-list."""
    expected = research_toolbox_metadata()
    found: list[int] = []
    for item in versions:
        metadata = item.get("metadata")
        if isinstance(metadata, dict) and all(metadata.get(k) == v for k, v in expected.items()):
            raw = str(item.get("version"))
            if raw.isdigit():
                found.append(int(raw))
    if not found:
        return None
    version = str(max(found))
    return ToolboxRef(version, toolbox_endpoint(project_endpoint, RESEARCH_TOOLBOX, version))


def capability_from(exchange: Exchange) -> bool:
    """A Story 4 specialist fails a simulated sub-task before any model call; older code answers it as chat."""
    return not exchange.completed and (
        SIMULATED_FAILURE in (exchange.error or "") or exchange.error_code == "server_error"
    )


def routing_case_checks(
    expected: Specialist, token: str, report: DelegationReport | None
) -> dict[str, tuple[bool, object]]:
    """AC1 assertions for one auto-routed request; pure so it can be unit tested."""
    if report is None:
        return {}
    sub = report.sub_agent
    invocation = report.invocation
    sub_ok = (
        sub is not None
        and sub.agent == expected
        and sub.correlation_id == report.correlation_id
        and token in sub.answer
        and (expected != "data_analysis" or SUM_OF_SQUARES in sub.answer)
    )
    return {
        "Routing decision": (
            report.routing.decided_by == "supervisor-model-tool-call"
            and report.routing.selected == expected
            and report.routing.model_tool_calls == 1,
            report.routing.model_dump(),
        ),
        "Invocation mechanism": (
            invocation is not None
            and invocation.mechanism == "responses-api-agent-endpoint"
            and invocation.target_agent == ALLOWED_TARGETS[expected]
            and invocation.status == "completed",
            invocation.model_dump(exclude={"headers_sent"}) if invocation else None,
        ),
        "Sub-agent -> Supervisor delivery": (
            sub_ok,
            {
                "agent": sub.agent if sub else None,
                "marker_in_sub_agent_answer": bool(sub and token in sub.answer),
                "correlation_echoed": bool(sub and sub.correlation_id == report.correlation_id),
                "tool_calls": [call.name for call in sub.tool_calls] if sub else [],
            },
        ),
        "Supervisor -> user delivery": (
            report.status == "completed" and token in report.final_answer,
            {"status": report.status, "marker_in_final_answer": token in report.final_answer},
        ),
    }


class Step1(Story4Step):
    """Prepare once, then demonstrate model-routed Supervisor delegation and result delivery."""

    number = 1
    scenario = "story-4-step-1"
    directory = "step-1-supervisor-delegation"
    report_name = "01-supervisor-delegation.md"
    title = "Story 4 / Step 1 - User -> Supervisor -> sub-agent -> Supervisor result delivery"
    index_label = "Step 1 (supervisor delegation)"
    task_label = f"{TASK_PREFIX} - 1 Demonstrate Supervisor delegation"
    ac = AC1
    scope_note = (
        "Reuses the three existing agents. `prepare-if-missing` may, once, create the Research MCP toolbox "
        f"`{RESEARCH_TOOLBOX}` and run `azd deploy --all` so all three run the Story 4 `POC_DELEGATE`/`POC_SUBTASK` "
        "protocol; agent identities are preserved. `verify` never changes anything. Synthetic prompts only."
    )

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        super().__init__(args, run, report)
        self.mode: Mode = args.mode
        self.toolbox: ToolboxRef | None = None
        self.deployed = False
        self.prepared = False
        self.cases: dict[str, bool] = {}

    def extra_fields(self) -> list[list[object]]:
        return [["Mode", self.mode]]

    def command_line(self) -> str:
        return super().command_line() + f" --mode {self.mode}"

    def guard_start(self) -> dict[str, AgentSnapshot] | None:
        self.report.heading("Agents reused - before")
        before = snapshot_agents(self.read)
        self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in before.values()])
        inactive = [s.name for s in before.values() if s.status != "active" or not s.latest_routing]
        if inactive:
            raise Blocked(f"Existing agents are not active with @latest routing: {', '.join(inactive)}")
        self.observe(
            "Agents reused - before",
            "PASS",
            "All three existing agents active",
            {name: snapshot.version for name, snapshot in before.items()},
        )
        self.toolbox = self.research_toolbox()
        self.report.heading("Story 4 protocol support (no model calls)")
        capable = self.capabilities()
        wired = before[RESEARCH].toolbox_endpoint == self.toolbox.endpoint
        ready = all(capable.values()) and wired
        expected = "All three agents run POC_DELEGATE/POC_SUBTASK; Research is wired to the Story 4 toolbox"
        observed = {"protocol": capable, "research_toolbox_wired": wired}
        if ready:
            self.record("Story 4 code deployed", "PASS", expected, observed)
        elif self.mode == "verify":
            raise Blocked(
                f"Story 4 code or Research toolbox wiring is missing ({observed}); rerun with prepare-if-missing."
            )
        else:
            self.report.paragraph(f"Before preparation: `{observed}`. Deploying the Story 4 protocol.")
            self.deploy_all(self.toolbox)
            after = snapshot_agents(self.read)
            self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in after.values()])
            identities = [name for name in before if before[name].principal_id != after[name].principal_id]
            capable = self.capabilities()
            wired = after[RESEARCH].toolbox_endpoint == self.toolbox.endpoint
            ok = (
                all(capable.values()) and wired and not identities and all(s.status == "active" for s in after.values())
            )
            self.check(
                "Story 4 code deployed",
                ok,
                "After deploy: protocol supported by all three, Research wired, identities unchanged",
                {"protocol": capable, "research_toolbox_wired": wired, "identity_changes": identities},
            )
            if not ok:
                return None
            before = after
        self.prepared = True
        return before

    def research_toolbox(self) -> ToolboxRef:
        self.report.heading("Research MCP toolbox")
        try:
            versions = [object_value(i.as_dict()) for i in self.project.toolboxes.list_versions(RESEARCH_TOOLBOX)]
        except ResourceNotFoundError:
            versions = []
        match = matching_research_toolbox(versions, self.endpoint)
        if match:
            self.record("Research toolbox", "PASS", "Reuse the Story 4 Microsoft Learn MCP toolbox", match.__dict__)
            return match
        if self.mode == "verify":
            raise Blocked(f"Toolbox {RESEARCH_TOOLBOX} is missing; rerun Step 1 with prepare-if-missing.")
        created = object_value(
            self.project.toolboxes.create_version(
                RESEARCH_TOOLBOX,
                tools=[
                    MCPToolboxTool(
                        server_label=MCP_SERVER_LABEL,
                        server_url=LEARN_MCP_URL,
                        allowed_tools=[ALLOWED_MCP_TOOL],
                        require_approval="never",
                    )
                ],
                description="Story 4 Research tool: public Microsoft Learn MCP server, docs search only",
                metadata=research_toolbox_metadata(),
            ).as_dict()
        )
        version = str(created.get("version"))
        ref = ToolboxRef(version, toolbox_endpoint(self.endpoint, RESEARCH_TOOLBOX, version))
        self.record("Research toolbox", "PASS", "Create one Story 4 toolbox version (MCP, allow-listed)", ref.__dict__)
        return ref

    def capabilities(self) -> dict[str, bool]:
        """Probe protocol support without a model call: allow-list denial and simulated sub-task failure."""
        command = DelegateCommand(task="Story 4 capability probe", correlation_id=uuid4(), target="capability-probe")
        _, report = self.delegate("Capability: Supervisor allow-list probe", command)
        result = {SUPERVISOR: report is not None and report.status == "denied" and report.invocation is None}
        for agent in (RESEARCH, DATA_ANALYSIS):
            probe = SubTask(task="Story 4 capability probe", correlation_id=uuid4(), simulate_failure=True)
            result[agent] = capability_from(
                self.hosted_call(f"Capability: {agent} sub-task probe", agent, probe.wire())
            )
        return result

    def deploy_all(self, toolbox: ToolboxRef) -> None:
        """All three share one image, so the Story 4 protocol needs ``azd deploy --all``; identities persist."""
        azd = command_prefix("azd")
        subprocess.run(
            [*azd, "env", "set", RESEARCH_TOOLBOX_ENV, toolbox.endpoint, "--environment", self.args.environment],
            check=True,
            capture_output=True,
            timeout=60,
        )
        self.run.console.print("Deploying the existing agents with the Story 4 protocol (azd deploy --all) ...")
        started = perf_counter()
        process = subprocess.Popen(
            [*azd, "deploy", "--all", "--environment", self.args.environment, "--no-prompt"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            self.run.console.print(line.rstrip(), markup=False, highlight=False)
            lines.append(line.rstrip())
        code = process.wait(timeout=3600)
        self.report.code("\n".join(lines[-30:]), "text")
        if code:
            self.record("Deploy existing agents", "FAIL", "azd deploy --all succeeds", f"exit {code}")
            raise Blocked("The existing agents were not updated; inspect the azd output above.")
        self.deployed = True
        self.record(
            "Deploy existing agents",
            "PASS",
            "New versions of the same three agents; foundation and identities reused",
            {"seconds": round(perf_counter() - started), "env": {RESEARCH_TOOLBOX_ENV: toolbox.endpoint}},
        )

    def body(self) -> None:
        self.report.heading("Delegation demonstrations (model-driven routing)")
        self.report.paragraph(
            "Each request is sent only to the Supervisor with `target: auto`. The Supervisor model chooses a "
            "specialist by calling one of two tools; the Supervisor application then invokes that specialist's "
            "agent endpoint and returns a `poc-delegation-v1` report. A run-unique token proves which hop carried "
            "the result."
        )
        rows: list[list[object]] = []
        for expected, template in ROUTING_CASES:
            token = marker("S4")
            command = DelegateCommand(task=template.format(token=token), correlation_id=uuid4(), target="auto")
            exchange, report = self.delegate(f"AC1 {expected} task: user -> Supervisor (target auto)", command)
            self.report.heading(f"{expected.replace('_', ' ').title()} task", level=3)
            self.report.code(command.task, "text")
            if not self.check(
                f"Supervisor response ({expected} task)",
                report is not None,
                "Completed poc-delegation-v1 report",
                exchange.observed(),
            ):
                self.cases[expected] = False
                continue
            assert report is not None
            results = routing_case_checks(expected, token, report)
            passed = [
                self.check(f"{name} ({expected} task)", ok, CASE_EXPECTED[name], observed)
                for name, (ok, observed) in results.items()
            ]
            self.cases[expected] = all(passed)
            sub = report.sub_agent
            invocation = report.invocation
            rows.append(
                [
                    expected,
                    report.routing.selected,
                    report.routing.decided_by,
                    invocation.mechanism if invocation else "-",
                    invocation.identity if invocation else "-",
                    invocation.response_id if invocation else "-",
                    (sub.answer[:160] + "...") if sub and len(sub.answer) > 160 else (sub.answer if sub else "-"),
                    report.final_answer[:160],
                ]
            )
        self.report.heading("Delegation flow observed")
        self.report.table(
            [
                "Expected route",
                "Selected",
                "Decided by",
                "Invocation",
                "Identity forwarded",
                "Sub-agent response ID",
                "Sub-agent answer (to Supervisor)",
                "Final answer (to user)",
            ],
            rows,
        )
        self.report.heading("Who controls delegation")
        self.report.table(["Concern", "Component", "Classification", "Evidence"], CONTROL_ROWS)

    def guard_end(self, before: Mapping[str, AgentSnapshot]) -> None:
        super().guard_end(before)
        if self.prepared and self.statuses.get("Agents reused - after") == "PASS" and self.toolbox:
            path = self.args.output_dir / BASELINE_FILE
            write_baseline(path, self.args.environment, self.run.run_id, before, self.toolbox.endpoint)
            self.report.paragraph(f"Reuse baseline for Steps 2-7 written to `{path.name}`.")

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        if self.statuses.get("Story 4 code deployed") == "FAIL" or not self.cases:
            return "Not met"
        passed = sum(self.cases.values())
        return "Met" if passed == len(ROUTING_CASES) else ("Partially met" if passed else "Not met")

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        results = [
            f"Model-routed delegation: {sum(self.cases.values())} of {len(ROUTING_CASES)} cases delivered the "
            "specialist result to the Supervisor and on to the user.",
            f"Agents {'were updated by azd deploy --all' if self.deployed else 'were reused unchanged'}.",
        ]
        findings = [
            Finding(
                "Routing decision: the Supervisor model selects the specialist by calling one of two function tools",
                "Selected the expected specialist" if all(self.cases.values()) and self.cases else "See checks",
                "Tested",
                f"{SDK} (MAF function calling); {CUSTOM} (tool definitions)",
                ("maf_function_tools",),
            ),
            Finding(
                "Invocation mechanism: Responses API on the specialist's agent-bound endpoint, one synchronous call",
                "Completed" if any(self.cases.values()) else "Not observed",
                "Tested",
                f"{FOUNDRY} (endpoint, auth); {CUSTOM} (client code)",
                ("hosted_invoke",),
            ),
            Finding(
                "Component controlling delegation: Supervisor application code (allow-list, one call, no retry)",
                "Enforced",
                "Tested",
                CUSTOM,
            ),
            Finding(
                "Foundry-managed agent-to-agent delegation through the A2A protocol/tool, and MAF orchestration "
                "patterns (handoff, magentic)",
                "Documented alternatives; not tested in this POC",
                "Documented",
                f"{FOUNDRY} (A2A); {SDK} (MAF orchestrations)",
                ("a2a_protocol", "a2a", "maf_orchestration"),
            ),
        ]
        gaps = [
            "Model routing is non-deterministic; the expected route is asserted per run, not guaranteed.",
            "The stateful POC_WORKFLOW path is unchanged and still stops at the nested caller guard (ADR-0001).",
            "A2A delegation was not tested; it needs incoming A2A enabled on the target and an A2A connection.",
        ]
        conclusion = (
            "The Supervisor routed each task to the correct specialist and delivered its result back to the user."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


CASE_EXPECTED = {
    "Routing decision": "decided_by supervisor-model-tool-call, expected specialist, exactly one tool call",
    "Invocation mechanism": "Responses API on the selected specialist's agent endpoint, completed",
    "Sub-agent -> Supervisor delivery": "Sub-agent report for the expected agent, same correlation, token present",
    "Supervisor -> user delivery": "Supervisor final answer contains the sub-agent token",
}
CONTROL_ROWS: list[list[object]] = [
    ["Routing decision", "Supervisor model via MAF function-calling loop", SDK, "Routing decision check"],
    ["Allow-list and single call", "Supervisor application (`app/services/orchestration.py`)", CUSTOM, "Step 5"],
    ["Transport, authentication, RBAC", "Foundry agent endpoint (Responses protocol)", FOUNDRY, "Invocation check"],
    ["Sub-agent execution", "Foundry hosted runtime + MAF agent in the specialist container", FOUNDRY, "Delivery"],
    ["Result delivery to the user", "Supervisor application composes the final answer", CUSTOM, "Delivery check"],
]
AC2 = (
    "Assess synchronous/asynchronous support and demonstrate one supported flow, including a representative "
    "sub-agent failure. Identify ownership of task creation, status, completion, failure handling, and result "
    "retrieval; record restrictions and untested alternatives."
)
FLOW_TASK = "In one sentence, describe delegation between two agents. End your answer with the exact token {token}."
BACKGROUND_MARKER = "STORY4-BACKGROUND-OK"
FLOW_EXPECTED = {
    ("sync", False): "Completed report; Research result (token) returned in the same call",
    ("sync", True): "Supervisor completes with sub_agent_failed; failure surfaced, handled by Supervisor, not retried",
    ("background", False): "Accepted queued/in_progress, polled by the Supervisor to completed; token returned",
    ("background", True): "Polled to failed; Supervisor reports sub_agent_failed without retry",
}


def flow_check(
    report: DelegationReport | None, token: str, fail: bool, mode: Literal["sync", "background"]
) -> tuple[bool, dict[str, object]]:
    """AC2 assertions for one Supervisor -> Research flow; pure so it can be unit tested."""
    if report is None or report.invocation is None:
        return False, {"report": None if report is None else report.status}
    invocation = report.invocation
    seen = invocation.statuses_seen
    ok = invocation.mode == mode and invocation.target_agent == ALLOWED_TARGETS["research"]
    if mode == "background":
        ok = ok and bool(seen) and seen[0] in ("queued", "in_progress")
    failure = report.failure
    if fail:
        ok = (
            ok
            and report.status == "sub_agent_failed"
            and failure is not None
            and failure.retried is False
            and failure.handled_by == "supervisor-application"
            and (SIMULATED_FAILURE in failure.message or failure.code == "server_error")
        )
    else:
        sub = report.sub_agent
        ok = ok and report.status == "completed" and sub is not None and token in sub.answer
        ok = ok and token in report.final_answer
    return ok, {
        "status": report.status,
        "invocation": invocation.model_dump(exclude={"headers_sent"}),
        "failure": failure.model_dump() if failure else None,
    }


class Step2(Story4Step):
    """Synchronous and background delegation, each with a representative sub-agent failure."""

    number = 2
    scenario = "story-4-step-2"
    directory = "step-2-sync-async-failure"
    report_name = "02-sync-async-and-failure.md"
    title = "Story 4 / Step 2 - Synchronous/asynchronous delegation and sub-agent failure"
    index_label = "Step 2 (sync/async and failure)"
    task_label = f"{TASK_PREFIX} - 2 Assess sync and async delegation"
    ac = AC2
    scope_note = (
        "Reuses the Step 1 baseline read-only. The Supervisor calls Research synchronously and in Responses "
        "background mode; each mode also runs a simulated sub-agent failure (no model call). No retries."
    )

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        super().__init__(args, run, report)
        self.flows: dict[tuple[str, bool], bool] = {}

    def body(self) -> None:
        self.report.heading("Supervisor -> Research flows")
        rows: list[list[object]] = []
        modes: tuple[Literal["sync", "background"], ...] = ("sync", "background")
        for mode in modes:
            for fail in (False, True):
                token = marker("S4")
                command = DelegateCommand(
                    task=FLOW_TASK.format(token=token),
                    correlation_id=uuid4(),
                    target="research",
                    invocation=mode,
                    simulate_failure=fail,
                )
                outcome = "failure" if fail else "success"
                exchange, report = self.delegate(f"AC2 {mode} {outcome}: Supervisor -> Research", command)
                ok, observed = flow_check(report, token, fail, mode)
                name = ("Synchronous" if mode == "sync" else "Background (async)") + (
                    " sub-agent failure handled" if fail else " delegation"
                )
                self.flows[(mode, fail)] = self.check(
                    name, ok, FLOW_EXPECTED[(mode, fail)], observed if report else exchange.observed()
                )
                invocation = report.invocation if report else None
                rows.append(
                    [
                        mode,
                        outcome,
                        report.status if report else exchange.status,
                        " -> ".join(invocation.statuses_seen) if invocation else "-",
                        invocation.polls if invocation else "-",
                        invocation.elapsed_ms if invocation else "-",
                        report.failure.message[:120] if report and report.failure else "-",
                    ]
                )
        self.report.table(
            ["Mode", "Case", "Supervisor report", "Sub-agent statuses", "Polls", "Sub-agent ms", "Failure"], rows
        )
        self.report.heading("Client -> agent background request (no Supervisor)")
        direct = self.hosted_call(
            "AC2 client background: Research plain request",
            RESEARCH,
            f"Reply with exactly: {BACKGROUND_MARKER}",
            background=True,
        )
        self.obs["client_background"] = direct.completed
        self.check(
            "Client-to-agent background response",
            direct.completed
            and BACKGROUND_MARKER in direct.text
            and direct.statuses_seen[:1] in (["queued"], ["in_progress"]),
            "Accepted as queued/in_progress, retrieved by polling until completed",
            direct.observed(),
        )
        probe = DelegateCommand(task="Story 4 restriction probe", correlation_id=uuid4(), target="capability-probe")
        restricted = self.hosted_call(
            "AC2 restriction: client background POC_DELEGATE", SUPERVISOR, probe.wire(), background=True
        )
        self.check(
            "Client-to-Supervisor background delegation is restricted",
            not restricted.completed and "background is not enabled" in (restricted.error or ""),
            "Rejected by the Supervisor application (an application restriction, not a platform one)",
            restricted.observed(),
        )
        self.report.heading("Ownership")
        self.report.table(["Concern", "Synchronous (tested)", "Background (tested)", "Owner"], OWNERSHIP_ROWS)
        self.report.heading("Restrictions")
        self.report.table(["Restriction", "Evidence", "Owner"], RESTRICTION_ROWS)
        self.report.heading("Untested alternatives")
        self.report.table(["Alternative", "Why not tested here", "Classification"], ALTERNATIVE_ROWS)

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        if not self.flows:
            return "Not met"
        sync = self.flows.get(("sync", False)) and self.flows.get(("sync", True))
        background = self.flows.get(("background", False)) and self.flows.get(("background", True))
        if sync or background:
            return "Met" if sync and background else "Partially met"
        return "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        def word(key: tuple[str, bool]) -> str:
            return "passed" if self.flows.get(key) else "did not pass"

        results = [
            f"Synchronous delegation {word(('sync', False))}; synchronous sub-agent failure handling "
            f"{word(('sync', True))}.",
            f"Background delegation {word(('background', False))}; background failure handling "
            f"{word(('background', True))}.",
            f"Client-to-agent background request {'completed' if self.obs.get('client_background') else 'failed'}.",
        ]
        findings = [
            Finding(
                "Synchronous Responses call from Supervisor to sub-agent, with failure surfaced and not retried",
                "Supported" if self.flows.get(("sync", True)) else "See checks",
                "Tested",
                f"{FOUNDRY} (protocol); {CUSTOM} (failure handling)",
                ("hosted_invoke",),
            ),
            Finding(
                "Asynchronous delegation using Responses background mode, polled with responses.retrieve",
                "Supported" if self.flows.get(("background", False)) else "See checks",
                "Tested",
                f"{FOUNDRY} (stored response status); {SDK} (agentserver background handling); {CUSTOM} (polling)",
                ("responses_background",),
            ),
            Finding(
                "Background responses surviving a host restart (resilient background)",
                "Not enabled in this POC",
                "Documented",
                f"{SDK} (opt-in)",
                ("resilience",),
            ),
            Finding(
                "Completion push/callback to the Supervisor instead of polling",
                "Not observed; completion is discovered by polling",
                "Unverified",
                NOT_SUPPORTED + " (not observed)",
            ),
        ]
        gaps = [
            "Streaming, cancellation, A2A tasks, and queue-based fan-out are untested alternatives (see table).",
            "The Supervisor polls inside its own synchronous request; its 240 s client timeout bounds background work.",
        ]
        conclusion = (
            "Both synchronous and background delegation work, and a sub-agent failure is surfaced, not retried."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


OWNERSHIP_ROWS: list[list[object]] = [
    [
        "Task creation",
        "Supervisor app: `responses.create` on the specialist endpoint",
        "Supervisor app: `responses.create(background=True, store=True)`",
        CUSTOM,
    ],
    [
        "Status",
        "Implicit: the call blocks until the response is terminal",
        "Foundry stored response: queued -> in_progress -> completed/failed",
        FOUNDRY,
    ],
    ["Completion", "Specialist host emits completed/failed", "Same; Foundry stores the terminal response", SDK],
    [
        "Failure handling",
        "Supervisor app: no retry, `sub_agent_failed` report with the failure",
        "Same, after polling",
        CUSTOM,
    ],
    ["Result retrieval", "Returned in the HTTP response", "Supervisor polls `responses.retrieve(id)`", FOUNDRY],
]
RESTRICTION_ROWS: list[list[object]] = [
    ["Client -> Supervisor `POC_DELEGATE` with `background: true` is rejected", "Tested (this step)", CUSTOM],
    ["The stateful `POC_WORKFLOW` path rejects background requests", "Existing code (ADR-0001)", CUSTOM],
    ["Background work is bounded by the Supervisor's own 240 s request", "Design of this POC", CUSTOM],
    ["A host restart during background work is not recovered (`resilient_background` off)", "Documented", SDK],
]
ALTERNATIVE_ROWS: list[list[object]] = [
    ["Streaming (`stream: true`) between agents", "Not needed for one representative flow", FOUNDRY],
    ["`responses.cancel` of a background sub-task", "Not part of the acceptance criterion", FOUNDRY],
    ["A2A protocol tasks via the Foundry A2A tool", "Requires A2A endpoints and connections not deployed", FOUNDRY],
    ["MAF in-process workflows (handoff, concurrent, magentic)", "Agents are separately hosted here", SDK],
    ["Queue-based fan-out (Service Bus / Storage queues)", "No queue in the POC foundation", CUSTOM],
    ["Resilient background with process-loss recovery", "Stretch goal #7", SDK],
]

AC7 = (
    "Evaluate direct client invocation of Hosted Agent B through the Responses API, without Agent A, using a "
    "separate conversation ID. Demonstrate the response or document the restriction or unresolved question."
)


class Step7(Story4Step):
    """The client calls Research directly in its own conversation; the Supervisor is never invoked."""

    number = 7
    scenario = "story-4-step-7"
    directory = "step-7-direct-invocation"
    report_name = "07-direct-invocation.md"
    title = "Story 4 / Step 7 - Direct client invocation of Hosted Agent B (Research)"
    index_label = "Step 7 (direct invocation)"
    task_label = f"{TASK_PREFIX} - 7 Invoke Research directly"
    ac = AC7
    scope_note = (
        "Agent A is the Supervisor and Agent B is Research. The client creates one conversation on each agent "
        "endpoint (no model call for A) and then sends two requests only to Research in conversation B."
    )

    def conversation(self, agent: str) -> str | None:
        try:
            with self.project.get_openai_client(agent_name=agent, timeout=60, max_retries=0) as client:
                return client.conversations.create().id
        except EXPECTED_ERRORS as error:
            self.obs[f"conversation_error_{agent}"] = sanitize_text(str(error))[:300]
            return None

    def body(self) -> None:
        self.report.heading("Conversations")
        conversation_a = self.conversation(SUPERVISOR)
        conversation_b = self.conversation(RESEARCH)
        self.check(
            "Separate conversation IDs",
            bool(conversation_a and conversation_b and conversation_a != conversation_b),
            "Conversation B created on Research's endpoint, distinct from Supervisor conversation A",
            {"conversation_a": conversation_a, "conversation_b": conversation_b, **self.obs},
        )
        if not conversation_b:
            return
        self.report.heading("Direct requests to Research")
        code = marker("S4B")
        first = self.hosted_call(
            "AC7 R1: client -> Research (conversation B)",
            RESEARCH,
            f"Remember this code word for later in this conversation: {code}. Reply with exactly: STORED",
            conversation=conversation_b,
        )
        self.check(
            "Direct response from Research",
            first.completed and bool(first.text.strip()),
            "Completed Responses API reply from Research without the Supervisor",
            {**first.observed(), "text": first.text[:200]},
        )
        second = self.hosted_call(
            "AC7 R2: client -> Research follow-up (conversation B)",
            RESEARCH,
            "What code word did I ask you to remember? Reply with only the code word.",
            conversation=conversation_b,
        )
        self.check(
            "Conversation continuity on Research",
            second.completed and code in second.text,
            "Research recalls the code word from conversation B history",
            {**second.observed(), "code_recalled": code in second.text},
        )
        called = sorted({str(row[2]) for row in self.requests})
        self.check(
            "Supervisor (Agent A) not invoked",
            called == [RESEARCH],
            "Every model request in this step targeted Research only",
            {"agents_called": called},
        )

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        names = (
            "Separate conversation IDs",
            "Direct response from Research",
            "Conversation continuity on Research",
            "Supervisor (Agent A) not invoked",
        )
        if all(self.statuses.get(name) == "PASS" for name in names):
            return "Met"
        return "Partially met" if self.statuses.get("Direct response from Research") == "PASS" else "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        results = [
            f"Direct Research response: {self.statuses.get('Direct response from Research', 'not run')}.",
            f"Conversation continuity: {self.statuses.get('Conversation continuity on Research', 'not run')}.",
        ]
        findings = [
            Finding(
                "A client can invoke a hosted sub-agent directly through its own Responses endpoint",
                "Supported" if self.verdict == "Met" else "See checks",
                "Tested",
                FOUNDRY,
                ("hosted_invoke",),
            ),
            Finding(
                "Each hosted agent keeps its own conversation; history continues within conversation B",
                "Observed" if self.statuses.get("Conversation continuity on Research") == "PASS" else "See checks",
                "Tested",
                FOUNDRY,
                ("hosted_conversations",),
            ),
            Finding(
                "Direct calls bypass Supervisor routing, allow-list, and correlation; access is governed only by "
                "Foundry RBAC on Research",
                "By design",
                "Tested",
                f"{FOUNDRY}; any extra policy is {CUSTOM}",
                ("foundry_rbac",),
            ),
        ]
        gaps = [
            "Whether a conversation created on one agent can be used on another agent's endpoint was not tested.",
            "Restricting direct access to sub-agents (Supervisor-only callers) would need separate RBAC design.",
        ]
        conclusion = (
            "Research answered the client directly in its own conversation, without the Supervisor."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


def step_classes() -> dict[int, type[Story4Step]]:
    # Imported lazily: the identity module builds on this one.
    from scripts.run_scenario import story4_identity

    return {
        1: Step1,
        2: Step2,
        3: story4_identity.Step3,
        4: story4_identity.Step4,
        5: story4_identity.Step5,
        6: story4_identity.Step6,
        7: Step7,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, choices=range(1, 8), required=True)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument(
        "--mode",
        choices=["verify", "prepare-if-missing"],
        default="verify",
        help="Step 1 only; Steps 2-7 never deploy or change agents.",
    )
    parser.add_argument("--timeout", type=float, default=300, help="Client timeout in seconds per hosted request.")
    add_output_arguments(parser)
    return parser


def run_step(args: argparse.Namespace, step_class: type[Story4Step]) -> int:
    run = ValidationRun(
        step_class.scenario,
        "hosted",
        args.output_dir,
        no_color=args.no_color,
        verbose=args.verbose,
        directory_label=step_class.directory,
    )
    report = MarkdownReport(run.path / step_class.report_name, step_class.title)
    step = step_class(args, run, report)
    try:
        step.execute()
    except Blocked as error:
        step.record("Prerequisite", "BLOCKED", "Existing agents and the Story 4 baseline ready", str(error))
    except EXPECTED_ERRORS as error:
        step.record("Unexpected failure", "FAIL", "Step completes", sanitize_text(str(error)))
    step.conclude()
    report.render(run.console)
    update_index(
        args.output_dir / "index.md",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC - {step_class.index_label} - **{step.verdict}** - "
        f"[{run.path.name}]({run.path.name}/{step_class.report_name})",
        title=INDEX_TITLE,
    )
    return run.finish()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.step != 1 and args.mode != "verify":
        parser.error("--mode applies to Step 1 only; Steps 2-7 never deploy or change agents.")
    args.output_dir = Path(args.output_dir) / STORY_DIR
    return run_step(args, step_classes()[args.step])


if __name__ == "__main__":
    raise SystemExit(main())
