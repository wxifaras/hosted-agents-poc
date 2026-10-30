"""Story6 final-results rendering uses saved evidence, not private artifacts or cloud."""

import json
import re
import socket
from hashlib import sha256
from pathlib import Path
from shutil import copyfile

import pytest

from scripts import story6_report as report
from scripts.generate_report import ReportError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Clone only the current public Markdown/evidence fixture, with no old reports."""
    root = tmp_path / "workspace"
    (root / "docs").mkdir(parents=True)
    copyfile(ROOT / report.REPORT, root / report.REPORT)
    target = root / report.EVIDENCE_SOURCE
    target.parent.mkdir(parents=True)
    copyfile(ROOT / report.EVIDENCE_SOURCE, target)
    monkeypatch.setattr(report, "ROOT", root)

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("Final results must render offline.")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    return root


def snapshot(root: Path) -> tuple[Path, dict]:
    path = root / report.EVIDENCE_SOURCE
    return path, json.loads(path.read_bytes())


def test_final_report_contains_all_criteria_and_real_gap_without_history_or_private_inputs(workspace: Path) -> None:
    sources = [workspace / report.REPORT, workspace / report.EVIDENCE_SOURCE]
    original = {path: sha256(path.read_bytes()).hexdigest() for path in sources}
    output = workspace / report.OUTPUT
    html_path = report.generate_report(workspace, output)
    html = html_path.read_text(encoding="utf-8")
    data = json.loads((output / "sanitized-evidence.json").read_bytes())
    assert data["schema"] == "story6-final-results-v1"
    assert len(data["criteria"]) == 5 and len(data["runs"]) == 10
    assert data["counts"] == {"PASS": 37, "FAIL": 1}
    direct = next(item for item in data["runs"] if item["key"] == "direct-access")
    failed = [check for check in direct["checks"] if check["status"] == "FAIL"]
    assert [check["name"] for check in failed] == ["Direct valid-token call without approval"]
    assert failed[0]["observed"]["unapproved_result_returned"] is True
    assert "Boundary: approval is runtime-scoped" in html
    assert "This is not a failure of Hosted approval" in html
    assert "Direct approval gate did not work" not in html
    assert "ten-observation evidence fixture" in html
    assert all(f'id="criterion-{number}"' in html for number in range(1, 6))
    for phrase in ("What worked", "What did not work", "How this was validated", "Notification assessment", "FAIL"):
        assert phrase in html
    for unwanted in ("Detailed recorded runs", "Sponsor decisions", "Native interpreter memory", "reconnect resend"):
        assert unwanted not in html
    assert len(list((output / "evidence").glob("*.json"))) == 10
    assert html.count('class="task-flow"') == 10
    assert html.count("<svg ") == 10
    for run in data["runs"]:
        flow = run["task_flow"]
        assert flow["available"] is True
        assert "not an execution trace" in flow["notice"]
        assert all(
            reference["anchor"] in {check["anchor"] for check in run["checks"]}
            for node in flow["nodes"]
            for reference in node["references"]
        )
    direct_flow = direct["task_flow"]
    assert next(node for node in direct_flow["nodes"] if node["key"] == "direct")["evidence_status"] == "FAIL"
    ids = re.findall(r'id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert all(line == line.rstrip() for line in html.splitlines())
    assert not list(workspace.glob("**/case-state.json"))
    assert {path: sha256(path.read_bytes()).hexdigest() for path in sources} == original
    assert report.generate_report(workspace, output, overwrite=True) == html_path
    assert html_path.read_text(encoding="utf-8") == html
    assert set(re.findall(r'href="#([^"]+)"', html)) <= set(re.findall(r'id="([^"]+)"', html))
    assert not re.search(r"<script\b|https?://[^\"'\s>]+\.(?:css|js)\b", html, re.I)
    for key, delta in (("entra-approval", 1), ("entra-rejection", 0)):
        custom = next(item for item in data["runs"] if item["key"] == key)
        assert custom["task_flow"]["definition"] == "story-6-entra-mcp"
        check = next(check for check in custom["checks"] if check["name"] == "Entra MCP execution evidence")
        assert check["observed"]["execution_delta"] == delta
        assert check["observed"]["remote_execution_independently_verified"] is True


@pytest.mark.parametrize("problem", ["missing", "other-story", "empty-checks", "counts", "local", "failure-erased"])
def test_bad_evidence_cannot_turn_into_a_final_success(workspace: Path, problem: str) -> None:
    path, source = snapshot(workspace)
    direct = next(run for run in source["runs"] if run["key"] == "direct-access")
    if problem == "missing":
        path.unlink()
    else:
        if problem == "other-story":
            source["story"] = 4
        elif problem == "empty-checks":
            direct["checks"] = []
        elif problem == "counts":
            direct["counts"] = {"PASS": 5}
        elif problem == "local":
            direct["provenance"]["mode"] = "offline-sdk-qualification"
        else:
            for check in direct["checks"]:
                check["status"] = "PASS"
            direct["counts"] = {"PASS": 5}
        path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises((ReportError, OSError)):
        report.generate_report(workspace, workspace / report.OUTPUT)
    assert not (workspace / report.OUTPUT).exists()


@pytest.mark.parametrize("problem", ["epoch", "count", "receipt", "route"])
def test_custom_mcp_requires_independent_same_epoch_receipts(workspace: Path, problem: str) -> None:
    path, source = snapshot(workspace)
    custom = next(run for run in source["runs"] if run["key"] == "entra-approval")
    execution = next(check["observed"] for check in custom["checks"] if check["name"] == "Entra MCP execution evidence")
    if problem == "epoch":
        execution["same_server_epoch"] = False
    elif problem == "count":
        execution["execution_delta"] = 2
    elif problem == "receipt":
        execution["matching_receipts"] = 0
    else:
        next(check["observed"] for check in custom["checks"] if check["name"] == "Coordinator command")["mcp_route"] = (
            "foundry-toolbox"
        )
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ReportError, match="independent receipts"):
        report.generate_report(workspace, workspace / report.OUTPUT)


def test_markdown_is_authoritative_and_summary_contract_is_strict(workspace: Path) -> None:
    path = workspace / report.REPORT
    text = path.read_text(encoding="utf-8").replace(
        "Use this interaction pattern where callers cannot bypass the required runtime",
        "A revised next step from the Markdown report",
    )
    path.write_text(text, encoding="utf-8")
    assert report.build_report(workspace)["criteria"][0]["next_step"] == "A revised next step from the Markdown report"
    path.write_text(text.replace("## Summary", "## Renamed summary"), encoding="utf-8")
    with pytest.raises(ReportError, match="Missing report section"):
        report.build_report(workspace)


def test_html_escape_and_shared_redaction_apply_before_exports(workspace: Path) -> None:
    path, source = snapshot(workspace)
    rejected = next(run for run in source["runs"] if run["key"] == "rejection")
    rejected["scope"] = '<script>alert("injection")</script> private@example.test'
    rejected["checks"][0]["observed"]["secret"] = "PRIVATE_PAYLOAD"
    path.write_text(json.dumps(source), encoding="utf-8")
    output = report.generate_report(workspace, workspace / report.OUTPUT)
    html = output.read_text(encoding="utf-8")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "private@example.test" not in html
    assert not any("PRIVATE_PAYLOAD" in p.read_text(encoding="utf-8") for p in output.parent.rglob("*.json"))


def test_overwrite_requires_unchanged_sources_and_refuses_unrelated_files(workspace: Path) -> None:
    output = workspace / report.OUTPUT
    report.generate_report(workspace, output)
    with pytest.raises(ReportError, match="Existing output"):
        report.generate_report(workspace, output)
    (output / "unrelated.txt").write_text("keep")
    with pytest.raises(ReportError, match="unrelated"):
        report.generate_report(workspace, output, overwrite=True)
    (output / "unrelated.txt").unlink()
    with (workspace / report.REPORT).open("a", encoding="utf-8") as stream:
        stream.write("\nReviewed wording changed.\n")
    with pytest.raises(ReportError, match="unchanged"):
        report.generate_report(workspace, output, overwrite=True)
    with pytest.raises(ReportError, match="Output must"):
        report.generate_report(workspace, workspace / "docs")


def test_cli_default_regenerates_only_final_results_after_cloning(workspace: Path) -> None:
    assert report.main([]) == 0
    assert (workspace / report.OUTPUT / "index.html").is_file()
    assert report.main(["--overwrite"]) == 0
    path = workspace / report.REPORT
    path.unlink()
    assert report.main(["--output", str(workspace / "artifacts/report")]) == 2


def test_final_report_task_is_offline_and_does_not_replace_generic_evidence_tasks() -> None:
    document = json.loads((ROOT / ".vscode/tasks.json").read_bytes())
    tasks = {item["label"]: item for item in document["tasks"]}
    task = tasks["Agents: Build Story6 final-results HTML"]
    assert task["command"] == "uv" and "dependsOn" not in task
    assert task["args"] == [
        "run",
        "--frozen",
        "--no-sync",
        "--group",
        "validation",
        "python",
        "-m",
        "scripts.story6_report",
        "--overwrite",
    ]
    assert "scripts.run_scenario.story6_approval" in tasks["Agents: Story 6 - 7 Build evidence report"]["args"]
    assert "View with HTML Preview" in (ROOT / report.REPORT).read_text(encoding="utf-8")
