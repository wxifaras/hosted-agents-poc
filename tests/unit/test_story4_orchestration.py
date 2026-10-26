"""Story 4 orchestration protocol tests; every remote call is an in-memory fake."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from agent_framework import Content, Message
from app import hosting
from app.core.settings import Settings
from app.services import orchestration
from app.services.orchestration import (
    CORRELATION_HEADER,
    SIMULATED_FAILURE,
    DelegateCommand,
    DelegationReport,
    Delegator,
    HopContext,
    SubTask,
    SubTaskReport,
    delegate_turn,
    delegated_label,
    fingerprint,
    identity_headers,
    subtask_turn,
    tool_calls,
)
from app.services.workflows import WorkflowError
from openai import APIStatusError
from pydantic import ValidationError

CORRELATION = uuid4()


def ambient(monkeypatch: pytest.MonkeyPatch, *, call: str | None = "secret-call", user: str | None = "secret-user"):
    context = SimpleNamespace(call_id=call, user_id=user, session_id="secret-session")
    monkeypatch.setattr(orchestration, "get_request_context", lambda: context)
    monkeypatch.setattr(orchestration.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=True))
    return context


def report_json(agent: str = "research", correlation: Any = CORRELATION, answer: str = "answer TOKEN") -> str:
    hop = HopContext(
        agent=agent,
        hosted_runtime=True,
        call_id_present=True,
        user_id_present=False,
        session_id_present=True,
        user_fingerprint=None,
        correlation_header_matches=True,
    )
    return SubTaskReport(agent=agent, correlation_id=correlation, context=hop, answer=answer).model_dump_json()


def test_fingerprints_are_salted_and_never_contain_the_identity() -> None:
    first = fingerprint("secret-user", "a")
    assert first == fingerprint("secret-user", "a")
    assert first != fingerprint("secret-user", "b")
    assert first is not None and "secret" not in first and len(first) == 16
    assert fingerprint(None, "a") is None and fingerprint("", "a") is None
    assert "secret" not in delegated_label("secret-user")


@pytest.mark.parametrize(
    ("mode", "header"),
    [("agent", None), ("call-context", "x-agent-foundry-call-id"), ("delegated-user", "x-ms-user-identity")],
)
def test_each_identity_mode_sends_exactly_one_forwarding_header(
    monkeypatch: pytest.MonkeyPatch, mode: str, header: str | None
) -> None:
    ambient(monkeypatch)
    headers, label = identity_headers(mode, "corr")  # type: ignore[arg-type]
    forwarding = {"x-agent-foundry-call-id", "x-ms-user-identity", "x-agent-user-id"} & set(headers)
    assert forwarding == ({header} if header else set())
    assert headers[CORRELATION_HEADER] == "corr"
    if mode == "call-context":
        assert headers["x-agent-foundry-call-id"] == "secret-call"
    if mode == "delegated-user":
        assert headers["x-ms-user-identity"] == delegated_label("secret-user")
        assert label == fingerprint(delegated_label("secret-user"), "corr")
    else:
        assert label is None


def test_identity_modes_fail_closed_without_verified_context(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch, call=None, user=None)
    with pytest.raises(WorkflowError, match="call ID"):
        identity_headers("call-context", "corr")
    with pytest.raises(WorkflowError, match="user ID"):
        identity_headers("delegated-user", "corr")
    assert set(identity_headers("agent", "corr")[0]) == {CORRELATION_HEADER}


def test_tool_calls_come_from_tool_messages_with_outcomes() -> None:
    messages = [
        Message("assistant", [Content.from_function_call(call_id="1", name="microsoft_docs_search", arguments={})]),
        Message("tool", [Content.from_function_result(call_id="1", result="ok")]),
        Message("assistant", [Content.from_function_call(call_id="2", name="other", arguments={})]),
        Message("tool", [Content.from_function_result(call_id="2", exception="boom")]),
        Message("assistant", [Content.from_text("I called microsoft_docs_search")]),
    ]
    assert [(call.name, call.succeeded) for call in tool_calls(messages)] == [
        ("microsoft_docs_search", True),
        ("other", False),
    ]


def test_commands_forbid_identity_fields_and_validate_target_shape() -> None:
    with pytest.raises(ValidationError):
        SubTask.model_validate({"task": "t", "correlation_id": str(CORRELATION), "user_id": "spoof"})
    with pytest.raises(ValidationError):
        DelegateCommand(task="t", correlation_id=CORRELATION, target="Bad Target!")
    command = DelegateCommand(task="t", correlation_id=CORRELATION)
    assert command.target == "auto" and command.identity == "call-context" and command.invocation == "sync"
    assert command.wire().startswith("POC_DELEGATE ")


async def test_simulated_subtask_failure_happens_before_any_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    agent = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("no model call")))
    context = SimpleNamespace(client_headers={})
    with pytest.raises(WorkflowError, match=SIMULATED_FAILURE):
        await subtask_turn(
            "research", agent, SubTask(task="t", correlation_id=CORRELATION, simulate_failure=True), context
        )
    with pytest.raises(WorkflowError, match="Only Research"):
        await subtask_turn("supervisor", agent, SubTask(task="t", correlation_id=CORRELATION), context)
    agent.run.assert_not_awaited()


async def test_subtask_reports_context_fingerprints_tools_and_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    messages = [
        Message("assistant", [Content.from_function_call(call_id="c", name="code_interpreter", arguments={})]),
        Message("tool", [Content.from_function_result(call_id="c", result='{"container_id": "cntr_ABCDEFGH12"}')]),
    ]
    agent = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="done", messages=messages)))
    context = SimpleNamespace(client_headers={CORRELATION_HEADER: str(CORRELATION)})
    text = await subtask_turn("data_analysis", agent, SubTask(task="t", correlation_id=CORRELATION), context)
    report = SubTaskReport.model_validate_json(text)
    assert report.agent == "data_analysis" and report.correlation_id == CORRELATION
    assert report.context.correlation_header_matches and report.context.user_id_present
    assert report.context.user_fingerprint == fingerprint("secret-user", str(CORRELATION))
    assert [call.name for call in report.tool_calls] == ["code_interpreter"]
    assert report.containers == ["cntr_ABCDEFGH12"]
    assert "secret" not in text


async def test_unlisted_target_is_denied_before_any_client_is_created(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    monkeypatch.setattr(orchestration, "DefaultAzureCredential", MagicMock(side_effect=AssertionError("no egress")))
    agent = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("no model call")))
    command = DelegateCommand(task="t", correlation_id=CORRELATION, target="billing-agent")
    report = DelegationReport.model_validate_json(
        await delegate_turn("supervisor", Settings(), agent, command, SimpleNamespace(client_headers={}))
    )
    assert report.status == "denied" and report.routing.decided_by == "supervisor-allow-list"
    assert report.invocation is None and report.sub_agent is None
    with pytest.raises(WorkflowError, match="Only the Supervisor"):
        await delegate_turn("research", Settings(), agent, command, SimpleNamespace(client_headers={}))


class FakeResponses:
    def __init__(self, created: Any, retrieved: list[Any] | None = None, error: Exception | None = None) -> None:
        self.created = created
        self.retrieved = list(retrieved or [])
        self.error = error
        self.create_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        if self.error:
            raise self.error
        return self.created

    async def retrieve(self, _response_id: str) -> Any:
        return self.retrieved.pop(0)


class FakeClient:
    def __init__(self, responses: FakeResponses) -> None:
        self.responses = responses

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def response(status: str, text: str = "", error: Any = None) -> SimpleNamespace:
    return SimpleNamespace(id="resp_1", status=status, output_text=text, error=error)


def delegator(command: DelegateCommand, responses: FakeResponses) -> Delegator:
    return Delegator(command, lambda _target: FakeClient(responses), ["x-client-correlation-id"], timeout=30)  # type: ignore[arg-type,return-value]


async def test_sync_delegation_success_and_failure_are_reported_without_retry() -> None:
    command = DelegateCommand(task="t", correlation_id=CORRELATION, target="research")
    ok = delegator(command, FakeResponses(response("completed", report_json())))
    await ok.invoke("research", "t")
    assert ok.sub_agent is not None and ok.failure is None
    assert ok.invocation is not None and ok.invocation.status == "completed" and ok.invocation.mode == "sync"
    failed_error = SimpleNamespace(code="server_error", message=SIMULATED_FAILURE)
    responses = FakeResponses(response("failed", error=failed_error))
    failed = delegator(command, responses)
    await failed.invoke("research", "t")
    assert failed.failure is not None and failed.failure.stage == "status" and failed.failure.code == "server_error"
    assert failed.failure.retried is False and failed.sub_agent is None
    assert "background" not in responses.create_kwargs


async def test_background_delegation_polls_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestration.asyncio, "sleep", AsyncMock())
    command = DelegateCommand(task="t", correlation_id=CORRELATION, target="research", invocation="background")
    responses = FakeResponses(
        response("queued"), [response("in_progress"), response("in_progress"), response("completed", report_json())]
    )
    flow = delegator(command, responses)
    await flow.invoke("research", "t")
    assert responses.create_kwargs["background"] is True and responses.create_kwargs["store"] is True
    assert flow.invocation is not None
    assert flow.invocation.statuses_seen == ["queued", "in_progress", "completed"]
    assert flow.invocation.polls == 3 and flow.sub_agent is not None


async def test_http_rejection_and_mismatched_reports_become_failures() -> None:
    request = httpx.Request("POST", "https://example.invalid/responses")
    denied = APIStatusError(
        "forbidden",
        response=httpx.Response(403, request=request),
        body={"error": {"code": "PermissionDenied", "message": "no"}},
    )
    command = DelegateCommand(task="t", correlation_id=CORRELATION, target="research", identity="delegated-user")
    rejected = delegator(command, FakeResponses(None, error=denied))
    await rejected.invoke("research", "t")
    assert rejected.failure is not None and rejected.failure.http_status == 403
    assert rejected.failure.code == "PermissionDenied" and rejected.invocation is not None
    wrong = delegator(command, FakeResponses(response("completed", report_json(agent="data_analysis"))))
    await wrong.invoke("research", "t")
    assert wrong.failure is not None and wrong.failure.stage == "report"


async def test_host_routes_story4_commands_and_rejects_client_background(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    server = SimpleNamespace()

    def register(handler: Any) -> Any:
        server.handler = handler
        return handler

    server.response_handler = register
    monkeypatch.setattr(hosting, "ResponsesAgentServerHost", lambda **_kwargs: server)
    agent = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("no model call")))

    async def send(name: str, text: str, request: dict[str, Any]) -> dict[str, Any]:
        hosting.build_host(name, Settings(), agent, lambda **_kwargs: None)
        context = SimpleNamespace(
            response_id="response",
            conversation_id=None,
            client_headers={},
            is_recovery=False,
            get_input_text=AsyncMock(return_value=text),
        )
        stream = await server.handler(request, context, asyncio.Event())
        return [event async for event in stream][-1]["response"]

    failure = SubTask(task="t", correlation_id=CORRELATION, simulate_failure=True).wire()
    result = await send("research", failure, {"background": True})
    assert result["status"] == "failed" and result["error"]["code"] == "server_error"
    assert SIMULATED_FAILURE in result["error"]["message"]
    probe = DelegateCommand(task="t", correlation_id=CORRELATION, target="unlisted").wire()
    result = await send("supervisor", probe, {})
    report = json.loads(result["output"][0]["content"][0]["text"])
    assert report["status"] == "denied"
    result = await send("supervisor", probe, {"background": True})
    assert result["status"] == "failed" and "background is not enabled" in result["error"]["message"]
    agent.run.assert_not_awaited()
