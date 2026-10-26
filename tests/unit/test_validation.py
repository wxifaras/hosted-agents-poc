"""Verify Rich/JSONL agreement, redaction, help, and explicit failure statuses."""

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from app.core.redaction import sanitize, sanitize_text

from scripts.common import validate_environment
from scripts.common.reporting import ValidationRun
from scripts.run_scenario import run_scenarios, trigger_agent


def test_recursive_redaction() -> None:
    result = sanitize(
        {
            "password": "private-value",
            "x-agent-foundry-call-id": "opaque-platform-context",
            "nested": [
                {"url": "https://example.com/file?sig=private-signature", "authorization": "Bearer private-token"}
            ],
        }
    )
    serialized = json.dumps(result)
    assert "private-" not in serialized
    assert "opaque-platform-context" not in serialized
    assert "[REDACTED]" in serialized
    assert "secret-value" not in sanitize_text("token=secret-value Bearer secret-value")
    assert "secret-value" not in sanitize_text('{"token": "secret-value"}')


@pytest.mark.parametrize(("status", "expected_code"), [("PASS", 0), ("FAIL", 1), ("BLOCKED", 2), ("SKIP", 1)])
def test_result_output_and_exit_code(tmp_path: Path, status: str, expected_code: int) -> None:
    run = ValidationRun("test", "unit", tmp_path, no_color=True)
    assert run.path == tmp_path / run.run_id
    # Use the command's public statuses, not an arbitrary unvalidated payload.
    from scripts.common.reporting import Status

    statuses: dict[str, Status] = {"PASS": "PASS", "FAIL": "FAIL", "BLOCKED": "BLOCKED", "SKIP": "SKIP"}
    run.result("step", statuses[status], expected="Observed result", observed={"token": "private-value"})
    assert run.finish() == expected_code
    event = json.loads((run.path / "events.jsonl").read_text(encoding="utf-8"))
    result = json.loads((run.path / "results.jsonl").read_text(encoding="utf-8"))
    assert event == result
    assert result["status"] == status
    assert result["observed"]["token"] == "[REDACTED]"


def test_empty_run_cannot_pass(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="without observations"):
        ValidationRun("test", "unit", tmp_path).finish()


def test_help_needs_no_configuration() -> None:
    for command in (trigger_agent.main, validate_environment.main, run_scenarios.main):
        with pytest.raises(SystemExit) as result:
            command(["--help"])
        assert result.value.code == 0


@pytest.mark.parametrize(
    "module",
    [
        "scripts.common.validate_environment",
        "scripts.deploy.grant_local_rbac",
        "scripts.run_scenario.trigger_agent",
        "scripts.run_scenario.run_scenarios",
    ],
)
def test_reorganized_module_help_is_runnable(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_local_preflight_does_not_read_azure_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail() -> None:
        raise AssertionError("Local preflight must not load Azure settings")

    monkeypatch.setattr(validate_environment, "get_settings", fail)
    assert validate_environment.main(["--mode", "local", "--output-dir", str(tmp_path), "--no-color"]) == 0


def test_configured_preflight_is_blocked_without_model(tmp_path: Path) -> None:
    assert validate_environment.main(["--mode", "configured", "--output-dir", str(tmp_path), "--no-color"]) == 2


def test_missing_host_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(*args: object, **kwargs: object) -> dict[str, object]:
        raise httpx.ConnectError("No local host is listening")

    monkeypatch.setattr(trigger_agent, "invoke_local", fail)
    assert trigger_agent.main(["--output-dir", str(tmp_path), "--no-color"]) == 2


def test_nonterminal_response_is_not_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(trigger_agent, "invoke_local", lambda *args, **kwargs: {"status": "in_progress"})
    assert trigger_agent.main(["--output-dir", str(tmp_path), "--no-color"]) == 1


def test_empty_completed_response_is_not_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(trigger_agent, "invoke_local", lambda *args, **kwargs: {"status": "completed"})
    assert trigger_agent.main(["--output-dir", str(tmp_path), "--no-color"]) == 1


def test_server_error_is_failed_not_a_blocked_prerequisite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(*args: object, **kwargs: object) -> dict[str, object]:
        response = httpx.Response(500, request=httpx.Request("POST", "http://127.0.0.1:8088/responses"))
        response.raise_for_status()
        raise AssertionError("HTTP 500 must raise")

    monkeypatch.setattr(trigger_agent, "invoke_local", fail)
    assert trigger_agent.main(["--output-dir", str(tmp_path), "--no-color"]) == 1


def test_completed_response_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        trigger_agent,
        "invoke_local",
        lambda *args, **kwargs: {
            "status": "completed",
            "id": "resp-unit-test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "HOSTED-AGENTS-POC-OK"}],
                }
            ],
        },
    )
    assert trigger_agent.main(["--output-dir", str(tmp_path), "--no-color"]) == 0


def test_scenario_invokes_three_distinct_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agents: list[str] = []

    def invoke(agent: str, message: str, *, timeout: float) -> dict[str, object]:
        agents.append(agent)
        return {
            "status": "completed",
            "id": f"resp-{agent}",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "HOSTED-AGENTS-POC-OK"}],
                }
            ],
        }

    monkeypatch.setattr(trigger_agent, "invoke_local", invoke)
    assert run_scenarios.main(["--output-dir", str(tmp_path), "--no-color"]) == 0
    assert agents == ["supervisor", "research", "data_analysis"]
    events = [json.loads(line) for line in next(tmp_path.glob("*/results.jsonl")).read_text().splitlines()]
    assert [event["observed"]["request"] for event in events] == [
        {"agent": agent, "message": "Reply with exactly: HOSTED-AGENTS-POC-OK"} for agent in agents
    ]


@pytest.mark.parametrize("failed", [False, True])
def test_request_message_is_shown_before_sending_and_sanitized_in_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failed: bool,
) -> None:
    message = "Explain your role. token=private-value"

    def invoke(agent: str, sent: str, *, timeout: float) -> dict[str, object]:
        before = capsys.readouterr().out
        assert "Sending to research:" in before
        assert "Explain your role." in before
        assert "private-value" not in before
        assert sent == message  # Presentation redaction must not alter the prompt sent to the host.
        if failed:
            raise httpx.ConnectError("No host listening")
        return {
            "status": "completed",
            "id": "resp-test",
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Research role"}]},
            ],
        }

    monkeypatch.setattr(trigger_agent, "invoke_local", invoke)
    assert trigger_agent.main(
        [
            "--agent",
            "research",
            "--message",
            message,
            "--output-dir",
            str(tmp_path),
            "--no-color",
        ]
    ) == (2 if failed else 0)
    evidence = next(tmp_path.glob("*/results.jsonl")).read_text()
    assert "private-value" not in evidence
    assert json.loads(evidence)["observed"]["request"] == {
        "agent": "research",
        "message": "Explain your role. token=[REDACTED]",
    }


def test_usage_counts_visible_but_credentials_still_redacted() -> None:
    assert sanitize(
        {
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "reasoning_tokens": 2,
            "cached_tokens": 0,
            "access_token": "secret-value",
            "token": 123,
            "input_tokens_details": "secret-value",
            "encrypted_content": "opaque-reasoning",
        }
    ) == {
        "input_tokens": 12,
        "output_tokens": 3,
        "total_tokens": 15,
        "reasoning_tokens": 2,
        "cached_tokens": 0,
        "access_token": "[REDACTED]",
        "token": "[REDACTED]",
        "input_tokens_details": "[REDACTED]",
        "encrypted_content": "[REDACTED]",
    }
    assert sanitize({"input_tokens": "secret-value"})["input_tokens"] == "[REDACTED]"
    assert sanitize({"total_tokens": -1})["total_tokens"] == "[REDACTED]"


def test_smoke_reporting_omits_reasoning_from_console_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "status": "completed",
        "id": "resp-test",
        "agent_session_id": "session-test",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque-do-not-print", "summary": ["hidden-do-not-print"]},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "HOSTED-AGENTS-POC-OK"}],
            },
        ],
    }
    monkeypatch.setattr(trigger_agent, "invoke_local", lambda *args, **kwargs: payload)
    assert trigger_agent.main(["--output-dir", str(tmp_path), "--no-color"]) == 0
    console = capsys.readouterr().out
    path = next(tmp_path.glob("*/results.jsonl"))
    evidence = path.read_text()
    for text in (console, evidence):
        assert "encrypted_content" not in text
        assert "opaque-do-not-print" not in text
        assert "hidden-do-not-print" not in text
        assert "HOSTED-AGENTS-POC-OK" in text
    response = json.loads(evidence)["observed"]["response"]
    assert response["usage"] == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "reasoning_tokens": 2}
    assert payload["output"][0]["encrypted_content"] == "opaque-do-not-print"  # Wire payload is not altered.


@pytest.mark.parametrize(
    "output",
    [
        [{"type": "reasoning", "encrypted_content": "opaque"}],
        [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "wrong-marker"}]}],
    ],
)
def test_smoke_requires_visible_exact_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    output: list,
) -> None:
    monkeypatch.setattr(
        trigger_agent,
        "invoke_local",
        lambda *args, **kwargs: {
            "status": "completed",
            "id": "resp-test",
            "output": output,
        },
    )
    assert trigger_agent.main(["--output-dir", str(tmp_path), "--no-color"]) == 1


@pytest.mark.parametrize("verbose", [False, True])
def test_three_agent_results_use_separate_readable_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    verbose: bool,
) -> None:
    def invoke(agent: str, message: str, *, timeout: float) -> dict[str, object]:
        if agent == "research":
            return {
                "id": "resp-research",
                "status": "failed",
                "error": {"code": "server_error", "message": "Model request timed out"},
                "output": [],
            }
        return {
            "id": f"resp-{agent}",
            "status": "completed",
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "HOSTED-AGENTS-POC-OK"}],
                }
            ],
        }

    monkeypatch.setattr(trigger_agent, "invoke_local", invoke)
    arguments = ["--output-dir", str(tmp_path), "--no-color"]
    if verbose:
        arguments.append("--verbose")
    assert run_scenarios.main(arguments) == 1
    console = capsys.readouterr().out
    for agent in ("supervisor", "research", "data_analysis"):
        assert console.count(f"{agent} - expected versus observed") == 1
    previous_end = -1
    for number, (agent, status) in enumerate(
        (("supervisor", "PASS"), ("research", "FAIL"), ("data_analysis", "PASS")),
        start=1,
    ):
        start = console.index(f"START {number:02d} | {agent} | local")
        end = console.index(f"END {number:02d} | {agent} | {status}")
        assert previous_end < start < end
        previous_end = end
    assert "RUN FINISHED | PASS: 2 | FAIL: 1" in console
    assert "\x1b[" not in console
    assert "Message sent" in console
    assert "Answer" in console
    assert "input_tokens" in console
    assert "Model request timed out" in console
    assert ('"observed":' in console) is verbose
    events = [json.loads(line) for line in next(tmp_path.glob("*/results.jsonl")).read_text().splitlines()]
    assert [event["status"] for event in events] == ["PASS", "FAIL", "PASS"]
    assert events[0]["observed"]["response"]["usage"]["total_tokens"] == 15


def test_connection_failure_also_has_agent_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("Research host is not listening")

    monkeypatch.setattr(trigger_agent, "invoke_local", fail)
    assert trigger_agent.main(["--agent", "research", "--output-dir", str(tmp_path), "--no-color"]) == 2
    console = capsys.readouterr().out
    assert "research - expected versus observed" in console
    assert "Research host is not listening" in console
    assert "BLOCKED" in console
    assert "START 01 | research | local" in console
    assert "END 01 | research | BLOCKED" in console
