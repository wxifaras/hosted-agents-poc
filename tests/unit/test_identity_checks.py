"""Diagnostic experiment regressions; fakes never count as hosted identity evidence."""

import json
from time import time
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import httpx2
import pytest
from app.core.settings import Settings
from app.services import identity_checks as checks
from app.services.foundry_state import Caller
from app.services.workflows import WorkflowError
from azure.ai.projects.aio import AIProjectClient
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from openai import APIStatusError, AsyncOpenAI
from pydantic import ValidationError


def hosted_context(
    monkeypatch: pytest.MonkeyPatch, *, call: str | None = "opaque-call", user: str | None = None
) -> None:
    monkeypatch.setattr(checks.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=True))
    monkeypatch.setattr(
        checks, "get_request_context", lambda: SimpleNamespace(call_id=call, session_id="session", user_id=user)
    )
    monkeypatch.setattr(
        checks.ContextPresence,
        "inspect",
        lambda _conversation: checks.ContextPresence(
            hosted_runtime=True,
            call_id_present=bool(call),
            user_id_present=bool(user),
            session_id_present=True,
            conversation_id_present=True,
        ),
    )


def test_experiments_default_off_and_require_platform_call_context(monkeypatch: pytest.MonkeyPatch) -> None:
    hosted_context(monkeypatch)
    with pytest.raises(WorkflowError, match="disabled"):
        checks.require_diagnostic_context(Settings())
    hosted_context(monkeypatch, call=None)
    with pytest.raises(WorkflowError, match="call and session"):
        checks.require_diagnostic_context(Settings(poc_enable_identity_tests=True))


def test_client_variants_do_not_mix_identity_mechanisms() -> None:
    project = SimpleNamespace(get_openai_client=MagicMock())
    caller = Caller("original-call", "verified-root-user", "root-session")
    runner = checks.IdentityRunner(project, caller, uuid4())
    runner.client("call-context")
    call_headers = project.get_openai_client.call_args.kwargs["default_headers"]
    assert call_headers == {
        "x-agent-foundry-call-id": caller.call_id,
        "x-client-expected-caller": caller.user_id,
    }
    runner.client("delegated-user")
    user_a = project.get_openai_client.call_args.kwargs["default_headers"]
    runner.client("delegated-user", "b")
    user_b = project.get_openai_client.call_args.kwargs["default_headers"]
    assert "x-agent-foundry-call-id" not in user_a and "x-agent-user-id" not in user_a
    assert user_a["x-ms-user-identity"] != user_b["x-ms-user-identity"]
    assert user_a["x-ms-user-identity"] != caller.user_id
    assert user_a["x-client-expected-caller"] == user_a["x-ms-user-identity"]
    assert user_b["x-client-expected-caller"] == user_b["x-ms-user-identity"]
    assert project.get_openai_client.call_args.kwargs["max_retries"] == 0
    with pytest.raises(WorkflowError, match="second genuine"):
        runner.client("call-context", "b")


def test_operator_control_has_no_invented_platform_call_or_session() -> None:
    project = SimpleNamespace(get_openai_client=MagicMock())
    runner = checks.IdentityRunner(project, None, uuid4())
    runner.client("delegated-user")
    headers = project.get_openai_client.call_args.kwargs["default_headers"]
    assert set(headers) == {"x-ms-user-identity", "x-client-expected-caller"}
    assert headers["x-ms-user-identity"] == runner.labels["a"]
    with pytest.raises(WorkflowError, match="genuine"):
        runner.client("call-context")


@pytest.mark.parametrize(
    ("user", "matches"),
    [("synthetic-delegated-a", True), ("verified-root-user", False), (None, None)],
)
async def test_delegated_label_comparison_is_presence_only_and_never_exposes_identities(
    user: str | None, matches: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted_context(monkeypatch, user=user)
    result = await checks.research_check(
        Settings(poc_enable_identity_tests=True),
        checks.IdentityCommand(operation="presence", run_id=uuid4()),
        SimpleNamespace(conversation_id=None, client_headers={"x-client-expected-caller": "synthetic-delegated-a"}),
    )
    assert result.user_matches_expected is matches
    assert result.presence.user_id_present is bool(user)
    assert result.marker_found is None
    assert "synthetic-delegated-a" not in result.model_dump_json()
    assert "verified-root-user" not in result.model_dump_json()


@pytest.mark.parametrize("operation", ["state-create", "state-read"])
async def test_dedicated_state_uses_original_call_and_isolated_expiring_store(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted_context(monkeypatch)
    store = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(user_isolation=True, item_ttl_seconds=3600)),
        create_item=AsyncMock(),
        get_item=AsyncMock(return_value=SimpleNamespace(value={"marker": "synthetic-marker"})),
        aclose=AsyncMock(),
    )
    factory = MagicMock(return_value=store)
    factory.get_or_create = AsyncMock(return_value=store)
    monkeypatch.setattr(checks, "FoundryStateStore", factory)
    run_id = uuid4()
    result = await checks.research_check(
        Settings(poc_enable_identity_tests=True),
        checks.IdentityCommand.model_validate({"operation": operation, "run_id": run_id}),
        SimpleNamespace(conversation_id="conv", client_headers={}),
    )
    assert result.marker_found is True and result.marker_fingerprint
    assert not result.presence.user_id_present
    assert "synthetic-marker" not in result.model_dump_json()
    store.get_item.assert_awaited_once_with(f"probe/{run_id}", call_id="opaque-call")
    if operation == "state-create":
        assert factory.get_or_create.call_args.kwargs["user_isolation"] is True
        assert factory.get_or_create.call_args.kwargs["item_ttl_seconds"] == 3600
        assert store.create_item.call_args.kwargs["call_id"] == "opaque-call"
    else:
        factory.get_or_create.assert_not_awaited()
        store.create_item.assert_not_awaited()
    store.aclose.assert_awaited_once()


async def test_state_read_missing_marker_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    hosted_context(monkeypatch)
    store = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(user_isolation=True, item_ttl_seconds=3600)),
        get_item=AsyncMock(return_value=None),
        create_item=AsyncMock(),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(checks, "FoundryStateStore", MagicMock(return_value=store))
    result = await checks.research_check(
        Settings(poc_enable_identity_tests=True),
        checks.IdentityCommand(operation="state-read", run_id=uuid4()),
        SimpleNamespace(conversation_id="conv", client_headers={}),
    )
    assert result.marker_found is False and result.marker_fingerprint is None
    store.create_item.assert_not_awaited()


@pytest.mark.parametrize("found", [False, True])
async def test_history_reads_authorized_assistant_items_without_model_fallback(
    found: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted_context(monkeypatch)
    run_id = uuid4()
    marker = f"POC-IDENTITY-HISTORY-{run_id}"
    history = [
        {
            "type": "message",
            "role": "assistant" if found else "user",
            "content": [{"type": "output_text", "text": marker}],
        }
    ]
    context = SimpleNamespace(conversation_id=None, client_headers={}, get_history=AsyncMock(return_value=history))
    result = await checks.research_check(
        Settings(poc_enable_identity_tests=True),
        checks.IdentityCommand(operation="history-read", run_id=run_id),
        context,
    )
    assert result.history_contains_marker is found
    context.get_history.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "code", "passed"),
    [
        (403, "session_not_accessible", True),
        (404, "response_not_found", True),
        (500, "response_not_found", False),
        (403, "PermissionDenied", False),
        (404, "not_found", False),
    ],
)
def test_only_specific_visibility_rejections_are_negative_passes(status: int, code: str, passed: bool) -> None:
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.invalid/responses"))
    error = APIStatusError("synthetic", response=response, body={"error": {"code": code}})
    assert checks.is_visibility_denial(error) is passed


def test_masked_not_found_requires_positive_owner_readback_and_correct_endpoint() -> None:
    response = httpx.Response(404, request=httpx.Request("POST", "https://example.invalid/responses"))
    error = APIStatusError("synthetic", response=response, body={"error": {"code": "not_found"}})
    assert not checks.is_visibility_denial(error)
    assert checks.is_visibility_denial(error, owner_resource_confirmed=True)
    response = httpx.Response(404, request=httpx.Request("POST", "https://example.invalid/incorrect-route"))
    error = APIStatusError("synthetic", response=response, body={"error": {"code": "not_found"}})
    assert not checks.is_visibility_denial(error, owner_resource_confirmed=True)


def test_client_cannot_supply_user_or_store_overrides() -> None:
    for field in ("user_id", "call_id", "store_name"):
        with pytest.raises(ValidationError):
            checks.IdentityCommand.model_validate(
                {
                    "operation": "presence",
                    "run_id": str(uuid4()),
                    field: "untrusted",
                }
            )


@pytest.mark.parametrize("use_previous", [False, True])
@pytest.mark.parametrize("bind_session", [False, True])
async def test_real_async_sdk_parses_report_and_omits_unselected_history_binding(
    use_previous: bool, bind_session: bool
) -> None:
    observed: list[dict[str, bool]] = []
    report = checks.IdentityReply(
        presence=checks.ContextPresence(
            hosted_runtime=True,
            call_id_present=True,
            user_id_present=False,
            session_id_present=True,
            conversation_id_present=not use_previous,
        ),
        expected_header_present=True,
        user_matches_expected=None,
    )

    def capture(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        observed.append(
            {
                "previous": body.get("previous_response_id") == "resp-a"
                if use_previous
                else "previous_response_id" not in body,
                "conversation": "conversation" not in body if use_previous else body.get("conversation") == "conv-a",
                "session": body.get("agent_session_id") == "session-a"
                if bind_session
                else "agent_session_id" not in body,
                "stored": body.get("store") is True,
            }
        )
        return httpx2.Response(
            200,
            headers={"x-agent-session-id": "session-a"},
            json={
                "id": "resp-b",
                "object": "response",
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "id": "msg-b",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": report.model_dump_json(), "annotations": []}],
                    }
                ],
            },
        )

    async with AsyncOpenAI(
        api_key="synthetic-test-only",
        base_url="https://example.invalid/openai",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture), trust_env=False),
        max_retries=0,
    ) as client:
        runner = checks.IdentityRunner(
            MagicMock(),
            Caller("call", "user", "root-session"),
            uuid4(),
            expected_endpoint="https://example.invalid/openai",
            expected_principal="expected-principal",
            expected_client="expected-client",
        )
        actual, response_id, session = await runner.send(
            client,
            "presence",
            conversation=None if use_previous else "conv-a",
            previous="resp-a" if use_previous else None,
            session="session-a" if bind_session else None,
        )
    assert actual == report and response_id == "resp-b" and session == "session-a"
    assert observed == [{"previous": True, "conversation": True, "session": True, "stored": True}]
    assert len(runner.egress) == 1
    assert runner.egress[0]["target_matches_expected_research_endpoint"] is True
    assert runner.egress[0]["credential_claims_readable"] is False
    assert runner.egress[0]["credential_principal_matches_supervisor_instance"] is None
    assert runner.egress[0]["previous_response_binding_matches_request"] is True
    assert runner.egress[0]["session_binding_matches_request"] is True


@pytest.mark.parametrize("field", ["previous_response_id", "agent_session_id"])
def test_egress_detects_serialized_binding_mismatch_without_returning_identifiers(field: str) -> None:
    runner = checks.IdentityRunner(
        MagicMock(),
        None,
        uuid4(),
        expected_endpoint="https://example.invalid/openai",
        expected_principal="synthetic-principal",
        expected_client="synthetic-client",
    )
    client = MagicMock()
    client.default_headers = {}
    body = {"previous_response_id": "owned-response", "agent_session_id": "owned-session"}
    body[field] = "unexpected-binding"
    runner.inspect_egress(
        client,
        "https://example.invalid/openai/responses",
        {},
        content=json.dumps(body).encode(),
        previous="owned-response",
        session="owned-session",
    )
    assert runner.egress[0]["previous_response_binding_matches_request"] is (field != "previous_response_id")
    assert runner.egress[0]["session_binding_matches_request"] is (field != "agent_session_id")
    assert all(
        value not in json.dumps(runner.egress) for value in ("owned-response", "owned-session", "unexpected-binding")
    )


@pytest.mark.parametrize("content", [b"[]", b"invalid-json", b"\xff"])
def test_uninspectable_egress_body_fails_explicitly(content: bytes) -> None:
    runner = checks.IdentityRunner(
        MagicMock(),
        None,
        uuid4(),
        expected_endpoint="https://example.invalid/openai",
        expected_principal="synthetic-principal",
        expected_client="synthetic-client",
    )
    with pytest.raises(WorkflowError, match="serialized diagnostic|Serialized diagnostic"):
        runner.inspect_egress(
            MagicMock(),
            "https://example.invalid/openai/responses",
            {},
            content=content,
            previous=None,
            session=None,
        )
    assert runner.egress == []


async def test_history_only_dispatch_changes_b_binding_without_changing_research_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hosted_context(monkeypatch)
    monkeypatch.setattr(checks.Caller, "current", lambda: Caller("call", "user", "root-session"))
    project = SimpleNamespace(
        agents=SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    as_dict=lambda: {"instance_identity": {"principal_id": "principal", "client_id": "client"}}
                )
            )
        )
    )
    project_context = AsyncMock()
    project_context.__aenter__.return_value = project
    monkeypatch.setattr(checks, "DefaultAzureCredential", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(checks, "AIProjectClient", MagicMock(return_value=project_context))
    variant = AsyncMock()
    monkeypatch.setattr(checks.IdentityRunner, "variant", variant)
    await checks.identity_experiment(
        "supervisor",
        Settings(poc_enable_identity_tests=True),
        checks.IdentityCommand(operation="compare-history-only", run_id=uuid4()),
        SimpleNamespace(),
    )
    assert [call.args for call in variant.await_args_list] == [("call-context",), ("delegated-user",)]
    assert all(
        call.kwargs == {"use_conversation": False, "bind_b_history_session": False} for call in variant.await_args_list
    )


async def test_history_service_outage_is_not_an_empty_history_success(monkeypatch: pytest.MonkeyPatch) -> None:
    hosted_context(monkeypatch)
    context = SimpleNamespace(
        conversation_id=None, client_headers={}, get_history=AsyncMock(side_effect=ConnectionError("synthetic outage"))
    )
    with pytest.raises(ConnectionError):
        await checks.research_check(
            Settings(poc_enable_identity_tests=True),
            checks.IdentityCommand(operation="history-read", run_id=uuid4()),
            context,
        )


@pytest.mark.parametrize(("mode", "actor"), [("call-context", "a"), ("delegated-user", "a"), ("delegated-user", "b")])
async def test_real_project_sdk_sends_only_the_selected_identity_mechanism(
    mode: checks.Mode, actor: Literal["a", "b"], monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = AsyncMock(spec=AsyncTokenCredential)
    credential.get_token.return_value = AccessToken("synthetic-credential", int(time()) + 3600)
    captures: list[dict[str, bool]] = []
    caller = Caller("original-call-context", "verified-root", "root-session")

    def capture(request: httpx2.Request) -> httpx2.Response:
        captures.append(
            {
                "correct_call_context": request.headers.get("x-agent-foundry-call-id") == caller.call_id
                if mode == "call-context"
                else "x-agent-foundry-call-id" not in request.headers,
                "correct_delegation_label": "x-ms-user-identity" not in request.headers
                if mode == "call-context"
                else request.headers.get("x-ms-user-identity") == runner.labels[actor],
                "diagnostic_only_expected_caller": request.headers.get("x-client-expected-caller")
                == (caller.user_id if mode == "call-context" else runner.labels[actor]),
                "no_forged_platform_user": "x-agent-user-id" not in request.headers,
            }
        )
        return httpx2.Response(200, json={"id": "conv-test", "object": "conversation", "created_at": 1})

    monkeypatch.setenv("AZURE_AI_PROJECTS_CONSOLE_LOGGING", "false")
    async with AIProjectClient(
        endpoint="https://example.services.ai.azure.com/api/projects/poc", credential=credential, allow_preview=True
    ) as project:
        original = project.get_openai_client

        def factory(*, agent_name: str | None = None, **kwargs: object) -> AsyncOpenAI:
            return original(
                agent_name=agent_name,
                **kwargs,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture), trust_env=False),
            )

        monkeypatch.setattr(project, "get_openai_client", factory)
        runner = checks.IdentityRunner(project, caller, uuid4())
        async with runner.client(mode, actor) as client:
            await client.conversations.create()
    assert captures == [
        {
            "correct_call_context": True,
            "correct_delegation_label": True,
            "diagnostic_only_expected_caller": True,
            "no_forged_platform_user": True,
        }
    ]


@pytest.mark.parametrize("bind_b_history_session", [False, True])
async def test_matrix_does_not_pass_when_b_can_read_a_state_and_history(
    bind_b_history_session: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.conversations.create.return_value = SimpleNamespace(id="conv")
    runner = checks.IdentityRunner(MagicMock(), Caller("call", "user", "root-session"), uuid4())
    monkeypatch.setattr(runner, "client", lambda *_args: client)
    reply = checks.IdentityReply(
        presence=checks.ContextPresence(
            hosted_runtime=True,
            call_id_present=True,
            user_id_present=False,
            session_id_present=True,
            conversation_id_present=True,
        ),
        expected_header_present=True,
        user_matches_expected=None,
        marker_found=True,
        marker_fingerprint="synthetic-fingerprint",
        history_marker="synthetic-history",
        history_contains_marker=True,
    )
    send = AsyncMock(side_effect=[(reply, "resp", "session")] * 7 + [(reply, "owner-followup", "session")])
    monkeypatch.setattr(runner, "send", send)
    await runner.variant("delegated-user", bind_b_history_session=bind_b_history_session)
    outcomes = {check.name: check.status for check in runner.result.checks}
    assert outcomes["delegated-user: A state continuity"] == "PASS"
    assert outcomes["delegated-user: A history continuity"] == "PASS"
    assert outcomes["delegated-user: B cannot read A state"] == "FAIL"
    assert outcomes["delegated-user: B cannot continue A history"] == "FAIL"
    assert outcomes["delegated-user: A continues after B attempt"] == "PASS"
    assert send.await_count == 8
    assert send.await_args_list[-1].args == (client, "history-read")
    assert send.await_args_list[-1].kwargs == {"previous": "resp", "session": "session"}
    assert send.await_args_list[-2].kwargs == {
        "previous": "resp",
        "session": "session" if bind_b_history_session else None,
    }
    history_check = next(check for check in runner.result.checks if check.name.endswith("B cannot continue A history"))
    assert history_check.observed["explicit_session_requested"] is bind_b_history_session


async def test_direct_control_uses_response_chains_without_conversation_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.__aenter__.return_value = client
    runner = checks.IdentityRunner(MagicMock(), None, uuid4())
    monkeypatch.setattr(runner, "client", lambda *_args: client)
    reply = checks.IdentityReply(
        presence=checks.ContextPresence(
            hosted_runtime=True,
            call_id_present=True,
            user_id_present=True,
            session_id_present=True,
            conversation_id_present=False,
        ),
        expected_header_present=True,
        user_matches_expected=None,
        marker_found=True,
        marker_fingerprint="synthetic",
        history_marker="synthetic",
        history_contains_marker=True,
    )
    send = AsyncMock(return_value=(reply, "resp", "session"))
    monkeypatch.setattr(runner, "send", send)
    await runner.variant("delegated-user", use_conversation=False)
    client.conversations.create.assert_not_awaited()
    assert send.await_args_list[0].kwargs == {"conversation": None}
    assert all(call.kwargs.get("conversation") is None for call in send.await_args_list)
    assert runner.result.checks[0].status == "PASS"
    outcomes = {check.name: check.status for check in runner.result.checks}
    assert outcomes["delegated-user: B cannot continue A history"] == "FAIL"


def test_failure_reports_safe_classification_not_raw_service_identity_payload() -> None:
    caller = Caller("sensitive-call", "sensitive-user", "session")
    runner = checks.IdentityRunner(MagicMock(), caller, uuid4())
    response = httpx.Response(403, request=httpx.Request("POST", "https://example.invalid/responses"))
    error = APIStatusError(
        "sensitive message",
        response=response,
        body={"error": {"code": "forbidden", "message": "sensitive-user UserIdentityImpersonation sensitive-call"}},
    )
    runner.failure("direct-control", error)
    result = runner.result.checks[0]
    assert result.status == "FAIL"
    assert result.observed["impersonation_action_mentioned"] is True
    assert "sensitive" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("owner_exists", "owner_continues", "resumed_session", "followup_id"),
    [
        (False, True, "session", "owner-followup"),
        (True, False, "session", "owner-followup"),
        (True, True, "different-session", "owner-followup"),
        (True, True, "session", "resp"),
        (True, True, "session", "owner-followup"),
    ],
)
@pytest.mark.parametrize("bind_b_history_session", [False, True])
async def test_aab_masked_404_is_checked_against_owner_resource(
    owner_exists: bool,
    owner_continues: bool,
    resumed_session: str,
    followup_id: str,
    bind_b_history_session: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.conversations.create.return_value = SimpleNamespace(id="conv")
    client.responses.retrieve.return_value = SimpleNamespace(
        id="resp",
        status="completed" if owner_exists else "failed",
        error=None,
    )
    other = AsyncMock()
    other.__aenter__.return_value = other
    runner = checks.IdentityRunner(MagicMock(), None, uuid4())
    monkeypatch.setattr(runner, "client", lambda _mode, actor="a": client if actor == "a" else other)
    reply = checks.IdentityReply(
        presence=checks.ContextPresence(
            hosted_runtime=True,
            call_id_present=True,
            user_id_present=True,
            session_id_present=True,
            conversation_id_present=False,
        ),
        expected_header_present=True,
        user_matches_expected=True,
        marker_found=True,
        marker_fingerprint="synthetic",
        history_marker="synthetic",
        history_contains_marker=True,
    )
    hidden = reply.model_copy(update={"marker_found": False, "marker_fingerprint": None})
    response = httpx.Response(404, request=httpx.Request("POST", "https://example.invalid/responses"))
    denied = APIStatusError("synthetic", response=response, body={"error": {"code": "not_found"}})
    owner_reply = reply.model_copy(update={"history_contains_marker": owner_continues})
    send = AsyncMock(
        side_effect=[
            (reply, "resp", "session"),
            (reply, "resp", "session"),
            (reply, "resp", "session"),
            (hidden, "resp", "session"),
            (reply, "resp", "session"),
            (reply, "resp", "session"),
            denied,
            (owner_reply, followup_id, resumed_session),
        ]
    )
    monkeypatch.setattr(
        runner,
        "send",
        send,
    )
    await runner.variant("delegated-user", use_conversation=False, bind_b_history_session=bind_b_history_session)
    client.responses.retrieve.assert_awaited_once_with("resp")
    outcomes = {check.name: check for check in runner.result.checks}
    result = outcomes["delegated-user: B cannot continue A history"]
    assert result.status == ("PASS" if owner_exists else "FAIL")
    assert result.observed["owner_resource_confirmed"] is owner_exists
    continuity_passes = owner_continues and resumed_session == "session" and followup_id != "resp"
    assert outcomes["delegated-user: A continues after B attempt"].status == ("PASS" if continuity_passes else "FAIL")
    assert all(check.status == "PASS" for check in runner.result.checks) is (owner_exists and continuity_passes)
    assert send.await_args_list[-2].args == (other, "history-read")
    assert send.await_args_list[-2].kwargs == {
        "previous": "resp",
        "session": "session" if bind_b_history_session else None,
    }
    assert result.observed["explicit_session_requested"] is bind_b_history_session
    assert send.await_args_list[-1].args == (client, "history-read")
    assert send.await_args_list[-1].kwargs == {"previous": "resp", "session": "session"}
