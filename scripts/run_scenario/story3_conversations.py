"""Story 3 (#3) multi-agent conversation management against the EXISTING hosted Supervisor and Research agents.

Step 1 (AC1) prepares once (``--mode prepare-if-missing``: ``azd deploy supervisor-agent`` when the Supervisor
does not yet accept ``POC_CONVERSE``) and demonstrates a Supervisor conversation with a separate,
Supervisor-mapped Research conversation. Steps 2-5 reuse the Step 1 baseline and never deploy or change an
agent: follow-up history in the same sub-agent conversation (2), conversation ownership, identity binding, and
allowed/denied access (3), Agent A invoking Agent B with A's conversation ID (4), and the shared versus separate
comparison (5). Synthetic prompts and run-unique markers only.
Exit codes: 0 all checks passed, 1 failure, 2 blocked prerequisite.
"""

import argparse
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import UUID, uuid4

from app.core.redaction import sanitize_text
from app.services.conversations import (
    HOP_TAG,
    MAP_STORE,
    ConversationReport,
    ConverseCommand,
    ConverseIdentity,
    Delivery,
    HistoryStats,
    Listing,
    Pattern,
    conversation_digest,
    history_stats,
)
from app.services.orchestration import CORRELATION_HEADER
from openai import APIStatusError, OpenAIError
from pydantic import ValidationError

from scripts.common.findings import Finding, Outcome
from scripts.common.reporting import (
    REPORT_FILE,
    MarkdownReport,
    Status,
    StepSpec,
    ValidationRun,
    add_output_arguments,
    index_entry,
    status_icon,
    update_index,
)
from scripts.deploy.grant_local_rbac import CliError, command_prefix
from scripts.run_scenario.story1_data_analysis import SNAPSHOT_HEADERS, AgentSnapshot, Blocked, snapshot_agents
from scripts.run_scenario.story4_common import (
    BASELINE_FILE,
    CUSTOM,
    EXPECTED_ERRORS,
    FOUNDRY,
    NOT_SUPPORTED,
    RESEARCH,
    SDK,
    SUPERVISOR,
    Exchange,
    Story4Step,
    classify_user,
    identity_candidates,
    marker,
    role_names,
    token_claims,
    write_baseline,
)

STORY_DIR = "story-3"
INDEX_TITLE = "Story 3 evidence index"
ISSUE = "#3"
TASK_PREFIX = "Agents: Story 3"
MODULE = "scripts.run_scenario.story3_conversations"
CAPABILITY_ERROR = "explicit Supervisor conversation"
DENIED_STATUSES = (401, 403, 404)
Mode = Literal["verify", "prepare-if-missing"]
REMEMBER = "Remember this code word for later in this conversation: {code}. Reply with exactly: STORED"
RECALL = "What code word did I ask you to remember? Reply with only the code word."
RECALL_SHARED = "What code word was given earlier in this conversation? Reply with only the code word."


@dataclass
class Access:
    """A client-side conversation read: the items when allowed, otherwise the HTTP outcome."""

    items: list[dict[str, Any]] | None = None
    http_status: int | None = None
    code: str | None = None
    error: str | None = None

    @property
    def allowed(self) -> bool:
        return self.items is not None

    @property
    def denied(self) -> bool:
        return self.http_status in DENIED_STATUSES

    def outcome(self) -> str:
        if self.items is not None:
            return f"allowed ({len(self.items)} items)"
        if self.http_status is not None:
            return f"{status_word(self.http_status)} (HTTP {self.http_status}{', ' + self.code if self.code else ''})"
        return f"error: {self.error or 'unknown'}"


def status_word(status: int) -> str:
    """Only a 4xx is a denial; a 5xx is a service error, never evidence of access control."""
    return "denied" if 400 <= status < 500 else "error"


def capability_from(exchange: Exchange) -> bool:
    """New Supervisor code rejects POC_CONVERSE without a conversation; older code answers it as chat."""
    return not exchange.completed and CAPABILITY_ERROR in (exchange.error or "")


def listing_outcome(listing: Listing | None) -> str:
    if listing is None:
        return "not listed"
    if listing.items is not None:
        return f"allowed ({listing.items} items)"
    failure = listing.failure
    if failure and failure.http_status is not None:
        word = status_word(failure.http_status)
        return f"{word} (HTTP {failure.http_status}{', ' + failure.code if failure.code else ''})"
    return f"error: {failure.message[:120] if failure else 'unknown'}"


def listing_definitive(listing: Listing | None) -> bool:
    """Allowed, or an explicit client-error status; transport errors and 5xx are not an answer."""
    if listing is None:
        return False
    if listing.items is not None:
        return True
    status = listing.failure.http_status if listing.failure else None
    return status is not None and 400 <= status < 500


def report_outcome(report: ConversationReport | None) -> str:
    """Allowed, denied by the Supervisor mapping, denied by HTTP status, or another failure; never inferred."""
    if report is None:
        return "no report"
    if report.status in ("completed", "inspected"):
        return "allowed"
    if report.status == "denied":
        return "denied by the Supervisor mapping (no outbound call)"
    failure = report.failure
    if failure and failure.http_status is not None:
        word = status_word(failure.http_status)
        return f"{word} (HTTP {failure.http_status}{', ' + failure.code if failure.code else ''})"
    return f"failed: {failure.message[:160] if failure else 'unknown'}"


def definitive(report: ConversationReport | None) -> bool:
    """The platform or the Supervisor gave an explicit answer: success, mapping denial, or a 4xx status."""
    if report is None:
        return False
    if report.status in ("completed", "denied", "inspected"):
        return True
    status = report.failure.http_status if report.failure else None
    return status is not None and 400 <= status < 500


def recalled(report: ConversationReport | None) -> bool:
    return bool(report and report.status == "completed" and report.tokens_in_sub_answer[:1] == [True])


def classify_received(report: ConversationReport | None, history: HistoryStats | None) -> str:
    """What the Supervisor ends up with, from what its conversation actually holds, not from what was requested.

    ``history`` must come from a later inspect that probed the sub-agent's token.
    """
    if report is None or report.status != "completed":
        return "nothing (sub-agent call did not complete)"
    if history is not None and history.hop_inputs and history.other_assistant:
        return "full history"
    if history is not None and history.reply_token_hits[:1] and history.reply_token_hits[0] > 0:
        return "result"
    return "placeholder"


def separate_variant_ok(report: ConversationReport | None, received: str, deliver: Delivery) -> bool:
    """Result delivery: the answer reached the Supervisor and is in its reply. Placeholder: kept out of it."""
    if report is None or received != deliver:
        return False
    if deliver == "result":
        return report.tokens_in_sub_answer == [True] and report.sub_answer is not None
    return report.tokens_in_sub_answer == [True] and report.sub_answer is None


class Story3Step(Story4Step):
    """Story 3 acceptance criterion on the Story 4 harness, with conversation helpers."""

    issue = ISSUE
    story_label = "Story 3"
    module = MODULE

    def new_conversation(self, agent: str) -> str | None:
        """The client creates a platform conversation on an agent endpoint (no model call)."""
        try:
            with self.project.get_openai_client(agent_name=agent, timeout=60, max_retries=0) as client:
                return client.conversations.create().id
        except EXPECTED_ERRORS as error:
            self.obs[f"conversation_error_{agent}"] = sanitize_text(str(error))[:300]
            return None

    def list_conversation(self, agent: str, conversation_id: str) -> Access:
        """The operator lists a conversation through an agent endpoint; a denial is recorded, never retried."""
        try:
            with self.project.get_openai_client(agent_name=agent, timeout=60, max_retries=0) as client:
                items = [item.model_dump(mode="json") for item in client.conversations.items.list(conversation_id)]
            return Access(items=items)
        except APIStatusError as error:
            body = error.body if isinstance(error.body, dict) else {}
            nested = body.get("error") if isinstance(body.get("error"), dict) else body
            code = str(nested.get("code")) if isinstance(nested, dict) and nested.get("code") else None
            return Access(http_status=error.status_code, code=code, error=sanitize_text(str(error))[:300])
        except OpenAIError as error:
            return Access(error=sanitize_text(f"{type(error).__name__}: {error}")[:300])

    def converse(
        self, label: str, conversation: str | None, command: ConverseCommand
    ) -> tuple[Exchange, ConversationReport | None]:
        """Client -> Supervisor POC_CONVERSE in the given Supervisor conversation, with the correlation header."""
        exchange = self.hosted_call(
            label,
            SUPERVISOR,
            command.wire(),
            headers={CORRELATION_HEADER: str(command.correlation_id)},
            conversation=conversation,
        )
        if not exchange.completed:
            return exchange, None
        try:
            return exchange, ConversationReport.model_validate_json(exchange.text)
        except ValidationError as error:
            exchange.error = f"Supervisor reply is not a poc-conversation-v1 report: {sanitize_text(str(error))[:200]}"
            return exchange, None

    def send(
        self,
        label: str,
        conversation: str | None,
        message: str,
        *,
        pattern: Pattern = "separate",
        deliver: Delivery = "result",
        identity: ConverseIdentity = "agent",
        tokens: Sequence[str] = (),
        claim: str | None = None,
        correlation: UUID | None = None,
    ) -> tuple[Exchange, ConversationReport | None]:
        command = ConverseCommand(
            message=message,
            correlation_id=correlation or uuid4(),
            pattern=pattern,
            deliver=deliver,
            identity=identity,
            probe_tokens=list(tokens),
            sub_conversation_id=claim,
        )
        return self.converse(label, conversation, command)

    def inspect(
        self,
        label: str,
        conversation: str | None,
        *,
        pattern: Pattern = "separate",
        identity: ConverseIdentity = "agent",
        tokens: Sequence[str] = (),
        claim: str | None = None,
    ) -> tuple[Exchange, ConversationReport | None]:
        command = ConverseCommand(
            operation="inspect",
            correlation_id=uuid4(),
            pattern=pattern,
            identity=identity,
            probe_tokens=list(tokens),
            sub_conversation_id=claim,
        )
        return self.converse(label, conversation, command)

    def report_observed(self, exchange: Exchange, report: ConversationReport | None) -> dict[str, object]:
        if report is None:
            return exchange.observed()
        return {
            "status": report.status,
            "outcome": report_outcome(report),
            "supervisor_conversation_id": report.supervisor_conversation_id,
            "sub_conversation_id": report.sub_conversation.conversation_id if report.sub_conversation else None,
            "mapping": report.mapping.model_dump() if report.mapping else None,
            "listing_before": listing_outcome(report.listing_before),
            "listing_after": listing_outcome(report.listing_after),
            "failure": report.failure.model_dump() if report.failure else None,
        }


AC1 = (
    "Demonstrate a supervisor conversation and a separate sub-agent conversation. Identify who owns and stores "
    "their mapping."
)
STEP1_CORE = (
    "Supervisor turn in the Supervisor conversation",
    "Separate sub-agent conversation",
    "Mapping owned and stored by the Supervisor",
    "Mapping re-read from the state store",
)


class Step1(Story3Step):
    """Prepare once, then show a Supervisor conversation, a separate Research conversation, and their mapping."""

    number = 1
    spec = StepSpec(3, number, "separate-conversations")
    title = "Story 3 / Step 1 - Supervisor conversation and a separate sub-agent conversation"
    task_label = f"{TASK_PREFIX} - 1 Demonstrate separate conversations"
    ac = AC1
    scope_note = (
        "Reuses the three existing agents. `prepare-if-missing` may, once, run `azd deploy supervisor-agent` so "
        "the Supervisor accepts the Story 3 `POC_CONVERSE` command; its identity is preserved and Research and "
        "Data Analysis are not redeployed. `verify` never changes anything. Synthetic prompts only."
    )

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        super().__init__(args, run, report)
        self.mode: Mode = args.mode
        self.deployed = False
        self.prepared = False

    def extra_fields(self) -> list[list[object]]:
        return [["Mode", self.mode]]

    def command_line(self) -> str:
        return super().command_line() + f" --mode {self.mode}"

    def capable(self) -> bool:
        probe = ConverseCommand(operation="inspect", correlation_id=uuid4())
        return capability_from(self.hosted_call("Capability: Supervisor POC_CONVERSE probe", SUPERVISOR, probe.wire()))

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
        self.report.heading("Story 3 protocol support")
        self.report.paragraph(
            "A `POC_CONVERSE` request without a conversation must be rejected by the Supervisor before any model "
            "or outbound call. Older Supervisor code answers it as plain chat."
        )
        expected = "Supervisor rejects POC_CONVERSE without a conversation (Story 3 code deployed)"
        if self.capable():
            self.record("Story 3 code deployed", "PASS", expected, {"supervisor_version": before[SUPERVISOR].version})
        elif self.mode == "verify":
            raise Blocked("The Supervisor does not accept POC_CONVERSE; rerun Step 1 with prepare-if-missing.")
        else:
            self.deploy_supervisor()
            after = snapshot_agents(self.read)
            self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in after.values()])
            identities = [name for name in before if before[name].principal_id != after[name].principal_id]
            others = [name for name in (RESEARCH,) if before[name].version != after[name].version]
            ok = self.capable() and not identities and not others and all(s.status == "active" for s in after.values())
            self.check(
                "Story 3 code deployed",
                ok,
                "After deploy: Supervisor accepts POC_CONVERSE; identities unchanged; Research not redeployed",
                {"identity_changes": identities, "unexpected_version_changes": others},
            )
            if not ok:
                return None
            before = after
        self.prepared = True
        return before

    def deploy_supervisor(self) -> None:
        """Only the Supervisor needs Story 3 code; specialists already hydrate history from their conversations."""
        azd = command_prefix("azd")
        self.run.console.print("Deploying the existing Supervisor with the Story 3 protocol ...")
        started = perf_counter()
        process = subprocess.Popen(
            [*azd, "deploy", SUPERVISOR, "--environment", self.args.environment, "--no-prompt"],
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
            self.record("Deploy existing Supervisor", "FAIL", f"azd deploy {SUPERVISOR} succeeds", f"exit {code}")
            raise Blocked("The existing Supervisor was not updated; inspect the azd output above.")
        self.deployed = True
        self.record(
            "Deploy existing Supervisor",
            "PASS",
            "New version of the same Supervisor; foundation, identity, and other agents reused",
            {"seconds": round(perf_counter() - started)},
        )

    def body(self) -> None:
        self.report.heading("Conversations")
        supervisor_conversation = self.new_conversation(SUPERVISOR)
        if not self.check(
            "Supervisor conversation created by the client",
            supervisor_conversation is not None,
            "Client (operator) creates a conversation on the Supervisor endpoint",
            {"conversation": supervisor_conversation, **self.obs},
        ):
            return
        assert supervisor_conversation is not None
        code = marker("S3A")
        correlation = uuid4()
        exchange, first = self.send(
            "AC1 R1: client -> Supervisor (separate pattern)",
            supervisor_conversation,
            REMEMBER.format(code=code),
            correlation=correlation,
        )
        sub = first.sub_conversation if first else None
        self.check(
            "Supervisor turn in the Supervisor conversation",
            bool(first and first.status == "completed" and first.supervisor_conversation_id == supervisor_conversation),
            "Completed poc-conversation-v1 report bound to the client's Supervisor conversation",
            self.report_observed(exchange, first),
        )
        after = first.listing_after.items if first and first.listing_after else None
        self.check(
            "Separate sub-agent conversation",
            bool(
                first
                and sub
                and sub.created_now
                and sub.conversation_id != supervisor_conversation
                and first.conversation_used == sub.conversation_id
                and (after or 0) >= 2
            ),
            "Supervisor created a distinct Research conversation and the Research turn was stored in it",
            {"sub_conversation": sub.model_dump() if sub else None, "items_after_turn": after},
        )
        mapping = first.mapping if first else None
        self.check(
            "Mapping owned and stored by the Supervisor",
            bool(
                mapping
                and mapping.created_now
                and mapping.owner == "supervisor-application"
                and mapping.storage == "foundry-state-store"
                and mapping.store == MAP_STORE
                and mapping.user_isolated
            ),
            "Supervisor application wrote the mapping to its user-isolated Foundry state store",
            mapping.model_dump() if mapping else None,
        )
        exchange, inspected = self.inspect("AC1 inspect: mapping re-read (no model call)", supervisor_conversation)
        isub = inspected.sub_conversation if inspected else None
        listed = inspected.listing_before if inspected else None
        self.check(
            "Mapping re-read from the state store",
            bool(
                inspected
                and inspected.status == "inspected"
                and inspected.mapping
                and inspected.mapping.found
                and not inspected.mapping.created_now
                and isub
                and sub
                and isub.conversation_id == sub.conversation_id
                and listed
                and (listed.items or 0) >= 2
            ),
            "A later Supervisor request finds the same sub-agent conversation and can list its history",
            self.report_observed(exchange, inspected),
        )
        metadata = isub.metadata if isub else None
        expected_metadata = {
            "parent_conversation_digest": conversation_digest(supervisor_conversation),
            "correlation_id": str(correlation),
        }
        self.check(
            "Parent link stored on the sub-agent conversation",
            bool(metadata and all(metadata.get(k) == v for k, v in expected_metadata.items())),
            "Foundry returns the Supervisor-written parent digest and correlation ID as conversation metadata",
            {"metadata_linked": isub.metadata_linked if isub else None, "metadata": metadata},
        )
        self.obs["sub"] = sub.conversation_id if sub else None
        self.report.heading("Who owns and stores what")
        self.report.table(["Item", "Created by", "Stored in", "Bound to", "Owner"], ownership_rows(first))

    def guard_end(self, before: Mapping[str, AgentSnapshot]) -> None:
        super().guard_end(before)
        if self.prepared and self.statuses.get("Agents reused - after") == "PASS":
            path = self.args.output_dir / BASELINE_FILE
            toolbox = before[RESEARCH].toolbox_endpoint or ""
            write_baseline(path, self.args.environment, self.run.run_id, before, toolbox)
            self.report.paragraph(f"Reuse baseline for Steps 2-5 written to `{path.name}`.")

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        if self.statuses.get("Story 3 code deployed") == "FAIL":
            return "Not met"
        core = [self.statuses.get(name) for name in STEP1_CORE]
        if all(status == "PASS" for status in core):
            if self.statuses.get("Parent link stored on the sub-agent conversation") == "PASS":
                return "Met"
            return "Met, with no Foundry-stored parent link (the state-store mapping is the only link)"
        return "Partially met" if "PASS" in core else "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        separate = self.statuses.get("Separate sub-agent conversation")
        results = [
            f"Separate Research conversation created and used by the Supervisor: {status_icon(separate)}.",
            f"Mapping written and re-read from the Supervisor's state store: "
            f"{status_icon(self.statuses.get('Mapping re-read from the state store'))}.",
            f"Agents {'Supervisor was updated by azd deploy' if self.deployed else 'were reused unchanged'}.",
        ]
        link = self.statuses.get("Parent link stored on the sub-agent conversation")
        findings = [
            Finding(
                "Each agent keeps its own Foundry conversation; the Supervisor conversation and the Research "
                "conversation are distinct platform conversations",
                "The client's Supervisor conversation and the Supervisor-created Research conversation had different "
                "IDs, and each stored its own turns"
                if separate == "PASS"
                else "See 'Separate sub-agent conversation' in the detailed evidence",
                "Tested",
                FOUNDRY,
                ("hosted_conversations", "history_hydration"),
                outcome=self.status_outcome(
                    "Supervisor turn in the Supervisor conversation", "Separate sub-agent conversation"
                ),
            ),
            Finding(
                "Who owns the Supervisor -> sub-agent mapping: the Supervisor application (custom code)",
                "Foundry has no native parent/child conversation relationship. The Supervisor application creates the "
                "sub-agent conversation, decides when to reuse it, and is the only component that knows the link",
                "Tested",
                CUSTOM,
                ("conversation_session",),
                outcome=self.status_outcome("Mapping owned and stored by the Supervisor"),
            ),
            Finding(
                "Where the mapping is stored: the Supervisor's Foundry state store (preview), in the calling user's "
                "partition",
                "The mapping was written and re-read under a digest of the Supervisor conversation ID, in the "
                f"user-isolated store `{MAP_STORE}`"
                if self.statuses.get("Mapping re-read from the state store") == "PASS"
                else "See 'Mapping re-read from the state store' in the detailed evidence",
                "Tested",
                f"{FOUNDRY} (state store, user partition); {CUSTOM} (record schema, keys)",
                ("state_store", "state_store_users"),
                outcome=self.status_outcome(
                    "Mapping owned and stored by the Supervisor", "Mapping re-read from the state store"
                ),
            ),
            Finding(
                "Parent link on the sub-agent conversation through conversation metadata",
                "Foundry stored and returned the Supervisor-written parent digest and correlation ID"
                if link == "PASS"
                else "Foundry did not return the parent link metadata, so the state-store mapping is the only link"
                if link == "FAIL"
                else "Not reached; see the checks below",
                "Tested",
                f"{FOUNDRY} (metadata storage); {CUSTOM} (what is written)",
                outcome=self.status_outcome("Parent link stored on the sub-agent conversation"),
            ),
        ]
        gaps = [
            "Writing the mapping and creating the sub-agent conversation are two operations; a failure between them "
            "leaves an unmapped conversation. Retries and cleanup are not implemented.",
            "The Data Analysis specialist uses the same code path but was not exercised in Story 3.",
        ]
        conclusion = (
            "The Supervisor kept its own conversation, created a separate Research conversation, and stored the "
            "mapping in its user-isolated state store."
            if self.verdict.startswith("Met")
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


def ownership_rows(report: ConversationReport | None) -> list[list[object]]:
    sub = report.sub_conversation if report else None
    mapping = report.mapping if report else None
    identity = sub.created_identity if sub else "agent"
    return [
        [
            "Supervisor conversation",
            "Client (operator's Entra token) on the Supervisor endpoint",
            "Foundry conversations",
            "The operator (authenticated caller)",
            FOUNDRY,
        ],
        [
            f"Sub-agent conversation `{sub.conversation_id if sub else '-'}`",
            f"Supervisor application, `{identity}` identity mode, on the Research endpoint",
            "Foundry conversations",
            "The principal Foundry resolved for the Supervisor's call (its agent identity in `agent` mode)",
            f"{FOUNDRY} (storage); {CUSTOM} (creation)",
        ],
        [
            "Supervisor -> sub-agent mapping",
            "Supervisor application",
            f"Foundry state store `{mapping.store if mapping else MAP_STORE}`, key "
            f"`{mapping.key if mapping else 'conversation-map/<digest>'}`",
            "The calling user's partition (resolved by Foundry from the forwarded call ID)",
            f"{CUSTOM} on {FOUNDRY} storage",
        ],
        [
            "Parent link metadata on the sub-agent conversation",
            "Supervisor application",
            "Foundry conversation metadata",
            "Same as the sub-agent conversation",
            f"{CUSTOM} on {FOUNDRY} storage",
        ],
        [
            "History hydration on each turn",
            "SDK adapter (`context.get_history()`) in each agent",
            "-",
            "The conversation ID of the request",
            SDK,
        ],
    ]


AC2 = (
    "Send a follow-up request to the same sub-agent conversation and verify that its prior history is "
    "independently available."
)
STEP2_CHECKS = (
    "First turn created the sub-agent conversation",
    "Follow-up reused the same sub-agent conversation",
    "Prior history stored in the sub-agent conversation",
    "Sub-agent recalled its own history",
    "Sub-agent history not copied into the Supervisor conversation",
    "Another Supervisor conversation has no mapping",
)


class Step2(Story3Step):
    """Two Supervisor turns: the second reaches the same Research conversation, which still holds the first."""

    number = 2
    spec = StepSpec(3, number, "follow-up-history")
    title = "Story 3 / Step 2 - Follow-up to the same sub-agent conversation"
    task_label = f"{TASK_PREFIX} - 2 Verify sub-agent follow-up history"
    ac = AC2
    scope_note = (
        "Reuses the Step 1 baseline read-only. The Supervisor forwards only the new message on each turn; the "
        "follow-up never contains the code word, so a correct answer can only come from Research's own conversation."
    )

    def body(self) -> None:
        conversation = self.new_conversation(SUPERVISOR)
        if not self.check(
            "Supervisor conversation created by the client",
            conversation is not None,
            "Client creates a conversation on the Supervisor endpoint",
            {"conversation": conversation, **self.obs},
        ):
            return
        code = marker("S3B")
        self.report.heading("Turns")
        ex1, first = self.send(
            "AC2 R1: Supervisor -> Research (new sub-agent conversation)", conversation, REMEMBER.format(code=code)
        )
        sub1 = first.sub_conversation if first else None
        self.check(
            "First turn created the sub-agent conversation",
            bool(first and first.status == "completed" and sub1 and sub1.created_now),
            "Completed; the Supervisor created and mapped a new Research conversation",
            self.report_observed(ex1, first),
        )
        ex2, second = self.send("AC2 R2: follow-up through the Supervisor", conversation, RECALL, tokens=[code])
        sub2 = second.sub_conversation if second else None
        self.check(
            "Follow-up reused the same sub-agent conversation",
            bool(
                second
                and sub1
                and sub2
                and second.mapping
                and second.mapping.found
                and not sub2.created_now
                and sub2.conversation_id == sub1.conversation_id
            ),
            "The mapping returned the first turn's Research conversation; nothing new was created",
            self.report_observed(ex2, second),
        )
        before = second.listing_before.items if second and second.listing_before else None
        after = second.listing_after.items if second and second.listing_after else None
        self.check(
            "Prior history stored in the sub-agent conversation",
            before is not None and before >= 2 and after is not None and after > before,
            "Before the follow-up the Research conversation already held the first turn; the follow-up added to it",
            {"items_before_follow_up": before, "items_after_follow_up": after},
        )
        self.check(
            "Sub-agent recalled its own history",
            recalled(second),
            "Research answers with the code word although the forwarded follow-up did not contain it",
            {"code_recalled": recalled(second), "answer": (second.sub_answer or "")[:200] if second else None},
        )
        history = second.supervisor_history if second else None
        self.check(
            "Sub-agent history not copied into the Supervisor conversation",
            bool(history and history.hop_inputs == 0 and history.other_assistant == 0),
            "The Supervisor hydrated only the client's inputs and its own replies, not Research's turns",
            history.model_dump() if history else None,
        )
        self.report.heading("Isolation from other Supervisor conversations")
        other = self.new_conversation(SUPERVISOR)
        ex3, inspected = self.inspect("AC2 control: inspect another Supervisor conversation (no model call)", other)
        self.check(
            "Another Supervisor conversation has no mapping",
            bool(inspected and inspected.status == "inspected" and inspected.mapping and not inspected.mapping.found),
            "A new Supervisor conversation has no sub-agent mapping, so Research history does not leak across",
            self.report_observed(ex3, inspected),
        )
        self.report.table(
            ["Turn", "Sub-agent conversation", "Created now", "Items before -> after", "Code word recalled"],
            [
                [
                    "R1 (remember)",
                    sub1.conversation_id if sub1 else "-",
                    sub1.created_now if sub1 else "-",
                    f"{listing_outcome(first.listing_before) if first else '-'} -> "
                    f"{listing_outcome(first.listing_after) if first else '-'}",
                    "-",
                ],
                [
                    "R2 (follow-up)",
                    sub2.conversation_id if sub2 else "-",
                    sub2.created_now if sub2 else "-",
                    f"{before} -> {after}",
                    recalled(second),
                ],
            ],
        )

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        if all(self.statuses.get(name) == "PASS" for name in STEP2_CHECKS):
            return "Met"
        return "Partially met" if self.statuses.get("Sub-agent recalled its own history") == "PASS" else "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        reused = self.statuses.get("Follow-up reused the same sub-agent conversation")
        recall = self.statuses.get("Sub-agent recalled its own history")
        results = [
            f"Follow-up reached the same Research conversation: {status_icon(reused)}.",
            f"Research recalled the first turn from its own history: {status_icon(recall)}.",
        ]
        findings = [
            Finding(
                "A follow-up sent through the Supervisor reaches the same sub-agent conversation",
                "The Supervisor looked up its mapping and reused the first turn's Research conversation"
                if reused == "PASS"
                else "See 'Follow-up reused the same sub-agent conversation' below",
                "Tested",
                CUSTOM,
                outcome=self.status_outcome("Follow-up reused the same sub-agent conversation"),
            ),
            Finding(
                "The sub-agent's prior history is available on its own, from its conversation",
                "Research answered with the first turn's code word although only the new message was forwarded"
                if recall == "PASS"
                else "See 'Sub-agent recalled its own history' below",
                "Tested",
                f"{FOUNDRY} (conversation storage); {SDK} (history hydration)",
                ("history_hydration", "hosted_conversations"),
                outcome=self.status_outcome(
                    "Prior history stored in the sub-agent conversation", "Sub-agent recalled its own history"
                ),
            ),
            Finding(
                "The sub-agent's history stays out of the Supervisor conversation, and other Supervisor "
                "conversations have no mapping to it",
                "Only the client's inputs and the Supervisor's replies were in the Supervisor conversation, and a new "
                "Supervisor conversation found no mapping"
                if self.statuses.get("Another Supervisor conversation has no mapping") == "PASS"
                and self.statuses.get("Sub-agent history not copied into the Supervisor conversation") == "PASS"
                else "See the isolation checks below",
                "Tested",
                f"{FOUNDRY} (separate conversations); {CUSTOM} (mapping per Supervisor conversation)",
                outcome=self.status_outcome(
                    "Sub-agent history not copied into the Supervisor conversation",
                    "Another Supervisor conversation has no mapping",
                ),
            ),
        ]
        gaps = [
            "Recall is model-based; the item counts are the deterministic evidence that the history was stored.",
            "Recovery of the mapping after a Supervisor crash mid-turn is not tested (see #7).",
        ]
        conclusion = (
            "The follow-up reached the same Research conversation, which still held its earlier turn."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


AC3 = (
    "Document conversation-ID ownership, authenticated user/principal binding, required permissions, and access "
    "enforcement across supervisor/sub-agent boundaries. Exercise representative allowed/denied conversation access "
    "and distinguish client-to-agent from agent-to-agent invocation; identify custom correlation required."
)


@dataclass
class AccessCase:
    name: str
    invocation: str
    principal: str
    conversation: str
    expected: str
    observed: str
    enforced_by: str
    status: Status


class Step3(Story3Step):
    """Ownership, principal binding, permissions, and allowed/denied conversation access at each boundary."""

    number = 3
    spec = StepSpec(3, number, "ownership-and-access")
    title = "Story 3 / Step 3 - Conversation ownership, identity binding, and access"
    task_label = f"{TASK_PREFIX} - 3 Assess conversation ownership and access"
    ac = AC3
    scope_note = (
        "Reuses the Step 1 baseline and reads role assignments only; grants nothing. One paid Supervisor -> "
        "Research turn creates the sub-agent conversation; every access case after it makes no model call."
    )

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.cases: list[AccessCase] = []

    def roles(self, principal: str | None) -> str:
        if not principal:
            return "unknown principal"
        try:
            return ", ".join(role_names(self.context, principal)) or "none at this project"
        except (CliError, ValueError, KeyError) as error:
            return f"unreadable ({sanitize_text(str(error))[:80]})"

    def principal_of(self, agent: str) -> str | None:
        identity = self.records[agent].get("instance_identity")
        value = identity.get("principal_id") if isinstance(identity, dict) else None
        return str(value) if value else None

    def case(self, case: AccessCase) -> None:
        self.cases.append(case)
        self.observe(
            case.name, case.status, case.expected, {"observed": case.observed, "enforced_by": case.enforced_by}
        )

    def body(self) -> None:
        claims = token_claims(self.token())
        candidates = identity_candidates(claims, self.records[SUPERVISOR])
        oid = claims.get("oid")
        permissions: list[list[object]] = [
            [
                "Operator (client)",
                self.roles(oid if isinstance(oid, str) else None),
                "Invoke agents; own its conversations",
            ],
            [
                "Supervisor agent identity",
                self.roles(self.principal_of(SUPERVISOR)),
                "Invoke Research; create/list its own sub-agent conversations; state-store items",
            ],
            ["Research agent identity", self.roles(self.principal_of(RESEARCH)), "Model inference only in this step"],
        ]
        conversation = self.new_conversation(SUPERVISOR)
        if not self.check(
            "Supervisor conversation created by the client",
            conversation is not None,
            "Client creates a conversation on the Supervisor endpoint",
            {"conversation": conversation, **self.obs},
        ):
            return
        correlation = uuid4()
        exchange, setup = self.send(
            "AC3 setup: client -> Supervisor -> Research (separate)",
            conversation,
            "Reply with exactly: READY",
            correlation=correlation,
        )
        sub = setup.sub_conversation if setup else None
        if not self.check(
            "Sub-agent conversation created by the Supervisor",
            bool(setup and setup.status == "completed" and sub),
            "Completed; the Supervisor created and mapped a Research conversation",
            self.report_observed(exchange, setup),
        ):
            return
        assert setup is not None and sub is not None and conversation is not None
        child = sub.conversation_id
        self.report.heading("Principal binding and correlation")
        hop = setup.supervisor
        user = classify_user(hop.user_fingerprint, str(correlation), candidates)
        self.check(
            "Original user bound at the Supervisor",
            hop.user_id_present and hop.call_id_present and user == "operator (original user)",
            "The Supervisor sees the operator as the platform user, with a call context to forward",
            {"supervisor_user": user, "call_id_present": hop.call_id_present},
        )
        self.check(
            "Custom correlation recorded with the mapping",
            hop.correlation_header_matches and sub.created_correlation_id == correlation,
            "The x-client-correlation-id header reached the Supervisor and the mapping recorded it",
            {
                "header_matched": hop.correlation_header_matches,
                "mapping_correlation_matches": sub.created_correlation_id == correlation,
            },
        )
        self.report.heading("Allowed and denied conversation access")
        own = self.list_conversation(SUPERVISOR, conversation)
        self.case(
            AccessCase(
                "Owner lists its Supervisor conversation",
                "client-to-agent",
                "Operator",
                "Supervisor conversation (operator)",
                "allowed",
                own.outcome(),
                "Foundry (conversation ownership)",
                "PASS" if own.allowed else "FAIL",
            )
        )
        cross = self.list_conversation(RESEARCH, conversation)
        self.case(
            AccessCase(
                "Owner lists its Supervisor conversation through the Research endpoint",
                "client-to-agent",
                "Operator",
                "Supervisor conversation (operator)",
                "recorded: is a conversation scoped to the endpoint that created it?",
                cross.outcome(),
                "Foundry",
                "PASS" if cross.allowed or status_word(cross.http_status or 0) == "denied" else "FAIL",
            )
        )
        foreign = self.list_conversation(RESEARCH, child)
        self.case(
            AccessCase(
                "Client lists the Supervisor-created sub-agent conversation",
                "client-to-agent",
                "Operator",
                "Sub-agent conversation (Supervisor identity)",
                "denied (one caller can't read another caller's conversations)",
                foreign.outcome(),
                "Foundry (conversation ownership)",
                "PASS" if foreign.denied else "FAIL",
            )
        )
        for mode in IDENTITIES:
            ex, inspected = self.inspect(
                f"AC3 Supervisor lists its sub-agent conversation ({mode})", conversation, identity=mode
            )
            listing = inspected.listing_before if inspected else None
            status: Status
            if mode == "agent":
                expected = "allowed"
                status = "PASS" if listing and listing.items is not None else "FAIL"
            else:
                expected = "recorded: which principal Foundry binds when the call context is forwarded"
                status = "PASS" if listing_definitive(listing) else "FAIL"
            self.case(
                AccessCase(
                    f"Supervisor lists its sub-agent conversation ({mode} identity)",
                    "agent-to-agent",
                    "Supervisor agent identity" + (" + forwarded call ID" if mode == "call-context" else ""),
                    "Sub-agent conversation (Supervisor identity)",
                    expected,
                    listing_outcome(listing) if inspected else f"no report ({ex.status})",
                    "Foundry (conversation ownership)",
                    status,
                )
            )
        forged = self.new_conversation(RESEARCH)
        if forged is None:
            claimed: ConversationReport | None = None
            forged_observed = f"not sent: client could not create a Research conversation ({self.obs})"
        else:
            ex, claimed = self.send(
                "AC3 denied: client claims a Research conversation the mapping does not hold",
                conversation,
                "Reply with exactly: SHOULD-NOT-RUN",
                claim=forged,
            )
            forged_observed = report_outcome(claimed) if claimed else ex.status
        self.case(
            AccessCase(
                "Client supplies an unmapped sub-agent conversation ID",
                "client-to-agent, then no agent-to-agent call",
                "Operator",
                "Client-created Research conversation",
                "denied by the Supervisor mapping, no outbound call",
                forged_observed,
                "Supervisor application (mapping check)",
                "PASS" if claimed and claimed.status == "denied" and claimed.response_id is None else "FAIL",
            )
        )
        other = self.new_conversation(SUPERVISOR)
        ex, reused = self.inspect(
            "AC3 denied: another Supervisor conversation claims the sub-agent conversation", other, claim=child
        )
        self.case(
            AccessCase(
                "Another Supervisor conversation claims the sub-agent conversation",
                "client-to-agent",
                "Operator (same user)",
                "Sub-agent conversation mapped to a different Supervisor conversation",
                "denied by the Supervisor mapping",
                report_outcome(reused) if reused else ex.status,
                "Supervisor application (mapping per Supervisor conversation)",
                "PASS" if reused and reused.status == "denied" else "FAIL",
            )
        )
        bare = self.hosted_call(
            "AC3 denied: POC_CONVERSE without a Supervisor conversation",
            SUPERVISOR,
            ConverseCommand(operation="inspect", correlation_id=uuid4()).wire(),
        )
        self.case(
            AccessCase(
                "Request without a Supervisor conversation",
                "client-to-agent",
                "Operator",
                "none",
                "rejected before any state or outbound call",
                f"{bare.status}: {(bare.error or '')[:120]}",
                "Supervisor application",
                "PASS" if capability_from(bare) else "FAIL",
            )
        )
        self.report.table(
            [
                "Case",
                "Invocation",
                "Authenticated principal",
                "Conversation (owner)",
                "Expected",
                "Observed",
                "Enforced by",
                "Status",
            ],
            [
                [
                    c.name,
                    c.invocation,
                    c.principal,
                    c.conversation,
                    c.expected,
                    c.observed,
                    c.enforced_by,
                    status_icon(c.status),
                ]
                for c in self.cases
            ],
        )
        self.report.heading("Required permissions (read-only)")
        self.report.table(["Principal", "Roles at the project", "Needed for"], permissions)
        self.report.heading("Ownership and binding")
        self.report.table(["Conversation or record", "Owning principal", "How it is bound"], BINDING_ROWS)
        self.report.heading("Custom correlation required")
        self.report.table(["Link", "Native in Foundry?", "Implemented by"], CORRELATION_ROWS)

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        if self.statuses.get("Sub-agent conversation created by the Supervisor") != "PASS":
            return "Not met"
        statuses = [case.status for case in self.cases] + [
            self.statuses.get("Original user bound at the Supervisor"),
            self.statuses.get("Custom correlation recorded with the mapping"),
        ]
        if all(status == "PASS" for status in statuses):
            return "Met"
        return "Partially met" if "PASS" in statuses else "Not met"

    def case_observed(self, name: str) -> str:
        return next((case.observed for case in self.cases if case.name == name), "not run")

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        foreign = "Client lists the Supervisor-created sub-agent conversation"
        cross = "Owner lists its Supervisor conversation through the Research endpoint"
        forwarded = "Supervisor lists its sub-agent conversation (call-context identity)"
        owner = "Supervisor lists its sub-agent conversation (agent identity)"
        results = [
            f"{sum(case.status == 'PASS' for case in self.cases)} of {len(self.cases)} access cases matched "
            "expectations.",
            f"Client reading the Supervisor-created sub-agent conversation: {self.case_observed(foreign)}.",
            f"Supervisor listing it with a forwarded call context: {self.case_observed(forwarded)}.",
        ]
        findings = [
            Finding(
                "Conversation ownership follows the principal that created it: the client owns the Supervisor "
                "conversation, and the Supervisor's identity owns the sub-agent conversation",
                f"Client -> sub-agent conversation: {self.case_observed(foreign)}; Supervisor (agent identity) -> "
                f"same conversation: {self.case_observed(owner)}",
                "Tested",
                FOUNDRY,
                ("conversation_privacy",),
                outcome=self.status_outcome(foreign, owner),
            ),
            Finding(
                "Conversation scope across agent endpoints of the same project",
                f"The owner listing its Supervisor conversation through Research: {self.case_observed(cross)}",
                "Observed",
                FOUNDRY,
                outcome=self.status_outcome(cross),
            ),
            Finding(
                "Authenticated user binding: the Supervisor sees the original user; the sub-agent conversation is "
                "bound to the Supervisor's identity, and the mapping to the user's state-store partition",
                "The Supervisor's salted user fingerprint matched the operator; the sub-agent conversation is not "
                "bound to the end user"
                if self.statuses.get("Original user bound at the Supervisor") == "PASS"
                else "See 'Original user bound at the Supervisor' below",
                "Tested",
                f"{FOUNDRY} (caller resolution, state-store partition); {CUSTOM} (what the mapping records)",
                ("hosted_contract", "state_store_users", "agent_identity"),
                outcome=self.status_outcome("Original user bound at the Supervisor"),
            ),
            Finding(
                "Application enforcement: the Supervisor only uses the sub-agent conversation mapped to the current "
                "Supervisor conversation",
                "Unmapped and cross-conversation claims were denied before any outbound call",
                "Tested",
                CUSTOM,
                outcome=self.status_outcome(
                    "Client supplies an unmapped sub-agent conversation ID",
                    "Another Supervisor conversation claims the sub-agent conversation",
                ),
            ),
            Finding(
                "Custom correlation is required to link conversations across agents",
                "Foundry links neither conversations nor responses across agents; the Supervisor's mapping record, "
                "the `x-client-correlation-id` header, and optional conversation metadata are the only links",
                "Tested",
                CUSTOM,
                ("client_headers",),
                outcome=self.status_outcome("Custom correlation recorded with the mapping"),
            ),
            Finding(
                "Native parent/child conversation relationships between agents",
                "No platform field or API links a sub-agent conversation to a Supervisor conversation",
                "Documented",
                NOT_SUPPORTED,
                ("conversation_session",),
                outcome="NOT SUPPORTED",
            ),
        ]
        gaps = [
            "A second Entra user was not used here; cross-user conversation and response denial on Research was "
            "established by the native-user isolation test (ADR-0001, 22/22 passed).",
            "Delegated-user (`x-ms-user-identity`) conversation ownership was not tested; Story 4 recorded that the "
            "header neither took effect nor was rejected on the hosted-agent path.",
            "Required roles are read from assignments, not checked per call.",
        ]
        conclusion = (
            "Conversations are owned by the principal that created them, the Supervisor enforces its own mapping, "
            "and the links between conversations are custom."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


BINDING_ROWS: list[list[object]] = [
    [
        "Supervisor conversation",
        "Operator (client's Entra principal)",
        "Foundry, at creation on the Supervisor endpoint",
    ],
    [
        "Sub-agent conversation",
        "Principal Foundry resolves for the Supervisor's call",
        "Foundry, at creation by the Supervisor",
    ],
    [
        "Mapping record",
        "Supervisor application",
        "Key = digest of the Supervisor conversation; partition = calling user",
    ],
    ["Correlation ID", "Client", "`x-client-correlation-id` header, mapping record, conversation metadata"],
]
CORRELATION_ROWS: list[list[object]] = [
    ["Supervisor conversation -> sub-agent conversation", "No", f"Supervisor mapping in the state store ({CUSTOM})"],
    [
        "Sub-agent conversation -> Supervisor conversation",
        "No (metadata is free-form)",
        f"Conversation metadata written by the Supervisor ({CUSTOM})",
    ],
    ["Client request -> every hop", "Only `x-client-*` header forwarding", f"`x-client-correlation-id` ({CUSTOM})"],
    ["Original user -> sub-agent", "Not observed on the agent-to-agent path (Story 4)", "Not implemented"],
]
AC4 = (
    "Evaluate Agent A invoking Agent B through the Responses API with the same conversation ID. Record whether B can "
    "read and append history and whether a later A invocation sees B's messages."
)
IDENTITIES: tuple[ConverseIdentity, ...] = ("agent", "call-context")


@dataclass
class SharedResult:
    outcome: str = "not run"
    definitive: bool = False
    read: bool | None = None
    input_appended: bool | None = None
    output_appended: bool | None = None
    detail: dict[str, object] = field(default_factory=dict)


class Step4(Story3Step):
    """The Supervisor passes its own conversation ID to Research, once per identity mode."""

    number = 4
    spec = StepSpec(3, number, "shared-conversation-id")
    title = "Story 3 / Step 4 - Agent A invokes Agent B with the same conversation ID"
    task_label = f"{TASK_PREFIX} - 4 Evaluate shared conversation ID"
    ac = AC4
    scope_note = (
        "Reuses the Step 1 baseline read-only. Agent A is the Supervisor and Agent B is Research. The client seeds A's "
        "conversation with a code word; A then calls B with A's own conversation ID (`agent` and `call-context` "
        "identity), delivering only a placeholder so that any B content in A's conversation was appended by B's turn."
    )

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.shared: dict[str, SharedResult] = {}
        self.sees: bool | None = None
        self.direct: str = "not run"

    def body(self) -> None:
        conversation = self.new_conversation(SUPERVISOR)
        if not self.check(
            "Supervisor conversation created by the client",
            conversation is not None,
            "Client creates a conversation on the Supervisor endpoint",
            {"conversation": conversation, **self.obs},
        ):
            return
        assert conversation is not None
        code = marker("S3C")
        seed = self.hosted_call(
            "AC4 R1: client -> Supervisor plain turn (seed A's history)",
            SUPERVISOR,
            REMEMBER.format(code=code),
            conversation=conversation,
        )
        if not self.check(
            "Supervisor conversation seeded",
            seed.completed,
            "A completed plain turn in A's conversation that contains the code word",
            seed.observed(),
        ):
            return
        baseline = self.list_conversation(SUPERVISOR, conversation)
        previous = history_stats(baseline.items or [], [code])
        start = previous
        self.report.heading("B invoked with A's conversation ID")
        rows: list[list[object]] = []
        for identity in IDENTITIES:
            ex, report = self.send(
                f"AC4 shared ({identity}): Supervisor -> Research with the Supervisor conversation ID",
                conversation,
                RECALL_SHARED,
                pattern="shared",
                deliver="placeholder",
                identity=identity,
                tokens=[code],
            )
            listing = self.list_conversation(SUPERVISOR, conversation)
            current = history_stats(listing.items or [], [code])
            result = SharedResult(
                outcome=report_outcome(report) if report else f"{ex.status}: {(ex.error or '')[:160]}",
                definitive=definitive(report),
                read=recalled(report) if report and report.status == "completed" else None,
                input_appended=current.hop_inputs > previous.hop_inputs if listing.allowed else None,
                output_appended=current.other_assistant > previous.other_assistant if listing.allowed else None,
                detail=self.report_observed(ex, report),
            )
            self.shared[identity] = result
            self.check(
                f"B invoked with A's conversation ID ({identity} identity)",
                result.definitive,
                "An explicit outcome: completed, or a 4xx denial from Foundry",
                {
                    **result.detail,
                    "b_read": result.read,
                    "input_appended": result.input_appended,
                    "output_appended": result.output_appended,
                },
            )
            rows.append([identity, result.outcome, result.read, result.input_appended, result.output_appended])
            previous = current
        self.report.table(
            ["Identity", "B call outcome", "B read A's history", "B's input appended to A", "B's output appended to A"],
            rows,
        )
        self.report.heading("Later Agent A invocation")
        ex, later = self.inspect(
            "AC4 later Supervisor turn: hydrated history (no model call)", conversation, pattern="shared", tokens=[code]
        )
        history = later.supervisor_history if later else None
        if history is not None:
            self.sees = history.hop_inputs > start.hop_inputs or history.other_assistant > start.other_assistant
        self.check(
            "Later Supervisor invocation observed its hydrated history",
            later is not None and later.status == "inspected",
            "The Supervisor reports what it hydrated from its own conversation (counts only)",
            {
                "sees_b_messages": self.sees,
                "history": history.model_dump() if history else None,
                "before_b": start.model_dump(),
            },
        )
        self.report.heading("Control: client -> Research with A's conversation ID (same principal)")
        direct = self.hosted_call(
            "AC4 control: client -> Research with the Supervisor conversation ID",
            RESEARCH,
            RECALL_SHARED,
            conversation=conversation,
        )
        self.direct = (
            f"completed; code word {'recalled' if code in direct.text else 'not recalled'}"
            if direct.completed
            else f"{direct.status}{f' (HTTP {direct.http_status}, {direct.error_code})' if direct.http_status else ''}"
        )
        self.check(
            "Control: owner uses A's conversation on B's endpoint",
            direct.completed or (direct.http_status is not None and 400 <= direct.http_status < 500),
            "Explicit outcome recorded; separates cross-endpoint scope from cross-principal ownership",
            {**direct.observed(), "summary": self.direct},
        )

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        answered = [self.shared[identity].definitive for identity in IDENTITIES if identity in self.shared]
        inspected = self.statuses.get("Later Supervisor invocation observed its hydrated history") == "PASS"
        if answered and all(answered) and len(answered) == len(IDENTITIES) and inspected:
            return "Met"
        return "Partially met" if any(answered) else "Not met"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        def text(identity: str) -> str:
            result = self.shared.get(identity)
            if result is None:
                return "not run"
            return (
                f"{result.outcome}; B read A's history: {yes_no(result.read)}; B's input appended: "
                f"{yes_no(result.input_appended)}; B's output appended: {yes_no(result.output_appended)}"
            )

        results = [f"`{identity}` identity: {text(identity)}." for identity in IDENTITIES]
        results.append(f"Later Supervisor invocation sees B's messages: {yes_no(self.sees)}.")
        results.append(f"Control (client -> Research with A's conversation ID): {self.direct}.")
        any_read = any(result.read for result in self.shared.values())
        any_append = any(result.input_appended or result.output_appended for result in self.shared.values())
        answered = [result.definitive for result in self.shared.values()]
        findings = [
            Finding(
                "Agent B invoked by Agent A with A's conversation ID",
                "; ".join(f"{identity}: {self.shared[identity].outcome}" for identity in self.shared) or "Not run",
                "Tested",
                FOUNDRY,
                ("conversation_privacy", "history_hydration"),
                outcome=self.observed_outcome(*answered) if answered else self.observed_outcome(None),
            ),
            Finding(
                "B reads and appends A's history",
                f"Read: {yes_no(any_read if answered else None)}; appended: {yes_no(any_append if answered else None)}",
                "Tested",
                FOUNDRY,
                ("history_hydration",),
                outcome=(
                    "LIMITATION"
                    if any_read or any_append
                    else (
                        "NOT SUPPORTED"
                        if answered and all(answered)
                        else (self.observed_outcome(*answered) if answered else self.observed_outcome(None))
                    )
                ),
            ),
            Finding(
                "A later Agent A invocation sees B's messages",
                f"{yes_no(self.sees)}: counted from the Supervisor's own hydrated history, not a model answer",
                "Tested",
                SDK,
                ("history_hydration",),
                outcome=self.status_outcome("Later Supervisor invocation observed its hydrated history"),
            ),
            Finding(
                "Shared conversation IDs as a supported multi-agent pattern",
                "No Microsoft documentation describes one conversation shared by several hosted agents; the separate-"
                "conversation pattern (Steps 1-2) remains the reference",
                "Documented",
                FOUNDRY,
                outcome="DOCUMENTED",
            ),
        ]
        gaps = [
            "Concurrent turns on one shared conversation (locking/steering) are covered by #7, not here.",
            "Delegated-user identity was not tested for the shared pattern.",
        ]
        conclusion = (
            "Every shared-conversation call produced an explicit outcome, and A's later hydrated history was counted."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


def yes_no(value: bool | None) -> str:
    return "not observed" if value is None else ("yes" if value else "no")


AC5 = (
    "Compare shared and separate conversation IDs. Record whether the supervisor receives the sub-agent's result, a "
    "placeholder, or full history."
)
VARIANTS: tuple[tuple[Pattern, Delivery, ConverseIdentity], ...] = (
    ("separate", "result", "agent"),
    ("separate", "placeholder", "agent"),
    ("shared", "placeholder", "agent"),
    ("shared", "placeholder", "call-context"),
)


@dataclass
class Variant:
    pattern: Pattern
    deliver: Delivery
    identity: ConverseIdentity
    report: ConversationReport | None = None
    later: ConversationReport | None = None
    received: str = "not run"

    @property
    def name(self) -> str:
        return f"{self.pattern} / {self.deliver} / {self.identity}"


class Step5(Story3Step):
    """Each pattern in a fresh Supervisor conversation; classify what the Supervisor ends up with."""

    number = 5
    spec = StepSpec(3, number, "shared-vs-separate")
    title = "Story 3 / Step 5 - Shared versus separate conversation IDs"
    task_label = f"{TASK_PREFIX} - 5 Compare shared and separate conversations"
    ac = AC5
    scope_note = (
        "Reuses the Step 1 baseline read-only. Four variants, each in a new Supervisor conversation: separate with "
        "result delivery, separate with placeholder delivery, and shared with placeholder delivery in `agent` and "
        "`call-context` identity. A model-free inspect after each turn counts what the Supervisor hydrates."
    )

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.variants: list[Variant] = []

    def body(self) -> None:
        rows: list[list[object]] = []
        for pattern, deliver, identity in VARIANTS:
            variant = Variant(pattern, deliver, identity)
            self.variants.append(variant)
            conversation = self.new_conversation(SUPERVISOR)
            token = marker("S3D")
            _, variant.report = self.send(
                f"AC5 {variant.name}",
                conversation,
                f"Reply with exactly: {token}",
                pattern=pattern,
                deliver=deliver,
                identity=identity,
                tokens=[token],
            )
            _, variant.later = self.inspect(
                f"AC5 {variant.name}: later Supervisor inspect (no model call)",
                conversation,
                pattern=pattern,
                identity=identity,
                tokens=[token],
            )
            history = variant.later.supervisor_history if variant.later else None
            variant.received = classify_received(variant.report, history)
            if pattern == "separate":
                ok = separate_variant_ok(variant.report, variant.received, deliver)
                expected = (
                    f"Supervisor ends up with the sub-agent's {deliver}; sub-agent history stays in its own "
                    "conversation"
                )
            else:
                ok = definitive(variant.report) and variant.later is not None
                expected = "Explicit outcome recorded and classified"
            self.check(
                f"Variant {variant.name}",
                ok,
                expected,
                {
                    "received": variant.received,
                    "outcome": report_outcome(variant.report),
                    "history": history.model_dump() if history else None,
                },
            )
            rows.append(comparison_row(variant))
        self.report.heading("Comparison")
        self.report.table(
            [
                "Variant (pattern / delivery / identity)",
                "Sub-agent call",
                "Supervisor received in the call",
                "Persisted in the Supervisor conversation",
                "Sub-agent history lives in",
                "Supervisor ends up with",
            ],
            rows,
        )
        self.report.paragraph(
            "`hop inputs` are messages starting with the Supervisor's forwarding tag "
            f"`{HOP_TAG}`, so they can only be the sub-agent's input. `sub-agent outputs` are assistant messages "
            "that are not Supervisor `poc-conversation-v1` replies."
        )

    def compute_verdict(self) -> str:
        early = self.blocked_or_failed()
        if early:
            return early
        statuses = [self.statuses.get(f"Variant {variant.name}") for variant in self.variants]
        if statuses and all(status == "PASS" for status in statuses) and len(statuses) == len(VARIANTS):
            return "Met"
        return "Partially met" if "PASS" in statuses else "Not met"

    def received(self, pattern: Pattern) -> str:
        values = sorted({variant.received for variant in self.variants if variant.pattern == pattern})
        return ", ".join(values) or "not run"

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        results = [f"{variant.name}: {variant.received}." for variant in self.variants] or ["No variant ran."]
        separate = [self.statuses.get(f"Variant {v.name}") == "PASS" for v in self.variants if v.pattern == "separate"]
        shared = [self.statuses.get(f"Variant {v.name}") == "PASS" for v in self.variants if v.pattern == "shared"]
        full = any(variant.received == "full history" for variant in self.variants)
        nothing = bool(shared) and all(
            variant.received.startswith("nothing") for variant in self.variants if variant.pattern == "shared"
        )
        if not shared:
            shared_outcome: Outcome = self.observed_outcome(None)
        elif full:
            shared_outcome = "LIMITATION"
        elif nothing and all(shared):
            shared_outcome = "NOT SUPPORTED"
        else:
            shared_outcome = self.observed_outcome(*shared)
        findings = [
            Finding(
                "Separate conversation IDs: the Supervisor receives the sub-agent's result and decides whether to keep "
                "it (result) or only a reference (placeholder)",
                f"Observed: {self.received('separate')}. The sub-agent's turns stayed in its own conversation",
                "Tested",
                f"{CUSTOM} (what is delivered); {FOUNDRY} (separate storage)",
                ("hosted_conversations",),
                outcome=self.observed_outcome(*separate) if separate else self.observed_outcome(None),
            ),
            Finding(
                "Shared conversation ID: what the Supervisor ends up with",
                f"Observed: {self.received('shared')}"
                + (
                    ". B's input and output were written into A's conversation and hydrated on A's next turn"
                    if full
                    else ""
                ),
                "Tested",
                FOUNDRY,
                ("history_hydration", "conversation_privacy"),
                outcome=shared_outcome,
            ),
            Finding(
                "Placeholder versus result is an application choice, not a platform setting",
                "The Supervisor decides what to write into its own reply; Foundry stores whatever the reply contains",
                "Tested",
                CUSTOM,
                outcome=self.observed_outcome(*separate) if separate else self.observed_outcome(None),
            ),
        ]
        gaps = [
            "Streaming and background delivery were not compared; both patterns used synchronous calls.",
            "Data Analysis artifacts (container files) in a shared conversation were not tested.",
        ]
        conclusion = (
            "The comparison recorded, for each pattern, whether the Supervisor ends up with the result, a placeholder, "
            "or the sub-agent's full history."
            if self.verdict == "Met"
            else "See the checks below."
        )
        return conclusion, results, findings, gaps


def comparison_row(variant: Variant) -> list[object]:
    report = variant.report
    later = variant.later.supervisor_history if variant.later else None
    sub_listing = variant.later.listing_before if variant.later else None
    if variant.pattern == "separate":
        lives = f"Sub-agent conversation ({listing_outcome(sub_listing)})"
    elif later is not None and later.hop_inputs and later.other_assistant:
        lives = "The Supervisor conversation (appended by B's turn)"
    else:
        lives = "Not in the Supervisor conversation"
    present = report.tokens_in_sub_answer[0] if report and report.tokens_in_sub_answer else None
    received = (
        f"{report.sub_answer_chars} chars; token present: {yes_no(present)}"
        if report and report.status == "completed"
        else "-"
    )
    persisted = (
        f"{later.protocol_replies} Supervisor replies, {later.hop_inputs} hop inputs, "
        f"{later.other_assistant} sub-agent outputs"
        if later
        else "-"
    )
    return [variant.name, report_outcome(report), received, persisted, lives, variant.received]


def step_classes() -> dict[int, type[Story3Step]]:
    return {1: Step1, 2: Step2, 3: Step3, 4: Step4, 5: Step5}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument(
        "--mode",
        choices=["verify", "prepare-if-missing"],
        default="verify",
        help="Step 1 only; Steps 2-5 never deploy or change agents.",
    )
    parser.add_argument("--timeout", type=float, default=300, help="Client timeout in seconds per hosted request.")
    add_output_arguments(parser)
    return parser


def run_step(args: argparse.Namespace, step_class: type[Story3Step]) -> int:
    run = ValidationRun(
        step_class.spec.scenario,
        "hosted",
        args.output_dir,
        no_color=args.no_color,
        verbose=args.verbose,
        step=step_class.spec,
    )
    report = MarkdownReport(run.path / REPORT_FILE, step_class.title)
    step = step_class(args, run, report)
    try:
        step.execute()
    except Blocked as error:
        step.record("Prerequisite", "BLOCKED", "Existing agents and the Story 3 baseline ready", str(error))
    except EXPECTED_ERRORS as error:
        step.record("Unexpected failure", "FAIL", "Step completes", sanitize_text(str(error)))
    step.conclude()
    report.render(run.console)
    index = args.output_dir / "index.md"
    update_index(index, index_entry(index, step_class.spec.label(), step.verdict, run.path), title=INDEX_TITLE)
    return run.finish()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.step != 1 and args.mode != "verify":
        parser.error("--mode applies to Step 1 only; Steps 2-5 never deploy or change agents.")
    args.output_dir = Path(args.output_dir) / STORY_DIR
    return run_step(args, step_classes()[args.step])


if __name__ == "__main__":
    raise SystemExit(main())
