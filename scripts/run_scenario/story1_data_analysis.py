"""Story 1 demonstrations against the EXISTING hosted agents in one azd environment.

Step 1 enables managed Code Interpreter on the already-deployed ``data-analysis-agent``.
It reuses that agent (name, endpoint, identity, routing) and never creates an agent.
``--mode verify`` is read-only. ``--mode enable-if-missing`` may, once, create the
Story 1 toolbox and move the existing Data Analysis agent to one new version.
Exit codes: 0 all checks passed, 1 failure, 2 blocked prerequisite.
"""

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Literal

import httpx
from app.core.redaction import sanitize_text
from app.services.code_interpreter import ARTIFACTS_FENCE, parse_artifacts
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AutoCodeInterpreterToolParam, CodeInterpreterToolboxTool
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import AzureCliCredential
from openai import OpenAIError
from pydantic import JsonValue

from scripts.common.reporting import MarkdownReport, Status, ValidationRun, add_output_arguments, update_index
from scripts.common.synthetic_data import DATASET_SIZES, Dataset, build_dataset
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
REPORT_NAME = "01-existing-agent-code-interpreter.md"
TASK_LABEL = "Agents: Story 1 - 1 Enable Code Interpreter on existing Data Analysis agent"
AC1 = (
    "Deploy a Foundry Hosted Data Analysis Agent using Microsoft-managed Code Interpreter, "
    "backed by Azure Container Apps dynamic sessions."
)
CI_DOCS = "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/code-interpreter"
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

    def record(self, step: str, status: Status, expected: str, observed: object) -> None:
        self.run.result(step, status, expected=expected, observed=observed)
        self.report.table(["Check", "Status", "Expected", "Observed"], [[step, status, expected, observed]])

    def execute(self) -> None:
        self.report.paragraph(f"**AC1 (verbatim from #1):** {AC1}")
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
        try:
            context = object_value(
                native_json("azd", ["env", "get-values", "--environment", self.args.environment, "--output", "json"])
            )
        except (CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            raise Blocked(f"Cannot read azd environment '{self.args.environment}': {error}") from error
        required = ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_LOCATION")
        missing = [key for key in required if not isinstance(context.get(key), str) or not context.get(key)]
        if missing:
            raise Blocked(f"azd environment is missing {', '.join(missing)}.")
        self.report.heading("Configuration")
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
        self.report.table(
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
        self.record(
            "Agent returns sandbox container reference",
            "PASS" if exists else "FAIL",
            f"Answer carries a `{ARTIFACTS_FENCE}` block whose container exists in this project",
            details,
        )

    def findings(self) -> None:
        self.report.heading("Findings")
        self.report.table(
            ["Capability", "Evidence basis", "Ownership"],
            [
                ["Code Interpreter tool via toolbox MCP endpoint", "Tested", "Foundry-managed"],
                ["Toolbox wiring in hosted agent code (FoundryToolbox)", "Tested", "Agent Framework / SDK-managed"],
                ["Synthetic input upload and checksum manifest", "Tested", "Custom implementation (Dev Team)"],
                ["Sandbox backed by ACA dynamic sessions with Hyper-V isolation", "Documented", "Foundry-managed"],
                [
                    "Per-user sandbox isolation with toolbox + hosted agent",
                    "Documented: not supported",
                    "Not supported",
                ],
                [
                    "Inputs via toolbox file_ids require project-scoped uploads on this project",
                    "Observed in implementation probe 2026-10-22",
                    "Foundry-managed",
                ],
                [
                    "Each tool call gets a fresh container; container reuse cannot be combined with toolbox file_ids",
                    "Observed in implementation probe 2026-10-22; retested in Step 5",
                    "Foundry-managed",
                ],
                [
                    "Default container: 2g memory, expires 20 min after last activity; per-call memory_limit "
                    "(1g/4g/16g/64g) and network_policy options in tool schema",
                    "Observed (container API and tools/list); retested in Steps 3-4",
                    "Foundry-managed",
                ],
                [
                    "Generated files delivered via container ID + authenticated container files API",
                    "Tested",
                    "Custom implementation (Dev Team)",
                ],
            ],
        )
        self.report.paragraph(
            f"Sandbox backing is documented in [Code Interpreter for Foundry agents]({CI_DOCS}); the underlying "
            "Azure Container Apps resource is Microsoft-managed and not visible for direct inspection."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, choices=[1], required=True)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--mode", choices=["verify", "enable-if-missing"], default="verify")
    parser.add_argument("--timeout", type=float, default=300)
    add_output_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.output_dir = Path(args.output_dir) / STORY_DIR
    run = ValidationRun(
        "story-1-step-1",
        "hosted",
        args.output_dir,
        no_color=args.no_color,
        verbose=args.verbose,
        directory_label=f"step-1-code-interpreter-{args.mode}",
    )
    report = MarkdownReport(
        run.path / REPORT_NAME, "Story 1 / Step 1 - Code Interpreter on the existing Data Analysis agent"
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
    step.findings()
    if step.verdict == "Met" and "FAIL" in run.statuses:
        step.verdict = "Not met"
    report.heading("Verdict")
    report.paragraph(f"**Acceptance verdict: {step.verdict}.**")
    report.render(run.console)
    update_index(
        args.output_dir / "index.md",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC - Step 1 ({args.mode}) - **{step.verdict}** - "
        f"[{run.path.name}]({run.path.name}/{REPORT_NAME})",
    )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
