"""Local fake-store logic tests, not evidence of Foundry isolation or recovery."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.core.settings import Settings
from app.hosting import failed_response, history_messages, workflow_turn
from app.services import foundry_state
from app.services.delegation import Command, Report, SpecialistClient
from app.services.foundry_state import Caller, FoundryStore, open_store
from app.services.workflows import Child, Item, MissingMapping, Registry, StateConflict, WorkflowError
from pydantic import ValidationError


class MemoryStore:
    """A separate fake instance represents one platform-user partition."""

    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.writes: list[str] = []
        self.version = 0

    async def read(self, key: str) -> Item | None:
        await asyncio.sleep(0)
        return deepcopy(self.items.get(key))

    async def create(self, key: str, value: dict[str, object]) -> None:
        await asyncio.sleep(0)
        if key in self.items:
            raise StateConflict("fake create-only conflict")
        self._write(key, value)

    async def replace(self, key: str, value: dict[str, object], etag: str) -> None:
        await asyncio.sleep(0)
        if key not in self.items or self.items[key].etag != etag:
            raise StateConflict("fake ETag conflict")
        self._write(key, value)

    def _write(self, key: str, value: dict[str, object]) -> None:
        self.version += 1
        self.items[key] = Item(deepcopy(value), str(self.version))
        self.writes.append(key)


async def test_random_mappings_are_reused_across_registry_instances() -> None:
    store = MemoryStore()
    workflow_id = uuid4()
    registry = Registry(store, "supervisor", "verified-session", "real-conversation")
    first = await registry.create(workflow_id)
    assert first.workflow.owner_id.version == first.workflow.session_id.version == 4
    assert first.workflow.owner_id != first.workflow.session_id
    assert "verified-session" not in repr(store.items)
    writes = list(store.writes)
    reopened = await Registry(store, "supervisor", "verified-session", "real-conversation").open(workflow_id)
    duplicate = await registry.create(workflow_id)
    assert first == reopened == duplicate
    assert store.writes == writes


async def test_new_sessions_reuse_owner_but_not_session_and_fake_users_are_distinct() -> None:
    store = MemoryStore()
    first = await Registry(store, "research", "session-one", "conversation-one").create(uuid4())
    second = await Registry(store, "research", "session-two", "conversation-two").create(uuid4())
    other_user = await Registry(MemoryStore(), "research", "session-one", "conversation-one").create(uuid4())
    assert first.workflow.owner_id == second.workflow.owner_id
    assert first.workflow.session_id != second.workflow.session_id
    assert first.workflow.owner_id != other_user.workflow.owner_id
    assert first.workflow.session_id != other_user.workflow.session_id


@pytest.mark.parametrize("missing", ["owner", "session", "workflow"])
async def test_continuation_missing_mapping_never_writes(missing: str) -> None:
    store = MemoryStore()
    registry = Registry(store, "supervisor", "verified-session", "conversation")
    workflow_id = uuid4()
    await registry.create(workflow_id)
    key = {"owner": "owner", "session": registry.session_key, "workflow": f"workflow/{workflow_id}"}[missing]
    del store.items[key]
    writes = list(store.writes)
    with pytest.raises(MissingMapping):
        await registry.open(workflow_id)
    assert store.writes == writes
    assert key not in store.items


async def test_creation_replay_cannot_remint_a_deleted_owner() -> None:
    store = MemoryStore()
    registry = Registry(store, "research", "session", "conversation")
    workflow_id = uuid4()
    await registry.create(workflow_id)
    del store.items["owner"]
    writes = list(store.writes)
    with pytest.raises(MissingMapping):
        await registry.create(workflow_id)
    assert "owner" not in store.items
    assert store.writes == writes


async def test_concurrent_creators_read_the_committed_random_winner() -> None:
    store = MemoryStore()
    workflow_id = uuid4()
    left = Registry(store, "supervisor", "same-session", "conversation")
    right = Registry(store, "supervisor", "same-session", "conversation")
    a, b = await asyncio.gather(left.create(workflow_id), right.create(workflow_id))
    assert a.workflow == b.workflow
    assert store.writes.count("owner") == 1
    assert store.writes.count(left.session_key) == 1
    assert store.writes.count(f"workflow/{workflow_id}") == 1


@pytest.mark.parametrize(
    ("session", "conversation"),
    [("different-session", "conversation"), ("verified-session", "different-conversation")],
)
async def test_scope_mismatch_is_lookup_only(session: str, conversation: str) -> None:
    store = MemoryStore()
    workflow_id = uuid4()
    await Registry(store, "research", "verified-session", "conversation").create(workflow_id)
    writes = list(store.writes)
    with pytest.raises(WorkflowError):
        await Registry(store, "research", session, conversation).open(workflow_id)
    assert store.writes == writes


async def test_corrupt_owner_binding_fails_without_regeneration() -> None:
    store = MemoryStore()
    registry = Registry(store, "research", "session", "conversation")
    workflow_id = uuid4()
    await registry.create(workflow_id)
    store.items["owner"] = Item({"schema_version": 1, "owner_id": str(uuid4())}, "corrupt")
    writes = list(store.writes)
    with pytest.raises(WorkflowError, match="inconsistent"):
        await registry.open(workflow_id)
    assert store.writes == writes


async def test_concurrent_turns_and_incomplete_replay_fail_closed() -> None:
    registry = Registry(MemoryStore(), "supervisor", "session", "conversation")
    original = await registry.create(uuid4())
    running = await registry.begin_turn(original)
    with pytest.raises(StateConflict):
        await registry.begin_turn(original)
    with pytest.raises(WorkflowError, match="unfinished"):
        await registry.begin_turn(running)


async def test_child_conversations_cannot_be_replaced_or_resurrect_parent() -> None:
    store = MemoryStore()
    registry = Registry(store, "supervisor", "session", "conversation")
    binding = await registry.create(uuid4())
    child = Child(agent="research", workflow_id=uuid4(), conversation_id="actual-child-conversation")
    saved = await registry.save_child(binding, child)
    assert saved.workflow.children["research"] == child
    with pytest.raises(WorkflowError, match="cannot be replaced"):
        await registry.save_child(saved, child.model_copy(update={"conversation_id": "different"}))
    del store.items[f"workflow/{saved.workflow.workflow_id}"]
    with pytest.raises(StateConflict):
        await registry.save_child(saved, child)


def test_local_stateful_access_fails_before_sdk_json_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    with pytest.raises(WorkflowError, match="verified Foundry"):
        Caller.current()


@pytest.mark.parametrize("field", ["owner_id", "session_id", "user_id", "call_id"])
def test_commands_reject_body_ownership_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        Command.model_validate({"operation": "create", "workflow_id": str(uuid4()), field: "untrusted"})


async def test_adapter_passes_original_call_id_and_requires_etag() -> None:
    sdk = SimpleNamespace(
        get_item=AsyncMock(return_value=SimpleNamespace(value={"value": 1}, etag="server-etag")),
        create_item=AsyncMock(),
        set_item=AsyncMock(),
    )
    caller = Caller("opaque-original-call", "platform-user", "platform-session")
    store = FoundryStore(sdk, caller)
    assert await store.read("key") == Item({"value": 1}, "server-etag")
    await store.create("new", {"value": 2})
    await store.replace("key", {"value": 3}, "server-etag")
    sdk.get_item.assert_awaited_once_with("key", call_id=caller.call_id)
    sdk.create_item.assert_awaited_once_with("new", {"value": 2}, call_id=caller.call_id)
    sdk.set_item.assert_awaited_once_with("key", {"value": 3}, if_match="server-etag", call_id=caller.call_id)
    with pytest.raises(WorkflowError, match="ETag"):
        await store.replace("key", {}, "")
    with pytest.raises(WorkflowError, match="200 KB"):
        await store.create("oversized", {"value": "x" * 200_000})
    assert sdk.create_item.await_count == 1
    assert "opaque-original-call" not in repr(caller)


async def test_store_failure_never_creates_replacement_mappings() -> None:
    store = MemoryStore()
    store.read = AsyncMock(side_effect=ConnectionError("state store unavailable"))
    with pytest.raises(ConnectionError):
        await Registry(store, "research", "session", "conversation").open(uuid4())
    assert store.writes == []


@pytest.mark.parametrize(("isolated", "ttl"), [(False, -1), (True, 2_592_000)])
async def test_existing_store_must_match_isolation_and_retention(
    isolated: bool, ttl: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(user_isolation=isolated, item_ttl_seconds=ttl)),
        aclose=AsyncMock(),
    )
    factory = MagicMock(return_value=sdk)
    monkeypatch.setattr(foundry_state, "FoundryStateStore", factory)
    monkeypatch.setattr(Caller, "current", lambda: Caller("call", "user", "session"))
    with pytest.raises(WorkflowError, match="refusing incompatible"):
        async with open_store("research", AsyncMock(), create=False):
            pytest.fail("Incompatible store must not be yielded.")
    sdk.aclose.assert_awaited_once()
    factory.get_or_create.assert_not_called()


async def test_completed_creation_replay_does_not_invoke_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    import app.hosting as hosting

    store = MemoryStore()
    workflow_id = uuid4()
    registry = Registry(store, "research", "session", "conversation")
    binding = await registry.create(workflow_id)
    await registry.finish_turn(binding, "persisted answer")
    writes = list(store.writes)

    @asynccontextmanager
    async def fake_open(*_args: object, **_kwargs: object):
        yield store

    monkeypatch.setattr(hosting, "open_store", fake_open)
    monkeypatch.setattr(Caller, "current", lambda: Caller("call", "user", "session"))
    context = SimpleNamespace(
        conversation_id="conversation", client_headers={}, get_history=AsyncMock(return_value=[]), is_recovery=False
    )
    agent = SimpleNamespace(run=AsyncMock())
    result = await workflow_turn(
        "research", Settings(), agent, Command(operation="create", workflow_id=workflow_id, message="retry"), context
    )
    assert Report.model_validate_json(result).answer == "persisted answer"
    agent.run.assert_not_awaited()
    assert store.writes == writes


async def test_protocol_domain_failure_is_failed_not_a_completed_error_answer() -> None:
    context = SimpleNamespace(response_id="real-response-id")
    events = [event async for event in failed_response({}, context, "Required workflow mapping is missing.")]
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["status"] == "failed"
    assert events[-1]["response"]["error"]["message"] == "Required workflow mapping is missing."
    assert events[-1]["response"]["output"] == []


@pytest.mark.parametrize("recovery", [False, True])
async def test_wrong_delegated_caller_and_recovery_fail_before_store_access(
    recovery: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.hosting as hosting

    monkeypatch.setattr(Caller, "current", lambda: Caller("call", "actual-platform-user", "session"))
    store = MagicMock()
    monkeypatch.setattr(hosting, "open_store", store)
    context = SimpleNamespace(
        is_recovery=recovery,
        conversation_id="conversation",
        client_headers={"x-client-expected-caller": "different-user"},
    )
    with pytest.raises(WorkflowError):
        await workflow_turn(
            "research",
            Settings(),
            MagicMock(),
            Command(operation="create", workflow_id=uuid4(), message="test"),
            context,
        )
    store.assert_not_called()


def test_history_exposes_task_and_visible_answers_not_registry_or_reasoning() -> None:
    workflow_id = uuid4()
    command = Command(operation="create", workflow_id=workflow_id, message="Remember ALPHA.")
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": command.wire()}]},
        {"type": "reasoning", "encrypted_content": "hidden"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "READY"}]},
    ]
    messages = history_messages(items)
    assert [message.text for message in messages] == ["Remember ALPHA.", "READY"]
    assert str(workflow_id) not in repr(messages)
    assert (
        history_messages(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "{ordinary unfinished JSON"}],
                }
            ]
        )[0].text
        == "{ordinary unfinished JSON"
    )


async def test_specialist_uses_real_binding_and_verified_delegated_headers() -> None:
    caller = Caller("never-forward-this-call-id-to-another-agent", "verified-user", "root-session")
    child = Child(
        agent="research",
        workflow_id=uuid4(),
        conversation_id="child-conversation",
        platform_session_id="child-session",
        owner_id=uuid4(),
        session_id=uuid4(),
        response_id="previous-child-response",
    )
    report = Report(
        agent="research",
        workflow_id=child.workflow_id,
        owner_id=child.owner_id,
        session_id=child.session_id,
        conversation_id=child.conversation_id,
        platform_session_id=child.platform_session_id,
        progress="ready",
        caller_propagation_verified=True,
        answer="ALPHA",
        children={},
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.responses.create.return_value = SimpleNamespace(
        status="completed", error=None, output_text=report.model_dump_json(), id="actual-child-response"
    )
    project = SimpleNamespace(get_openai_client=MagicMock(return_value=client))
    delegate = SpecialistClient(project, caller, 180)
    updated, answer = await delegate.invoke(child, "What token?", create=False)
    assert answer == "ALPHA"
    assert updated.response_id == "actual-child-response"
    request = client.responses.create.call_args.kwargs
    assert request["conversation"] == child.conversation_id
    assert request["extra_body"] == {"agent_session_id": child.platform_session_id}
    headers = project.get_openai_client.call_args.kwargs["default_headers"]
    assert headers["x-agent-foundry-call-id"] == caller.call_id
    assert headers["x-ms-user-identity"] == caller.user_id
    assert "x-agent-user-id" not in headers
    assert project.get_openai_client.call_args.kwargs["max_retries"] == 0
    report = report.model_copy(update={"caller_propagation_verified": False})
    client.responses.create.return_value.output_text = report.model_dump_json()
    with pytest.raises(WorkflowError, match="caller propagation"):
        await delegate.invoke(child, "What token?", create=False)
