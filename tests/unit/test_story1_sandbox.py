"""Story 1 Steps 4 and 5: sandbox boundaries and state continuity, without Azure calls."""

import base64
import json
import re
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from azure.core.exceptions import ResourceNotFoundError

from scripts.common.reporting import REPORT_FILE, MarkdownReport, ValidationRun
from scripts.common.synthetic_data import build_dataset, expected_aggregates, generate_rows
from scripts.run_scenario import story1_data_analysis as story
from scripts.run_scenario import story1_sandbox as sb

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "https://a.services.ai.azure.com/api/projects/p"
TEN = build_dataset(10_000)
EXPECTED = expected_aggregates(generate_rows(10_000))
GROUPS = {name: {"rows": t.rows, "units": t.units, "revenue_cents": t.revenue_cents} for name, t in EXPECTED.items()}
TOOLBOX = story.ToolboxRecord(story.TOOLBOX_NAME, "1", f"{PROJECT}/toolboxes/x/versions/1/mcp", {"10000": "file-10k"})


def emitted(value: dict[str, Any]) -> str:
    encoded = base64.b64encode(json.dumps(value).encode()).decode()
    return f"content_type='execution_output' text='{sb.RESULT_MARKER}{encoded}\\n'"


def args(tmp_path: Path, **extra: object) -> Namespace:
    values: dict[str, object] = {
        "environment": "env",
        "mode": "verify",
        "timeout": 300,
        "output_dir": tmp_path,
        "options_toolbox": "probe",
        "idle_wait": "skip",
        "blob_checkpoint": "write",
    }
    return Namespace(**{**values, **extra})


def step(cls: type[sb.SandboxStep], tmp_path: Path) -> Any:
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    instance = cls(args(tmp_path), run, MarkdownReport(run.path / REPORT_FILE, "t"))
    instance.project_endpoint = PROJECT
    instance.toolbox = TOOLBOX
    return instance


@pytest.mark.parametrize(
    ("body", "values"),
    [
        (
            sb.BOUNDARY_CODE,
            {"__TARGETS__": repr(json.dumps({"public_host": "h", "public_ip": None, "storage_host": "s"}))},
        ),
        (sb.LIMITS_CODE, {"__ALLOWED__": "a", "__OTHER__": "b"}),
        (sb.REUSE_CHECK_CODE, {}),
        (sb.STATE_WRITE_CODE, {"__FILE__": TEN.filename, "__NONCE__": "n1", "__STORAGE_HOST__": "s"}),
        (sb.STATE_CHECK_CODE, {"__PHASE__": "r2"}),
        (sb.REHYDRATE_CODE, {"__CHECKPOINT__": repr("{}")}),
    ],
)
def test_sandbox_programs_are_complete_and_compile(body: str, values: dict[str, str]) -> None:
    code = sb.sandbox_program(body, {**values, "__RESULT__": "r.json"})
    assert not re.search(r"__[A-Z]+__", code)
    compile(code, "sandbox", "exec")
    prompt = sb.run_prompt(code, "Reply.")
    assert "ONE execution" in prompt and "Do not modify, split, or retry" in prompt


def test_emitted_result_survives_tool_output_escaping() -> None:
    assert sb.emitted_result(emitted({"a": "it's", "b": [1, 2]})) == {"a": "it's", "b": [1, 2]}
    assert sb.emitted_result("no marker") is None
    assert sb.emitted_result(f"{sb.RESULT_MARKER}!!!") is None
    assert sb.emitted_result(sb.RESULT_MARKER + base64.b64encode(b"[1]").decode()) is None


def test_toolbox_client_sends_options_in_meta_and_parses_results() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["params"].get("_meta", {}).get("memory_limit") == "64g":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no"}})
        result = {
            "content": [{"type": "text", "text": emitted({"x": 1})}],
            "structuredContent": {"container_id": "cntr_a"},
        }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    client = sb.ToolboxClient(httpx.Client(transport=httpx.MockTransport(handler)), "https://t/mcp", "t")
    call = client.call("print(1)", {"memory_limit": "4g"})
    assert (call.container, call.result, call.error) == ("cntr_a", {"x": 1}, None)
    assert sent[0]["params"] == {
        "name": "code_interpreter",
        "arguments": {"code": "print(1)"},
        "_meta": {"memory_limit": "4g"},
    }
    assert "_meta" not in client.call("print(1)").__dict__ and "_meta" not in sent[1]["params"]
    failed = client.call("print(1)", {"memory_limit": "64g"})
    assert failed.result is None and failed.error and "Toolbox MCP error" in failed.error


def test_platform_rejections_are_recorded_as_evidence() -> None:
    message = "UserError[400, user=Network policy 'allowlist' is not permitted because the organization policy ...]"

    def handler(request: httpx.Request) -> httpx.Response:
        result = {"content": [{"type": "text", "text": message}], "isError": True}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    call = sb.ToolboxClient(httpx.Client(transport=httpx.MockTransport(handler)), "https://t/mcp", "t").call("x")
    assert call.is_error and call.result is None and call.error and "organization policy" in call.error


def test_boundary_helpers() -> None:
    probe = {
        "identity": {"user": "sandbox", "uid": 1001},
        "limits": {"cgroup_memory_max": "2147483648"},
        "network": {"dns_public": {"outcome": "dns_failure"}, "https_public": {"outcome": "reachable", "status": 200}},
    }
    view = sb.boundary_view(probe)
    assert view["identity.user"] == "sandbox" and view["network.dns_public.outcome"] == "dns_failure"
    assert sb.network_open(probe) == ["https_public"]
    assert sb.network_open(None) == []
    assert sb.memory_applied("4294967296", "4g") == "applied"
    assert sb.memory_applied("2147483648", "4g") == "ignored (enforced 2.0 GiB)"
    assert sb.memory_applied(None, "4g") == "unknown"
    assert sb.network_names(probe) == ["dns_public", "https_public"]
    assert sb.nonce_line("NONCE: `abc123`\nok") == "abc123" and sb.nonce_line("nothing") is None


def test_checkpoint_seal_and_validation() -> None:
    result = {"filtered_rows": 1249, "by_category": GROUPS}
    checkpoint = sb.build_checkpoint("0123456789abcdef", TEN, "file-10k", result, {"container": "cntr_a"})
    sealed = sb.seal_checkpoint(checkpoint)
    body, problems = sb.open_checkpoint(sealed, TEN, "file-10k")
    assert problems == [] and body is not None and body["id"] == "story1-0123456789ab"
    assert b"2026-" not in sealed.replace(checkpoint["created_at"].encode(), b"")  # no data rows
    assert sb.open_checkpoint(None, TEN, "file-10k") == (None, ["missing"])
    assert sb.open_checkpoint(b"{", TEN, "file-10k") == (None, ["corrupt: not JSON"])
    tampered = sealed.replace(b'"filtered_rows": 1249', b'"filtered_rows": 1250')
    assert sb.open_checkpoint(tampered, TEN, "file-10k") == (None, ["corrupt: checksum mismatch"])
    assert sb.open_checkpoint(sealed, build_dataset(1_000), "file-10k")[1] == [
        "stale: dataset checksum differs from the mounted input"
    ]
    assert sb.open_checkpoint(sealed, TEN, "file-other")[1] == ["stale: toolbox file ID differs"]
    old = sb.seal_checkpoint({**checkpoint, "schema": "story1-checkpoint-v0"})
    assert sb.open_checkpoint(old, TEN, "file-10k")[1] == ["schema 'story1-checkpoint-v0' is not story1-checkpoint-v1"]


def test_fingerprint_comparison_and_blob_status() -> None:
    def reply(container: str, host: str) -> sb.HostedReply:
        return sb.HostedReply(container=container, result={"fingerprint": {"hostname_sha256": host}})

    assert sb.same_sandbox(reply("c1", "h1"), reply("c1", "h1")) is True
    assert sb.same_sandbox(reply("c1", "h1"), reply("c2", "h2")) is False
    assert sb.same_sandbox(reply("c1", "h1"), sb.HostedReply()) is None
    request = httpx.Request("PUT", "https://s")
    assert sb.blob_status(httpx.Response(201, request=request)) == ("PASS", "HTTP 201")
    status, detail = sb.blob_status(
        httpx.Response(403, headers={"x-ms-error-code": "AuthorizationPermissionMismatch"}, request=request)
    )
    assert status == "BLOCKED" and "Access: Apply developer permissions" in detail
    assert sb.blob_status(httpx.Response(500, request=request))[0] == "FAIL"


def test_verdicts() -> None:
    assert sb.step4_verdict({}) == "Not evaluated"
    assert sb.step4_verdict({"Direct probe A": "PASS", "Idle expiry": "PASS"}) == "Met"
    assert sb.step4_verdict({"Direct probe A": "PASS", "Outbound network blocked": "FAIL"}) == "Partially met"
    core = dict.fromkeys(sb.CORE_CHECKS, "PASS")
    assert sb.step5_verdict({}) == "Not evaluated"
    assert sb.step5_verdict({**core, "Checkpoint in customer Blob": "PASS"}) == "Met"
    assert sb.step5_verdict({**core, "Checkpoint in customer Blob": "BLOCKED"}) == "Met, with a validation gap"
    assert sb.step5_verdict({**core, "Rehydration": "FAIL"}) == "Partially met"
    assert sb.step5_verdict({**core, "Agents reused - after": "FAIL", "Rehydration": "FAIL"}) == "Not met"


class FakeToolboxes:
    def __init__(self, fail_on: int | None = None) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.fail_on = fail_on

    def create_version(self, name: str, **kwargs: Any) -> Any:
        if self.fail_on is not None and len(self.created) == self.fail_on:
            raise RuntimeError("create failed")
        self.created.append(name)
        tool = kwargs["tools"][0].as_dict()
        return SimpleNamespace(as_dict=lambda: {"version": str(len(self.created)), "tools": [tool]})

    def delete(self, name: str) -> None:
        self.deleted.append(name)

    def get(self, name: str) -> None:
        raise ResourceNotFoundError("gone")


def test_probe_toolbox_is_always_deleted_and_never_the_step1_toolbox(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mcp"):
            result = {
                "content": [{"type": "text", "text": emitted({"cgroup_memory_max": "2147483648"})}],
                "structuredContent": {"container_id": "cntr_o"},
            }
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
        return httpx.Response(200, json={"memory_limit": "2g"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    s4 = step(sb.Step4, tmp_path)
    toolboxes = FakeToolboxes()
    s4.options_toolbox(SimpleNamespace(toolboxes=toolboxes), http, "t")
    assert len(toolboxes.created) == 4 and set(toolboxes.created) == set(toolboxes.deleted)
    assert story.TOOLBOX_NAME not in toolboxes.created
    assert s4.statuses["Probe-only toolbox deleted"] == "PASS"
    assert "ignored (enforced" not in json.dumps(s4.obs["options_toolbox"])
    assert "memory 2.0 GiB" in s4.obs["options_toolbox"]["memory_limit 4g"]

    failing = FakeToolboxes(fail_on=1)
    s4b = step(sb.Step4, tmp_path / "b")
    with pytest.raises(RuntimeError):
        s4b.options_toolbox(SimpleNamespace(toolboxes=failing), http, "t")
    assert failing.deleted == failing.created[:1]
    assert s4b.statuses["Probe-only toolbox deleted"] == "PASS"


def test_step5_end_to_end_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s5 = step(sb.Step5, tmp_path)
    nonce = s5.nonce
    replies = {
        "R1": {
            "phase": "write",
            "nonce": nonce,
            "input_sha256": TEN.sha256,
            "filtered_rows": 1249,
            "by_category": GROUPS,
            "fingerprint": {"hostname_sha256": "h1"},
            "customer_storage": {"dns": {"outcome": "dns_failure"}, "https": {"outcome": "dns_failure"}},
        },
        "R2": {
            "phase": "r2",
            "variable": None,
            "marker": None,
            "fingerprint": {"hostname_sha256": "h2"},
            "inputs": ["a", "b", "c"],
        },
        "R3": {
            "phase": "r3",
            "variable": None,
            "marker": None,
            "fingerprint": {"hostname_sha256": "h3"},
            "inputs": ["a", "b", "c"],
        },
        "R4": {
            "phase": "rehydrate",
            "input_sha256_matches": True,
            "matches_checkpoint": True,
            "filtered_rows": 1249,
            "by_category": GROUPS,
            "fingerprint": {"hostname_sha256": "h4"},
        },
    }
    texts = {"R2": f"NONCE: {nonce}\nState was not found.", "R3": "NONCE: unknown\nState was not found."}
    prompts: dict[str, str] = {}

    def hosted(
        self: sb.SandboxStep,
        project: Any,
        token: Any,
        label: str,
        prompt: str,
        result: str,
        conversation: str | None = None,
    ) -> sb.HostedReply:
        name = label.split(" ")[0]
        prompts[name] = prompt
        return sb.HostedReply(
            status="PASS",
            text=texts.get(name, "done"),
            conversation=conversation,
            container=f"cntr_{name}",
            containers=[f"cntr_{name}"],
            agent_session_id="s1",
            result=replies[name],
        )

    conversations = iter(["conv_1", "conv_2", "conv_3"])
    monkeypatch.setattr(sb.SandboxStep, "hosted", hosted)
    monkeypatch.setattr(sb.SandboxStep, "new_conversation", lambda self, project: next(conversations))
    blobs: dict[str, bytes] = {}
    deleted_containers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "s.blob":
            if request.method == "PUT":
                blobs[path] = request.content
                return httpx.Response(201)
            if request.method == "DELETE":
                blobs.pop(path, None)
                return httpx.Response(202)
            return httpx.Response(200, content=blobs[path]) if path in blobs else httpx.Response(404)
        if request.method == "DELETE":
            deleted_containers.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"deleted": True})
        gone = any(path.endswith(c) or path.endswith(c + "/files") for c in deleted_containers)
        return httpx.Response(404 if gone else 200, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    blob = sb.BlobStore(http, "https://s.blob/", "artifacts", "t")
    s5.continuity(None, lambda: "t", "s.blob")
    s5.save_checkpoint(blob)
    s5.recreate(http, "t")
    s5.rehydrate(None, lambda: "t", blob)
    s5.cleanup_blob(blob)
    s5.statuses["Agents reused - after"] = "PASS"
    s5.conclude()

    assert nonce in prompts["R1"] and nonce not in prompts["R2"] and nonce not in prompts["R3"]
    assert json.dumps(GROUPS)[:40] in prompts["R4"].replace("\\", "")  # checkpoint, not data rows, reaches R4
    assert deleted_containers == ["cntr_R1"] and blobs == {}
    assert all(s5.statuses[name] == "PASS" for name in sb.CORE_CHECKS)
    assert s5.statuses["Checkpoint in customer Blob"] == "PASS" and s5.statuses["Blob checkpoint cleanup"] == "PASS"
    assert s5.verdict == "Met"
    report = s5.report.path.read_text(encoding="utf-8")
    assert "| Conversation history | recalled | isolated |" in report
    assert "| `story1_state` variable | lost | lost | rebuilt from checkpoint |" in report
    assert "Customer-network placement" in report and "Tested (Blob)" in report
    assert report.startswith("# t\n\n## How to read this report") and "**Acceptance verdict: Met.**" in report
    assert report.index("## How to read this report") < report.index("## Summary")
    for row in (line for line in report.splitlines() if "| Not supported |" in line):
        assert "](https://learn.microsoft.com/" in row, row
    assert "| ✅ PASS | Conversation history continues within a conversation" in report
    assert "| ⚠️ LIMITATION | Sandbox variables and files between requests | Lost on every request" in report
    assert "| 🚫 NOT SUPPORTED | Automatic restore of sandbox memory or files" in report
    assert "| ✅ PASS | Rehydration from a small, sealed checkpoint" in report
    assert "| ✅ PASS | Keep custom checkpoints in customer storage | Written, read back" in report
    assert (s5.run.path / "checkpoint.json").is_file()
    # Outcomes follow the measurements: a missed recall and a leak across conversations fail the history finding.
    s5.write_summary("not-the-nonce", s5.nonce, {}, {})
    again = s5.report.path.read_text(encoding="utf-8").rsplit("## Summary", 1)[-1]
    assert "| ❌ FAIL | Conversation history continues within a conversation" in again
    assert "R2 did not recall R1's nonce; R3 leaked it" in again


def test_step5_rehydration_refuses_a_tampered_checkpoint(tmp_path: Path) -> None:
    s5 = step(sb.Step5, tmp_path)
    s5.checkpoint = sb.build_checkpoint(s5.run.run_id, TEN, "file-10k", {"filtered_rows": 1, "by_category": GROUPS}, {})
    (s5.run.path / "checkpoint.json").write_bytes(
        sb.seal_checkpoint(s5.checkpoint).replace(b'"rows": 10000', b'"rows": 9')
    )
    s5.rehydrate(None, lambda: "t", None)
    assert s5.statuses["Rehydration"] == "FAIL"


@pytest.mark.parametrize(("number", "name"), [(4, "step-04-sandbox-boundaries"), (5, "step-05-state-continuity")])
def test_blocked_runs_write_reports_and_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: int, name: str
) -> None:
    cls = sb.Step4 if number == 4 else sb.Step5

    def blocked(_self: object) -> None:
        raise story.Blocked("Reuse baseline agents-baseline.json is missing. Run Task 1 (Step 1) first.")

    monkeypatch.setattr(cls, "execute", blocked)
    assert story.main(["--step", str(number), "--output-dir", str(tmp_path), "--no-color"]) == 2
    (directory,) = [path for path in (tmp_path / "story-1" / name).iterdir() if path.is_dir()]
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", directory.name)
    report = (directory / REPORT_FILE).read_text(encoding="utf-8")
    assert "Run Task 1" in report and "Acceptance verdict: Not evaluated" in report
    index = (tmp_path / "story-1" / "index.md").read_text(encoding="utf-8")
    link = f"{name}/{directory.name}"
    assert f"{cls.spec.label()} - **Not evaluated** - [{link}]({link}/{REPORT_FILE})" in index


def test_steps_4_and_5_never_change_agents_or_the_step1_toolbox() -> None:
    source = Path(sb.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "Popen",
        "subprocess",
        "azd",
        "/openai/v1/files",
        "agents.create",
        "agents.update",
        "delete(TOOLBOX_NAME",
    ):
        assert forbidden not in source, forbidden
    assert source.count("toolboxes.create_version(") == 1 and "story1-sandbox-options-" in source


def test_tasks_and_inputs() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {task["label"]: task for task in document["tasks"]}
    four, five = tasks[sb.STEP4_TASK_LABEL], tasks[sb.STEP5_TASK_LABEL]
    assert four["args"][-8:] == [
        "--step",
        "4",
        "--environment",
        "${input:infraEnvironment}",
        "--options-toolbox",
        "${input:story1OptionsToolbox}",
        "--idle-wait",
        "${input:story1IdleWait}",
    ]
    assert five["args"][-4:] == ["--step", "5", "--environment", "${input:infraEnvironment}"]
    assert "dependsOn" not in four and "dependsOn" not in five
    inputs = {item["id"]: item for item in document["inputs"]}
    assert inputs["story1OptionsToolbox"]["default"] == "probe" and inputs["story1IdleWait"]["default"] == "skip"
