"""Shared offline, sanitized story evidence engine and legacy Story 1 entry point.

Run with ``python -m scripts.story1_report --input CONFIG --output DIRECTORY``.
Paths in ``runs[].run_dir`` are relative to the repository working directory.
Config schema: story-report-bundle-v1 with positive integer story, or legacy
story1-report-bundle-v1 (Story 1 only); title, environment, summary_source,
runs (key, label, run_dir, scope, agent_version?, kind?), decisions (area,
status, detail, evidence_keys), chart? (run_key, path, review_path).
Explicit traceability rows and sponsor_decisions are also supported.
summary_source and approved chart imports are Story 1-only. Other stories use
explicit traceability; unknown future stories receive neutral evidence-only context.
Legacy Story 1 bundles retain cross-story references; shared-schema bundles must
explicitly declare kind cross-story-reference for a mismatched story.
No runner is imported.
Exit codes: 0 rendered recorded evidence, 1 I/O error, 2 invalid evidence/config.
Generation is not an acceptance verdict. --overwrite requires unchanged sources.
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

from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import JsonValue

from scripts.check_details import describe_check
from scripts.generate_report import Redactor, ReportError, pretty, read_bytes, read_object, read_records
from scripts.report_story_context import story3_api_catalog, story4_api_catalog
from scripts.report_task_flows import build_task_flow

SOURCE_COMMIT = "e139a6917c86fda32b8570545e8ec54eca04ccf4"
STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "SKIP"})
SAFE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
POSIX_PATH = re.compile(r"(?<![\w:])/(?:home|Users|mnt|var|etc|opt|workspace|workspaces|root)/[^\s<>\"']+")
UNC_PATH = re.compile(r"\\\\[^\s<>\"']+")
FINGERPRINT = re.compile(r"\b(?:[0-9a-f]{16}|[0-9a-f]{32}|[0-9a-f]{64})\b", re.I)
NATIVE_ID = re.compile(
    r"\b(?:session|workflow|thread|msg|user|principal|container|conversation|request|agent|conv|caresp|resp)_[\w-]+\b"
)
FORBIDDEN_KEY = re.compile(
    r"principal|identity|fingerprint|session.?id|workflow.?id|conversation|"
    r"subscription|tenant|endpoint|url|path|upn|credential|secret|token|key|"
    r"session_sha|workflow_sha|owner_sha|user_sha|actor_sha|container_sha",
    re.I,
)
# Values not in this reviewed schema never enter a downloadable bundle.
OBSERVED_KEYS = frozenset(
    {
        "environment",
        "agents",
        "supervisor-agent",
        "research-agent",
        "data-analysis-agent",
        "version",
        "agent_version",
        "status",
        "rows",
        "bytes",
        "input_sha256",
        "checks",
        "filtered_rows",
        "by_category",
        "total",
        "filtered_csv",
        "by_category_csv",
        "png",
        "png_sha256",
        "peak_rss_bytes",
        "memory_limit_bytes",
        "cgroup_memory_peak_bytes",
        "pressure_fraction",
        "end_to_end_seconds",
        "target_reached",
        "note",
        "reviewed",
        "next",
        "accepted",
        "original_run",
        "failure",
        "retry",
        "foreign_denied",
        "owner_control",
        "denied",
        "unchanged",
        "native_model_submissions",
        "evidence",
        "pending_requests",
        "calls_before_decision",
        "calls_after_decision",
        "session_round_trip",
        "wrong_session",
        "human_approval_tested",
        "foundry_enforcement_tested",
        "decision_bound",
        "pending_visible_as_text",
        "provider_execution_tested",
        "calls_without_agent_approval",
        "live_valid_token_bypass_tested",
        "call_attempts",
        "reconnections",
        "remote_effects_known",
        "phase",
        "decision",
        "file_sha256",
        "file_retained",
        "same_process",
        "research_response_observed",
        "research_user_context_present",
        "provider_responses_observed",
        "mcp_calls_reported",
        "mcp_route",
        "remote_execution_independently_verified",
        "delegated_user_authority_verified",
        "replay_allowed",
        "pending",
        "server_label",
        "tool_name",
        "fixed_query_matches",
        "role_present",
        "effective_runtime_access_tested",
        "boundary",
        "distinct_principals",
        "distinct_profile_paths",
        "metadata_matches",
        "result_returned",
        "approval_message_submitted",
        "unapproved_result_returned",
        "explicit_approval_denial",
        "authentication_is_not_approval",
        "permission_denied",
        "approval_enforcement_tested",
        "rpc_error",
        "caller_alias",
        "conversation_alias",
        "session_alias",
        "operation_alias",
        "same_authenticated_requester",
        "identity_evidence",
        "correlation_is_authorization",
        "counts",
        "user_items",
        "assistant_items",
        "other_items",
        "non_message_items",
        "start_commands",
        "inspect_commands",
        "decide_commands",
        "unclassified_text_parts",
        "observed_phases",
        "observed_decisions",
        "reported_mcp_call_counts",
        "operation_mismatches",
        "pending_approval_count",
        "observed_pending_tools",
        "decision_reference_matches_pending",
        "raw_history_exported",
        "joined_distributed_trace_verified",
        "downstream_original_user_verified",
        "history_pages",
        "independent_tool_invocation_trace_verified",
        "actors",
        "deletion",
        "forced_lifecycle_change",
        "created_at",
        "last_accessed_at",
        "stopped_at",
        "quiet_seconds",
        "metadata_reads",
        "samples",
        "process_changed",
        "home_retained",
        "before",
        "idle_before_resume",
        "after_resume",
        "same_session_binding",
        "hosted_runtime",
        "output",
        "result",
        "available",
        "supported",
        "required",
        "measured",
        "reason",
        "duration_ms",
        "count",
        "length",
        "sha256",
        "is_png",
        "size_bytes",
        "public_network_access",
        "authentication",
        "private_endpoint",
        "blob",
        "hosted",
        "storage",
        "access",
        "read",
        "write",
        "ok",
        "reached",
        "limit_bytes",
        "rss_bytes",
        "elapsed_seconds",
        "marker_retained",
        "original_bytes",
        "original_sha256",
        "first",
        "second",
        "scope",
        "mode",
        "visibility",
        "actor",
        "new_session",
        "new_interpreter",
        "matches",
        "checkpoint_retained",
        "original_interpreter_deleted",
        "kind",
        "seconds",
        "blocker",
        "analytical_submission",
        "agent",
        "selected",
        "requested",
        "decided_by",
        "controller",
        "model_tool_calls",
        "mechanism",
        "target_agent",
        "invocation",
        "statuses_seen",
        "polls",
        "elapsed_ms",
        "stage",
        "http_status",
        "code",
        "handled_by",
        "retried",
        "protocol",
        "research_toolbox_wired",
        "marker_in_sub_agent_answer",
        "marker_in_final_answer",
        "correlation_echoed",
        "correlation_header_matches",
        "call_id_present",
        "user_id_present",
        "tool_calls",
        "name",
        "succeeded",
        "agents_called",
        "code_recalled",
        "granted",
        "granting_roles",
        "outcome",
        "research_sees",
        "supervisor_user",
        "research_user",
        "listed",
        "rejected",
        "met",
        "supervisor_version",
        "mapping",
        "owner",
        "store",
        "user_isolated",
        "found",
        "created_now",
        "metadata_linked",
        "listing_before",
        "listing_after",
        "items_after_turn",
        "items_before_follow_up",
        "items_after_follow_up",
        "items",
        "user_messages",
        "assistant_messages",
        "hop_inputs",
        "protocol_replies",
        "other_assistant",
        "history",
        "before_b",
        "b_read",
        "input_appended",
        "output_appended",
        "sees_b_messages",
        "received",
        "observed",
        "enforced_by",
        "header_matched",
        "mapping_correlation_matches",
    }
)


class BundleRedactor(Redactor):
    """Extend branch privacy rules to nested local POSIX and UNC references."""

    def text(self, value: str) -> str:
        value = POSIX_PATH.sub("{local-path}", value)
        value = UNC_PATH.sub("{local-path}", value)
        for pattern in (FINGERPRINT, NATIVE_ID):

            def reference(match: re.Match[str]) -> str:
                original = match.group(0)
                if original not in self.aliases:
                    self.aliases[original] = f"{{reference-{len(self.aliases) + 1}}}"
                return self.aliases[original]

            value = pattern.sub(reference, value)
        return super().text(value)


def observed_fields(value: JsonValue, redactor: BundleRedactor) -> JsonValue:
    """Project observed values onto a recursively reviewed, identity-free schema."""
    if isinstance(value, dict):
        return {
            key: observed_fields(item, redactor)
            for key, item in value.items()
            if key in OBSERVED_KEYS and not FORBIDDEN_KEY.search(key)
        }
    if isinstance(value, list):
        return [observed_fields(item, redactor) for item in value]
    return redactor.value(value)


def _text(value: object, field: str, redactor: BundleRedactor) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"{field} must be a nonempty string.")
    return redactor.text(value)


def _objects(value: JsonValue, field: str) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ReportError(f"{field} must be an array of objects.")
    return [item for item in value if isinstance(item, dict)]


def _unique(records: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    seen: set[str] = set()
    result: list[dict[str, JsonValue]] = []
    for record in records:
        key = json.dumps(record, sort_keys=True, allow_nan=False)
        if key not in seen:
            result.append(record)
            seen.add(key)
    return result


def _load_run(
    entry: dict[str, JsonValue],
    redactor: BundleRedactor,
    story: int = 1,
    *,
    legacy_references: bool = False,
) -> dict[str, JsonValue]:
    run_path = entry.get("run_dir")
    if not isinstance(run_path, str):
        raise ReportError("run_dir must be a local directory.")
    run = Path(run_path)
    if run.is_symlink() or run.is_junction() or not run.is_dir():
        raise ReportError("Run directory is missing or is a link.")
    manifest = read_object(run / "manifest.json")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ReportError("Manifest must contain a safe, nonempty run_id.")
    if not isinstance(manifest.get("scenario"), str):
        raise ReportError("Manifest must contain a scenario.")
    scenario_story = re.match(r"story[-_](\d+)(?:[-_]|$)", str(manifest["scenario"]))
    cross_story = bool(scenario_story and int(scenario_story[1]) != story)
    if cross_story and not legacy_references and entry.get("kind") != "cross-story-reference":
        raise ReportError("Manifest story does not match bundle story; use kind cross-story-reference explicitly.")
    if not (run / "results.jsonl").is_file():
        raise ReportError("results.jsonl was not recorded.")
    results, warnings = read_records(run / "results.jsonl")
    events, event_warnings = read_records(run / "events.jsonl")
    warnings.extend(event_warnings)
    if cross_story:
        warnings.append(
            f"Cross-story reference: {manifest['scenario']} in Story {story} bundle. "
            "Original verdicts apply only to the declared reference scope, not combined story acceptance."
        )
    if not results:
        warnings.append("No recorded result checks.")
    for result in results:
        if not isinstance(result.get("step"), str) or result.get("status") not in STATUSES:
            raise ReportError("Result must contain a step and supported status.")
        if result.get("run_id") != run_id:
            raise ReportError("Result run-ID does not match manifest.")
        observed = result.get("observed")
        if result["step"] == "Native deployment binding" and isinstance(observed, dict):
            agents = observed.get("agents")
            analysis = agents.get("data-analysis-agent") if isinstance(agents, dict) else None
            actual_version = analysis.get("version") if isinstance(analysis, dict) else None
            if entry.get("agent_version") is not None and actual_version is not None:
                if str(entry["agent_version"]) != str(actual_version):
                    raise ReportError("Declared agent version does not match recorded deployment binding.")
    for event in events:
        if event.get("run_id") != run_id:
            raise ReportError("Event run-ID does not match manifest.")
        if "status" in event and event["status"] not in STATUSES:
            raise ReportError("Event contains an unsupported status.")
    conclusion = read_object(run / "conclusion.json") if (run / "conclusion.json").exists() else {}
    if conclusion and conclusion.get("run_id") != run_id:
        raise ReportError("Conclusion run-ID does not match manifest.")
    if conclusion.get("state") == "finished" and conclusion.get("validation_exit_code") == 0:
        if any(result["status"] in {"FAIL", "BLOCKED"} for result in results):
            raise ReportError("Success conclusion conflicts with recorded failure/block.")
    results = _unique(results)
    result_keys = {json.dumps(result, sort_keys=True, allow_nan=False) for result in results}
    extra_events = [
        event for event in _unique(events) if json.dumps(event, sort_keys=True, allow_nan=False) not in result_keys
    ]
    sources: list[JsonValue] = [
        {"name": name, "sha256": sha256(read_bytes(run / name)).hexdigest()}
        for name in ("manifest.json", "results.jsonl", "events.jsonl", "conclusion.json")
        if (run / name).exists()
    ]
    cases: list[JsonValue] = []
    for ordinal, result in enumerate(results, 1):
        name = str(result["step"])
        cases.append(
            {
                "ordinal": ordinal,
                "name": redactor.text(name),
                "status": result["status"],
                "timestamp": redactor.value(result.get("timestamp")),
                "expected": redactor.value(result.get("expected", "Not recorded")),
                "observed": observed_fields(result.get("observed"), redactor),
                "explanation": redactor.value(describe_check(name, basis="implementation")),
            }
        )
    provenance: dict[str, JsonValue] = {
        field: redactor.value(manifest[field])
        for field in (
            "scenario",
            "mode",
            "started_at",
            "python",
            "project_version",
            "maf_version",
            "agentserver_version",
            "variant",
            "step",
        )
        if field in manifest
    }
    timestamps = sorted(str(result["timestamp"]) for result in results if isinstance(result.get("timestamp"), str))
    return {
        "run_id": redactor.text(run_id),
        "provenance": provenance,
        "sources": sources,
        "completion": (
            "Explicit finish record present; this is not combined story acceptance."
            if conclusion.get("state") == "finished"
            else "Recorded checks, no finish-event recorded."
        ),
        "evidence_span": (
            f"{timestamps[0]} — {timestamps[-1]} (recorded evidence timestamps, not execution duration)"
            if timestamps
            else "Not recorded"
        ),
        "checks": cases,
        "counts": dict(Counter(str(result["status"]) for result in results)),
        "additional_event_count": len(extra_events),
        "additional_events": [],
        "task_flow": build_task_flow(provenance, _objects(cases, "task-flow checks"), prefix=str(entry["key"])),
        "warnings": [str(warning) for warning in warnings],
        "error": None,
    }


def _memory_rows(runs: list[dict[str, JsonValue]]) -> list[JsonValue]:
    rows: list[JsonValue] = []
    for run in runs:
        checks = run.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            observed = check.get("observed")
            if not isinstance(observed, dict):
                continue
            if not any(key in observed for key in ("peak_rss_bytes", "pressure_fraction", "memory_limit_bytes")):
                continue
            fraction = observed.get("pressure_fraction")
            pressure = (
                round(float(fraction) * 100, 4)
                if isinstance(fraction, (float, int))
                and not isinstance(fraction, bool)
                and math.isfinite(float(fraction))
                else None
            )
            rows.append(
                {
                    "run_key": run["key"],
                    "check": check["name"],
                    "status": check["status"],
                    "peak_rss_bytes": observed.get("peak_rss_bytes"),
                    "memory_limit_bytes": observed.get("memory_limit_bytes"),
                    "cgroup_memory_peak_bytes": observed.get("cgroup_memory_peak_bytes"),
                    "pressure_percent": pressure,
                    "bar_percent": max(0, min(100, pressure)) if pressure is not None else 0,
                    "end_to_end_seconds": observed.get("end_to_end_seconds"),
                }
            )
    return rows


def _native_api_catalog() -> list[JsonValue]:
    """Describe reviewed current-code call shapes, not captured network requests."""
    entries = [
        (
            "Hosted session binding",
            "Caller / runner",
            "Metadata; no model submission",
            'project.agents.create_session("data-analysis-agent", '
            "version_indicator=VersionRefIndicator(agent_version=version))",
            "scripts.run_scenario.story1_native.create_actor",
            "Explicit version and caller-owned session; not an agent deployment.",
        ),
        (
            "Hosted conversation binding",
            "Caller / runner",
            "State creation; no model submission",
            "hosted.conversations.create()",
            "scripts.run_scenario.story1_native.create_actor",
            "Keep the returned conversation bound to the same authorized Hosted session.",
        ),
        (
            "Native application command",
            "Caller / runner",
            "Hosted turn; analyze alone triggers native analysis",
            'hosted.responses.with_raw_response.create(input="POC_STORY1_NATIVE " + json.dumps(command), '
            'conversation=own_conversation, extra_body={"agent_session_id": own_session}, stream=False)',
            "scripts.run_scenario.story1_native.send",
            "poc-story1-native-v1 operations prepare / inspect / observe do not submit native analysis. "
            "Caller, Hosted session and conversation are checked before privileged backend work.",
        ),
        (
            "Native synthetic input upload",
            "Authorized backend only",
            "File upload; no model submission",
            'native.files.create(purpose="assistants", file=(filename, verified_bytes, "text/csv"))',
            "app.services.story1_native.NativeWorkflow.analyze",
            "Only the fixed synthetic fixture is uploaded after owner/session and byte-integrity checks.",
        ),
        (
            "Native Code Interpreter analysis",
            "Authorized backend only",
            "Model call with one interpreter tool",
            "native.responses.create(model=model, store=False, stream=False, max_output_tokens=8000, "
            'max_tool_calls=1, tools=[{"type":"code_interpreter","container":'
            '{"type":"auto","file_ids":[uploaded_file]}}], input=ANALYSIS_PROMPT)',
            "app.services.story1_native.NativeWorkflow.analyze",
            "Model-written analysis executes in managed Code Interpreter, not the Hosted Python process. "
            "Ambiguous submissions are not automatically replayed.",
        ),
        (
            "Native original output retrieval",
            "Authorized backend only",
            "Byte retrieval; no model submission",
            "native.containers.files.content.with_streaming_response.retrieve("
            "file_id=cited_file, container_id=cited_container)",
            "app.services.story1_native.original_outputs",
            "Retrieve only cited original outputs within byte ceilings, then stage authorized session files. "
            "This backend privilege is not granted to the calling user by this design.",
        ),
        (
            "Original user delivery",
            "Authorized owning caller / runner",
            "Session file download; no model submission",
            'project.agents.download_session_file("data-analysis-agent", own_session, path=approved_relative_path)',
            "scripts.run_scenario.story1_native.download_originals",
            "SessionFiles-mediated delivery checks original length/hash. Foreign callers and clean-session access "
            "are separate denial controls, not evidence of privileged native-project-file confidentiality.",
        ),
        (
            "Hosted lifecycle metadata",
            "Caller / runner",
            "Read-only metadata; no model submission",
            'project.agents.get_session("data-analysis-agent", own_session)',
            "scripts.run_scenario.story1_native.NativeValidation.lifecycle_snapshot",
            "Read status/version/server lifecycle fields. Reading metadata does not force stop/resume or "
            "independently prove native interpreter RAM survival.",
        ),
        (
            "Customer-owned Blob checkpoint",
            "Authenticated validation runner",
            "Storage I/O; no model submission",
            "Authenticated PUT(checkpoint_bytes); GET(same_checkpoint); verify original bytes and binding",
            "scripts.run_scenario.story1_native.NativeValidation.recovery",
            "Separate runner-driven persistence, followed by recomputation in a new authorized binding. "
            "Authenticated public Blob access does not establish private networking or full-session snapshots.",
        ),
    ]
    return [
        {
            "operation": operation,
            "caller": caller,
            "activity": activity,
            "shape": shape,
            "source": source,
            "scope": scope,
        }
        for operation, caller, activity, shape, source, scope in entries
    ]


def _lifecycle_timeline(runs: list[dict[str, JsonValue]]) -> list[JsonValue]:
    """Project explicit final lifecycle bindings without inferring transition times or causes."""

    def timeline(run: dict[str, JsonValue]) -> dict[str, JsonValue]:
        checks = run.get("checks")
        by_name = (
            {str(check.get("name")): check for check in checks if isinstance(check, dict)}
            if isinstance(checks, list)
            else {}
        )
        notes: list[JsonValue] = []
        rows: list[JsonValue] = []
        version = run.get("agent_version")

        def observation(name: str) -> dict[str, JsonValue]:
            check = by_name.get(name)
            if not isinstance(check, dict) or not isinstance(check.get("observed"), dict):
                notes.append(f"{name}: not recorded.")
                return {}
            observed = check.get("observed")
            if check.get("status") != "PASS":
                notes.append(f"{name}: original check did not pass.")
            return observed if isinstance(observed, dict) else {}

        def add_row(phase: str, snapshot: JsonValue, quiet: JsonValue = None) -> bool:
            if (
                not isinstance(snapshot, dict)
                or not isinstance(snapshot.get("status"), str)
                or snapshot.get("status") not in ("active", "idle")
            ):
                notes.append(f"{phase}: supported platform state not recorded.")
                return False
            if version not in (None, "Not specified") and snapshot.get("agent_version") != version:
                notes.append(f"{phase}: platform version missing or differs from declared binding.")
            if quiet is not None and (
                not isinstance(quiet, (int, float))
                or isinstance(quiet, bool)
                or not math.isfinite(float(quiet))
                or quiet < 0
            ):
                notes.append(f"{phase}: invalid runner quiet interval omitted.")
                quiet = None
            server: dict[str, JsonValue] = {}
            for field in ("created_at", "last_accessed_at", "stopped_at"):
                value = snapshot.get(field)
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                    or value < 0
                ):
                    notes.append(f"{phase}: invalid server metadata omitted.")
                    value = None
                server[field] = value
            if server["created_at"] is None:
                notes.append(f"{phase}: server creation timestamp not recorded.")
            rows.append(
                {
                    "phase": phase,
                    "status": snapshot["status"],
                    "quiet_seconds": quiet,
                    "agent_version": snapshot.get("agent_version"),
                    "server_metadata": server,
                }
            )
            return True

        before = observation("Platform state before quiet interval")
        resume = observation("Platform state before resume")
        witness = observation("Platform idle boundary witness")
        markers = observation("Optional Hosted marker observation")
        add_row("Before quiet interval", before or witness.get("before"))
        samples = resume.get("samples")
        if not isinstance(samples, list) or not samples:
            notes.append("Metadata sample sequence not recorded.")
            samples = []
        sample_states: list[str] = []
        previous_quiet = -1.0
        for ordinal, sample in enumerate(samples, 1):
            if not isinstance(sample, dict):
                notes.append("Malformed metadata sample omitted.")
                continue
            quiet = sample.get("quiet_seconds")
            if quiet is None:
                notes.append("Metadata sample has no runner quiet interval.")
            if add_row(f"Metadata sample {ordinal}", sample, quiet):
                sample_states.append(str(sample["status"]))
                if isinstance(quiet, (float, int)) and not isinstance(quiet, bool) and math.isfinite(float(quiet)):
                    if quiet < previous_quiet:
                        notes.append("Recorded runner sample intervals are not monotonic.")
                    previous_quiet = float(quiet)
        if "active" not in sample_states or "idle" not in sample_states:
            notes.append("The recorded samples do not include both active and idle states.")
        if not samples:
            add_row("Idle witness (no sample sequence)", witness.get("idle_before_resume"))
        add_row("After resume", witness.get("after_resume"))
        for field, expected in (("before", "active"), ("idle_before_resume", "idle"), ("after_resume", "active")):
            snapshot = witness.get(field)
            if not isinstance(snapshot, dict) or snapshot.get("status") != expected:
                notes.append(f"{field}: expected witnessed state not recorded.")
        if witness.get("same_session_binding") is not True:
            notes.append("Same-session binding witness missing or not satisfied.")
        if witness.get("forced_lifecycle_change") is not False:
            notes.append("No-forced-lifecycle-change witness missing or not satisfied.")
        marker_values: dict[str, JsonValue] = {}
        for field in ("process_changed", "home_retained"):
            value = markers.get(field)
            marker_values[field] = value if isinstance(value, bool) else None
            if not isinstance(value, bool):
                notes.append(f"{field}: marker observation not recorded as a boolean.")
        return {
            "run_key": run["key"],
            "status": "PARTIAL" if notes or run.get("error") else "RECORDED",
            "rows": rows,
            "markers": marker_values,
            "notes": notes,
        }

    return [timeline(run) for run in runs if run.get("kind") == "lifecycle"]


def _summary(config: dict[str, JsonValue], keys: set[str], redactor: BundleRedactor) -> tuple[list[JsonValue], str]:
    path = config.get("summary_source")
    if path is None:
        return [], ""
    if not isinstance(path, str):
        raise ReportError("summary_source must be a local Markdown filename.")
    data = read_bytes(Path(path))
    text = data.decode("utf-8")
    match = re.search(r"^## Summary\s*$([\s\S]*?)(?=^## |\Z)", text, re.M)
    if match is None:
        raise ReportError("summary_source has no current Summary section.")
    table = [line.strip() for line in match[1].splitlines() if line.strip().startswith("|")]
    if len(table) != 15:
        raise ReportError("Current Summary must contain its header, separator and 13 rows.")
    bindings = {
        "1": [],
        "2": ["readiness"],
        "3": ["analysis", "baseline-capacity", "pressure"],
        "4": ["analysis", "pressure"],
        "4a": ["analysis", "chart-review", "pressure"],
        "4b": ["analysis", "isolation", "supervisor"],
        "5": ["baseline-capacity", "pressure"],
        "6": ["pressure", "hosted-original", "hosted-short", "hosted-five-minute", "hosted"],
        "7": ["pressure", "blob", "hosted", "supervisor"],
        "8": ["isolation"],
        "8a": [],
        "8b": ["blob", "hosted"],
        "8c": ["blob"],
    }
    rows: list[JsonValue] = []
    numbers: list[str] = []
    for line in table[2:]:
        values = [value.strip() for value in line.strip("|").split("|")]
        if len(values) != 6 or values[0] not in bindings:
            raise ReportError("Current Summary row schema is unsupported.")
        number, requirement, status, works, limitations, next_step = values
        numbers.append(number)
        rows.append(
            {
                "requirement": redactor.text(f"{number}. {requirement}"),
                "status": redactor.text(status),
                "summary": redactor.text(works),
                "limitations": redactor.text(limitations),
                "next_step": redactor.text(next_step),
                "run_keys": [key for key in bindings[number] if key in keys],
            }
        )
    if numbers != list(bindings):
        raise ReportError("Current Summary rows are missing, duplicated, or reordered.")
    return rows, sha256(data).hexdigest()


def _chart(config: dict[str, JsonValue], redactor: BundleRedactor) -> dict[str, JsonValue] | None:
    chart = config.get("chart")
    if chart is None:
        return None
    if not isinstance(chart, dict):
        raise ReportError("chart must be an object.")
    entry = next((entry for entry in _objects(config["runs"], "runs") if entry["key"] == chart.get("run_key")), None)
    if entry is None:
        raise ReportError("Chart must reference a bound run.")
    root = Path(str(entry["run_dir"])).resolve()
    paths: list[Path] = []
    for field in ("path", "review_path"):
        relative = chart.get(field)
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ReportError("Chart paths must be relative to the bound run.")
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ReportError("Chart path escapes its bound run.")
        paths.append(path)
    image = read_bytes(paths[0], limit=1024 * 1024)
    receipt = read_object(paths[1])
    digest = sha256(image).hexdigest()
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or receipt.get("accepted") is not True:
        raise ReportError("Chart requires a PNG original and explicit accepted review.")
    if receipt.get("png_sha256") != digest:
        raise ReportError("Approved chart SHA256 does not match the retained original.")
    categories = receipt.get("categories")
    cents = receipt.get("revenue_cents")
    if (
        not isinstance(categories, list)
        or not categories
        or not all(isinstance(item, str) for item in categories)
        or not isinstance(cents, list)
        or len(cents) != len(categories)
        or not all(type(item) is int for item in cents)
    ):
        raise ReportError("Chart review must bind category labels and integer revenue_cents.")
    return {
        "run_key": chart["run_key"],
        "sha256": digest,
        "review_sha256": sha256(read_bytes(paths[1])).hexdigest(),
        "review_source": redactor.value(receipt.get("review_source", "Explicit user approval")),
        "values": [
            {"category": redactor.value(category), "revenue_cents": value}
            for category, value in zip(categories, cents, strict=True)
        ],
        "data_uri": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
        "download": "evidence/approved-chart.png",
    }


def build_bundle(config: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Validate bundle bindings and preserve original check verdicts without rollup success."""
    schema = config.get("schema", "story1-report-bundle-v1")
    if not isinstance(schema, str) or schema not in {"story1-report-bundle-v1", "story-report-bundle-v1"}:
        raise ReportError("Unsupported report bundle schema.")
    story = config.get("story", 1) if schema == "story1-report-bundle-v1" else config.get("story")
    if type(story) is not int or story < 1:
        raise ReportError("story must be a positive integer.")
    if schema == "story1-report-bundle-v1" and story != 1:
        raise ReportError("Legacy Story 1 schema cannot bind another story.")
    if story != 1 and ("summary_source" in config or "chart" in config):
        raise ReportError("summary_source and chart are Story 1-only; supply explicit traceability.")
    redactor = BundleRedactor()
    entries = _objects(config.get("runs"), "runs")
    if not entries:
        raise ReportError("runs must not be empty.")
    seen: set[str] = set()
    seen_paths: set[Path] = set()
    seen_run_ids: set[str] = set()
    runs: list[dict[str, JsonValue]] = []
    for entry in entries:
        key = entry.get("key")
        if not isinstance(key, str) or not SAFE_KEY.fullmatch(key) or key in seen:
            raise ReportError("Each run key must be a unique lowercase slug.")
        seen.add(key)
        run_path = Path(str(entry.get("run_dir", ""))).resolve()
        if run_path in seen_paths:
            raise ReportError("Duplicate run_dir binding would count the same evidence twice.")
        seen_paths.add(run_path)
        binding: dict[str, JsonValue] = {
            "key": key,
            "label": _text(entry.get("label"), "run label", redactor),
            "scope": _text(entry.get("scope"), "run scope", redactor),
            "agent_version": redactor.value(entry.get("agent_version", "Not specified")),
            "kind": redactor.value(entry.get("kind", "Recorded evidence")),
            "download": f"evidence/{key}.json",
        }
        try:
            binding.update(_load_run(entry, redactor, story, legacy_references=schema == "story1-report-bundle-v1"))
        except (OSError, ValueError, UnicodeError) as error:
            binding.update(
                {
                    "error": redactor.text(str(error))
                    if isinstance(error, ReportError)
                    else "Malformed or unreadable evidence.",
                    "checks": [],
                    "counts": {},
                    "sources": [],
                    "warnings": [],
                    "completion": "INVALID EVIDENCE — not evaluated",
                    "run_id": "Unavailable",
                    "provenance": {},
                    "evidence_span": "Unavailable",
                    "additional_event_count": 0,
                    "additional_events": [],
                    "task_flow": {
                        "available": False,
                        "notice": "Invalid evidence: no task flow or check outcomes are inferred.",
                    },
                }
            )
        if binding["error"] is None:
            recorded_id = str(binding["run_id"])
            if recorded_id in seen_run_ids:
                raise ReportError("Duplicate run_id binding would count the same recorded run twice.")
            seen_run_ids.add(recorded_id)
        runs.append(binding)
    decision_entries = config.get("sponsor_decisions", [])
    if "decisions" in config:
        decision_entries = [
            {
                "topic": item.get("area"),
                "decision": item.get("status"),
                "rationale": item.get("detail"),
                "run_keys": item.get("evidence_keys", []),
            }
            for item in _objects(config["decisions"], "decisions")
        ]
        for item in _objects(config["decisions"], "decisions"):
            refs = item.get("evidence_keys", [])
            if not isinstance(refs, list) or any(not isinstance(key, str) or key not in seen for key in refs):
                raise ReportError("Decision evidence_keys must reference bound runs.")
    decisions: list[JsonValue] = [
        {
            **{
                field: _text(item.get(field), f"sponsor_decisions.{field}", redactor)
                for field in ("topic", "decision", "rationale")
            },
            "run_keys": item.get("run_keys", []),
        }
        for item in _objects(decision_entries, "sponsor_decisions")
    ]
    for decision in decisions:
        if isinstance(decision, dict):
            refs = decision["run_keys"]
            if not isinstance(refs, list) or any(not isinstance(key, str) or key not in seen for key in refs):
                raise ReportError("Sponsor decision run_keys must reference bound runs.")
    traceability: list[JsonValue] = []
    for item in _objects(config.get("traceability", []), "traceability"):
        keys = item.get("run_keys", [])
        if not isinstance(keys, list) or any(not isinstance(key, str) or key not in seen for key in keys):
            raise ReportError("Traceability run_keys must reference existing bindings.")
        traceability.append(
            {
                **{
                    field: _text(
                        item.get(field, item.get("works") if field == "summary" else None),
                        f"traceability.{field}",
                        redactor,
                    )
                    for field in ("requirement", "status", "summary")
                },
                "run_keys": keys,
                **{
                    field: _text(item[field], f"traceability.{field}", redactor)
                    for field in ("id", "limitations", "next_step")
                    if field in item
                },
            }
        )
    summary, summary_hash = _summary(config, seen, redactor)
    return {
        "story": story,
        "title": _text(config.get("title", f"Story {story} evidence report"), "title", redactor),
        "environment_label": _text(
            config.get("environment", config.get("environment_label", "Local evidence")), "environment", redactor
        ),
        "report_source_commit": SOURCE_COMMIT,
        "generated_at": datetime.now(UTC).isoformat(),
        "acceptance": "RECORDED EVIDENCE — NOT A COMBINED COMPLETION VERDICT",
        "runs": [run for run in runs],
        "sponsor_decisions": decisions,
        "traceability": traceability or summary,
        "summary_sha256": summary_hash,
        "chart": _chart(config, redactor),
        "memory": _memory_rows(runs) if story == 1 else [],
        "native_api_catalog": (
            _native_api_catalog()
            if story == 1
            else story3_api_catalog()
            if story == 3
            else story4_api_catalog()
            if story == 4
            else []
        ),
        "lifecycle_timeline": _lifecycle_timeline(runs) if story == 1 else [],
        "errors": [run["key"] for run in runs if run["error"]],
    }


def generate_bundle(input_path: Path, output: Path, *, overwrite: bool = False) -> Path:
    """Write only sanitized local exports; refuse changed-source or unrelated overwrites."""
    config = read_object(input_path)
    bundle = build_bundle(config)
    output = output.absolute()
    if output.is_symlink() or output.is_junction():
        raise ReportError("Output must not be a link.")
    artifact_root = (Path.cwd() / "artifacts").resolve()
    if not output.resolve().is_relative_to(artifact_root):
        raise ReportError("Output must be inside the local ignored artifacts directory.")
    for entry in _objects(config.get("runs"), "runs"):
        source = Path(str(entry.get("run_dir", ""))).resolve()
        if output.resolve() == source or output.resolve().is_relative_to(source):
            raise ReportError("Output must not overwrite or be nested inside source evidence.")
    signature = sha256(
        pretty(
            {
                "config_sha256": sha256(read_bytes(input_path)).hexdigest(),
                "runs": bundle["runs"],
                "summary_sha256": bundle["summary_sha256"],
                "chart": bundle["chart"],
            }
        ).encode()
    ).hexdigest()
    bundle["source_fingerprint"] = signature
    if output.exists():
        prior = output / "sanitized-evidence.json"
        if not overwrite or not prior.is_file() or read_object(prior).get("source_fingerprint") != signature:
            raise ReportError("Output exists; --overwrite requires unchanged input and evidence.")
        expected = {"index.html", "sanitized-evidence.json", "evidence"}
        if any(path.name not in expected or path.is_symlink() or path.is_junction() for path in output.iterdir()):
            raise ReportError("Output contains unrelated files or links.")
        evidence = output / "evidence"
        if evidence.exists():
            expected_evidence = {f"{entry['key']}.json" for entry in _objects(config.get("runs"), "runs")}
            if bundle["chart"] is not None:
                expected_evidence.add("approved-chart.png")
            if any(
                path.name not in expected_evidence or not path.is_file() or path.is_symlink() or path.is_junction()
                for path in evidence.iterdir()
            ):
                raise ReportError("Output evidence directory contains unrelated files or links.")
    environment = Environment(
        autoescape=True, undefined=StrictUndefined, loader=PackageLoader("scripts.report_assets", package_path="")
    )
    environment.filters["json_pretty"] = pretty
    template = environment.from_string(
        files("scripts.report_assets").joinpath("story1.html").read_text(encoding="utf-8")
    )
    html = template.render(**bundle)
    (output / "evidence").mkdir(parents=True, exist_ok=True)
    chart = bundle["chart"]
    if isinstance(chart, dict):
        (output / str(chart["download"])).write_bytes(base64.b64decode(str(chart["data_uri"]).split(",", 1)[1]))
    for run in bundle["runs"] if isinstance(bundle["runs"], list) else []:
        if isinstance(run, dict):
            (output / str(run["download"])).write_text(pretty(run), encoding="utf-8")
    (output / "sanitized-evidence.json").write_text(pretty(bundle), encoding="utf-8")
    path = output / "index.html"
    path.write_text(html, encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    """Build the offline-only bundle command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Render saved evidence without invoking models, runners, or the network."""
    args = build_parser().parse_args(argv)
    try:
        path = generate_bundle(args.input, args.output, overwrite=args.overwrite)
        print(path)
        return 2 if read_object(path.parent / "sanitized-evidence.json")["errors"] else 0
    except (ValueError, UnicodeError) as error:
        print(f"Report input error: {BundleRedactor().text(str(error))}", file=sys.stderr)
        return 2
    except OSError:
        print("Report I/O error; verify local input/output permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
