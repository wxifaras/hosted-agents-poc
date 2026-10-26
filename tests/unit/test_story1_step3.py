"""Story 1 Step 3: progressively larger datasets, without Azure calls."""

import csv
import io
import json
import re
import struct
import zlib
from argparse import Namespace
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from scripts.common.reporting import MarkdownReport, ValidationRun
from scripts.common.synthetic_data import (
    COLUMNS,
    DATASET_SIZES,
    Dataset,
    build_dataset,
    expected_aggregates,
    expected_filtered_ids,
    generate_rows,
)
from scripts.run_scenario import story1_data_analysis as story

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "https://a.services.ai.azure.com/api/projects/p"
CONTAINER = "cntr_6adb9c2984d08190bc2c7098eb62cbf409e9166058601c2c"
REQUEST = httpx.Request("POST", "https://x")


def png(width: int = 10, height: int = 6) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return story.PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + header + struct.pack(">I", zlib.crc32(header))


def sandbox_outputs(dataset: Dataset) -> dict[str, bytes]:
    """What the fixed workload writes, built from the local oracle with the csv module."""
    rows = list(generate_rows(dataset.rows))
    expected = expected_aggregates(rows)
    keep = set(expected_filtered_ids(rows))
    source = list(csv.DictReader(io.StringIO(dataset.content.decode())))
    filtered = io.StringIO(newline="")
    writer = csv.DictWriter(filtered, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(row for row in source if int(row["row_id"]) in keep)
    by_category = io.StringIO(newline="")
    table = csv.writer(by_category, lineterminator="\n")
    table.writerow(story.BY_CATEGORY_COLUMNS)
    table.writerows([name, t.rows, t.units, t.revenue_cents] for name, t in expected.items())
    result = {
        "input_file": f"assistant-x-{dataset.filename}",
        "input_bytes": dataset.size_bytes,
        "input_sha256": dataset.sha256,
        "filtered_rows": len(keep),
        "by_category": {name: asdict(totals) for name, totals in expected.items()},
        "seconds": {"hash": 0.01, "import": 1.2, "load": 0.05, "filter_group": 0.02, "plot": 0.3, "total": 1.58},
        "peak_rss_kb": 180_224,
        "cpu_count": 2,
        "mem_total_kb": 4_030_000,
        "cgroup_memory_max": "2147483648",
        "cgroup_cpu_max": "max 100000",
        "disk_free_bytes": 10 * 1024**3,
        "python": "3.11.15",
        "pandas": "2.2.3",
        "outputs": {},
    }
    return {
        story.RESULT_FILE: json.dumps(result).encode(),
        "filtered.csv": filtered.getvalue().encode(),
        "by_category.csv": by_category.getvalue().encode(),
        "revenue_by_category.png": png(),
    }


def files_api(outputs: dict[str, bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer t"
        path = request.url.path
        if path.endswith(f"/containers/{CONTAINER}"):
            return httpx.Response(200, json={"memory_limit": "2g", "expires_after": {"minutes": 20}})
        if path.endswith(f"/containers/{CONTAINER}/files"):
            data = [
                {"id": f"cfile_{i}", "path": f"/mnt/data/{name}", "source": "assistant"}
                for i, name in enumerate(outputs)
            ]
            data.append({"id": "cfile_in", "path": "/mnt/data/input.csv", "source": "user"})
            return httpx.Response(200, json={"data": data})
        match = re.search(r"/files/cfile_(\d+)/content$", path)
        if match:
            return httpx.Response(200, content=list(outputs.values())[int(match.group(1))])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class FakeProject:
    """Records every hosted call; a test fails if anything else is touched."""

    def __init__(self, reply: Callable[[str], object]) -> None:
        self.reply = reply
        self.client_options: list[dict[str, object]] = []
        self.prompts: list[str] = []

    @contextmanager
    def get_openai_client(self, **options: object) -> Iterator[Any]:
        self.client_options.append(options)
        project = self

        class Responses:
            def create(self, *, input: str, stream: bool) -> Any:
                assert stream is False
                project.prompts.append(input)
                outcome = project.reply(input)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        class Client:
            responses = Responses()

        yield Client()


class Reply:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(self, mode: str) -> dict[str, object]:
        return self.payload


def completed(text: str) -> Reply:
    return Reply(
        {
            "id": "caresp_1",
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 1500, "output_tokens": 300, "total_tokens": 1800},
        }
    )


def answer(result: bytes) -> str:
    return f'Done.\n\n```json\n{result.decode()}\n```\n\n```poc-artifacts-v1\n{{"containers": ["{CONTAINER}"]}}\n```'


def make_step(tmp_path: Path, timeout: float = 300) -> story.Step3:
    args = Namespace(environment="env", mode="verify", timeout=timeout, output_dir=tmp_path)
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    step = story.Step3(args, run, MarkdownReport(run.path / story.STEP3_REPORT, "t"))
    step.project_endpoint = PROJECT
    return step


@pytest.fixture
def fake_files(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, bytes]], None]:
    real = httpx.Client

    def install(outputs: dict[str, bytes]) -> None:
        monkeypatch.setattr(story.httpx, "Client", lambda **_: real(transport=files_api(outputs)))

    return install


def test_workload_code_is_fixed_and_only_the_file_name_varies() -> None:
    small, large = (story.capacity_code(build_dataset(n).filename) for n in (1_000, 100_000))
    assert "__" not in small.replace("__version__", "")
    assert small.replace("1000.csv", "100000.csv") == large
    compile(small, "capacity", "exec")
    prompt = story.capacity_prompt(build_dataset(1_000))
    assert "ONE execution" in prompt and "Do not modify, split, or retry" in prompt
    # Data never passes through the model: no CSV rows in the prompt, and prompt length is size-independent.
    assert "2026-" not in prompt
    assert len(story.capacity_prompt(build_dataset(100_000))) - len(prompt) == 2


def test_sizes_are_bounded_to_the_agreed_list(tmp_path: Path) -> None:
    assert [d.rows for d in make_step(tmp_path).datasets] == list(DATASET_SIZES) == [1_000, 10_000, 100_000]
    with pytest.raises(ValueError, match="agreed"):
        story.evaluate_sizes([build_dataset(500)], lambda d: story.SizeResult(d.rows, 0, "PASS"), lambda _: None)
    with pytest.raises(SystemExit):
        story.build_parser().parse_args(["--step", "6"])
    with pytest.raises(SystemExit):
        story.main(["--step", "3", "--mode", "enable-if-missing", "--output-dir", str(tmp_path)])


def test_sizes_run_sequentially_ascending_and_stop_after_platform_failure() -> None:
    datasets = [build_dataset(n) for n in reversed(DATASET_SIZES)]
    calls: list[int] = []
    recorded: list[int] = []

    def attempt(dataset: Dataset) -> story.SizeResult:
        calls.append(dataset.rows)
        if dataset.rows == 10_000:
            return story.SizeResult(dataset.rows, dataset.size_bytes, "FAIL", category="timeout")
        return story.SizeResult(dataset.rows, dataset.size_bytes, "PASS")

    results = story.evaluate_sizes(datasets, attempt, lambda result: recorded.append(result.rows))
    assert calls == [1_000, 10_000]
    assert recorded == [1_000, 10_000, 100_000]
    assert [r.status for r in results] == ["PASS", "FAIL", "SKIP"]
    assert results[2].detail == "SKIP (not run after failure): timeout at 10,000 rows"


def test_correctness_failures_do_not_stop_escalation() -> None:
    def attempt(dataset: Dataset) -> story.SizeResult:
        status = "FAIL" if dataset.rows == 1_000 else "PASS"
        return story.SizeResult(dataset.rows, 0, status, category="incorrect" if status == "FAIL" else None)

    results = story.evaluate_sizes([build_dataset(n) for n in DATASET_SIZES], attempt, lambda _: None)
    assert [r.status for r in results] == ["FAIL", "PASS", "PASS"]
    assert {"incorrect", "no_execution", "output_error"}.isdisjoint(story.STOP_CATEGORIES)


def test_hosted_call_has_bounded_timeout_and_no_retries(tmp_path: Path) -> None:
    step = make_step(tmp_path, timeout=123)
    project = FakeProject(lambda _: openai.APITimeoutError(request=REQUEST))
    result = step.attempt(project, lambda: "t", build_dataset(1_000))  # type: ignore[arg-type]
    assert project.client_options == [{"agent_name": story.DATA_ANALYSIS, "timeout": 123, "max_retries": 0}]
    assert len(project.prompts) == 1
    assert (result.status, result.category) == ("FAIL", "timeout")
    assert result.seconds is not None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APITimeoutError(request=REQUEST), ("timeout", "FAIL")),
        (
            openai.RateLimitError("slow down", response=httpx.Response(429, request=REQUEST), body=None),
            ("quota", "FAIL"),
        ),
        (
            openai.APIStatusError("denied", response=httpx.Response(403, request=REQUEST), body=None),
            ("permission", "BLOCKED"),
        ),
        (
            openai.APIStatusError("boom", response=httpx.Response(500, request=REQUEST), body=None),
            ("service_error", "FAIL"),
        ),
        (
            openai.APIStatusError("bad", response=httpx.Response(400, request=REQUEST), body=None),
            ("request_error", "FAIL"),
        ),
        (openai.APIConnectionError(request=REQUEST), ("connection", "FAIL")),
    ],
)
def test_exceptions_map_to_capacity_categories(error: Exception, expected: tuple[str, str]) -> None:
    assert story.classify_exception(error) == expected


def test_failure_text_classification() -> None:
    assert story.classify_failure("Traceback ... MemoryError", "x") == "resource"
    assert story.classify_failure("process was Killed", "x") == "resource"
    assert story.classify_failure("Error code: 429 - rate limit", "x") == "quota"
    assert story.classify_failure("The request timed out", "x") == "timeout"
    assert story.classify_failure("zoomed chart and bloom filter", "agent_error") == "agent_error"


def test_metric_parsing_and_units() -> None:
    outputs = sandbox_outputs(build_dataset(1_000))
    parsed = story.parse_sandbox_result(outputs[story.RESULT_FILE])
    assert parsed["filtered_rows"] == 113
    with pytest.raises(ValueError, match="filtered_rows"):
        story.parse_sandbox_result(json.dumps({**parsed, "filtered_rows": "113"}).encode())
    with pytest.raises(ValueError, match="JSON object"):
        story.parse_sandbox_result(b"[]")
    assert story.human_bytes(157) == "157 B"
    assert story.human_bytes(54_449) == "53.2 KiB"
    assert story.human_bytes(5_633_261) == "5.4 MiB"
    assert story.human_bytes(None) == "-"
    assert story.kib_text(180_224) == "176.0 MiB"
    assert story.kib_text("max") == "-"
    assert story.seconds_text(1.234) == "1.23" and story.seconds_text(None) == "-"
    assert story.tokens_text({"input_tokens": 1500, "output_tokens": 30}) == "input 1,500, output 30"
    assert story.tokens_text(None) == "not reported"


def test_cgroup_limits_are_rendered_as_enforced_values() -> None:
    assert story.cgroup_memory_text("2147483648") == "2.0 GiB"
    assert story.cgroup_memory_text("max") == "unlimited"
    assert story.cgroup_memory_text("9223372036854771712") == "unlimited"
    assert story.cgroup_memory_text(None) == "-"
    assert story.cgroup_cpu_text("100000 100000") == "1 CPU (100000/100000 us)"
    assert story.cgroup_cpu_text("150000 100000") == "1.5 CPU (150000/100000 us)"
    assert story.cgroup_cpu_text("max 100000") == "unlimited"
    assert story.cgroup_cpu_text("-1 100000") == "unlimited"
    assert story.cgroup_cpu_text("100000") == "quota 100000 (period unknown)"
    measured = story.SizeResult(1_000, 0, "PASS", sandbox={"peak_rss_kb": 165_580, "cgroup_memory_max": "2147483648"})
    assert story.memory_headroom(measured) == "peak RSS 161.7 MiB of 2.0 GiB enforced limit (8%)"
    assert story.memory_headroom(story.SizeResult(1_000, 0, "PASS")) == "not measured"


def test_markdown_code_block_keeps_nested_fences(tmp_path: Path) -> None:
    report = MarkdownReport(tmp_path / "r.md", "t")
    report.code("before\n```python\nprint(1)\n```\nafter", "text")
    report.code("plain", "text")
    text = report.path.read_text(encoding="utf-8")
    assert "\n````text\nbefore\n```python\nprint(1)\n```\nafter\n````\n" in text
    assert "\n```text\nplain\n```\n" in text


@pytest.mark.parametrize("encoding", ["cp1252", "utf-8"])
def test_report_renders_images_on_legacy_and_utf8_consoles(tmp_path: Path, encoding: str) -> None:
    from rich.console import Console

    report = MarkdownReport(tmp_path / "r.md", "Title")
    report.paragraph("![Revenue by category](outputs/1000/revenue_by_category.png)")
    report.table(["A"], [["━ box"]])
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
    report.render(Console(file=stream, width=100, no_color=True))
    stream.flush()
    rendered = stream.buffer.getvalue().decode(encoding)  # type: ignore[attr-defined]
    assert "Revenue by category" in rendered and "Markdown report:" in rendered


def test_output_checks_use_the_independent_oracle() -> None:
    dataset = build_dataset(1_000)
    rows = list(generate_rows(dataset.rows))
    expected, ids = expected_aggregates(rows), expected_filtered_ids(rows)
    outputs = sandbox_outputs(dataset)
    assert story.check_by_category_csv(outputs["by_category.csv"], expected)
    assert all(
        v is True
        for v in story.check_filtered_csv(outputs["filtered.csv"], ids, COLUMNS).values()
        if isinstance(v, bool)
    )
    assert story.png_dimensions(outputs["revenue_by_category.png"]) == (10, 6)
    assert story.png_dimensions(b"GIF89a") is None
    first = next(iter(expected))
    wrong = {name: asdict(t) for name, t in expected.items()}
    wrong[first]["revenue_cents"] += 1
    assert not story.aggregates_match(wrong, expected)
    as_float = {name: {**asdict(t), "units": float(t.units)} for name, t in expected.items()}
    assert not story.aggregates_match(as_float, expected)  # type: ignore[arg-type]
    assert not story.check_filtered_csv(outputs["filtered.csv"], ids[:-1], COLUMNS)["row_ids_match"]


def test_attempt_passes_only_on_sandbox_written_results(
    tmp_path: Path, fake_files: Callable[[dict[str, bytes]], None]
) -> None:
    dataset = build_dataset(1_000)
    outputs = sandbox_outputs(dataset)
    fake_files(outputs)
    step = make_step(tmp_path)
    result = step.attempt(FakeProject(lambda _: completed(answer(outputs[story.RESULT_FILE]))), lambda: "t", dataset)  # type: ignore[arg-type]
    assert result.status == "PASS" and result.correct is True, result.checks
    assert result.tokens == {"input_tokens": 1500, "output_tokens": 300, "total_tokens": 1800}
    assert result.container == {"id": CONTAINER, "memory_limit": "2g", "expires_after": {"minutes": 20}}
    assert set(result.files) == set(story.OUTPUT_FILES)
    for name in (*story.OUTPUT_FILES, story.RESULT_FILE):
        assert (step.run.path / "outputs" / "1000" / name).read_bytes() == outputs[name]
    assert result.checks[-1][1:] == ["INFO", "Model copied the printed JSON unchanged", "identical"]


def test_attempt_fails_when_sandbox_numbers_are_wrong_even_if_answer_looks_right(
    tmp_path: Path, fake_files: Callable[[dict[str, bytes]], None]
) -> None:
    dataset = build_dataset(1_000)
    good = sandbox_outputs(dataset)
    tampered = json.loads(good[story.RESULT_FILE])
    tampered["filtered_rows"] = 112
    outputs = {**good, story.RESULT_FILE: json.dumps(tampered).encode()}
    fake_files(outputs)
    result = make_step(tmp_path).attempt(
        FakeProject(lambda _: completed(answer(good[story.RESULT_FILE]))),
        lambda: "t",
        dataset,  # type: ignore[arg-type]
    )
    assert (result.status, result.category) == ("FAIL", "incorrect")
    assert "Filter" in result.detail


def test_attempt_classifies_failed_and_tool_less_responses(tmp_path: Path) -> None:
    dataset = build_dataset(1_000)
    failed = Reply({"status": "failed", "output": [], "error": {"code": "server_error", "message": "MemoryError"}})
    result = make_step(tmp_path).attempt(FakeProject(lambda _: failed), lambda: "t", dataset)  # type: ignore[arg-type]
    assert (result.status, result.category) == ("FAIL", "resource")
    plain = make_step(tmp_path).attempt(FakeProject(lambda _: completed("I cannot run code.")), lambda: "t", dataset)  # type: ignore[arg-type]
    assert (plain.status, plain.category) == ("FAIL", "no_execution")


def test_verdict_is_scoped_to_tested_sizes() -> None:
    def results(*statuses: tuple[str, str | None]) -> list[story.SizeResult]:
        return [
            story.SizeResult(rows, 10 * rows, status, category=category)  # type: ignore[arg-type]
            for rows, (status, category) in zip(DATASET_SIZES, statuses, strict=True)
        ]

    verdict, text = story.capacity_verdict(results(("PASS", None), ("PASS", None), ("PASS", None)))
    assert verdict == "Met" and "up to 100,000 rows" in text and "not the platform maximum" in text
    verdict, text = story.capacity_verdict(results(("PASS", None), ("FAIL", "timeout"), ("SKIP", "not_run")))
    assert verdict == "Met, with an observed limit" and "up to 1,000 rows" in text and "10,000 rows" in text
    verdict, _ = story.capacity_verdict(results(("PASS", None), ("FAIL", "incorrect"), ("PASS", None)))
    assert verdict == "Partially met"
    verdict, _ = story.capacity_verdict(results(("BLOCKED", "permission"), ("SKIP", "not_run"), ("SKIP", "not_run")))
    assert verdict == "Not evaluated"
    assert story.capacity_verdict([])[0] == "Not evaluated"


def test_partial_report_is_written_when_the_run_stops(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def interrupted(step: story.Step3) -> None:
        step.report.heading("Results by size")
        first = story.SizeResult(1_000, step.datasets[0].size_bytes, "PASS", seconds=12.5, correct=True)
        step.results.append(first)
        step.record_size(first)
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(story.Step3, "execute", interrupted)
    assert story.main(["--step", "3", "--output-dir", str(tmp_path), "--no-color"]) == 1
    output = tmp_path / "story-1"
    (directory,) = [path for path in output.iterdir() if path.is_dir()]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}Z-step-3-dataset-sizes-[0-9a-f]{8}", directory.name)
    report = (directory / story.STEP3_REPORT).read_text(encoding="utf-8")
    assert "### 1,000 rows" in report and "Capacity summary" in report
    assert "SKIP (not run): the run stopped before this size" in report
    assert "Unexpected failure" in report and "Acceptance verdict: Partially met" in report
    index = (output / "index.md").read_text(encoding="utf-8")
    assert f"Step 3 (dataset sizes) - **Partially met** - [{directory.name}]({directory.name}/03-capacity.md)" in index
    statuses = [json.loads(line)["status"] for line in (directory / "results.jsonl").read_text().splitlines()]
    assert statuses == ["PASS", "FAIL", "SKIP", "SKIP"]


def test_baseline_guard_blocks_or_fails_and_never_repairs(tmp_path: Path) -> None:
    with pytest.raises(story.Blocked, match="Run Task 1"):
        story.load_baseline(tmp_path / story.BASELINE_FILE, "env")
    endpoint = f"{PROJECT}/toolboxes/{story.TOOLBOX_NAME}/versions/1/mcp?api-version=v1"
    toolbox = story.ToolboxRecord(story.TOOLBOX_NAME, "1", endpoint, {"1000": "assistant-a"})
    agents = {
        name: story.AgentSnapshot(
            name, "2", "active", f"p-{name}", True, endpoint if name == story.DATA_ANALYSIS else None
        )
        for name in story.SERVICES.values()
    }
    path = tmp_path / story.BASELINE_FILE
    path.write_text(
        json.dumps(
            {"environment": "env", "toolbox": asdict(toolbox), "agents": {k: asdict(v) for k, v in agents.items()}}
        ),
        encoding="utf-8",
    )
    assert story.load_baseline(path, "env") == (toolbox, agents)
    with pytest.raises(story.Blocked, match="recorded for 'env'"):
        story.load_baseline(path, "other")
    assert story.baseline_problems(agents, agents, toolbox) == ([], [])
    unwired = {**agents, story.DATA_ANALYSIS: story.AgentSnapshot(story.DATA_ANALYSIS, "2", "active", "p", True, None)}
    assert story.baseline_problems(agents, unwired, toolbox)[0] == [
        "data-analysis-agent is not wired to the Step 1 toolbox"
    ]
    moved = {
        **agents,
        "supervisor-agent": story.AgentSnapshot("supervisor-agent", "3", "active", "p-supervisor-agent", True, None),
    }
    assert story.baseline_problems(agents, moved, toolbox) == (
        [],
        ["supervisor-agent changed version or configuration unexpectedly"],
    )


def test_steps_2_and_3_have_no_deploy_or_mutation_paths() -> None:
    source = Path(story.__file__).read_text(encoding="utf-8")
    shared = source[source.index("def generated_files_by_name(") : source.index("def build_parser(")]
    assert "class Step2(ReusingStep)" in shared and "class Step3(ReusingStep)" in shared
    for forbidden in ("Popen", "subprocess", "create_version", "azd", "/openai/v1/files", ".delete(", ".update("):
        assert forbidden not in shared, forbidden


def test_step3_task_and_host_settings_match_config() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    task = {task["label"]: task for task in document["tasks"]}[story.STEP3_TASK_LABEL]
    assert task["command"] == "uv" and "dependsOn" not in task
    assert task["args"][-4:] == ["--step", "3", "--environment", "${input:infraEnvironment}"]
    assert "--mode" not in task["args"]
    manifest = (ROOT / "azure.yaml").read_text(encoding="utf-8")
    data_analysis = manifest[manifest.index("  data-analysis-agent:") : manifest.index("\ninfra:")]
    assert "cpu: '0.5'" in data_analysis and "memory: 1Gi" in data_analysis
    assert "idleTimeoutSeconds: 120" in data_analysis
