"""Story 1 Steps 4 and 5: sandbox boundaries (AC4) and state continuity (AC5).

Both steps reuse the existing agents and the Step 1 toolbox behind the shared Step 1 baseline guard.
They never deploy or change an agent or the Step 1 toolbox. Step 4 may create and then delete one
separate probe-only toolbox (``--options-toolbox probe``) to test toolbox-level options, and deletes
the sandbox containers its direct probes created. Step 5 retains original containers and checkpoints
by default; only explicit destructive mode deletes them. Required reconstruction uses verified Blob
bytes exclusively, never the separately retained local diagnostic checkpoint.
"""

import argparse
import base64
import json
import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import httpx
from app.core.redaction import sanitize_text
from app.services.code_interpreter import ARTIFACTS_FENCE, parse_artifacts
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AutoCodeInterpreterToolParam,
    CodeInterpreterToolboxTool,
    ContainerNetworkPolicyAllowlistParam,
    ContainerNetworkPolicyDisabledParam,
)
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import AzureCliCredential
from openai import OpenAIError
from pydantic import JsonValue

from scripts.common.findings import Finding, Outcome, checks_outcome, doc_cell, isolation_options_link, render_summary
from scripts.common.reporting import MarkdownReport, Status, StepSpec, ValidationRun, status_icon
from scripts.common.synthetic_data import (
    FILTER_REGION,
    FILTER_STATUS,
    CategoryTotals,
    Dataset,
    expected_aggregates,
    generate_rows,
)
from scripts.deploy.grant_local_rbac import object_value
from scripts.run_scenario.story1_data_analysis import (
    DATA_ANALYSIS,
    DATA_SCOPE,
    HOST_SETTINGS,
    TOOLBOX_NAME,
    ReusingStep,
    aggregates_match,
    bearer,
    cgroup_memory_text,
    classify_exception,
    container_details,
    download_files,
    human_bytes,
    mcp_result,
    read_environment,
    result_text,
    select_container,
    toolbox_endpoint,
)
from scripts.run_scenario.trigger_agent import response_summary

STORAGE_SCOPE = "https://storage.azure.com/.default"
BLOB_VERSION = "2023-11-03"
DOCUMENTED_LIFETIME = "Each session is active by default for one hour with an idle timeout of 30 minutes"
DOCUMENTED_ISOLATION = (
    "When Code Interpreter is used through a toolbox in a hosted agent, user isolation isn't supported. "
    "All users in the same project share the same container context."
)
RESULT_MARKER = "STORY1-RESULT:"
PUBLIC_HOST = "learn.microsoft.com"
OTHER_HOST = "pypi.org"
IDLE_MARGIN_SECONDS = 90
IDLE_MAX_SECONDS = 35 * 60

# Shared, portable helpers for every fixed sandbox program. Results go to a file the runner downloads
# and to one base64 line, so neither depends on the model copying numbers correctly.
PRELUDE = """
import base64, getpass, glob, hashlib, json, os, platform, shutil, socket, time, urllib.error, urllib.request
DATA = "__DATA__"
def _read(path):
    try:
        with open(path) as stream:
            return stream.read().strip()
    except OSError:
        return None
def _classify(error):
    reason = getattr(error, "reason", error)
    text = str(reason).lower()
    if isinstance(reason, socket.gaierror) or "name resolution" in text or "name or service" in text:
        return "dns_failure"
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "timeout"
    if isinstance(reason, ConnectionRefusedError) or "refused" in text:
        return "refused"
    if "unreachable" in text:
        return "unreachable"
    return type(reason).__name__
def _url(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return {"outcome": "reachable", "status": response.status}
    except urllib.error.HTTPError as error:
        return {"outcome": "reachable", "status": error.code}
    except Exception as error:
        return {"outcome": _classify(error)}
def _dns(host):
    try:
        socket.getaddrinfo(host, 443)
        return {"outcome": "resolved"}
    except Exception as error:
        return {"outcome": _classify(error)}
def _tcp(host, port):
    if not host:
        return {"outcome": "not_run"}
    try:
        socket.create_connection((host, port), timeout=5).close()
        return {"outcome": "reachable"}
    except Exception as error:
        return {"outcome": _classify(error)}
def _fingerprint():
    return {
        "hostname_sha256": hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16],
        "boot_id_sha256": hashlib.sha256((_read("/proc/sys/kernel/random/boot_id") or "").encode()).hexdigest()[:16],
        "uptime_s": float((_read("/proc/uptime") or "0").split()[0]),
    }
def _memory_max():
    return _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
def _input(suffix):
    return next(p for p in sorted(glob.glob(os.path.join(DATA, "*"))) if p.endswith(suffix))
def _emit(name, value):
    with open(os.path.join(DATA, name), "w") as stream:
        json.dump(value, stream)
    print("STORY1-RESULT:" + base64.b64encode(json.dumps(value).encode()).decode())
"""

GROUP = """
filtered = frame[(frame["status"] == "__STATUS__") & (frame["region"] == "__REGION__")]
grouped = filtered.groupby("category", sort=True).agg(
    rows=("row_id", "size"), units=("units", "sum"), revenue_cents=("revenue_cents", "sum")
).reset_index()
by_category = {
    str(row.category): {"rows": int(row.rows), "units": int(row.units), "revenue_cents": int(row.revenue_cents)}
    for row in grouped.itertuples(index=False)
}
"""

BOUNDARY_CODE = """
targets = json.loads(__TARGETS__)
marker = os.path.join(DATA, "story1-boundary-marker.txt")
def _writable(path):
    probe = os.path.join(path, ".story1-write-test")
    try:
        with open(probe, "w") as stream:
            stream.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False
def _status(name):
    for line in (_read("/proc/self/status") or "").splitlines():
        if line.startswith(name + ":"):
            return line.split(":", 1)[1].strip()
    return None
packages = {}
try:
    from importlib.metadata import version as _version
    for name in ("pandas", "numpy", "matplotlib"):
        try:
            packages[name] = _version(name)
        except Exception:
            packages[name] = None
except Exception:
    pass
result = {
    "identity": {
        "user": getpass.getuser(),
        "uid": getattr(os, "getuid", lambda: None)(),
        "gid": getattr(os, "getgid", lambda: None)(),
    },
    "privileges": {"cap_eff": _status("CapEff"), "no_new_privs": _status("NoNewPrivs"), "seccomp": _status("Seccomp")},
    "filesystem": {
        "root_writable": _writable("/"),
        "tmp_writable": _writable("/tmp"),
        "data_writable": _writable(DATA),
        "inputs": sorted(n for n in os.listdir(DATA) if "story1-sales-" in n),
        "marker_seen": os.path.exists(marker),
    },
    "processes_visible": len([p for p in os.listdir("/proc") if p.isdigit()]) if os.path.isdir("/proc") else None,
    "fingerprint": _fingerprint(),
    "kernel": platform.release(),
    "limits": {
        "cgroup_memory_max": _memory_max(),
        "cpu_count": os.cpu_count(),
        "pids_max": _read("/sys/fs/cgroup/pids/pids.max") or _read("/sys/fs/cgroup/pids.max"),
        "disk_free_bytes": shutil.disk_usage(DATA).free,
    },
    "runtime": {"python": platform.python_version(), "packages": packages},
    "platform_hint_env_names": sorted(
        n for n in os.environ if any(t in n.upper() for t in ("SESSION", "CAAS", "CONTAINER_APP", "ACA_"))
    ),
    "network": {
        "dns_public": _dns(targets["public_host"]),
        "https_public": _url("https://" + targets["public_host"]),
        "tcp_public_ip": _tcp(targets["public_ip"], 443),
        "dns_customer_storage": _dns(targets["storage_host"]),
        "https_customer_storage": _url("https://" + targets["storage_host"]),
    },
}
with open(marker, "w") as stream:
    stream.write("story1")
_emit("__RESULT__", result)
"""

LIMITS_CODE = """
_emit("__RESULT__", {
    "cgroup_memory_max": _memory_max(),
    "https_allowed": _url("https://__ALLOWED__"),
    "https_other": _url("https://__OTHER__"),
    "fingerprint": _fingerprint(),
})
"""

TIMEOUT_CODE = "import time\nprint('story1-sleep-start', flush=True)\ntime.sleep(100)\nprint('story1-sleep-finished')"

STATE_WRITE_CODE = """
import pandas as pd
path = _input("__FILE__")
with open(path, "rb") as stream:
    digest = hashlib.sha256(stream.read()).hexdigest()
frame = pd.read_csv(path)
__GROUP__
story1_state = {"nonce": "__NONCE__", "filtered_rows": int(len(filtered)), "by_category": by_category}
with open(os.path.join(DATA, "story1-state-marker.json"), "w") as stream:
    json.dump(story1_state, stream)
_emit("__RESULT__", {
    "phase": "write",
    "nonce": "__NONCE__",
    "input_file": os.path.basename(path),
    "input_sha256": digest,
    "filtered_rows": int(len(filtered)),
    "by_category": by_category,
    "fingerprint": _fingerprint(),
    "customer_storage": {"dns": _dns("__STORAGE_HOST__"), "https": _url("https://__STORAGE_HOST__")},
})
"""

STATE_CHECK_CODE = """
try:
    variable = story1_state
except NameError:
    variable = None
marker_path = os.path.join(DATA, "story1-state-marker.json")
marker = None
if os.path.exists(marker_path):
    with open(marker_path) as stream:
        marker = json.load(stream)
_emit("__RESULT__", {
    "phase": "__PHASE__",
    "variable": variable,
    "marker": marker,
    "fingerprint": _fingerprint(),
    "inputs": sorted(n for n in os.listdir(DATA) if "story1-sales-" in n),
})
"""

REHYDRATE_CODE = """
checkpoint = json.loads(__CHECKPOINT__)
import pandas as pd
path = _input(checkpoint["dataset"]["file"])
with open(path, "rb") as stream:
    digest = hashlib.sha256(stream.read()).hexdigest()
frame = pd.read_csv(path)
__GROUP__
story1_state = {"rehydrated_from": checkpoint["id"], "filtered_rows": int(len(filtered)), "by_category": by_category}
_emit("__RESULT__", {
    "phase": "rehydrate",
    "checkpoint_id": checkpoint["id"],
    "input_sha256_matches": digest == checkpoint["dataset"]["sha256"],
    "filtered_rows": int(len(filtered)),
    "by_category": by_category,
    "matches_checkpoint": by_category == checkpoint["result"]["by_category"]
    and int(len(filtered)) == checkpoint["result"]["filtered_rows"],
    "fingerprint": _fingerprint(),
})
"""


def sandbox_program(body: str, replacements: Mapping[str, str], data_dir: str = "/mnt/data") -> str:
    """Join the prelude and a fixed program; placeholders are filled from runner-side values only."""
    code = PRELUDE + body.replace("__GROUP__", GROUP.strip())
    for token, value in {
        "__DATA__": data_dir,
        "__STATUS__": FILTER_STATUS,
        "__REGION__": FILTER_REGION,
        **replacements,
    }.items():
        code = code.replace(token, value)
    return code.strip()


def emitted_result(text: str) -> dict[str, JsonValue] | None:
    """Decode the base64 result line; immune to how the tool output quotes or escapes JSON."""
    match = re.search(re.escape(RESULT_MARKER) + r"([A-Za-z0-9+/=]+)", text)
    if not match:
        return None
    try:
        value = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def run_prompt(code: str, reply: str) -> str:
    return (
        "Use your code interpreter tool to run the Python below exactly as written, in ONE execution. "
        f"Do not modify, split, or retry it, and do not run any other code. {reply}\n\n```python\n{code}\n```"
    )


def nested(value: JsonValue, *keys: str) -> JsonValue:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def as_mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def execution_outcome(call: ToolCall) -> str:
    """completed, stopped (the sandbox ended it), or inconclusive (transport failure with no output)."""
    if "story1-sleep-finished" in call.text:
        return "completed"
    if "story1-sleep-start" in call.text or call.is_error:
        return "stopped"
    return "inconclusive"


def runtime_text(timeout: Mapping[str, Any]) -> str:
    seconds = timeout.get("seconds", "-")
    outcome = timeout.get("outcome")
    if outcome == "completed":
        return (
            f"A 100 s execution completed ({seconds} s): consistent with the documented 220 s maximum, not the 90 s "
            "in the tool's own description"
        )
    if outcome == "stopped":
        return (
            f"A 100 s execution was stopped after {seconds} s: the limit is below 100 s, consistent with the 90 s in "
            "the tool's own description rather than the documented 220 s"
        )
    if outcome == "inconclusive":
        return f"Inconclusive: the connection ended after {seconds} s without output ({timeout.get('error')})"
    return "Not run"


def network_names(value: JsonValue) -> list[str]:
    network = nested(value, "network")
    return sorted(network) if isinstance(network, dict) else []


def outcome(value: JsonValue, *keys: str) -> str:
    found = nested(value, *keys, "outcome")
    return str(found) if found is not None else "-"


@dataclass
class ToolCall:
    """One direct toolbox MCP call made with the operator's identity."""

    container: str | None = None
    text: str = ""
    is_error: bool = False
    error: str | None = None
    seconds: float = 0.0
    result: dict[str, JsonValue] | None = None


class ToolboxClient:
    """Minimal MCP client for a toolbox endpoint; options travel in ``_meta`` as the tool schema declares."""

    def __init__(self, http: httpx.Client, url: str, token: str) -> None:
        self.http = http
        self.url = url
        self.headers = {
            **bearer(token),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.counter = 0

    def request(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        self.counter += 1
        body = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}
        return mcp_result(self.http.post(self.url, headers=self.headers, json=body))

    def tools(self) -> list[dict[str, JsonValue]]:
        tools = self.request("tools/list", {}).get("tools")
        return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []

    def call(self, code: str, meta: Mapping[str, JsonValue] | None = None) -> ToolCall:
        params: dict[str, JsonValue] = {"name": "code_interpreter", "arguments": {"code": code}}
        if meta:
            params["_meta"] = dict(meta)
        started = perf_counter()
        try:
            result = self.request("tools/call", params)
        except (httpx.HTTPError, ValueError) as error:
            return ToolCall(error=sanitize_text(str(error))[:400], seconds=round(perf_counter() - started, 2))
        structured = result.get("structuredContent")
        container = structured.get("container_id") if isinstance(structured, dict) else None
        text = result_text(result)
        is_error = result.get("isError") is True
        emitted = emitted_result(text)
        return ToolCall(
            container=container if isinstance(container, str) else None,
            text=text,
            is_error=is_error,
            # A platform rejection (for example an organization network policy) arrives as an isError result.
            error=sanitize_text(text)[:400] if is_error and emitted is None else None,
            seconds=round(perf_counter() - started, 2),
            result=emitted,
        )


@dataclass
class HostedReply:
    """One request to the existing Data Analysis endpoint and the sandbox result file it produced."""

    status: Status = "FAIL"
    text: str = ""
    response_id: str | None = None
    agent_session_id: str | None = None
    conversation: str | None = None
    seconds: float = 0.0
    error: str | None = None
    containers: list[str] = field(default_factory=list)
    container: str | None = None
    result: dict[str, JsonValue] | None = None


def nonce_line(text: str) -> str | None:
    match = re.search(r"NONCE:\s*`?([A-Za-z0-9_-]+)`?", text)
    return match.group(1) if match else None


# Customer-network placement is assessed per state layer; only the custom checkpoint is exercised live.
CHECKPOINT_SCHEMA = "story1-checkpoint-v1"


def build_checkpoint(
    run_id: str, dataset: Dataset, file_id: str, result: Mapping[str, JsonValue], source: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Small, versioned, data-free state: what to reload, how to recompute it, and the validated answer."""
    return {
        "schema": CHECKPOINT_SCHEMA,
        "id": f"story1-{run_id[:12]}",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {"file": dataset.filename, "rows": dataset.rows, "sha256": dataset.sha256, "file_id": file_id},
        "transform": {
            "filter": {"status": FILTER_STATUS, "region": FILTER_REGION},
            "group_by": "category",
            "aggregates": ["rows", "units", "revenue_cents"],
        },
        "result": {"filtered_rows": result.get("filtered_rows"), "by_category": result.get("by_category")},
        "source": dict(source),
    }


def canonical(value: JsonValue) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal_checkpoint(checkpoint: Mapping[str, JsonValue]) -> bytes:
    body: JsonValue = dict(checkpoint)
    return json.dumps({"checkpoint": body, "sha256": sha256(canonical(body)).hexdigest()}, indent=2).encode("utf-8")


def open_checkpoint(
    raw: bytes | None,
    dataset: Dataset,
    file_id: str,
    *,
    expected_source: Mapping[str, JsonValue] | None = None,
) -> tuple[dict[str, JsonValue] | None, list[str]]:
    """Return the checkpoint only when it is present, intact, current-schema, and not stale."""
    if not raw:
        return None, ["missing"]
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None, ["corrupt: not JSON"]
    body = envelope.get("checkpoint") if isinstance(envelope, dict) else None
    if not isinstance(body, dict) or envelope.get("sha256") != sha256(canonical(body)).hexdigest():
        return None, ["corrupt: checksum mismatch"]
    problems: list[str] = []
    if body.get("schema") != CHECKPOINT_SCHEMA:
        problems.append(f"schema {body.get('schema')!r} is not {CHECKPOINT_SCHEMA}")
    dataset_info = body.get("dataset")
    if not isinstance(dataset_info, dict) or dataset_info.get("sha256") != dataset.sha256:
        problems.append("stale: dataset checksum differs from the mounted input")
    elif dataset_info.get("file_id") != file_id:
        problems.append("stale: toolbox file ID differs")
    elif dataset_info.get("file") != dataset.filename or dataset_info.get("rows") != dataset.rows:
        problems.append("stale: dataset identity differs")
    source = body.get("source")
    if expected_source is not None and (
        not isinstance(source, dict) or any(source.get(key) != value for key, value in expected_source.items())
    ):
        problems.append("owner/session/source binding differs")
    transform = body.get("transform")
    if transform != {
        "filter": {"status": FILTER_STATUS, "region": FILTER_REGION},
        "group_by": "category",
        "aggregates": ["rows", "units", "revenue_cents"],
    }:
        problems.append("unsupported transform")
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("by_category"), dict):
        problems.append("missing result.by_category")
    return (None if problems else body), problems


def same_sandbox(first: HostedReply, second: HostedReply) -> bool | None:
    """Same container and same in-sandbox host fingerprint; None when either side is unknown."""
    if not first.container or not second.container or not first.result or not second.result:
        return None
    a = nested(first.result, "fingerprint", "hostname_sha256")
    b = nested(second.result, "fingerprint", "hostname_sha256")
    return first.container == second.container and a == b


@dataclass
class BlobStore:
    """Synthetic checkpoint I/O against the existing customer Blob container, with the operator's identity."""

    http: httpx.Client
    endpoint: str
    container: str
    token: str

    def url(self, name: str) -> str:
        return f"{self.endpoint.rstrip('/')}/{self.container}/{name}"

    def headers(self, **extra: str) -> dict[str, str]:
        return {**bearer(self.token), "x-ms-version": BLOB_VERSION, **extra}

    def put(self, name: str, data: bytes) -> httpx.Response:
        return self.http.put(
            self.url(name),
            headers=self.headers(**{"x-ms-blob-type": "BlockBlob", "Content-Type": "application/json"}),
            content=data,
        )

    def get(self, name: str) -> httpx.Response:
        return self.http.get(self.url(name), headers=self.headers())

    def delete(self, name: str) -> httpx.Response:
        return self.http.delete(self.url(name), headers=self.headers())


def blob_status(response: httpx.Response) -> tuple[Status, str]:
    """403 means this operator lacks data access; it is a prerequisite, never fixed here."""
    if response.is_success:
        return "PASS", f"HTTP {response.status_code}"
    code = response.headers.get("x-ms-error-code", "")
    if response.status_code == 403:
        return "BLOCKED", f"HTTP 403 {code}: run 'Access: Apply developer permissions' (no roles are granted here)"
    return "FAIL", f"HTTP {response.status_code} {code}".strip()


class BlobCheckpointError(ValueError):
    """A required Blob checkpoint failed; callers must not substitute local bytes."""

    def __init__(self, detail: str, status: Status = "FAIL") -> None:
        super().__init__(detail)
        self.status = status


def read_blob_checkpoint(
    store: BlobStore,
    name: str,
    dataset: Dataset,
    file_id: str,
    *,
    expected_source: Mapping[str, JsonValue] | None = None,
    expected_sha256: str | None = None,
) -> dict[str, JsonValue]:
    """Read authenticated Blob bytes and validate schema, integrity, input, and trusted source."""
    try:
        response = store.get(name)
    except httpx.HTTPError as error:
        raise BlobCheckpointError(f"Blob read unavailable: {sanitize_text(str(error))}") from error
    status, detail = blob_status(response)
    if status != "PASS":
        raise BlobCheckpointError(f"Blob read failed: {detail}", status)
    body, problems = open_checkpoint(response.content, dataset, file_id, expected_source=expected_source)
    if body is None:
        raise BlobCheckpointError("; ".join(problems))
    if expected_sha256 is not None and sha256(response.content).hexdigest() != expected_sha256:
        raise BlobCheckpointError("Blob checkpoint hash differs from this run's written checkpoint")
    return body


def persist_and_read_checkpoint(
    store: BlobStore,
    name: str,
    sealed_bytes: bytes,
    dataset: Dataset,
    file_id: str,
    *,
    expected_source: Mapping[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Write once and return only the validated read-back; never delete or fall back locally."""
    body, problems = open_checkpoint(sealed_bytes, dataset, file_id, expected_source=expected_source)
    if body is None:
        raise BlobCheckpointError("; ".join(problems))
    try:
        response = store.put(name, sealed_bytes)
    except httpx.HTTPError as error:
        raise BlobCheckpointError(f"Blob write unavailable: {sanitize_text(str(error))}") from error
    status, detail = blob_status(response)
    if status != "PASS":
        raise BlobCheckpointError(f"Blob write failed: {detail}", status)
    return read_blob_checkpoint(
        store,
        name,
        dataset,
        file_id,
        expected_source=expected_source,
        expected_sha256=sha256(sealed_bytes).hexdigest(),
    )


class SandboxStep(ReusingStep):
    """Shared hosted-call plumbing for Steps 4 and 5."""

    def header(self, criterion: str, label: str, task: str, rows: Sequence[list[object]]) -> None:
        self.report.paragraph(f"**{label} (verbatim from GitHub issue #1):** {criterion}")
        self.report.table(
            ["Field", "Value"],
            [
                ["Task", task],
                ["Run ID", self.run.run_id],
                ["UTC time", datetime.now(UTC).isoformat(timespec="seconds")],
                ["Environment", self.args.environment],
                ["Client timeout per hosted request", f"{self.args.timeout:g} s (no retries)"],
                *rows,
            ],
        )

    def new_conversation(self, project: AIProjectClient) -> str:
        with project.get_openai_client(agent_name=DATA_ANALYSIS, timeout=self.args.timeout, max_retries=0) as client:
            return str(client.conversations.create().id)

    def hosted(
        self,
        project: AIProjectClient,
        token: Callable[[], str],
        label: str,
        prompt: str,
        result_name: str,
        conversation: str | None = None,
    ) -> HostedReply:
        """One request to the existing agent; the sandbox result file is downloaded, never transcribed."""
        reply = HostedReply(conversation=conversation)
        number = self.run.start_request(DATA_ANALYSIS, "hosted", label)
        started = perf_counter()
        try:
            try:
                with project.get_openai_client(
                    agent_name=DATA_ANALYSIS, timeout=self.args.timeout, max_retries=0
                ) as client:
                    extra = {"conversation": conversation} if conversation else {}
                    response = client.responses.create(input=prompt, stream=False, **extra)  # type: ignore[call-overload]
                summary = response_summary(response.model_dump(mode="json"))
            except (OpenAIError, AzureError) as error:
                _, reply.status = classify_exception(error)
                reply.error = sanitize_text(str(error))[:500]
                return reply
            finally:
                reply.seconds = round(perf_counter() - started, 2)
            reply.text = str(summary.get("text") or "")
            reply.response_id = str(summary.get("id") or summary.get("response_id") or "") or None
            session = summary.get("agent_session_id")
            reply.agent_session_id = session if isinstance(session, str) else None
            if summary.get("status") != "completed":
                reply.error = sanitize_text(json.dumps(summary.get("error")) or str(summary.get("status")))[:500]
                return reply
            reply.containers = parse_artifacts(reply.text)
            if not reply.containers:
                reply.error = f"Answer has no `{ARTIFACTS_FENCE}` container reference"
                return reply
            try:
                with httpx.Client(timeout=120, trust_env=False) as http:
                    value = token()
                    selected = select_container(http, self.project_endpoint, value, reply.containers, [result_name])
                    if selected is None:
                        reply.error = f"No referenced container holds {result_name}"
                        return reply
                    reply.container, files = selected
                    local = self.run.path / "outputs" / label.split(" ")[0].lower()
                    downloaded = download_files(
                        http, self.project_endpoint, value, reply.container, files, [result_name], local
                    )
                parsed = json.loads(downloaded[result_name][0].decode("utf-8"))
                reply.result = parsed if isinstance(parsed, dict) else None
            except (httpx.HTTPError, ValueError, KeyError, UnicodeDecodeError) as error:
                reply.error = sanitize_text(f"Result retrieval failed: {error}")[:500]
                return reply
            reply.status = "PASS" if reply.result is not None else "FAIL"
            return reply
        finally:
            self.run.end_request(number, DATA_ANALYSIS, reply.status, (perf_counter() - started) * 1000)


STEP4 = StepSpec(1, 4, "sandbox-boundaries")
STEP4_TASK_LABEL = "Agents: Story 1 - 4 Assess sandbox boundaries"
AC4 = (
    "Document sandbox security boundaries, session lifetime, network restrictions, and available configuration options."
)
REUSE_CHECK_CODE = """
_emit("__RESULT__", {
    "marker_seen": os.path.exists(os.path.join(DATA, "story1-boundary-marker.txt")),
    "fingerprint": _fingerprint(),
})
"""
BOUNDARY_KEYS = (
    ("identity", "user"),
    ("identity", "uid"),
    ("privileges", "no_new_privs"),
    ("privileges", "seccomp"),
    ("privileges", "cap_eff"),
    ("filesystem", "root_writable"),
    ("filesystem", "data_writable"),
    ("limits", "cgroup_memory_max"),
    ("network", "dns_public", "outcome"),
    ("network", "https_public", "outcome"),
    ("network", "tcp_public_ip", "outcome"),
    ("network", "https_customer_storage", "outcome"),
)
OPEN_OUTCOMES = {"reachable", "resolved"}


def boundary_view(result: Mapping[str, JsonValue] | None) -> dict[str, JsonValue]:
    """The boundary-relevant fields only, so two sandboxes can be compared class by class."""
    value: JsonValue = dict(result) if result else {}
    return {".".join(keys): nested(value, *keys) for keys in BOUNDARY_KEYS}


def network_open(result: Mapping[str, JsonValue] | None) -> list[str]:
    network = result.get("network") if result else None
    if not isinstance(network, dict):
        return []
    return sorted(name for name, item in network.items() if nested(item, "outcome") in OPEN_OUTCOMES)


def memory_applied(observed: JsonValue, requested: str) -> str:
    sizes = {"1g": 1024**3, "4g": 4 * 1024**3, "16g": 16 * 1024**3, "64g": 64 * 1024**3}
    if not isinstance(observed, str) or not observed.isdigit():
        return "unknown"
    return "applied" if int(observed) == sizes.get(requested) else f"ignored (enforced {cgroup_memory_text(observed)})"


def step4_verdict(statuses: Mapping[str, Status]) -> str:
    if statuses.get("Direct probe A") != "PASS":
        return "Not evaluated"
    return "Partially met" if "FAIL" in statuses.values() else "Met"


class Step4(SandboxStep):
    """Benign, targeted boundary probes and a documented-versus-observed matrix; no escape attempts or scans."""

    spec = STEP4
    title = "Story 1 / Step 4 - Sandbox security, lifetime, network, and configuration"

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        super().__init__(args, run, report)
        self.obs: dict[str, Any] = {}
        self.probe_containers: list[str] = []

    def execute(self) -> None:
        self.header(
            AC4,
            "AC4",
            STEP4_TASK_LABEL,
            [
                ["Options toolbox", self.args.options_toolbox],
                ["Idle-expiry wait", self.args.idle_wait],
            ],
        )
        self.report.paragraph(
            "Reuses the existing agents and the Step 1 toolbox without changing them. Probes are benign: no "
            "scanning, escape attempts, or secrets; environment variables are recorded by name only."
        )
        context = read_environment(self.args.environment, self.report)
        self.project_endpoint = str(context["FOUNDRY_PROJECT_ENDPOINT"]).rstrip("/")
        storage = context.get("AZURE_STORAGE_BLOB_ENDPOINT")
        try:
            public_ip: str | None = socket.gethostbyname(PUBLIC_HOST)
        except OSError:
            public_ip = None
        targets = {
            "public_host": PUBLIC_HOST,
            "public_ip": public_ip,
            "storage_host": urlparse(storage).hostname if isinstance(storage, str) else None,
        }
        self.obs["targets"] = {k: v for k, v in targets.items() if k != "public_ip"}
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=self.project_endpoint, credential=credential, allow_preview=True) as project,
        ):

            def read(name: str) -> Mapping[str, JsonValue]:
                return object_value(project.agents.get(name).as_dict())

            def token() -> str:
                return str(credential.get_token(DATA_SCOPE).token)

            before = self.guard_start(project, read)
            if before is None or self.toolbox is None:
                return
            with httpx.Client(timeout=180, trust_env=False) as http:
                toolbox = ToolboxClient(http, self.toolbox.endpoint, token())
                self.schema(project, toolbox)
                self.direct_probes(toolbox, targets)
                self.per_call_options(toolbox)
                self.execution_timeout(toolbox)
                if self.args.options_toolbox == "probe":
                    self.options_toolbox(project, http, token())
                else:
                    self.report.heading("Toolbox-level options")
                    self.report.paragraph("Not run (`--options-toolbox document`); see the tool schema above.")
                self.hosted_confirmation(project, token, targets)
                self.lifetime(http, token)
                self.cleanup(http, token())
            self.guard_end(read, before)

    def schema(self, project: AIProjectClient, toolbox: ToolboxClient) -> None:
        self.report.heading("Configuration surface")
        tool = next((item for item in toolbox.tools() if item.get("name") == "code_interpreter"), None)
        if tool is None:
            self.record("Tool schema", "FAIL", "tools/list returns code_interpreter", None)
            return
        description = str(tool.get("description") or "")
        meta = tool.get("_meta")
        properties = nested(meta, "properties")
        options: dict[str, JsonValue] = {}
        rows: list[list[object]] = []
        if isinstance(properties, dict):
            for name, spec in properties.items():
                allowed = nested(spec, "enum")
                if name == "network_policy":
                    variants = nested(spec, "oneOf")
                    allowed = (
                        [nested(v, "properties", "type", "enum") for v in variants]
                        if isinstance(variants, list)
                        else None
                    )
                options[name] = allowed
                detail = str(nested(spec, "description") or "").split("\n")[0].strip()
                rows.append([name, allowed or "free text", detail])
        self.obs["options"] = options
        self.obs["timeout_documented"] = bool(re.search(r"time out after 90 seconds", description))
        self.obs["internet_documented"] = "Internet access is disabled" in description
        self.record(
            "Tool schema",
            "PASS",
            "tools/list exposes code_interpreter and its per-call `_meta` options",
            {"options": sorted(options), "stateful_jupyter": "stateful Jupyter" in description},
        )
        self.report.paragraph("Per-call options declared in the tool's `_meta` schema:")
        self.report.table(["Option", "Allowed values", "Schema description"], rows)
        self.report.paragraph(
            "Tool description (platform-provided): "
            + "; ".join(
                line.replace("**", "").strip("-* ").strip() for line in description.splitlines()[3:] if line.strip()
            )
        )
        version = self.toolbox.version if self.toolbox else ""
        definition = object_value(project.toolboxes.get_version(TOOLBOX_NAME, version).as_dict())
        tools = definition.get("tools")
        container = nested(tools[0], "container") if isinstance(tools, list) and tools else None
        self.obs["toolbox_container"] = container
        self.record(
            "Step 1 toolbox definition",
            "PASS",
            "Read back the toolbox container configuration",
            {
                "type": nested(container, "type"),
                "file_ids": len(f) if isinstance(f := nested(container, "file_ids"), list) else 0,
                "memory_limit": nested(container, "memory_limit"),
                "network_policy": nested(container, "network_policy"),
            },
        )
        self.report.table(
            ["Layer", "Setting", "Value"],
            [["Host (hosted agent)", name, value] for name, value in HOST_SETTINGS],
        )

    def direct_probes(self, toolbox: ToolboxClient, targets: Mapping[str, str | None]) -> None:
        self.report.heading("Direct sandbox probes (operator identity, Step 1 toolbox)")
        code = sandbox_program(
            BOUNDARY_CODE, {"__TARGETS__": repr(json.dumps(dict(targets))), "__RESULT__": "story1-step4-direct.json"}
        )
        calls: dict[str, ToolCall] = {}
        for name in ("A", "B"):
            call = toolbox.call(code)
            calls[name] = call
            if call.container:
                self.probe_containers.append(call.container)
            self.record(
                f"Direct probe {name}",
                "PASS" if call.result else "FAIL",
                "Fixed boundary probe runs and returns its result",
                {"container": call.container, "seconds": call.seconds, "error": call.error},
            )
        a, b = calls["A"].result, calls["B"].result
        self.obs["direct_a"] = a
        self.obs["container_a"] = calls["A"].container
        if not a:
            return
        identity, privileges, filesystem = a.get("identity"), a.get("privileges"), a.get("filesystem")
        limits, runtime = a.get("limits"), a.get("runtime")
        self.report.table(
            ["Area", "Observed (probe A)"],
            [
                ["Identity", identity],
                ["Privileges (/proc/self/status)", privileges],
                ["Filesystem", filesystem],
                ["Visible processes", a.get("processes_visible")],
                ["Kernel", a.get("kernel")],
                [
                    "Limits",
                    {
                        **(limits if isinstance(limits, dict) else {}),
                        "memory": cgroup_memory_text(nested(a, "limits", "cgroup_memory_max")),
                        "disk_free": human_bytes(nested(a, "limits", "disk_free_bytes")),
                    },
                ],
                ["Runtime", runtime],
                ["Platform hint variable names (circumstantial)", a.get("platform_hint_env_names")],
            ],
        )
        self.report.table(
            ["Network target", "Outcome (probe A)"],
            [[name, outcome(a, "network", name)] for name in network_names(a)],
        )
        opened = network_open(a)
        self.obs["network_open"] = opened
        self.record(
            "Outbound network blocked",
            "FAIL" if opened else "PASS",
            "No DNS resolution or connection to public or customer endpoints (tool description: internet disabled)",
            opened or "all blocked",
        )
        cross = {
            "container_a": calls["A"].container,
            "container_b": calls["B"].container,
            "same_container": calls["A"].container == calls["B"].container,
            "marker_from_a_seen_in_b": nested(b, "filesystem", "marker_seen") if b else None,
            "same_hostname": nested(a, "fingerprint", "hostname_sha256") == nested(b, "fingerprint", "hostname_sha256")
            if b
            else None,
        }
        self.obs["cross_call"] = cross
        self.record(
            "Cross-call isolation",
            "PASS" if b else "FAIL",
            "Observe whether a file written in call A is visible to call B by the same principal",
            cross,
        )

    def per_call_options(self, toolbox: ToolboxClient) -> None:
        self.report.heading("Per-call `_meta` options on the Step 1 toolbox")
        limits = sandbox_program(
            LIMITS_CODE, {"__ALLOWED__": PUBLIC_HOST, "__OTHER__": OTHER_HOST, "__RESULT__": "story1-step4-limits.json"}
        )
        reuse = sandbox_program(REUSE_CHECK_CODE, {"__RESULT__": "story1-step4-reuse.json"})
        container_a = self.obs.get("container_a")
        probes: list[tuple[str, str, dict[str, JsonValue]]] = [
            ("memory_limit 4g", limits, {"memory_limit": "4g"}),
            (
                "network_policy allowlist",
                limits,
                {"network_policy": {"type": "allowlist", "allowed_domains": [PUBLIC_HOST]}},
            ),
        ]
        if isinstance(container_a, str):
            probes.append(("container reuse (probe A)", reuse, {"container": container_a}))
        rows: list[list[object]] = []
        effects: dict[str, JsonValue] = {}
        for label, code, meta in probes:
            call = toolbox.call(code, meta)
            if call.container:
                self.probe_containers.append(call.container)
            result = call.result or {}
            if label.startswith("memory"):
                effect = memory_applied(result.get("cgroup_memory_max"), "4g")
            elif label.startswith("network"):
                effect = f"https {PUBLIC_HOST}: {outcome(result, 'https_allowed')}"
            else:
                reused = call.container == container_a
                effect = "reused" if reused else f"ignored: new container, marker seen {result.get('marker_seen')}"
            effects[label] = call.error or effect
            rows.append([label, json.dumps(meta), call.container, call.error or effect])
            self.observe(
                f"Per-call {label}",
                "PASS" if call.result or call.error else "FAIL",
                "Record whether the platform applies the per-call option",
                {"container": call.container, "effect": call.error or effect},
            )
        self.obs["per_call"] = effects
        self.report.table(["Option", "`_meta` sent", "Container", "Observed effect"], rows)

    def execution_timeout(self, toolbox: ToolboxClient) -> None:
        self.report.heading("Execution time limit")
        call = toolbox.call(TIMEOUT_CODE)
        if call.container:
            self.probe_containers.append(call.container)
        finished = "story1-sleep-finished" in call.text
        result = execution_outcome(call)
        observed = {
            "outcome": result,
            "seconds": call.seconds,
            "finished_100s_sleep": finished,
            "is_error": call.is_error,
            "error": call.error,
            "output_excerpt": sanitize_text(call.text)[-200:],
        }
        self.obs["timeout"] = observed
        self.record(
            "Execution time limit",
            "FAIL" if result == "inconclusive" else "PASS",
            "Observe a 100 s execution against the per-execution limit (tool description: 90 s; documented: 220 s)",
            observed,
        )

    def options_toolbox(self, project: AIProjectClient, http: httpx.Client, token: str) -> None:
        """Create, probe, and always delete a probe-only toolbox that no agent uses."""
        self.report.heading("Toolbox-level options (temporary probe-only toolbox)")
        name = f"story1-sandbox-options-{self.run.run_id[:8]}"
        variants: list[tuple[str, AutoCodeInterpreterToolParam]] = [
            ("default", AutoCodeInterpreterToolParam()),
            ("memory_limit 4g", AutoCodeInterpreterToolParam(memory_limit="4g")),
            (
                "network_policy disabled",
                AutoCodeInterpreterToolParam(network_policy=ContainerNetworkPolicyDisabledParam()),
            ),
            (
                f"network_policy allowlist [{PUBLIC_HOST}]",
                AutoCodeInterpreterToolParam(
                    network_policy=ContainerNetworkPolicyAllowlistParam(allowed_domains=[PUBLIC_HOST])
                ),
            ),
        ]
        code = sandbox_program(
            LIMITS_CODE, {"__ALLOWED__": PUBLIC_HOST, "__OTHER__": OTHER_HOST, "__RESULT__": "story1-step4-option.json"}
        )
        rows: list[list[object]] = []
        effects: dict[str, JsonValue] = {}
        try:
            for label, param in variants:
                created = object_value(
                    project.toolboxes.create_version(
                        name,
                        tools=[CodeInterpreterToolboxTool(container=param)],
                        description="Story 1 AC4 probe-only toolbox; not attached to any agent; deleted by the run",
                        metadata={"story": "1", "purpose": "ac4-options-probe", "run_id": self.run.run_id},
                    ).as_dict()
                )
                version = str(created.get("version"))
                tools = created.get("tools")
                accepted = nested(tools[0], "container") if isinstance(tools, list) and tools else None
                call = ToolboxClient(http, toolbox_endpoint(self.project_endpoint, name, version), token).call(code)
                details = (
                    container_details(http, self.project_endpoint, token, call.container) if call.container else {}
                )
                if call.container:
                    self.probe_containers.append(call.container)
                result = call.result or {}
                effect = call.error or (
                    f"memory {cgroup_memory_text(result.get('cgroup_memory_max'))}, container API "
                    f"{details.get('memory_limit')}; https {PUBLIC_HOST}: {outcome(result, 'https_allowed')}, "
                    f"{OTHER_HOST}: {outcome(result, 'https_other')}"
                )
                effects[label] = effect
                if label == "memory_limit 4g" and not call.error:
                    self.obs["toolbox_memory_applied"] = (
                        memory_applied(result.get("cgroup_memory_max"), "4g") == "applied"
                    )
                if label.startswith("network_policy allowlist"):
                    self.obs["allowlist_applied"] = not call.error and outcome(result, "https_allowed") in OPEN_OUTCOMES
                rows.append([label, version, accepted, effect])
                self.observe(
                    f"Toolbox option {label}",
                    "PASS" if call.result or call.error else "FAIL",
                    "Record the effect of the toolbox-level option",
                    {"accepted": accepted, "effect": effect},
                )
        finally:
            deleted: Status = "FAIL"
            detail = ""
            try:
                project.toolboxes.delete(name)
                try:
                    project.toolboxes.get(name)
                    detail = "still readable after delete"
                except ResourceNotFoundError:
                    deleted, detail = "PASS", "deleted (read-back 404)"
            except AzureError as error:
                detail = sanitize_text(str(error))[:300]
            self.obs["options_toolbox"] = effects
            self.report.table(["Variant", "Version", "Accepted definition", "Observed"], rows)
            self.record("Probe-only toolbox deleted", deleted, f"Delete {name}", detail)

    def hosted_confirmation(
        self, project: AIProjectClient, token: Callable[[], str], targets: Mapping[str, str | None]
    ) -> None:
        self.report.heading("Hosted agent confirmation and cross-principal access")
        code = sandbox_program(
            BOUNDARY_CODE, {"__TARGETS__": repr(json.dumps(dict(targets))), "__RESULT__": "story1-step4-hosted.json"}
        )
        reply = self.hosted(
            project,
            token,
            "Hosted boundary probe",
            run_prompt(code, "Then reply with one sentence describing the printed result."),
            "story1-step4-hosted.json",
        )
        direct = self.obs.get("direct_a")
        hosted_view = boundary_view(reply.result)
        direct_view = boundary_view(direct if isinstance(direct, dict) else None)
        differences = {k: [direct_view[k], hosted_view[k]] for k in direct_view if direct_view[k] != hosted_view[k]}
        self.obs["hosted_container"] = reply.container
        self.obs["hosted_differences"] = differences
        self.record(
            "Hosted agent sandbox has the same boundaries",
            "PASS" if reply.result and not differences else "FAIL",
            "The existing agent's sandbox matches the direct probe on identity, privileges, limits, and network",
            {"container": reply.container, "differences": differences or "none", "error": reply.error},
        )
        self.record(
            "Operator reads the agent's sandbox outputs",
            "PASS" if reply.result else "FAIL",
            f"Documented: {DOCUMENTED_ISOLATION}",
            {
                "agent_container": reply.container,
                "read_by": "operator identity via the containers API",
                "result_file_downloaded": reply.result is not None,
            },
        )

    def lifetime(self, http: httpx.Client, token: Callable[[], str]) -> None:
        self.report.heading("Session lifetime")
        container = self.obs.get("container_a")
        if not isinstance(container, str):
            self.record("Container expiry policy", "FAIL", "Probe A container is known", None)
            return
        reply = http.get(f"{self.project_endpoint}/openai/v1/containers/{container}", headers=bearer(token()))
        info = object_value(reply.json()) if reply.status_code == 200 else {}
        policy = info.get("expires_after")
        self.obs["expires_after"] = policy
        self.record(
            "Container expiry policy",
            "PASS" if policy else "FAIL",
            f"Documented: {DOCUMENTED_LIFETIME}",
            {"expires_after": policy, "memory_limit": info.get("memory_limit"), "status": info.get("status")},
        )
        if self.args.idle_wait != "wait":
            self.obs["idle"] = "not run (opt-in)"
            return
        minutes = nested(policy, "minutes")
        last_active = info.get("last_active_at")
        if not isinstance(minutes, int) or not isinstance(last_active, int):
            self.record("Idle expiry", "FAIL", "Container reports last_active_at and minutes", info)
            return
        deadline = last_active + minutes * 60 + IDLE_MARGIN_SECONDS
        wait = max(0, min(IDLE_MAX_SECONDS, deadline - int(time.time())))
        self.run.console.print(f"Idle-expiry wait: {wait // 60} min {wait % 60} s (container left untouched) ...")
        end = time.monotonic() + wait
        while (remaining := end - time.monotonic()) > 0:
            time.sleep(min(60, remaining))
            self.run.console.print(f"  {max(0, int(end - time.monotonic())) // 60} min remaining")
        after = http.get(f"{self.project_endpoint}/openai/v1/containers/{container}", headers=bearer(token()))
        state = object_value(after.json()).get("status") if after.status_code == 200 else f"HTTP {after.status_code}"
        expired = after.status_code == 404 or state == "expired"
        self.obs["idle"] = f"{state} after {round(wait / 60, 1)} min idle"
        self.record(
            "Idle expiry",
            "PASS" if expired else "FAIL",
            f"Container expires {minutes} min after last activity (observed policy)",
            {"waited_minutes": round(wait / 60, 1), "status_after": state},
        )

    def cleanup(self, http: httpx.Client, token: str) -> None:
        self.report.heading("Cleanup")
        outcomes: dict[str, int] = {}
        for container in dict.fromkeys(self.probe_containers):
            response = http.delete(f"{self.project_endpoint}/openai/v1/containers/{container}", headers=bearer(token))
            outcomes[container] = response.status_code
        failed = {c: s for c, s in outcomes.items() if s not in (200, 404)}
        self.obs["deleted_containers"] = len(outcomes) - len(failed)
        self.record(
            "Direct-probe containers deleted",
            "FAIL" if failed else "PASS",
            "Delete only the sandbox containers this run's direct probes created (200 or already gone)",
            {"deleted_or_gone": len(outcomes) - len(failed), "failed": failed or "none"},
        )

    def conclude(self) -> None:
        self.verdict = step4_verdict(self.statuses)
        o = self.obs
        a = o.get("direct_a")
        per_call = as_mapping(o.get("per_call"))
        options = as_mapping(o.get("options_toolbox"))
        timeout = as_mapping(o.get("timeout"))
        network = {name: outcome(a, "network", name) for name in network_names(a)}
        memory = cgroup_memory_text(nested(a, "limits", "cgroup_memory_max"))
        allowlist = next((v for k, v in options.items() if "allowlist" in k), None)
        minutes = nested(o.get("expires_after"), "minutes")
        identity, privileges = nested(a, "identity"), nested(a, "privileges")
        results = [
            f"Runs as `{nested(identity, 'user')}` (uid {nested(identity, 'uid')}) with capabilities "
            f"{nested(privileges, 'cap_eff')}, NoNewPrivs {nested(privileges, 'no_new_privs')}; the root filesystem "
            "is read-only and only /tmp and /mnt/data are writable.",
            "Network: " + ", ".join(f"{name} = {value}" for name, value in network.items()) + ".",
            "Every tool call got a new container; a file written in one call was not visible in the next.",
            "The operator's identity downloaded the hosted agent's sandbox output through the containers API.",
            f"Containers report expiry {minutes} minutes after last activity; idle test: {o.get('idle')}.",
            f"{runtime_text(timeout)}.",
            f"memory_limit 4g: per call {per_call.get('memory_limit 4g', 'not run')}; toolbox level "
            f"{options.get('memory_limit 4g', 'not run')}.",
            f"Network allowlist at toolbox level: {allowlist or 'not run'}.",
        ]
        memory_seen = [value for value in (per_call.get("memory_limit 4g"), options.get("memory_limit 4g")) if value]
        memory_ok = per_call.get("memory_limit 4g") == "applied" or o.get("toolbox_memory_applied") is True
        same_container = nested(o.get("cross_call"), "same_container")
        operator_read = self.statuses.get("Operator reads the agent's sandbox outputs") == "PASS"
        idle_status = self.statuses.get("Idle expiry")
        timeout_outcome = timeout.get("outcome")
        runtime_outcomes: dict[str, Outcome] = {
            "completed": "LIMITATION",
            "stopped": "LIMITATION",
            "inconclusive": "FAIL",
        }
        findings = [
            Finding(
                "The sandbox is an Azure Container Apps dynamic session isolated by a Hyper-V boundary",
                "Microsoft documents this; it can't be inspected directly. The platform variable names seen in the "
                f"sandbox ({nested(a, 'platform_hint_env_names')}) are consistent with it",
                "Documented",
                "Foundry-managed",
                ("ci_sandbox", "aca_isolation"),
                outcome="DOCUMENTED",
            ),
            Finding(
                "Code runs unprivileged with a read-only root filesystem",
                f"User `{nested(identity, 'user')}`, capabilities {nested(privileges, 'cap_eff')}, NoNewPrivs "
                f"{nested(privileges, 'no_new_privs')}; only /tmp and /mnt/data are writable",
                "Tested",
                "Foundry-managed",
                ("ci_sandbox",),
                outcome=checks_outcome(self.statuses.get("Direct probe A")),
            ),
            Finding(
                "Outbound network access from the sandbox is blocked (public internet, raw IP, customer storage)",
                "All outbound attempts were blocked" if not o.get("network_open") else f"Open: {o.get('network_open')}",
                "Tested",
                "Foundry-managed",
                ("ci_network", "toolbox_network"),
                outcome=checks_outcome(self.statuses.get("Outbound network blocked")),
            ),
            Finding(
                "Network allowlist (network_policy) to enable selected egress",
                (
                    f"Applied: the allowlisted host was reachable ({allowlist})"
                    if o.get("allowlist_applied")
                    else f"Not available in this project: {allowlist}. An organization policy may block it; confirm "
                    "with your administrator or Microsoft"
                )
                if allowlist
                else "Not run (--options-toolbox document)",
                "Tested" if allowlist else "Documented",
                "Foundry-managed" if o.get("allowlist_applied") else "Not supported",
                ("sdk_network", "ci_network"),
                outcome=("PASS" if o.get("allowlist_applied") else "NOT SUPPORTED") if allowlist else "NOT RUN",
            ),
            Finding(
                "Larger sandbox memory (memory_limit 4g)",
                (
                    f"Applied: per call {per_call.get('memory_limit 4g', 'not run')}; toolbox level "
                    f"{options.get('memory_limit 4g', 'not run')}"
                    if memory_ok
                    else f"Not applied: the sandbox stayed at {memory} per call and at toolbox level. No Microsoft "
                    "page explains this"
                )
                if memory_seen
                else "Not run",
                "Tested",
                "Foundry-managed" if memory_ok else "Not supported",
                ("sdk_memory",),
                outcome=("PASS" if memory_ok else "NOT SUPPORTED") if memory_seen else "NOT RUN",
            ),
            Finding(
                "Each tool call runs in a new container; container reuse is ignored",
                (
                    "Every tool call got a new container, even within one conversation, and files from one call were "
                    "not visible in the next. Microsoft describes separate sessions per conversation"
                    if same_container is False
                    else "Calls reused the same container"
                )
                if same_container is not None
                else "Not measured",
                "Tested",
                "Foundry-managed",
                ("ci_sessions",),
                outcome=("LIMITATION" if same_container is False else "PASS")
                if same_container is not None
                else "NOT RUN",
            ),
            Finding(
                "Per-user isolation of sandbox files",
                (
                    "Another principal (the operator) downloaded the agent's sandbox output through the containers API"
                    if operator_read
                    else "Not confirmed in this run; Microsoft documents that all project users share the container "
                    "context"
                )
                + f". Untested options that may provide it: {isolation_options_link(self.report.path.parent)}",
                "Tested" if operator_read else "Documented",
                "Not supported",
                ("ci_isolation",),
                outcome="NOT SUPPORTED",
            ),
            Finding(
                "Idle container expiry",
                (
                    f"The container reports a {minutes}-minute policy; idle test: {o.get('idle')}. This matches the "
                    "Responses API container docs (20 min), not the Code Interpreter page (30 min idle)"
                ),
                "Tested" if idle_status else "Observed",
                "Foundry-managed",
                ("responses_containers", "ci_lifetime"),
                outcome="FAIL" if idle_status == "FAIL" else ("LIMITATION" if minutes else "NOT RUN"),
            ),
            Finding(
                "Per-execution runtime limit",
                runtime_text(timeout),
                "Tested" if timeout.get("outcome") in ("completed", "stopped") else "Documented",
                "Foundry-managed",
                ("aca_runtime",),
                outcome=runtime_outcomes.get(str(timeout_outcome), "NOT RUN"),
            ),
            Finding(
                "Hosted agent resources and session idle timeout are configured separately from the sandbox",
                "Set in azure.yaml: 0.5 vCPU / 1 GiB and a 120 s session idle timeout. They don't change the "
                "sandbox limits",
                "Documented",
                "Custom implementation (Dev Team)",
                ("hosted_sizes", "hosted_sessions"),
                outcome="DOCUMENTED",
            ),
            Finding(
                "Alternative when isolation, network, or larger limits are required",
                "A custom code interpreter on your own Azure Container Apps session pool (preview; not provisioned "
                f"here). See {isolation_options_link(self.report.path.parent)}",
                "Documented",
                "Custom implementation (Dev Team)",
                ("custom_ci",),
                outcome="DOCUMENTED",
            ),
        ]
        gaps = [
            "Confirm with Microsoft: the 20-minute container expiry versus the documented 30-minute idle timeout, "
            "why memory_limit above 2 GiB is not applied, and which organization policy blocks network allowlists.",
            "Executions longer than the documented 220 s maximum were not tested.",
            "With a toolbox in a hosted agent there is no per-user sandbox isolation; sensitive multi-tenant data "
            f"needs a custom design. See {isolation_options_link(self.report.path.parent)}.",
        ]
        conclusion = (
            "Every area (security boundary, session lifetime, network, configuration) has tested or documented "
            "evidence. Three platform behaviors differ from Microsoft's Code Interpreter page and need confirmation."
            if self.verdict == "Met"
            else "See the checks below."
        )
        self.report.summary(render_summary(self.verdict, conclusion, results, findings, gaps))
        self.matrix(a, per_call, options, timeout)

    def matrix(
        self,
        a: JsonValue,
        per_call: Mapping[str, Any],
        options: Mapping[str, Any],
        timeout: Mapping[str, Any],
    ) -> None:
        """Detailed documented-versus-observed matrix, one row per behavior, with its Microsoft source."""
        o = self.obs
        per_call_text = "; ".join(f"{k}: {v}" for k, v in per_call.items()) or "-"
        options_text = "; ".join(f"{k}: {v}" for k, v in options.items()) or "not run"
        rows: list[tuple[str, str, str, object, tuple[str, ...]]] = [
            (
                "Security",
                "Process identity and privileges",
                "Sandbox for untrusted code",
                {"identity": nested(a, "identity"), "privileges": nested(a, "privileges")},
                ("ci_sandbox",),
            ),
            (
                "Security",
                "Filesystem scope",
                "Files you attach are available; generated files are downloadable",
                nested(a, "filesystem"),
                ("ci_sandbox",),
            ),
            (
                "Security",
                "Isolation between tool calls",
                "Separate sessions per conversation",
                o.get("cross_call"),
                ("ci_sessions",),
            ),
            (
                "Security",
                "Isolation between principals",
                "User isolation isn't supported with a toolbox",
                f"Operator read the agent's sandbox output (container {o.get('hosted_container')})",
                ("ci_isolation",),
            ),
            (
                "Security",
                "Isolation technology",
                "ACA dynamic sessions, Hyper-V boundary",
                f"Platform variable names: {nested(a, 'platform_hint_env_names')}",
                ("ci_sandbox", "aca_isolation"),
            ),
            (
                "Session lifetime",
                "Container expiry",
                "1 h active / 30 min idle (Code Interpreter); 20 min unused (Responses API containers)",
                {"expires_after": o.get("expires_after"), "idle_test": o.get("idle")},
                ("ci_lifetime", "responses_containers"),
            ),
            (
                "Session lifetime",
                "Per-execution time limit",
                "220 s maximum (ACA code interpreter sessions)",
                timeout,
                ("aca_runtime",),
            ),
            (
                "Network",
                "Outbound access",
                "Dynamic sessions can't make outbound network requests",
                {name: outcome(a, "network", name) for name in network_names(a)},
                ("ci_network", "toolbox_network"),
            ),
            (
                "Network",
                "network_policy",
                "disabled or allowlist (SDK)",
                f"Per call: {per_call_text}. Toolbox level: {options_text}",
                ("sdk_network",),
            ),
            (
                "Configuration",
                "memory_limit",
                "1g / 4g / 16g / 64g (SDK)",
                f"Enforced {cgroup_memory_text(nested(a, 'limits', 'cgroup_memory_max'))} regardless of the option",
                ("sdk_memory",),
            ),
            (
                "Configuration",
                "Toolbox container definition",
                "Toolbox exposes tools through one MCP endpoint",
                o.get("toolbox_container"),
                ("toolbox_overview",),
            ),
            (
                "Configuration",
                "Hosted agent host resources",
                "Sizes and session idle timeout",
                dict(HOST_SETTINGS),
                ("hosted_sizes", "hosted_sessions"),
            ),
            (
                "Configuration",
                "Python runtime",
                "Fixed set of common data science packages",
                nested(a, "runtime"),
                ("ci_packages",),
            ),
            (
                "Configuration",
                "Alternative environment",
                "Custom code interpreter on ACA dynamic sessions",
                "Not provisioned",
                ("custom_ci",),
            ),
        ]
        self.report.heading("Boundary matrix")
        self.report.paragraph(
            "Each row compares what Microsoft documents with what this run observed. " + runtime_text(timeout) + "."
        )
        self.report.table(
            ["Area", "Behavior", "Microsoft documents", "Observed in this run", "Microsoft documentation"],
            [[area, behavior, doc, seen, doc_cell(keys)] for area, behavior, doc, seen, keys in rows],
        )


STEP5 = StepSpec(1, 5, "state-continuity")
STEP5_TASK_LABEL = "Agents: Story 1 - 5 Assess state continuity"
AC5 = (
    "Assess state continuity across requests, rehydration after session recreation, and whether state storage "
    "can reside in the customer's network."
)
CHECK_REPLY = (
    "Then reply with exactly two lines. Line 1: `NONCE: <value>`, where <value> is the nonce string that appears "
    "in the Python code of an earlier user message in this conversation, or `NONCE: unknown` if there is no such "
    "earlier message. Line 2: one sentence describing the printed result."
)
CORE_CHECKS = ("R1 builds state", "Checkpoint in customer Blob", "Rehydration")


def step5_verdict(statuses: Mapping[str, Status]) -> str:
    if statuses.get("R1 builds state") is None:
        return "Not evaluated"
    core = [statuses.get(name) for name in CORE_CHECKS]
    if statuses.get("Agents reused - after") == "FAIL":
        return "Not met"
    if all(status == "PASS" for status in core):
        return "Met"
    return "Partially met" if statuses.get("R1 builds state") == "PASS" else "Not met"


def presence(value: JsonValue) -> str:
    return "present" if value not in (None, {}, []) else "absent"


class Step5(SandboxStep):
    """Continuity, recreation, and checkpoint rehydration across the state layers of the existing agent."""

    spec = STEP5
    title = "Story 1 / Step 5 - State continuity, rehydration, and customer-network storage"

    def __init__(self, args: argparse.Namespace, run: ValidationRun, report: MarkdownReport) -> None:
        super().__init__(args, run, report)
        self.dataset = self.datasets[1]
        self.expected: dict[str, CategoryTotals] = expected_aggregates(generate_rows(self.dataset.rows))
        self.nonce = self.run.run_id[:12]
        self.replies: dict[str, HostedReply] = {}
        self.checkpoint: dict[str, JsonValue] | None = None
        self.blob_name = f"story1/checkpoints/{self.run.run_id}.json"
        self.obs: dict[str, Any] = {}

    @property
    def destructive(self) -> bool:
        """Only explicit destructive mode permits deletion; unknown/missing modes retain evidence."""
        return getattr(self.args, "lifecycle_mode", "observe") == "destructive"

    def execute(self) -> None:
        self.header(
            AC5,
            "AC5",
            STEP5_TASK_LABEL,
            [["Dataset", self.dataset.filename], ["Blob checkpoint", self.args.blob_checkpoint]],
        )
        self.report.paragraph(
            'Reuses the existing agents and the Step 1 toolbox without changing them. "Session recreation" means '
            "the Code Interpreter sandbox container only; the agent is never recreated or redeployed. "
            "Original containers and checkpoints are retained unless explicit destructive mode is selected. "
            "Hosted session continuity is a bounded best-effort observation, separate from required Blob "
            "reconstruction; a failed probe does not trigger an automatic recovery redesign."
        )
        context = read_environment(self.args.environment, self.report)
        self.project_endpoint = str(context["FOUNDRY_PROJECT_ENDPOINT"]).rstrip("/")
        endpoint = context.get("AZURE_STORAGE_BLOB_ENDPOINT")
        container = context.get("AZURE_STORAGE_CONTAINER_NAME")
        storage_host = urlparse(endpoint).hostname if isinstance(endpoint, str) else None
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=self.project_endpoint, credential=credential, allow_preview=True) as project,
        ):

            def read(name: str) -> Mapping[str, JsonValue]:
                return object_value(project.agents.get(name).as_dict())

            def token() -> str:
                return str(credential.get_token(DATA_SCOPE).token)

            before = self.guard_start(project, read)
            if before is None or self.toolbox is None:
                return
            self.layers()
            self.continuity(project, token, storage_host or "invalid.invalid")
            with httpx.Client(timeout=60, trust_env=False) as http:
                blob = (
                    BlobStore(http, endpoint, container, credential.get_token(STORAGE_SCOPE).token)
                    if self.args.blob_checkpoint == "write" and isinstance(endpoint, str) and isinstance(container, str)
                    else None
                )
                self.save_checkpoint(blob)
                self.recreate(http, token())
                self.rehydrate(project, token, blob)
                if blob is not None:
                    self.cleanup_blob(blob)
            self.guard_end(read, before)

    def layers(self) -> None:
        self.report.heading("State layers tracked")
        self.report.table(
            ["Layer", "Identifier recorded", "Owner"],
            [
                ["Responses conversation", "conversation ID per request", "Foundry-managed"],
                ["Hosted agent session", "agent_session_id from each response", "Foundry-managed"],
                ["Code Interpreter sandbox", "container ID + hashed hostname/boot ID", "Foundry-managed"],
                ["Python kernel variable", "`story1_state`", "Foundry-managed (sandbox)"],
                ["Sandbox file", "`/mnt/data/story1-state-marker.json`", "Foundry-managed (sandbox)"],
                ["Mounted inputs", f"toolbox file ID for {self.dataset.filename}", "Foundry-managed"],
                [
                    "Custom checkpoint",
                    "versioned JSON with SHA-256 seal (local and customer Blob)",
                    "Custom implementation (Dev Team)",
                ],
            ],
        )

    def continuity(self, project: AIProjectClient, token: Callable[[], str], storage_host: str) -> None:
        self.report.heading("Continuity across requests")
        write = sandbox_program(
            STATE_WRITE_CODE,
            {
                "__FILE__": self.dataset.filename,
                "__NONCE__": self.nonce,
                "__STORAGE_HOST__": storage_host,
                "__RESULT__": "story1-step5-r1.json",
            },
        )
        c1 = self.new_conversation(project)
        r1 = self.hosted(
            project,
            token,
            "R1 write state (C1)",
            run_prompt(write, "Then reply with one sentence describing the printed result."),
            "story1-step5-r1.json",
            c1,
        )
        self.replies["R1"] = r1
        result = r1.result or {}
        correct = result.get("input_sha256") == self.dataset.sha256 and aggregates_match(
            result.get("by_category"), self.expected
        )
        self.record(
            "R1 builds state",
            "PASS" if r1.result and correct else ("BLOCKED" if r1.status == "BLOCKED" else "FAIL"),
            f"Loads {self.dataset.filename}, sets `story1_state`, writes the marker, and computes exact aggregates",
            {"container": r1.container, "aggregates_exact": correct, "seconds": r1.seconds, "error": r1.error},
        )
        self.obs["customer_storage_from_sandbox"] = result.get("customer_storage")
        if not r1.result:
            return
        check = {
            phase: sandbox_program(STATE_CHECK_CODE, {"__PHASE__": phase, "__RESULT__": f"story1-step5-{phase}.json"})
            for phase in ("r2", "r3")
        }
        r2 = self.hosted(
            project, token, "R2 check state (C1)", run_prompt(check["r2"], CHECK_REPLY), "story1-step5-r2.json", c1
        )
        c2 = self.new_conversation(project)
        r3 = self.hosted(
            project, token, "R3 check state (C2)", run_prompt(check["r3"], CHECK_REPLY), "story1-step5-r3.json", c2
        )
        self.replies.update({"R2": r2, "R3": r3})
        for reply, label in ((r2, "R2 same conversation"), (r3, "R3 new conversation")):
            data = reply.result or {}
            self.record(
                label,
                "PASS" if reply.result else ("BLOCKED" if reply.status == "BLOCKED" else "FAIL"),
                "Check the variable, marker file, sandbox identity, and conversation recall after R1",
                {
                    "variable": presence(data.get("variable")),
                    "marker": presence(data.get("marker")),
                    "same_sandbox_as_r1": same_sandbox(r1, reply),
                    "nonce_recalled": nonce_line(reply.text),
                    "error": reply.error,
                },
            )
        rows: list[list[object]] = []
        for name, reply in self.replies.items():
            data = reply.result or {}
            rows.append(
                [
                    name,
                    reply.conversation,
                    reply.agent_session_id,
                    reply.container,
                    nested(data, "fingerprint", "hostname_sha256"),
                    "set" if name == "R1" else presence(data.get("variable")),
                    "written" if name == "R1" else presence(data.get("marker")),
                    "-" if name == "R1" else len(i) if isinstance(i := data.get("inputs"), list) else "-",
                    "-" if name == "R1" else nonce_line(reply.text) or "no NONCE line",
                    status_icon(reply.status),
                ]
            )
        self.report.table(
            [
                "Request",
                "Conversation",
                "Agent session",
                "Sandbox container",
                "Host fingerprint",
                "`story1_state`",
                "Marker file",
                "Inputs mounted",
                "Conversation recall (nonce)",
                "Status",
            ],
            rows,
        )
        self.report.paragraph(f"R1 wrote nonce `{self.nonce}`; R2 and R3 code does not contain it.")

    def save_checkpoint(self, blob: BlobStore | None) -> None:
        self.report.heading("Checkpoint")
        r1 = self.replies.get("R1")
        if r1 is None or r1.result is None or self.toolbox is None or self.statuses.get("R1 builds state") != "PASS":
            self.report.paragraph("Not created: R1 produced no validated state.")
            self.record("Checkpoint in customer Blob", "BLOCKED", "Validated R1 state exists", "No validated state")
            return
        file_id = self.toolbox.file_ids.get(str(self.dataset.rows), "")
        self.checkpoint = build_checkpoint(
            self.run.run_id,
            self.dataset,
            file_id,
            r1.result,
            {"conversation": r1.conversation, "response_id": r1.response_id, "container": r1.container},
        )
        sealed = seal_checkpoint(self.checkpoint)
        local = self.run.path / "checkpoint.json"
        local.write_bytes(sealed)
        self.record(
            "Checkpoint saved locally",
            "PASS",
            "Versioned, sealed, data-free checkpoint in the run folder",
            {"file": local.name, "bytes": len(sealed), "id": self.checkpoint["id"]},
        )
        self.report.code(sealed.decode("utf-8"), "json")
        if blob is None:
            self.record(
                "Checkpoint in customer Blob",
                "BLOCKED",
                "Required authenticated customer Blob write and read-back",
                "Not run (--blob-checkpoint skip or no Blob endpoint); local bytes are diagnostic only",
            )
            return
        status: Status = "PASS"
        detail = "Authenticated Blob upload and exact, validated read-back succeeded; checkpoint retained"
        try:
            persist_and_read_checkpoint(
                blob, self.blob_name, sealed, self.dataset, file_id, expected_source=self.checkpoint_source()
            )
        except BlobCheckpointError as error:
            status, detail = error.status, str(error)
        self.record(
            "Checkpoint in customer Blob",
            status,
            f"Write and read back {blob.container}/{self.blob_name} with the operator identity",
            detail,
        )

    def recreate(self, http: httpx.Client, token: str) -> None:
        self.report.heading("Session recreation")
        if not self.destructive:
            self.record(
                "Sandbox recreation",
                "SKIP",
                "Retain the original container and files",
                "Observe mode: deletion not attempted; reconstruction uses a separate request",
            )
            return
        r1 = self.replies.get("R1")
        if r1 is None or r1.container is None:
            self.record("Sandbox recreation", "BLOCKED", "R1 container known", "R1 did not return a container")
            return
        base = f"{self.project_endpoint}/openai/v1/containers/{r1.container}"
        before = http.get(f"{base}/files", headers=bearer(token))
        deleted = http.delete(base, headers=bearer(token))
        after = http.get(base, headers=bearer(token))
        files_after = http.get(f"{base}/files", headers=bearer(token))
        observed = {
            "container": r1.container,
            "files_before": before.status_code,
            "delete": deleted.status_code,
            "read_after": after.status_code,
            "files_after": files_after.status_code,
        }
        self.obs["recreation"] = observed
        status: Status = "PASS" if deleted.status_code == 200 and after.status_code == 404 else "FAIL"
        if deleted.status_code == 403:
            status = "BLOCKED"
        self.record(
            "Sandbox recreation",
            status,
            "Delete R1's sandbox container; it and its files are no longer readable (the agent is untouched)",
            observed,
        )

    def rehydrate(self, project: AIProjectClient, token: Callable[[], str], blob: BlobStore | None) -> None:
        self.report.heading("Rehydration from the checkpoint")
        if self.checkpoint is None or self.toolbox is None:
            self.record("Rehydration", "BLOCKED", "A validated checkpoint exists", "No checkpoint")
            return
        if blob is None or self.statuses.get("Checkpoint in customer Blob") != "PASS":
            self.record(
                "Rehydration",
                "BLOCKED",
                "Required reconstruction reads the verified customer Blob checkpoint",
                "No successful Blob write/read-back; local diagnostic checkpoint is never substituted",
            )
            return
        file_id = self.toolbox.file_ids.get(str(self.dataset.rows), "")
        try:
            body = read_blob_checkpoint(
                blob,
                self.blob_name,
                self.dataset,
                file_id,
                expected_source=self.checkpoint_source(),
                expected_sha256=sha256(seal_checkpoint(self.checkpoint)).hexdigest(),
            )
        except BlobCheckpointError as error:
            self.record("Rehydration", error.status, "Required Blob checkpoint validates without fallback", str(error))
            return
        code = sandbox_program(
            REHYDRATE_CODE, {"__CHECKPOINT__": repr(json.dumps(body)), "__RESULT__": "story1-step5-r4.json"}
        )
        reply = self.hosted(
            project,
            token,
            "R4 rehydrate (C3)",
            run_prompt(code, "Then reply with one sentence describing the printed result."),
            "story1-step5-r4.json",
            self.new_conversation(project),
        )
        self.replies["R4"] = reply
        result = reply.result or {}
        saved = nested(body, "result", "by_category")
        rebuilt = result.get("by_category")
        exact = aggregates_match(rebuilt, self.expected) and rebuilt == saved
        r1 = self.replies.get("R1")
        different = bool(r1 and r1.container and reply.container and reply.container != r1.container)
        ok = (
            bool(reply.result)
            and result.get("input_sha256_matches") is True
            and result.get("matches_checkpoint") is True
            and exact
            and different
        )
        self.record(
            "Rehydration",
            "PASS" if ok else ("BLOCKED" if reply.status == "BLOCKED" else "FAIL"),
            "New sandbox reloads the mounted input, verifies its SHA-256, and rebuilds exactly the checkpointed state",
            {
                "checkpoint_source": "customer Blob",
                "container": reply.container,
                "different_sandbox_from_r1": different,
                "input_sha256_matches": result.get("input_sha256_matches"),
                "matches_checkpoint": result.get("matches_checkpoint"),
                "matches_local_oracle": aggregates_match(rebuilt, self.expected),
                "error": reply.error,
            },
        )
        rows: list[list[object]] = [
            [
                name,
                *[nested(saved, name, k) for k in ("rows", "units", "revenue_cents")],
                *[nested(rebuilt, name, k) for k in ("rows", "units", "revenue_cents")],
            ]
            for name in self.expected
        ]
        self.report.table(
            [
                "Category",
                "Rows (checkpoint)",
                "Units (checkpoint)",
                "Revenue (checkpoint)",
                "Rows (rebuilt)",
                "Units (rebuilt)",
                "Revenue (rebuilt)",
            ],
            rows,
        )

    def checkpoint_source(self) -> Mapping[str, JsonValue]:
        """Trusted source binding for this run, not values taken from downloaded Blob bytes."""
        r1 = self.replies.get("R1")
        return {
            "conversation": r1.conversation if r1 else None,
            "response_id": r1.response_id if r1 else None,
            "container": r1.container if r1 else None,
        }

    def cleanup_blob(self, blob: BlobStore) -> None:
        if not self.destructive:
            self.record(
                "Blob checkpoint cleanup", "SKIP", "Retain the checkpoint", "Observe mode: deletion not attempted"
            )
            return
        if self.statuses.get("Checkpoint in customer Blob") != "PASS":
            return
        deleted = blob.delete(self.blob_name)
        gone = blob.get(self.blob_name)
        ok = deleted.is_success and gone.status_code == 404
        self.record(
            "Blob checkpoint cleanup",
            "PASS" if ok else "FAIL",
            "Delete only this run's synthetic checkpoint",
            {"delete": deleted.status_code, "read_after": gone.status_code},
        )

    def conclude(self) -> None:
        self.verdict = step5_verdict(self.statuses)
        r = self.replies
        r1, r2, r3, r4 = (r.get(name) for name in ("R1", "R2", "R3", "R4"))
        recall2 = nonce_line(r2.text) if r2 else None
        recall3 = nonce_line(r3.text) if r3 else None
        recreation = self.obs.get("recreation")
        storage = self.obs.get("customer_storage_from_sandbox")

        def lost(reply: HostedReply | None, key: str) -> str:
            if reply is None or reply.result is None:
                return "not measured"
            return "survived" if reply.result.get(key) not in (None, {}, []) else "lost"

        self.report.heading("What survived, was lost, or was rebuilt")
        self.report.table(
            ["Layer", "Next request, same conversation (R2)", "New conversation (R3)", "After recreation (R4)"],
            [
                [
                    "Conversation history",
                    "recalled" if recall2 == self.nonce else f"not recalled ({recall2})",
                    "isolated" if recall3 in (None, "unknown") else f"leaked ({recall3})",
                    "new conversation (C3)",
                ],
                [
                    "Hosted agent session",
                    "same" if r1 and r2 and r1.agent_session_id == r2.agent_session_id else "different",
                    "same" if r1 and r3 and r1.agent_session_id == r3.agent_session_id else "different",
                    "-",
                ],
                [
                    "Sandbox container",
                    "same" if r1 and r2 and same_sandbox(r1, r2) else "new",
                    "same" if r1 and r3 and same_sandbox(r1, r3) else "new",
                    "new" if r4 and r1 and r4.container != r1.container else "-",
                ],
                [
                    "`story1_state` variable",
                    lost(r2, "variable"),
                    lost(r3, "variable"),
                    "rebuilt from checkpoint" if self.statuses.get("Rehydration") == "PASS" else "-",
                ],
                ["Marker file in /mnt/data", lost(r2, "marker"), lost(r3, "marker"), "-"],
                [
                    "R1 generated files",
                    "readable until deleted",
                    "readable until deleted",
                    "lost (container deleted)"
                    if isinstance(recreation, dict) and recreation.get("files_after") == 404
                    else "-",
                ],
                [
                    "Mounted toolbox inputs",
                    "re-mounted",
                    "re-mounted",
                    "re-mounted, SHA-256 verified" if self.statuses.get("Rehydration") == "PASS" else "-",
                ],
                [
                    "Custom checkpoint",
                    "saved (local"
                    + (", customer Blob)" if self.statuses.get("Checkpoint in customer Blob") == "PASS" else ")"),
                    "unaffected",
                    "used to rebuild state" if self.statuses.get("Rehydration") == "PASS" else "not reconstructed",
                ],
            ],
        )
        self.placement(storage)
        self.write_summary(recall2, recall3, recreation, storage)

    def placement(self, storage: JsonValue) -> None:
        blob = self.statuses.get("Checkpoint in customer Blob") == "PASS"
        rows: list[tuple[str, str, str, str, str, tuple[str, ...]]] = [
            (
                "Code Interpreter sandbox (kernel variables, /mnt/data)",
                "Microsoft-managed sandbox",
                "No. It does not inherit the agent's network and cannot make outbound requests (from R1's sandbox, "
                f"customer storage DNS: {outcome(storage, 'dns')}, HTTPS: {outcome(storage, 'https')})",
                "Tested + Documented",
                "Not supported",
                ("ci_network", "toolbox_network"),
            ),
            (
                "Uploaded files (toolbox inputs)",
                "Foundry project storage; this POC uses the Basic setup (Microsoft-managed)",
                "Yes, with the Standard agent setup: bring-your-own Azure Storage holds uploaded files",
                "Documented (not tested)",
                "Foundry-managed (customer-owned resources)",
                ("standard_files",),
            ),
            (
                "Conversations and responses",
                "Stored in Foundry (Basic setup)",
                "Yes, with the Standard agent setup: bring-your-own Cosmos DB holds conversation history. Whether the "
                "hosted-agent runtime uses that store is not stated",
                "Documented (not tested)",
                "Foundry-managed (customer-owned resources)",
                ("standard_threads", "hosted_conversations"),
            ),
            (
                "Hosted workflow state (POC_WORKFLOW registry)",
                "Foundry-managed hosted agent state store",
                "No bring-your-own option is documented; the store is provided and secured by Foundry",
                "Documented",
                "Foundry-managed",
                ("state_store",),
            ),
            (
                "Custom checkpoint",
                "Run folder and the customer's `artifacts` Blob container (network security perimeter, no private "
                "endpoint)",
                ("Customer-owned Blob write/read-back succeeded. " if blob else "Not proven in this run. ")
                + "A private endpoint is an untested architectural option because the client mediates",
                "Tested (Blob) + Architectural assessment (private endpoint)" if blob else "Architectural assessment",
                "Custom implementation (Dev Team)",
                ("blob_rbac", "nsp", "storage_private_endpoints"),
            ),
        ]
        self.report.heading("Customer-network placement")
        self.report.table(
            [
                "Layer",
                "Where it lives in this POC",
                "Can it reside in the customer's network?",
                "Basis",
                "Ownership",
                "Microsoft documentation",
            ],
            [[layer, now, answer, basis, owner, doc_cell(keys)] for layer, now, answer, basis, owner, keys in rows],
        )

    def write_summary(
        self, recall2: str | None, recall3: str | None, recreation: JsonValue, storage: JsonValue
    ) -> None:
        r = self.replies
        r1, r2, r3, r4 = (r.get(name) for name in ("R1", "R2", "R3", "R4"))
        blob = self.statuses.get("Checkpoint in customer Blob")
        rehydrated = self.statuses.get("Rehydration") == "PASS"

        def state(reply: HostedReply | None) -> str:
            if reply is None or reply.result is None:
                return "not measured"
            data = reply.result
            same = bool(r1 and same_sandbox(r1, reply))
            return (
                f"variable {presence(data.get('variable'))}, marker file {presence(data.get('marker'))}, "
                f"{'same' if same else 'new'} container"
            )

        results = [
            f"R1 built state from {self.dataset.filename} with exact aggregates in container "
            f"{r1.container if r1 else '-'}.",
            f"R2 (same conversation): {state(r2)}; conversation recall: "
            + ("the agent recalled the nonce." if recall2 == self.nonce else f"not recalled ({recall2})."),
            f"R3 (new conversation): {state(r3)}; conversation recall: "
            + ("none, as expected." if recall3 in (None, "unknown") else f"leaked ({recall3})."),
            "Hosted agent session ID was "
            + ("the same" if r1 and r2 and r1.agent_session_id == r2.agent_session_id else "different")
            + " for R1 and R2.",
            (
                f"Recreation: deleting R1's container returned {nested(recreation, 'delete')}; the container and its "
                f"files then returned {nested(recreation, 'read_after')} / {nested(recreation, 'files_after')}."
                if self.destructive
                else "Recreation: original container retained; destructive lifecycle probe not run."
            ),
            f"Checkpoint: local diagnostic sealed JSON; required customer Blob result: {blob or 'not run'}.",
            "Rehydration: "
            + (
                f"a new sandbox ({r4.container if r4 else '-'}) rebuilt exactly the checkpointed state from the "
                "re-mounted input."
                if rehydrated
                else "not completed."
            ),
            f"From the sandbox, customer storage DNS: {outcome(storage, 'dns')}, HTTPS: {outcome(storage, 'https')}.",
        ]
        measured = [
            (name, reply) for name, reply in (("R2", r2), ("R3", r3)) if reply is not None and reply.result is not None
        ]
        if r2 is not None and r2.result is not None and r3 is not None and r3.result is not None:
            isolated = recall3 in (None, "unknown")
            history: Outcome = "PASS" if recall2 == self.nonce and isolated else "FAIL"
            history_text = (
                "The agent recalled R1's nonce in R2" if recall2 == self.nonce else "R2 did not recall R1's nonce"
            ) + ("; R3 in a new conversation could not see it" if isolated else f"; R3 leaked it ({recall3})")
        else:
            history = checks_outcome(
                self.statuses.get("R2 same conversation"), self.statuses.get("R3 new conversation")
            )
            history_text = "Not measured; see 'R2 same conversation' and 'R3 new conversation' in the detailed evidence"
        kept = [
            name
            for name, reply in measured
            if "present"
            in (presence((reply.result or {}).get("variable")), presence((reply.result or {}).get("marker")))
        ]
        recreation_status = self.statuses.get("Sandbox recreation")
        findings = [
            Finding(
                "Conversation history continues within a conversation and is isolated across conversations",
                history_text,
                "Tested",
                "Foundry-managed",
                ("hosted_conversations",),
                outcome=history,
            ),
            Finding(
                "Sandbox variables and files between requests",
                (
                    f"Kept in: {', '.join(kept)}"
                    if kept
                    else "Lost on every request: each request ran in a new container, even in the same conversation"
                )
                if measured
                else "Not measured",
                "Tested",
                "Foundry-managed",
                ("ci_sessions",),
                outcome=("PASS" if kept else "LIMITATION") if measured else "NOT RUN",
            ),
            Finding(
                "Automatic restore of sandbox memory or files after the container ends",
                "Not available: after R1's container was deleted, the container and its files were gone"
                if recreation_status == "PASS"
                else "Not measured; see 'Sandbox recreation' in the detailed evidence",
                "Tested",
                "Not supported",
                ("responses_containers", "ci_lifetime"),
                outcome="NOT SUPPORTED" if recreation_status == "PASS" else checks_outcome(recreation_status),
            ),
            Finding(
                "Rehydration from a small, sealed checkpoint and the re-mounted input",
                "A new sandbox rebuilt the checkpointed state exactly"
                if rehydrated
                else "Not completed; see 'Rehydration' in the detailed evidence",
                "Tested",
                "Custom implementation (Dev Team)",
                ("state_store",),
                outcome=checks_outcome(self.statuses.get("Rehydration")),
            ),
            Finding(
                "Place the Code Interpreter sandbox in the customer's network",
                "The sandbox doesn't inherit the agent's network and has no outbound access; it could not reach "
                f"customer storage (DNS {outcome(storage, 'dns')}, HTTPS {outcome(storage, 'https')})",
                "Tested",
                "Not supported",
                ("ci_network", "toolbox_network"),
                outcome="NOT SUPPORTED",
            ),
            Finding(
                "Keep uploaded files and conversation history in customer-owned resources",
                "Possible with the Standard agent setup (bring-your-own Storage and Cosmos DB). This POC uses the "
                "Basic setup, so it was not tested",
                "Documented",
                "Foundry-managed (customer-owned resources)",
                ("standard_files", "standard_threads"),
                outcome="DOCUMENTED",
            ),
            Finding(
                "Keep hosted workflow state in customer-owned storage",
                "The hosted agent state store is provided and managed by Foundry; it can't be placed in your own "
                "storage",
                "Documented",
                "Not supported",
                ("state_store",),
                outcome="NOT SUPPORTED",
            ),
            Finding(
                "Keep custom checkpoints in customer storage",
                "Written, read back, "
                + ("used for rehydration" if rehydrated else "but reconstruction was not completed")
                + (", and deleted" if self.statuses.get("Blob checkpoint cleanup") == "PASS" else ", and retained")
                + " (authenticated public Blob; private endpoints were not tested)"
                if blob == "PASS"
                else (
                    "Not run (--blob-checkpoint skip)"
                    if blob is None
                    else "See 'Checkpoint in customer Blob' in the detailed evidence"
                ),
                "Tested" if blob == "PASS" else "Architectural assessment",
                "Custom implementation (Dev Team)",
                ("blob_rbac", "nsp", "storage_private_endpoints"),
                outcome=checks_outcome(blob),
            ),
        ]
        gaps = [
            "Rehydration replays the transform on the re-mounted input; it is not restored memory, and each "
            "execution must stay within the documented 220 s maximum.",
            "Bring-your-own storage for files and conversations (Standard agent setup) was not tested; this POC uses "
            "the Basic setup.",
            "The POC storage account uses a network security perimeter, not a private endpoint, so private residency "
            "is an architectural assessment.",
            "Process-loss recovery and artifact rehydration boundaries are covered separately in GitHub issue #7.",
            "Hosted session continuity and destructive lifecycle observations are separate best-effort probes. "
            "A failure or unsupported contract is a blocker to report, not approval for automatic redesign.",
        ]
        conclusion = (
            "Continuity and rehydration have tested results, and every state layer has a scoped placement assessment."
            + ("" if self.verdict == "Met" else " The customer Blob checkpoint was not proven in this run.")
            if self.verdict.startswith("Met")
            else "See the checks below."
        )
        self.report.summary(render_summary(self.verdict, conclusion, results, findings, gaps))
