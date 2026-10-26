"""Shared Story 4 harness: reuse guard, hosted request helpers, identity classification, and reporting."""

import argparse
import base64
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from uuid import uuid4

from app.core.redaction import sanitize_text
from app.services.orchestration import CORRELATION_HEADER, DelegateCommand, DelegationReport, fingerprint
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from openai import APIStatusError, OpenAIError
from pydantic import JsonValue, ValidationError

from scripts.common.findings import Finding, render_summary
from scripts.common.reporting import MarkdownReport, Status, ValidationRun
from scripts.deploy.grant_agent_rbac import IMPERSONATION_ACTION
from scripts.deploy.grant_local_rbac import CliError, az, object_value
from scripts.run_scenario.hosted_smoke import SERVICES
from scripts.run_scenario.story1_data_analysis import (
    DATA_SCOPE,
    SNAPSHOT_HEADERS,
    AgentSnapshot,
    Blocked,
    read_environment,
    reuse_differences,
    snapshot_agents,
)
from scripts.run_scenario.trigger_agent import response_summary

STORY_DIR = "story-4"
STORY1_DIR = "story-1"
BASELINE_FILE = "agents-baseline.json"
INDEX_TITLE = "Story 4 evidence index"
ISSUE = "#4"
SUPERVISOR = SERVICES["supervisor"]
RESEARCH = SERVICES["research"]
DATA_ANALYSIS = SERVICES["data_analysis"]
RESEARCH_TOOLBOX = "story4-research-mcp"
RESEARCH_TOOLBOX_ENV = "RESEARCH_TOOLBOX_ENDPOINT"
LEARN_MCP_URL = "https://learn.microsoft.com/api/mcp"
MCP_SERVER_LABEL = "microsoft_learn"
ALLOWED_MCP_TOOL = "microsoft_docs_search"
EXCLUDED_MCP_TOOL = "microsoft_docs_fetch"
FOUNDRY = "Foundry-managed"
SDK = "Agent Framework / SDK-managed"
CUSTOM = "Custom implementation (Dev Team)"
NOT_SUPPORTED = "Not supported"
TASK_PREFIX = "Agents: Story 4"


def new_correlation() -> str:
    return str(uuid4())


def marker(prefix: str) -> str:
    """A run-unique token the model cannot guess; it proves which hop carried a result."""
    return f"{prefix}-{uuid4().hex[:10].upper()}"


@dataclass
class Exchange:
    """One hosted request as observed by the client: outcome, timing, and sanitized error details."""

    label: str
    agent: str
    status: str = "not sent"
    response_id: str | None = None
    text: str = ""
    error: str | None = None
    error_code: str | None = None
    http_status: int | None = None
    seconds: float = 0.0
    statuses_seen: list[str] = field(default_factory=list)
    polls: int = 0

    @property
    def completed(self) -> bool:
        return self.status == "completed" and self.error is None

    def observed(self) -> dict[str, object]:
        data = {key: value for key, value in asdict(self).items() if key not in {"text", "label"}}
        return {key: value for key, value in data.items() if value not in (None, [], 0, "")}


def token_claims(token: str) -> dict[str, JsonValue]:
    """Decode the JWT payload for in-memory comparison only; never validated, logged, or persisted."""
    try:
        payload = token.split(".")[1]
        value: JsonValue = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, UnicodeDecodeError) as _:
        return {}
    return value if isinstance(value, dict) else {}


def identity_candidates(claims: Mapping[str, JsonValue], supervisor: Mapping[str, JsonValue]) -> dict[str, list[str]]:
    """Possible raw forms of the operator and Supervisor identities; only salted digests are compared."""
    oid, tid = claims.get("oid"), claims.get("tid")
    operator: list[str] = []
    if isinstance(oid, str):
        operator.append(oid)
        if isinstance(tid, str):
            operator += [f"{tid}:{oid}", f"{oid}:{tid}", f"{oid}@{tid}", f"{tid}_{oid}"]
    for key in ("upn", "unique_name", "preferred_username"):
        value = claims.get(key)
        if isinstance(value, str):
            operator.append(value)
    identity = supervisor.get("instance_identity")
    if not isinstance(identity, dict):
        latest = object_value(object_value(supervisor.get("versions") or {}).get("latest") or {})
        identity = latest.get("instance_identity")
    managed = (
        [str(v) for k, v in (identity or {}).items() if k in {"principal_id", "client_id"} and v]
        if isinstance(identity, dict)
        else []
    )
    return {"operator (original user)": operator, "Supervisor managed identity": managed}


def classify_user(
    observed: str | None,
    salt: str,
    candidates: Mapping[str, Sequence[str]],
    references: Mapping[str, str | None] | None = None,
) -> str:
    """Name the principal behind a salted fingerprint without ever handling the hop's raw identity."""
    if observed is None:
        return "absent (no user context visible)"
    for label, reference in (references or {}).items():
        if reference is not None and reference == observed:
            return label
    for label, values in candidates.items():
        if any(fingerprint(value, salt) == observed for value in values):
            return label
    return "present, unrecognized principal"


def role_names(context: Mapping[str, JsonValue], principal: str) -> list[str]:
    """Read-only: role names assigned to a principal at the Foundry project, including inherited ones."""
    assignments = az(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal,
            "--scope",
            str(context["AZURE_AI_PROJECT_ID"]),
            "--include-inherited",
            "--fill-principal-name",
            "false",
            "--subscription",
            str(context["AZURE_SUBSCRIPTION_ID"]),
        ]
    )
    if not isinstance(assignments, list):
        raise ValueError("Expected a role-assignment list.")
    return sorted({str(object_value(item).get("roleDefinitionName")) for item in assignments})


def impersonation_granted(context: Mapping[str, JsonValue], principal: str) -> tuple[bool, list[str]]:
    """Read-only: does any assigned role explicitly grant the UserIdentityImpersonation data action?

    Only an exact action grants it: Microsoft documents that ``Microsoft.CognitiveServices/*`` no longer does.
    """
    subscription = str(context["AZURE_SUBSCRIPTION_ID"])
    assignments = az(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal,
            "--scope",
            str(context["AZURE_AI_PROJECT_ID"]),
            "--include-inherited",
            "--fill-principal-name",
            "false",
            "--subscription",
            subscription,
        ]
    )
    if not isinstance(assignments, list):
        raise ValueError("Expected a role-assignment list.")
    granting: list[str] = []
    action = IMPERSONATION_ACTION.casefold()
    for item in assignments:
        role_id = str(object_value(item).get("roleDefinitionId", "")).rsplit("/", 1)[-1]
        definitions = az(["role", "definition", "list", "--name", role_id, "--subscription", subscription])
        if not isinstance(definitions, list) or not definitions:
            continue
        definition = object_value(definitions[0])
        permissions = definition.get("permissions")
        for permission in permissions if isinstance(permissions, list) else []:
            allowed = object_value(permission).get("dataActions") or []
            denied = object_value(permission).get("notDataActions") or []
            if (
                isinstance(allowed, list)
                and isinstance(denied, list)
                and any(str(pattern).casefold() == action for pattern in allowed)
                and not any(fnmatchcase(action, str(pattern).casefold()) for pattern in denied)
            ):
                granting.append(str(definition.get("roleName")))
    return bool(granting), sorted(set(granting))


class Story4Step:
    """One acceptance criterion: shared configuration, reuse guard, hosted helpers, and summary."""

    number = 0
    scenario = ""
    directory = ""
    report_name = ""
    title = ""
    index_label = ""
    task_label = ""
    ac = ""
    scope_note = ""

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        self.args = args
        self.run = run
        self.report = report
        self.statuses: dict[str, Status] = {}
        self.verdict = "Not evaluated"
        self.obs: dict[str, Any] = {}
        self.requests: list[list[object]] = []
        self.records: dict[str, dict[str, JsonValue]] = {}
        self.context: dict[str, JsonValue] = {}
        self.endpoint = ""
        self._project: AIProjectClient | None = None
        self._credential: AzureCliCredential | None = None

    @property
    def project(self) -> AIProjectClient:
        if self._project is None:
            raise RuntimeError("Project client is only available inside execute().")
        return self._project

    @property
    def credential(self) -> AzureCliCredential:
        if self._credential is None:
            raise RuntimeError("Credential is only available inside execute().")
        return self._credential

    def observe(self, step: str, status: Status, expected: str, observed: object) -> None:
        self.statuses[step] = status
        self.run.result(step, status, expected=expected, observed=observed)

    def record(self, step: str, status: Status, expected: str, observed: object) -> None:
        self.observe(step, status, expected, observed)
        self.report.table(["Check", "Status", "Expected", "Observed"], [[step, status, expected, observed]])

    def check(self, step: str, ok: bool, expected: str, observed: object) -> bool:
        self.record(step, "PASS" if ok else "FAIL", expected, observed)
        return ok

    def read(self, name: str) -> dict[str, JsonValue]:
        record = object_value(self.project.agents.get(name).as_dict())
        self.records[name] = record
        return record

    def execute(self) -> None:
        self.report.paragraph(f"**AC{self.number} (verbatim from {ISSUE}):** {self.ac}")
        if self.scope_note:
            self.report.paragraph(self.scope_note)
        self.report.table(
            ["Field", "Value"],
            [
                ["Task", self.task_label],
                ["Run ID", self.run.run_id],
                ["UTC time", datetime.now(UTC).isoformat(timespec="seconds")],
                ["Environment", self.args.environment],
                *self.extra_fields(),
            ],
        )
        self.context = read_environment(self.args.environment, self.report)
        self.endpoint = str(self.context["FOUNDRY_PROJECT_ENDPOINT"]).rstrip("/")
        self.report.heading("Reproduction")
        self.report.paragraph(
            f"VS Code: **Terminal > Run Task... > {self.task_label}**. Equivalent command (synthetic data only):"
        )
        self.report.code(self.command_line(), "powershell")
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=self.endpoint, credential=credential, allow_preview=True) as project,
        ):
            self._credential = credential
            self._project = project
            before = self.guard_start()
            if before is None:
                return
            self.body()
            if self.requests:
                self.report.heading("Requests sent")
                self.report.table(
                    ["#", "Request", "Agent called by the client", "Status", "Response ID", "Seconds"], self.requests
                )
            self.guard_end(before)

    def extra_fields(self) -> list[list[object]]:
        return []

    def command_line(self) -> str:
        return (
            "uv run --frozen --no-sync --group validation python -m scripts.run_scenario.story4_agent_patterns "
            f"--step {self.number} --environment {self.args.environment}"
        )

    def body(self) -> None:
        raise NotImplementedError

    def guard_start(self) -> dict[str, AgentSnapshot] | None:
        """Steps 2-7 reuse the Step 1 baseline unchanged; they never deploy or change an agent."""
        baseline = load_baseline(self.args.output_dir / BASELINE_FILE, self.args.environment)
        self.report.heading("Agents reused - before")
        before = snapshot_agents(self.read)
        self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in before.values()])
        inactive = [s.name for s in before.values() if s.status != "active" or not s.latest_routing]
        if inactive:
            raise Blocked(f"Agents not active with @latest routing: {', '.join(inactive)}. Run Story 4 Step 1.")
        differences = reuse_differences(baseline, before, version_change=None)
        self.record(
            "Agents reused - before",
            "FAIL" if differences else "PASS",
            "Identical to the Story 4 Step 1 baseline (versions, identities, toolbox wiring)",
            differences or {name: snapshot.version for name, snapshot in before.items()},
        )
        return None if differences else before

    def guard_end(self, before: Mapping[str, AgentSnapshot]) -> None:
        self.report.heading("Agents reused - after")
        after = snapshot_agents(self.read)
        self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in after.values()])
        differences = reuse_differences(before, after, version_change=None)
        self.record(
            "Agents reused - after",
            "FAIL" if differences else "PASS",
            "Same three agents, versions, identities, and toolbox wiring as at the start",
            differences or "No changes",
        )

    def token(self) -> str:
        return str(self.credential.get_token(DATA_SCOPE).token)

    def hosted_call(
        self,
        label: str,
        agent: str,
        text: str,
        *,
        headers: Mapping[str, str] | None = None,
        conversation: str | None = None,
        background: bool = False,
    ) -> Exchange:
        """One client request to an agent-bound Responses endpoint; no retries, every outcome recorded."""
        exchange = Exchange(label, agent)
        number = self.run.start_request(agent, "hosted", label)
        started = perf_counter()
        status: Status = "FAIL"
        try:
            with self.project.get_openai_client(
                agent_name=agent, timeout=self.args.timeout, max_retries=0, default_headers=dict(headers or {})
            ) as client:
                extra: dict[str, Any] = {"conversation": conversation} if conversation else {}
                if background:
                    response = client.responses.create(input=text, stream=False, background=True, store=True, **extra)
                else:
                    response = client.responses.create(input=text, stream=False, **extra)
                exchange.response_id = response.id
                exchange.statuses_seen.append(str(response.status))
                while background and response.status in ("queued", "in_progress"):
                    if perf_counter() - started > self.args.timeout:
                        raise TimeoutError(f"Background response did not finish in {self.args.timeout:.0f}s")
                    sleep(1)
                    exchange.polls += 1
                    response = client.responses.retrieve(response.id)
                    if exchange.statuses_seen[-1] != str(response.status):
                        exchange.statuses_seen.append(str(response.status))
            summary = response_summary(response.model_dump(mode="json"))
            exchange.status = str(summary.get("status"))
            exchange.text = str(summary.get("text") or "")
            if response.error:
                exchange.error = sanitize_text(response.error.message)[:500]
                exchange.error_code = response.error.code
            status = "PASS" if exchange.completed else "FAIL"
        except APIStatusError as error:
            exchange.status = "rejected"
            exchange.http_status = error.status_code
            body = error.body if isinstance(error.body, dict) else {}
            nested = body.get("error") if isinstance(body.get("error"), dict) else body
            exchange.error_code = str(nested.get("code")) if isinstance(nested, dict) and nested.get("code") else None
            exchange.error = sanitize_text(str(error))[:500]
        except (OpenAIError, AzureError, TimeoutError) as error:
            exchange.status = "error"
            exchange.error = sanitize_text(f"{type(error).__name__}: {error}")[:500]
        finally:
            exchange.seconds = round(perf_counter() - started, 2)
            self.run.end_request(number, agent, status, exchange.seconds * 1000)
            self.requests.append(
                [len(self.requests) + 1, label, agent, exchange.status, exchange.response_id or "-", exchange.seconds]
            )
        return exchange

    def delegate(self, label: str, command: DelegateCommand) -> tuple[Exchange, DelegationReport | None]:
        """Client -> Supervisor with a custom correlation header; parse the structured delegation report."""
        exchange = self.hosted_call(
            label, SUPERVISOR, command.wire(), headers={CORRELATION_HEADER: str(command.correlation_id)}
        )
        if not exchange.completed:
            return exchange, None
        try:
            return exchange, DelegationReport.model_validate_json(exchange.text)
        except ValidationError as error:
            exchange.error = f"Supervisor reply is not a poc-delegation-v1 report: {sanitize_text(str(error))[:200]}"
            return exchange, None

    def conclude(self) -> None:
        self.verdict = self.compute_verdict()
        conclusion, results, findings, gaps = self.summary()
        self.report.summary(render_summary(self.verdict, conclusion, results, findings, gaps))

    def compute_verdict(self) -> str:
        raise NotImplementedError

    def summary(self) -> tuple[str, list[str], list[Finding], list[str]]:
        raise NotImplementedError

    def blocked_or_failed(self) -> str | None:
        """Shared verdict for a run that never reached its demonstration."""
        if self.statuses.get("Prerequisite") == "BLOCKED":
            return "Not evaluated"
        if "FAIL" in (self.statuses.get("Agents reused - before"), self.statuses.get("Agents reused - after")):
            return "Not met"
        if self.statuses.get("Unexpected failure") == "FAIL":
            return "Not met"
        return None


def load_baseline(path: Path, environment: str) -> dict[str, AgentSnapshot]:
    """The Story 4 Step 1 baseline; Steps 2-7 never recreate it."""
    if not path.is_file():
        raise Blocked(f"Story 4 baseline {path.name} is missing. Run Story 4 Step 1 first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        agents = {name: AgentSnapshot(**value) for name, value in data["agents"].items()}
        recorded = data["environment"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Blocked(f"Story 4 baseline {path.name} is unreadable ({error}). Run Story 4 Step 1 again.") from error
    if recorded != environment:
        raise Blocked(f"Story 4 baseline was recorded for '{recorded}', not '{environment}'. Run Step 1 for it.")
    return agents


def write_baseline(
    path: Path, environment: str, run_id: str, agents: Mapping[str, AgentSnapshot], toolbox: str
) -> None:
    path.write_text(
        json.dumps(
            {
                "environment": environment,
                "recorded_at": datetime.now(UTC).isoformat(),
                "run_id": run_id,
                "research_toolbox_endpoint": toolbox,
                "agents": {name: asdict(snapshot) for name, snapshot in agents.items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


EXPECTED_ERRORS = (CliError, AzureError, OpenAIError, ValueError, OSError, subprocess.SubprocessError, KeyError)
