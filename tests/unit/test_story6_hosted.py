"""Pinned hosted client contracts; all external operations use explicit doubles."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx2
import pytest
from app.services.story6_approval import Command, PendingApproval, Reply
from openai import AsyncOpenAI

from scripts.run_scenario import story6_hosted as live


def reply(operation_id, phase="pending", decision=None) -> Reply:
    return Reply(
        operation_id=operation_id,
        phase=phase,
        pending=PendingApproval(reference="1" * 64) if phase == "pending" else None,
        decision=decision,
        answer="",
        research_response_observed=True,
        research_user_context_present=False,
        provider_responses_observed=1,
        mcp_calls_reported=1 if phase == "completed" else 0,
        file_sha256="2" * 64,
        file_retained=True,
        same_process=True,
    )


async def test_real_sdk_keeps_explicit_session_conversation_and_custom_json(tmp_path: Path) -> None:
    case = live.Case(
        environment="poc",
        version="1",
        operation_id=uuid4(),
        session_id="session-private",
        conversation_id="conversation-private",
    )
    expected = reply(case.operation_id)
    requests = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["agent_session_id"] == case.session_id
        assert body["conversation"] == case.conversation_id
        assert body["input"] == Command(operation="start", operation_id=case.operation_id).wire()
        return httpx2.Response(
            200,
            json={
                "id": "response-private",
                "object": "response",
                "created_at": 1,
                "model": "synthetic",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "message-synthetic",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": expected.model_dump_json(), "annotations": []}],
                    }
                ],
            },
        )

    async with AsyncOpenAI(
        api_key="synthetic-not-a-credential",
        base_url="https://synthetic.invalid",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    ) as client:
        assert await live.send(client, case, Command(operation="start", operation_id=case.operation_id)) == expected
    assert len(requests) == 1
    public = json.dumps(live.public_observation(expected))
    for private in ("session-private", "conversation-private", "1" * 64, str(case.operation_id)):
        assert private not in public
    assert "fixed_query_matches" in public


def test_custom_case_is_approved_only_with_exact_operation_arguments() -> None:
    operation = uuid4()
    custom = reply(operation).model_copy(
        update={
            "mcp_route": "entra-mcp",
            "pending": PendingApproval(
                reference="1" * 64,
                tool_name="story6_record_action",
                arguments={"case_id": str(operation)},
            ),
        }
    )
    assert live.expected_arguments(custom)
    observed = live.public_observation(custom)
    assert observed["pending"]["case_id_matches_operation"]
    assert str(operation) not in json.dumps(observed)
    changed = custom.model_copy(
        update={
            "pending": custom.pending.model_copy(update={"arguments": {"case_id": str(uuid4())}}),
        }
    )
    assert not live.expected_arguments(changed)


async def test_custom_task_refuses_old_saved_route_before_any_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(live, "ROOT", tmp_path)
    monkeypatch.setattr(live, "deployment_context", lambda *args: {})
    state = tmp_path / "artifacts" / "story-6" / "case-state.json"
    state.parent.mkdir(parents=True)
    operation = uuid4()
    live.save(
        state,
        live.Case(
            environment="poc",
            version="2",
            operation_id=operation,
            phase="pending",
            last_reply=reply(operation),
        ),
    )
    monkeypatch.setattr(
        live, "AzureCliCredential", MagicMock(side_effect=AssertionError("No credential lookup expected"))
    )
    with pytest.raises(ValueError, match="expected MCP route"):
        await live.execute("poc", "approve", state, timeout=30, expected_route="entra-mcp")


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("audit", [False, True])
async def test_start_never_auto_decides_and_terminal_decision_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str, audit: bool
) -> None:
    from scripts.run_scenario import story6_history as history

    identity = AsyncMock(return_value="private-principal")
    snapshots = AsyncMock(return_value={"items": 0, "operation_mismatches": 0})
    monkeypatch.setattr(history, "verified_operator", identity)
    monkeypatch.setattr(history, "snapshot", snapshots)
    monkeypatch.setattr(live, "ROOT", tmp_path)
    monkeypatch.setattr(
        live,
        "deployment_context",
        lambda *args: {
            "AZURE_AI_PROJECT_ENDPOINT": "https://synthetic.invalid",
            "AZURE_TENANT_ID": "private-tenant",
        },
    )
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    monkeypatch.setattr(live, "AzureCliCredential", MagicMock(return_value=credential))
    project = MagicMock()
    project.__aenter__.return_value = project
    project.agents.get = AsyncMock(
        return_value=SimpleNamespace(as_dict=lambda: {"versions": {"latest": {"version": "1", "status": "active"}}})
    )
    project.agents.create_session = AsyncMock(return_value=SimpleNamespace(agent_session_id="session-private"))
    client = MagicMock()
    client.__aenter__.return_value = client
    client.conversations.create = AsyncMock(return_value=SimpleNamespace(id="conversation-private"))
    project.get_openai_client.return_value = client
    monkeypatch.setattr(live, "AIProjectClient", MagicMock(return_value=project))
    commands = []
    state_file = tmp_path / "artifacts/story-6/case-state.json"
    state_file.parent.mkdir(parents=True)

    async def send(received_client, case, command):
        assert received_client is client
        persisted = live.load(state_file, "poc")
        commands.append(command)
        if command.operation == "start":
            assert persisted.phase == "start_submitted"
            return reply(case.operation_id)
        assert persisted.phase == "decision_submitted"
        return reply(case.operation_id, "completed" if command.approve else "rejected", command.approve)

    monkeypatch.setattr(live, "send", send)
    records = []
    await live.execute(
        "poc",
        "start",
        state_file,
        timeout=120,
        capture_history=audit,
        evidence=lambda *args: records.append(args),
    )
    assert [command.operation for command in commands] == ["start"]
    assert live.load(state_file, "poc").phase == "pending"
    if audit:
        identity.return_value = "different-principal"
        with pytest.raises(ValueError, match="original authenticated requester"):
            await live.execute("poc", decision, state_file, timeout=120, evidence=lambda *args: None)
        assert live.load(state_file, "poc").phase == "pending"
        assert len(commands) == 1
        identity.return_value = "private-principal"
    await live.execute("poc", decision, state_file, timeout=120, evidence=lambda *args: records.append(args))
    with pytest.raises(ValueError, match="Only a saved pending"):
        await live.execute("poc", decision, state_file, timeout=120)
    assert len(commands) == 2
    assert commands[-1].approve is (decision == "approve")
    project.agents.create_session.assert_awaited_once()
    client.conversations.create.assert_awaited_once()
    assert project.get_openai_client.call_args.kwargs["max_retries"] == 0
    if audit:
        assert snapshots.await_count == 4
        assert live.load(state_file, "poc").audit_history is True
        callers = [value["caller_alias"] for name, value in records if name == "Authenticated caller correlation"]
        assert len(callers) == 2 and callers[0] == callers[1]
    else:
        snapshots.assert_not_awaited()
        identity.assert_not_awaited()


async def test_lost_start_keeps_the_original_locator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "ROOT", tmp_path)
    monkeypatch.setattr(
        live, "deployment_context", lambda *args: {"AZURE_AI_PROJECT_ENDPOINT": "https://synthetic.invalid"}
    )
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    monkeypatch.setattr(live, "AzureCliCredential", MagicMock(return_value=credential))
    project = MagicMock()
    project.__aenter__.return_value = project
    project.agents.get = AsyncMock(
        return_value=SimpleNamespace(as_dict=lambda: {"versions": {"latest": {"version": "1", "status": "active"}}})
    )
    project.agents.create_session = AsyncMock(return_value=SimpleNamespace(agent_session_id="session-private"))
    client = MagicMock()
    client.__aenter__.return_value = client
    client.conversations.create = AsyncMock(return_value=SimpleNamespace(id="conversation-private"))
    project.get_openai_client.return_value = client
    monkeypatch.setattr(live, "AIProjectClient", MagicMock(return_value=project))
    monkeypatch.setattr(live, "send", AsyncMock(side_effect=TimeoutError("Synthetic lost result.")))
    state_file = tmp_path / "artifacts/story-6/case-state.json"
    state_file.parent.mkdir(parents=True)
    with pytest.raises(TimeoutError):
        await live.execute("poc", "start", state_file, timeout=120)
    assert live.load(state_file, "poc").phase == "start_submitted"
    with pytest.raises(ValueError, match="never replace"):
        await live.execute("poc", "start", state_file, timeout=120)
    project.agents.create_session.assert_awaited_once()


def test_case_locator_cannot_be_read_from_an_unrelated_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="under artifacts"):
        live.load(tmp_path / "outside.json", "poc")
