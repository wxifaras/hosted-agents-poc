"""Bounded native Story 1 validation on the existing Data Analysis hosted agent.

Preview is local and credential-free. Explicit run mode may incur service charges;
it never deploys, grants roles, retries, or deletes service resources.
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import UUID

from app.services.story1_native import AnalysisResult, NativeReply
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import VersionRefIndicator
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError, HttpResponseError
from azure.identity.aio import AzureCliCredential
from openai import APIStatusError, AsyncOpenAI, OpenAIError
from pydantic import JsonValue

from scripts.common.reporting import (
    REPORT_FILE,
    MarkdownReport,
    Status,
    StepSpec,
    ValidationRun,
    add_output_arguments,
    index_entry,
    update_index,
)
from scripts.common.synthetic_data import (
    COLUMNS,
    DATASET_SIZES,
    Dataset,
    build_dataset,
    expected_aggregates,
    expected_filtered_ids,
    generate_rows,
)
from scripts.deploy.grant_local_rbac import CliError, native_json, object_value
from scripts.run_scenario.story1_data_analysis import (
    Blocked,
    aggregates_match,
    check_by_category_csv,
    check_filtered_csv,
    png_dimensions,
    snapshot_agent,
    total_of,
)

NATIVE_BASELINE = "native-agents-baseline.json"
PRESSURE_SIZES = (250_000, 500_000, 1_000_000)
OUTPUT_NAMES = ("result.json", "filtered.csv", "by_category.csv", "revenue_by_category.png")
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
COMMAND_PREFIX = "POC_STORY1_NATIVE "
PROTOCOL = "poc-story1-native-v1"
SPECS = {
    1: StepSpec(1, 1, "code-interpreter"),
    2: StepSpec(1, 2, "analysis-and-outputs"),
    3: StepSpec(1, 3, "dataset-sizes"),
    4: StepSpec(1, 4, "sandbox-boundaries"),
    5: StepSpec(1, 5, "state-continuity"),
    6: StepSpec(4, 6, "data-analysis"),
}
Profile = Literal["baseline", "pressure"]


class NativeFailure(RuntimeError):
    """A hosted protocol failure, distinct from a successful analytical answer."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Native hosted operation failed ({code}); no retry was attempted.")
        self.code = code


@dataclass
class NativeActor:
    """One explicit native user/session/conversation binding, kept out of reports."""

    project: AIProjectClient
    hosted: AsyncOpenAI
    session: str
    conversation: str


async def create_actor(
    stack: AsyncExitStack, endpoint: str, credential: AsyncTokenCredential, version: str, timeout: float
) -> NativeActor:
    """Create one bound Hosted session using the selected user's credential."""
    project = await stack.enter_async_context(
        AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True, retry_total=0)
    )
    hosted = await stack.enter_async_context(
        project.get_openai_client(agent_name="data-analysis-agent", max_retries=0, timeout=timeout)
    )
    session = await project.agents.create_session(
        "data-analysis-agent", version_indicator=VersionRefIndicator(agent_version=version)
    )
    conversation = await hosted.conversations.create()
    if not session.agent_session_id or not conversation.id:
        raise ValueError("The platform did not create an explicit native session/conversation binding.")
    return NativeActor(project, hosted, session.agent_session_id, conversation.id)


async def download_originals(actor: NativeActor, receipts: Sequence[Mapping[str, object]]) -> dict[str, bytes]:
    """Download only approved session-scoped originals and verify each receipt."""
    files: dict[str, bytes] = {}
    combined = 0
    for receipt in receipts:
        name, path = receipt.get("filename"), receipt.get("path")
        length, expected_hash = receipt.get("size_bytes"), receipt.get("sha256")
        if (
            not isinstance(name, str)
            or name not in OUTPUT_NAMES
            or name in files
            or not isinstance(path, str)
            or path.startswith(("/", "\\"))
            or "\\" in path
            or ":" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or path.split("/")[-1] != name
            or type(length) is not int
            or not 0 < length <= MAX_OUTPUT_BYTES
            or not isinstance(expected_hash, str)
        ):
            raise ValueError("Invalid native output receipt; no unrelated session file was requested.")
        stream = await actor.project.agents.download_session_file("data-analysis-agent", actor.session, path=path)
        data = bytearray()
        async for chunk in stream:
            data.extend(chunk)
            if combined + len(data) > MAX_OUTPUT_BYTES or len(data) > length:
                raise ValueError("Native session-file bytes exceed the approved receipt/envelope.")
        body = bytes(data)
        if len(body) != length or hashlib.sha256(body).hexdigest() != expected_hash:
            raise ValueError("Native session-file original bytes do not match the backend receipt.")
        combined += len(body)
        files[name] = body
    if set(files) != set(OUTPUT_NAMES):
        raise ValueError("The native operation did not deliver the full approved output inventory.")
    return files


async def send(actor: NativeActor, command: Mapping[str, object]) -> dict[str, object]:
    """Send only the fixed application protocol through an explicitly bound session."""
    raw = await actor.hosted.responses.with_raw_response.create(
        input=COMMAND_PREFIX + json.dumps(dict(command)),
        conversation=actor.conversation,
        extra_body={"agent_session_id": actor.session},
        stream=False,
    )
    response = raw.parse()
    if response.status != "completed" or response.error:
        raise NativeFailure(response.error.code if response.error else "incomplete_response")
    reply = NativeReply.model_validate_json(response.output_text)
    if reply.status in ("failed", "running"):
        raise NativeFailure(f"native_{reply.status}")
    return reply.model_dump(mode="json")


async def snapshots(project: AIProjectClient) -> tuple[dict[str, JsonValue], str]:
    """Pin the same three deployed identities/versions without exporting raw identities."""
    result: dict[str, JsonValue] = {}
    native_version = ""
    for name in ("supervisor-agent", "research-agent", "data-analysis-agent"):
        record = await project.agents.get(name)
        snapshot = snapshot_agent(record.as_dict())
        try:
            UUID(snapshot.principal_id)
        except ValueError as error:
            raise Blocked("Each existing agent must have a known deployed identity.") from error
        if snapshot.status != "active" or not snapshot.latest_routing:
            raise Blocked("All three existing agents must be active with known identities and @latest routing.")
        if name == "data-analysis-agent":
            versions = record.as_dict().get("versions", {})
            latest = versions.get("latest", {}) if isinstance(versions, dict) else {}
            definition = latest.get("definition", {}) if isinstance(latest, dict) else {}
            environment = definition.get("environment_variables", {}) if isinstance(definition, dict) else {}
            if (
                not isinstance(environment, dict)
                or str(environment.get("STORY1_NATIVE_ENABLED", "")).casefold() != "true"
            ):
                raise Blocked("Deploy the approved native version of the existing Data Analysis agent first.")
            if snapshot.toolbox_endpoint:
                raise Blocked("Native Data Analysis must not retain an active toolbox endpoint.")
            native_version = snapshot.version
        result[name] = {
            "version": snapshot.version,
            "principal_sha256": hashlib.sha256(snapshot.principal_id.encode()).hexdigest(),
            "status": snapshot.status,
        }
    return result, native_version


def baseline_guard(path: Path, environment: str, current: Mapping[str, JsonValue]) -> None:
    """A toolbox or drifted baseline cannot certify the native path."""
    if not path.is_file():
        raise Blocked("Native Step 1 baseline is missing; run the existing Step 1 task in run mode first.")
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("execution") != PROTOCOL
        or value.get("environment") != environment
        or value.get("agents") != current
    ):
        raise Blocked("Native baseline differs from the selected environment/deployed versions; refresh Step 1.")


def result_verdict(statuses: Sequence[Status]) -> str:
    """Never turn a failed, skipped, or blocked observation into Met."""
    if "FAIL" in statuses:
        return "Not met"
    if "BLOCKED" in statuses or "SKIP" in statuses:
        return "Not evaluated"
    return "Met" if statuses else "Not evaluated"


class NativeValidation:
    """One bounded run with existing reports and no remote retries or deletion."""

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        self.args = args
        self.run = run
        self.report = report
        self.started = perf_counter()
        self.endpoint = ""
        self.version = ""
        self.before: dict[str, JsonValue] = {}

    def record(self, name: str, status: Status, expected: str, observed: object) -> None:
        """Persist the assertion, not a successful transport proxy."""
        self.run.result(name, status, expected=expected, observed=observed)
        self.report.table(["Check", "Status", "Expected", "Observed"], [[name, status, expected, observed]])

    def remaining(self) -> float:
        """A global deadline bounds the next request, not just loop iteration count."""
        left = float(self.args.max_seconds) - (perf_counter() - self.started)
        if left <= 0:
            raise TimeoutError("The approved native validation wall-clock budget expired.")
        return min(left, float(self.args.timeout))

    async def command(self, actor: NativeActor, value: Mapping[str, object]) -> dict[str, object]:
        """Timeout is ambiguous remote state and is never retried."""
        async with asyncio.timeout(self.remaining()):
            return await send(actor, value)

    async def analyze(
        self, actor: NativeActor, dataset: Dataset, *, save_to: Path
    ) -> tuple[dict[str, object], dict[str, bytes], dict[str, object]]:
        """One preparation plus one analysis; original bytes remain the source of truth."""
        prepared = await self.command(
            actor, {"operation": "prepare", "rows": dataset.rows, "profile": self.args.profile}
        )
        workflow = prepared.get("workflow_id")
        input_metadata = prepared.get("input")
        if (
            not isinstance(workflow, str)
            or prepared.get("status") != "prepared"
            or not isinstance(input_metadata, dict)
            or input_metadata.get("sha256") != dataset.sha256
        ):
            raise ValueError("Native prepared fixture does not match the independent seeded fixture.")
        reply = await self.command(actor, {"operation": "analyze", "workflow_id": workflow})
        receipts = reply.get("artifacts")
        if not isinstance(receipts, list) or not all(isinstance(item, dict) for item in receipts):
            raise ValueError("Native response omitted its original-file receipts.")
        async with asyncio.timeout(self.remaining()):
            files = await download_originals(actor, receipts)
        checks = verify_outputs(dataset, files)
        self.record(
            f"{dataset.rows:,} rows: analytical originals",
            "PASS" if all(checks.values()) else "FAIL",
            "Input hash, exact rows/cells/aggregates, original CSVs, and a valid PNG match",
            {"rows": dataset.rows, "bytes": dataset.size_bytes, "input_sha256": dataset.sha256, "checks": checks},
        )
        save_to.mkdir(parents=True, exist_ok=False)
        for name, body in files.items():
            (save_to / name).write_bytes(body)
        result: object = json.loads(files["result.json"])
        if not isinstance(result, dict):
            raise ValueError("Native analytical result is not an object.")
        if not all(checks.values()):
            raise ValueError("Native analytical validation failed; the ladder stops without retry.")
        return {**prepared, "container_fingerprint": reply.get("container_fingerprint")}, files, result

    async def execute(self) -> None:
        """Resolve configuration only after the caller selected explicit run mode."""
        context = object_value(
            native_json("azd", ["env", "get-values", "--environment", self.args.environment, "--output", "json"])
        )
        endpoint = context.get("FOUNDRY_PROJECT_ENDPOINT")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise Blocked("The selected environment must contain an HTTPS FOUNDRY_PROJECT_ENDPOINT.")
        self.endpoint = endpoint
        self.report.paragraph(
            "Native-only fixed synthetic validation. No deployment, grants, retries, or service deletion. "
            "Toolbox reports are historical, not evidence for this execution path."
        )
        async with AsyncExitStack() as stack:
            credential = await stack.enter_async_context(AzureCliCredential(process_timeout=60))
            project = await stack.enter_async_context(
                AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True, retry_total=0)
            )
            self.before, self.version = await snapshots(project)
            baseline = self.args.output_dir.parent / "story-1" / NATIVE_BASELINE
            if self.args.step != 1:
                baseline_guard(baseline, self.args.environment, self.before)
            self.record(
                "Native deployment binding",
                "PASS",
                "Same three active agents; Data Analysis is native-only",
                {"environment": self.args.environment, "agents": self.before},
            )
            if self.args.step == 4:
                await self.isolation(stack, context)
            elif self.args.step == 6:
                await self.supervisor(project)
            else:
                actor = await create_actor(stack, endpoint, credential, self.version, self.remaining())
                if self.args.step == 3:
                    await self.capacity(actor)
                elif self.args.step == 5:
                    await self.recovery(actor, credential, context, stack)
                else:
                    dataset = build_dataset(1000)
                    _, files, _ = await self.analyze(actor, dataset, save_to=self.run.path / "outputs")
                    if self.args.step == 2:
                        self.chart_review(dataset, files)
            after, _ = await snapshots(project)
            self.record(
                "Deployed versions unchanged",
                "PASS" if after == self.before else "FAIL",
                "No agent identity/version changes during validation",
                {"unchanged": after == self.before},
            )
            if self.args.step == 1 and result_verdict(self.run.statuses) == "Met":
                baseline.write_text(
                    json.dumps(
                        {
                            "execution": PROTOCOL,
                            "environment": self.args.environment,
                            "agents": self.before,
                            "run_id": self.run.run_id,
                            "recorded_at": datetime.now(UTC).isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

    def chart_review(self, dataset: Dataset, files: Mapping[str, bytes]) -> None:
        """Keep valid image bytes and a reviewed analytical chart as separate claims."""
        png = files["revenue_by_category.png"]
        expected = expected_aggregates(generate_rows(dataset.rows))
        hashes = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
        (self.run.path / "analysis-context.json").write_text(
            json.dumps(
                {
                    "protocol": PROTOCOL,
                    "rows": dataset.rows,
                    "environment": self.args.environment,
                    "agents": self.before,
                    "original_sha256": hashes,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        template = {
            "accepted": False,
            "png_sha256": hashes["revenue_by_category.png"],
            "categories": sorted(expected),
            "revenue_cents": [expected[name].revenue_cents for name in sorted(expected)],
        }
        (self.run.path / "chart-review-template.json").write_text(json.dumps(template, indent=2), encoding="utf-8")
        receipt: object = None
        if self.args.chart_review:
            receipt = json.loads(self.args.chart_review.read_text(encoding="utf-8"))
        passed = validate_chart_review(dataset, png, receipt)
        self.record(
            "Chart meaning",
            "PASS" if passed else "BLOCKED",
            "Human review of original PNG labels/values, bound to its SHA-256",
            {
                "png_sha256": hashes["revenue_by_category.png"],
                "reviewed": passed,
                "next": "Review the original PNG; use --review-run and --chart-review offline, without new inference.",
            },
        )

    async def capacity(self, actor: NativeActor) -> None:
        """Stop the finite ladder at any failure, not only a guessed resource category."""
        reached = False
        for rows in profile_sizes(self.args.profile):
            dataset = build_dataset(rows)
            started = perf_counter()
            try:
                _, _, result = await self.analyze(actor, dataset, save_to=self.run.path / f"rows-{rows}")
            except (NativeFailure, OpenAIError, AzureError, ValueError, TimeoutError) as error:
                self.record(
                    "Capacity ladder stopped",
                    "FAIL",
                    "One successful bounded submission per ascending case",
                    {"rows": rows, "failure": classify_native_failure(error), "retry": False},
                )
                break
            metrics = result.get("telemetry")
            reading = capacity_reading(metrics if isinstance(metrics, dict) else {})
            reached = reached or (reading.pressure_fraction is not None and reading.pressure_fraction >= 0.75)
            self.record(
                f"{rows:,} rows: interpreter memory",
                "PASS" if reading.pressure_fraction is not None else "BLOCKED",
                "Measured process peak RSS and observed interpreter cgroup memory limit, in bytes",
                {**asdict(reading), "end_to_end_seconds": round(perf_counter() - started, 3)},
            )
            if reading.pressure_fraction is None or reached:
                break
        self.record(
            "Memory-pressure target",
            "PASS" if reached else "BLOCKED",
            "At least 75% of observed interpreter memory limit",
            {
                "target_reached": reached,
                "note": "A finite viable envelope or an earlier failure is not a pressure pass.",
            },
        )

    async def isolation(self, stack: AsyncExitStack, context: Mapping[str, JsonValue]) -> None:
        """Use two actual identities; synthetic user labels cannot establish isolation."""
        from scripts.common.auth_profiles import parse_users
        from scripts.run_scenario.native_user_isolation import ProfileCredential, prepare_user

        users = parse_users(self.args.users)
        tenant = context.get("AZURE_TENANT_ID")
        if len(users) != 2 or not isinstance(tenant, str) or not tenant:
            raise Blocked("Step 4 needs exactly two existing isolated CLI user profiles and AZURE_TENANT_ID.")
        profiles = [prepare_user(user, tenant) for user in users]
        if profiles[0].principal == profiles[1].principal:
            raise Blocked("Step 4 requires two distinct authenticated principals.")
        actors = []
        credentials: list[AsyncTokenCredential] = []
        for profile in profiles:
            credential = await stack.enter_async_context(ProfileCredential(profile))
            credentials.append(credential)
            actors.append(await create_actor(stack, self.endpoint, credential, self.version, self.remaining()))
        prepared = []
        for index, actor in enumerate(actors):
            binding, _, _ = await self.analyze(actor, build_dataset(1000), save_to=self.run.path / f"owner-{index + 1}")
            prepared.append(binding)
        for owner_index, owner in enumerate(actors):
            foreign = actors[1 - owner_index]
            workflow = prepared[owner_index]["workflow_id"]
            command = {"operation": "inspect", "workflow_id": workflow}
            await self.command(owner, command)
            denied = False
            try:
                await self.command(foreign, command)
            except NativeFailure as error:
                denied = error.code == "native_workflow_denied"
            except APIStatusError as error:
                denied = error.status_code in (403, 404)
            await self.command(owner, command)
            self.record(
                f"Owner {owner_index + 1}: foreign workflow denied",
                "PASS" if denied else "FAIL",
                "Real foreign caller cannot inspect the owner's workflow; owner control succeeds before/after",
                {"foreign_denied": denied, "owner_control": True},
            )
            own_inspection = await self.command(owner, command)
            receipts = own_inspection.get("artifacts")
            if not isinstance(receipts, list) or not receipts or not isinstance(receipts[0], dict):
                raise ValueError("The owner control did not return a valid artifact receipt.")
            path = receipts[0].get("path")
            if not isinstance(path, str):
                raise ValueError("The owner artifact has no session-relative path.")
            file_denied = False
            try:
                await foreign.project.agents.download_session_file("data-analysis-agent", owner.session, path=path)
            except HttpResponseError as error:
                file_denied = error.status_code in (403, 404)
            async with asyncio.timeout(self.remaining()):
                await download_originals(owner, receipts)
            self.record(
                f"Owner {owner_index + 1}: foreign session file denied",
                "PASS" if file_denied else "FAIL",
                "Foreign user cannot open the original owner's session file; owner download still succeeds",
                {"foreign_denied": file_denied, "owner_control": True},
            )
        fresh = await create_actor(stack, self.endpoint, credentials[0], self.version, self.remaining())
        denied = False
        try:
            await self.command(fresh, {"operation": "inspect", "workflow_id": prepared[0]["workflow_id"]})
        except NativeFailure as error:
            denied = error.code == "native_workflow_denied"
        except APIStatusError as error:
            denied = error.status_code in (403, 404)
        self.record(
            "Same user, clean session",
            "PASS" if denied else "FAIL",
            "A distinct session cannot inherit the original workflow binding",
            {"denied": denied},
        )
        self.report.paragraph(
            "Scope: application-mediated workflow/session access by the named test users. "
            "This is not privileged project-user native-file confidentiality or a full permissions audit."
        )

    async def recovery(
        self,
        actor: NativeActor,
        credential: AsyncTokenCredential,
        context: Mapping[str, JsonValue],
        stack: AsyncExitStack,
    ) -> None:
        """Recompute in a fresh binding only from a successfully read-back Blob checkpoint."""
        import httpx

        from scripts.run_scenario.story1_sandbox import (
            BlobCheckpointError,
            BlobStore,
            build_checkpoint,
            persist_and_read_checkpoint,
            seal_checkpoint,
        )

        endpoint = context.get("AZURE_STORAGE_BLOB_ENDPOINT")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise Blocked("Step 5 requires an existing HTTPS AZURE_STORAGE_BLOB_ENDPOINT; no storage is provisioned.")
        dataset = build_dataset(1000)
        prepared, _, result = await self.analyze(actor, dataset, save_to=self.run.path / "original")
        workflow = str(prepared["workflow_id"])
        original_container = prepared.get("container_fingerprint")
        if not isinstance(original_container, str) or len(original_container) != 64:
            raise Blocked("The native runtime did not provide a trusted interpreter-container fingerprint.")
        result_values: dict[str, JsonValue] = AnalysisResult.model_validate(result).model_dump(mode="json")
        source: dict[str, JsonValue] = {
            "execution": PROTOCOL,
            "actor": "current-authenticated-cli-caller",
            "session_sha256": hashlib.sha256(actor.session.encode()).hexdigest(),
            "workflow": workflow,
            "container_fingerprint": original_container,
            "agent_version": self.version,
            "rows": dataset.rows,
            "input_sha256": dataset.sha256,
            "by_category": object_value(result_values.get("by_category")),
        }
        checkpoint = build_checkpoint(self.run.run_id, dataset, workflow, result_values, source)
        sealed = seal_checkpoint(checkpoint)
        name = f"story1/checkpoints/{self.run.run_id}.json"
        token = await credential.get_token("https://storage.azure.com/.default")
        timeout = min(self.remaining(), 60)

        def round_trip() -> dict[str, JsonValue]:
            with httpx.Client(timeout=timeout, trust_env=False) as http:
                store = BlobStore(http, endpoint, self.args.blob_container, token.token)
                return persist_and_read_checkpoint(store, name, sealed, dataset, workflow, expected_source=source)

        try:
            restored = await asyncio.to_thread(round_trip)
        except BlobCheckpointError as error:
            self.record(
                "Required Blob checkpoint",
                error.status,
                "Authenticated write plus validated original-byte readback; no local substitution",
                {"error": type(error).__name__, "reconstruction_attempted": False, "checkpoint_retained": True},
            )
            return
        restored_source = object_value(restored.get("source"))
        rows = restored_source.get("rows")
        if type(rows) is not int or rows != 1000 or restored_source != source:
            raise ValueError("Blob reconstruction source does not match the authorized original workflow.")
        rebuilt_dataset = build_dataset(rows)
        if rebuilt_dataset.sha256 != restored_source.get("input_sha256"):
            raise ValueError("Reconstructed input differs from the verified Blob source.")
        fresh = await create_actor(stack, self.endpoint, credential, self.version, self.remaining())
        rebuilt_binding, _, rebuilt = await self.analyze(
            fresh, rebuilt_dataset, save_to=self.run.path / "reconstructed"
        )
        new_interpreter = (
            isinstance(rebuilt_binding.get("container_fingerprint"), str)
            and len(str(rebuilt_binding["container_fingerprint"])) == 64
            and rebuilt_binding["container_fingerprint"] != restored_source.get("container_fingerprint")
        )
        matches = (
            fresh.session != actor.session
            and new_interpreter
            and rebuilt.get("by_category") == restored_source.get("by_category")
            and rebuilt.get("input_sha256") == restored_source.get("input_sha256")
        )
        self.record(
            "Blob-backed reconstruction",
            "PASS" if matches else "FAIL",
            "Verified Blob metadata selects input and expected aggregates; a new authorized session recomputes them",
            {
                "actor": "validation runner",
                "new_session": fresh.session != actor.session,
                "new_interpreter": new_interpreter,
                "matches": matches,
                "checkpoint_retained": True,
                "original_interpreter_deleted": False,
                "kind": "recomputation, not recovered interpreter RAM or original intermediate state",
            },
        )
        if self.args.hosted_idle == "observe":
            await self.hosted_observation(fresh, str(rebuilt_binding["workflow_id"]))
        else:
            self.report.paragraph("Optional Hosted idle/resume probe not run. No idle or interpreter-expiry claim.")

    async def hosted_observation(self, actor: NativeActor, workflow: str) -> None:
        """One no-request interval; no lifecycle mutation or automatic redesign."""
        try:
            seeded = await self.command(actor, {"operation": "observe", "workflow_id": workflow})
            before = seeded.get("observation")
            if not isinstance(before, dict):
                raise Blocked("The native runtime exposes no supported Hosted observation contract.")
            if not isinstance(before.get("process_boot_id"), str) or not isinstance(before.get("home_sentinel"), str):
                raise Blocked("The native runtime exposes no supported Hosted process/HOME observation contract.")
            if self.args.max_seconds - (perf_counter() - self.started) < 180 + self.args.timeout:
                raise Blocked("The approved budget cannot cover the one 180-second optional quiet interval.")
            await asyncio.sleep(180)
            resumed = await self.command(actor, {"operation": "observe", "workflow_id": workflow})
            after = resumed.get("observation")
            if not isinstance(after, dict):
                raise Blocked("The resumed runtime returned no Hosted observation.")
        except (NativeFailure, OpenAIError, AzureError, Blocked, ValueError, TimeoutError) as error:
            self.record(
                "Optional Hosted idle/resume",
                "BLOCKED",
                "Supported process/HOME observation within one bounded quiet interval",
                {"blocker": classify_native_failure(error), "redesign": "Proposal only; explicit approval required."},
            )
            return
        changed = before["process_boot_id"] != after.get("process_boot_id")
        retained = before["home_sentinel"] == after.get("home_sentinel")
        self.record(
            "Optional Hosted marker observation",
            "PASS" if changed and retained else "BLOCKED",
            "Changed process marker and retained HOME sentinel after a measured no-request interval",
            {"seconds": 180, "process_changed": changed, "home_retained": retained},
        )
        self.record(
            "Platform idle boundary witness",
            "BLOCKED",
            "Independent platform lifecycle evidence before an idle/resume pass",
            {"note": "Quiet time and markers alone do not identify the platform lifecycle transition."},
        )

    async def supervisor(self, project: AIProjectClient) -> None:
        """Check the existing native delegation contract before any speculative model call."""
        evidence = self.args.output_dir.parent / "story-1" / "native-analysis-evidence.json"
        baseline_guard(evidence, self.args.environment, self.before)
        self.record(
            "Optional native Supervisor integration",
            "BLOCKED",
            "Supported nested native session/file authorization contract before analytical delegation",
            {
                "blocker": "Existing stateless subtasks lack the direct native session/file authorization contract.",
                "analytical_submission": False,
                "next": "Propose a minimal trusted adapter and revised validation budget for explicit user approval.",
            },
        )
        self.report.paragraph(
            "Preflight stopped before inference: no identity header workaround, synthesized native call, "
            "or framework redesign was attempted. This is not a successful Supervisor delivery result."
        )


@dataclass(frozen=True)
class CapacityReading:
    """Interpreter measurements only; no inferred limits or Hosted memory."""

    peak_rss_bytes: int | None
    memory_limit_bytes: int | None
    cgroup_memory_peak_bytes: int | None
    pressure_fraction: float | None


def positive_integer(value: object) -> int | None:
    """Reject booleans, zero, strings, and unavailable readings."""
    return value if type(value) is int and value > 0 else None


def capacity_reading(value: Mapping[str, object]) -> CapacityReading:
    """Calculate process peak versus observed cgroup limit with explicit sources."""
    peak = positive_integer(value.get("peak_rss_bytes"))
    limit = positive_integer(value.get("memory_limit_bytes"))
    cgroup_peak = positive_integer(value.get("cgroup_memory_peak_bytes"))
    return CapacityReading(peak, limit, cgroup_peak, peak / limit if peak is not None and limit else None)


def profile_sizes(profile: Profile) -> tuple[int, ...]:
    """Keep the original baseline and a separate finite, ascending pressure ladder."""
    return DATASET_SIZES if profile == "baseline" else PRESSURE_SIZES


def classify_native_failure(error: Exception) -> str:
    """Do not label timeouts or unknown failures as memory exhaustion."""
    from openai import APIStatusError, APITimeoutError

    if isinstance(error, APITimeoutError | TimeoutError):
        return "timeout"
    if isinstance(error, APIStatusError):
        return f"http_{error.status_code}"
    if isinstance(error, Blocked):
        return "prerequisite"
    if isinstance(error, NativeFailure):
        return error.code
    return type(error).__name__


def verify_outputs(dataset: Dataset, files: Mapping[str, bytes]) -> dict[str, bool]:
    """Validate original outputs against an independent seeded integer-cents oracle."""
    if set(files) != set(OUTPUT_NAMES):
        raise ValueError("Native output inventory does not match the fixed Story 1 contract.")
    if any(not body for body in files.values()) or sum(map(len, files.values())) > MAX_OUTPUT_BYTES:
        raise ValueError("Native output bytes are empty or exceed the approved output limit.")
    value: object = json.loads(files["result.json"])
    if not isinstance(value, dict):
        raise ValueError("Native result.json must contain an object.")
    expected = expected_aggregates(generate_rows(dataset.rows))
    ids = expected_filtered_ids(generate_rows(dataset.rows))
    filtered = check_filtered_csv(files["filtered.csv"], ids, COLUMNS)
    observed_rows = csv.DictReader(io.StringIO(files["filtered.csv"].decode("utf-8")))
    source = csv.DictReader(io.StringIO(dataset.content.decode("utf-8")))
    expected_rows = (row for row in source if row["status"] == "completed" and row["region"] == "West")
    rows_match = all(
        same_csv_row(actual, expected_row) for actual, expected_row in zip_longest(observed_rows, expected_rows)
    )
    png = files["revenue_by_category.png"]
    dimensions = png_dimensions(png) if len(png) >= 24 else None
    totals = value.get("total")
    return {
        "input_sha256": value.get("input_sha256") == dataset.sha256,
        "filtered_rows": type(value.get("filtered_rows")) is int and value["filtered_rows"] == len(ids),
        "by_category": aggregates_match(value.get("by_category"), expected),
        "total": isinstance(totals, dict)
        and all(type(totals.get(key)) is int for key in ("rows", "units", "revenue_cents"))
        and totals == asdict(total_of(expected)),
        "filtered_csv": all(
            filtered[key] is True for key in ("columns_match", "row_ids_match", "all_rows_match_filter")
        )
        and rows_match,
        "by_category_csv": check_by_category_csv(files["by_category.csv"], expected),
        "png": dimensions is not None and all(0 < dimension <= 10_000 for dimension in dimensions),
    }


def same_csv_row(actual: Mapping[str, str] | None, expected: Mapping[str, str] | None) -> bool:
    """Compare exact values, permitting lossless nullable integer CSV formatting."""
    if actual is None or expected is None or set(actual) != set(expected):
        return False
    numeric = {"row_id", "units", "unit_price_cents", "revenue_cents"}
    for column, value in expected.items():
        candidate = actual[column]
        if not isinstance(candidate, str):
            return False
        if column not in numeric or value == "":
            if candidate != value:
                return False
            continue
        try:
            observed = Decimal(candidate)
        except InvalidOperation:
            return False
        if not observed.is_finite() or observed != Decimal(value):
            return False
    return True


def validate_chart_review(dataset: Dataset, png: bytes, receipt: object) -> bool:
    """A human review must name the exact original PNG and expected plotted data."""
    if not isinstance(receipt, dict) or receipt.get("accepted") is not True:
        return False
    expected = expected_aggregates(generate_rows(dataset.rows))
    return (
        receipt.get("png_sha256") == hashlib.sha256(png).hexdigest()
        and receipt.get("categories") == sorted(expected)
        and receipt.get("revenue_cents") == [expected[name].revenue_cents for name in sorted(expected)]
    )


def build_parser() -> argparse.ArgumentParser:
    """Keep every service operation behind explicit execution and finite limits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, choices=range(1, 7), required=True)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--execution", choices=("preview", "run"), default="preview")
    parser.add_argument("--profile", choices=("baseline", "pressure"), default="baseline")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--max-seconds", type=float, default=1800)
    parser.add_argument("--users", default="", help="Step 4: two existing isolated CLI user profiles, comma-separated.")
    parser.add_argument("--blob-container", default="artifacts")
    parser.add_argument("--hosted-idle", choices=("skip", "observe"), default="skip")
    parser.add_argument("--chart-review", type=Path, help="Human review receipt tied to the original chart SHA-256.")
    parser.add_argument(
        "--review-run", type=Path, help="Offline chart review of a prior native Step 2 run; no new model request."
    )
    add_output_arguments(parser)
    return parser


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject unbounded, unsupported, or misleading combinations before service access."""
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds.")
    if not math.isfinite(args.max_seconds) or not args.timeout <= args.max_seconds <= 1800:
        parser.error("--max-seconds must cover one request and be at most 1800 seconds.")
    if args.profile == "pressure" and args.step != 3:
        parser.error("--profile pressure applies only to Step 3.")
    if args.review_run and (args.step != 2 or not args.chart_review or args.execution != "preview"):
        parser.error("--review-run requires Step 2, --chart-review, and preview execution (offline only).")
    if args.hosted_idle != "skip" and args.step != 5:
        parser.error("--hosted-idle applies only to Step 5.")


def preview(args: argparse.Namespace) -> dict[str, object]:
    """Describe the exact intended operations without resolving Azure configuration."""
    analyses = {1: 1, 2: 1, 3: len(profile_sizes(args.profile)), 4: 2, 5: 2, 6: 0}
    return {
        "agent": "data-analysis-agent",
        "execution": "native-managed-code-interpreter",
        "step": args.step,
        "profile": args.profile,
        "rows": list(profile_sizes(args.profile)) if args.step == 3 else [1000],
        "analyses_max": analyses[args.step],
        "supervisor_contract": "Currently blocked before inference" if args.step == 6 else "Not used",
        "timeout_seconds": args.timeout,
        "max_seconds": args.max_seconds,
        "automatic_retries": 0,
        "service_deletion": False,
        "role_changes": False,
        "deployment": False,
        "optional_probe_policy": "Stop on blocker; propose redesign only for explicit sponsor approval.",
    }


def offline_chart_review(validation: NativeValidation) -> None:
    """Review retained originals without making another paid analysis submission."""
    args = validation.args
    origin = args.review_run.resolve()
    if not origin.is_relative_to(args.output_dir.resolve()) or origin == validation.run.path.resolve():
        raise ValueError("The reviewed run must be an existing run under this Story 1 output directory.")
    context: object = json.loads((origin / "analysis-context.json").read_text(encoding="utf-8"))
    if (
        not isinstance(context, dict)
        or context.get("protocol") != PROTOCOL
        or context.get("environment") != args.environment
        or type(context.get("rows")) is not int
        or context["rows"] != 1000
        or not isinstance(context.get("agents"), dict)
        or not isinstance(context.get("original_sha256"), dict)
    ):
        raise ValueError("The original run has no valid native Step 2 review context.")
    files = {}
    combined = 0
    for name in OUTPUT_NAMES:
        path = origin / "outputs" / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_OUTPUT_BYTES:
            raise ValueError("An original review file is missing, linked, or oversized.")
        files[name] = path.read_bytes()
        combined += len(files[name])
        if combined > MAX_OUTPUT_BYTES or hashlib.sha256(files[name]).hexdigest() != context["original_sha256"].get(
            name
        ):
            raise ValueError("The reviewed files differ from the original native run.")
    checks = verify_outputs(build_dataset(context["rows"]), files)
    validation.record(
        "Retained analytical originals",
        "PASS" if all(checks.values()) else "FAIL",
        "Original bytes and oracle match",
        checks,
    )
    if not all(checks.values()):
        raise ValueError("The original analytical proof cannot be approved by a chart review.")
    receipt: object = json.loads(args.chart_review.read_text(encoding="utf-8"))
    accepted = validate_chart_review(build_dataset(context["rows"]), files["revenue_by_category.png"], receipt)
    validation.record(
        "Chart meaning",
        "PASS" if accepted else "FAIL",
        "Review bound to the exact original PNG and expected plotted data",
        {"accepted": accepted, "original_run": origin.name},
    )
    validation.before = object_value(context["agents"])
    baseline_guard(args.output_dir / NATIVE_BASELINE, args.environment, validation.before)
    validation.report.paragraph("Offline review of retained evidence; no new model request or resource mutation.")


def write_analysis_evidence(validation: NativeValidation) -> None:
    """Only fully checked native Step 2 evidence can satisfy the optional Supervisor prerequisite."""
    if validation.args.step == 2 and result_verdict(validation.run.statuses) == "Met":
        (validation.args.output_dir / "native-analysis-evidence.json").write_text(
            json.dumps(
                {
                    "execution": PROTOCOL,
                    "environment": validation.args.environment,
                    "agents": validation.before,
                    "verdict": "Met",
                    "run_id": validation.run.run_id,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Preview safely by default; live execution requires an explicit approved run."""
    from scripts.common.auth_profiles import ProfileError

    parser = build_parser()
    args = parser.parse_args(argv)
    validate_arguments(parser, args)
    if args.execution == "preview" and not args.review_run:
        print(json.dumps(preview(args), indent=2))
        return 0
    spec = SPECS[args.step]
    args.output_dir = args.output_dir / f"story-{spec.story}"
    run = ValidationRun(
        spec.scenario,
        "hosted",
        args.output_dir,
        no_color=args.no_color,
        verbose=args.verbose,
        step=spec,
        variant="native",
    )
    report = MarkdownReport(run.path / REPORT_FILE, f"Story {spec.story} / Step {spec.number} - Native validation")
    validation = NativeValidation(args, run, report)
    try:
        if args.review_run:
            offline_chart_review(validation)
        else:

            async def bounded() -> None:
                async with asyncio.timeout(args.max_seconds):
                    await validation.execute()

            asyncio.run(bounded())
        write_analysis_evidence(validation)
    except Blocked as error:
        validation.record("Prerequisite", "BLOCKED", "Native deployment and required evidence ready", str(error))
    except (
        CliError,
        ProfileError,
        NativeFailure,
        AzureError,
        OpenAIError,
        ValueError,
        OSError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        validation.record(
            "Validation stopped",
            "FAIL",
            "One bounded attempt, no fallback or retry",
            {"failure": classify_native_failure(error), "retry": False},
        )
    verdict = result_verdict(run.statuses)
    report.summary(
        f"**Verdict: {verdict}.** Native execution only; this report does not certify every Story 1 requirement. "
        "Optional blockers require an approved redesign proposal before further implementation."
    )
    report.render(run.console)
    index = args.output_dir / "index.md"
    update_index(index, index_entry(index, spec.label("native"), verdict, run.path))
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
