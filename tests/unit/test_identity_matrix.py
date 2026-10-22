"""Offline operator regressions: incomplete evidence never passes as hosted proof."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from app.services.identity_checks import IDENTITY_PREFIX, Check, IdentityCommand, IdentityMatrix

from scripts.run_scenario import identity_matrix


@pytest.mark.parametrize("direct_control", [False, True])
@pytest.mark.parametrize("history_only", [False, True])
@pytest.mark.parametrize(
    "shape",
    [
        "complete",
        "empty",
        "missing-final",
        "missing-state-denial",
        "missing-history-denial",
        "duplicate",
        "failed",
        "blocked",
        "invalid-genuine",
    ],
)
def test_operator_requires_complete_unique_assertions_and_preserves_failures(
    direct_control: bool, history_only: bool, shape: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    names = [
        "delegated-user: context",
        "delegated-user: A state continuity",
        "delegated-user: B cannot read A state",
        "delegated-user: A history continuity",
        "delegated-user: B cannot continue A history",
        "delegated-user: A continues after B attempt",
    ]
    if not direct_control:
        names += [
            "call-context: context",
            "call-context: A state continuity",
            "call-context: A history continuity",
            "call-context: cross-user tests",
        ]
    checks = [
        Check(name=name, status="BLOCKED" if name == "call-context: cross-user tests" else "PASS") for name in names
    ]
    for check in checks:
        if check.name == "delegated-user: B cannot continue A history":
            check.observed["explicit_session_requested"] = not history_only
    omitted = {
        "missing-final": "delegated-user: A continues after B attempt",
        "missing-state-denial": "delegated-user: B cannot read A state",
        "missing-history-denial": "delegated-user: B cannot continue A history",
    }.get(shape)
    checks = [check for check in checks if check.name != omitted]
    if shape == "empty":
        checks = []
    elif shape == "duplicate":
        checks.append(checks[0])
    elif shape in ("failed", "blocked"):
        checks[2] = checks[2].model_copy(update={"status": "FAIL" if shape == "failed" else "BLOCKED"})
    elif shape == "invalid-genuine":
        checks = [check for check in checks if check.name != "call-context: cross-user tests"]
        checks.append(Check(name="call-context: cross-user tests", status="PASS"))
    matrix = IdentityMatrix(checks=checks)
    endpoint = "https://example.invalid/api/projects/poc"
    project = MagicMock()
    project.__enter__.return_value = project
    project.agents.get.return_value.as_dict.return_value = {
        "versions": {"latest": {"version": "10", "status": "active"}},
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                ]
            }
        },
    }
    response = SimpleNamespace(
        status="completed", error=None, id="synthetic-response", output_text=matrix.model_dump_json()
    )
    raw = SimpleNamespace(
        parse=lambda: response,
        http_request=httpx.Request("POST", endpoint + "/agents/supervisor-agent/endpoint/protocols/openai/responses"),
        status_code=200,
        headers={},
    )
    client = project.get_openai_client.return_value.__enter__.return_value
    client.conversations.create.return_value = SimpleNamespace(id="synthetic-conversation")
    client.responses.with_raw_response.create.return_value = raw
    direct = AsyncMock(return_value=matrix)
    monkeypatch.setattr(identity_matrix, "native_json", lambda *_args: {"FOUNDRY_PROJECT_ENDPOINT": endpoint})
    monkeypatch.setattr(identity_matrix, "AzureCliCredential", MagicMock())
    monkeypatch.setattr(identity_matrix, "AIProjectClient", MagicMock(return_value=project))
    monkeypatch.setattr(identity_matrix, "verify_idle_timeout", lambda *_args: 120)
    monkeypatch.setattr(identity_matrix, "direct_control", direct)
    args = ["--output-dir", str(tmp_path), "--no-color"]
    if direct_control:
        args.append("--direct-control")
    if history_only:
        args.append("--history-only")
    code = identity_matrix.main(args)
    expected_code = 1
    if shape == "blocked" or (shape == "complete" and not direct_control):
        expected_code = 2
    elif shape == "complete":
        expected_code = 0
    assert code == expected_code
    evidence = next(tmp_path.iterdir()) / "results.jsonl"
    results = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines()]
    collection = [result for result in results if result["step"] == "Matrix completeness"]
    assert len(collection) == 1
    complete = shape in ("complete", "failed", "blocked")
    assert collection[0]["status"] == ("PASS" if complete else "FAIL")
    observed = collection[0]["observed"]
    assert observed["missing_checks"] == (sorted(names) if shape == "empty" else [omitted] if omitted else [])
    assert observed["duplicate_checks"] == ([names[0]] if shape == "duplicate" else [])
    assert observed["invalid_checks"] == (["call-context: cross-user tests"] if shape == "invalid-genuine" else [])
    for check in checks:
        assert any(result["step"] == check.name and result["status"] == check.status for result in results)
    assert direct.await_count == int(direct_control)
    assert client.responses.with_raw_response.create.call_count == int(not direct_control)
    if direct_control:
        assert direct.await_args.kwargs == {"history_only": history_only}
    else:
        wire = client.responses.with_raw_response.create.call_args.kwargs["input"]
        command = IdentityCommand.model_validate_json(wire.removeprefix(IDENTITY_PREFIX))
        assert command.operation == ("compare-history-only" if history_only else "compare")
    assert matrix.genuine_two_entra_users_tested is False


@pytest.mark.parametrize("reported_binding", [None, True, False, 0])
def test_history_only_completeness_requires_explicit_boolean_omission(reported_binding: bool | int | None) -> None:
    matrix = IdentityMatrix(
        checks=[
            Check(name=f"delegated-user: {name}", status="PASS")
            for name in (
                "context",
                "A state continuity",
                "B cannot read A state",
                "A history continuity",
                "B cannot continue A history",
                "A continues after B attempt",
            )
        ]
    )
    matrix.checks[4].observed["explicit_session_requested"] = reported_binding
    result = matrix.completeness_check(direct_control=True, history_only=True)
    assert result.status == ("PASS" if reported_binding is False else "FAIL")


def test_history_modes_cannot_be_combined() -> None:
    with pytest.raises(SystemExit) as error:
        identity_matrix.build_parser().parse_args(["--response-chains", "--history-only"])
    assert error.value.code == 2
