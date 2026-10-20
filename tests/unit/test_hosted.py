"""Packaging and hosted verification regressions; all remote operations are mocked."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from app import hosted
from app.core.settings import Settings
from openai import APIStatusError

from scripts.deploy import grant_agent_rbac
from scripts.run_scenario import hosted_smoke

ROOT = Path(__file__).resolve().parents[2]
SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
TENANT = "33333333-3333-4333-8333-333333333333"
ACCOUNT = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-poc/providers/Microsoft.CognitiveServices/accounts/poc"
CONTEXT = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://poc.services.ai.azure.com/api/projects/poc",
    "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
    "AZURE_RESOURCE_GROUP": "rg-poc",
    "AZURE_TENANT_ID": TENANT,
    "AZURE_AI_ACCOUNT_ID": ACCOUNT,
}


def agent_record(name: str, *, active: bool = True) -> dict:
    return {
        "instance_identity": {"principal_id": PRINCIPAL},
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100},
                ]
            }
        },
        "versions": {
            "latest": {"id": f"{name}:1", "name": name, "version": "1", "status": "active" if active else "failed"}
        },
    }


def test_shared_manifest_reuses_root_lock_and_runtime_role() -> None:
    manifest = yaml.safe_load((ROOT / "azure.yaml").read_text())
    assert manifest["infra"]["provider"] == "bicep"
    assert set(manifest["services"]) == set(hosted_smoke.SERVICES.values())
    for name, service in manifest["services"].items():
        assert service["name"] == name
        assert service["kind"] == "hosted"
        assert service["sessionConfiguration"] == {"idleTimeoutSeconds": 120}
        assert service["project"] == "."
        assert service["docker"] == {"path": "src/backend/Dockerfile", "context": ".", "remoteBuild": True}
        assert service["protocols"] == [{"protocol": "responses", "version": "2.0.0"}]
        assert service["container"]["resources"] == {"cpu": "0.5", "memory": "1Gi"}
        assert service["env"]["ENVIRONMENT"] == "hosted"
        assert service["env"]["POC_AGENT_NAME"] in {"supervisor", "research", "data_analysis"}
        assert not any(key.startswith(("FOUNDRY_", "AGENT_")) or key == "PORT" for key in service["env"])
    docker = (ROOT / "src" / "backend" / "Dockerfile").read_text()
    assert "python:3.14.8-slim" in docker
    assert "uv:0.13.1" in docker
    assert "uv sync --frozen --no-dev" in docker
    assert "COPY pyproject.toml uv.lock" in docker
    assert '"/app/.venv/bin/python", "-m", "app.hosted"' in docker
    assert "COPY . ." not in docker
    ignored = (ROOT / ".dockerignore").read_text()
    assert ignored.splitlines()[0] == "**"
    assert "**/.env" in ignored
    assert "!uv.lock" in ignored
    assert "!.azure" not in ignored
    assert "!artifacts" not in ignored


@pytest.mark.parametrize("name", ["supervisor", "research", "data_analysis"])
def test_shared_entrypoint_dispatches_selected_agent(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(hosted, "get_settings", lambda: Settings(poc_agent_name=name))
    run = MagicMock()
    monkeypatch.setattr(hosted, "run_agent", run)
    hosted.main()
    run.assert_called_once_with(name)


def mock_project(monkeypatch: pytest.MonkeyPatch, module: object) -> MagicMock:
    project = MagicMock()
    constructor = MagicMock()
    constructor.return_value.__enter__.return_value = project
    monkeypatch.setattr(module, "AIProjectClient", constructor)
    monkeypatch.setattr(module, "AzureCliCredential", MagicMock())
    monkeypatch.setattr(module, "native_json", lambda *_args: CONTEXT)
    project.agents.get.side_effect = lambda name: MagicMock(as_dict=lambda: agent_record(name))
    return project


def test_hosted_smoke_uses_agent_bound_endpoints_and_no_automatic_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = mock_project(monkeypatch, hosted_smoke)
    client = project.get_openai_client.return_value.__enter__.return_value
    raw = client.responses.with_raw_response.create.return_value
    raw.http_request = httpx.Request(
        "POST",
        CONTEXT["FOUNDRY_PROJECT_ENDPOINT"]
        + "/agents/supervisor-agent/endpoint/protocols/openai/responses?api-version=v1",
        headers={"Content-Type": "application/json", "Authorization": "Bearer secret-token"},
    )
    raw.status_code = 200
    raw.headers = {"Content-Type": "application/json", "x-request-id": "request-test", "Set-Cookie": "secret-cookie"}
    raw.parse.return_value.model_dump.return_value = {
        "id": "remote-response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "HOSTED-AGENTS-POC-OK"}],
            }
        ],
    }
    assert hosted_smoke.main(["--output-dir", str(tmp_path), "--no-color"]) == 0
    assert client.responses.with_raw_response.create.call_count == 3
    assert [call.kwargs["agent_name"] for call in project.get_openai_client.call_args_list] == [
        "supervisor-agent",
        "research-agent",
        "data-analysis-agent",
    ]
    assert all(call.kwargs["max_retries"] == 0 for call in project.get_openai_client.call_args_list)
    events = [json.loads(line) for line in next(tmp_path.glob("*/results.jsonl")).read_text().splitlines()]
    assert len(events) == 3
    assert all(event["observed"]["deployment"]["version"] == "1" for event in events)
    assert all(event["observed"]["response"]["text"] == "HOSTED-AGENTS-POC-OK" for event in events)
    assert all(event["observed"]["http"]["method"] == "POST" for event in events)
    assert all(event["observed"]["http"]["status_code"] == 200 for event in events)
    serialized = json.dumps(events)
    assert "secret-token" not in serialized
    assert "secret-cookie" not in serialized
    assert events[0]["observed"]["http"]["query_parameters"] == {"api-version": "v1"}
    console = capsys.readouterr().out
    for number, name in enumerate(hosted_smoke.SERVICES.values(), start=1):
        assert f"START {number:02d} | {name} | hosted" in console
        assert f"END {number:02d} | {name} | PASS" in console
    assert "RUN FINISHED | PASS: 3" in console


def test_inactive_agents_are_not_invoked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project = mock_project(monkeypatch, hosted_smoke)
    project.agents.get.side_effect = lambda name: MagicMock(as_dict=lambda: agent_record(name, active=False))
    assert hosted_smoke.main(["--output-dir", str(tmp_path), "--no-color"]) == 1
    project.get_openai_client.assert_not_called()


def test_hosted_http_error_keeps_actual_transport_without_secret_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = mock_project(monkeypatch, hosted_smoke)
    client = project.get_openai_client.return_value.__enter__.return_value
    request = httpx.Request(
        "POST",
        CONTEXT["FOUNDRY_PROJECT_ENDPOINT"]
        + "/agents/supervisor-agent/endpoint/protocols/openai/responses?api-version=v1",
        headers={"Content-Type": "application/json", "Authorization": "Bearer secret-auth"},
    )
    response = httpx.Response(
        429,
        request=request,
        headers={"x-request-id": "request-limited", "retry-after": "5", "set-cookie": "secret-cookie"},
    )
    client.responses.with_raw_response.create.side_effect = APIStatusError("Rate limit", response=response, body=None)
    assert hosted_smoke.main(["--output-dir", str(tmp_path), "--no-color"]) == 1
    events = [json.loads(line) for line in next(tmp_path.glob("*/results.jsonl")).read_text().splitlines()]
    assert all(event["status"] == "FAIL" for event in events)
    assert all(event["observed"]["http"]["status_code"] == 429 for event in events)
    assert all(event["observed"]["http"]["response_headers"]["retry-after"] == "5" for event in events)
    assert "secret-auth" not in json.dumps(events)
    assert "secret-cookie" not in json.dumps(events)


def test_non_latest_endpoint_routing_is_not_misreported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project = mock_project(monkeypatch, hosted_smoke)

    def metadata(name: str) -> MagicMock:
        record = agent_record(name)
        record["agent_endpoint"]["version_selector"]["version_selection_rules"][0]["agent_version"] = "older-version"
        return MagicMock(as_dict=lambda: record)

    project.agents.get.side_effect = metadata
    assert hosted_smoke.main(["--output-dir", str(tmp_path), "--no-color"]) == 1
    project.get_openai_client.assert_not_called()


def test_agent_grants_use_actual_instance_principals_and_preview_is_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mock_project(monkeypatch, grant_agent_rbac)
    calls: list[list[str]] = []

    def az(arguments: list[str]) -> object:
        calls.append(arguments)
        if arguments[:2] == ["account", "show"]:
            return {"tenantId": TENANT}
        if arguments[:3] == ["role", "definition", "list"]:
            return [{"roleName": "Foundry User", "roleType": "BuiltInRole", "name": PRINCIPAL}]
        if arguments[:3] == ["role", "assignment", "list"]:
            assert "--assignee-object-id" in arguments
            assert "--assignee" not in arguments
            assert arguments[arguments.index("--fill-principal-name") + 1] == "false"
            return []
        raise AssertionError(arguments)

    monkeypatch.setattr(grant_agent_rbac, "az", az)
    create = MagicMock()
    monkeypatch.setattr(grant_agent_rbac, "create_grant", create)
    assert grant_agent_rbac.main(["--output-dir", str(tmp_path), "--no-color"]) == 0
    create.assert_not_called()
    monkeypatch.setattr(grant_agent_rbac, "assignment_allowed", lambda *args: True)
    assert grant_agent_rbac.main(["--output-dir", str(tmp_path), "--no-color", "--apply"]) == 0
    assert create.call_count == 3
    for call in create.call_args_list:
        grant, principal, subscription = call.args
        assert principal == PRINCIPAL
        assert subscription == SUBSCRIPTION
        assert grant.scope == ACCOUNT
        assert grant.role == "Foundry User"
        assert call.kwargs["principal_type"] == "ServicePrincipal"


def test_hosted_tasks_are_explicit_and_not_in_ordinary_checks() -> None:
    tasks = {task["label"]: task for task in json.loads((ROOT / ".vscode" / "tasks.json").read_text())["tasks"]}
    compound = tasks["Agents: Deploy and smoke test all hosted agents"]
    assert compound["dependsOrder"] == "sequence"
    assert compound["dependsOn"] == [
        "Agents: Deploy all hosted agents",
        "Access: Apply hosted agent permissions",
        "Agents: Smoke test all hosted agents",
    ]
    assert tasks["Agents: Deploy all hosted agents"]["command"] == "azd"
    assert tasks["Agents: Deploy all hosted agents"]["args"][:2] == ["deploy", "--all"]
    assert "scripts.run_scenario.hosted_smoke" in tasks["Agents: Smoke test all hosted agents"]["args"]
    assert "--apply" in tasks["Access: Apply hosted agent permissions"]["args"]
    assert compound["dependsOn"][0] not in tasks["Quality: Run all local checks"]["dependsOn"]
