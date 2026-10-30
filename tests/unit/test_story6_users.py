"""Two-user probe oracles and no-replay controls, not live identity evidence."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx2
import pytest
from app.services.story6_approval import Command, PendingApproval, Reply
from openai import AsyncOpenAI

from scripts.run_scenario import story6_users as users
from scripts.run_scenario.story6_hosted import Case, load, save


@pytest.mark.parametrize(
    ("status", "code", "message", "outcome"),
    [
        (403, "session_not_accessible", "Foreign session.", "denied"),
        (401, "Unauthorized", "Authentication failed.", "inconclusive"),
        (403, "Forbidden", "No endpoint permission.", "inconclusive"),
        (404, "not_found", "Missing route.", "inconclusive"),
        (500, "server_error", "Service error.", "inconclusive"),
        (200, "story6_request_failed", users.MISSING_OPERATION, "denied"),
        (200, "story6_request_failed", "Native SDK failed.", "inconclusive"),
    ],
)
async def test_probe_classifies_only_explicit_owner_visibility_denials(
    status: int, code: str, message: str, outcome: str
) -> None:
    case = Case(
        environment="poc",
        version="1",
        operation_id=uuid4(),
        session_id="private-session",
        conversation_id="private-conversation",
    )
    command = Command(operation="inspect", operation_id=case.operation_id)
    calls = []

    def transport(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert body["agent_session_id"] == case.session_id and body["conversation"] == case.conversation_id
        if status != 200:
            return httpx2.Response(status, json={"error": {"code": code, "message": message}})
        return httpx2.Response(
            200,
            json={
                "id": "response-private",
                "object": "response",
                "created_at": 1,
                "model": "synthetic",
                "status": "failed",
                "error": {"code": code, "message": message},
                "output": [],
            },
        )

    async with AsyncOpenAI(
        api_key="synthetic-not-a-credential",
        base_url="https://synthetic.invalid",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
    ) as client:
        observed = await users.probe(client, case, command)
    assert observed.outcome == outcome
    assert observed.denied is (outcome == "denied") and len(calls) == 1
    public = json.dumps(observed.observed())
    assert "private" not in public and "Foreign session." not in public


def pending(case: Case) -> Reply:
    return Reply(
        operation_id=case.operation_id,
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


def mocked_users(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from scripts.run_scenario import story6_hosted

    monkeypatch.setattr(story6_hosted, "ROOT", tmp_path)
    profiles = [SimpleNamespace(label="A"), SimpleNamespace(label="B")]
    monkeypatch.setattr(
        users,
        "users_and_version",
        AsyncMock(
            return_value=(
                {"AZURE_AI_PROJECT_ENDPOINT": "https://synthetic.invalid"},
                profiles,
                "1",
            )
        ),
    )
    credential = MagicMock()
    credential.__aenter__.return_value = credential
    monkeypatch.setattr(users, "ProfileCredential", MagicMock(return_value=credential))
    project = MagicMock()
    project.__aenter__.return_value = project
    client = MagicMock()
    client.__aenter__.return_value = client
    project.get_openai_client.return_value = client
    monkeypatch.setattr(users, "AIProjectClient", MagicMock(return_value=project))

    async def create(project, client, environment, version, path):
        case = Case(
            environment=environment,
            version=version,
            operation_id=uuid4(),
            session_id="synthetic-session",
            conversation_id="synthetic-conversation",
            phase="ready",
        )
        save(path, case)
        return case

    monkeypatch.setattr(users, "new_case", create)
    return client


async def test_foreign_probes_use_the_exact_owner_binding_without_auto_owner_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mocked_users(monkeypatch, tmp_path)
    sent = []

    async def send(client, case, command):
        sent.append(command)
        assert command.operation in ("start", "inspect")
        return pending(case)

    probes = []

    async def probe(client, case, command):
        probes.append((case.operation_id, command))
        boundary = "Foundry session" if len(probes) in (2, 3) else "coordinator user-isolated operation"
        return users.Probe("denied", boundary, "operation_not_visible")

    monkeypatch.setattr(users, "send", send)
    monkeypatch.setattr(users, "probe", probe)
    path = tmp_path / "artifacts/story-6/case-state.json"
    path.parent.mkdir(parents=True)
    records = []
    await users.execute("poc", "not-saved-in-evidence", "start", path, lambda *args: records.append(args), timeout=120)
    case = load(path, "poc")
    assert case.phase == "pending" and case.last_reply.decision is None
    assert len(probes) == 5 and len(sent) == 2
    owner_decisions = [command for command in sent if command.operation == "decide"]
    assert owner_decisions == []
    assert probes[2][1].operation == probes[4][1].operation == "decide"
    assert probes[2][1].approve is probes[4][1].approve is True
    assert all(record[1] == "PASS" for record in records)


async def test_generic_forbidden_route_never_counts_as_unauthorized_approver_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mocked_users(monkeypatch, tmp_path)
    monkeypatch.setattr(users, "send", AsyncMock(side_effect=lambda client, case, command: pending(case)))
    probe = AsyncMock(return_value=users.Probe("inconclusive", "Foundry session", "Forbidden", 403))
    monkeypatch.setattr(users, "probe", probe)
    path = tmp_path / "artifacts/story-6/case-state.json"
    path.parent.mkdir(parents=True)
    records = []
    with pytest.raises(ValueError, match="positive route control"):
        await users.execute(
            "poc", "private-profile-input", "start", path, lambda *args: records.append(args), timeout=120
        )
    assert probe.await_count == 1
    assert records[-1][1] == "BLOCKED"
    assert load(path, "poc").phase == "pending"


async def test_explicit_owner_approval_uses_original_case_and_cannot_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = mocked_users(monkeypatch, tmp_path)
    path = tmp_path / "artifacts/story-6/case-state.json"
    path.parent.mkdir(parents=True)
    case = Case(
        environment="poc",
        version="1",
        operation_id=uuid4(),
        phase="pending",
        session_id="private-session",
        conversation_id="private-conversation",
    )
    case.last_reply = pending(case)
    save(path, case)
    completed = case.last_reply.model_copy(
        update={
            "phase": "completed",
            "pending": None,
            "decision": True,
            "mcp_calls_reported": 1,
        }
    )

    async def send(received_client, received_case, command):
        assert received_client is client
        assert received_case.operation_id == case.operation_id and command.approve is True
        assert load(path, "poc").phase == "decision_submitted"
        return completed

    submit = AsyncMock(side_effect=send)
    monkeypatch.setattr(users, "send", submit)
    no_create = AsyncMock(side_effect=AssertionError("Owner continuation must not create another case."))
    monkeypatch.setattr(users, "new_case", no_create)
    records = []
    await users.execute("poc", "not-saved", "approve", path, lambda *args: records.append(args), timeout=120)
    assert load(path, "poc").phase == "completed"
    assert records[-1][1] == "PASS"
    with pytest.raises(ValueError, match="pending approval"):
        await users.execute("poc", "not-saved", "approve", path, lambda *args: None, timeout=120)
    submit.assert_awaited_once()
    no_create.assert_not_awaited()
