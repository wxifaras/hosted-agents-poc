"""Offline reusable Jinja2 report generator for manifest + results/events JSONL evidence.

No cloud access, models, replay, dependency installation, external assets or JavaScript.
Returns 0 when report generation succeeds (not a test verdict), 1 for operational
errors, 2 for malformed/incompatible evidence. Existing originals are never modified.
"""

import argparse
import base64
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from app.core.redaction import sanitize, sanitize_text
from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import JsonValue

from scripts.check_details import describe_check
from scripts.report_api_catalog import api_catalog, related_api_ids
from scripts.report_appendix import build_appendix
from scripts.report_diagrams import architecture_svg, diagram_sources, native_bypass_sequence_svg, sequence_svg
from scripts.report_task_flows import build_task_flow

MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 1024 * 1024
MAX_ATTACHMENTS = 8
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_OPAQUE = re.compile(
    r"\b(?:cntr|conv|caresp|resp|assistant|file)_[A-Za-z0-9_-]+\b"
    r"|\b(?:assistant|file)-[A-Za-z0-9_-]+\b"
)
_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL = re.compile(r"https?://[^\s<>\"']+")
_WINPATH = re.compile(r"[A-Za-z]:\\[^\r\n\"<>]+")
_ARM = re.compile(r"/subscriptions/[^\s\"<>]+")
_LONG_HEX = re.compile(r"\b[0-9a-f]{32}\b", re.I)
_PRIVATE_KEY = re.compile(r"private.?key|passphrase", re.I)


class ReportError(ValueError):
    """Evidence cannot support a trustworthy offline report."""


class Redactor:
    """Redact material before template rendering and before downloadable text copies."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}

    def text(self, value: str) -> str:
        text = sanitize_text(value)
        for pattern, label in (
            (_EMAIL, "user"),
            (_GUID, "id"),
            (_OPAQUE, "reference"),
            (_LONG_HEX, "reference"),
            (_URL, "url"),
            (_WINPATH, "local-path"),
            (_ARM, "scope"),
        ):

            def replace(match: re.Match[str], kind: str = label) -> str:
                original = match.group(0)
                if original not in self.aliases:
                    self.aliases[original] = f"{{{kind}-{len(self.aliases) + 1}}}"
                return self.aliases[original]

            text = pattern.sub(replace, text)
        return text

    def value(self, value: object) -> JsonValue:
        """Retain safe hashes/counts/statuses; never serialize arbitrary objects as raw repr."""
        safe = sanitize(value)
        if isinstance(safe, dict):
            return {
                self.text(key): "[REDACTED]"
                if _PRIVATE_KEY.search(key)
                else "[TEST-CONTROL DATA OMITTED]"
                if key in {"sealed_probe_lineage", "ciphertext", "wrapped_key", "nonce"}
                else self.value(item)
                for key, item in safe.items()
            }
        if isinstance(safe, list):
            return [self.value(item) for item in safe]
        if isinstance(safe, str):
            return self.text(safe)
        if isinstance(safe, float) and not math.isfinite(safe):
            return "Invalid nonfinite value"
        return safe


def read_bytes(path: Path, limit: int = MAX_EVIDENCE_BYTES) -> bytes:
    """Bound local input reads and reject links; never follow evidence to unrelated files."""
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ReportError(f"Expected a regular evidence file: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ReportError(f"Evidence file exceeds size limit: {path.name}")
    return data


def read_object(path: Path) -> dict[str, JsonValue]:
    value: object = json.loads(read_bytes(path))
    if not isinstance(value, dict):
        raise ReportError(f"Expected JSON object: {path.name}")
    return value


def read_records(path: Path, *, interrupted: bool = False) -> tuple[list[dict[str, JsonValue]], list[str]]:
    """Only tolerate an incomplete final JSONL line when explicit incomplete-report handling is requested."""
    if not path.exists():
        return [], [f"{path.name} was not recorded."]
    raw = read_bytes(path).decode("utf-8")
    records: list[dict[str, JsonValue]] = []
    warnings: list[str] = []
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            value: object = json.loads(line)
        except ValueError as error:
            if interrupted and index == len(lines) - 1 and not raw.endswith("\n"):
                warnings.append(f"{path.name}: incomplete final record omitted; run is incomplete.")
                break
            raise ReportError(f"{path.name}: malformed record {index + 1}.") from error
        if not isinstance(value, dict):
            raise ReportError(f"{path.name}: record {index + 1} must be an object.")
        records.append(value)
    return records, warnings


def expected_consumer_checks(*, expanded: bool = False) -> list[tuple[str, str]]:
    """Versioned conceptual coverage; old checks map here without inventing executions."""
    checks = [("deployment", "Deployment and effective Consumer permissions")]
    for actor in ("user1", "user2"):
        resource_owner = "user2" if actor == "user1" else "user1"
        checks.extend(
            (f"{actor}:{key}", f"{actor}: {name}")
            for key, name in (
                ("runtime", "Deployed runtime/source verification"),
                ("input", "Original input binding"),
                ("json-bytes", "Original JSON delivery"),
                ("png-bytes", "Original PNG delivery"),
                ("values", "Input-specific numerical values/marker"),
                ("png", "PNG file check"),
                ("foreign-json", f"Download {resource_owner}'s JSON denied"),
                ("foreign-png", f"Download {resource_owner}'s PNG denied"),
                ("inspect", f"Inspect {resource_owner}'s workflow denied"),
                ("artifact", f"Inspect {resource_owner}'s artifact denied"),
                ("response", f"Retrieve {resource_owner}'s Hosted response denied"),
                ("listing", "Native Files listing denied"),
            )
        )
        if expanded:
            checks.extend(
                (
                    f"{actor}:native-{kind}" + (":" + name if name else ""),
                    f"{actor}: {resource_owner}'s native {kind} {name or 'input/container'} denied",
                )
                for kind, name in (
                    ("input-metadata", None),
                    ("input-content", None),
                    ("container-list", None),
                    ("output-metadata", "result.json"),
                    ("output-content", "result.json"),
                    ("output-metadata", "chart.png"),
                    ("output-content", "chart.png"),
                )
            )
    return checks


def display_references(record: dict[str, JsonValue], key: str | None, *, consumer: bool) -> tuple[str, str, str]:
    """Normalize only known legacy directions; never guess generic resource ownership."""
    name = str(record["step"])
    actor = str(
        record.get("actor") or (name.split(":", 1)[0] if name.startswith(("user1:", "user2:")) else "unspecified")
    )
    owner = str(record.get("resource_owner") or "unspecified")
    if consumer and key and actor in ("user1", "user2"):
        operation = key.split(":", 1)[1]
        if owner == "unspecified" and operation in {"runtime", "input", "json-bytes", "png-bytes", "values", "png"}:
            owner = actor
        if owner == "unspecified" and operation in {"foreign-json", "foreign-png", "inspect", "artifact", "response"}:
            owner = "user2" if actor == "user1" else "user1"
        if "foreign" in name.lower() and owner != "unspecified":
            if operation.startswith("foreign-"):
                filename = "result.json" if operation == "foreign-json" else "chart.png"
                name = f"{actor}: download {owner}'s {filename} denied (legacy label normalized)"
            else:
                name = f"{actor}: access {owner}'s {operation} denied (legacy label normalized)"
    return name, actor, owner


def coverage_key(name: str, ordinal: int) -> str | None:
    if name == "Consumer deployment and permissions":
        return "deployment"
    actor = name.split(":", 1)[0]
    if actor not in ("user1", "user2"):
        return None
    lower = name.lower()
    if "deployed runtime" in lower:
        key = "runtime"
    elif "original input" in lower:
        key = "input"
    elif "original bytes" in lower:
        key = "png-bytes" if "chart.png" in lower or ordinal == 2 else "json-bytes"
    elif "analysis values" in lower:
        key = "values"
    elif "png bytes" in lower:
        key = "png"
    elif "foreign hosted output download" in lower or ("download " in lower and " denied" in lower):
        key = "foreign-png" if "chart.png" in lower or ordinal == 2 else "foreign-json"
    elif "foreign application inspect" in lower:
        key = "inspect"
    elif "foreign application artifact" in lower:
        key = "artifact"
    elif "foreign hosted response" in lower:
        key = "response"
    elif "native files privilege boundary" in lower:
        key = "listing"
    else:
        return None
    return f"{actor}:{key}"


def pretty(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False)


def explicit_user_text(text: str, actor: str, resource_owner: str, key: str | None) -> str:
    """Clarify role wording only when the recorded check establishes both user references."""
    if actor not in ("user1", "user2") or resource_owner not in ("user1", "user2") or not key:
        return text
    other = actor if resource_owner != actor else ("user2" if actor == "user1" else "user1")
    text = re.sub(r"\b(?:the )?other user\b", other, text, flags=re.I)
    text = re.sub(r"\b(?:the )?owner\b", resource_owner, text, flags=re.I)
    return text


def boundary_status(cases: list[dict[str, object]], required: set[str]) -> str:
    """Aggregate only the actual checks required for one boundary."""
    statuses = {str(case["coverage_key"]): str(case["status"]) for case in cases if case["coverage_key"] in required}
    if any(value == "FAIL" for value in statuses.values()):
        return "FAIL"
    if any(value == "BLOCKED" for value in statuses.values()):
        return "BLOCKED"
    if set(statuses) != required:
        return "NOT RECORDED"
    return "PASS" if all(value == "PASS" for value in statuses.values()) else "INCOMPLETE"


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def attachment_records(run: Path, redactor: Redactor) -> tuple[list[dict[str, str]], list[str]]:
    """Only include explicitly captured synthetic outputs with verified receipts; never glob arbitrary inputs."""
    manifest = run / "report-attachments.json"
    if not manifest.exists():
        return [], [
            "Original JSON/PNG attachments were not captured for this run. No files were fetched or regenerated."
        ]
    records = read_object(manifest).get("attachments")
    if not isinstance(records, list) or len(records) > MAX_ATTACHMENTS:
        raise ReportError("Invalid synthetic attachment inventory.")
    output: list[dict[str, str]] = []
    warnings: list[str] = []
    for entry in records:
        if not isinstance(entry, dict):
            raise ReportError("Invalid synthetic attachment receipt.")
        actor, name, relative = entry.get("actor"), entry.get("filename"), entry.get("local_file")
        if actor not in ("user1", "user2") or name not in ("result.json", "chart.png") or not isinstance(relative, str):
            raise ReportError("Unrecognized synthetic output attachment.")
        if relative != f"report-artifacts/{actor}-{name}":
            raise ReportError("Attachment path is outside the fixed synthetic-output capture directory.")
        path = run / "report-artifacts" / f"{actor}-{name}"
        if (run / "report-artifacts").is_symlink() or (run / "report-artifacts").is_junction():
            raise ReportError("Attachment directory must not be a link.")
        data = read_bytes(path, MAX_ATTACHMENT_BYTES)
        checksum = sha256(data).hexdigest()
        if len(data) != entry.get("length") or checksum != entry.get("sha256"):
            raise ReportError("Captured output no longer matches its original-byte receipt.")
        media = "application/json" if name == "result.json" else "image/png"
        if name == "chart.png":
            if not data.startswith(PNG_HEADER):
                raise ReportError("Captured chart is not a PNG.")
            contents = data
        else:
            parsed: object = json.loads(data)
            if not isinstance(parsed, dict) or set(parsed) != {"total", "marker"}:
                raise ReportError("Only bounded synthetic total/marker JSON is attachable.")
            contents = pretty(redactor.value(parsed)).encode()
        exact = contents == data
        output.append(
            {
                "name": f"{actor}-{name}",
                "actor": str(actor),
                "media": media,
                "url": f"data:{media};base64," + base64.b64encode(contents).decode(),
                "text": contents.decode() if media == "application/json" else "",
                "sha256": checksum,
                "length": str(len(data)),
                "notice": "Original bytes"
                if exact
                else "Sanitized/formatted JSON copy; hash/length describe original bytes",
            }
        )
    return output, warnings


def generate_report(
    run_dir: Path, *, title: str = "Validation evidence report", archive: bool = True, allow_incomplete: bool = False
) -> Path:
    """Generate a new self-contained report directory without overwriting evidence or rerunning tests."""
    run = run_dir.resolve(strict=True)
    if not run.is_dir():
        raise ReportError("Run path must be an evidence directory.")
    manifest = read_object(run / "manifest.json")
    results, warnings = read_records(run / "results.jsonl", interrupted=allow_incomplete)
    events, event_warnings = read_records(run / "events.jsonl", interrupted=allow_incomplete)
    warnings.extend(event_warnings)
    for record in results:
        if not isinstance(record.get("step"), str) or record.get("status") not in ("PASS", "FAIL", "BLOCKED", "SKIP"):
            raise ReportError("Result must contain a step and a supported verdict.")
        if record.get("run_id") != manifest.get("run_id"):
            raise ReportError("Result belongs to a different run.")
    for record in events:
        if record.get("run_id") not in (None, manifest.get("run_id")):
            raise ReportError("Event belongs to a different run.")
    redactor = Redactor()
    conclusion_path = run / "conclusion.json"
    conclusion = read_object(conclusion_path) if conclusion_path.exists() else {}
    plan_path = run / "suite-plan.json"
    plan = read_object(plan_path) if plan_path.exists() else {}
    if plan and plan.get("run_id") != manifest.get("run_id"):
        raise ReportError("Suite plan belongs to a different run.")
    if plan and conclusion and plan.get("suite") != conclusion.get("suite"):
        raise ReportError("Suite plan and completion refer to different acceptance versions.")
    consumer = manifest.get("scenario") == "consumer-native-ci"
    cases: list[dict[str, object]] = []
    catalog = api_catalog() if consumer else []
    catalog_by_id = {entry["id"]: entry for entry in catalog}
    covered: set[str] = set()
    occurrences: Counter[str] = Counter()
    for index, result in enumerate(results):
        name = str(result["step"])
        occurrences[name] += 1
        key = (
            str(result["check_id"])
            if isinstance(result.get("check_id"), str)
            else (coverage_key(name, occurrences[name]) if consumer else None)
        )
        if key:
            covered.add(key)
        display_name, actor, resource_owner = display_references(result, key, consumer=consumer)
        recorded_execution = result.get("execution")
        if recorded_execution is not None and (
            not isinstance(recorded_execution, dict)
            or any(not isinstance(recorded_execution.get(field), str) for field in describe_check(name, key))
        ):
            raise ReportError("Recorded execution description is incomplete or malformed.")
        execution: dict[str, object] = (
            dict(recorded_execution)
            if isinstance(recorded_execution, dict)
            else dict(describe_check(name, key, basis="implementation"))
        )
        correlation = execution.get("correlation_id")
        correlated = [
            event
            for event in events
            if isinstance(correlation, str) and correlation and event.get("correlation_id") == correlation
        ]
        observed = result.get("observed")
        if isinstance(observed, dict) and isinstance(observed.get("model_prompt"), str):
            execution["prompt"] = observed["model_prompt"]
            execution["prompt_applicability"] = "Recorded backend-confirmed submitted model prompt."
        phase_evidence = [
            {
                "phase": phase,
                "records": [
                    pretty(redactor.value(event)) for event in correlated if event.get("control_phase") == phase
                ],
            }
            for phase in ("positive_before", "negative_attempt", "positive_after", "hosted_delivery_recheck")
        ]
        cases.append(
            {
                "id": sha256(f"{manifest.get('scenario')}:{key or name}".encode()).hexdigest()[:16],
                "name": redactor.text(display_name),
                "actor": redactor.text(actor),
                "resource_owner": redactor.text(resource_owner),
                "coverage_key": key,
                "api_references": [
                    catalog_by_id[identifier]
                    for identifier in related_api_ids(name, key, execution)
                    if identifier in catalog_by_id
                ],
                "execution": redactor.value(execution),
                "phase_evidence": phase_evidence,
                "correlation_notice": (
                    "Linked by explicitly recorded operation correlation ID; no timestamp-based inference."
                    if correlated
                    else "Per-check correlation was not recorded. See raw timeline; HTTP statuses/controls "
                    "are not reconstructed from neighboring events."
                ),
                "status": result["status"],
                "expected": redactor.text(
                    explicit_user_text(str(result.get("expected", "Not recorded")), actor, resource_owner, key)
                ),
                "detail": redactor.text(
                    explicit_user_text(
                        str(result.get("detailed_step", result.get("expected", "Not recorded"))),
                        actor,
                        resource_owner,
                        key,
                    )
                ),
                "observed": pretty(redactor.value(result.get("observed"))),
                "timestamp": str(result.get("timestamp", "Not recorded")),
                "duration": (
                    f"{result['duration_ms']} ms (recorded)"
                    if type(result.get("duration_ms")) in (int, float)
                    else "Not recorded; assertion timestamps are not operation durations"
                ),
                "ordinal": index + 1,
            }
        )
    missing = [(key, name) for key, name in expected_consumer_checks() if key not in covered] if consumer else []
    required = expected_consumer_checks() if consumer else []
    advanced_required = expected_consumer_checks(expanded=True) if consumer else []
    advanced_missing = [(key, name) for key, name in advanced_required if key not in covered]
    mandatory_missing = [(key, name) for key, name in required if key not in covered]
    mandatory_keys = {key for key, _ in required}
    failed_required = [case for case in cases if case["coverage_key"] in mandatory_keys and case["status"] != "PASS"]
    expanded_run = (plan.get("suite") or conclusion.get("suite")) == "consumer-native-v2"
    if not consumer:
        acceptance = "NOT APPLICABLE"
    elif any(case["status"] == "FAIL" for case in failed_required):
        acceptance = "FAILED"
    elif any(case["status"] == "BLOCKED" for case in cases) and not all(
        any(case["coverage_key"] == key and case["status"] == "PASS" for case in cases) for key in mandatory_keys
    ):
        acceptance = "BLOCKED"
    elif (
        not mandatory_missing
        and not failed_required
        and not warnings
        and (
            (
                conclusion.get("state") == "finished"
                and conclusion.get("analysis_attempts", 2) == 2
                and (expanded_run or conclusion.get("validation_exit_code") == 0)
                and (expanded_run or conclusion.get("foreign_probes", 12) == 12)
            )
            or (not conclusion and bool(results) and all(case["status"] == "PASS" for case in cases))
        )
    ):
        acceptance = "COMPLETE"
    else:
        acceptance = "INCOMPLETE"
    advanced_failed = [
        case
        for case in cases
        if case["coverage_key"] in {key for key, _ in advanced_required} and case["status"] != "PASS"
    ]
    advanced_acceptance = (
        "NOT ASSESSED - OPTIONAL"
        if not expanded_run
        else "FAILED"
        if any(case["status"] == "FAIL" for case in advanced_failed)
        else "BLOCKED"
        if any(case["status"] == "BLOCKED" for case in cases)
        else "COMPLETE"
        if (
            not advanced_missing
            and not advanced_failed
            and not warnings
            and conclusion.get("state") == "finished"
            and conclusion.get("validation_exit_code") == 0
            and conclusion.get("analysis_attempts") == 2
            and conclusion.get("foreign_probes") == 26
            and conclusion.get("native_control_requests") == 28
        )
        else "INCOMPLETE"
    )
    counts = Counter(str(case["status"]) for case in cases)
    legacy_complete = consumer and not expanded_run and not missing and bool(results) and not warnings
    explicit_complete = conclusion.get("state") == "finished"
    if conclusion and (
        conclusion.get("run_id") != manifest.get("run_id") or conclusion.get("validation_exit_code") not in (0, 1, 2)
    ):
        raise ReportError("Invalid conclusion record.")
    if explicit_complete and conclusion.get("validation_exit_code") == 0 and (counts["FAIL"] or counts["BLOCKED"]):
        raise ReportError("Successful conclusion conflicts with recorded failed/blocked checks.")
    if counts["FAIL"]:
        verdict = "FAIL"
    elif counts["BLOCKED"]:
        verdict = "BLOCKED"
    elif counts["SKIP"]:
        verdict = "INCOMPLETE"
    elif results and (explicit_complete or legacy_complete) and not warnings:
        verdict = "PASS" if not consumer or not missing else "INCOMPLETE"
    else:
        verdict = "INCOMPLETE"
    if not conclusion:
        warnings.append(
            "No explicit run completion record. Legacy consumer coverage indicates all core checks were recorded; "
            "this is inferred completeness, not a recorded finish event."
            if legacy_complete
            else "No explicit completion record; do not interpret a partial set of passing checks as a completed proof."
        )
    if conclusion.get("mode") == "readiness":
        verdict = "READINESS ONLY"
        acceptance = "NOT EXECUTED - READINESS ONLY"
        advanced_acceptance = "NOT EXECUTED - READINESS ONLY"
    artifacts, attachment_warnings = attachment_records(run, redactor)
    warnings.extend(attachment_warnings)
    if not any(event.get("kind") == "operation" for event in events):
        warnings.append("Operation durations were not recorded. The report does not invent precise timings.")
    safe_events = [redactor.value(event) for event in events]
    start = timestamp(manifest.get("started_at"))
    finish = timestamp(conclusion.get("finished_at"))
    timeline = [
        {
            "kind": str(event.get("kind", "assertion")),
            "timestamp": str(event.get("timestamp", "Not recorded")),
            "step": redactor.text(str(event.get("step", event.get("operation", event.get("family", "Evidence"))))),
            "detail": pretty(redactor.value(event)),
        }
        for event in events
    ]
    render = Environment(
        autoescape=True, undefined=StrictUndefined, loader=PackageLoader("scripts.report_assets", package_path="")
    )
    template = render.from_string(
        files("scripts.report_assets").joinpath("validation.html").read_text(encoding="utf-8")
    )
    diagrams = (
        [
            {"name": "Trust-boundary architecture under test", "svg": architecture_svg()},
            {"name": "Request and original-file delivery sequence under test", "svg": sequence_svg()},
        ]
        if consumer
        else []
    )
    coverage = (
        [
            ("Application workflow/artifact controls", "Recorded checks; inspect per-user verdicts"),
            (
                "Hosted Session Files original output downloads",
                "user1/user2 original-byte delivery and explicit opposite-user download checks; "
                "inspect actual statuses",
            ),
            ("Hosted responses", "Response retrieval only; not all session/history operations"),
            ("Native Files listing", "Privilege-boundary check only; not content isolation"),
            (
                "Native Files metadata/content",
                "OPTIONAL ADVANCED: inspect recorded per-user verdicts"
                if expanded_run
                else "NOT TESTED by the bounded consumer proof; optional Advanced scope, "
                "not pending Consumer acceptance",
            ),
            (
                "Native container-file listing/content",
                "OPTIONAL ADVANCED: listing, metadata and original JSON/PNG content checks"
                if expanded_run
                else "NOT TESTED by the bounded consumer proof; optional Advanced scope, "
                "not pending Consumer acceptance",
            ),
            (
                "Privileged project-user isolation",
                "OUTSIDE Consumer acceptance. Earlier broad-role native-access findings "
                "remain separate; this suite makes no universal privileged-user isolation claim.",
            ),
            (
                "VM / Hyper-V isolation",
                "DOCUMENTED PLATFORM ASSURANCE, not an application test PASS. Microsoft documents "
                "managed dynamic sessions isolated by a Hyper-V boundary. Reference reviewed 2026-10-26: "
                "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/code-interpreter"
                "#sandboxed-execution-environment . Applies to the documented managed sandbox; "
                "this suite does not penetrate or independently prove VM isolation.",
            ),
            (
                "Chart visual quality",
                "OUTSIDE Consumer isolation acceptance. Aesthetics/readability are not graded. "
                "Numerical results, PNG format, original-byte integrity and authorization remain mandatory.",
            ),
        ]
        if consumer
        else [("General report", "Only recorded assertions are represented; no additional coverage is inferred")]
    )
    if consumer:

        def keys(*suffixes: str) -> set[str]:
            return {f"{actor}:{suffix}" for actor in ("user1", "user2") for suffix in suffixes}

        statuses = [
            boundary_status(cases, keys("inspect", "artifact")),
            boundary_status(cases, keys("json-bytes", "png-bytes", "foreign-json", "foreign-png")),
            boundary_status(cases, keys("response")),
            boundary_status(cases, keys("listing")),
            boundary_status(cases, keys("native-input-metadata", "native-input-content")),
            boundary_status(
                cases,
                keys(
                    "native-container-list",
                    "native-output-metadata:result.json",
                    "native-output-content:result.json",
                    "native-output-metadata:chart.png",
                    "native-output-content:chart.png",
                ),
            ),
            "OUT OF SCOPE",
            "DOCUMENTED",
            "OUT OF SCOPE",
        ]
        coverage_rows = [
            (family, status, meaning) for (family, meaning), status in zip(coverage, statuses, strict=True)
        ]
        if not expanded_run:
            coverage_rows = [
                (family, "NOT ASSESSED - OPTIONAL" if index in (4, 5) else status, meaning)
                for index, (family, status, meaning) in enumerate(coverage_rows)
            ]
    else:
        coverage_rows = [(family, "RECORDED ONLY", meaning) for family, meaning in coverage]
    safe_conclusion = redactor.value(conclusion)
    if not isinstance(safe_conclusion, dict):
        raise ReportError("Sanitized conclusion must remain an object.")
    boundary_appendix, counter_appendix = build_appendix(coverage_rows, safe_conclusion, cases, catalog)
    task_flow = build_task_flow(
        {"scenario": manifest.get("scenario"), "variant": manifest.get("variant")},
        [{"name": case["name"], "status": case["status"], "ordinal": index} for index, case in enumerate(cases, 1)],
        prefix="single-run",
        anchors={index: f"check-{case['id']}" for index, case in enumerate(cases, 1)},
    )
    bundle = {
        "manifest": redactor.value(manifest),
        "conclusion": redactor.value(conclusion),
        "suite_plan": redactor.value(plan),
        "results": [redactor.value(record) for record in results],
        "events": safe_events,
        "warnings": [redactor.text(warning) for warning in warnings],
        "verdict": verdict,
        "consumer_acceptance": acceptance,
        "acceptance_scope": (
            "Consumer-facing analysis, original-file delivery and recorded authorization checks"
            if consumer
            else "Recorded assertions only; no story-specific acceptance inferred"
        ),
        "acceptance_policy": (
            "consumer-facing-v1; native bypass is optional Advanced scope" if consumer else "recorded-evidence-only"
        ),
        "advanced_native_acceptance": advanced_acceptance,
        "mandatory_total": len(required),
        "mandatory_missing": [name for _, name in mandatory_missing],
        "alias_policy": (
            "user1=first verified UPN; user2=second; legacy directions normalized only where recorded."
            if consumer
            else "No identity or resource ownership inferred."
        ),
        "task_flow": task_flow,
    }
    downloads = [
        {
            "name": "sanitized-evidence.json",
            "media": "application/json",
            "url": "data:application/json;base64," + base64.b64encode(pretty(bundle).encode()).decode(),
        }
    ]
    if consumer:
        downloads.append(
            {
                "name": "architecture-source.json",
                "media": "application/json",
                "url": "data:application/json;base64," + base64.b64encode(diagram_sources().encode()).decode(),
            }
        )
    if consumer:
        downloads.append(
            {
                "name": "api-catalog.json",
                "media": "application/json",
                "url": "data:application/json;base64," + base64.b64encode(pretty(catalog).encode()).decode(),
            }
        )
    html = template.render(
        consumer=consumer,
        title=redactor.text(title),
        generated_at=datetime.now(UTC).isoformat(),
        verdict=verdict,
        manifest=pretty(redactor.value(manifest)),
        conclusion=pretty(redactor.value(conclusion)),
        counts=counts,
        cases=cases,
        task_flow=task_flow,
        missing=missing,
        warnings=[redactor.text(value) for value in warnings],
        events=timeline,
        artifacts=artifacts,
        downloads=downloads,
        coverage=coverage_rows,
        boundary_appendix=boundary_appendix,
        counter_appendix=counter_appendix,
        appendix_diagrams=[
            {
                "id": identifier,
                "name": name,
                "url": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode(),
            }
            for identifier, name, svg in (
                ("appendix-sequence-consumer", "Consumer analysis and original-file delivery", sequence_svg()),
                (
                    "appendix-sequence-native",
                    "Optional native bypass and privileged controls",
                    native_bypass_sequence_svg(),
                ),
            )
        ]
        if consumer
        else [],
        api_groups=[
            {"name": group, "entries": [entry for entry in catalog if entry["group"] == group]}
            for group in dict.fromkeys(entry["group"] for entry in catalog)
        ],
        acceptance=acceptance,
        advanced_acceptance=advanced_acceptance,
        mandatory_total=len(required),
        mandatory_missing=mandatory_missing,
        mandatory_completed=len(required) - len(mandatory_missing),
        diagrams=[
            {
                "name": view["name"],
                "url": "data:image/svg+xml;base64," + base64.b64encode(view["svg"].encode()).decode(),
            }
            for view in diagrams
        ],
        elapsed=f"{(finish - start).total_seconds():.2f} s" if start and finish and finish >= start else "Not recorded",
    )
    output = run / f"html-report-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output.mkdir(exist_ok=False)
    (output / "index.html").write_text(html, encoding="utf-8")
    (output / "sanitized-evidence.json").write_text(pretty(bundle), encoding="utf-8")
    if consumer:
        (output / "architecture-source.json").write_text(diagram_sources(), encoding="utf-8")
        (output / "api-catalog.json").write_text(pretty(catalog), encoding="utf-8")
    for name, content in (
        (
            ("architecture.svg", architecture_svg()),
            ("sequence.svg", sequence_svg()),
            ("native-bypass-sequence.svg", native_bypass_sequence_svg()),
        )
        if consumer
        else ()
    ):
        (output / name).write_text(content, encoding="utf-8")
    for attachment in artifacts:
        encoded = attachment["url"].split(",", 1)[1]
        (output / attachment["name"]).write_bytes(base64.b64decode(encoded, validate=True))
    if archive:
        with ZipFile(output / "report.zip", "x", ZIP_DEFLATED) as zipped:
            for path in sorted(output.iterdir()):
                if path.name != "report.zip":
                    zipped.write(path, path.name)
    return output / "index.html"


def build_parser() -> argparse.ArgumentParser:
    """Accept a saved run directory; report generation never invokes its original runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--title", default="Story 1 - Consumer Code Interpreter evidence")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Allow a truncated final JSONL record, mark incomplete and disclose missing evidence.",
    )
    parser.add_argument("--no-archive", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print the persistent report path; generation success is separate from validation success."""
    args = build_parser().parse_args(argv)
    try:
        report = generate_report(
            args.run_dir, title=args.title, archive=not args.no_archive, allow_incomplete=args.allow_incomplete
        )
    except (ReportError, ValueError, UnicodeError) as error:
        print(f"Report evidence error: {sanitize_text(str(error))}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"Report generation failed: {sanitize_text(str(error))}", file=sys.stderr)
        return 1
    print(f"HTML report: {report}\nReport generation did not execute or change validation.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
