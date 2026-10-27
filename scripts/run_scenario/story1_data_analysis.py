"""Story 1 demonstrations against the EXISTING hosted agents in one azd environment.

Step 1 enables managed Code Interpreter on the already-deployed ``data-analysis-agent``.
It reuses that agent (name, endpoint, identity, routing) and never creates an agent.
``--mode verify`` is read-only. ``--mode enable-if-missing`` may, once, create the
Story 1 toolbox and move the existing Data Analysis agent to one new version.

Step 2 asks the existing Data Analysis agent to filter, group, summarize, and chart the 1k-row
input with model-written code, validates the printed aggregates and the generated files against a
local oracle, and records the unchanged Supervisor workflow path. It never changes an agent.

Step 3 runs the same analysis workload on the 1k/10k/100k-row inputs already mounted by the
Step 1 toolbox, one sequential request per size with no retries, and stops escalating at the
first platform failure. It never deploys, uploads, or changes an agent or toolbox.
Exit codes: 0 all checks passed, 1 failure, 2 blocked prerequisite.
"""

import argparse
import csv
import io
import json
import re
import struct
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

import httpx
from app.core.redaction import sanitize_text
from app.services.code_interpreter import ARTIFACTS_FENCE, parse_artifacts
from app.services.delegation import Command, Report
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AutoCodeInterpreterToolParam, CodeInterpreterToolboxTool
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import AzureCliCredential
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError, RateLimitError
from pydantic import JsonValue, ValidationError

from scripts.common.findings import Finding, checks_outcome, isolation_options_link, render_summary
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
from scripts.common.synthetic_data import (
    COLUMNS,
    DATASET_SIZES,
    FILTER_REGION,
    FILTER_STATUS,
    SEED,
    CategoryTotals,
    Dataset,
    build_dataset,
    expected_aggregates,
    expected_filtered_ids,
    generate_rows,
)
from scripts.deploy.grant_local_rbac import CliError, command_prefix, native_json, object_value
from scripts.run_scenario.hosted_smoke import SERVICES
from scripts.run_scenario.trigger_agent import response_summary

DATA_ANALYSIS = SERVICES["data_analysis"]
TOOLBOX_NAME = "story1-data-analysis-ci"
TOOLBOX_ENV = "DATA_ANALYSIS_TOOLBOX_ENDPOINT"
AGENT_TOOLBOX_ENV = "TOOLBOX_ENDPOINT"
DATA_SCOPE = "https://ai.azure.com/.default"
STORY_DIR = "story-1"
BASELINE_FILE = "agents-baseline.json"
STEP1 = StepSpec(1, 1, "code-interpreter")
TASK_LABEL = "Agents: Story 1 - 1 Enable Code Interpreter on existing Data Analysis agent"
AC1 = (
    "Deploy a Foundry Hosted Data Analysis Agent using Microsoft-managed Code Interpreter, "
    "backed by Azure Container Apps dynamic sessions."
)
Mode = Literal["verify", "enable-if-missing"]


class Blocked(RuntimeError):
    """A prerequisite is missing; nothing was changed to work around it."""


@dataclass(frozen=True)
class AgentSnapshot:
    """The identity and version facts that must stay stable when an agent is reused."""

    name: str
    version: str
    status: str
    principal_id: str
    latest_routing: bool
    toolbox_endpoint: str | None

    def row(self) -> list[object]:
        return [
            self.name,
            self.version,
            self.status,
            self.principal_id,
            "@latest 100%" if self.latest_routing else "custom",
            "configured" if self.toolbox_endpoint else "none",
        ]


SNAPSHOT_HEADERS = ["Agent", "Version", "Status", "Instance principal", "Routing", "Toolbox"]


@dataclass(frozen=True)
class ToolboxRecord:
    name: str
    version: str
    endpoint: str
    file_ids: dict[str, str]


def snapshot_agent(record: Mapping[str, JsonValue]) -> AgentSnapshot:
    """Read only the stable fields of an existing agent record."""
    latest = object_value(object_value(record.get("versions")).get("latest"))
    definition = object_value(latest.get("definition"))
    environment = definition.get("environment_variables")
    identity = record.get("instance_identity") or latest.get("instance_identity")
    selector = object_value(record.get("agent_endpoint") or {}).get("version_selector")
    rules = object_value(selector).get("version_selection_rules") if isinstance(selector, dict) else None
    toolbox = environment.get(AGENT_TOOLBOX_ENV) if isinstance(environment, dict) else None
    return AgentSnapshot(
        name=str(record.get("name")),
        version=str(latest.get("version")),
        status=str(latest.get("status")),
        principal_id=str(object_value(identity).get("principal_id")) if isinstance(identity, dict) else "unknown",
        latest_routing=rules == [{"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}],
        toolbox_endpoint=toolbox if isinstance(toolbox, str) and toolbox else None,
    )


def snapshot_agents(read: Callable[[str], Mapping[str, JsonValue]]) -> dict[str, AgentSnapshot]:
    """Snapshot every existing POC agent; a missing agent is a blocker, never created."""
    snapshots: dict[str, AgentSnapshot] = {}
    for service in SERVICES.values():
        try:
            snapshots[service] = snapshot_agent(read(service))
        except ResourceNotFoundError as error:
            raise Blocked(f"Existing agent '{service}' was not found; this Task never creates agents.") from error
    return snapshots


def reuse_differences(
    before: Mapping[str, AgentSnapshot], after: Mapping[str, AgentSnapshot], *, version_change: str | None
) -> list[str]:
    """Only the explicitly enabled agent may change version; identities never change."""
    differences: list[str] = []
    for name, old in before.items():
        new = after.get(name)
        if new is None:
            differences.append(f"{name} disappeared")
            continue
        if new.principal_id != old.principal_id:
            differences.append(f"{name} instance identity changed")
        if new.status != "active":
            differences.append(f"{name} is {new.status}")
        if name != version_change and (new.version, new.toolbox_endpoint) != (old.version, old.toolbox_endpoint):
            differences.append(f"{name} changed version or configuration unexpectedly")
    return differences


def dataset_metadata(datasets: Sequence[Dataset], file_ids: Mapping[str, str] | None = None) -> dict[str, str]:
    """Toolbox metadata that lets later runs prove the mounted inputs are unchanged."""
    metadata = {"story": "1", "seed_schema": "story1-sales-v1"}
    for dataset in datasets:
        metadata[f"sha256_{dataset.rows}"] = dataset.sha256
        if file_ids is not None:
            metadata[f"file_{dataset.rows}"] = file_ids[str(dataset.rows)]
    return metadata


def matching_toolbox(versions: Sequence[Mapping[str, JsonValue]], datasets: Sequence[Dataset]) -> dict[str, str] | None:
    """Return the newest toolbox version whose recorded checksums match these exact datasets."""
    expected = dataset_metadata(datasets)
    candidates: list[tuple[int, dict[str, str]]] = []
    for item in versions:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if any(metadata.get(key) != value for key, value in expected.items()):
            continue
        file_ids = {str(d.rows): metadata.get(f"file_{d.rows}") for d in datasets}
        if not all(isinstance(value, str) and value for value in file_ids.values()):
            continue
        raw_version = str(item.get("version"))
        if raw_version.isdigit():
            candidates.append((int(raw_version), {"version": raw_version, **{k: str(v) for k, v in file_ids.items()}}))
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def toolbox_endpoint(project_endpoint: str, name: str, toolbox_version: str) -> str:
    return f"{project_endpoint.rstrip('/')}/toolboxes/{name}/versions/{toolbox_version}/mcp?api-version=v1"


UPLOAD_FINDING = (
    "Upload finding (tested 2026-10-22): this Foundry project has no `amlWorkspace`. Files uploaded at the "
    "resource-level `/openai/v1/files` endpoint (without a project header, or with `properties.internalId` as "
    "`x-aml-project-id` in dashed or no-dash form) failed toolbox ownership verification. Project-scoped "
    "uploads succeeded, which differs from the documented toolbox guidance written for `amlWorkspace` projects."
)


def mcp_result(response: httpx.Response) -> dict[str, JsonValue]:
    """Accept JSON or a single-event SSE body from the stateless toolbox MCP endpoint."""
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("content-type", ""):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        if not data:
            raise ValueError("Toolbox MCP returned an empty event stream.")
        payload: JsonValue = json.loads(data[-1])
    else:
        payload = response.json()
    message = object_value(payload)
    if "error" in message:
        raise ValueError(f"Toolbox MCP error: {sanitize_text(json.dumps(message['error']))[:500]}")
    return object_value(message.get("result"))


def result_text(result: Mapping[str, JsonValue]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(item.get("text")) for item in content if isinstance(item, dict) and item.get("type") == "text")


def result_shape(result: Mapping[str, JsonValue]) -> dict[str, object]:
    """Describe how a tool result references files without copying file content into evidence."""
    content = result.get("content")
    items = content if isinstance(content, list) else []
    structured = result.get("structuredContent")
    meta = result.get("_meta")
    return {
        "keys": sorted(result),
        "is_error": result.get("isError"),
        "content": [
            {
                key: (value if key in {"type", "mimeType", "name", "uri"} else type(value).__name__)
                for key, value in item.items()
            }
            for item in items
            if isinstance(item, dict)
        ],
        "structured_keys": sorted(structured) if isinstance(structured, dict) else None,
        "meta_keys": sorted(meta) if isinstance(meta, dict) else None,
        "text_excerpt": sanitize_text(result_text(result))[:400],
    }


HASH_CODE = """
import glob, hashlib, json, os, platform
files = {}
for path in sorted(glob.glob('/mnt/data/*story1-sales-*.csv')):
    with open(path, 'rb') as stream:
        files[os.path.basename(path)] = hashlib.sha256(stream.read()).hexdigest()
print(json.dumps({'python': platform.python_version(), 'six_times_seven': 6 * 7, 'files': files}))
"""

FILE_OUTPUT_CODE = """
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.figure(figsize=(2, 2)); plt.bar(['a', 'b'], [1, 2]); plt.savefig('/mnt/data/story1-probe.png'); plt.close()
print('saved /mnt/data/story1-probe.png')
"""


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def list_generated_files(
    client: httpx.Client, project_endpoint: str, token: str, container: str
) -> list[dict[str, JsonValue]]:
    """Files the sandbox created (``source=assistant``); mounted inputs have ``source=user``."""
    response = client.get(
        f"{project_endpoint}/openai/v1/containers/{container}/files", headers={"Authorization": f"Bearer {token}"}
    )
    response.raise_for_status()
    data = object_value(response.json()).get("data")
    items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    return [item for item in items if item.get("source") == "assistant"]


def mounted_hashes(text: str) -> tuple[str | None, dict[str, str]]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None, {}
    value = json.loads(match.group(0))
    files = value.get("files") if isinstance(value, dict) else None
    python = value.get("python") if isinstance(value, dict) else None
    return (
        python if isinstance(python, str) else None,
        {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {},
    )


def hash_matches(datasets: Sequence[Dataset], hashes: Mapping[str, str]) -> dict[str, bool]:
    """Mounted files are named ``{file-id}-{original-name}``."""
    return {
        dataset.filename: any(
            name.endswith(dataset.filename) and digest == dataset.sha256 for name, digest in hashes.items()
        )
        for dataset in datasets
    }


def hosted_prompt(dataset: Dataset) -> str:
    return (
        "Use your code interpreter tool to run Python that computes the SHA-256 hex digest of the file in "
        f"/mnt/data whose name ends with '{dataset.filename}', and prints platform.python_version(). "
        'Reply with only this JSON, using the values the tool printed: {"sha256": "<hex>", "python": "<version>"}'
    )


def read_environment(environment: str, report: MarkdownReport) -> dict[str, JsonValue]:
    """Read the azd environment and write the shared Configuration table; never changes it."""
    try:
        context = object_value(
            native_json("azd", ["env", "get-values", "--environment", environment, "--output", "json"])
        )
    except (CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        raise Blocked(f"Cannot read azd environment '{environment}': {error}") from error
    required = ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_LOCATION")
    missing = [key for key in required if not isinstance(context.get(key), str) or not context.get(key)]
    if missing:
        raise Blocked(f"azd environment is missing {', '.join(missing)}.")
    report.heading("Configuration")
    azd_version = subprocess.run(
        [*command_prefix("azd"), "version"], capture_output=True, text=True, timeout=60, check=False
    ).stdout.strip()
    packages: list[list[object]] = [
        [package, version(package)]
        for package in (
            "agent-framework-core",
            "agent-framework-foundry",
            "agent-framework-foundry-hosting",
            "azure-ai-projects",
            "azure-ai-agentserver-responses",
        )
    ]
    report.table(
        ["Setting", "Value"],
        [
            ["Project endpoint", context["FOUNDRY_PROJECT_ENDPOINT"]],
            ["Region", context["AZURE_LOCATION"]],
            ["Model deployment", context.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")],
            ["azd", azd_version],
            *packages,
            ["Toolbox MCP API", "v1"],
        ],
    )
    return context


class Step1:
    """Enable and verify managed Code Interpreter on the existing Data Analysis agent."""

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        self.args = args
        self.mode: Mode = args.mode
        self.run = run
        self.report = report
        self.datasets = [build_dataset(size) for size in DATASET_SIZES]
        self.deployed = False
        self.verdict = "Not met"
        self.project_endpoint = ""
        self.statuses: dict[str, Status] = {}
        self.obs: dict[str, Any] = {}

    def record(self, step: str, status: Status, expected: str, observed: object) -> None:
        self.statuses[step] = status
        self.run.result(step, status, expected=expected, observed=observed)
        self.report.table(
            ["Check", "Status", "Expected", "Observed"], [[step, status_icon(status), expected, observed]]
        )

    def execute(self) -> None:
        self.report.paragraph(f"**AC1 (verbatim from GitHub issue #1):** {AC1}")
        self.report.paragraph(
            "This step **reuses the already-deployed agents**. It enables Code Interpreter on the existing "
            f"`{DATA_ANALYSIS}` and never creates an agent. Supervisor and Research are not changed."
        )
        self.report.table(
            ["Field", "Value"],
            [
                ["Task", TASK_LABEL],
                ["Run ID", self.run.run_id],
                ["UTC time", datetime.now(UTC).isoformat(timespec="seconds")],
                ["Environment", self.args.environment],
                ["Mode", self.mode],
            ],
        )
        context = self.environment()
        endpoint = str(context["FOUNDRY_PROJECT_ENDPOINT"]).rstrip("/")
        self.project_endpoint = endpoint
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
        ):

            def read(name: str) -> Mapping[str, JsonValue]:
                return object_value(project.agents.get(name).as_dict())

            self.report.heading("Agents reused - before")
            before = snapshot_agents(read)
            self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in before.values()])
            inactive = [s.name for s in before.values() if s.status != "active" or not s.latest_routing]
            if inactive:
                raise Blocked(f"Existing agents are not active with @latest routing: {', '.join(inactive)}")
            self.run.result(
                "Agents reused - before",
                "PASS",
                expected="All three existing agents active",
                observed=[asdict(s) for s in before.values()],
            )

            self.report.heading("Synthetic datasets")
            self.report.table(
                ["File", "Rows", "Bytes", "SHA-256"],
                [[d.filename, d.rows, d.size_bytes, d.sha256] for d in self.datasets],
            )
            token = credential.get_token(DATA_SCOPE).token
            toolbox = self.toolbox(project, endpoint, token)
            self.probe(toolbox, token)
            self.enable(before[DATA_ANALYSIS], toolbox)

            after = snapshot_agents(read)
            self.obs["agent_version"] = after[DATA_ANALYSIS].version
            self.obs["toolbox"] = f"{toolbox.name} v{toolbox.version}"
            self.hosted_proof(project, token)
            self.report.heading("Agents reused - after")
            self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in after.values()])
            differences = reuse_differences(before, after, version_change=DATA_ANALYSIS if self.deployed else None)
            if after[DATA_ANALYSIS].toolbox_endpoint != toolbox.endpoint:
                differences.append(f"{DATA_ANALYSIS} active version is not wired to the Story 1 toolbox")
            self.record(
                "Agents reused - after",
                "FAIL" if differences else "PASS",
                "Same three agents and identities; only Data Analysis may move to its Code Interpreter version",
                differences or "No unexpected changes",
            )
            if not differences and "FAIL" not in self.run.statuses and "BLOCKED" not in self.run.statuses:
                baseline = self.args.output_dir / BASELINE_FILE
                baseline.write_text(
                    json.dumps(
                        {
                            "environment": self.args.environment,
                            "recorded_at": datetime.now(UTC).isoformat(),
                            "run_id": self.run.run_id,
                            "toolbox": asdict(toolbox),
                            "agents": {name: asdict(snapshot) for name, snapshot in after.items()},
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self.report.paragraph(f"Reuse baseline for Steps 2-5 written to `{baseline.name}`.")
                self.verdict = "Met"

    def environment(self) -> dict[str, JsonValue]:
        return read_environment(self.args.environment, self.report)

    def toolbox(self, project: AIProjectClient, endpoint: str, token: str) -> ToolboxRecord:
        self.report.heading("Code Interpreter toolbox")
        try:
            versions = [object_value(item.as_dict()) for item in project.toolboxes.list_versions(TOOLBOX_NAME)]
        except ResourceNotFoundError:
            versions = []
        match = matching_toolbox(versions, self.datasets)
        if match:
            record = ToolboxRecord(
                TOOLBOX_NAME,
                match["version"],
                toolbox_endpoint(endpoint, TOOLBOX_NAME, match["version"]),
                {str(d.rows): match[str(d.rows)] for d in self.datasets},
            )
            self.record("Toolbox", "PASS", "Reuse existing Story 1 toolbox with matching inputs", asdict(record))
            return record
        if self.mode == "verify":
            raise Blocked(
                f"Toolbox '{TOOLBOX_NAME}' with these datasets does not exist. "
                "Rerun with mode enable-if-missing to create it once."
            )
        file_ids: dict[str, str] = {}
        with httpx.Client(timeout=300, trust_env=False) as client:
            for dataset in self.datasets:
                response = client.post(
                    f"{endpoint.rstrip('/')}/openai/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"purpose": "assistants"},
                    files={"file": (dataset.filename, dataset.content, "text/csv")},
                )
                response.raise_for_status()
                file_id = object_value(response.json()).get("id")
                if not isinstance(file_id, str):
                    raise ValueError("Files API returned no file id.")
                file_ids[str(dataset.rows)] = file_id
        self.record(
            "Synthetic uploads", "PASS", "Project-scoped Files API (purpose=assistants)", {"file_ids": file_ids}
        )
        self.report.paragraph(UPLOAD_FINDING)
        created = object_value(
            project.toolboxes.create_version(
                TOOLBOX_NAME,
                tools=[
                    CodeInterpreterToolboxTool(container=AutoCodeInterpreterToolParam(file_ids=list(file_ids.values())))
                ],
                description="Story 1 synthetic-only managed Code Interpreter for the existing Data Analysis agent",
                metadata=dataset_metadata(self.datasets, file_ids),
            ).as_dict()
        )
        toolbox_version = str(created.get("version"))
        record = ToolboxRecord(
            TOOLBOX_NAME, toolbox_version, toolbox_endpoint(endpoint, TOOLBOX_NAME, toolbox_version), file_ids
        )
        self.record(
            "Toolbox", "PASS", "Create one Story 1 toolbox version (code_interpreter + 3 files)", asdict(record)
        )
        return record

    def probe(self, toolbox: ToolboxRecord, token: str) -> None:
        """Operator-identity MCP calls prove the sandbox reads the mounted inputs."""
        self.report.heading("Direct toolbox probe (operator identity)")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        with httpx.Client(timeout=180, trust_env=False) as client:

            def call(identifier: int, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
                body = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
                return mcp_result(client.post(toolbox.endpoint, headers=headers, json=body))

            tools = call(1, "tools/list", {}).get("tools")
            names = [str(t.get("name")) for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
            tool = next((name for name in names if "code_interpreter" in name), None)
            if tool is None:
                self.record("tools/list", "FAIL", "A code_interpreter tool", names)
                return
            self.record("tools/list", "PASS", "A code_interpreter tool", names)
            started = perf_counter()
            result = call(2, "tools/call", {"name": tool, "arguments": {"code": HASH_CODE}})
            python, hashes = mounted_hashes(result_text(result))
            matches = hash_matches(self.datasets, hashes)
            self.obs["python"] = python
            self.obs["inputs_verified"] = sum(matches.values())
            self.record(
                "Sandbox reads mounted inputs",
                "PASS" if matches and all(matches.values()) else "FAIL",
                "Sandbox SHA-256 of each mounted CSV equals the local SHA-256",
                {"python": python, "matches": matches, "seconds": round(perf_counter() - started, 2)},
            )
            output = call(3, "tools/call", {"name": tool, "arguments": {"code": FILE_OUTPUT_CODE}})
            self.record(
                "Generated-file result shape",
                "PASS" if not output.get("isError") else "FAIL",
                "Record how a generated file is referenced in the tool result",
                result_shape(output),
            )
            structured = output.get("structuredContent")
            container = structured.get("container_id") if isinstance(structured, dict) else None
            if not isinstance(container, str):
                self.record("Generated-file delivery", "FAIL", "Tool result carries a container_id", None)
                return
            files = list_generated_files(client, self.project_endpoint, token, container)
            png = next((item for item in files if str(item.get("path", "")).endswith("story1-probe.png")), None)
            downloaded = (
                client.get(
                    f"{self.project_endpoint}/openai/v1/containers/{container}/files/{png['id']}/content",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if png
                else None
            )
            is_png = (
                downloaded is not None and downloaded.status_code == 200 and downloaded.content[:8] == PNG_SIGNATURE
            )
            self.record(
                "Generated-file delivery",
                "PASS" if is_png else "FAIL",
                "Container files API lists the generated PNG (source=assistant) and returns valid PNG bytes",
                {
                    "container_id": container,
                    "generated_files": [item.get("path") for item in files],
                    "png_bytes": len(downloaded.content) if downloaded is not None else None,
                },
            )
            self.obs["png_bytes"] = len(downloaded.content) if is_png and downloaded is not None else None

    def enable(self, current: AgentSnapshot, toolbox: ToolboxRecord) -> None:
        self.report.heading("Existing Data Analysis agent")
        if current.toolbox_endpoint == toolbox.endpoint:
            self.record(
                "Reuse existing version", "PASS", "Active version already wired to this toolbox", current.version
            )
            return
        if self.mode == "verify":
            raise Blocked(
                f"Existing {DATA_ANALYSIS} v{current.version} is not wired to the Story 1 toolbox. "
                "Rerun with mode enable-if-missing to update this same agent once."
            )
        azd = command_prefix("azd")
        subprocess.run(
            [*azd, "env", "set", TOOLBOX_ENV, toolbox.endpoint, "--environment", self.args.environment],
            check=True,
            capture_output=True,
            timeout=60,
        )
        self.run.console.print(f"Updating existing {DATA_ANALYSIS} only (azd deploy {DATA_ANALYSIS}) ...")
        started = perf_counter()
        process = subprocess.Popen(
            [*azd, "deploy", DATA_ANALYSIS, "--environment", self.args.environment, "--no-prompt"],
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
        code = process.wait(timeout=1800)
        self.report.code("\n".join(lines[-30:]), "text")
        if code:
            self.record("Update existing agent", "FAIL", f"azd deploy {DATA_ANALYSIS} succeeds", f"exit {code}")
            raise Blocked("The existing agent was not updated; inspect the azd output above.")
        self.deployed = True
        self.record(
            "Update existing agent",
            "PASS",
            f"New version of the existing {DATA_ANALYSIS}; no other service deployed",
            {"seconds": round(perf_counter() - started), "service": DATA_ANALYSIS},
        )

    def hosted_proof(self, project: AIProjectClient, token: str) -> None:
        """The existing agent endpoint must return a hash only real sandbox execution can produce."""
        self.report.heading("Hosted execution proof (existing agent endpoint)")
        dataset = self.datasets[0]
        prompt = hosted_prompt(dataset)
        observed: dict[str, object] = {"agent": DATA_ANALYSIS, "prompt": prompt, "expected_sha256": dataset.sha256}
        started = perf_counter()
        status: Status
        text = ""
        try:
            with project.get_openai_client(
                agent_name=DATA_ANALYSIS, timeout=self.args.timeout, max_retries=0
            ) as client:
                response = client.responses.create(input=prompt, stream=False)
            summary = response_summary(response.model_dump(mode="json"))
            observed["response"] = summary
            text = str(summary.get("text"))
            status = "PASS" if dataset.sha256 in text else "FAIL"
        except (OpenAIError, AzureError) as error:
            observed["error"] = str(error)
            status = "BLOCKED" if "403" in str(error) else "FAIL"
            if status == "BLOCKED":
                observed["next_step"] = (
                    "Run the existing task 'Access: Apply hosted agent permissions'; no roles are granted here."
                )
        observed["seconds"] = round(perf_counter() - started, 2)
        self.record(
            "Hosted agent executes in managed sandbox",
            status,
            "Answer contains the exact local SHA-256 of the 1k dataset",
            observed,
        )
        if status != "PASS":
            return
        containers = parse_artifacts(text)
        details: dict[str, object] = {"containers": containers}
        with httpx.Client(timeout=60, trust_env=False) as client:
            for container in containers:
                reply = client.get(
                    f"{self.project_endpoint}/openai/v1/containers/{container}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if reply.status_code == 200:
                    info = object_value(reply.json())
                    details[container] = {key: info.get(key) for key in ("status", "memory_limit", "expires_after")}
                else:
                    details[container] = f"HTTP {reply.status_code}"
        exists = bool(containers) and all(isinstance(details[c], dict) for c in containers)
        self.obs["hosted_seconds"] = observed["seconds"]
        self.obs["container_info"] = next((details[c] for c in containers if isinstance(details[c], dict)), None)
        self.record(
            "Agent returns sandbox container reference",
            "PASS" if exists else "FAIL",
            f"Answer carries a `{ARTIFACTS_FENCE}` block whose container exists in this project",
            details,
        )

    def conclude(self) -> None:
        """Verdict plus a reader-first summary with Microsoft documentation for every finding."""
        if "BLOCKED" in self.statuses.values() and self.verdict != "Met":
            self.verdict = "Not evaluated"
        elif self.verdict == "Met" and "FAIL" in self.statuses.values():
            self.verdict = "Not met"
        ok = self.verdict == "Met"
        info = self.obs.get("container_info") or {}
        results = [
            f"Existing `{DATA_ANALYSIS}` (version {self.obs.get('agent_version', '-')}) is wired to toolbox "
            f"`{self.obs.get('toolbox', '-')}`; no agent was created.",
            f"The sandbox read all {self.obs.get('inputs_verified', 0)} of {len(self.datasets)} mounted CSVs with "
            f"matching SHA-256 checksums (Python {self.obs.get('python', '-')}).",
            "The hosted agent answered with the exact sandbox-computed SHA-256 "
            f"in {self.obs.get('hosted_seconds', '-')} s and returned its sandbox container reference.",
            f"Sandbox container: memory limit {info.get('memory_limit', '-')}, expiry "
            f"{info.get('expires_after', '-')}.",
            f"A sandbox-generated PNG ({self.obs.get('png_bytes', '-')} bytes) was downloaded through the "
            "authenticated containers API.",
        ]
        delivered = checks_outcome(
            self.statuses.get("Generated-file delivery"), self.statuses.get("Agent returns sandbox container reference")
        )
        findings = [
            Finding(
                "The hosted Data Analysis agent runs Python in Microsoft-managed Code Interpreter through a toolbox",
                "The existing agent answered with checksums that only the sandbox could compute"
                if ok
                else "Not shown in this run; see 'Hosted execution proof' in the detailed evidence",
                "Tested",
                "Foundry-managed",
                ("ci_overview", "toolbox_overview"),
                outcome="PASS" if ok else ("BLOCKED" if self.verdict == "Not evaluated" else "FAIL"),
            ),
            Finding(
                "The Code Interpreter sandbox uses Azure Container Apps dynamic sessions, each isolated by a Hyper-V "
                "boundary",
                "Microsoft documents this. It can't be inspected directly: the sessions are Microsoft-managed and no "
                "Container Apps resource exists in this subscription. Step 4 saw consistent platform variable names.",
                "Documented",
                "Foundry-managed",
                ("ci_sandbox", "aca_isolation"),
                outcome="DOCUMENTED",
            ),
            Finding(
                "Synthetic inputs are uploaded with purpose=assistants and mounted through toolbox file_ids",
                "All three CSVs were mounted under /mnt/data and their checksums matched"
                if self.statuses.get("Sandbox reads mounted inputs") == "PASS"
                else "See 'Sandbox reads mounted inputs' in the detailed evidence",
                "Tested",
                "Custom implementation (Dev Team)",
                ("files_upload",),
                outcome=checks_outcome(self.statuses.get("Sandbox reads mounted inputs")),
            ),
            Finding(
                "Generated files are retrieved by container ID and file ID",
                "The runner downloaded a sandbox-generated PNG with its own identity; the agent returned a "
                "poc-artifacts-v1 container reference"
                if delivered == "PASS"
                else "See 'Generated-file delivery' and 'Agent returns sandbox container reference' below",
                "Tested",
                "Custom implementation (Dev Team)",
                ("ci_files",),
                outcome=delivered,
            ),
            Finding(
                "Per-user sandbox isolation when Code Interpreter is used through a toolbox in a hosted agent",
                "All users of the project share the sandbox container context (Microsoft documents it; Step 4 "
                "confirmed another principal could read sandbox output). Untested options that may provide it: "
                f"{isolation_options_link(self.report.path.parent)}",
                "Documented",
                "Not supported",
                ("ci_isolation",),
                outcome="NOT SUPPORTED",
            ),
        ]
        gaps = [
            "The Azure Container Apps backing relies on Microsoft's documentation; it cannot be inspected from this "
            "subscription. A custom code interpreter on your own session pool is the verifiable alternative.",
            "`container_file_citation` annotations were observed inconsistently, so the runner relies on the "
            "container reference instead.",
        ]
        conclusion = (
            "The existing hosted Data Analysis agent executes code in Microsoft-managed Code Interpreter, which "
            "Microsoft documents as Azure Container Apps dynamic sessions."
            if ok
            else "See the checks below."
        )
        self.report.summary(render_summary(self.verdict, conclusion, results if ok else [], findings, gaps))


STEP3 = StepSpec(1, 3, "dataset-sizes")
STEP3_TASK_LABEL = "Agents: Story 1 - 3 Evaluate dataset sizes"
AC3 = (
    "Test a small, agreed set of progressively larger datasets. Record observed limits and failures, "
    "and conclude whether the tested workloads are viable in the evaluated configuration."
)
SANDBOX_DATA = "/mnt/data"
RESULT_FILE = "story1-step3-result.json"
OUTPUT_FILES = ("filtered.csv", "by_category.csv", "revenue_by_category.png")
BY_CATEGORY_COLUMNS = ["category", "rows", "units", "revenue_cents"]
# A platform failure stops escalation to larger inputs; a completed response with wrong or missing
# outputs is a correctness finding, so the next size still runs.
STOP_CATEGORIES = frozenset(
    {"timeout", "resource", "quota", "service_error", "connection", "permission", "agent_error"}
)
HOST_SETTINGS = (
    ("Hosted agent container", "0.5 CPU / 1 GiB (azure.yaml data-analysis-agent container.resources)"),
    ("Hosted agent session idle timeout", "120 s (azure.yaml sessionConfiguration)"),
)

CAPACITY_CODE = """
import glob, hashlib, json, os, platform, shutil, time
started = time.perf_counter()
DATA = "__DATA__"
path = next(p for p in sorted(glob.glob(os.path.join(DATA, "*"))) if p.endswith("__FILE__"))
with open(path, "rb") as stream:
    digest = hashlib.sha256(stream.read()).hexdigest()
hashed = time.perf_counter()
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
imported = time.perf_counter()
frame = pd.read_csv(path)
loaded = time.perf_counter()
filtered = frame[(frame["status"] == "__STATUS__") & (frame["region"] == "__REGION__")]
filtered.to_csv(os.path.join(DATA, "filtered.csv"), index=False)
grouped = filtered.groupby("category", sort=True).agg(
    rows=("row_id", "size"), units=("units", "sum"), revenue_cents=("revenue_cents", "sum")
).reset_index()
grouped[["rows", "units", "revenue_cents"]] = grouped[["rows", "units", "revenue_cents"]].astype("int64")
grouped.to_csv(os.path.join(DATA, "by_category.csv"), index=False)
summarized = time.perf_counter()
figure, axis = plt.subplots(figsize=(10, 6))
axis.bar(grouped["category"], grouped["revenue_cents"])
axis.set_title("revenue_cents by category (__STATUS__, __REGION__)")
figure.savefig(os.path.join(DATA, "revenue_by_category.png"))
plt.close(figure)
plotted = time.perf_counter()
def first_text(*candidates):
    for candidate in candidates:
        try:
            with open(candidate) as stream:
                return stream.read().strip()
        except OSError:
            pass
    return None
meminfo = first_text("/proc/meminfo") or ""
mem_total_kb = next((int(line.split()[1]) for line in meminfo.splitlines() if line.startswith("MemTotal:")), None)
try:
    import resource
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
except ImportError:
    peak_rss_kb = None
cpu_max = first_text("/sys/fs/cgroup/cpu.max")
if cpu_max is None:
    quota = first_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = first_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    cpu_max = f"{quota} {period}" if quota and period else quota
result = {
    "input_file": os.path.basename(path),
    "input_bytes": os.path.getsize(path),
    "input_sha256": digest,
    "filtered_rows": int(len(filtered)),
    "by_category": {
        str(row.category): {"rows": int(row.rows), "units": int(row.units), "revenue_cents": int(row.revenue_cents)}
        for row in grouped.itertuples(index=False)
    },
    "seconds": {
        "hash": round(hashed - started, 3),
        "import": round(imported - hashed, 3),
        "load": round(loaded - imported, 3),
        "filter_group": round(summarized - loaded, 3),
        "plot": round(plotted - summarized, 3),
        "total": round(plotted - started, 3),
    },
    "peak_rss_kb": peak_rss_kb,
    "cpu_count": os.cpu_count(),
    "mem_total_kb": mem_total_kb,
    "cgroup_memory_max": first_text("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    "cgroup_cpu_max": cpu_max,
    "disk_free_bytes": shutil.disk_usage(DATA).free,
    "python": platform.python_version(),
    "pandas": pd.__version__,
    "outputs": {
        name: os.path.getsize(os.path.join(DATA, name))
        for name in ("filtered.csv", "by_category.csv", "revenue_by_category.png")
    },
}
with open(os.path.join(DATA, "__RESULT__"), "w") as stream:
    json.dump(result, stream)
print(json.dumps(result))
"""


def capacity_code(filename: str, data_dir: str = SANDBOX_DATA) -> str:
    """The fixed sandbox workload; only the input file differs between sizes."""
    replacements = {
        "__DATA__": data_dir,
        "__FILE__": filename,
        "__STATUS__": FILTER_STATUS,
        "__REGION__": FILTER_REGION,
        "__RESULT__": RESULT_FILE,
    }
    code = CAPACITY_CODE
    for token, value in replacements.items():
        code = code.replace(token, value)
    return code.strip()


def capacity_prompt(dataset: Dataset) -> str:
    return (
        "Use your code interpreter tool to run the Python below exactly as written, in ONE execution. "
        "Do not modify, split, or retry it, and do not run any other code. Then reply with a 1-3 sentence "
        "plain-language summary of the printed result, followed by the exact JSON the tool printed in a "
        "```json fenced block. If the execution fails, reply with the exact error message instead.\n\n"
        f"```python\n{capacity_code(dataset.filename)}\n```"
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class SizeResult:
    """One dataset size: a single attempt, never retried."""

    rows: int
    size_bytes: int
    status: Status
    category: str | None = None
    detail: str = ""
    seconds: float | None = None
    response_id: str | None = None
    tokens: dict[str, int] | None = None
    containers: list[str] = field(default_factory=list)
    container: dict[str, JsonValue] | None = None
    sandbox: dict[str, JsonValue] | None = None
    correct: bool | None = None
    files: dict[str, int] = field(default_factory=dict)
    checks: list[list[object]] = field(default_factory=list)

    @classmethod
    def skipped(cls, dataset: Dataset, reason: str) -> SizeResult:
        return cls(dataset.rows, dataset.size_bytes, "SKIP", category="not_run", detail=reason)


def evaluate_sizes(
    datasets: Sequence[Dataset],
    attempt: Callable[[Dataset], SizeResult],
    on_result: Callable[[SizeResult], None],
) -> list[SizeResult]:
    """Run sizes in ascending order, one at a time; stop escalating after the first platform failure."""
    results: list[SizeResult] = []
    stopped: SizeResult | None = None
    for dataset in sorted(datasets, key=lambda item: item.rows):
        if dataset.rows not in DATASET_SIZES:
            raise ValueError(f"{dataset.rows} rows is not in the agreed Story 1 dataset sizes {DATASET_SIZES}.")
        if stopped is None:
            result = attempt(dataset)
            if result.status != "PASS" and result.category in STOP_CATEGORIES:
                stopped = result
        else:
            result = SizeResult.skipped(
                dataset, f"SKIP (not run after failure): {stopped.category} at {stopped.rows:,} rows"
            )
        results.append(result)
        on_result(result)
    return results


_RESOURCE_MARKERS = (
    "memoryerror",
    "out of memory",
    "oom",
    "killed",
    "no space left",
    "cannot allocate",
    "resource exhausted",
    "memory limit",
)
_QUOTA_MARKERS = ("429", "rate limit", "ratelimit", "quota", "too many requests", "tokens per minute")
_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded")


def classify_failure(text: str, default: str) -> str:
    """Map an error message to a capacity category; resource signals win over generic timeouts."""
    lowered = text.lower()
    for category, markers in (
        ("resource", _RESOURCE_MARKERS),
        ("quota", _QUOTA_MARKERS),
        ("timeout", _TIMEOUT_MARKERS),
    ):
        if any(re.search(rf"\b{re.escape(marker)}\b", lowered) for marker in markers):
            return category
    return default


def classify_exception(error: Exception) -> tuple[str, Status]:
    if isinstance(error, APITimeoutError):
        return "timeout", "FAIL"
    if isinstance(error, RateLimitError):
        return "quota", "FAIL"
    if isinstance(error, APIStatusError):
        if error.status_code in (401, 403):
            return "permission", "BLOCKED"
        if error.status_code in (408, 504):
            return "timeout", "FAIL"
        default = "service_error" if error.status_code >= 500 else "request_error"
        return classify_failure(str(error), default), "FAIL"
    if isinstance(error, APIConnectionError):
        return "connection", "FAIL"
    return classify_failure(str(error), "service_error"), "FAIL"


def parse_sandbox_result(content: bytes) -> dict[str, JsonValue]:
    """The result file the sandbox wrote; model-transcribed numbers are never the source of truth."""
    value = json.loads(content.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Sandbox result is not a JSON object.")
    shapes: dict[str, type] = {"input_sha256": str, "filtered_rows": int, "by_category": dict, "seconds": dict}
    wrong = [key for key, kind in shapes.items() if not isinstance(value.get(key), kind)]
    if wrong:
        raise ValueError(f"Sandbox result is missing or has invalid {', '.join(wrong)}.")
    return value


def aggregates_match(observed: JsonValue, expected: Mapping[str, CategoryTotals]) -> bool:
    if not isinstance(observed, dict) or sorted(observed) != sorted(expected):
        return False
    for name, totals in expected.items():
        item = observed[name]
        if not isinstance(item, dict):
            return False
        actual = [item.get("rows"), item.get("units"), item.get("revenue_cents")]
        if any(type(number) is not int for number in actual):
            return False
        if actual != [totals.rows, totals.units, totals.revenue_cents]:
            return False
    return True


def check_by_category_csv(content: bytes, expected: Mapping[str, CategoryTotals]) -> bool:
    reader = csv.reader(io.StringIO(content.decode("utf-8")))
    header = next(reader, None)
    if header != BY_CATEGORY_COLUMNS:
        return False
    try:
        observed: dict[str, JsonValue] = {
            row[0]: {"rows": int(row[1]), "units": int(row[2]), "revenue_cents": int(row[3])} for row in reader
        }
    except IndexError, ValueError:
        return False
    return aggregates_match(observed, expected)


def check_filtered_csv(content: bytes, expected_ids: Sequence[int], columns: Sequence[str]) -> dict[str, object]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    rows = list(reader)
    try:
        ids = [int(float(row["row_id"])) for row in rows]
    except KeyError, ValueError:
        ids = []
    return {
        "columns_match": list(reader.fieldnames or []) == list(columns),
        "rows": len(rows),
        "expected_rows": len(expected_ids),
        "row_ids_match": ids == list(expected_ids),
        "all_rows_match_filter": all(
            row.get("status") == FILTER_STATUS and row.get("region") == FILTER_REGION for row in rows
        ),
    }


def png_dimensions(content: bytes) -> tuple[int, int] | None:
    if content[:8] != PNG_SIGNATURE or content[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", content[16:24])
    return width, height


def fenced_json(text: str) -> JsonValue:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        return None
    try:
        value: JsonValue = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value


def human_bytes(value: object) -> str:
    if type(value) is not int or value < 0:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{value} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def kib_text(value: JsonValue) -> str:
    return human_bytes(value * 1024) if type(value) is int else "-"


def seconds_text(value: object) -> str:
    return f"{value:.2f}" if isinstance(value, int | float) and not isinstance(value, bool) else "-"


def tokens_text(tokens: Mapping[str, int] | None) -> str:
    if not tokens:
        return "not reported"
    parts = [
        f"{key.removesuffix('_tokens')} {tokens[key]:,}" for key in ("input_tokens", "output_tokens") if key in tokens
    ]
    return ", ".join(parts) or json.dumps(dict(tokens))


def cgroup_memory_bytes(value: JsonValue) -> int | None:
    """An enforced cgroup memory limit in bytes; ``max`` and the v1 sentinel mean unlimited."""
    if not isinstance(value, str) or not value.isdigit():
        return None
    limit = int(value)
    return limit if limit < 2**60 else None


def cgroup_memory_text(value: JsonValue) -> str:
    limit = cgroup_memory_bytes(value)
    if limit is not None:
        return human_bytes(limit)
    return "unlimited" if isinstance(value, str) and value else "-"


def cgroup_cpu_text(value: JsonValue) -> str:
    """Render ``quota period`` (cgroup v2 cpu.max, or v1 quota and period) as effective CPUs."""
    if not isinstance(value, str) or not value:
        return "-"
    parts = value.split()
    if parts[0] in ("max", "-1"):
        return "unlimited"
    if len(parts) == 2 and all(part.isdigit() for part in parts) and int(parts[1]):
        return f"{int(parts[0]) / int(parts[1]):g} CPU ({parts[0]}/{parts[1]} us)"
    return f"quota {value} (period unknown)"


def memory_headroom(result: SizeResult) -> str:
    peak = sandbox_value(result, "peak_rss_kb")
    limit = cgroup_memory_bytes(sandbox_value(result, "cgroup_memory_max"))
    if type(peak) is not int or limit is None:
        return "not measured"
    return f"peak RSS {kib_text(peak)} of {human_bytes(limit)} enforced limit ({peak * 1024 / limit:.0%})"


def sandbox_value(result: SizeResult, key: str) -> JsonValue:
    return result.sandbox.get(key) if result.sandbox else None


def sandbox_seconds(result: SizeResult, key: str) -> JsonValue:
    seconds = sandbox_value(result, "seconds")
    return seconds.get(key) if isinstance(seconds, dict) else None


def capacity_verdict(results: Sequence[SizeResult]) -> tuple[str, str]:
    """A scoped conclusion for the tested sizes only; no SLA is implied because none was agreed."""
    if not results or all(result.status == "SKIP" for result in results):
        return "Not evaluated", "No dataset size was run, so no capacity conclusion can be drawn."
    passed = [result for result in results if result.status == "PASS"]
    failure = next((result for result in results if result.status in ("FAIL", "BLOCKED")), None)
    largest = max(passed, key=lambda result: result.rows) if passed else None
    envelope = (
        f"up to {largest.rows:,} rows ({human_bytes(largest.size_bytes)})" if largest else "for none of the sizes"
    )
    if failure is None and len(passed) == len(results):
        return "Met", (
            f"The tested workload is viable in the evaluated configuration {envelope}: every size produced exact "
            "results and all output files. This is the tested envelope, not the platform maximum."
        )
    if failure is None:
        return "Partially met", f"Viable {envelope}; remaining sizes were not run."
    if failure.status == "BLOCKED":
        return "Not evaluated", (
            f"Blocked at {failure.rows:,} rows ({failure.category}) before a capacity conclusion; viable {envelope}."
        )
    if failure.category in STOP_CATEGORIES:
        return "Met, with an observed limit", (
            f"Viable in the evaluated configuration {envelope}. The single attempt at {failure.rows:,} rows "
            f"({human_bytes(failure.size_bytes)}) failed with `{failure.category}`, so larger sizes were not run. "
            "One attempt is evidence of a limit in this configuration, not a proven platform maximum."
        )
    return "Partially met", (
        f"Viable {envelope}. The completed response at {failure.rows:,} rows did not produce verifiable outputs "
        f"(`{failure.category}`), which is a correctness finding rather than a capacity limit; rerun to conclude."
    )


def generated_files_by_name(
    client: httpx.Client, project_endpoint: str, token: str, container: str
) -> dict[str, dict[str, JsonValue]]:
    listed = list_generated_files(client, project_endpoint, token, container)
    return {Path(str(item.get("path", ""))).name: item for item in listed}


def select_container(
    client: httpx.Client, project_endpoint: str, token: str, containers: Sequence[str], names: Sequence[str]
) -> tuple[str, dict[str, dict[str, JsonValue]]] | None:
    """The newest referenced container holding the most of ``names``; None when none holds any."""
    best: tuple[int, str, dict[str, dict[str, JsonValue]]] | None = None
    for container in reversed(containers):
        files = generated_files_by_name(client, project_endpoint, token, container)
        score = sum(name in files for name in names)
        if score and (best is None or score > best[0]):
            best = (score, container, files)
    return (best[1], best[2]) if best else None


def container_details(client: httpx.Client, project_endpoint: str, token: str, container: str) -> dict[str, JsonValue]:
    reply = client.get(f"{project_endpoint}/openai/v1/containers/{container}", headers=bearer(token))
    if reply.status_code != 200:
        return {"id": container, "http_status": reply.status_code}
    info = object_value(reply.json())
    return {"id": container, **{key: info.get(key) for key in ("memory_limit", "expires_after")}}


def download_files(
    client: httpx.Client,
    project_endpoint: str,
    token: str,
    container: str,
    files: Mapping[str, Mapping[str, JsonValue]],
    names: Sequence[str],
    local: Path,
) -> dict[str, tuple[bytes, str | None]]:
    """Download the named sandbox files with the caller's own identity and keep local report copies."""
    content: dict[str, tuple[bytes, str | None]] = {}
    for name in names:
        item = files.get(name)
        if item is None:
            continue
        download = client.get(
            f"{project_endpoint}/openai/v1/containers/{container}/files/{item['id']}/content", headers=bearer(token)
        )
        download.raise_for_status()
        content[name] = (download.content, download.headers.get("content-type"))
        local.mkdir(parents=True, exist_ok=True)
        (local / name).write_bytes(download.content)
    return content


class ReusingStep:
    """Shared reuse guard for Steps 2-5: compare with the Step 1 baseline and never change anything."""

    spec: StepSpec
    title = ""

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        self.args = args
        self.run = run
        self.report = report
        self.datasets = [build_dataset(size) for size in DATASET_SIZES]
        self.toolbox: ToolboxRecord | None = None
        self.statuses: dict[str, Status] = {}
        self.verdict = "Not evaluated"
        self.project_endpoint = ""

    def observe(self, step: str, status: Status, expected: str, observed: object) -> None:
        self.statuses[step] = status
        self.run.result(step, status, expected=expected, observed=observed)

    def record(self, step: str, status: Status, expected: str, observed: object) -> None:
        self.observe(step, status, expected, observed)
        self.report.table(
            ["Check", "Status", "Expected", "Observed"], [[step, status_icon(status), expected, observed]]
        )

    def execute(self) -> None:
        raise NotImplementedError

    def conclude(self) -> None:
        raise NotImplementedError

    def guard_start(
        self, project: AIProjectClient, read: Callable[[str], Mapping[str, JsonValue]]
    ) -> dict[str, AgentSnapshot] | None:
        toolbox, baseline = load_baseline(self.args.output_dir / BASELINE_FILE, self.args.environment)
        self.report.heading("Agents reused - before")
        before = snapshot_agents(read)
        self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in before.values()])
        blockers, differences = baseline_problems(baseline, before, toolbox)
        if blockers:
            raise Blocked("; ".join(blockers) + ". Run Task 1 (Step 1) first; this Task never changes agents.")
        if differences:
            self.record(
                "Agents reused - before",
                "FAIL",
                f"Identical to the Step 1 baseline (run {self.baseline_run_id()})",
                differences,
            )
            return None
        try:
            versions = [object_value(item.as_dict()) for item in project.toolboxes.list_versions(TOOLBOX_NAME)]
        except ResourceNotFoundError:
            versions = []
        recorded = [item for item in versions if str(item.get("version")) == toolbox.version]
        match = matching_toolbox(recorded, self.datasets)
        if match is None or any(match[str(d.rows)] != toolbox.file_ids.get(str(d.rows)) for d in self.datasets):
            raise Blocked(
                f"Toolbox {TOOLBOX_NAME} v{toolbox.version} no longer records these synthetic inputs; run Task 1."
            )
        self.record(
            "Agents reused - before",
            "PASS",
            f"Identical to the Step 1 baseline (run {self.baseline_run_id()}); toolbox {TOOLBOX_NAME} "
            f"v{toolbox.version} records the same input checksums",
            {name: snapshot.version for name, snapshot in before.items()},
        )
        self.toolbox = toolbox
        return before

    def guard_end(self, read: Callable[[str], Mapping[str, JsonValue]], before: Mapping[str, AgentSnapshot]) -> None:
        self.report.heading("Agents reused - after")
        after = snapshot_agents(read)
        self.report.table(SNAPSHOT_HEADERS, [snapshot.row() for snapshot in after.values()])
        differences = reuse_differences(before, after, version_change=None)
        self.record(
            "Agents reused - after",
            "FAIL" if differences else "PASS",
            "Same three agents, versions, identities, and toolbox wiring as at the start",
            differences or "No changes",
        )

    def baseline_run_id(self) -> str:
        path = self.args.output_dir / BASELINE_FILE
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("run_id", "unknown"))[:8]
        except OSError, ValueError:
            return "unknown"


class Step3(ReusingStep):
    """Progressively larger datasets on the existing Data Analysis agent; strictly read-only."""

    spec = STEP3
    title = "Story 1 / Step 3 - Progressively larger datasets"

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        super().__init__(args, run, report)
        self.results: list[SizeResult] = []
        self.conclusion = ""

    def execute(self) -> None:
        self.report.paragraph(f"**AC3 (verbatim from GitHub issue #1):** {AC3}")
        self.report.paragraph(
            "This step **reuses the existing agents and the Step 1 toolbox read-only**. It never deploys, uploads, "
            "creates a toolbox, or changes an agent. Each size is one request in a fresh conversation, sent "
            "sequentially, with no automatic retries."
        )
        self.report.table(
            ["Field", "Value"],
            [
                ["Task", STEP3_TASK_LABEL],
                ["Run ID", self.run.run_id],
                ["UTC time", datetime.now(UTC).isoformat(timespec="seconds")],
                ["Environment", self.args.environment],
                ["Client timeout per request", f"{self.args.timeout:g} s"],
                ["Retries", "0 (client max_retries=0; one attempt per size)"],
                ["Concurrency", "1 (sizes run in ascending order)"],
            ],
        )
        context = read_environment(self.args.environment, self.report)
        self.report.heading("Host and sandbox settings")
        settings: list[list[object]] = [["Host (hosted agent)", name, value] for name, value in HOST_SETTINGS]
        settings += [
            ["Sandbox (managed Code Interpreter)", "Per-request memory_limit", "Platform default (not overridden)"],
            ["Sandbox (managed Code Interpreter)", "Inputs", "Mounted by toolbox file_ids under /mnt/data"],
        ]
        self.report.table(["Layer", "Setting", "Value"], settings)
        self.report.paragraph(
            "The analysis runs in the sandbox, not in the hosted agent container; the two layers are reported "
            "separately and their limits are not assumed to be the same."
        )
        endpoint = str(context["FOUNDRY_PROJECT_ENDPOINT"]).rstrip("/")
        self.project_endpoint = endpoint
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
        ):

            def read(name: str) -> Mapping[str, JsonValue]:
                return object_value(project.agents.get(name).as_dict())

            before = self.guard_start(project, read)
            if before is None:
                return
            self.reproduction()
            self.report.heading("Results by size")
            self.results = evaluate_sizes(
                self.datasets,
                lambda dataset: self.attempt(project, lambda: credential.get_token(DATA_SCOPE).token, dataset),
                self.record_size,
            )
            self.guard_end(read, before)

    def reproduction(self) -> None:
        self.report.heading("Reproduction")
        self.report.table(
            ["Dataset", "Rows", "Bytes", "SHA-256", "Expected filtered rows", "Toolbox file ID"],
            [
                [
                    d.filename,
                    d.rows,
                    d.size_bytes,
                    d.sha256,
                    len(expected_filtered_ids(generate_rows(d.rows))),
                    self.toolbox.file_ids.get(str(d.rows)) if self.toolbox else "-",
                ]
                for d in self.datasets
            ],
        )
        self.report.paragraph(
            f"Workload (same as Step 2): filter `status == '{FILTER_STATUS}' and region == '{FILTER_REGION}'`, group "
            "by category with row count and integer sums of units and revenue_cents (blanks ignored), and a bar "
            "chart of revenue_cents by category. To keep data size the only variable, the agent is asked to run "
            "this fixed Python verbatim; only the input file name changes. Data never passes through the prompt."
        )
        self.report.paragraph(
            f"The sandbox writes `{RESULT_FILE}` with its measurements. The runner downloads it through the "
            "authenticated container files API and compares it with an independent local calculation. Numbers "
            "the model writes in its answer are compared for information only."
        )
        self.report.paragraph("Exact request sent for the 1,000-row size:")
        self.report.code(capacity_prompt(self.datasets[0]), "text")

    def attempt(self, project: AIProjectClient, token: Callable[[], str], dataset: Dataset) -> SizeResult:
        result = SizeResult(dataset.rows, dataset.size_bytes, "FAIL")
        rows = list(generate_rows(dataset.rows))
        expected = expected_aggregates(rows)
        expected_ids = expected_filtered_ids(rows)
        number = self.run.start_request(DATA_ANALYSIS, "hosted", f"Step 3 workload on {dataset.filename}")
        started = perf_counter()
        try:
            try:
                with project.get_openai_client(
                    agent_name=DATA_ANALYSIS, timeout=self.args.timeout, max_retries=0
                ) as client:
                    response = client.responses.create(input=capacity_prompt(dataset), stream=False)
                payload = response.model_dump(mode="json")
            except (OpenAIError, AzureError) as error:
                result.category, result.status = classify_exception(error)
                result.detail = sanitize_text(str(error))[:500]
                return result
            finally:
                result.seconds = round(perf_counter() - started, 2)
            summary = response_summary(payload)
            text = str(summary.get("text") or "")
            usage = summary.get("usage")
            result.tokens = {k: v for k, v in usage.items() if type(v) is int} if isinstance(usage, dict) else None
            result.response_id = str(summary.get("id") or summary.get("response_id") or "") or None
            if summary.get("status") != "completed":
                detail = json.dumps(summary.get("error")) if summary.get("error") else text
                result.category = classify_failure(detail, "agent_error")
                result.detail = sanitize_text(f"Response status {summary.get('status')}: {detail}")[:500]
                return result
            result.containers = parse_artifacts(text)
            if not result.containers:
                result.category = classify_failure(text, "no_execution")
                result.detail = sanitize_text(f"Answer has no `{ARTIFACTS_FENCE}` container reference: {text}")[:500]
                return result
            try:
                self.collect(result, dataset, expected, expected_ids, text, token())
            except (httpx.HTTPError, AzureError, ValueError, UnicodeDecodeError) as error:
                result.status, result.category = "FAIL", "output_error"
                result.detail = sanitize_text(f"Output retrieval failed: {error}")[:500]
            return result
        finally:
            self.run.end_request(number, DATA_ANALYSIS, result.status, (perf_counter() - started) * 1000)

    def collect(
        self,
        result: SizeResult,
        dataset: Dataset,
        expected: Mapping[str, CategoryTotals],
        expected_ids: Sequence[int],
        text: str,
        token: str,
    ) -> None:
        """Download the sandbox's own result and outputs, then compare them with the local oracle."""
        local = self.run.path / "outputs" / str(dataset.rows)
        with httpx.Client(timeout=120, trust_env=False) as client:
            selected = select_container(client, self.project_endpoint, token, result.containers, [RESULT_FILE])
            if selected is None:
                result.category = classify_failure(text, "no_execution")
                result.detail = sanitize_text(f"No container holds {RESULT_FILE}; answer: {text}")[:500]
                return
            container, files = selected
            result.container = container_details(client, self.project_endpoint, token, container)
            downloaded = download_files(
                client, self.project_endpoint, token, container, files, [RESULT_FILE, *OUTPUT_FILES], local
            )
        content = {name: data for name, (data, _) in downloaded.items()}
        result.files = {name: len(data) for name, data in content.items() if name != RESULT_FILE}
        sandbox = parse_sandbox_result(content[RESULT_FILE])
        result.sandbox = sandbox

        def check(name: str, ok: bool, expected_text: str, observed: object) -> bool:
            result.checks.append([name, "PASS" if ok else "FAIL", expected_text, observed])
            return ok

        missing = [name for name in OUTPUT_FILES if name not in content]
        dimensions = png_dimensions(content.get("revenue_by_category.png", b""))
        filtered = check_filtered_csv(content.get("filtered.csv", b""), expected_ids, COLUMNS)
        outcomes = [
            check(
                "Sandbox read the exact input",
                sandbox["input_sha256"] == dataset.sha256,
                dataset.sha256,
                sandbox["input_sha256"],
            ),
            check(
                "Filter",
                sandbox["filtered_rows"] == len(expected_ids),
                f"filtered_rows == {len(expected_ids)}",
                sandbox["filtered_rows"],
            ),
            check(
                "Group and summarize",
                aggregates_match(sandbox["by_category"], expected),
                "Every category matches the local oracle exactly (integer cents)",
                sandbox["by_category"],
            ),
            check("All output files generated", not missing, ", ".join(OUTPUT_FILES), missing or "all present"),
            check(
                "by_category.csv",
                check_by_category_csv(content.get("by_category.csv", b""), expected),
                "Columns and values match the local oracle",
                human_bytes(result.files.get("by_category.csv")),
            ),
            check(
                "filtered.csv",
                all(v is True for v in filtered.values() if isinstance(v, bool)),
                f"{len(expected_ids)} rows, original columns, exact row IDs",
                filtered,
            ),
            check(
                "revenue_by_category.png",
                dimensions is not None,
                "Valid PNG",
                f"{dimensions[0]}x{dimensions[1]}" if dimensions else "invalid",
            ),
        ]
        transcribed = fenced_json(text)
        result.checks.append(
            [
                "Answer JSON equals sandbox result (informational)",
                "INFO",
                "Model copied the printed JSON unchanged",
                "identical" if transcribed == sandbox else "differs or missing; the sandbox file is authoritative",
            ]
        )
        result.correct = all(outcomes)
        result.status = "PASS" if result.correct else "FAIL"
        result.category = None if result.correct else ("output_error" if missing else "incorrect")
        if not result.correct:
            result.detail = "; ".join(str(item[0]) for item in result.checks if item[1] == "FAIL")
        if len(result.containers) > 1:
            result.detail = (result.detail + "; " if result.detail else "") + (
                f"{len(result.containers)} sandbox executions (asked for one)"
            )

    def record_size(self, result: SizeResult) -> None:
        self.report.heading(f"{result.rows:,} rows", level=3)
        expected = "Exact results and all output files from one sandbox execution"
        observed = {
            key: value
            for key, value in asdict(result).items()
            if key not in {"checks"} and value not in (None, "", [], {})
        }
        self.run.result(f"{result.rows:,} rows", result.status, expected=expected, observed=observed)
        self.report.table(
            ["Field", "Value"],
            [
                ["Outcome", result.status if result.category is None else f"{result.status} ({result.category})"],
                ["End-to-end seconds", seconds_text(result.seconds)],
                ["Response ID", result.response_id or "-"],
                ["Sandbox container", result.container or (result.containers or "-")],
                ["Tokens", tokens_text(result.tokens)],
                ["Detail", result.detail or "-"],
            ],
        )
        if result.checks:
            self.report.table(
                ["Check", "Status", "Expected", "Observed"],
                [[name, status_icon(str(status)), *rest] for name, status, *rest in result.checks],
            )
        if result.files:
            self.report.paragraph(
                "Local copies: "
                + ", ".join(
                    f"[{name}](outputs/{result.rows}/{name})"
                    for name in (*OUTPUT_FILES, RESULT_FILE)
                    if name in result.files or name == RESULT_FILE
                )
            )
        if "revenue_by_category.png" in result.files:
            self.report.paragraph(
                f"![Revenue by category, {result.rows:,} rows](outputs/{result.rows}/revenue_by_category.png)"
            )

    def conclude(self) -> None:
        """Always write the summary, even for a partial or blocked run."""
        done = {result.rows for result in self.results}
        for dataset in self.datasets:
            if dataset.rows not in done:
                skipped = SizeResult.skipped(dataset, "SKIP (not run): the run stopped before this size")
                self.results.append(skipped)
                self.run.result(f"{dataset.rows:,} rows", "SKIP", expected="Size evaluated", observed=skipped.detail)
        self.results.sort(key=lambda result: result.rows)
        self.verdict, self.conclusion = capacity_verdict(self.results)
        self.report.heading("Capacity summary")
        self.report.table(
            [
                "Rows",
                "Bytes",
                "Correct?",
                "Sandbox time (s)",
                "End-to-end (s)",
                "Peak RSS",
                "CPU count",
                "Memory total",
                "Sandbox memory limit",
                "Tokens",
                "Files",
                "Outcome",
                "Failure detail",
            ],
            [
                [
                    f"{r.rows:,}",
                    human_bytes(r.size_bytes),
                    "-" if r.correct is None else ("yes" if r.correct else "no"),
                    seconds_text(sandbox_seconds(r, "total")),
                    seconds_text(r.seconds),
                    kib_text(sandbox_value(r, "peak_rss_kb")),
                    sandbox_value(r, "cpu_count") or "-",
                    kib_text(sandbox_value(r, "mem_total_kb")),
                    (r.container or {}).get("memory_limit") or "-",
                    tokens_text(r.tokens),
                    "; ".join(f"{name} {human_bytes(size)}" for name, size in r.files.items()) or "-",
                    r.status if r.category is None else f"{r.status} ({r.category})",
                    r.detail or "-",
                ]
                for r in self.results
            ],
        )
        measured = [r for r in self.results if r.sandbox]
        if measured:
            self.report.heading("Sandbox observations")
            self.report.paragraph(
                "Measured inside the sandbox by the fixed workload. `MemTotal` and `os.cpu_count()` can reflect the "
                "sandbox host rather than what is enforced; the container API `memory_limit` and the cgroup values "
                "are the enforced limits."
            )
            self.report.table(
                [
                    "Rows",
                    "Hash (s)",
                    "Import (s)",
                    "Load (s)",
                    "Filter + group (s)",
                    "Plot (s)",
                    "Peak RSS",
                    "Enforced memory (cgroup)",
                    "Enforced CPU (cgroup)",
                    "Disk free",
                    "Python / pandas",
                ],
                [
                    [
                        f"{r.rows:,}",
                        *(
                            seconds_text(sandbox_seconds(r, key))
                            for key in ("hash", "import", "load", "filter_group", "plot")
                        ),
                        kib_text(sandbox_value(r, "peak_rss_kb")),
                        cgroup_memory_text(sandbox_value(r, "cgroup_memory_max")),
                        cgroup_cpu_text(sandbox_value(r, "cgroup_cpu_max")),
                        human_bytes(sandbox_value(r, "disk_free_bytes")),
                        f"{sandbox_value(r, 'python')} / {sandbox_value(r, 'pandas')}",
                    ]
                    for r in measured
                ],
            )
        passed = [r for r in self.results if r.status == "PASS"]
        failure = next((r for r in self.results if r.status in ("FAIL", "BLOCKED")), None)
        self.report.table(
            ["Item", "Value"],
            [
                [
                    "Largest successful size",
                    f"{passed[-1].rows:,} rows ({human_bytes(passed[-1].size_bytes)})" if passed else "none",
                ],
                [
                    "Sandbox memory at largest successful size",
                    memory_headroom(passed[-1]) if passed else "not measured",
                ],
                [
                    "First failure",
                    f"{failure.rows:,} rows: {failure.category} - {failure.detail}" if failure else "none",
                ],
                ["Viability (scoped to the tested sizes and configuration)", self.conclusion],
            ],
        )
        self.write_summary()

    def write_summary(self) -> None:
        passed = [r for r in self.results if r.status == "PASS"]
        failure = next((r for r in self.results if r.status in ("FAIL", "BLOCKED")), None)
        tested = ", ".join(f"{r.rows:,}" for r in passed) or "none"
        largest = passed[-1] if passed else None
        results = [
            f"{r.rows:,} rows ({human_bytes(r.size_bytes)}): "
            + (
                f"exact results and all files; {seconds_text(sandbox_seconds(r, 'total'))} s in the sandbox, "
                f"{seconds_text(r.seconds)} s end to end, peak memory {kib_text(sandbox_value(r, 'peak_rss_kb'))}"
                if r.status == "PASS"
                else f"{r.status} ({r.category}) - {r.detail}"
            )
            for r in self.results
        ]
        if largest:
            results.append(f"Memory at the largest size: {memory_headroom(largest)}.")
            results.append(
                f"Enforced sandbox limits: {cgroup_memory_text(sandbox_value(largest, 'cgroup_memory_max'))} memory, "
                f"{cgroup_cpu_text(sandbox_value(largest, 'cgroup_cpu_max'))}; the sandbox reports "
                f"{sandbox_value(largest, 'cpu_count')} CPUs to Python."
            )
        timings = [
            value
            for value in (sandbox_seconds(r, "total") for r in passed)
            if isinstance(value, int | float) and not isinstance(value, bool)
        ]
        findings = [
            Finding(
                "Filter, group, summarize, and chart run in the managed sandbox as data size grows",
                f"Exact results at {tested} rows"
                + (f"; first failure at {failure.rows:,} rows ({failure.category})" if failure else ""),
                "Tested",
                "Foundry-managed",
                ("ci_overview",),
                outcome=checks_outcome(*(r.status for r in self.results)),
            ),
            Finding(
                "The sandbox enforces its own memory and CPU limits, separate from the hosted agent container",
                (
                    f"Sandbox limit: {cgroup_memory_text(sandbox_value(largest, 'cgroup_memory_max'))} memory and "
                    f"{cgroup_cpu_text(sandbox_value(largest, 'cgroup_cpu_max'))}"
                    if largest
                    else "Sandbox limits were not measured because no size completed"
                )
                + ". The host container has its own 0.5 vCPU / 1 GiB (azure.yaml); the analysis never runs there",
                "Observed",
                "Foundry-managed",
                ("hosted_sizes", "ci_sandbox"),
                outcome="LIMITATION" if largest else "NOT RUN",
            ),
            Finding(
                "Larger sandbox memory (memory_limit 1g/4g/16g/64g) is a documented option",
                "Microsoft lists these sizes. Not tested in this step (the default was used); Step 4 found that 4g "
                "was not applied in this project",
                "Documented",
                "Foundry-managed",
                ("sdk_memory",),
                outcome="DOCUMENTED",
            ),
            Finding(
                "Input data is mounted as files and never passes through the model prompt",
                "The prompt was the same length for every size; only the mounted file changed. Token usage is not "
                "reported by the hosted response",
                "Tested",
                "Custom implementation (Dev Team)",
                ("files_upload", "toolbox_overview"),
                outcome="PASS" if passed else "NOT RUN",
            ),
            Finding(
                "A single code execution has a documented maximum runtime",
                (
                    f"The slowest size used {max(timings):.2f} s of sandbox time"
                    if timings
                    else "No sandbox timings were measured"
                )
                + "; Microsoft documents a 220 s maximum per execution (Step 4 tests it)",
                "Documented",
                "Foundry-managed",
                ("aca_runtime",),
                outcome="LIMITATION",
            ),
        ]
        gaps = [
            "One attempt per size, sequential, no concurrency; timings are observations, not an SLA (none was agreed).",
            "Only the agreed 1k/10k/100k sizes were tested; passing them shows the tested envelope, not the platform "
            "maximum. The run does not search for a crash limit.",
            "The agent ran fixed Python to isolate data size; Step 2 covers model-written analysis code.",
            "Upload size limits for toolbox file_ids beyond the 5.6 MB input were not tested.",
        ]
        self.report.summary(render_summary(self.verdict, self.conclusion, results, findings, gaps))


def load_baseline(path: Path, environment: str) -> tuple[ToolboxRecord, dict[str, AgentSnapshot]]:
    """The Step 1 reuse baseline; Steps 2-5 never recreate it."""
    if not path.is_file():
        raise Blocked(f"Reuse baseline {path.name} is missing. Run Task 1 (Step 1) first.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        toolbox = ToolboxRecord(**data["toolbox"])
        agents = {name: AgentSnapshot(**value) for name, value in data["agents"].items()}
        recorded_environment = data["environment"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Blocked(f"Reuse baseline {path.name} is unreadable ({error}). Run Task 1 (Step 1) again.") from error
    if recorded_environment != environment:
        raise Blocked(
            f"Reuse baseline was recorded for '{recorded_environment}', not '{environment}'. Run Task 1 for this "
            "environment."
        )
    return toolbox, agents


def baseline_problems(
    baseline: Mapping[str, AgentSnapshot], current: Mapping[str, AgentSnapshot], toolbox: ToolboxRecord
) -> tuple[list[str], list[str]]:
    """Blockers mean Step 1 must run; differences mean something changed the reused agents unexpectedly."""
    blockers = [
        f"{snapshot.name} is not active with @latest routing"
        for snapshot in current.values()
        if snapshot.status != "active" or not snapshot.latest_routing
    ]
    data_analysis = current.get(DATA_ANALYSIS)
    if data_analysis is None or data_analysis.toolbox_endpoint != toolbox.endpoint:
        blockers.append(f"{DATA_ANALYSIS} is not wired to the Step 1 toolbox")
    if blockers:
        return blockers, []
    return [], reuse_differences(baseline, current, version_change=None)


STEP2 = StepSpec(1, 2, "analysis-and-outputs")
STEP2_TASK_LABEL = "Agents: Story 1 - 2 Analyze data and deliver outputs"
AC2 = (
    "Demonstrate filtering, grouping, summarization, and visualization using synthetic tabular data. "
    "Show how summaries, files, and visuals reach the supervisor or user."
)
SUPERVISOR = SERVICES["supervisor"]
SUPERVISOR_CHECK = "Supervisor -> Research -> Data Analysis (POC_WORKFLOW create)"
SUPERVISOR_EXPECTED = (
    "Supervisor calls Research, then Data Analysis; BLOCKED means the known nested-user-ID guard (ADR-0001) "
    "stopped a hop, and it is left unchanged"
)
# The missing-user-ID check of the nested caller guard (ADR-0001); hitting it is a known blocker, not a failure.
NESTED_GUARD_MARKER = "user_id_present"
FILE_EXPECTED = "Authenticated download validates against the local oracle"
FORWARD_CHECK = "Files and chart forwarded through the Supervisor"
FORWARD_EXPECTED = (
    "The Supervisor's poc-workflow-v1 report carries the Data Analysis container IDs; every output file downloads "
    "with the caller's identity and validates against the local oracle"
)
USER_PATH_CHECKS = (
    "Hosted analysis request",
    "Sandbox read the exact input",
    "Filter",
    "Group and summarize",
    "Container reference returned",
    *OUTPUT_FILES,
)
ANALYSIS_CHECKS = ("Sandbox read the exact input", "Filter", "Group and summarize")


def analysis_prompt(dataset: Dataset) -> str:
    """The model writes the pandas/matplotlib code; only its outputs are checked."""
    return (
        "Use your code interpreter tool in ONE execution, with pandas and matplotlib (Agg backend), on the file in "
        f"/mnt/data whose name ends with '{dataset.filename}':\n"
        f"1. Filter: keep rows where status == '{FILTER_STATUS}' and region == '{FILTER_REGION}'. Save them with all "
        "original columns, in their original order, to /mnt/data/filtered.csv (index=False).\n"
        "2. Group the filtered rows by category, sorted by category. Per category compute rows (row count, including "
        "rows with blank units), units (sum, blanks ignored), and revenue_cents (sum, blanks ignored), all as Python "
        "int. Save exactly the columns category,rows,units,revenue_cents to /mnt/data/by_category.csv (index=False, "
        "no total row).\n"
        "3. Visualize: save a bar chart of revenue_cents by category to /mnt/data/revenue_by_category.png.\n"
        '4. Print one JSON object: {"input_sha256": "<SHA-256 hex of the input file bytes>", "filtered_rows": <int>, '
        '"by_category": {"<category>": {"rows": <int>, "units": <int>, "revenue_cents": <int>}}, "total": '
        '{"rows": <int>, "units": <int>, "revenue_cents": <int>}}.\n'
        "Then reply with a 2-4 sentence plain-language summary of what the numbers show, followed by the exact JSON "
        "the tool printed in a ```json fenced block."
    )


def total_of(expected: Mapping[str, CategoryTotals]) -> CategoryTotals:
    return CategoryTotals(
        sum(t.rows for t in expected.values()),
        sum(t.units for t in expected.values()),
        sum(t.revenue_cents for t in expected.values()),
    )


def answer_summary(text: str) -> str:
    """The plain-language part of an answer, without fenced JSON or artifact blocks."""
    return re.sub(r"`{3,}.*?`{3,}", "", text, flags=re.S).strip()


def int_triplet(value: JsonValue) -> list[JsonValue] | None:
    if not isinstance(value, dict):
        return None
    numbers = [value.get("rows"), value.get("units"), value.get("revenue_cents")]
    return numbers if all(type(number) is int for number in numbers) else None


def by_category_rows(content: bytes) -> dict[str, list[int]]:
    """Observed per-category values from by_category.csv, for the expected-versus-observed table."""
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
        return {
            row["category"]: [int(row["rows"]), int(row["units"]), int(row["revenue_cents"])]
            for row in reader
            if row.get("category")
        }
    except KeyError, ValueError, UnicodeDecodeError:
        return {}


def filtered_revenue(content: bytes) -> int | None:
    try:
        rows = csv.DictReader(io.StringIO(content.decode("utf-8")))
        return sum(int(float(row["revenue_cents"])) for row in rows if row.get("revenue_cents"))
    except KeyError, ValueError, UnicodeDecodeError:
        return None


def supervisor_outcome(
    payload: Mapping[str, JsonValue], expected: Mapping[str, CategoryTotals]
) -> tuple[Status, dict[str, object]]:
    """Classify the Supervisor workflow response without weakening the nested caller guard."""
    status = payload.get("status")
    error = payload.get("error")
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    if status != "completed":
        hop = re.match(r"(research|data_analysis) hosted response", message)
        observed: dict[str, object] = {
            "status": status,
            "failed_hop": hop.group(1) if hop else None,
            "error": sanitize_text(message)[:500],
        }
        return ("BLOCKED" if NESTED_GUARD_MARKER in message else "FAIL"), observed
    text = str(response_summary(dict(payload)).get("text") or "")
    try:
        report = Report.model_validate_json(text)
    except ValidationError:
        return "FAIL", {"status": status, "error": "Completed response is not a poc-workflow-v1 report"}
    printed = fenced_json(report.answer)
    matches = isinstance(printed, dict) and aggregates_match(printed.get("by_category"), expected)
    child = report.children.get("data_analysis")
    return ("PASS" if matches else "FAIL"), {
        "status": status,
        "workflow_id": str(report.workflow_id),
        "children": sorted(report.children),
        "aggregates_match": matches,
        "containers": list(report.containers),
        "data_analysis_containers": list(child.containers) if child else [],
        "files_forwarded": bool(report.containers),
    }


def step2_verdict(statuses: Mapping[str, Status]) -> str:
    """AC2 accepts delivery to the supervisor or the user; the user path decides the verdict."""
    request = statuses.get("Hosted analysis request")
    if request is None or request == "BLOCKED":
        return "Not evaluated"
    if statuses.get("Agents reused - after") == "FAIL" or statuses.get("Unexpected failure") == "FAIL":
        return "Not met"
    if all(statuses.get(name) == "PASS" for name in USER_PATH_CHECKS):
        return "Met"
    return "Partially met" if request == "PASS" else "Not met"


class Step2(ReusingStep):
    """Analysis operations and output delivery on the existing Data Analysis agent; never changes agents."""

    spec = STEP2
    title = "Story 1 / Step 2 - Analysis operations and output delivery"

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        super().__init__(args, run, report)
        self.dataset = self.datasets[0]
        rows = list(generate_rows(self.dataset.rows))
        self.expected = expected_aggregates(rows)
        self.expected_ids = expected_filtered_ids(rows)
        self.total = total_of(self.expected)
        self.obs: dict[str, Any] = {}

    def execute(self) -> None:
        self.report.paragraph(f"**AC2 (verbatim from GitHub issue #1):** {AC2}")
        self.report.paragraph(
            "This step **reuses the existing agents and the Step 1 toolbox read-only**. It never deploys, uploads, "
            "creates a toolbox, or changes an agent."
        )
        self.report.table(
            ["Field", "Value"],
            [
                ["Task", STEP2_TASK_LABEL],
                ["Run ID", self.run.run_id],
                ["UTC time", datetime.now(UTC).isoformat(timespec="seconds")],
                ["Environment", self.args.environment],
            ],
        )
        context = read_environment(self.args.environment, self.report)
        endpoint = str(context["FOUNDRY_PROJECT_ENDPOINT"]).rstrip("/")
        self.project_endpoint = endpoint
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
        ):

            def read(name: str) -> Mapping[str, JsonValue]:
                return object_value(project.agents.get(name).as_dict())

            before = self.guard_start(project, read)
            if before is None:
                return
            self.reproduction()
            text = self.analyze(project)
            if text is not None:
                self.deliver(text, credential.get_token(DATA_SCOPE).token)
            self.supervisor(project, lambda: credential.get_token(DATA_SCOPE).token)
            self.guard_end(read, before)

    def reproduction(self) -> None:
        dataset = self.dataset
        self.report.heading("Reproduction")
        self.report.table(
            ["Input", "Value"],
            [
                ["Dataset", dataset.filename],
                ["Rows / bytes", f"{dataset.rows} / {dataset.size_bytes}"],
                ["Seed", SEED],
                ["SHA-256", dataset.sha256],
                ["Filter", f"status == '{FILTER_STATUS}' and region == '{FILTER_REGION}'"],
                ["Group / summarize", "by category: row count, sum(units), sum(revenue_cents); blanks ignored"],
                ["Visualize", "bar chart of revenue_cents by category"],
                ["Expected filtered rows", len(self.expected_ids)],
            ],
        )
        self.report.paragraph("Exact request sent to the existing Data Analysis agent:")
        self.report.code(analysis_prompt(dataset), "text")

    def check(self, step: str, ok: bool, expected: str, observed: object) -> bool:
        self.record(step, "PASS" if ok else "FAIL", expected, observed)
        return ok

    def analyze(self, project: AIProjectClient) -> str | None:
        """One hosted request; the model writes the analysis code in the managed sandbox."""
        self.report.heading("Hosted analysis (existing Data Analysis endpoint)")
        expected = "Completed response with answer text"
        number = self.run.start_request(DATA_ANALYSIS, "hosted", f"Step 2 analysis of {self.dataset.filename}")
        started = perf_counter()
        status: Status = "FAIL"
        try:
            try:
                with project.get_openai_client(
                    agent_name=DATA_ANALYSIS, timeout=self.args.timeout, max_retries=0
                ) as client:
                    response = client.responses.create(input=analysis_prompt(self.dataset), stream=False)
                summary = response_summary(response.model_dump(mode="json"))
            except (OpenAIError, AzureError) as error:
                category, status = classify_exception(error)
                self.record(
                    "Hosted analysis request",
                    status,
                    expected,
                    {
                        "category": category,
                        "error": sanitize_text(str(error))[:500],
                        "seconds": round(perf_counter() - started, 2),
                    },
                )
                return None
            text = str(summary.get("text") or "")
            completed = summary.get("status") == "completed" and bool(answer_summary(text) or text.strip())
            status = "PASS" if completed else "FAIL"
            observed: dict[str, object] = {
                "response_id": summary.get("id") or summary.get("response_id"),
                "status": summary.get("status"),
                "seconds": round(perf_counter() - started, 2),
            }
            if summary.get("error"):
                observed["error"] = summary["error"]
            self.obs["seconds"] = observed["seconds"]
            self.record("Hosted analysis request", status, expected, observed)
            return text if completed else None
        finally:
            self.run.end_request(number, DATA_ANALYSIS, status, (perf_counter() - started) * 1000)

    def deliver(self, text: str, token: str) -> None:
        """Check the summary and printed aggregates, then fetch and validate the generated files."""
        self.report.heading("Summary delivered to the user", level=3)
        summary = answer_summary(text)
        self.obs["summary"] = " ".join(summary.split())
        self.report.paragraph("\n".join(f"> {line}" for line in summary.splitlines()) if summary else "_(empty)_")
        printed = fenced_json(text)
        values = printed if isinstance(printed, dict) else {}
        self.check(
            "Sandbox read the exact input",
            values.get("input_sha256") == self.dataset.sha256,
            f"Printed input_sha256 equals {self.dataset.sha256}",
            values.get("input_sha256", "no printed JSON"),
        )
        self.check(
            "Filter",
            values.get("filtered_rows") == len(self.expected_ids),
            f"filtered_rows == {len(self.expected_ids)}",
            values.get("filtered_rows", "no printed JSON"),
        )
        self.report.heading("Group and summarize: printed JSON vs. local oracle", level=3)
        observed_groups = values.get("by_category")
        printed_total = int_triplet(values.get("total"))
        self.comparison_table(
            {name: int_triplet(item) for name, item in observed_groups.items()}
            if isinstance(observed_groups, dict)
            else {},
            printed_total or [None, None, None],
        )
        total_ok = printed_total == [self.total.rows, self.total.units, self.total.revenue_cents]
        self.check(
            "Group and summarize",
            aggregates_match(observed_groups, self.expected) and total_ok,
            "Every category and the total match the independent local calculation exactly (integer cents)",
            "All match" if aggregates_match(observed_groups, self.expected) and total_ok else observed_groups,
        )

        self.report.heading("Files and chart delivered to the user")
        containers = parse_artifacts(text)
        if not self.check(
            "Container reference returned", bool(containers), f"Answer carries a `{ARTIFACTS_FENCE}` block", containers
        ):
            for name in OUTPUT_FILES:
                self.observe(name, "FAIL", FILE_EXPECTED, "No container reference to download from")
            return
        with httpx.Client(timeout=120, trust_env=False) as client:
            selected = select_container(client, self.project_endpoint, token, containers, OUTPUT_FILES)
            container, files = selected if selected else (containers[-1], {})
            downloaded = download_files(
                client, self.project_endpoint, token, container, files, OUTPUT_FILES, self.run.path / "outputs"
            )
        self.validate_files(container, downloaded)

    def comparison_table(self, observed: Mapping[str, list[JsonValue] | None], total: list[JsonValue] | None) -> None:
        """Expected versus observed per category; ``total`` adds a TOTAL row when the source has one."""
        rows: list[list[object]] = []
        expected_rows = [*self.expected.items(), *([("TOTAL", self.total)] if total is not None else [])]
        for name, totals in expected_rows:
            seen = total if name == "TOTAL" else observed.get(name)
            values = seen or [None, None, None]
            want = [totals.rows, totals.units, totals.revenue_cents]
            rows.append(
                [name, want[0], values[0], want[1], values[1], want[2], values[2], "yes" if seen == want else "no"]
            )
        for name in sorted(set(observed) - set(self.expected)):
            values = observed[name] or [None, None, None]
            rows.append([name, "-", values[0], "-", values[1], "-", values[2], "unexpected category"])
        self.report.table(
            [
                "Category",
                "Rows (expected)",
                "Rows (observed)",
                "Units (expected)",
                "Units (observed)",
                "Revenue cents (expected)",
                "Revenue cents (observed)",
                "Match",
            ],
            rows,
        )

    def file_checks(self, downloaded: Mapping[str, tuple[bytes, str | None]]) -> dict[str, tuple[bool, object]]:
        """Content-based checks of each output file against the local oracle; never trusts names or types."""
        filtered_bytes = downloaded.get("filtered.csv", (b"", None))[0]
        by_category_bytes = downloaded.get("by_category.csv", (b"", None))[0]
        png_bytes = downloaded.get("revenue_by_category.png", (b"", None))[0]
        filtered = check_filtered_csv(filtered_bytes, self.expected_ids, COLUMNS)
        filtered["revenue_cents"] = filtered_revenue(filtered_bytes)
        filtered["expected_revenue_cents"] = self.total.revenue_cents
        filtered_ok = bool(filtered_bytes) and all(v is True for v in filtered.values() if isinstance(v, bool))
        filtered_ok = filtered_ok and filtered["revenue_cents"] == self.total.revenue_cents
        groups_ok = bool(by_category_bytes) and check_by_category_csv(by_category_bytes, self.expected)
        dimensions = png_dimensions(png_bytes)
        return {
            "filtered.csv": (filtered_ok, filtered),
            "by_category.csv": (groups_ok, {"groups_match": groups_ok}),
            "revenue_by_category.png": (
                dimensions is not None,
                {"png": f"{dimensions[0]}x{dimensions[1]}" if dimensions else "invalid"},
            ),
        }

    def validate_files(self, container: str, downloaded: Mapping[str, tuple[bytes, str | None]]) -> None:
        filtered_bytes = downloaded.get("filtered.csv", (b"", None))[0]
        by_category_bytes = downloaded.get("by_category.csv", (b"", None))[0]
        dimensions = png_dimensions(downloaded.get("revenue_by_category.png", (b"", None))[0])
        self.obs["files"] = {name: len(data) for name, (data, _) in downloaded.items()}
        self.obs["png"] = f"{dimensions[0]}x{dimensions[1]} PNG" if dimensions else None
        checks = self.file_checks(downloaded)
        self.report.heading("by_category.csv vs. local oracle", level=3)
        observed_rows = by_category_rows(by_category_bytes)
        self.comparison_table({name: list(values) for name, values in observed_rows.items()}, None)
        table: list[list[object]] = []
        for name, (ok, details) in checks.items():
            present = name in downloaded
            status: Status = "PASS" if present and ok else "FAIL"
            observed = details if present else "Not generated in the referenced container"
            self.observe(name, status, FILE_EXPECTED, {"container": container, "checks": observed})
            data, content_type = downloaded.get(name, (b"", None))
            link = f"[{name}](outputs/{name})" if present else name
            table.append([link, status_icon(status), container, len(data) if present else "-", content_type, observed])
        self.report.table(
            ["File (local copy)", "Status", "Sandbox container", "Bytes", "Content type", "Checks"], table
        )
        self.report.paragraph(
            "File types are validated from content (CSV parsing and PNG signature/IHDR), never from model-provided "
            "names, URLs, or the container files API `Content-Type`."
        )
        if filtered_bytes:
            sample = list(csv.reader(io.StringIO(filtered_bytes.decode("utf-8"))))
            self.report.heading("Sample of filtered rows (first 5 of the downloaded file)", level=3)
            self.report.table(sample[0], [list(row) for row in sample[1:6]])
        if dimensions is not None:
            self.report.heading("Chart", level=3)
            self.report.paragraph("![Revenue by category](outputs/revenue_by_category.png)")

    def supervisor(self, project: AIProjectClient, token: Callable[[], str]) -> None:
        """Send the same task through the existing Supervisor workflow, unchanged; never weaken its guard."""
        self.report.heading("Supervisor delivery path (existing supervisor-agent)")
        command = Command(operation="create", workflow_id=uuid4(), message=analysis_prompt(self.dataset))
        number = self.run.start_request(SUPERVISOR, "hosted", f"POC_WORKFLOW create ({self.dataset.filename})")
        started = perf_counter()
        status: Status = "FAIL"
        observed: dict[str, object] = {}
        try:
            try:
                with project.get_openai_client(
                    agent_name=SUPERVISOR, timeout=self.args.timeout, max_retries=0
                ) as client:
                    conversation = client.conversations.create()
                    response = client.responses.create(input=command.wire(), conversation=conversation.id, stream=False)
                status, observed = supervisor_outcome(response.model_dump(mode="json"), self.expected)
            except (OpenAIError, AzureError) as error:
                _, status = classify_exception(error)
                observed = {"error": sanitize_text(str(error))[:500]}
            observed["seconds"] = round(perf_counter() - started, 2)
            self.record(SUPERVISOR_CHECK, status, SUPERVISOR_EXPECTED, observed)
        finally:
            self.run.end_request(number, SUPERVISOR, status, (perf_counter() - started) * 1000)
        self.forwarded(status, observed, token)

    def forwarded(self, supervisor: Status, observed: Mapping[str, object], token: Callable[[], str]) -> None:
        """Download the files from the container IDs the Supervisor forwarded, with the caller's own identity."""
        self.report.heading("Files and chart forwarded through the Supervisor", level=3)
        if supervisor != "PASS":
            hop = observed.get("failed_hop")
            where = f" at the {hop} hop" if hop else ""
            before = ", before Data Analysis ran, so no container IDs existed to forward" if hop == "research" else ""
            self.obs["forward_blocked"] = f"the Supervisor workflow ended {supervisor}{where}{before}"
            self.record(
                FORWARD_CHECK,
                "BLOCKED" if supervisor == "BLOCKED" else "SKIP",
                FORWARD_EXPECTED,
                f"Not reached: {self.obs['forward_blocked']}",
            )
            return
        raw = observed.get("containers")
        containers = [str(item) for item in raw] if isinstance(raw, list) else []
        if not containers:
            self.record(
                FORWARD_CHECK,
                "FAIL",
                FORWARD_EXPECTED,
                "The Supervisor report carries no container IDs (agents not redeployed with container forwarding?)",
            )
            return
        local = self.run.path / "outputs" / "supervisor"
        with httpx.Client(timeout=120, trust_env=False) as client:
            bearer_token = token()
            selected = select_container(client, self.project_endpoint, bearer_token, containers, OUTPUT_FILES)
            container, files = selected if selected else (containers[-1], {})
            downloaded = download_files(
                client, self.project_endpoint, bearer_token, container, files, OUTPUT_FILES, local
            )
        checks = self.file_checks(downloaded)
        rows: list[list[object]] = []
        for name, (ok, details) in checks.items():
            present = name in downloaded
            rows.append(
                [
                    f"[{name}](outputs/supervisor/{name})" if present else name,
                    status_icon("PASS" if present and ok else "FAIL"),
                    len(downloaded[name][0]) if present else "-",
                    details if present else "Not in the forwarded container",
                ]
            )
        self.report.table(["File (local copy)", "Status", "Bytes", "Checks"], rows)
        passed = all(name in downloaded and ok for name, (ok, _) in checks.items())
        self.obs["forwarded_files"] = {name: len(data) for name, (data, _) in downloaded.items()}
        self.record(
            FORWARD_CHECK,
            "PASS" if passed else "FAIL",
            FORWARD_EXPECTED,
            {"containers": containers, "container": container, "files": self.obs["forwarded_files"]},
        )

    def conclude(self) -> None:
        self.verdict = step2_verdict(self.statuses)
        supervisor = self.statuses.get(SUPERVISOR_CHECK, "SKIP")

        def evidence(name: str, text: str) -> list[object]:
            return [status_icon(self.statuses.get(name, "SKIP")), text]

        self.report.heading("Delivery paths")
        self.report.table(
            ["Path", "Status", "Evidence"],
            [
                ["Summary text -> user", *evidence("Hosted analysis request", "Agent answer quoted above")],
                [
                    "Structured aggregates -> user",
                    *evidence("Group and summarize", "Fenced JSON parsed and compared exactly with the local oracle"),
                ],
                *[
                    [
                        f"{name} -> user",
                        *evidence(
                            name,
                            f"`{ARTIFACTS_FENCE}` container ID -> authenticated container files API -> local copy",
                        ),
                    ]
                    for name in OUTPUT_FILES
                ],
                ["Supervisor -> Data Analysis", supervisor, "Existing POC_WORKFLOW create; see the Supervisor section"],
                [
                    "Files and chart -> Supervisor",
                    self.statuses.get(FORWARD_CHECK, "SKIP"),
                    "Supervisor report `containers` -> authenticated container files API -> `outputs/supervisor/`",
                ],
            ],
        )
        self.write_summary(supervisor)

    def write_summary(self, supervisor: str) -> None:
        files = self.obs.get("files") or {}
        forward = self.statuses.get(FORWARD_CHECK, "SKIP")
        forward_result, forward_basis = {
            "PASS": (
                "The Supervisor's workflow report carried the Data Analysis container IDs; the caller downloaded "
                "every file with its own identity and validated it by content",
                "Tested",
            ),
            "FAIL": (
                "Container IDs were missing or the forwarded files did not validate; see 'Files and chart forwarded "
                "through the Supervisor' in the detailed evidence",
                "Tested",
            ),
        }.get(
            forward,
            (
                "Built, not yet demonstrated: workflow reports carry sandbox container IDs from Data "
                "Analysis through the Supervisor (`containers`), but "
                + str(self.obs.get("forward_blocked") or f"the Supervisor path ended {supervisor}")
                + ". Story 4 Step 6 demonstrates container forwarding through the Supervisor on the stateless "
                "POC_DELEGATE path",
                "Unit-tested; not reached end to end",
            ),
        )
        results = [
            f"Analysis request completed in {self.obs.get('seconds', '-')} s; the agent wrote and ran its own "
            "pandas/matplotlib code in the managed sandbox.",
            f"Filter kept {len(self.expected_ids)} completed West rows; every category total and the overall total "
            f"({self.total.rows} rows, {self.total.units:,} units, {self.total.revenue_cents:,} revenue cents) "
            + (
                "matched the independent calculation exactly."
                if self.statuses.get("Group and summarize") == "PASS"
                else "did not match the independent calculation."
            ),
            "Delivered to the user: plain-language summary, machine-readable totals, "
            + ", ".join(f"{name} ({human_bytes(size)})" for name, size in files.items())
            + (f"; chart {self.obs['png']}" if self.obs.get("png") else "")
            + ".",
            f"Supervisor -> Research -> Data Analysis: {supervisor}"
            + (" at the nested-user-ID guard, which is left unchanged." if supervisor == "BLOCKED" else ".")
            + f" Files and chart forwarded through the Supervisor: {forward}.",
        ]
        if self.obs.get("summary"):
            results.append(f'Agent summary: "{self.obs["summary"]}"')
        analysis = checks_outcome(*(self.statuses.get(name) for name in ANALYSIS_CHECKS))
        files_outcome = checks_outcome(
            *(self.statuses.get(name) for name in ("Container reference returned", *OUTPUT_FILES))
        )
        supervisor_text = {
            "PASS": "The Supervisor completed the workflow through Research and Data Analysis for the verified user",
            "BLOCKED": "The nested Research call arrived without a verified user ID, so the workflow guard stopped it "
            "(the guard is left unchanged). Microsoft documents x-ms-user-identity for your own users, but not its "
            "propagation across nested agent calls",
            "FAIL": "The Supervisor workflow failed; see 'Supervisor delivery path' in the detailed evidence",
        }.get(supervisor, "Not run in this execution")
        findings = [
            Finding(
                "Filter, group, summarize, and chart with model-written code in the managed sandbox",
                "The sandbox read the exact input, and every printed total matched an independent local calculation "
                "to the cent"
                if analysis == "PASS"
                else "See 'Sandbox read the exact input', 'Filter', and 'Group and summarize' in the detailed evidence",
                "Tested",
                "Foundry-managed",
                ("ci_overview",),
                outcome=analysis,
            ),
            Finding(
                "Summary text and printed totals return to the user in the agent's answer",
                "The plain-language summary is quoted below and the printed totals were parsed and compared",
                "Tested",
                "Agent Framework / SDK-managed",
                ("hosted_conversations",),
                outcome=checks_outcome(
                    self.statuses.get("Hosted analysis request"), self.statuses.get("Group and summarize")
                ),
            ),
            Finding(
                "Generated CSVs and the chart reach the user by container ID and file ID",
                "All three files were downloaded with the caller's identity and validated by content"
                if files_outcome == "PASS"
                else "See 'Files and chart delivered to the user' in the detailed evidence",
                "Tested",
                "Custom implementation (Dev Team)",
                ("ci_files",),
                outcome=files_outcome,
            ),
            Finding(
                "Generated files are temporary",
                "Microsoft documents a 20-minute idle expiry for sandbox containers (Step 4 measures it). Copy files "
                "to customer storage if they must outlive the sandbox",
                "Documented",
                "Foundry-managed",
                ("responses_containers", "ci_lifetime"),
                outcome="LIMITATION",
            ),
            Finding(
                "Supervisor delegates the task to Data Analysis on the user's behalf",
                supervisor_text,
                "Tested",
                "Custom implementation (Dev Team)",
                ("isolate_sessions",),
                outcome=checks_outcome(supervisor),
            ),
            Finding(
                FORWARD_CHECK,
                forward_result,
                forward_basis,
                "Custom implementation (Dev Team)",
                ("ci_files",),
                outcome=checks_outcome(forward),
            ),
            Finding(
                "Per-user isolation of sandbox files",
                "Any principal with access to the project can read sandbox files (Microsoft documents it; Step 4 "
                "confirmed it). Untested options that may provide it: "
                f"{isolation_options_link(self.report.path.parent)}",
                "Documented",
                "Not supported",
                ("ci_isolation",),
                outcome="NOT SUPPORTED",
            ),
        ]
        gaps = [
            *(
                []
                if forward == "PASS"
                else [
                    "Supervisor path: align the nested caller guard with the platform contract (GitHub issues #3 and "
                    "#4) without "
                    "weakening it, then rerun this step to demonstrate container forwarding end to end."
                ]
            ),
            "Forwarded container IDs expire with the sandbox; copy generated files to customer storage for durable "
            "delivery.",
            "One synthetic 1k-row dataset and one request; dataset sizes are covered in Step 3.",
        ]
        conclusion = (
            "AC2 accepts delivery to the supervisor **or** the user. The summary, totals, CSVs, and chart all reach "
            "the user and are validated exactly; "
            + (
                "the same files also reach the caller through the Supervisor by forwarded container IDs."
                if forward == "PASS"
                else "the Supervisor path is a recorded limitation."
            )
            if self.verdict == "Met"
            else "See the checks below."
        )
        self.report.summary(render_summary(self.verdict, conclusion, results, findings, gaps))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, choices=[1, 2, 3, 4, 5], required=True)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument(
        "--mode",
        choices=["verify", "enable-if-missing"],
        default="verify",
        help="Step 1 only; Steps 2-5 never change agents or the Step 1 toolbox.",
    )
    parser.add_argument("--timeout", type=float, default=300, help="Client timeout in seconds per hosted request.")
    parser.add_argument(
        "--options-toolbox",
        choices=["probe", "document"],
        default="probe",
        help="Step 4: create, probe, and delete a separate probe-only toolbox, or document the schema only.",
    )
    parser.add_argument(
        "--idle-wait",
        choices=["skip", "wait"],
        default="skip",
        help="Step 4: 'wait' idles past the observed container expiry (about 21 minutes) and checks it.",
    )
    parser.add_argument(
        "--blob-checkpoint",
        choices=["write", "skip"],
        default="write",
        help="Step 5: write, read, and delete one synthetic checkpoint in the existing Blob container.",
    )
    add_output_arguments(parser)
    return parser


def run_reusing_step(args: argparse.Namespace, step_class: type[ReusingStep]) -> int:
    """Run a Step 2-5 class with the shared reuse guard, report, and index entry."""
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
        step.record(
            "Prerequisite", "BLOCKED", "Existing agents, Step 1 baseline, and Story 1 toolbox ready", str(error)
        )
    except (
        CliError,
        AzureError,
        OpenAIError,
        httpx.HTTPError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        step.record("Unexpected failure", "FAIL", "Step completes", sanitize_text(str(error)))
    step.conclude()
    report.render(run.console)
    index = args.output_dir / "index.md"
    update_index(index, index_entry(index, step_class.spec.label(), step.verdict, run.path))
    return run.finish()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.step != 1 and args.mode != "verify":
        parser.error("--mode applies to Step 1 only; Steps 2-5 never change agents or the Step 1 toolbox.")
    args.output_dir = Path(args.output_dir) / STORY_DIR
    if args.step == 2:
        return run_reusing_step(args, Step2)
    if args.step == 3:
        return run_reusing_step(args, Step3)
    if args.step in (4, 5):
        # Imported lazily: the sandbox module builds on this one.
        from scripts.run_scenario import story1_sandbox

        return run_reusing_step(args, story1_sandbox.Step4 if args.step == 4 else story1_sandbox.Step5)
    run = ValidationRun(
        STEP1.scenario,
        "hosted",
        args.output_dir,
        no_color=args.no_color,
        verbose=args.verbose,
        step=STEP1,
        variant=args.mode,
    )
    report = MarkdownReport(
        run.path / REPORT_FILE, "Story 1 / Step 1 - Code Interpreter on the existing Data Analysis agent"
    )
    step = Step1(args, run, report)
    try:
        step.execute()
    except Blocked as error:
        step.record("Prerequisite", "BLOCKED", "Existing agents and Story 1 toolbox ready", str(error))
    except (
        CliError,
        AzureError,
        OpenAIError,
        httpx.HTTPError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        step.record("Unexpected failure", "FAIL", "Step completes", sanitize_text(str(error)))
    step.conclude()
    report.render(run.console)
    index = args.output_dir / "index.md"
    update_index(index, index_entry(index, STEP1.label(args.mode), step.verdict, run.path))
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
