"""Presence-only probe tests; no cloud behavior is claimed by these fakes."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app import hosting
from app.core.redaction import sanitize
from app.core.settings import Settings
from app.services import foundry_state
from app.services.delegation import ContextCommand, ContextReport, SpecialistClient
from app.services.foundry_state import Caller, ContextPresence
from app.services.workflows import WorkflowError
from azure.ai.projects.aio import AIProjectClient
from pydantic import ValidationError

from scripts.common.reporting import ValidationRun
from scripts.run_scenario.context_probe import compare


def ambient(monkeypatch: pytest.MonkeyPatch, *, hosted: bool = True, session: str | None = "secret-session") -> None:
    context = SimpleNamespace(call_id="secret-call", user_id="secret-user", session_id=session)
    monkeypatch.setattr(foundry_state, "get_request_context", lambda: context)
    monkeypatch.setattr(hosting, "get_request_context", lambda: context)
    monkeypatch.setattr(foundry_state.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=hosted))


def test_presence_and_error_never_include_actual_context_values(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch, session=None)
    flags = ContextPresence.inspect("secret-conversation")
    assert flags.call_id_present and flags.user_id_present
    assert not flags.session_id_present
    assert flags.conversation_id_present
    assert "secret" not in flags.model_dump_json()
    assert sanitize(flags.model_dump())["call_id_present"] is True
    assert sanitize({"call_id_present": "secret", "call_id": "secret"}) == {
        "call_id_present": "[REDACTED]",
        "call_id": "[REDACTED]",
    }
    with pytest.raises(WorkflowError, match="session_id_present") as error:
        Caller.current()
    assert "secret" not in str(error.value)


async def test_direct_probe_works_with_missing_context_without_model_or_state_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient(monkeypatch, session=None)
    store = MagicMock(side_effect=AssertionError("Probe must not open a state store."))
    monkeypatch.setattr(hosting, "open_store", store)
    context = SimpleNamespace(conversation_id="secret-conversation", client_headers={})
    report = await hosting.context_probe("research", Settings(), ContextCommand(), context)
    assert not report.presence.session_id_present
    assert report.research is None
    assert report.caller_matches_expected is None
    assert "secret" not in report.model_dump_json()
    store.assert_not_called()


async def test_host_routes_context_command_without_invoking_maf(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    server = SimpleNamespace()

    def register(handler):
        server.handler = handler
        return handler

    server.response_handler = register
    monkeypatch.setattr(hosting, "ResponsesAgentServerHost", lambda **_kwargs: server)
    agent = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("Probe must not invoke a model.")))
    hosting.build_host("research", Settings(), agent, lambda **_kwargs: None)
    context = SimpleNamespace(
        response_id="response",
        conversation_id="secret-conversation",
        client_headers={},
        is_recovery=False,
        get_input_text=AsyncMock(return_value=ContextCommand().wire()),
    )
    stream = await server.handler({}, context, asyncio.Event())
    events = [event async for event in stream]
    text = events[-1]["response"]["output"][0]["content"][0]["text"]
    report = ContextReport.model_validate_json(text)
    assert report.agent == "research"
    assert all(report.presence.model_dump().values())
    agent.run.assert_not_awaited()
    assert "secret" not in text


async def test_offplatform_supervisor_cannot_forward_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch, hosted=False)
    project = MagicMock(side_effect=AssertionError("No outbound calls without a verified root caller."))
    monkeypatch.setattr(hosting, "AIProjectClient", project)
    with pytest.raises(WorkflowError, match="hosted_runtime"):
        await hosting.context_probe(
            "supervisor",
            Settings(),
            ContextCommand(operation="research"),
            SimpleNamespace(conversation_id="conversation", client_headers={}),
        )
    project.assert_not_called()


async def test_specialist_cannot_chain_another_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    with pytest.raises(WorkflowError, match="Only Supervisor"):
        await hosting.context_probe(
            "research",
            Settings(),
            ContextCommand(operation="research"),
            SimpleNamespace(conversation_id="conversation", client_headers={}),
        )


async def test_nested_probe_uses_the_same_client_but_returns_missing_flags() -> None:
    flags = ContextPresence(
        hosted_runtime=True,
        call_id_present=False,
        user_id_present=True,
        session_id_present=True,
        conversation_id_present=True,
    )
    report = ContextReport(agent="research", presence=flags, caller_matches_expected=True)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.conversations.create.return_value = SimpleNamespace(id="actual-conversation")
    client.responses.create.return_value = SimpleNamespace(
        status="completed", error=None, output_text=report.model_dump_json()
    )
    project = SimpleNamespace(get_openai_client=MagicMock(return_value=client))
    delegate = SpecialistClient(project, Caller("secret-call", "secret-user", "secret-session"), 180)
    assert await delegate.probe_research() == report
    client.responses.create.assert_awaited_once_with(
        input=ContextCommand().wire(), conversation="actual-conversation", stream=False, extra_body={}
    )
    assert project.get_openai_client.call_args.kwargs["default_headers"]["x-agent-foundry-call-id"] == "secret-call"
    assert project.get_openai_client.call_args.kwargs["default_headers"]["x-ms-user-identity"] == "secret-user"


async def test_real_sdk_preserves_both_context_and_user_delegation_headers() -> None:
    credential = AsyncMock()
    async with AIProjectClient(
        endpoint="https://example.services.ai.azure.com/api/projects/poc",
        credential=credential,
        allow_preview=True,
    ) as project:
        delegate = SpecialistClient(project, Caller("opaque-call", "verified-user", "session"), 180)
        async with delegate._client("research") as client:
            assert client.default_headers["x-agent-foundry-call-id"] == "opaque-call"
            assert client.default_headers["x-ms-user-identity"] == "verified-user"
            assert client.default_headers["x-client-expected-caller"] == "verified-user"
    credential.get_token.assert_not_called()


def test_delegation_does_not_rewrite_an_unsupported_verified_user_id() -> None:
    project = SimpleNamespace(get_openai_client=MagicMock())
    delegate = SpecialistClient(project, Caller("call", "invalid user value", "session"), 180)
    with pytest.raises(WorkflowError, match="header format"):
        delegate._client("research")
    project.get_openai_client.assert_not_called()


def test_probe_command_rejects_client_supplied_identity() -> None:
    with pytest.raises(ValidationError):
        ContextCommand.model_validate({"operation": "direct", "user_id": "untrusted"})


@pytest.mark.parametrize("nested_session", [True, False])
def test_comparison_requires_all_presence_flags_and_exact_caller_match(nested_session: bool, tmp_path: Path) -> None:
    flags = ContextPresence(
        hosted_runtime=True,
        call_id_present=True,
        user_id_present=True,
        session_id_present=True,
        conversation_id_present=True,
    )
    direct = ContextReport(agent="research", presence=flags, caller_matches_expected=None)
    nested = ContextReport(
        agent="research",
        presence=flags.model_copy(update={"session_id_present": nested_session}),
        caller_matches_expected=True,
    )
    root = ContextReport(agent="supervisor", presence=flags, caller_matches_expected=None, research=nested)
    run = ValidationRun("comparison", "unit", tmp_path, no_color=True)
    assert compare(direct, root, run) is nested_session
    wrong_caller = root.model_copy(update={"research": nested.model_copy(update={"caller_matches_expected": False})})
    assert compare(direct, wrong_caller, run) is False
