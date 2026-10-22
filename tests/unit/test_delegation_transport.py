"""DLG-01: inspect real SDK requests at an in-memory transport, never Foundry."""

import json
from time import time
from typing import Literal
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from app.core.redaction import sanitize
from app.services.delegation import COMMAND_PREFIX, Command, Report, SpecialistClient
from app.services.foundry_state import Caller
from app.services.workflows import Child
from azure.ai.projects.aio import AIProjectClient
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from openai import AsyncOpenAI

from scripts.common.http_details import http_details


@pytest.mark.parametrize("specialist", ["research", "data_analysis"])
async def test_actual_specialist_requests_preserve_identity_and_child_scope(
    specialist: Literal["research", "data_analysis"],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exercise conversation/create/continue through real SDK serialization and auth."""
    endpoint = "https://transport-test.services.ai.azure.com/api/projects/poc"
    caller = Caller("opaque-transport-call", "verified-transport-user", "supervisor-session-do-not-forward")
    bearer = "synthetic-credential-never-log"
    conversation_id = f"conv-{specialist}-transport-test"
    child_platform_session = f"{specialist}-platform-session"
    workflow_id, owner_id, session_id = uuid4(), uuid4(), uuid4()
    service = {"research": "research-agent", "data_analysis": "data-analysis-agent"}[specialist]
    base_url = f"{endpoint}/agents/{service}/endpoint/protocols/openai"
    credential = AsyncMock(spec=AsyncTokenCredential)
    credential.get_token.return_value = AccessToken(bearer, int(time()) + 3600)
    captures: list[tuple[str, dict[str, bool]]] = []
    safe_http: list[dict[str, object]] = []
    client_checks: list[bool] = []

    monkeypatch.setenv("AZURE_AI_PROJECTS_CONSOLE_LOGGING", "false")
    monkeypatch.setattr(
        httpx2.AsyncHTTPTransport,
        "handle_async_request",
        AsyncMock(side_effect=AssertionError("A real network transport must never run in DLG-01.")),
    )

    def capture(request: httpx2.Request) -> httpx2.Response:
        stage = ("conversation", "create", "continue")[len(captures)]
        body = json.loads(request.content)
        expected_url = base_url + ("/conversations" if stage == "conversation" else "/responses")
        headers = request.headers
        checks = {
            "method": request.method == "POST",
            "target": str(request.url).split("?")[0] == expected_url,
            "api_version": dict(request.url.params) == {"api-version": "v1"},
            "content_type": headers.get("content-type") == "application/json",
            "credential": headers.get("authorization") == f"Bearer {bearer}",
            "original_call_context": headers.get("x-agent-foundry-call-id") == caller.call_id,
            "delegation_label": headers.get("x-ms-user-identity") == caller.user_id,
            "expected_caller": headers.get("x-client-expected-caller") == caller.user_id,
            "no_forged_platform_user": "x-agent-user-id" not in headers,
            "no_supervisor_session_in_body": caller.platform_session_id not in request.content.decode(),
            "no_identity_values_in_body": all(
                value not in request.content.decode() for value in (caller.call_id, caller.user_id, bearer)
            ),
            "no_identity_query": caller.call_id not in str(request.url) and caller.user_id not in str(request.url),
        }
        if stage == "conversation":
            checks["empty_conversation_body"] = body == {}
            payload: dict[str, object] = {
                "id": conversation_id,
                "object": "conversation",
                "created_at": 1,
                "metadata": {},
            }
        else:
            command = Command.model_validate_json(body["input"][len(COMMAND_PREFIX) :])
            checks.update(
                {
                    "command_prefix": body["input"].startswith(COMMAND_PREFIX),
                    "command_operation": command.operation == stage,
                    "workflow_binding": command.workflow_id == workflow_id,
                    "message": command.message == ("Remember ALPHA." if stage == "create" else "Recall the token."),
                    "conversation_binding": body.get("conversation") == conversation_id,
                    "not_streaming": body.get("stream") is False,
                    "no_previous_response_chain": "previous_response_id" not in body,
                    "session_binding": (
                        "agent_session_id" not in body
                        if stage == "create"
                        else body.get("agent_session_id") == child_platform_session
                    ),
                }
            )
            report = Report(
                agent=specialist,
                workflow_id=workflow_id,
                owner_id=owner_id,
                session_id=session_id,
                conversation_id=conversation_id,
                platform_session_id=child_platform_session,
                progress="ready",
                caller_propagation_verified=True,
                answer="READY" if stage == "create" else "ALPHA",
                children={},
            )
            payload = {
                "id": f"resp-{specialist}-{stage}",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "id": f"msg-{stage}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": report.model_dump_json(), "annotations": []}],
                    }
                ],
            }
        captures.append((stage, checks))
        safe_http.append(http_details(request.method, str(request.url), headers, status_code=200, response_headers={}))
        return httpx2.Response(200, json=payload)

    async with AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project:
        real_factory = project.get_openai_client

        def offline_factory(*, agent_name: str | None = None, **kwargs: object) -> AsyncOpenAI:
            client_checks.append(
                agent_name == service and kwargs.get("max_retries") == 0 and kwargs.get("timeout") == 180
            )
            return real_factory(
                agent_name=agent_name,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture), trust_env=False),
                **kwargs,
            )

        monkeypatch.setattr(project, "get_openai_client", offline_factory)
        delegate = SpecialistClient(project, caller, 180)
        conversation = await delegate.create_conversation(specialist)
        child = Child(agent=specialist, workflow_id=workflow_id, conversation_id=conversation)
        created, first_answer = await delegate.invoke(child, "Remember ALPHA.", create=True)
        continued, next_answer = await delegate.invoke(created, "Recall the token.", create=False)

    assert [stage for stage, _checks in captures] == ["conversation", "create", "continue"]
    for stage, checks in captures:
        assert all(checks.values()), {stage: checks}
    assert client_checks == [True, True, True], "Unexpected target, retry, or timeout configuration."
    bindings = {
        "first_answer": first_answer == "READY",
        "followup_answer": next_answer == "ALPHA",
        "conversation_reused": created.conversation_id == continued.conversation_id == conversation_id,
        "platform_session_reused": created.platform_session_id
        == continued.platform_session_id
        == child_platform_session,
        "owner_reused": created.owner_id == continued.owner_id == owner_id,
        "application_session_reused": created.session_id == continued.session_id == session_id,
        "new_response": created.response_id != continued.response_id == f"resp-{specialist}-continue",
    }
    assert all(bindings.values()), bindings
    credential.get_token.assert_awaited()
    assert all(call.args == ("https://ai.azure.com/.default",) for call in credential.get_token.await_args_list)
    output = capsys.readouterr()
    diagnostics = json.dumps(sanitize(safe_http)) + output.out + output.err + caplog.text
    assert {
        "no_call_value": caller.call_id not in diagnostics,
        "no_user_value": caller.user_id not in diagnostics,
        "no_bearer_value": bearer not in diagnostics,
    } == {"no_call_value": True, "no_user_value": True, "no_bearer_value": True}
