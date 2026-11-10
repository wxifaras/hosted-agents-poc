"""Capture explicit-version hosted timings; preview by default, --apply incurs Azure usage.

Exit codes: 0 preview/all checks pass, 1 observed failure, 2 invalid configuration.
Only allowlisted diagnostics and synthetic state comparisons are persisted.
"""

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter, sleep
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

LOGGER = logging.getLogger(__name__)
type Condition = Literal["baseline", "snapshot", "optimized"]
type Phase = Literal["cold", "warm", "resume"]
PREFIX = "x-ms-debug-latency-"
HEADER_METRICS = (
    "platform-preprocessing-ms",
    "infra-setup-ms",
    "container-readiness-ms",
    "container-response-ms",
    "response-begin-ms",
    "first-byte-ms",
    "total-last-byte-ms",
)
CLIENT_METRICS = (
    "client_headers_ms",
    "client_first_chunk_ms",
    "client_complete_ms",
    "client_journey_ms",
    "session_create_ms",
)


class Evidence(BaseModel):
    """Reject malformed/nonfinite evidence instead of producing plausible charts."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class RunInfo(Evidence):
    """Comparison controls for one condition, bound to an immutable version."""

    kind: Literal["run"] = "run"
    schema_version: Literal[1] = 1
    source: Literal["hosted"] = "hosted"
    run_id: str
    timestamp: str
    condition: Condition
    project_hash: str
    agent: str
    agent_version: str
    image: str = Field(pattern=r"^.+@sha256:[a-f0-9]{64}$")
    cpu: str
    memory: str
    configuration_hash: str
    snapshots: StrictBool
    core_version: str
    responses_version: str
    samples: int = Field(ge=1)
    resume_samples: int = Field(ge=0)
    idle_seconds: float = Field(ge=0)


class Sample(Evidence):
    """One attempt, including failures and absent native diagnostics."""

    kind: Literal["sample"] = "sample"
    trial: int = Field(ge=1)
    phase: Phase
    timestamp: str
    session_hash: str
    start_type: Literal["cold", "warm", "resume"] | None = None
    response_id: str | None = None
    passed: StrictBool = False
    errors: list[str] = Field(default_factory=list)
    timings: dict[str, float | None] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, str]) -> dict[str, str]:
        """Do not let imported evidence export arbitrary authentication headers."""
        allowed = {PREFIX + key for key in (*HEADER_METRICS, "session-start-type")}
        if not value.keys() <= allowed:
            raise ValueError("Evidence may contain only allowlisted native latency headers.")
        return value

    @field_validator("timings")
    @classmethod
    def valid_timings(cls, value: dict[str, float | None]) -> dict[str, float | None]:
        """Missing is unknown; negative/nonfinite times are invalid."""
        for key, timing in value.items():
            if key not in (*HEADER_METRICS, *CLIENT_METRICS):
                raise ValueError(f"Unknown timing metric: {key}")
            if timing is not None and (not math.isfinite(timing) or timing < 0):
                raise ValueError("Timings must be finite nonnegative milliseconds or null.")
        return value


class ProbeReply(Evidence):
    """Expected deterministic output, never written verbatim to evidence."""

    schema_name: Literal["story13-probe-v1"] = Field(alias="schema")
    previous_memory: str | None
    previous_file: str | None
    turn: int = Field(ge=1)
    restores: int = Field(ge=0)
    instance: str = Field(pattern=r"^[a-f0-9]{32}$")
    request: str = Field(pattern=r"^[a-f0-9]{32}$")
    core_version: Literal["2.3.0"]
    responses_version: Literal["2.4.0"]


class OutputText(BaseModel):
    """Validate the text content needed by this no-model probe."""

    model_config = ConfigDict(extra="ignore", strict=True)
    type: Literal["output_text"]
    text: str


class OutputMessage(BaseModel):
    """Accept unrelated protocol metadata without requiring model/tool fields."""

    model_config = ConfigDict(extra="ignore", strict=True)
    type: Literal["message"]
    role: Literal["assistant"]
    content: list[OutputText]


class HostedResponse(BaseModel):
    """Validate native Responses output, whose no-tool replies omit tools/tool_choice."""

    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(min_length=1)
    status: Literal["completed", "failed", "incomplete", "cancelled", "queued", "in_progress"]
    output: list[OutputMessage]
    error: object | None = None
    agent_session_id: str | None = Field(default=None, min_length=1)

    @property
    def output_text(self) -> str:
        """Return only validated assistant text for the deterministic probe."""
        return "".join(part.text for message in self.output for part in message.content)


def digest(value: str) -> str:
    """Use a stable opaque binding in evidence instead of raw session/project IDs."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def diagnostics(headers: Mapping[str, str]) -> tuple[dict[str, str], dict[str, float | None], Phase | None]:
    """Parse only documented latency headers; do not infer missing components."""
    lowered = {key.lower(): value for key, value in headers.items()}
    allowed = (*HEADER_METRICS, "session-start-type")
    captured = {PREFIX + key: lowered[PREFIX + key] for key in allowed if PREFIX + key in lowered}
    timings: dict[str, float | None] = {}
    for metric in HEADER_METRICS:
        raw = captured.get(PREFIX + metric)
        if raw is not None and not re.fullmatch(r"[0-9]+", raw):
            raise ValueError(f"Malformed native latency header: {metric}")
        timing = float(raw) if raw is not None else None
        if timing is not None and not math.isfinite(timing):
            raise ValueError(f"Native latency header overflows milliseconds: {metric}")
        timings[metric] = timing
    start = captured.get(PREFIX + "session-start-type")
    if start is not None and start not in ("cold", "warm", "resume"):
        raise ValueError("Unknown native session-start classification.")
    if start == "cold":
        return captured, timings, "cold"
    if start == "warm":
        return captured, timings, "warm"
    if start == "resume":
        return captured, timings, "resume"
    return captured, timings, None


def state_errors(
    reply: ProbeReply, phase: Phase, marker: str, snapshots: bool, previous: ProbeReply | None
) -> list[str]:
    """Check clean sessions, same-session warmth, and documented snapshot reset."""
    errors: list[str] = []
    if phase == "cold" or (phase == "resume" and snapshots):
        if reply.previous_memory is not None or reply.previous_file is not None or reply.turn != 1:
            errors.append("Expected clean process/file state for a new or snapshot-restored sandbox.")
    elif phase == "warm":
        if reply.previous_memory != marker or reply.previous_file != marker or reply.turn != 2:
            errors.append("Warm invocation did not retain this session's synthetic state.")
    elif reply.previous_file != marker:
        errors.append("Snapshot-disabled idle/resume did not preserve the synthetic session file.")
    if snapshots and reply.restores < 1:
        errors.append("Snapshot-enabled invocation has no completed application restore hook.")
    if previous is not None:
        if reply.request == previous.request:
            errors.append("Request identifier was reused.")
        if phase == "warm" and reply.instance != previous.instance:
            errors.append("Warm request unexpectedly changed the runtime instance.")
        if phase == "resume" and snapshots and reply.instance == previous.instance:
            errors.append("Snapshot resume did not regenerate the runtime instance identifier.")
    return errors


def invoke(
    client: OpenAI,
    session: str | None,
    marker: str,
    phase: Phase,
    trial: int,
    snapshots: bool,
    previous: ProbeReply | None,
) -> tuple[Sample, ProbeReply | None, str | None]:
    """Time one POST without automatic retries; delayed diagnostics use GET only."""
    sample = Sample(
        trial=trial,
        phase=phase,
        timestamp=datetime.now(UTC).isoformat(),
        session_hash=digest(session) if session else "unavailable",
    )
    started = perf_counter()
    try:
        with client.responses.with_streaming_response.create(
            input=marker,
            stream=False,
            extra_body={"agent_session_id": session} if session else {},
            extra_headers={PREFIX + "enabled": "true"},
        ) as raw:
            sample.timings["client_headers_ms"] = (perf_counter() - started) * 1000
            sample.headers, native, sample.start_type = diagnostics(dict(raw.headers))
            sample.timings.update(native)
            chunks: list[bytes] = []
            for chunk in raw.iter_bytes():
                if not chunks and chunk:
                    sample.timings["client_first_chunk_ms"] = (perf_counter() - started) * 1000
                if chunk:
                    chunks.append(chunk)
            complete_ms = (perf_counter() - started) * 1000
            sample.timings["client_complete_ms"] = complete_ms
            sample.timings["client_journey_ms"] = complete_ms
            response = HostedResponse.model_validate_json(b"".join(chunks))
            headers: dict[str, str] = dict(raw.headers)
        sample.response_id = response.id
        if session is None:
            if not response.agent_session_id:
                raise ValueError("Implicit cold invocation returned no session binding.")
            session = response.agent_session_id
            sample.session_hash = digest(session)
        elif response.agent_session_id is not None and response.agent_session_id != session:
            raise ValueError("Response session binding differs from the requested session.")
        sample.headers, native, sample.start_type = diagnostics(headers)
        sample.timings.update(native)
        # Eventual consistency may delay stored timings. Never retry an invocation.
        for _ in range(3):
            if all(PREFIX + name in headers for name in ("session-start-type", "total-last-byte-ms")):
                break
            sleep(0.5)
            stored = client.responses.with_raw_response.retrieve(response.id)
            headers.update(stored.headers)
        sample.headers, native, sample.start_type = diagnostics(headers)
        sample.timings.update(native)
        if response.status != "completed" or response.error is not None:
            sample.errors.append("Response did not complete successfully.")
            return sample, None, session
        reply = ProbeReply.model_validate_json(response.output_text)
        if sample.start_type != phase:
            sample.errors.append(f"Expected {phase}; native classification was {sample.start_type or 'unknown'}.")
        sample.errors.extend(state_errors(reply, phase, marker, snapshots, previous))
        sample.passed = not sample.errors
        return sample, reply, session
    except ValidationError as error:
        sample.timings.setdefault("client_complete_ms", (perf_counter() - started) * 1000)
        details = ", ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['type']}"
            for issue in error.errors(include_input=False, include_context=False, include_url=False)[:5]
        )
        sample.errors.append(f"Response validation failed ({details}); no POST retry attempted.")
        LOGGER.error("%s", sample.errors[-1])
        return sample, None, session
    except (OpenAIError, AzureError, OSError, ValueError) as error:
        sample.timings.setdefault("client_complete_ms", (perf_counter() - started) * 1000)
        sample.errors.append(f"Invocation/diagnostic failure: {type(error).__name__}; no POST retry attempted.")
        LOGGER.error("%s", sample.errors[-1])
        return sample, None, session


def run_info(args: argparse.Namespace, record: Mapping[str, object]) -> RunInfo:
    """Require an active image version and the expected snapshot flag before timing."""
    definition = record.get("definition")
    if not isinstance(definition, dict):
        raise ValueError("Agent version has no definition.")
    container = definition.get("container_configuration")
    image = container.get("image") if isinstance(container, dict) else None
    if record.get("status") != "active" or not isinstance(image, str) or "@sha256:" not in image:
        raise ValueError("Require an active version with a digest-pinned container image.")
    snapshots = definition.get("enable_memory_snapshot", False)
    if not isinstance(snapshots, bool) or snapshots != (args.condition != "baseline"):
        raise ValueError("Version snapshot flag does not match the selected experiment condition.")
    cpu, memory = definition.get("cpu"), definition.get("memory")
    if cpu is None or memory is None:
        raise ValueError("Version definition must expose CPU and memory controls.")
    if str(record.get("version")) != args.version:
        raise ValueError("Returned version does not match the explicitly selected version.")
    controls = {
        key: value
        for key, value in definition.items()
        if key not in ("container_configuration", "enable_memory_snapshot")
    }
    return RunInfo(
        run_id=uuid4().hex,
        timestamp=datetime.now(UTC).isoformat(),
        condition=args.condition,
        project_hash=digest(args.project_endpoint.rstrip("/")),
        agent=args.agent,
        agent_version=args.version,
        image=image,
        cpu=str(cpu),
        memory=str(memory),
        configuration_hash=digest(json.dumps(controls, sort_keys=True)),
        snapshots=snapshots,
        core_version=version("azure-ai-agentserver-core"),
        responses_version=version("azure-ai-agentserver-responses"),
        samples=args.samples,
        resume_samples=args.resume_samples,
        idle_seconds=args.idle_seconds,
    )


def append_record(path: Path, record: Evidence) -> None:
    """Flush every attempt immediately so an interrupted experiment retains evidence."""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(record.model_dump_json() + "\n")
        stream.flush()


def endpoint_matches(record: Mapping[str, object], agent_version: str) -> bool:
    """Require native endpoint routing pinned to one version before implicit cold starts."""
    endpoint = record.get("agent_endpoint")
    selector = endpoint.get("version_selector") if isinstance(endpoint, dict) else None
    rules = selector.get("version_selection_rules") if isinstance(selector, dict) else None
    return rules == [{"type": "FixedRatio", "agent_version": agent_version, "traffic_percentage": 100}]


def checked_info(project: AIProjectClient, args: argparse.Namespace) -> RunInfo:
    """Share read-only version/route checks between verification and execution."""
    info = run_info(args, project.agents.get_version(args.agent, args.version).as_dict())
    if not endpoint_matches(project.agents.get(args.agent).as_dict(), args.version):
        raise ValueError("Pin the isolated agent endpoint to this exact version before measuring.")
    return info


def verify_version(args: argparse.Namespace) -> int:
    """Check deployment controls without creating sessions, invocations, or evidence files."""
    with (
        AzureCliCredential() as credential,
        AIProjectClient(
            endpoint=args.project_endpoint, credential=credential, allow_preview=True, retry_total=0
        ) as project,
    ):
        info = checked_info(project, args)
    print(
        f"VERIFIED: {info.agent} version {info.agent_version}; condition={info.condition}; snapshots={info.snapshots}."
    )
    return 0


def resolve_endpoint(explicit: str | None, environment: str) -> str:
    """Read only the isolated azd environment when no explicit endpoint was provided."""
    if explicit is not None:
        return explicit
    completed = subprocess.run(
        ["azd", "env", "get-values", "--environment", environment, "--output", "json"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    document: object = json.loads(completed.stdout)
    if not isinstance(document, dict):
        raise ValueError("The isolated azd environment did not return an object.")
    candidates = [document.get(key) for key in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT")]
    if any(value is not None and not isinstance(value, str) for value in candidates):
        raise ValueError("Foundry project endpoint aliases must be strings.")
    present = [value.rstrip("/") for value in candidates if isinstance(value, str) and value]
    if not present or len(set(present)) != 1:
        raise ValueError("Configure matching Foundry project endpoint aliases in the isolated azd environment.")
    return present[0]


def validate_endpoint(value: str) -> None:
    """Reject malformed endpoint inputs before authenticating to Azure."""
    endpoint = urlparse(value)
    if (
        endpoint.scheme != "https"
        or not endpoint.netloc
        or endpoint.query
        or endpoint.fragment
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise ValueError("Provide an HTTPS Foundry project endpoint without a query or fragment.")


def collect(args: argparse.Namespace) -> int:
    """Invoke new implicit sessions so management prewarming cannot mask cold starts."""
    with (
        AzureCliCredential() as credential,
        AIProjectClient(
            endpoint=args.project_endpoint, credential=credential, allow_preview=True, retry_total=0
        ) as project,
        project.get_openai_client(agent_name=args.agent, max_retries=0, timeout=args.timeout) as client,
    ):
        info = checked_info(project, args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(info.model_dump_json() + "\n")
        failures = 0
        for trial in range(1, args.samples + 1):
            session: str | None = None
            marker = uuid4().hex
            previous: ProbeReply | None = None
            phases: list[Phase] = ["cold", "warm"]
            if trial <= args.resume_samples:
                phases.append("resume")
            for phase in phases:
                if phase == "resume":
                    LOGGER.info("Waiting %.0f seconds without invoking session trial %d", args.idle_seconds, trial)
                    sleep(args.idle_seconds)
                sample, reply, session = invoke(
                    client,
                    session,
                    marker,
                    phase,
                    trial,
                    info.snapshots,
                    previous,
                )
                if phase == "cold" and sample.passed and session is not None:
                    try:
                        binding = project.agents.get_session(args.agent, session).as_dict().get("version_indicator")
                        if not isinstance(binding, dict) or binding.get("agent_version") != args.version:
                            raise ValueError("Implicit session is not bound to the approved version.")
                    except (AzureError, OSError, ValueError) as error:
                        sample.passed = False
                        sample.errors.append(f"Session version verification failed: {type(error).__name__}.")
                append_record(args.output, sample)
                failures += not sample.passed
                LOGGER.info(
                    "trial=%d requested=%s observed=%s passed=%s", trial, phase, sample.start_type, sample.passed
                )
                if reply is None or not sample.passed:
                    LOGGER.error("Stopping this run after a failed check; remaining budget was not spent.")
                    return 1
                previous = reply
        LOGGER.info(
            "Evidence saved; %d failed attempts. Sessions created here are left to native idle expiry.", failures
        )
        return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    """Expose the paid budget and explicit bindings without reading ambient azd state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", help="Explicit endpoint; otherwise read the isolated azd environment.")
    parser.add_argument("--environment", default="story13-cold-start")
    parser.add_argument("--agent", default="story13-cold-start-probe")
    parser.add_argument("--version", required=True, help="Explicit immutable version; never latest.")
    parser.add_argument("--condition", choices=("baseline", "snapshot", "optimized"), required=True)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--resume-samples", type=int, default=1)
    parser.add_argument("--idle-seconds", type=float, default=210)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--output", type=Path, required=True, help="New JSONL file; existing evidence is never overwritten."
    )
    parser.add_argument("--apply", action="store_true", help="Approve paid hosted invocations; otherwise preview only.")
    parser.add_argument(
        "--execution",
        choices=("preview", "verify", "run"),
        default="preview",
        help="preview: no Azure calls; verify: metadata GETs only; run: approved paid scenario.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate inputs before authentication, file creation, or hosted requests."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.project_endpoint is not None:
        try:
            validate_endpoint(args.project_endpoint)
        except ValueError as error:
            parser.error(str(error))
    if args.apply and args.execution == "verify":
        parser.error("--apply cannot be combined with read-only verification.")
    if not re.fullmatch(r"[0-9]+", args.version) or int(args.version) < 1:
        parser.error("--version must be a positive numeric immutable version.")
    if args.samples < 1 or not 0 <= args.resume_samples <= args.samples:
        parser.error("Require samples >= 1 and 0 <= resume-samples <= samples.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive.")
    if not math.isfinite(args.idle_seconds) or args.idle_seconds <= 120:
        parser.error("--idle-seconds must exceed the configured 120-second idle timeout.")
    if args.output.exists() and args.execution != "verify":
        parser.error("--output already exists; choose a new evidence filename.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    count = args.samples * 2 + args.resume_samples
    if not args.apply and args.execution == "preview":
        print(
            f"PREVIEW: {args.samples} new version-bound sessions; at most {count} POSTs; "
            f"{args.resume_samples} idle waits. No authentication, files, deployment, or Azure requests. "
            "Select --execution run or add --apply only after approving this budget."
        )
        return 0
    try:
        args.project_endpoint = resolve_endpoint(args.project_endpoint, args.environment)
        validate_endpoint(args.project_endpoint)
        if args.execution == "verify":
            return verify_version(args)
        return collect(args)
    except ValidationError:
        LOGGER.error("Run blocked: deployment metadata did not match the evidence schema.")
        return 2
    except ValueError as error:
        LOGGER.error("Run blocked: %s", error)
        return 2
    except (AzureError, OpenAIError, OSError, subprocess.SubprocessError) as error:
        LOGGER.error(
            "Run blocked: %s. Check endpoint, access, version, and output path; no retry.", type(error).__name__
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
