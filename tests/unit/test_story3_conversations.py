"""Story 3 conversation protocol tests; the state store, specialist endpoints, and conversations are in-memory fakes."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from app import hosting
from app.core.settings import Settings
from app.services import conversations, orchestration
from app.services.conversations import (
    HOP_TAG,
    MAP_PREFIX,
    ConversationReport,
    ConverseCommand,
    HistoryStats,
    Turn,
    conversation_digest,
    converse_turn,
    history_stats,
    separate_turn,
    shared_turn,
)
from app.services.orchestration import CORRELATION_HEADER, HopContext
from app.services.workflows import Item, StateConflict, WorkflowError
from openai import APIStatusError, BadRequestError
from pydantic import ValidationError

CORRELATION = uuid4()
REQUEST = httpx.Request("POST", "https://example.invalid/conversations")


def status_error(code: int, error: str) -> APIStatusError:
    return APIStatusError(error, response=httpx.Response(code, request=REQUEST), body={"error": {"code": error}})


class FakeStore:
    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.version = 0

    async def read(self, key: str) -> Item | None:
        return self.items.get(key)

    async def create(self, key: str, value: dict[str, object]) -> None:
        if key in self.items:
            raise StateConflict("exists")
        self.version += 1
        self.items[key] = Item(value, f"etag-{self.version}")

    async def replace(self, key: str, value: dict[str, object], etag: str) -> None:
        if self.items[key].etag != etag:
            raise StateConflict("etag")
        self.version += 1
        self.items[key] = Item(value, f"etag-{self.version}")


class Platform:
    """Conversations by ID, with the IDs this specialist identity may not access."""

    def __init__(self, *, reject_metadata: bool = False, denied: set[str] | None = None) -> None:
        self.items: dict[str, list[dict[str, Any]]] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.reject_metadata = reject_metadata
        self.denied = denied or set()
        self.inputs: list[tuple[str, str]] = []
        self.created = 0

    def message(self, role: str, text: str) -> dict[str, Any]:
        kind = "input_text" if role == "user" else "output_text"
        return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


class FakeItems:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    def list(self, conversation_id: str, limit: int = 100) -> AsyncIterator[dict[str, Any]]:
        async def generate() -> AsyncIterator[dict[str, Any]]:
            if conversation_id in self.platform.denied or conversation_id not in self.platform.items:
                raise status_error(404, "not_found")
            for item in list(self.platform.items[conversation_id]):
                yield item

        return generate()


class FakeConversations:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self.items = FakeItems(platform)

    async def create(self, metadata: dict[str, str] | None = None) -> Any:
        if metadata is not None and self.platform.reject_metadata:
            raise BadRequestError("metadata", response=httpx.Response(400, request=REQUEST), body=None)
        self.platform.created += 1
        conversation_id = f"conv_child_{self.platform.created}"
        self.platform.items[conversation_id] = []
        self.platform.metadata[conversation_id] = dict(metadata or {})
        return SimpleNamespace(id=conversation_id)

    async def retrieve(self, conversation_id: str) -> Any:
        return SimpleNamespace(metadata=self.platform.metadata.get(conversation_id))


class FakeResponses:
    def __init__(self, platform: Platform, answer: str) -> None:
        self.platform = platform
        self.answer = answer

    async def create(self, *, input: str, conversation: str, stream: bool) -> Any:
        if conversation in self.platform.denied:
            raise status_error(403, "forbidden")
        self.platform.inputs.append((conversation, input))
        history = self.platform.items.setdefault(conversation, [])
        history.append(self.platform.message("user", input))
        history.append(self.platform.message("assistant", self.answer))
        return SimpleNamespace(id="resp_sub", status="completed", output_text=self.answer, error=None)


class FakeClient:
    def __init__(self, platform: Platform, answer: str) -> None:
        self.conversations = FakeConversations(platform)
        self.responses = FakeResponses(platform, answer)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def hop() -> HopContext:
    return HopContext(
        agent="supervisor",
        hosted_runtime=True,
        call_id_present=True,
        user_id_present=True,
        session_id_present=True,
        user_fingerprint="f",
        correlation_header_matches=True,
    )


def turn(command: ConverseCommand, conversation: str = "conv_sup") -> Turn:
    return Turn(command=command, supervisor=hop(), conversation_id=conversation, history=HistoryStats())


def command(**fields: Any) -> ConverseCommand:
    return ConverseCommand(correlation_id=CORRELATION, **{"message": "hello", **fields})


def factory(platform: Platform, answer: str = "answer CODE-1234") -> Any:
    return lambda: FakeClient(platform, answer)


def test_command_validation_forbids_identity_fields_and_bad_ids() -> None:
    with pytest.raises(ValidationError):
        ConverseCommand.model_validate({"correlation_id": str(CORRELATION), "user_id": "spoof"})
    with pytest.raises(ValidationError):
        command(sub_conversation_id="bad id!")
    with pytest.raises(ValidationError):
        command(probe_tokens=["lowercase"])
    with pytest.raises(ValidationError):
        command(identity="delegated-user")
    default = command()
    assert default.pattern == "separate" and default.identity == "agent" and default.deliver == "result"
    assert default.wire().startswith("POC_CONVERSE ")


def test_history_stats_classify_origin_and_never_echo_tokens() -> None:
    platform = Platform()
    items = [
        platform.message("user", 'POC_CONVERSE {"message": "x CODE-1"}'),
        platform.message("assistant", '{"protocol": "poc-conversation-v1", "final_answer": "y"}'),
        platform.message("user", f"{HOP_TAG} what is CODE-1?"),
        platform.message("assistant", "CODE-1"),
        {"type": "function_call", "content": None},
        "not a mapping",
    ]
    stats = history_stats(items, ["CODE-1", "ABSENT"])
    assert (stats.items, stats.user_messages, stats.assistant_messages) == (5, 2, 2)
    assert (stats.hop_inputs, stats.protocol_replies, stats.other_assistant) == (1, 1, 1)
    assert stats.token_hits == [3, 0] and stats.reply_token_hits == [0, 0]
    assert "CODE-1" not in stats.model_dump_json()
    reply = platform.message("assistant", '{"protocol": "poc-conversation-v1", "sub_answer": "CODE-1"}')
    assert history_stats([*items, reply], ["CODE-1"]).reply_token_hits == [1]


async def test_separate_creates_maps_and_reuses_one_child_conversation() -> None:
    platform, store = Platform(), FakeStore()
    first = ConversationReport.model_validate_json(
        await separate_turn(turn(command(probe_tokens=["CODE-1234"])), factory(platform), store)
    )
    assert first.status == "completed" and first.mapping and first.mapping.created_now and first.mapping.found is False
    sub = first.sub_conversation
    assert sub and sub.created_now and sub.conversation_id == "conv_child_1" and sub.metadata_linked and sub.turns == 1
    assert first.conversation_used == "conv_child_1" and first.tokens_in_sub_answer == [True]
    assert first.listing_before and first.listing_before.items == 0
    assert first.listing_after and first.listing_after.items == 2
    assert first.sub_answer == first.final_answer == "answer CODE-1234"
    assert platform.inputs == [("conv_child_1", f"{HOP_TAG} hello")]
    key = MAP_PREFIX + conversation_digest("conv_sup")
    assert first.mapping.key == key and key in store.items
    assert platform.metadata["conv_child_1"]["parent_conversation_digest"] == conversation_digest("conv_sup")
    assert platform.metadata["conv_child_1"]["correlation_id"] == str(CORRELATION)
    second = ConversationReport.model_validate_json(
        await separate_turn(turn(command(message="again")), factory(platform), store)
    )
    assert second.mapping and second.mapping.found and not second.mapping.created_now
    assert second.sub_conversation and not second.sub_conversation.created_now
    assert second.sub_conversation.conversation_id == "conv_child_1" and second.sub_conversation.turns == 2
    assert second.listing_before and second.listing_before.items == 2 and platform.created == 1
    other = ConversationReport.model_validate_json(
        await separate_turn(turn(command(operation="inspect"), "conv_other"), factory(platform), store)
    )
    assert other.status == "inspected" and other.mapping and not other.mapping.found and other.sub_conversation is None


async def test_placeholder_delivery_keeps_the_answer_out_of_the_supervisor_reply() -> None:
    report = ConversationReport.model_validate_json(
        await separate_turn(
            turn(command(deliver="placeholder", probe_tokens=["CODE-1234"])), factory(Platform()), FakeStore()
        )
    )
    assert report.status == "completed" and report.sub_answer is None and report.tokens_in_sub_answer == [True]
    assert "CODE-1234" not in report.model_dump_json() and "resp_sub" in report.final_answer


async def test_inspect_lists_the_mapped_child_and_reads_its_metadata() -> None:
    platform, store = Platform(), FakeStore()
    await separate_turn(turn(command()), factory(platform), store)
    report = ConversationReport.model_validate_json(
        await separate_turn(turn(command(operation="inspect", message="")), factory(platform), store)
    )
    assert report.status == "inspected" and report.response_id is None and platform.inputs[1:] == []
    assert report.listing_before and report.listing_before.items == 2
    assert report.sub_conversation and report.sub_conversation.metadata
    assert report.sub_conversation.metadata["created_by"] == "supervisor-agent"
    platform.denied.add("conv_child_1")
    denied = ConversationReport.model_validate_json(
        await separate_turn(turn(command(operation="inspect", message="")), factory(platform), store)
    )
    failure = denied.listing_before.failure if denied.listing_before else None
    assert failure and failure.http_status == 404 and failure.code == "not_found"
    assert denied.sub_conversation and denied.sub_conversation.metadata is None


async def test_unmapped_or_foreign_child_claims_are_denied_before_any_call() -> None:
    platform, store = Platform(), FakeStore()
    claim = ConversationReport.model_validate_json(
        await separate_turn(turn(command(sub_conversation_id="conv_forged")), factory(platform), store)
    )
    assert claim.status == "denied" and claim.denial and claim.response_id is None
    assert platform.inputs == [] and platform.created == 0 and store.items == {}
    await separate_turn(turn(command()), factory(platform), store)
    foreign = ConversationReport.model_validate_json(
        await separate_turn(
            turn(command(operation="inspect", sub_conversation_id="conv_child_1"), "conv_other"),
            factory(platform),
            store,
        )
    )
    assert foreign.status == "denied" and len(platform.inputs) == 1
    matching = ConversationReport.model_validate_json(
        await separate_turn(turn(command(sub_conversation_id="conv_child_1")), factory(platform), store)
    )
    assert matching.status == "completed" and len(platform.inputs) == 2


async def test_metadata_rejection_falls_back_and_failures_are_not_retried() -> None:
    platform, store = Platform(reject_metadata=True), FakeStore()
    report = ConversationReport.model_validate_json(await separate_turn(turn(command()), factory(platform), store))
    assert report.sub_conversation and report.sub_conversation.metadata_linked is False
    platform.denied.add("conv_child_1")
    failed = ConversationReport.model_validate_json(await separate_turn(turn(command()), factory(platform), store))
    assert failed.status == "sub_agent_failed" and failed.failure and failed.failure.http_status == 403
    assert failed.failure.retried is False and failed.sub_answer is None and "did not retry" in failed.final_answer
    assert failed.sub_conversation and failed.sub_conversation.turns == 1


async def test_shared_pattern_uses_the_supervisor_conversation() -> None:
    platform = Platform()
    platform.items["conv_sup"] = [platform.message("user", "code word CODE-9")]
    report = ConversationReport.model_validate_json(
        await shared_turn(turn(command(pattern="shared", deliver="placeholder")), factory(platform, "CODE-9"))
    )
    assert report.status == "completed" and report.conversation_used == "conv_sup" and report.mapping is None
    assert report.listing_before and report.listing_before.items == 1
    assert report.listing_after and report.listing_after.items == 3
    assert history_stats(platform.items["conv_sup"]).hop_inputs == 1
    platform.denied.add("conv_sup")
    denied = ConversationReport.model_validate_json(
        await shared_turn(turn(command(pattern="shared")), factory(platform))
    )
    assert denied.status == "sub_agent_failed" and denied.failure and denied.failure.http_status == 403
    assert denied.listing_before and denied.listing_before.failure
    inspected = ConversationReport.model_validate_json(
        await shared_turn(turn(command(pattern="shared", operation="inspect")), factory(platform))
    )
    assert inspected.status == "inspected" and inspected.conversation_used is None


def ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    context = SimpleNamespace(call_id="secret-call", user_id="secret-user", session_id="secret-session")
    monkeypatch.setattr(orchestration, "get_request_context", lambda: context)
    monkeypatch.setattr(orchestration.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=True))


def request_context(conversation: str | None, history: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=conversation,
        client_headers={CORRELATION_HEADER: str(CORRELATION)},
        get_history=AsyncMock(return_value=history or []),
    )


async def test_converse_turn_guards_fail_before_any_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    monkeypatch.setattr(conversations, "DefaultAzureCredential", AsyncMock(side_effect=AssertionError("no egress")))
    with pytest.raises(WorkflowError, match="Only the Supervisor"):
        await converse_turn("research", Settings(), command(), request_context("c"))  # type: ignore[arg-type]
    with pytest.raises(WorkflowError, match="explicit Supervisor conversation"):
        await converse_turn("supervisor", Settings(), command(), request_context(None))  # type: ignore[arg-type]
    with pytest.raises(WorkflowError, match="nonempty message"):
        await converse_turn("supervisor", Settings(), command(message=" "), request_context("c"))  # type: ignore[arg-type]
    with pytest.raises(WorkflowError, match="omit sub_conversation_id"):
        await converse_turn(
            "supervisor",
            Settings(),
            command(pattern="shared", sub_conversation_id="conv_x"),
            request_context("c"),  # type: ignore[arg-type]
        )


async def test_converse_turn_wires_identity_store_and_supervisor_history(monkeypatch: pytest.MonkeyPatch) -> None:
    ambient(monkeypatch)
    platform, store = Platform(), FakeStore()
    seen: dict[str, Any] = {}

    class Credential:
        async def __aenter__(self) -> Credential:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Project:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> Project:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def get_openai_client(self, **kwargs: Any) -> FakeClient:
            seen.update(kwargs)
            return FakeClient(platform, "ok")

    @asynccontextmanager
    async def open_store(agent: str, _credential: Any, *, create: bool) -> AsyncIterator[FakeStore]:
        seen["store"] = (agent, create)
        yield store

    monkeypatch.setattr(conversations, "DefaultAzureCredential", Credential)
    monkeypatch.setattr(conversations, "AIProjectClient", Project)
    monkeypatch.setattr(conversations, "open_store", open_store)
    history = [platform.message("user", f"{HOP_TAG} x"), platform.message("assistant", "y")]
    text = await converse_turn(
        "supervisor",
        Settings(),
        command(identity="call-context"),
        request_context("conv_sup", history),  # type: ignore[arg-type]
    )
    report = ConversationReport.model_validate_json(text)
    assert report.status == "completed" and report.identity == "call-context" and seen["store"] == ("supervisor", True)
    assert seen["agent_name"] == "research-agent" and seen["max_retries"] == 0
    assert seen["default_headers"]["x-agent-foundry-call-id"] == "secret-call"
    assert report.supervisor.correlation_header_matches and report.supervisor_history.hop_inputs == 1
    assert "secret" not in text


async def test_host_routes_converse_rejects_background_and_hydrates_readable_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient(monkeypatch)
    server = SimpleNamespace()

    def register(handler: Any) -> Any:
        server.handler = handler
        return handler

    server.response_handler = register
    monkeypatch.setattr(hosting, "ResponsesAgentServerHost", lambda **_kwargs: server)
    agent = SimpleNamespace(run=AsyncMock(side_effect=AssertionError("no model call")))
    hosting.build_host("supervisor", Settings(), agent, lambda **_kwargs: None)

    async def send(text: str, request: dict[str, Any], conversation: str | None) -> dict[str, Any]:
        context = SimpleNamespace(
            response_id="response",
            conversation_id=conversation,
            client_headers={},
            is_recovery=False,
            get_input_text=AsyncMock(return_value=text),
            get_history=AsyncMock(return_value=[]),
        )
        stream = await server.handler(request, context, asyncio.Event())
        return [event async for event in stream][-1]["response"]

    probe = ConverseCommand(operation="inspect", correlation_id=CORRELATION).wire()
    missing = await send(probe, {}, None)
    assert missing["status"] == "failed" and "explicit Supervisor conversation" in missing["error"]["message"]
    background = await send(probe, {"background": True}, "conv")
    assert background["status"] == "failed" and "background" in background["error"]["message"]
    invalid = await send("POC_CONVERSE {}", {}, "conv")
    assert invalid["status"] == "failed" and "Invalid POC conversation command" in invalid["error"]["message"]
    reply = ConversationReport(
        correlation_id=CORRELATION,
        operation="send",
        status="completed",
        pattern="separate",
        identity="agent",
        deliver="result",
        target_agent="research-agent",
        supervisor=hop(),
        supervisor_conversation_id="conv",
        supervisor_history=HistoryStats(),
        final_answer="readable answer",
    ).model_dump_json()
    assert hosting._visible_text(command(message="readable question").wire(), "user") == "readable question"
    assert hosting._visible_text(reply, "assistant") == "readable answer"
    assert hosting._visible_text("POC_CONVERSE {broken", "user") == "POC_CONVERSE {broken"
