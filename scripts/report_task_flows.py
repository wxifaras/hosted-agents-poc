"""Reviewed task plans and one offline SVG renderer, not inferred execution traces."""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from textwrap import wrap
from xml.etree.ElementTree import Element, SubElement, tostring

from pydantic import JsonValue

FLOW_VERSION = "task-flow-definitions-v1"
STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "SKIP"})
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")
SCENARIO = re.compile(r"story-(\d+)-step-(\d+)\Z")
SCOPE = (
    "Reviewed code-defined test plan, not an execution trace. Arrows do not prove requests, "
    "identity propagation, span linkage, or successful authorization. Badges summarize only "
    "the explicitly linked recorded checks; missing checks remain NOT RECORDED."
)


@dataclass(frozen=True)
class FlowStep:
    """A reviewed action and exact/full-match selectors for its recorded assertions."""

    key: str
    title: str
    actor: str
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowEdge:
    """A plan relationship, never a measured network hop or timestamp."""

    source: str
    target: str
    label: str = ""


@dataclass(frozen=True)
class TaskFlow:
    """Small reusable description maintained alongside its runner contract."""

    key: str
    title: str
    steps: tuple[FlowStep, ...]
    sources: tuple[str, ...]
    notes: tuple[str, ...] = ()
    edges: tuple[FlowEdge, ...] = ()


def _checks(*names: str) -> tuple[str, ...]:
    return tuple(re.escape(name) for name in names)


def _step(key: str, title: str, actor: str, *names: str) -> FlowStep:
    return FlowStep(key, title, actor, _checks(*names))


BASELINE = _step("baseline", "Verify the recorded deployment baseline", "Validation runner", "Agents reused - before")
END = _step("unchanged", "Check that the deployed agents did not change", "Validation runner", "Agents reused - after")
NATIVE_BASELINE = _step(
    "baseline", "Verify native deployment and version binding", "Validation runner", "Native deployment binding"
)
NATIVE_END = _step(
    "unchanged", "Check that the deployed versions did not change", "Validation runner", "Deployed versions unchanged"
)
ANALYSIS = FlowStep(
    "analysis",
    "Analyze the seeded CSV and check original outputs",
    "Native interpreter + independent runner oracle",
    (r"[0-9][0-9,]* rows: analytical originals",),
)

STORY3_READY = _step(
    "prerequisite", "Require Story 3 support and its saved baseline", "Validation runner", "Prerequisite"
)
STORY3: dict[int, TaskFlow] = {
    1: TaskFlow(
        "story-3-step-1",
        "Story 3 Step 1: Separate conversations and mapping",
        (
            BASELINE,
            STORY3_READY,
            _step(
                "capability", "Verify Supervisor accepts the conversation protocol", "Runner", "Story 3 code deployed"
            ),
            _step(
                "create",
                "Create Supervisor and distinct Research conversations",
                "Operator -> Supervisor -> Research",
                "Supervisor conversation created by the client",
                "Supervisor turn in the Supervisor conversation",
                "Separate sub-agent conversation",
            ),
            _step(
                "mapping",
                "Persist and re-read the application-owned child mapping",
                "Supervisor / user-isolated Foundry state",
                "Mapping owned and stored by the Supervisor",
                "Mapping re-read from the state store",
            ),
            _step(
                "metadata",
                "Check the custom parent metadata link",
                "Runner / Supervisor inspect",
                "Parent link stored on the sub-agent conversation",
            ),
            END,
        ),
        ("scripts.run_scenario.story3_conversations.Step1", "app.services.conversations.separate_turn"),
        ("Parent metadata is application-written; child creation and mapping persistence are not atomic.",),
    ),
    2: TaskFlow(
        "story-3-step-2",
        "Story 3 Step 2: Reuse independent Research history",
        (
            BASELINE,
            STORY3_READY,
            _step(
                "first",
                "Remember a synthetic code word in a new child",
                "Operator -> Supervisor -> Research",
                "Supervisor conversation created by the client",
                "First turn created the sub-agent conversation",
            ),
            _step(
                "followup",
                "Reuse the mapping and recall without resending the code word",
                "Supervisor -> Research",
                "Follow-up reused the same sub-agent conversation",
                "Prior history stored in the sub-agent conversation",
                "Sub-agent recalled its own history",
            ),
            _step(
                "isolate",
                "Check separate histories and a new-parent control",
                "Runner / model-free Supervisor inspect",
                "Sub-agent history not copied into the Supervisor conversation",
                "Another Supervisor conversation has no mapping",
            ),
            END,
        ),
        ("scripts.run_scenario.story3_conversations.Step2", "app.services.conversations.separate_turn"),
        ("Recall is checked alongside stored-item counts, not treated alone as deterministic persistence proof.",),
    ),
    3: TaskFlow(
        "story-3-step-3",
        "Story 3 Step 3: Ownership, correlation and access",
        (
            BASELINE,
            STORY3_READY,
            _step(
                "setup",
                "Create the parent and mapped child",
                "Operator -> Supervisor -> Research",
                "Supervisor conversation created by the client",
                "Sub-agent conversation created by the Supervisor",
            ),
            _step(
                "context",
                "Check original-user binding and custom correlation",
                "Supervisor platform context / runner",
                "Original user bound at the Supervisor",
                "Custom correlation recorded with the mapping",
            ),
            _step(
                "reads",
                "Record owner, cross-endpoint and agent-context reads",
                "Operator / Supervisor -> Foundry",
                "Owner lists its Supervisor conversation",
                "Owner lists its Supervisor conversation through the Research endpoint",
                "Client lists the Supervisor-created sub-agent conversation",
                "Supervisor lists its sub-agent conversation (agent identity)",
                "Supervisor lists its sub-agent conversation (call-context identity)",
            ),
            _step(
                "guards",
                "Reject unmapped claims and missing parent context",
                "Supervisor application guards",
                "Client supplies an unmapped sub-agent conversation ID",
                "Another Supervisor conversation claims the sub-agent conversation",
                "Request without a Supervisor conversation",
            ),
            END,
        ),
        ("scripts.run_scenario.story3_conversations.Step3", "app.services.conversations.separate_turn"),
        ("Operator reads do not establish least-privileged two-user isolation or the reason for unexpected access.",),
    ),
    4: TaskFlow(
        "story-3-step-4",
        "Story 3 Step 4: Evaluate a shared conversation ID",
        (
            BASELINE,
            STORY3_READY,
            _step(
                "seed",
                "Seed Supervisor history with a synthetic code word",
                "Operator -> Supervisor",
                "Supervisor conversation created by the client",
                "Supervisor conversation seeded",
            ),
            _step(
                "shared",
                "Attempt Research calls with the Supervisor ID in both modes",
                "Supervisor -> Research",
                "B invoked with A's conversation ID (agent identity)",
                "B invoked with A's conversation ID (call-context identity)",
            ),
            _step(
                "inspect",
                "Inspect later history and run the same-principal control",
                "Supervisor inspect / operator -> Research",
                "Later Supervisor invocation observed its hydrated history",
                "Control: owner uses A's conversation on B's endpoint",
            ),
            END,
        ),
        ("scripts.run_scenario.story3_conversations.Step4", "app.services.conversations.shared_turn"),
        ("PASS records an explicit outcome, including HTTP 404; it does not mean shared history worked.",),
    ),
    5: TaskFlow(
        "story-3-step-5",
        "Story 3 Step 5: Compare result, placeholder and shared history",
        (
            BASELINE,
            STORY3_READY,
            _step(
                "separate",
                "Compare explicit result and placeholder delivery",
                "Supervisor -> mapped Research child / later inspect",
                "Variant separate / result / agent",
                "Variant separate / placeholder / agent",
            ),
            _step(
                "shared",
                "Classify shared-ID outcomes in both identity modes",
                "Supervisor -> Research / later inspect",
                "Variant shared / placeholder / agent",
                "Variant shared / placeholder / call-context",
            ),
            END,
        ),
        (
            "scripts.run_scenario.story3_conversations.Step5",
            "scripts.run_scenario.story3_conversations.classify_received",
        ),
        (
            "Classification uses recorded results and history counters; "
            "shared-call rejection is not successful delivery.",
        ),
    ),
}

STORY4: dict[int, TaskFlow] = {
    1: TaskFlow(
        "story-4-step-1",
        "Story 4 Step 1: Supervisor routing",
        (
            BASELINE,
            _step(
                "prepare",
                "Reuse Research search tooling; prepare only when approved",
                "Runner / deployment tooling",
                "Research toolbox",
                "Deploy existing agents",
                "Story 4 code deployed",
            ),
            _step(
                "route",
                "Ask Supervisor to choose Research or Data Analysis",
                "Supervisor routing model",
                "Routing decision (research task)",
                "Routing decision (data_analysis task)",
            ),
            _step(
                "invoke",
                "Invoke the selected specialist's Responses endpoint",
                "Supervisor backend",
                "Invocation mechanism (research task)",
                "Invocation mechanism (data_analysis task)",
            ),
            _step(
                "result",
                "Return the specialist result to Supervisor",
                "Research / Data Analysis text handler",
                "Sub-agent -> Supervisor delivery (research task)",
                "Sub-agent -> Supervisor delivery (data_analysis task)",
            ),
            _step(
                "deliver",
                "Check the final result delivered to the caller",
                "Supervisor + validation runner",
                "Supervisor response (research task)",
                "Supervisor response (data_analysis task)",
                "Supervisor -> user delivery (research task)",
                "Supervisor -> user delivery (data_analysis task)",
            ),
            END,
        ),
        ("scripts.run_scenario.story4_agent_patterns.Step1", "app.services.orchestration.delegate_turn"),
        ("Data Analysis here is text delegation, not native Code Interpreter execution or original-file delivery.",),
    ),
    2: TaskFlow(
        "story-4-step-2",
        "Story 4 Step 2: Sync, background, and failure cases",
        (
            BASELINE,
            _step(
                "success",
                "Run sync and stored-background Research cases",
                "Supervisor -> Research; status retrieval",
                "Synchronous delegation",
                "Background (async) delegation",
            ),
            _step(
                "failure",
                "Surface deliberate specialist failures without resubmission",
                "Research failure guard / Supervisor",
                "Synchronous sub-agent failure handled",
                "Background (async) sub-agent failure handled",
            ),
            _step(
                "direct",
                "Poll a direct Research background response",
                "Client -> Research",
                "Client-to-agent background response",
            ),
            _step(
                "restrict",
                "Check rejection of client-background delegation",
                "Supervisor application guard",
                "Client-to-Supervisor background delegation is restricted",
            ),
            END,
        ),
        ("scripts.run_scenario.story4_agent_patterns.Step2", "app.services.orchestration.Delegator"),
        ("Success and simulated-failure cases are separate tests. Background polling is not crash recovery.",),
    ),
    3: TaskFlow(
        "story-4-step-3",
        "Story 4 Step 3: Research Learn MCP tool",
        (
            BASELINE,
            _step(
                "definition",
                "Inspect the search-only toolbox definition",
                "Runner metadata read",
                "Research toolbox definition",
            ),
            _step("list", "List tools on that endpoint", "Operator -> toolbox MCP", "tools/list"),
            _step(
                "control",
                "Check a permitted public Learn search",
                "Operator -> toolbox -> public Learn MCP",
                "Toolbox reaches the MCP server",
            ),
            _step(
                "invoke",
                "Delegate Research and observe its actual tool call",
                "Supervisor -> Research",
                "Research tool call",
            ),
            _step(
                "answer",
                "Check that the delivered answer used the tool result",
                "Research -> Supervisor -> caller",
                "Tool result used in the answer",
            ),
            END,
        ),
        ("scripts.run_scenario.story4_identity.Step3", "app.runtime.research_tools"),
        ("The remote Learn MCP server is public. This task does not prove protected-tool user OAuth.",),
    ),
    4: TaskFlow(
        "story-4-step-4",
        "Story 4 Step 4: Identity-mode comparison",
        (
            BASELINE,
            _step(
                "roles",
                "Read the existing Supervisor impersonation role inventory",
                "Operator metadata read; no grants",
                "Supervisor impersonation permission",
            ),
            _step(
                "agent",
                "Test the agent-owned request mode",
                "Supervisor workload -> Research",
                "Supervisor -> Research (agent)",
            ),
            _step(
                "context",
                "Test forwarding the trusted platform call context",
                "Supervisor workload -> Research",
                "Supervisor -> Research (call-context)",
            ),
            _step(
                "label",
                "Test the delegated-label request mode",
                "Supervisor workload -> Research",
                "Supervisor -> Research (delegated-user)",
            ),
            _step(
                "tool",
                "Check Research's agent-owned toolbox access",
                "Research workload -> public tool path",
                "Research -> toolbox (agent-owned)",
            ),
            END,
        ),
        ("scripts.run_scenario.story4_identity.Step4", "app.services.orchestration.identity_headers"),
        (
            "A completed mode request is not proof that Research sees an original user.",
            "Existing grant presence, user-label forwarding, and OAuth OBO are different claims.",
        ),
    ),
    5: TaskFlow(
        "story-4-step-5",
        "Story 4 Step 5: Correlation and access observations",
        (
            BASELINE,
            _step(
                "correlation",
                "Observe original caller presence and application correlation",
                "Supervisor / Research reports",
                "Trace: original user and correlation",
            ),
            _step(
                "allowed",
                "Exercise the permitted Research/tool route",
                "Caller -> Supervisor -> Research -> toolbox",
                "Allowed: operator -> Supervisor -> Research -> tool",
            ),
            _step(
                "header",
                "Compare delegated-header behavior with the role inventory",
                "Foundry agent endpoint",
                "Platform enforcement: delegated user header",
            ),
            _step(
                "target",
                "Request an excluded target; expect no outbound delegation",
                "Supervisor application allow-list",
                "Application enforcement: Supervisor allow-list",
            ),
            _step(
                "excluded",
                "Probe an excluded tool on the configured endpoint",
                "Foundry toolbox allowed_tools",
                "Platform enforcement: toolbox allowed_tools",
            ),
            END,
        ),
        ("scripts.run_scenario.story4_identity.Step5",),
        (
            "Application correlation is not a joined distributed trace or authenticated tool-side user.",
            "A recorded PASS is preserved, not re-audited here: "
            "generic transport/parsing failures cannot prove denial.",
            "An allowed delegated header with an existing grant is not a grant-absence negative control.",
        ),
    ),
    7: TaskFlow(
        "story-4-step-7",
        "Story 4 Step 7: Direct Research invocation",
        (
            BASELINE,
            _step(
                "conversations",
                "Create separate Supervisor and Research conversations",
                "Client metadata operations",
                "Separate conversation IDs",
            ),
            _step(
                "first",
                "Send the synthetic code word directly to Research",
                "Client -> Research",
                "Direct response from Research",
            ),
            _step(
                "recall",
                "Ask Research to recall it in the same conversation",
                "Client -> Research",
                "Conversation continuity on Research",
            ),
            _step(
                "bypass",
                "Verify the client did not invoke the Supervisor model",
                "Runner request inventory",
                "Supervisor (Agent A) not invoked",
            ),
            END,
        ),
        ("scripts.run_scenario.story4_agent_patterns.Step7",),
        ("Direct access bypasses Supervisor routing; this single-caller task does not establish two-user isolation.",),
    ),
}

NATIVE_SUPERVISOR = TaskFlow(
    "story-4-step-6-native",
    "Story 4 Step 6: Native Supervisor contract preflight",
    (
        NATIVE_BASELINE,
        FlowStep("review", "Require reviewed native analytical evidence", "Runner baseline/evidence guard"),
        _step(
            "contract",
            "Check whether trusted nested session/file authorization is supported",
            "Native integration preflight",
            "Optional native Supervisor integration",
        ),
        FlowStep(
            "stop",
            "Retain the blocker before any analytical submission",
            "No identity workaround or automatic redesign",
        ),
        NATIVE_END,
    ),
    ("scripts.run_scenario.story1_native.NativeValidation.supervisor",),
    ("The current handler is a preflight blocker, not an implemented native analytical delivery path.",),
)

STORY1: dict[int, TaskFlow] = {
    1: TaskFlow(
        "story-1-step-1-native",
        "Story 1 Step 1: Native readiness",
        (
            NATIVE_BASELINE,
            FlowStep("prepare", "Prepare an owned seeded fixture", "Native owner/session/workflow guard"),
            ANALYSIS,
            FlowStep("files", "Retrieve session-scoped originals and verify receipts", "Caller + runner"),
            NATIVE_END,
            FlowStep("baseline-write", "Write a new baseline only after required checks pass", "Validation runner"),
        ),
        ("scripts.run_scenario.story1_native.NativeValidation.execute", "app.services.story1_native"),
    ),
    2: TaskFlow(
        "story-1-step-2-native",
        "Story 1 Step 2: Analysis and chart review",
        (
            NATIVE_BASELINE,
            FlowStep("prepare", "Prepare the fixed owned input", "Backend fixture / ownership checks"),
            ANALYSIS,
            _step(
                "chart",
                "Review the exact original chart separately",
                "User-approved hash-bound receipt",
                "Chart meaning",
            ),
            NATIVE_END,
        ),
        (
            "scripts.run_scenario.story1_native.NativeValidation.analyze",
            "scripts.run_scenario.story1_native.NativeValidation.chart_review",
        ),
        ("PNG format validity is not semantic chart approval. Missing human review remains blocked.",),
    ),
    3: TaskFlow(
        "story-1-step-3-native",
        "Story 1 Step 3: Finite dataset ladder",
        (
            NATIVE_BASELINE,
            FlowStep("profile", "Select the approved baseline or pressure profile", "Runner + backend limits"),
            ANALYSIS,
            FlowStep(
                "memory",
                "Record interpreter RSS, cgroup limit, and timings",
                "Interpreter readings; runner records",
                (r"[0-9][0-9,]* rows: interpreter memory",),
            ),
            _step(
                "target",
                "Stop at the target, first failure, or finite case ceiling",
                "No automatic retry or limit increase",
                "Memory-pressure target",
                "Capacity ladder stopped",
            ),
            NATIVE_END,
        ),
        ("scripts.run_scenario.story1_native.NativeValidation.capacity",),
        ("Accepting the POC envelope does not change an unreached 75% diagnostic check to PASS.",),
    ),
    4: TaskFlow(
        "story-1-step-4-native",
        "Story 1 Step 4: Two-user application isolation",
        (
            NATIVE_BASELINE,
            FlowStep("users", "Verify two genuine caller profiles", "Existing isolated CLI profiles"),
            ANALYSIS,
            FlowStep(
                "foreign",
                "Try foreign workflow/file access with owner controls",
                "Named Consumer callers / application and Session Files",
                (r"Owner [12]: foreign (?:workflow|session file) denied",),
            ),
            _step(
                "clean",
                "Try the original workflow from a clean same-user session",
                "Fresh owned session",
                "Same user, clean session",
            ),
            NATIVE_END,
        ),
        ("scripts.run_scenario.story1_native.NativeValidation.isolation",),
        ("This does not prove universal native-file confidentiality against privileged project users.",),
    ),
}

BLOB = TaskFlow(
    "story-1-step-5-blob",
    "Story 1 Step 5: Blob-backed reconstruction",
    (
        NATIVE_BASELINE,
        ANALYSIS,
        _step(
            "blob",
            "Write the checkpoint and require verified Blob readback",
            "Authenticated runner -> customer Blob",
            "Required Blob checkpoint",
        ),
        FlowStep(
            "fresh",
            "Recompute in a fresh authorized session and interpreter",
            "Native analysis + independently checked originals",
        ),
        _step(
            "compare",
            "Compare input, aggregates, and distinct interpreter binding",
            "Validation runner",
            "Blob-backed reconstruction",
        ),
        NATIVE_END,
    ),
    ("scripts.run_scenario.story1_native.NativeValidation.recovery",),
    (
        "The readback helper has no standalone PASS on success; downstream reconstruction evidence is separate.",
        "Blob checkpoint reconstruction is not an entire Hosted-session or interpreter-RAM restore. "
        "No deletion by default.",
    ),
)
HOSTED = TaskFlow(
    "story-1-step-5-hosted",
    "Story 1 Step 5: Metadata-only Hosted idle/resume",
    (
        NATIVE_BASELINE,
        _step(
            "prepare",
            "Prepare an owned workflow without native analysis",
            "Backend synthetic fixture",
            "Metadata-only preparation",
        ),
        _step(
            "active",
            "Observe process/HOME and read platform active state",
            "Agent observe + SDK session metadata",
            "Platform state before quiet interval",
        ),
        _step(
            "idle",
            "Wait, then check SDK idle state and a fresh stop before resume",
            "Bounded read-only metadata polling",
            "Platform state before resume",
        ),
        _step(
            "resume",
            "Resume the same binding and compare process/HOME",
            "Agent observe",
            "Optional Hosted marker observation",
        ),
        _step(
            "witness",
            "Verify ordered platform active / idle / active metadata",
            "Independent SDK lifecycle witness",
            "Platform idle boundary witness",
        ),
        NATIVE_END,
    ),
    (
        "scripts.run_scenario.story1_native.NativeValidation.hosted_only",
        "scripts.run_scenario.story1_native.NativeValidation.hosted_observation",
    ),
    (
        "The diagram describes the current controller, not the historical timing or proof of older runs.",
        "Quiet time alone is not an idle witness. This track has zero native analyses and no Blob reconstruction.",
    ),
)
REVIEW = TaskFlow(
    "story-1-step-2-review",
    "Story 1 Step 2: Offline retained-chart review",
    (
        _step(
            "originals",
            "Recheck retained original hashes and independent oracle",
            "Local validation only",
            "Retained analytical originals",
        ),
        _step(
            "chart",
            "Check the user receipt against the exact original PNG",
            "User review / chart hash",
            "Chart meaning",
        ),
        FlowStep("record", "Record the offline assessment without another analysis", "Local evidence writer"),
    ),
    ("scripts.run_scenario.story1_native.offline_chart_review",),
    ("This is an offline review of prior originals, not a new native execution.",),
)


STORY6_AUDIT = TaskFlow(
    "story-6-history-audit",
    "Story6: Caller, actual conversation items and reported invocation",
    (
        _step(
            "caller",
            "Verify the original authenticated requester and pinned aliases",
            "CLI account + authenticated Graph; aliases are not authority",
            "Authenticated caller correlation",
        ),
        _step(
            "before",
            "Read actual stored conversation items before this command",
            "Bounded, owner-authorized conversation listing",
            "Conversation history before command",
        ),
        _step(
            "command",
            "Submit exactly the selected start, inspect or explicit decision",
            "Client -> dedicated Hosted coordinator; inspect does not execute a model/tool",
            "Coordinator command",
        ),
        _step(
            "after",
            "Read stored items and compare operation/pending/decision bindings",
            "Bounded, owner-authorized conversation listing",
            "Conversation history after command",
        ),
        _step(
            "binding",
            "Record logical phase, original file and provider-reported calls",
            "Coordinator reply + application correlation; not independent server effects",
            "Operation and reported invocation binding",
        ),
    ),
    (
        "scripts.run_scenario.story6_history.snapshot",
        "scripts.run_scenario.story6_hosted.execute",
        "app.services.story6_approval.Coordinator",
    ),
    (
        "Each selected command has its own before/after observations; pending is not approval success.",
        "No raw transcript, independent downstream invocation span or end-user delegation is inferred.",
    ),
)
STORY6_OWNER = TaskFlow(
    "story-6-owner-continuation",
    "Story6 Step6: Original owner explicitly continues the same operation",
    (
        _step(
            "profiles",
            "Verify the two existing authenticated CLI profiles",
            "Separate credential caches; no sign-in or operator fallback",
            "Two authenticated isolated profiles",
        ),
        _step(
            "decision",
            "Submit the owner's chosen decision on the saved pending case",
            "Original User A credential -> same Hosted session/conversation/operation",
            "User A owner continuation",
        ),
    ),
    ("scripts.run_scenario.story6_users.execute",),
    ("The user's explicit choice precedes this task; no automatic owner approval is inferred.",),
)
STORY6_ENTRA = TaskFlow(
    "story-6-entra-mcp",
    "Story6: Direct Entra MCP approval or rejection, without toolbox",
    (
        _step(
            "pending",
            "Start through Hosted coordinator and inspect the approval pause",
            "Requester -> coordinator -> Research context -> native Responses",
            "Entra MCP approval pause",
        ),
        _step(
            "decision",
            "Submit the human's explicit bound decision to the same operation",
            "Authenticated requester -> Hosted coordinator",
            "Coordinator command",
        ),
        _step(
            "receipts",
            "Compare independent server counts and matching execution receipts",
            "One Entra-protected MCP tool; same-epoch sequential observations",
            "Entra MCP execution evidence",
        ),
    ),
    ("scripts.run_scenario.story6_hosted.execute", "app.story6_mcp", "app.services.story6_approval.Coordinator"),
    (
        "Approval produced one matching receipt; rejection produced no new effect in a separate case.",
        "Entra authenticates the workload; native Responses enforces approval, not the MCP server.",
    ),
)

STORY6: dict[int, TaskFlow] = {
    1: TaskFlow(
        "story-6-step-1",
        "Story6 Step1: Deployment and runtime permission metadata",
        (
            _step(
                "readiness",
                "Verify active isolated coordinator and runtime-role presence",
                "Read-only operator metadata; no model inference",
                "Deployment/access metadata",
            ),
        ),
        ("scripts.run_scenario.story6_approval.hosted_readiness",),
        ("A metadata PASS is not effective invocation or approval enforcement proof.",),
    ),
    2: TaskFlow(
        "story-6-step-2",
        "Story6 Step2: Native MCP pending, approval or rejection command",
        (
            FlowStep(
                "binding",
                "Reuse the pinned case or explicitly create one new operation",
                "Authenticated client -> isolated Hosted coordinator",
            ),
            FlowStep(
                "native",
                "Request native approval or continue the exact explicit decision",
                "Coordinator -> Research context on start -> native Responses MCP",
            ),
            _step(
                "result",
                "Record the returned phase, decision, original file and call count",
                "Actual coordinator response; not independent downstream execution",
                "Coordinator command",
            ),
        ),
        ("scripts.run_scenario.story6_hosted.execute", "app.services.story6_approval.Coordinator"),
        (
            "One saved command does not prove the entire approve/reject lifecycle by itself.",
            "The separate Research context call happens on start only; decision continuation does not repeat it.",
        ),
    ),
    3: TaskFlow(
        "story-6-step-3",
        "Story6 Step3: Fresh-client discovery of the same pending approval",
        (
            FlowStep("open", "Open the saved pinned case with the original authenticated credential", "Fresh client"),
            FlowStep(
                "inspect", "Read the existing owner-bound operation without a model/tool call", "Hosted coordinator"
            ),
            _step(
                "result",
                "Record the actual pending phase, tool/query binding and file",
                "Coordinator inspect response",
                "Coordinator command",
            ),
        ),
        ("scripts.run_scenario.story6_hosted.execute", "app.services.story6_approval.Coordinator.inspect"),
        ("Custom discovery/polling is not a native Event Grid or webhook approval feed.",),
    ),
    4: TaskFlow(
        "story-6-step-4",
        "Story6 Step4: Inspect original operation and pause-file continuity",
        (
            FlowStep("reuse", "Open the original operation, never create a replacement", "Authenticated owner client"),
            _step(
                "observe",
                "Inspect logical phase, original file hash and reported calls",
                "Hosted coordinator; no model/tool execution from inspect",
                "Coordinator command",
            ),
        ),
        ("scripts.run_scenario.story6_hosted.execute", "app.services.story6_approval.Coordinator.reply"),
        ("A retained HOME sentinel or changed boot marker does not prove injected process-loss recovery.",),
    ),
    5: TaskFlow(
        "story-6-step-5",
        "Story6 Step5: Authenticated direct MCP approval boundary",
        (
            _step(
                "definition",
                "Verify the exact isolated immutable search-only toolbox",
                "Read-only operator metadata",
                "Approval-required proxy definition",
            ),
            _step(
                "metadata",
                "Initialize and verify MCP require_approval=always metadata",
                "Valid Entra-token client -> first-party Foundry toolbox",
                "Runtime MCP approval metadata",
            ),
            _step(
                "control",
                "Prove ordinary authorized direct tool access",
                "Fixed read-only search; no approval message",
                "Authorized direct tool control",
            ),
            _step(
                "direct",
                "Call directly without approval and preserve the enforcement verdict",
                "Same authorized direct route, not a Hosted Agent request",
                "Direct valid-token call without approval",
            ),
            _step(
                "permission",
                "Test the existing Consumer user's ordinary proxy access",
                "Separate CLI profile; not an approval-denial test",
                "Invoke-only user direct toolbox permission",
            ),
        ),
        ("scripts.run_scenario.story6_boundary.execute", "scripts.run_scenario.story6_boundary.RPC"),
        (
            "Unapproved returned data remains an enforcement FAIL even when the evaluation task ran correctly.",
            "The authenticated boundary is the Foundry proxy; downstream Learn is public.",
        ),
    ),
    6: TaskFlow(
        "story-6-step-6",
        "Story6 Step6: Two real users and unauthorized approval attempts",
        (
            _step(
                "profiles",
                "Verify distinct authenticated profiles without login or role changes",
                "Existing User A and User B CLI caches",
                "Two authenticated isolated profiles",
            ),
            _step(
                "owner",
                "User A starts a real pending approval",
                "Owner Hosted session/conversation",
                "User A pending positive control",
            ),
            _step(
                "route",
                "Prove User B reaches coordinator ownership handling in B's own context",
                "B's authorized route; missing B operation is the expected control",
                "User B reaches coordinator ownership boundary",
            ),
            _step(
                "session",
                "Attempt foreign inspect and approval using A's session",
                "User B -> Foundry session authorization",
                "User B inspects A's session",
                "User B attempts decision in A's session",
            ),
            _step(
                "operation",
                "Attempt A's operation and valid approval reference in B's own context",
                "User B -> coordinator/user-isolated state ownership",
                "User B inspects A's operation in B's session",
                "User B attempts A's decision in B's session",
            ),
            _step(
                "unchanged",
                "User A confirms original pending request/file and zero reported calls",
                "Original owner credential; no automatic terminal decision",
                "User A pending unchanged after foreign attempts",
            ),
        ),
        ("scripts.run_scenario.story6_users.execute", "scripts.run_scenario.story6_users.probe"),
        (
            "Authentication failure, generic forbidden responses and transport errors are not approval-denial passes.",
            "The owner terminal decision is a separate explicitly chosen continuation.",
        ),
    ),
}


def select_task_flow(provenance: Mapping[str, object], checks: Sequence[Mapping[str, object]]) -> TaskFlow | None:
    """Select only a reviewed scenario/variant, never infer a plan from free text."""
    scenario = provenance.get("scenario")
    match = SCENARIO.fullmatch(scenario) if isinstance(scenario, str) else None
    if match is None:
        return None
    story, number = int(match[1]), int(match[2])
    variant = provenance.get("variant")
    native = variant in ("native", "native-hosted-only", "native-hosted-observation")
    names = {check.get("name") for check in checks if isinstance(check.get("name"), str)}
    if story == 6:
        if variant not in (None, "") or provenance.get("mode") not in {
            "hosted",
            "hosted-history-audit",
            "hosted-two-users",
            "hosted-metadata",
            "authenticated-direct-mcp",
        }:
            return None
        if number in (2, 3, 4) and "Conversation history before command" in names:
            return STORY6_AUDIT
        if number == 2 and "Entra MCP execution evidence" in names:
            return STORY6_ENTRA
        if number == 6 and "User A owner continuation" in names:
            return STORY6_OWNER
        return STORY6.get(number)
    if story == 3:
        return STORY3.get(number) if variant in (None, "") else None
    if story == 4:
        if number == 6:
            return NATIVE_SUPERVISOR if native else None
        if variant not in (None, ""):
            return None
        return STORY4.get(number)
    if story != 1 or not native:
        return None
    if number == 2 and "Retained analytical originals" in names:
        return REVIEW
    if number == 5:
        if variant in ("native-hosted-only", "native-hosted-observation"):
            return HOSTED
        return BLOB if "Blob-backed reconstruction" in names or variant == "native" else None
    return STORY1.get(number)


def render_task_svg(flow: TaskFlow, evidence: Mapping[str, str], *, prefix: str) -> str:
    """Render XML-escaped plan nodes with separately labeled check-evidence badges."""
    if not SAFE_ID.fullmatch(prefix) or not flow.steps:
        raise ValueError("A task SVG requires a safe prefix and at least one plan step.")
    keys = [step.key for step in flow.steps]
    if len(set(keys)) != len(keys) or any(not SAFE_ID.fullmatch(key) for key in keys):
        raise ValueError("Task-flow step keys must be unique safe slugs.")
    edges = flow.edges or tuple(FlowEdge(first, second) for first, second in pairwise(keys))
    if any(edge.source not in keys or edge.target not in keys for edge in edges):
        raise ValueError("Task-flow edges must reference reviewed steps.")
    positions: dict[str, tuple[int, int]] = {}
    cursor = 88
    wrapped = []
    for step in flow.steps:
        title = wrap(step.title, width=65) or [""]
        actor = wrap("Actor: " + step.actor, width=84) or [""]
        height = 52 + 23 * len(title) + 19 * len(actor)
        positions[step.key] = cursor, height
        wrapped.append((step, title, actor))
        cursor += height + 46
    svg = Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "viewBox": f"0 0 1000 {cursor}",
            "role": "img",
            "aria-labelledby": f"{prefix}-title {prefix}-description",
            "style": "width:100%;height:auto;display:block",
        },
    )
    SubElement(svg, "title", {"id": f"{prefix}-title"}).text = flow.title
    SubElement(svg, "desc", {"id": f"{prefix}-description"}).text = SCOPE
    defs = SubElement(svg, "defs")
    marker = SubElement(
        defs,
        "marker",
        {
            "id": f"{prefix}-arrow",
            "viewBox": "0 0 10 10",
            "refX": "9",
            "refY": "5",
            "markerWidth": "7",
            "markerHeight": "7",
            "orient": "auto-start-reverse",
        },
    )
    SubElement(marker, "path", {"d": "M0 0 L10 5 L0 10 Z", "fill": "#516477"})
    SubElement(
        svg, "text", {"x": "70", "y": "35", "fill": "#19314b", "font-size": "19", "font-family": "system-ui"}
    ).text = "CODE-DEFINED PLAN — NOT AN EXECUTION TRACE"
    SubElement(
        svg, "text", {"x": "70", "y": "60", "fill": "#516477", "font-size": "16", "font-family": "system-ui"}
    ).text = "Badges describe linked check records, not inferred actions or identity propagation."
    for edge in edges:
        source_y, height = positions[edge.source]
        target_y, _ = positions[edge.target]
        start, end = source_y + height, target_y - 7
        adjacent = keys.index(edge.target) == keys.index(edge.source) + 1
        path = f"M460 {start} V{end}" if adjacent else f"M850 {source_y + height // 2} H940 V{target_y + 30} H850"
        SubElement(
            svg,
            "path",
            {
                "d": path,
                "fill": "none",
                "stroke": "#516477",
                "stroke-width": "2",
                "stroke-dasharray": "5 4",
                "marker-end": f"url(#{prefix}-arrow)",
            },
        )
        if edge.label:
            SubElement(
                svg,
                "text",
                {
                    "x": "480" if adjacent else "865",
                    "y": str(end - 8),
                    "font-size": "14",
                    "font-family": "system-ui",
                    "fill": "#516477",
                },
            ).text = edge.label
    colors = {"PASS": "#176538", "FAIL": "#a51b26", "BLOCKED": "#805000", "SKIP": "#805000"}
    for index, (step, title, actor) in enumerate(wrapped, 1):
        y, height = positions[step.key]
        group = SubElement(svg, "g", {"id": f"{prefix}-{step.key}"})
        SubElement(
            group,
            "rect",
            {
                "x": "70",
                "y": str(y),
                "width": "780",
                "height": str(height),
                "rx": "10",
                "fill": "#f3f6fa",
                "stroke": "#a4b3c3",
                "stroke-width": "2",
            },
        )
        line_y = y + 29
        for line_index, line in enumerate(title):
            SubElement(
                group,
                "text",
                {
                    "x": "88",
                    "y": str(line_y),
                    "fill": "#19314b",
                    "font-size": "18",
                    "font-family": "system-ui",
                    "font-weight": "600",
                },
            ).text = (f"{index}. " if line_index == 0 else "") + line
            line_y += 23
        for line in actor:
            SubElement(
                group,
                "text",
                {
                    "x": "88",
                    "y": str(line_y),
                    "fill": "#516477",
                    "font-size": "15",
                    "font-family": "system-ui",
                },
            ).text = line
            line_y += 19
        badge = evidence.get(step.key, "NOT RECORDED")
        status = badge.split(" ", 1)[0]
        SubElement(
            group,
            "text",
            {
                "x": "88",
                "y": str(line_y + 5),
                "fill": colors.get(status, "#516477"),
                "font-size": "15",
                "font-family": "system-ui",
                "font-weight": "600",
            },
        ).text = "Mapped check evidence: " + badge
    return tostring(svg, encoding="unicode")


def build_task_flow(
    provenance: Mapping[str, object],
    checks: Sequence[Mapping[str, object]],
    *,
    prefix: str,
    anchors: Mapping[int, str] | None = None,
) -> dict[str, JsonValue]:
    """Attach exact recorded assertions without evaluating or inventing task success."""
    flow = select_task_flow(provenance, checks)
    if flow is None:
        return {
            "available": False,
            "version": FLOW_VERSION,
            "notice": "No reviewed flow definition for this scenario/variant. No execution flow is inferred.",
        }
    if not SAFE_ID.fullmatch(prefix):
        raise ValueError("Task-flow prefix must be a safe slug.")
    nodes: list[JsonValue] = []
    badges = {}
    for step in flow.steps:
        references: list[JsonValue] = []
        statuses: list[str] = []
        for index, check in enumerate(checks, 1):
            name, status = check.get("name"), check.get("status")
            if not isinstance(name, str) or not any(re.fullmatch(pattern, name) for pattern in step.checks):
                continue
            if not isinstance(status, str) or status not in STATUSES:
                raise ValueError("A mapped task-flow check must have a supported recorded status.")
            ordinal = check.get("ordinal", index)
            if type(ordinal) is not int or ordinal < 1:
                raise ValueError("A mapped task-flow check must have a valid ordinal.")
            anchor = anchors.get(ordinal) if anchors is not None else f"check-{prefix}-{ordinal}"
            if not isinstance(anchor, str) or not SAFE_ID.fullmatch(anchor):
                raise ValueError("A mapped task-flow check must link to a safe evidence anchor.")
            references.append({"name": name, "status": str(status), "anchor": anchor})
            statuses.append(str(status))
        counts: dict[str, JsonValue] = {status: count for status, count in Counter(statuses).items()}
        state = statuses[0] if statuses and len(counts) == 1 else "MIXED" if statuses else "NOT RECORDED"
        badge = state + (" (" + ", ".join(f"{key}: {value}" for key, value in counts.items()) + ")" if counts else "")
        badges[step.key] = badge
        nodes.append(
            {
                "key": step.key,
                "title": step.title,
                "actor": step.actor,
                "evidence_status": state,
                "counts": counts,
                "references": references,
            }
        )
    return {
        "available": True,
        "version": FLOW_VERSION,
        "definition": flow.key,
        "title": flow.title,
        "notice": SCOPE,
        "sources": list(flow.sources),
        "notes": list(flow.notes),
        "nodes": nodes,
        "svg": render_task_svg(flow, badges, prefix=f"flow-{prefix}"),
    }
