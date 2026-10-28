"""Reference-only technical appendix for boundary and completion-field explanations.

No evidence acquisition or runtime inspection occurs here. Recorded values and check
links are supplied by the offline exporter; design instructions never become outcomes.
"""

import json
from dataclasses import dataclass
from hashlib import sha256

from pydantic import JsonValue

from scripts.report_api_catalog import ApiEntry

RUNNER = "scripts/run_scenario/consumer_code_interpreter.py"
SERVICE = "src/backend/app/services/consumer_code_interpreter.py"
PROBES = "scripts/run_scenario/native_bypass.py"
EXPORTER = "scripts/generate_report.py"
CONSUMER_DIAGRAM = "appendix-sequence-consumer"
NATIVE_DIAGRAM = "appendix-sequence-native"


@dataclass(frozen=True)
class Reference:
    purpose: str
    flow: tuple[str, ...]
    apis: tuple[str, ...]
    implementation: tuple[str, ...]
    prerequisites: str
    limitations: str
    diagram: str = CONSUMER_DIAGRAM


def boundary_references() -> dict[str, Reference]:
    """One explicit explanation per reviewed boundary, including non-testable/excluded areas."""
    return {
        "Application workflow/artifact controls": Reference(
            "Check that opaque references never substitute for trusted user/session/workflow authorization.",
            (
                "user1 prepares a workflow in user1's authenticated Hosted session/conversation.",
                "The backend records trusted caller/session/conversation fingerprints in the user-isolated registry.",
                "user2 submits user1's workflow or artifact reference through user2's own command client.",
                "ConsumerWorkflow.owned rejects a mismatched or unavailable binding before native resource access.",
                "user1's positive controls before/after establish continued access; repeat the mirrored direction.",
            ),
            ("consumer-command",),
            (
                RUNNER + " — Proof.command, Proof.deny, Proof.direction",
                SERVICE + " — ConsumerWorkflow.owned, inspect",
                "src/backend/app/services/foundry_state.py — Caller.current, open_store, FoundryStore",
            ),
            "Verified Consumer-only roles, active consumer interface, real distinct user sessions and valid workflow.",
            "This tests application authorization, not permissions for direct native API calls or privileged users.",
        ),
        "Hosted Session Files original output downloads": Reference(
            "Deliver original generated JSON/PNG bytes only through the authorized user's Hosted file boundary.",
            (
                "user1 uploads synthetic input.xlsx through agents.upload_session_file using user1's credential.",
                "The backend authorizes user1's input, runs native analysis, "
                "validates native citations/container binding "
                "and stages original result.json/chart.png bytes in the authorized Hosted HOME.",
                "user1 downloads both staged outputs; local byte length and SHA-256 must match backend receipts.",
                "user2 attempts the exact user1 session/path download with "
                "user2's Consumer client, not a model prompt.",
                "Record actual denial and reverify user1's same original bytes before/after; mirror for user2.",
            ),
            ("consumer-upload", "backend-upload", "backend-analysis", "backend-output", "consumer-download"),
            (
                RUNNER + " — Proof.own_analysis, session_download, owner_artifact, direction",
                SERVICE + " — ConsumerWorkflow.analyze, original_outputs, home_path",
            ),
            "Valid input/analysis/output controls and a live Hosted session. This uses already-created original bytes.",
            "Hash equality proves unchanged delivery, not universal API isolation or chart aesthetics.",
        ),
        "Hosted responses": Reference(
            "Test retrieval of another user's Hosted response independently from file download authorization.",
            (
                "Retain the response reference from user1's successful analysis in harness memory.",
                "user1 retrieves that response as the positive control.",
                "user2 calls responses.retrieve for the same reference under user2's Consumer credential.",
                "A specific session authorization denial is required; "
                "user1 must still retrieve the response afterward.",
                "Repeat with user1 targeting user2. These GET requests do not create a new model turn.",
            ),
            ("consumer-response",),
            (RUNNER + " — Proof.hosted_response, deny, direction",),
            "A completed owner response that still exists and two valid Consumer clients.",
            "Response retrieval only; not every response-chain, history or session lifecycle operation.",
        ),
        "Native Files listing": Reference(
            "Check the Consumer's project-file listing privilege without claiming file-content isolation.",
            (
                "Verify user1 and user2 have the intended agent-scoped Consumer-only effective permissions.",
                "Each Consumer calls the native Files list API with limit=1.",
                "Record actual HTTP/error classification; an unrelated service or authentication error is not a pass.",
                "No exact foreign resource is selected, so this is not a resource-existence positive-control test.",
            ),
            ("consumer-native-list",),
            (RUNNER + " — Proof.native_listing, verify_consumer_roles, verify_role_definition",),
            "Existing native project endpoint and authenticated Consumer client; no additional data grants.",
            "A listing denial cannot replace direct metadata or content checks. No prompt or model invocation.",
        ),
        "Native Files metadata/content": Reference(
            "OPTIONAL ADVANCED: determine whether Consumer credentials can "
            "directly read the other input's native resource.",
            (
                "An approved deployment public key seals backend-recorded native lineage from the same fresh analyses.",
                "The harness decrypts locally and validates workflow/session/conversation/input/output byte bindings.",
                "privileged-control verifies the exact native input metadata or original content before the attempt.",
                "user2 calls native Files retrieve/content for user1's exact input under user2's Consumer credential.",
                "Recheck the same resource with privileged-control and normal user1 Hosted delivery; mirror for user2.",
            ),
            ("bypass-input-metadata", "control-input-metadata", "bypass-input-content", "control-input-content"),
            (
                PROBES + " — request, authorized_denial, direction",
                "src/backend/app/services/probe_lineage.py — Lineage, seal, unseal",
                RUNNER + " — Proof.own_analysis, execute",
            ),
            "Separate Advanced approval, deployed public-key binding, matching local encrypted private key and "
            "passphrase, a distinct authorized privileged-control operator, unexpired exact references.",
            "Not required for consumer-facing COMPLETE. No extra analysis, Consumer privilege expansion, token "
            "fallback or foreign payload capture. A failed positive control blocks the probe.",
            NATIVE_DIAGRAM,
        ),
        "Native container-file listing/content": Reference(
            "OPTIONAL ADVANCED: test direct listing, metadata and original JSON/PNG download authorization.",
            (
                "Resolve only SDK-native container/output references from sealed fresh-run lineage in harness memory.",
                "privileged-control checks the exact container listing or output metadata/content; byte controls "
                "verify the original hash and length.",
                "user2 requests user1's container listing and each JSON/PNG metadata/content route using user2's "
                "Consumer credential. These are GET operations, not analysis prompts.",
                "Close unexpected successful foreign content responses without reading/logging the payload; stop.",
                "Recheck exact privileged controls and the allowed user1 Hosted delivery; repeat mirrored direction.",
            ),
            (
                "bypass-container-list",
                "control-container-list",
                "bypass-output-metadata",
                "control-output-metadata",
                "bypass-output-content",
                "control-output-content",
            ),
            (PROBES + " — request, direction", RUNNER + " — Proof.consume_probe, owner_artifact"),
            "Same separate Advanced instrumentation/approval as native input probes; exact live original outputs. "
            "The harness supplies no extra Consumer native permissions.",
            "Listing, metadata and content are distinct assertions. Controls confirm resource existence; "
            "their privileged success is not a privileged-user isolation result.",
            NATIVE_DIAGRAM,
        ),
        "Privileged project-user isolation": Reference(
            "EXCLUDED: explain why broad project developers are a different threat model from Consumers.",
            (
                "Inventory actual direct/inherited/group privileges before interpreting diagnostic access.",
                "Earlier privileged diagnostic runs used native Files/container APIs and observed cross-user access.",
                "To investigate further, separately approve the privilege profile, exact resources, controls and "
                "diagnostic execution budget; preserve Consumer roles and original receipts.",
            ),
            ("bypass-input-content", "bypass-output-content", "control-output-content"),
            (
                "scripts/run_scenario/native_code_interpreter.py — Experiment.direction",
                "scripts/deploy/grant_agent_rbac.py — native_user_plans",
            ),
            "A separate privileged-user experiment. Do not silently restore project Foundry User to Consumer testers.",
            "Consumer-only isolation does not promise that project administrators/developers "
            "cannot access native files. "
            "The catalog methods explain shared SDK surfaces, not identical authentication or observed execution.",
            NATIVE_DIAGRAM,
        ),
        "VM / Hyper-V isolation": Reference(
            "DOCUMENTED PLATFORM ASSURANCE: distinguish Microsoft's managed sandbox "
            "boundary from application evidence.",
            (
                "Consult the linked Microsoft Code Interpreter sandbox documentation "
                "and its applicable managed service.",
                "Record the source/reference-review date and the statement that "
                "dynamic sessions have a Hyper-V boundary.",
                "Keep that platform statement separate from actual application "
                "checks and recorded execution-container IDs.",
                "Any independent infrastructure security assurance requires "
                "a separate authorized review outside this proof.",
            ),
            (),
            (
                "scripts/generate_report.py — generate_report: coverage classification",
                "scripts/report_assets/validation.html — platform assurance explanation",
            ),
            "Documentation/reference review only. No application SDK method independently proves the Hyper-V boundary.",
            "No penetration test, VM introspection, isolation certification or application PASS is implied. "
            "Reference reviewed 2026-10-26; managed service documentation may evolve.",
        ),
        "Chart visual quality": Reference(
            "EXCLUDED: explain the difference between a valid original PNG and a visually correct/useful chart.",
            (
                "The model-generated Python creates chart.png in managed Code Interpreter.",
                "The backend validates the PNG header and preserves original bytes; "
                "the client checks receipt hashes/lengths.",
                "If visual quality becomes required, define a separate rubric for labels, values, units, readability "
                "and accessibility, then record a human review or explicitly designed image checks.",
                "Do not infer aesthetic or semantic chart approval from the PNG header or unchanged-byte checks.",
            ),
            ("backend-analysis", "backend-output", "consumer-download"),
            (SERVICE + " — original_outputs", RUNNER + " — Proof.own_analysis"),
            "A separately approved visual-quality requirement and explicit reviewer/rubric; "
            "no new model run is assumed.",
            "Chart aesthetics are outside Consumer isolation acceptance. Numerical JSON results, file format, "
            "original bytes and access authorization remain required.",
        ),
    }


@dataclass(frozen=True)
class CounterReference:
    purpose: str
    flow: tuple[str, ...]
    implementation: str
    interpretation: str
    apis: tuple[str, ...] = ()
    diagram: str = CONSUMER_DIAGRAM


@dataclass(frozen=True)
class CheckLink:
    identifier: str
    name: str
    status: str


@dataclass(frozen=True)
class BoundaryEntry:
    identifier: str
    name: str
    status: str
    evidence: str
    reference: Reference
    apis: tuple[ApiEntry, ...]
    checks: tuple[CheckLink, ...]


@dataclass(frozen=True)
class CounterEntry:
    identifier: str
    name: str
    recorded: bool
    value: str
    reference: CounterReference
    apis: tuple[ApiEntry, ...]


def counter_references() -> dict[str, CounterReference]:
    """Explain actual recorded fields without converting configured limits into executed work."""
    return {
        "run_id": CounterReference(
            "Associate journals and completion with one validation run, not a user or an ownership proof.",
            (
                "ValidationRun allocates a fresh run identifier.",
                "The runner records it in evidence and conclusion.",
                "The exporter rejects mismatched journals/conclusion instead of merging unrelated runs.",
            ),
            "scripts/common/reporting.py — ValidationRun.__init__; " + EXPORTER + " — generate_report",
            "Sanitized as needed. Its presence does not indicate that live tests executed.",
        ),
        "state": CounterReference(
            "Distinguish a written finish record from interrupted evidence.",
            (
                "The runner records assertion outcomes, including failed/blocked stops.",
                "After report-summary preparation it writes state=finished.",
                "The exporter checks the finish against results.",
            ),
            RUNNER + " — main; " + EXPORTER + " — generate_report",
            "finished means the CLI reached completion recording, not that tests passed or every optional test ran.",
        ),
        "mode": CounterReference(
            "Identify whether this was Consumer execution or read-only readiness.",
            (
                "Parse --check and live approvals.",
                "Record readiness or consumer mode in conclusion.",
                "Readiness reports never receive an isolation completion claim.",
            ),
            RUNNER + " — build_parser, main",
            "Readiness can inspect metadata but creates no user analysis/session. "
            "Mode is not the native/diagnostic setting.",
        ),
        "suite": CounterReference(
            "Declare the selected finite validation plan and its optional Advanced scope.",
            (
                "Default consumer-v1 runs the ordinary consumer-facing proof.",
                "Explicit consumer-native-v2 plus separate approval/key setup enables optional native GET probes.",
                "The exporter checks suite-plan/conclusion consistency and "
                "evaluates Consumer and Advanced results separately.",
            ),
            RUNNER + " — build_parser, main, Proof.__init__; " + EXPORTER + " — generate_report",
            "Suite selection is a plan, not execution evidence. Missing legacy suite stays unrecorded.",
        ),
        "finished_at": CounterReference(
            "Record the runner's UTC completion time.",
            (
                "The runner captures datetime.now(UTC) after validation/stop handling.",
                "The exporter compares this to manifest.started_at for a recorded overall duration.",
            ),
            RUNNER + " — main; " + EXPORTER + " — timestamp, generate_report",
            "Overall elapsed time includes orchestration. It is not model latency or a per-operation duration.",
        ),
        "validation_exit_code": CounterReference(
            "Preserve the runner's outcome independently from HTML generation success.",
            (
                "ValidationRun.finish aggregates recorded statuses.",
                "0=all-pass, 1=failure or skip, 2=blocked without failure.",
                "The runner journals this code before offline export; export errors do not rewrite it.",
            ),
            "scripts/common/reporting.py — ValidationRun.finish; " + RUNNER + " — main",
            "A successful HTML export is not a test PASS. A mixed failure/blocked run returns failure.",
        ),
        "analysis_attempts": CounterReference(
            "Count attempted analyze submissions, not every request or successful interpreter execution.",
            (
                "Proof.command checks the two-attempt cap.",
                "It increments before submitting an analyze command.",
                "Completed tool-call evidence and output/numerical checks are separate results.",
            ),
            RUNNER + " — Proof.command, execute",
            "Two attempts need not mean two successful model calls; an error/timeout can leave execution ambiguous.",
            ("consumer-command", "backend-analysis"),
        ),
        "foreign_probes": CounterReference(
            "Count budgeted opposite-user/privilege-boundary probe attempts, not successful denials.",
            (
                "Proof.consume_probe enforces the selected cap.",
                "Probe counters increment before the negative request.",
                "Record actual request result and positive-control assertions; stop on access or failed controls.",
            ),
            RUNNER + " — Proof.consume_probe, deny, native_listing; " + PROBES + " — direction",
            "consumer-v1 caps at 12; optional native-v2 caps at 26. Counter equality alone never proves isolation.",
            ("consumer-download", "consumer-command", "consumer-response", "consumer-native-list"),
        ),
        "native_control_requests": CounterReference(
            "Count optional privileged native resource controls, not Consumer requests or extra model analyses.",
            (
                "Optional native_bypass.request checks its 28-request cap.",
                "The counter increments before privileged metadata/list/content reads.",
                "Controls before/after must verify the exact live sealed-lineage resource; "
                "content checks original bytes.",
            ),
            PROBES + " — request, direction",
            "Legacy/ordinary Consumer runs do not need these controls. "
            "Zero or missing is not a Consumer acceptance gap.",
            (
                "control-input-metadata",
                "control-input-content",
                "control-container-list",
                "control-output-metadata",
                "control-output-content",
            ),
            NATIVE_DIAGRAM,
        ),
        "max_analysis_attempts": CounterReference(
            "Declare the fixed model-submission budget.",
            (
                "The runner records the cap of two.",
                "Proof.command refuses further analyze submissions.",
                "Neither reports nor failed probes replay or expand the cap.",
            ),
            RUNNER + " — Proof.command, main",
            "Configured budget, not actual execution; compare analysis_attempts "
            "and recorded execution/output evidence.",
        ),
        "max_foreign_probes": CounterReference(
            "Declare the selected negative-probe cap.",
            (
                "Proof.__init__ chooses 12 for consumer-v1 or 26 for optional native-v2.",
                "Proof.consume_probe refuses requests after that cap.",
            ),
            RUNNER + " — Proof.__init__, consume_probe, main",
            "A limit is not a count of passed tests. Optional native probes cannot be inferred from a maximum of 26.",
        ),
        "max_seconds": CounterReference(
            "Declare the overall execution deadline in seconds.",
            (
                "Proof tracks monotonic elapsed time from runner preparation.",
                "bounded_execute subtracts preflight time and wraps remaining live activity in asyncio.timeout.",
                "Backend consumer_turn also enforces a 300-second analysis deadline.",
            ),
            RUNNER + " — Proof.__init__, bounded_execute; " + SERVICE + " — consumer_turn",
            "900 seconds ordinary; 1200 optional Advanced. Configuration is not an observed duration or timeout event.",
        ),
        "synthetic_output_capture": CounterReference(
            "Record opt-in local retention of already-downloaded synthetic outputs for report attachments.",
            (
                "--capture-artifacts enables Proof.retain_output.",
                "The runner verifies receipt length/hash before saving only result.json/chart.png under fixed aliases.",
                "The exporter rechecks capture receipts and embeds permitted originals/sanitized JSON copies.",
            ),
            RUNNER + " — Proof.retain_output; " + EXPORTER + " — attachment_records",
            "No extra download or image regeneration. "
            "True does not prove files were captured if execution stopped early.",
            ("consumer-download",),
        ),
        "alias_policy": CounterReference(
            "Keep identity labels stable without publishing UPNs or principal identifiers.",
            (
                "Verify the first supplied UPN and call it user1; verify the second and call it user2.",
                "Record request actor and resource_owner separately for opposite-user checks.",
                "Legacy direction labels are normalized only when recorded evidence establishes them.",
            ),
            RUNNER + " — main, execute; " + EXPORTER + " — display_references, explicit_user_text",
            "Aliases are presentation labels, not authorization inputs. privileged-control is a distinct operator.",
        ),
    }


def _anchor(kind: str, name: str) -> str:
    return f"appendix-{kind}-" + sha256(name.encode()).hexdigest()[:12]


def build_appendix(
    coverage: list[tuple[str, str, str]],
    conclusion: dict[str, JsonValue],
    cases: list[dict[str, object]],
    catalog: list[ApiEntry],
) -> tuple[list[BoundaryEntry], list[CounterEntry]]:
    """Build read-only reference models; caller supplies only sanitized recorded conclusion values."""
    references = boundary_references()
    entries: list[BoundaryEntry] = []
    counter_entries: list[CounterEntry] = []
    by_id = {api["id"]: api for api in catalog}
    for name, status, meaning in coverage:
        reference = references.get(name)
        if reference is None:
            reference = Reference(
                "Explain only recorded checks for this validator.",
                ("Inspect the matching manifest/results and explicit operation evidence.",),
                (),
                (EXPORTER + " — generate_report",),
                "Valid evidence for this validator.",
                "No Consumer-specific execution flow is inferred for an unknown validator.",
            )
        checks: list[CheckLink] = []
        for case in cases:
            related = case.get("api_references")
            if isinstance(related, list) and any(
                isinstance(api, dict) and api.get("id") in reference.apis for api in related
            ):
                checks.append(CheckLink(str(case["id"]), str(case["name"]), str(case["status"])))
        entries.append(
            BoundaryEntry(
                _anchor("boundary", name),
                name,
                status,
                meaning,
                reference,
                tuple(by_id[identifier] for identifier in reference.apis),
                tuple(checks),
            )
        )
    fields = counter_references() if any(name in references for name, _, _ in coverage) else {}
    for field in dict.fromkeys((*fields, *conclusion)):
        counter_reference = fields.get(field)
        if counter_reference is None:
            counter_reference = CounterReference(
                "Preserve an additional recorded completion field without inventing its semantics.",
                ("Inspect the original completion schema and the generating runner for this field.",),
                RUNNER + " — main (field semantics not established by this exporter)",
                "Unknown field: no unit, SDK call, success interpretation or missing-value default is assumed.",
            )
        counter_entries.append(
            CounterEntry(
                _anchor("counter", field),
                field,
                field in conclusion,
                json.dumps(conclusion.get(field), ensure_ascii=True, indent=2)
                if field in conclusion
                else "NOT RECORDED",
                counter_reference,
                tuple(by_id[identifier] for identifier in counter_reference.apis),
            )
        )
    return entries, counter_entries
