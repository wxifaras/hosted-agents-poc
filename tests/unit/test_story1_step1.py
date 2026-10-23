"""Story 1 Step 1: managed Code Interpreter on the existing Data Analysis agent, without Azure calls."""

import csv
import io
import json
import re
import subprocess
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from agent_framework import Content, Message
from app.core.settings import AGENT_NAMES, Settings
from app.runtime import data_analysis_tools
from app.services.code_interpreter import container_ids, parse_artifacts, with_artifacts
from azure.identity import DefaultAzureCredential
from pydantic import ValidationError

from scripts.common.reporting import MarkdownReport, ValidationRun, markdown_cell, update_index
from scripts.common.synthetic_data import COLUMNS, build_dataset, expected_aggregates, generate_rows
from scripts.run_scenario import story1_data_analysis as story

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = (
    "https://a.services.ai.azure.com/api/projects/p/toolboxes/story1-data-analysis-ci/versions/1/mcp?api-version=v1"
)
CONTAINER = "cntr_6ada778c5b808190bc5978e8903a3d40"


def agent_record(
    name: str, version: str = "1", principal: str = "p-1", toolbox: str | None = None
) -> dict[str, object]:
    environment = {"POC_AGENT_NAME": "x", **({"TOOLBOX_ENDPOINT": toolbox} if toolbox else {})}
    return {
        "name": name,
        "versions": {
            "latest": {"version": version, "status": "active", "definition": {"environment_variables": environment}}
        },
        "instance_identity": {"principal_id": principal},
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                ]
            }
        },
    }


def test_synthetic_data_is_deterministic_prefix_stable_and_pinned() -> None:
    small, large = build_dataset(1_000), build_dataset(10_000)
    assert small.content == build_dataset(1_000).content
    assert large.content.startswith(small.content)
    # The sandbox-computed hash is compared with this exact value; any drift breaks Step 1 proof.
    assert small.sha256 == "243639654992badc63e3f881ce9665752245af6263baaf3f6f4b3d3b979f27d3"
    rows = list(csv.DictReader(io.StringIO(small.content.decode())))
    assert tuple(rows[0]) == COLUMNS and len(rows) == 1_000
    assert any(row["units"] == "" and row["revenue_cents"] == "" for row in rows)


def test_expected_aggregates_match_an_independent_csv_calculation() -> None:
    rows = list(csv.DictReader(io.StringIO(build_dataset(1_000).content.decode())))
    independent: dict[str, list[int]] = {}
    for row in rows:
        if row["status"] == "completed" and row["region"] == "West":
            totals = independent.setdefault(row["category"], [0, 0, 0])
            totals[0] += 1
            totals[1] += int(row["units"] or 0)
            totals[2] += int(row["revenue_cents"] or 0)
    expected = expected_aggregates(generate_rows(1_000))
    assert {k: [v.rows, v.units, v.revenue_cents] for k, v in expected.items()} == independent


def test_toolbox_endpoint_setting_is_optional_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().toolbox_endpoint is None
    monkeypatch.setenv("TOOLBOX_ENDPOINT", "")
    assert Settings().toolbox_endpoint is None
    monkeypatch.setenv("TOOLBOX_ENDPOINT", ENDPOINT)
    assert Settings().toolbox_endpoint == ENDPOINT
    monkeypatch.setenv("TOOLBOX_ENDPOINT", "http://example.com/mcp")
    with pytest.raises(ValidationError):
        Settings()


def test_only_configured_data_analysis_gets_the_toolbox() -> None:
    credential = DefaultAzureCredential()
    configured = Settings(toolbox_endpoint=ENDPOINT)
    for name in AGENT_NAMES:
        tools = data_analysis_tools(name, configured, credential)
        assert len(tools) == (1 if name == "data_analysis" else 0)
    assert data_analysis_tools("data_analysis", Settings(), credential) == []
    toolbox = data_analysis_tools("data_analysis", configured, credential)[0]
    assert toolbox.url == ENDPOINT
    assert toolbox.tool_result_content == "both"


def test_container_references_come_only_from_tool_results() -> None:
    messages = [
        Message("assistant", [Content.from_text(f"I invented {CONTAINER}")]),
        Message(
            "tool",
            [Content.from_function_result("c1", result=json.dumps({"container_id": CONTAINER}))],
        ),
        Message("tool", [Content.from_function_result("c2", result=f"again {CONTAINER}")]),
    ]
    assert container_ids(messages) == [CONTAINER]
    assert container_ids([Message("assistant", [Content.from_text(CONTAINER)])]) == []


def test_artifact_block_round_trip_and_smoke_text_unchanged() -> None:
    assert with_artifacts("HOSTED-AGENTS-POC-OK", []) == "HOSTED-AGENTS-POC-OK"
    text = with_artifacts("answer", [CONTAINER])
    assert parse_artifacts(text) == [CONTAINER]
    assert parse_artifacts("no block") == []
    assert parse_artifacts('```poc-artifacts-v1\n{"containers": ["../etc"]}\n```') == []


def test_snapshot_and_reuse_guard() -> None:
    before = {name: story.snapshot_agent(agent_record(name)) for name in story.SERVICES.values()}
    assert all(snapshot.latest_routing and snapshot.status == "active" for snapshot in before.values())
    updated = dict(before)
    updated[story.DATA_ANALYSIS] = story.snapshot_agent(agent_record(story.DATA_ANALYSIS, "2", toolbox=ENDPOINT))
    assert story.reuse_differences(before, updated, version_change=story.DATA_ANALYSIS) == []
    assert story.reuse_differences(before, updated, version_change=None)
    moved = dict(updated)
    moved["supervisor-agent"] = story.snapshot_agent(agent_record("supervisor-agent", "2"))
    assert story.reuse_differences(before, moved, version_change=story.DATA_ANALYSIS) == [
        "supervisor-agent changed version or configuration unexpectedly"
    ]
    new_identity = dict(updated)
    new_identity[story.DATA_ANALYSIS] = story.snapshot_agent(agent_record(story.DATA_ANALYSIS, "2", "p-2", ENDPOINT))
    assert "data-analysis-agent instance identity changed" in story.reuse_differences(
        before, new_identity, version_change=story.DATA_ANALYSIS
    )


def test_missing_agent_blocks_instead_of_creating() -> None:
    from azure.core.exceptions import ResourceNotFoundError

    def read(name: str) -> dict[str, object]:
        if name == "research-agent":
            raise ResourceNotFoundError("missing")
        return agent_record(name)

    with pytest.raises(story.Blocked, match="never creates agents"):
        story.snapshot_agents(read)  # type: ignore[arg-type]


def test_toolbox_reuse_requires_matching_checksums() -> None:
    datasets = [build_dataset(1_000), build_dataset(10_000)]
    file_ids = {"1000": "assistant-a", "10000": "assistant-b"}
    good = {"version": "2", "metadata": story.dataset_metadata(datasets, file_ids)}
    stale = {"version": "3", "metadata": {**story.dataset_metadata(datasets, file_ids), "sha256_1000": "0" * 64}}
    match = story.matching_toolbox([good, stale], datasets)
    assert match == {"version": "2", **file_ids}
    assert story.matching_toolbox([stale], datasets) is None
    assert story.toolbox_endpoint("https://x/api/projects/p/", "tb", "2") == (
        "https://x/api/projects/p/toolboxes/tb/versions/2/mcp?api-version=v1"
    )


def test_mounted_hash_parsing_and_matching() -> None:
    dataset = build_dataset(1_000)
    printed = json.dumps({"python": "3.11", "files": {f"assistant-x-{dataset.filename}": dataset.sha256}})
    python, hashes = story.mounted_hashes(f"content_type='execution_output' text='{printed}\\n'")
    assert python == "3.11"
    assert story.hash_matches([dataset], hashes) == {dataset.filename: True}
    assert story.hash_matches([dataset], {f"assistant-x-{dataset.filename}": "0" * 64}) == {dataset.filename: False}


def test_mcp_result_accepts_json_and_sse_and_surfaces_errors() -> None:
    request = httpx.Request("POST", "https://x")
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "42"}]}}
    as_json = httpx.Response(200, json=body, request=request)
    as_sse = httpx.Response(
        200,
        text=f"event: message\ndata: {json.dumps(body)}\n\n",
        headers={"content-type": "text/event-stream"},
        request=request,
    )
    assert story.result_text(story.mcp_result(as_json)) == "42"
    assert story.result_text(story.mcp_result(as_sse)) == "42"
    with pytest.raises(ValueError, match="Toolbox MCP error"):
        story.mcp_result(httpx.Response(200, json={"error": {"code": -1}}, request=request))


def test_verify_mode_never_deploys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("verify mode must not run azd")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    args = Namespace(environment="env", mode="verify", timeout=10, output_dir=tmp_path)
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    step = story.Step1(args, run, MarkdownReport(tmp_path / "r.md", "t"))
    current = story.snapshot_agent(agent_record(story.DATA_ANALYSIS))
    record = story.ToolboxRecord(story.TOOLBOX_NAME, "1", ENDPOINT, {})
    with pytest.raises(story.Blocked, match="enable-if-missing"):
        step.enable(current, record)
    already = story.snapshot_agent(agent_record(story.DATA_ANALYSIS, "2", toolbox=ENDPOINT))
    step.enable(already, record)
    assert step.deployed is False


def test_markdown_report_is_sanitized_and_table_safe(tmp_path: Path) -> None:
    assert markdown_cell("a|b\nc") == "a\\|b<br>c"
    assert markdown_cell({"token": "secret"}) == '{"token": "[REDACTED]"}'
    report = MarkdownReport(tmp_path / "x" / "r.md", "Title")
    report.heading("Section")
    report.table(["A", "B"], [["1", "Bearer abc.def"]])
    text = report.path.read_text(encoding="utf-8")
    assert text.startswith("# Title") and "| A | B |" in text and "abc.def" not in text
    update_index(tmp_path / "index.md", "first")
    update_index(tmp_path / "index.md", "second")
    assert (tmp_path / "index.md").read_text(encoding="utf-8").count("- ") == 2


@pytest.mark.parametrize("mode", ["verify", "enable-if-missing"])
def test_story1_run_directories_are_descriptive_and_index_links_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    def blocked(_step: story.Step1) -> None:
        raise story.Blocked("Offline test prerequisite")

    monkeypatch.setattr(story.Step1, "execute", blocked)
    arguments = ["--step", "1", "--mode", mode, "--output-dir", str(tmp_path), "--no-color"]
    assert story.main(arguments) == 2
    assert story.main(arguments) == 2
    output = tmp_path / "story-1"
    directories = [path for path in output.iterdir() if path.is_dir()]
    assert len(directories) == 2
    index = (output / "index.md").read_text(encoding="utf-8")
    run_ids = set()
    for directory in directories:
        assert re.fullmatch(
            rf"\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}Z-step-1-code-interpreter-{mode}-[0-9a-f]{{8}}",
            directory.name,
        )
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        started_at = datetime.fromisoformat(manifest["started_at"])
        assert directory.name.startswith(f"{started_at:%Y-%m-%d_%H-%M-%SZ}-")
        assert directory.name.endswith(manifest["run_id"][:8])
        run_ids.add(manifest["run_id"])
        assert (directory / story.REPORT_NAME).is_file()
        assert f"]({directory.name}/{story.REPORT_NAME})" in index
        for filename in ("events.jsonl", "results.jsonl"):
            event = json.loads((directory / filename).read_text(encoding="utf-8"))
            assert event["run_id"] == manifest["run_id"]
    assert len(run_ids) == 2


def test_story1_task_reuses_agents_and_has_no_hidden_dependencies() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {task["label"]: task for task in document["tasks"]}
    task = tasks[story.TASK_LABEL]
    assert task["command"] == "uv" and "dependsOn" not in task
    assert task["args"][-5:] == [
        "1",
        "--environment",
        "${input:infraEnvironment}",
        "--mode",
        "${input:story1Step1Mode}",
    ]
    mode = next(item for item in document["inputs"] if item["id"] == "story1Step1Mode")
    assert mode["default"] == "verify" and mode["options"] == ["verify", "enable-if-missing"]
    service = (ROOT / "azure.yaml").read_text(encoding="utf-8")
    assert service.count("TOOLBOX_ENDPOINT: ${DATA_ANALYSIS_TOOLBOX_ENDPOINT}") == 1
