"""Story6 previews and reporting must never imply or perform hosted execution."""

import json
from pathlib import Path

import httpx
import httpx2
import pytest

from scripts.run_scenario import story6_approval as runner
from scripts.run_scenario import story6_qualification as qualification


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Story6 preview/blocked execution must not access the network or credentials.")

    import azure.identity

    for client in (httpx.Client, httpx.AsyncClient, httpx2.Client, httpx2.AsyncClient):
        monkeypatch.setattr(client, "send", forbidden)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", forbidden)
    monkeypatch.setattr(azure.identity, "AzureCliCredential", forbidden)


@pytest.mark.parametrize("step", range(1, 8))
def test_preview_is_credential_free_and_does_not_create_evidence(
    step: int, tmp_path: Path, capsys: pytest.CaptureFixture[str], offline: None
) -> None:
    assert runner.main(["--step", str(step), "--output-dir", str(tmp_path)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["azure_requests"] == preview["model_requests"] == preview["mcp_requests"] == 0
    assert preview["hosted_ready"] is False
    assert preview["deployment"] is False and preview["role_changes"] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("step", range(1, 7))
def test_live_steps_record_blocked_before_any_credential_or_network(step: int, tmp_path: Path, offline: None) -> None:
    assert runner.main(["--step", str(step), "--execution", "run", "--output-dir", str(tmp_path)]) == 2
    reports = list(tmp_path.glob("story-6/step-*/*/results.jsonl"))
    assert len(reports) == 1
    events = [json.loads(line) for line in reports[0].read_text().splitlines()]
    assert [event["status"] for event in events] == ["BLOCKED"]
    assert "explicit environment" in events[0]["observed"]
    manifest = json.loads((reports[0].parent / "manifest.json").read_text())
    assert manifest["mode"] == "blocked-before-hosted-execution"


def test_qualification_failures_and_blockers_survive_offline_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: None
) -> None:
    monkeypatch.chdir(tmp_path)

    async def observations() -> list[qualification.Qualification]:
        return [
            qualification.Qualification("Local binding", "PASS", "Synthetic only", {"decision_bound": True}),
            qualification.Qualification(
                "Ordinary MCP wrapper single-send requirement",
                "FAIL",
                "One attempt",
                {"call_attempts": 2, "remote_effects_known": False},
            ),
        ]

    monkeypatch.setattr(qualification, "qualify", observations)
    output = tmp_path / "artifacts"
    assert runner.main(["--step", "1", "--execution", "qualify", "--output-dir", str(output)]) == 1
    configs = list(output.glob("story-6/step-*/*/bundle-config.json"))
    assert len(configs) == 1
    assert (
        runner.main(
            ["--step", "7", "--execution", "run", "--bundle-config", str(configs[0]), "--output-dir", str(output)]
        )
        == 0
    )
    rendered = json.loads((output / "story-6/html/sanitized-evidence.json").read_text())
    assert rendered["story"] == 6
    assert rendered["runs"][0]["counts"] == {"PASS": 1, "FAIL": 1, "BLOCKED": 1}
    checks = rendered["runs"][0]["checks"]
    assert checks[1]["observed"]["call_attempts"] == 2
    assert checks[1]["observed"]["remote_effects_known"] is False
    assert "hosted not evaluated" in rendered["traceability"][0]["summary"].lower()


def test_report_rejects_another_story_instead_of_relabeling_it(tmp_path: Path, offline: None) -> None:
    config = tmp_path / "wrong-story.json"
    config.write_text(json.dumps({"schema": "story-report-bundle-v1", "story": 4}))
    assert runner.main(["--step", "7", "--execution", "run", "--bundle-config", str(config)]) == 2


def test_history_binding_failure_stops_before_a_later_command_is_submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: None
) -> None:
    from scripts.run_scenario import story6_hosted

    submitted = []

    async def execute(*args, evidence, **kwargs):
        evidence("Conversation history before command", {"operation_mismatches": 1})
        submitted.append("would submit")
        pytest.fail("A failed pre-command binding check must stop execution.")

    monkeypatch.setattr(story6_hosted, "execute", execute)
    assert (
        runner.main(
            [
                "--step",
                "2",
                "--execution",
                "run",
                "--environment",
                "synthetic",
                "--capture-history",
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert submitted == []
    events_file = next(tmp_path.glob("story-6/step-*/*/results.jsonl"))
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    assert all(event["status"] == "FAIL" for event in events)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--step", "2", "--execution", "qualify"],
        ["--step", "7", "--execution", "run"],
        ["--step", "1", "--max-seconds", "nan"],
        ["--step", "1", "--max-seconds", "121"],
        ["--step", "2", "--bundle-config", "unrelated.json"],
        ["--step", "1", "--overwrite"],
    ],
)
def test_invalid_options_fail_before_execution(arguments: list[str], offline: None) -> None:
    with pytest.raises(SystemExit) as result:
        runner.main(arguments)
    assert result.value.code == 2


def test_story6_tasks_are_separate_and_explicit_about_deferred_hosted_work() -> None:
    root = Path(__file__).resolve().parents[2]
    document = json.loads((root / ".vscode/tasks.json").read_text())
    tasks = [task for task in document["tasks"] if task["label"].startswith("Agents: Story 6 - ")]
    assert len(tasks) == 7
    for step, task in enumerate(tasks, 1):
        assert task["label"].startswith(f"Agents: Story 6 - {step} ")
        assert task["command"] == "uv" and "dependsOn" not in task
        assert task["args"][task["args"].index("--step") + 1] == str(step)
        assert "--frozen" in task["args"] and "--no-sync" in task["args"]
    assert "--caller-user" in tasks[4]["args"]
    assert "--users" in tasks[5]["args"] and "--case-state" in tasks[5]["args"]
    assert "--case-state" in tasks[1]["args"] and "--action" in tasks[1]["args"]
    assert tasks[2]["args"][tasks[2]["args"].index("--action") + 1] == "inspect"
    inputs = {item["id"]: item for item in document["inputs"]}
    assert inputs["story6ReadinessExecution"]["default"] == inputs["story6Execution"]["default"] == "preview"
