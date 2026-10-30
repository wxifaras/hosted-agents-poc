"""Dedicated host wiring/readiness; no Azure authentication or model inference."""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from app import story6_host as host
from app.services.foundry_state import Caller
from app.services.story6_approval import STORE_NAME, Command, PendingApproval, Reply
from app.services.workflows import WorkflowError


def reply() -> Reply:
    return Reply(
        operation_id=uuid4(),
        phase="pending",
        pending=PendingApproval(reference="1" * 64),
        decision=None,
        answer="",
        research_response_observed=True,
        research_user_context_present=False,
        provider_responses_observed=1,
        mcp_calls_reported=0,
        file_sha256="2" * 64,
        file_retained=True,
        same_process=True,
    )


async def test_handler_preserves_structured_pending_json_and_does_not_change_other_protocols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = SimpleNamespace()

    def register(handler):
        server.handler = handler
        return handler

    server.response_handler = register
    monkeypatch.setattr(host, "ResponsesAgentServerHost", lambda **kwargs: server)
    observed = reply()
    execute = AsyncMock(return_value=observed)
    monkeypatch.setattr(host, "execute", execute)
    text = Command(operation="start", operation_id=observed.operation_id).wire()
    context = SimpleNamespace(
        response_id="outer-response",
        conversation_id="outer-conversation",
        is_recovery=False,
        get_input_text=AsyncMock(return_value=text),
    )

    async def send(settings: host.Story6Settings, request: dict, *, recovery: bool = False):
        host.build_host(settings)
        context.is_recovery = recovery
        stream = await server.handler(request, context, asyncio.Event())
        return [event async for event in stream][-1]["response"]

    response = await send(host.Story6Settings(story6_enabled=True), {})
    payload = json.loads(response["output"][0]["content"][0]["text"])
    assert payload["protocol"] == "poc-story6-approval-v1"
    assert payload["phase"] == "pending" and payload["pending"]["tool_name"] == "microsoft_docs_search"
    assert payload["pending"]["reference"] == "1" * 64
    assert execute.await_count == 1
    for settings, request, recovery in (
        (host.Story6Settings(), {}, False),
        (host.Story6Settings(story6_enabled=True), {"background": True}, False),
        (host.Story6Settings(story6_enabled=True), {}, True),
    ):
        response = await send(settings, request, recovery=recovery)
        assert response["status"] == "failed" and response["error"]["code"] == "story6_request_failed"
    context.get_input_text.return_value = "POC_CONVERSE {}"
    assert (await send(host.Story6Settings(story6_enabled=True), {}))["status"] == "failed"
    assert execute.await_count == 1


async def test_missing_verified_context_fails_before_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = MagicMock(side_effect=AssertionError("Credentials must not be constructed."))
    monkeypatch.setattr(host, "DefaultAzureCredential", forbidden)
    from app.services import foundry_state

    monkeypatch.setattr(foundry_state, "get_request_context", lambda: None)
    with pytest.raises(WorkflowError, match="verified Foundry hosted"):
        await host.execute(
            Command(operation="inspect", operation_id=uuid4()),
            SimpleNamespace(conversation_id="conversation"),
            host.Story6Settings(),
        )
    forbidden.assert_not_called()


@pytest.mark.parametrize("create", [False, True])
async def test_state_namespace_is_separate_and_continuation_does_not_create(
    monkeypatch: pytest.MonkeyPatch, create: bool
) -> None:
    store = MagicMock()
    store.get = AsyncMock(return_value=SimpleNamespace(user_isolation=True, item_ttl_seconds=-1))
    store.aclose = AsyncMock()
    factory = MagicMock(return_value=store)
    factory.get_or_create = AsyncMock(return_value=store)
    monkeypatch.setattr(host, "FoundryStateStore", factory)
    caller = Caller("private-call", "private-owner", "private-session")
    async with host.open_approval_store(MagicMock(), caller, create=create) as adapter:
        assert adapter.caller == caller
    if create:
        assert factory.get_or_create.await_args.args == (STORE_NAME,)
        assert factory.get_or_create.await_args.kwargs["user_isolation"] is True
    else:
        assert factory.call_args.args == (STORE_NAME,)
        factory.get_or_create.assert_not_awaited()
    store.aclose.assert_awaited_once()


async def test_nonisolated_store_is_rejected_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MagicMock()
    store.get = AsyncMock(return_value=SimpleNamespace(user_isolation=False, item_ttl_seconds=-1))
    store.aclose = AsyncMock()
    monkeypatch.setattr(host, "FoundryStateStore", MagicMock(return_value=store))
    with pytest.raises(WorkflowError, match="user-isolated"):
        async with host.open_approval_store(MagicMock(), Caller("call", "user", "session"), create=False):
            pytest.fail("An incompatible store must not be used.")
    store.aclose.assert_awaited_once()


@pytest.mark.parametrize("operation", ["start", "inspect", "decide"])
async def test_dispatch_uses_agent_bound_research_and_nonretrying_native_provider(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    observed = reply()
    caller = Caller("call", "user", "session")
    monkeypatch.setattr(Caller, "current", lambda: caller)
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    monkeypatch.setattr(host, "DefaultAzureCredential", MagicMock(return_value=credential))
    store = MagicMock()
    store.read = AsyncMock(return_value=None)

    @asynccontextmanager
    async def opened(received_credential, received_caller, *, create):
        assert received_credential is credential and received_caller is caller
        assert create is (operation == "start")
        yield store

    monkeypatch.setattr(host, "open_approval_store", opened)
    coordinator = MagicMock()
    coordinator.start = AsyncMock(return_value=observed)
    coordinator.inspect = AsyncMock(return_value=observed)
    coordinator.decide = AsyncMock(return_value=observed)
    coordinator.open = AsyncMock()
    monkeypatch.setattr(host, "Coordinator", MagicMock(return_value=coordinator))
    project = MagicMock()
    project.__aenter__.return_value = project
    constructor = MagicMock(return_value=project)
    monkeypatch.setattr(host, "AIProjectClient", constructor)
    fields: dict[str, object] = {"operation": operation, "operation_id": observed.operation_id}
    if operation == "decide":
        fields.update(approval_ref="1" * 64, approve=True)
    settings = host.Story6Settings(
        foundry_project_endpoint="https://synthetic.invalid/api/projects/poc",
        azure_ai_model_deployment_name="synthetic",
        story6_enabled=True,
    )
    assert (
        await host.execute(Command.model_validate(fields), SimpleNamespace(conversation_id="conversation"), settings)
        == observed
    )
    calls = project.get_openai_client.call_args_list
    if operation == "inspect":
        constructor.assert_not_called()
        assert not calls
    else:
        assert all(call.kwargs["max_retries"] == 0 for call in calls)
        assert "agent_name" not in calls[0].kwargs
        assert len(calls) == (2 if operation == "start" else 1)
        if operation == "start":
            assert calls[1].kwargs["agent_name"] == "research-agent"


def test_story6_debug_is_separate_and_root_deploy_all_still_has_three_agents() -> None:
    import yaml

    root = Path(__file__).resolve().parents[2]
    launch = json.loads((root / ".vscode/launch.json").read_text())
    config = next(item for item in launch["configurations"] if item["name"].startswith("Story6 coordinator"))
    assert config["request"] == "attach" and config["connect"]["port"] == 5680
    tasks = json.loads((root / ".vscode/tasks.json").read_text())
    task = next(item for item in tasks["tasks"] if item["label"] == "Debug: Start Story6 coordinator for Inspector")
    assert task["command"] == "${workspaceFolder}\\.venv\\Scripts\\python.exe"
    assert task["args"][-2:] == ["-m", "app.agents.story6.main"]
    assert task["options"]["env"]["PORT"] == "8091"
    quality = next(item for item in tasks["tasks"] if item["label"] == "Quality: Check Story6 coordinator")
    assert quality["args"][-2:] == ["tests\\unit\\test_story6_coordinator.py", "tests\\unit\\test_story6_host.py"]
    assert "dependsOn" not in quality
    manifest = yaml.safe_load((root / "azure.yaml").read_text())
    assert set(manifest["services"]) == {"supervisor-agent", "research-agent", "data-analysis-agent"}


def test_actual_dedicated_host_readiness_without_credentials(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "FOUNDRY_PROJECT_ENDPOINT": "https://synthetic.invalid/api/projects/poc",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "not-invoked",
        "PORT": str(port),
        "ENVIRONMENT": "development",
        "STORY6_ENABLED": "false",
        "POC_ENV_FILE": str(tmp_path / "unused.env"),
        "APP_CONFIG_ENDPOINT": "",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
        "POC_APPLICATIONINSIGHTS_CONNECTION_STRING": "",
    }
    log = tmp_path / "story6-host.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.agents.story6.main"],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 40
            with httpx.Client(trust_env=False, timeout=1) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        pytest.fail(log.read_text(encoding="utf-8"))
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/readiness")
                    except httpx.TransportError:
                        time.sleep(0.2)
                    else:
                        assert response.status_code == 200
                        assert re.search(rf"Running on http://127\.0\.0\.1:{port}(?:\s|$)", log.read_text())
                        return
            pytest.fail(log.read_text(encoding="utf-8"))
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.parametrize("operation", ["start", "decide"])
async def test_optional_toolbox_auth_is_request_scoped_and_only_used_for_the_proxy(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    observed = reply()
    caller = Caller("verified-current-call", "user", "session")
    monkeypatch.setattr(Caller, "current", lambda: caller)
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    credential.get_token = AsyncMock(return_value=SimpleNamespace(token="fresh-private-token"))
    monkeypatch.setattr(host, "DefaultAzureCredential", MagicMock(return_value=credential))
    store = MagicMock()
    store.read = AsyncMock(return_value=None)

    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield store

    monkeypatch.setattr(host, "open_approval_store", opened)
    coordinator = MagicMock()
    coordinator.open = AsyncMock(
        return_value=SimpleNamespace(record=SimpleNamespace(mcp_route="foundry-toolbox", toolbox_version="1"))
    )
    coordinator.start = AsyncMock(return_value=observed)
    coordinator.decide = AsyncMock(return_value=observed)
    constructor = MagicMock(return_value=coordinator)
    monkeypatch.setattr(host, "Coordinator", constructor)
    project = MagicMock()
    project.__aenter__.return_value = project
    monkeypatch.setattr(host, "AIProjectClient", MagicMock(return_value=project))
    settings = host.Story6Settings(
        foundry_project_endpoint="https://synthetic.services.ai.azure.com/api/projects/poc",
        azure_ai_model_deployment_name="synthetic",
        story6_enabled=True,
        story6_toolbox_version="1" if operation == "start" else "2",
    )
    fields: dict[str, object] = {"operation": operation, "operation_id": observed.operation_id}
    if operation == "decide":
        fields.update(approval_ref="1" * 64, approve=True)
    assert (
        await host.execute(Command.model_validate(fields), SimpleNamespace(conversation_id="conversation"), settings)
        == observed
    )
    credential.get_token.assert_awaited_once_with("https://ai.azure.com/.default")
    proxy = constructor.call_args.kwargs["proxy"]
    assert proxy.version == "1"
    assert proxy.headers == {
        "Authorization": "Bearer fresh-private-token",
        "x-agent-foundry-call-id": "verified-current-call",
    }
    assert "story6-approval-mcp/versions/1/mcp?api-version=v1" in proxy.endpoint()
    assert "fresh-private-token" not in repr(proxy)


def test_toolbox_configuration_remains_unset_by_default_and_rejects_nonversions() -> None:
    from pydantic import ValidationError

    assert host.Story6Settings().story6_toolbox_version is None
    assert host.Story6Settings(story6_toolbox_version="").story6_toolbox_version is None
    for version in ("0", "../1", "latest", "1" * 11):
        with pytest.raises(ValidationError):
            host.Story6Settings(story6_toolbox_version=version)
