"""Story 1 Step 2: analysis operations and output delivery, without Azure calls."""

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
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import openai
import pytest
from app.services.delegation import Report

from scripts.common.reporting import REPORT_FILE, MarkdownReport, ValidationRun
from scripts.common.synthetic_data import (
    COLUMNS,
    build_dataset,
    expected_aggregates,
    expected_filtered_ids,
    generate_rows,
)
from scripts.run_scenario import story1_data_analysis as story

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "https://a.services.ai.azure.com/api/projects/p"
CONTAINER = "cntr_6adb9c2984d08190bc2c7098eb62cbf409e9166058601c2c"
DATASET = build_dataset(1_000)
ROWS = list(generate_rows(1_000))
EXPECTED = expected_aggregates(ROWS)
IDS = expected_filtered_ids(ROWS)
GUARD_ERROR = (
    "research hosted response caresp_0c8 failed: Stateful commands require verified Foundry hosted caller/session "
    "context. Missing checks: user_id_present.; parent remains incomplete."
)


def png(width: int = 1000, height: int = 600) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return story.PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + header + struct.pack(">I", zlib.crc32(header))


def outputs() -> dict[str, bytes]:
    keep = set(IDS)
    source = list(csv.DictReader(io.StringIO(DATASET.content.decode())))
    filtered = io.StringIO(newline="")
    writer = csv.DictWriter(filtered, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(row for row in source if int(row["row_id"]) in keep)
    by_category = io.StringIO(newline="")
    table = csv.writer(by_category, lineterminator="\n")
    table.writerow(story.BY_CATEGORY_COLUMNS)
    table.writerows([name, t.rows, t.units, t.revenue_cents] for name, t in EXPECTED.items())
    return {
        "filtered.csv": filtered.getvalue().encode(),
        "by_category.csv": by_category.getvalue().encode(),
        "revenue_by_category.png": png(),
    }


def printed(**changes: object) -> dict[str, object]:
    total = story.total_of(EXPECTED)
    value: dict[str, object] = {
        "input_sha256": DATASET.sha256,
        "filtered_rows": len(IDS),
        "by_category": {name: asdict(t) for name, t in EXPECTED.items()},
        "total": asdict(total),
    }
    return {**value, **changes}


def answer(values: dict[str, object], *, containers: bool = True) -> str:
    text = (
        f"Filtered to {len(IDS)} completed West sales.\nTraining leads revenue.\n\n```json\n{json.dumps(values)}\n```"
    )
    return text + (f'\n\n```poc-artifacts-v1\n{{"containers": ["{CONTAINER}"]}}\n```' if containers else "")


def files_api(files: dict[str, bytes]) -> httpx.MockTransport:
    names = list(files)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer t"
        path = request.url.path
        if path.endswith(f"/containers/{CONTAINER}/files"):
            data = [{"id": f"cfile_{i}", "path": f"/mnt/data/{n}", "source": "assistant"} for i, n in enumerate(names)]
            return httpx.Response(200, json={"data": data})
        match = re.search(r"/files/cfile_(\d+)/content$", path)
        if match:
            return httpx.Response(200, content=files[names[int(match.group(1))]])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


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
        }
    )


def failed(message: str) -> Reply:
    return Reply({"id": "caresp_2", "status": "failed", "output": [], "error": {"code": "x", "message": message}})


class FakeProject:
    """Answers per agent name and records every call; nothing else exists to be mutated."""

    def __init__(self, replies: dict[str, Callable[[str], object]]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, object]]] = []

    @contextmanager
    def get_openai_client(self, *, agent_name: str, **options: object) -> Iterator[Any]:
        self.calls.append((agent_name, options))
        reply = self.replies[agent_name]

        def create(*, input: str, stream: bool, **_: object) -> object:
            assert stream is False
            outcome = reply(input)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        yield SimpleNamespace(
            responses=SimpleNamespace(create=create),
            conversations=SimpleNamespace(create=lambda: SimpleNamespace(id="conv_1")),
        )


def make_step(tmp_path: Path) -> story.Step2:
    args = Namespace(environment="env", mode="verify", timeout=300, output_dir=tmp_path)
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    step = story.Step2(args, run, MarkdownReport(run.path / REPORT_FILE, "t"))
    step.project_endpoint = PROJECT
    return step


@pytest.fixture
def fake_files(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, bytes]], None]:
    real = httpx.Client

    def install(files: dict[str, bytes]) -> None:
        monkeypatch.setattr(story.httpx, "Client", lambda **_: real(transport=files_api(files)))

    return install


def run_user_path(step: story.Step2, text: str) -> None:
    project = FakeProject({story.DATA_ANALYSIS: lambda _: completed(text)})
    returned = step.analyze(project)  # type: ignore[arg-type]
    assert returned == text
    step.deliver(returned, "t")


def test_prompt_is_the_recorded_step2_workload() -> None:
    prompt = story.analysis_prompt(DATASET)
    assert prompt.startswith(
        "Use your code interpreter tool in ONE execution, with pandas and matplotlib (Agg backend), on the file in "
        "/mnt/data whose name ends with 'story1-sales-1000.csv':\n1. Filter: keep rows where status == 'completed' "
        "and region == 'West'."
    )
    for fragment in ("/mnt/data/filtered.csv", "/mnt/data/by_category.csv", "/mnt/data/revenue_by_category.png"):
        assert fragment in prompt
    assert prompt.endswith("followed by the exact JSON the tool printed in a ```json fenced block.")
    assert "2026-" not in prompt  # data never passes through the prompt
    assert len(IDS) == 113 and story.total_of(EXPECTED).revenue_cents == 30_545_682


def test_answer_summary_drops_fenced_blocks() -> None:
    assert story.answer_summary(answer(printed())) == (
        f"Filtered to {len(IDS)} completed West sales.\nTraining leads revenue."
    )


def test_full_user_path_passes_only_with_exact_outputs(
    tmp_path: Path, fake_files: Callable[[dict[str, bytes]], None]
) -> None:
    files = outputs()
    fake_files(files)
    step = make_step(tmp_path)
    run_user_path(step, answer(printed()))
    assert {name: step.statuses[name] for name in story.USER_PATH_CHECKS} == dict.fromkeys(
        story.USER_PATH_CHECKS, "PASS"
    )
    for name, content in files.items():
        assert (step.run.path / "outputs" / name).read_bytes() == content
    report = step.report.path.read_text(encoding="utf-8")
    assert "> Filtered to 113 completed West sales." in report
    assert "| TOTAL | 113 | 113 | 1218 | 1218 | 30545682 | 30545682 | yes |" in report
    assert "[filtered.csv](outputs/filtered.csv)" in report and "![Revenue by category]" in report
    assert '"revenue_cents": 30545682' in report and '"png": "1000x600"' in report
    assert "Sample of filtered rows" in report
    step.statuses["Agents reused - after"] = "PASS"
    assert story.step2_verdict(step.statuses) == "Met"


def test_wrong_printed_numbers_fail_even_with_good_files(
    tmp_path: Path, fake_files: Callable[[dict[str, bytes]], None]
) -> None:
    fake_files(outputs())
    step = make_step(tmp_path)
    wrong = printed(filtered_rows=112, total={"rows": 113, "units": 1218, "revenue_cents": 1})
    run_user_path(step, answer(wrong))
    assert step.statuses["Filter"] == "FAIL" and step.statuses["Group and summarize"] == "FAIL"
    assert step.statuses["by_category.csv"] == "PASS"
    assert story.step2_verdict(step.statuses) == "Partially met"


def test_tampered_or_missing_files_fail(tmp_path: Path, fake_files: Callable[[dict[str, bytes]], None]) -> None:
    files = outputs()
    files["by_category.csv"] = files["by_category.csv"].replace(b"Training,28", b"Training,27")
    del files["revenue_by_category.png"]
    fake_files(files)
    step = make_step(tmp_path)
    run_user_path(step, answer(printed()))
    assert step.statuses["filtered.csv"] == "PASS"
    assert step.statuses["by_category.csv"] == "FAIL"
    assert step.statuses["revenue_by_category.png"] == "FAIL"


def test_missing_container_reference_fails_every_file(tmp_path: Path) -> None:
    step = make_step(tmp_path)
    run_user_path(step, answer(printed(), containers=False))
    assert step.statuses["Container reference returned"] == "FAIL"
    assert all(step.statuses[name] == "FAIL" for name in story.OUTPUT_FILES)


def test_hosted_request_has_no_retries_and_maps_errors(tmp_path: Path) -> None:
    step = make_step(tmp_path)
    error = openai.APITimeoutError(request=httpx.Request("POST", "https://x"))
    project = FakeProject({story.DATA_ANALYSIS: lambda _: error})
    assert step.analyze(project) is None  # type: ignore[arg-type]
    assert project.calls == [(story.DATA_ANALYSIS, {"timeout": 300, "max_retries": 0})]
    assert step.statuses["Hosted analysis request"] == "FAIL"
    assert story.step2_verdict(step.statuses) == "Not met"


def test_supervisor_outcomes_keep_the_nested_guard() -> None:
    status, observed = story.supervisor_outcome(failed(GUARD_ERROR).payload, EXPECTED)  # type: ignore[arg-type]
    assert status == "BLOCKED" and observed["failed_hop"] == "research"
    status, observed = story.supervisor_outcome(failed("boom").payload, EXPECTED)  # type: ignore[arg-type]
    assert status == "FAIL" and observed["failed_hop"] is None
    status, _ = story.supervisor_outcome(completed("not a report").payload, EXPECTED)  # type: ignore[arg-type]
    assert status == "FAIL"
    report = Report(
        agent="supervisor",
        workflow_id=uuid4(),
        owner_id=uuid4(),
        session_id=uuid4(),
        conversation_id="conv_1",
        platform_session_id="s",
        progress="ready",
        answer="research: ok\n\ndata_analysis: " + answer(printed(), containers=False),
        children={},
    )
    status, observed = story.supervisor_outcome(completed(report.model_dump_json()).payload, EXPECTED)  # type: ignore[arg-type]
    assert status == "PASS" and observed["files_forwarded"] is False and observed["containers"] == []
    forwarded = report.model_copy(update={"containers": [CONTAINER]})
    status, observed = story.supervisor_outcome(completed(forwarded.model_dump_json()).payload, EXPECTED)  # type: ignore[arg-type]
    assert status == "PASS" and observed["files_forwarded"] is True and observed["containers"] == [CONTAINER]


def supervisor_report(containers: list[str]) -> str:
    return Report(
        agent="supervisor",
        workflow_id=uuid4(),
        owner_id=uuid4(),
        session_id=uuid4(),
        conversation_id="conv_1",
        platform_session_id="s",
        progress="ready",
        answer="research: ok\n\ndata_analysis: " + answer(printed(), containers=False),
        children={},
        containers=containers,
    ).model_dump_json()


def test_forwarded_container_files_are_downloaded_and_validated(
    tmp_path: Path, fake_files: Callable[[dict[str, bytes]], None]
) -> None:
    files = outputs()
    fake_files(files)
    step = make_step(tmp_path)
    project = FakeProject({story.SUPERVISOR: lambda _: completed(supervisor_report([CONTAINER]))})
    step.supervisor(project, lambda: "t")  # type: ignore[arg-type]
    assert step.statuses[story.SUPERVISOR_CHECK] == "PASS"
    assert step.statuses[story.FORWARD_CHECK] == "PASS"
    for name, content in files.items():
        assert (step.run.path / "outputs" / "supervisor" / name).read_bytes() == content
    assert "[filtered.csv](outputs/supervisor/filtered.csv)" in step.report.path.read_text(encoding="utf-8")


def test_forwarding_fails_without_containers_or_with_tampered_files(
    tmp_path: Path, fake_files: Callable[[dict[str, bytes]], None]
) -> None:
    step = make_step(tmp_path)
    step.supervisor(FakeProject({story.SUPERVISOR: lambda _: completed(supervisor_report([]))}), lambda: "t")  # type: ignore[arg-type]
    assert step.statuses[story.FORWARD_CHECK] == "FAIL"
    files = outputs()
    files["by_category.csv"] = files["by_category.csv"].replace(b"Training,28", b"Training,27")
    fake_files(files)
    step = make_step(tmp_path)
    step.supervisor(FakeProject({story.SUPERVISOR: lambda _: completed(supervisor_report([CONTAINER]))}), lambda: "t")  # type: ignore[arg-type]
    assert step.statuses[story.FORWARD_CHECK] == "FAIL"


def test_supervisor_path_sends_the_existing_workflow_command(tmp_path: Path) -> None:
    step = make_step(tmp_path)
    sent: list[str] = []

    def reply(text: str) -> Reply:
        sent.append(text)
        return failed(GUARD_ERROR)

    def no_token() -> str:
        raise AssertionError("No download is attempted when the Supervisor path is blocked")

    project = FakeProject({story.SUPERVISOR: reply})
    step.supervisor(project, no_token)  # type: ignore[arg-type]
    assert step.statuses[story.SUPERVISOR_CHECK] == "BLOCKED"
    assert step.statuses[story.FORWARD_CHECK] == "BLOCKED"
    assert "at the research hop, before Data Analysis ran" in step.obs["forward_blocked"]
    assert project.calls == [(story.SUPERVISOR, {"timeout": 300, "max_retries": 0})]
    command = json.loads(sent[0].removeprefix("POC_WORKFLOW "))
    assert command["operation"] == "create" and command["message"] == story.analysis_prompt(DATASET)


def test_verdict_rules() -> None:
    passing = dict.fromkeys(story.USER_PATH_CHECKS, "PASS")
    assert story.step2_verdict({**passing, story.SUPERVISOR_CHECK: "BLOCKED"}) == "Met"
    assert story.step2_verdict({**passing, "Agents reused - after": "FAIL"}) == "Not met"
    assert story.step2_verdict({**passing, "filtered.csv": "FAIL"}) == "Partially met"
    assert story.step2_verdict({"Hosted analysis request": "BLOCKED"}) == "Not evaluated"
    assert story.step2_verdict({}) == "Not evaluated"


def test_blocked_run_still_writes_report_and_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def blocked(_step: story.Step2) -> None:
        raise story.Blocked("Reuse baseline agents-baseline.json is missing. Run Task 1 (Step 1) first.")

    monkeypatch.setattr(story.Step2, "execute", blocked)
    assert story.main(["--step", "2", "--output-dir", str(tmp_path), "--no-color"]) == 2
    output = tmp_path / "story-1"
    (directory,) = [path for path in (output / "step-02-analysis-and-outputs").iterdir() if path.is_dir()]
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", directory.name)
    report = (directory / REPORT_FILE).read_text(encoding="utf-8")
    assert "Run Task 1" in report and "Acceptance verdict: Not evaluated" in report and "Delivery paths" in report
    index = (output / "index.md").read_text(encoding="utf-8")
    link = f"step-02-analysis-and-outputs/{directory.name}"
    assert f"Step 2 (analysis and outputs) - **Not evaluated** - [{link}]({link}/report.md)" in index
    with pytest.raises(SystemExit):
        story.main(["--step", "2", "--mode", "enable-if-missing", "--output-dir", str(tmp_path)])


def test_step2_task_is_read_only_and_has_no_hidden_dependencies() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    task = {task["label"]: task for task in document["tasks"]}[story.STEP2_TASK_LABEL]
    assert task["command"] == "uv" and "dependsOn" not in task
    assert task["args"][-4:] == ["--step", "2", "--environment", "${input:infraEnvironment}"]
    assert "--mode" not in task["args"]
