"""Local coordinator contracts with real Responses SDKs and synthetic transports."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from app.services import story6_approval as story6
from app.services.foundry_state import Caller
from app.services.orchestration import HopContext, SubTaskReport
from app.services.workflows import Item, StateConflict, WorkflowError
from openai import AsyncOpenAI, OpenAIError
from pydantic import ValidationError

OPERATION = uuid4()
CALLER = Caller("private-call", "private-owner", "private-session")


class MemoryStore:
    """Conditional fake store; not evidence of platform-user isolation."""

    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.version = 0

    async def read(self, key: str) -> Item | None:
        await asyncio.sleep(0)
        return deepcopy(self.items.get(key))

    async def create(self, key: str, value: dict[str, object]) -> None:
        await asyncio.sleep(0)
        if key in self.items:
            raise StateConflict("Synthetic create conflict.")
        self.version += 1
        self.items[key] = Item(deepcopy(value), str(self.version))

    async def replace(self, key: str, value: dict[str, object], etag: str) -> None:
        await asyncio.sleep(0)
        if key not in self.items or self.items[key].etag != etag:
            raise StateConflict("Synthetic conditional conflict.")
        self.version += 1
        self.items[key] = Item(deepcopy(value), str(self.version))


def command(operation: str, pending: story6.Reply | None = None, *, approve: bool = True) -> story6.Command:
    values: dict[str, object] = {"operation": operation, "operation_id": OPERATION}
    if operation == "decide":
        assert pending is not None and pending.pending is not None
        values.update(approval_ref=pending.pending.reference, approve=approve)
    return story6.Command.model_validate(values)


class Peer:
    """Synthetic Research and native provider endpoints; no real MCP server."""

    def __init__(self, proxy: story6.ToolboxRoute | story6.EntraRoute | None = None) -> None:
        self.proxy = proxy
        self.posts: list[tuple[str, dict]] = []
        self.pending_problem = ""
        self.fail_path = ""
        self.rejected_call = False
        self.changed_arguments = False

    @property
    def tool_name(self):
        if isinstance(self.proxy, story6.EntraRoute):
            return story6.ENTRA_TOOL
        return story6.TOOLBOX_TOOL if self.proxy else story6.TOOL_NAME

    @property
    def arguments(self):
        return (
            {"case_id": str(OPERATION)} if isinstance(self.proxy, story6.EntraRoute) else {"query": story6.SEARCH_QUERY}
        )

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "synthetic.invalid"
        body = json.loads(request.content)
        self.posts.append((request.url.path, body))
        if request.url.path == self.fail_path:
            raise httpx2.ReadError("Synthetic lost result.", request=request)
        if request.url.path == "/research/responses":
            assert request.headers[story6.CORRELATION_HEADER] == str(OPERATION)
            assert body["stream"] is False
            assert "POC_SUBTASK " in body["input"] and story6.RESEARCH_TASK in body["input"]
            context = HopContext(
                agent="research",
                hosted_runtime=True,
                call_id_present=True,
                user_id_present=False,
                session_id_present=True,
                user_fingerprint=None,
                correlation_header_matches=True,
            )
            text = SubTaskReport(
                agent="research", correlation_id=OPERATION, context=context, answer="Synthetic research context."
            ).model_dump_json()
            return self.response([self.message(text)], "resp_research_private")
        assert request.url.path == "/responses"
        assert body["tools"] == [story6.mcp_tool(self.proxy)]
        assert body["tools"][0]["require_approval"] == "always"
        if self.proxy is None:
            assert "headers" not in body["tools"][0]
        else:
            assert body["tools"][0]["headers"] == self.proxy.headers
        assert body["store"] is True and body["stream"] is False and body["max_tool_calls"] == 1
        if "previous_response_id" in body:
            assert body["previous_response_id"] == "resp_pending_private"
            decision = body["input"]
            assert len(decision) == 1 and decision[0]["type"] == "mcp_approval_response"
            assert decision[0]["approval_request_id"] == "approval-private"
            output = [self.message("Synthetic result after decision.")]
            if decision[0]["approve"] or self.rejected_call:
                output.insert(0, self.call())
            return self.response(output, "resp_completed_private")
        tool_name = self.tool_name
        assert body["tool_choice"] == {"type": "mcp", "server_label": story6.SERVER_LABEL, "name": tool_name}
        assert "Synthetic research context." in body["input"]
        approval = {
            "type": "mcp_approval_request",
            "id": "approval-private",
            "name": tool_name,
            "server_label": story6.SERVER_LABEL,
            "arguments": json.dumps(self.arguments),
        }
        output = [approval]
        if self.pending_problem == "wrong-tool":
            approval["name"] = "sign_up"
        elif self.pending_problem == "changed-arguments":
            approval["arguments"] = '{"query":"unexpected"}'
        elif self.pending_problem == "invalid-json":
            approval["arguments"] = "{"
        elif self.pending_problem == "multiple":
            output.append({**approval, "id": "another-approval"})
        elif self.pending_problem == "executed":
            output.append(self.call())
        elif self.pending_problem == "no-pending":
            output = [self.message("No pending approval.")]
        return self.response(output, "resp_pending_private")

    @staticmethod
    def message(text: str) -> dict:
        return {
            "type": "message",
            "id": "message-synthetic",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }

    def call(self) -> dict:
        return {
            "type": "mcp_call",
            "id": "mcp-private",
            "name": self.tool_name,
            "server_label": story6.SERVER_LABEL,
            "arguments": json.dumps({"query": "changed"} if self.changed_arguments else self.arguments),
            "approval_request_id": "approval-private",
            "output": "Synthetic public documentation.",
            "status": "completed",
        }

    @staticmethod
    def response(output: list[dict], identifier: str) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": identifier,
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "synthetic",
                "output": output,
            },
        )


@asynccontextmanager
async def clients(peer: Peer) -> AsyncIterator[tuple[AsyncOpenAI, AsyncOpenAI]]:
    async with (
        AsyncOpenAI(
            api_key="synthetic-not-a-credential",
            base_url="https://synthetic.invalid/research",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(peer.handle), trust_env=False),
        ) as research,
        AsyncOpenAI(
            api_key="synthetic-not-a-credential",
            base_url="https://synthetic.invalid",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(peer.handle), trust_env=False),
        ) as provider,
    ):
        yield research, provider


@pytest.mark.parametrize("approve", [True, False])
async def test_native_pending_inspect_decide_and_duplicate_are_one_shot(tmp_path: Path, approve: bool) -> None:
    store, peer = MemoryStore(), Peer()
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, boot_marker="first-process")
    async with clients(peer) as (research, provider):
        pending = await coordinator.start(command("start"), research, provider, "synthetic")
        assert pending.phase == "pending" and pending.pending is not None
        assert pending.pending.arguments == {"query": story6.SEARCH_QUERY}
        assert pending.provider_responses_observed == 1 and pending.mcp_calls_reported == 0
        assert pending.research_response_observed and pending.research_user_context_present is False
        assert pending.file_retained and pending.same_process
        assert not pending.remote_execution_independently_verified
        assert not pending.delegated_user_authority_verified
        reopened = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, boot_marker="new-process")
        inspected = await reopened.inspect(OPERATION)
        assert inspected.pending == pending.pending and inspected.same_process is False
        assert (await coordinator.start(command("start"), research, provider, "synthetic")) == pending
        assert len(peer.posts) == 2
        result = await coordinator.decide(command("decide", pending, approve=approve), provider, "synthetic")
        assert result.phase == ("completed" if approve else "rejected")
        assert result.decision is approve and result.pending is None
        assert result.mcp_calls_reported == (1 if approve else 0)
        assert result.provider_responses_observed == 2
        assert result.file_sha256 == pending.file_sha256 and result.file_retained
        assert len(peer.posts) == 3
        assert await coordinator.decide(command("decide", pending, approve=approve), provider, "synthetic") == result
        assert len(peer.posts) == 3
        with pytest.raises(WorkflowError, match="Only a pending"):
            await coordinator.decide(command("decide", pending, approve=not approve), provider, "synthetic")
        text = result.model_dump_json() + pending.model_dump_json()
        for secret in ("private-owner", "private-session", "private-call", "private-conversation", "approval-private"):
            assert secret not in text
        assert "resp_pending_private" not in text and "resp_completed_private" not in text


@pytest.mark.parametrize("approve", [True, False])
async def test_entra_route_pending_and_decision_preserve_case_and_refresh_headers(
    tmp_path: Path, approve: bool
) -> None:
    route = story6.EntraRoute(
        "https://fixture.synthetic.azurecontainerapps.io/mcp",
        "api://11111111-1111-4111-8111-111111111111/.default",
        {"Authorization": "Bearer synthetic-start-token"},
    )
    peer, store = Peer(route), MemoryStore()
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=route)
    async with clients(peer) as (research, provider):
        pending = await coordinator.start(command("start"), research, provider, "synthetic")
        assert pending.mcp_route == "entra-mcp"
        assert pending.pending.tool_name == story6.ENTRA_TOOL
        assert pending.pending.arguments == {"case_id": str(OPERATION)}
        assert pending.mcp_calls_reported == 0
        assert (await coordinator.inspect(OPERATION)).pending == pending.pending
        binding = await coordinator.open(OPERATION)
        assert "synthetic-start-token" not in binding.record.model_dump_json()
        assert binding.record.mcp_url == route.url and binding.record.mcp_scope == route.scope
        refreshed = story6.EntraRoute(route.url, route.scope, {"Authorization": "Bearer synthetic-fresh-token"})
        peer.proxy = refreshed
        resumed = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=refreshed)
        result = await resumed.decide(command("decide", pending, approve=approve), provider, "synthetic")
        assert result.phase == ("completed" if approve else "rejected")
        assert result.mcp_calls_reported == int(approve)
        assert len(peer.posts) == 3
        await resumed.decide(command("decide", pending, approve=approve), provider, "synthetic")
        assert len(peer.posts) == 3
        changed = story6.EntraRoute(route.url.replace("fixture.", "other."), route.scope, route.headers)
        with pytest.raises(WorkflowError, match="cannot switch routes"):
            story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=changed).tool_configuration(
                binding.record
            )


@pytest.mark.parametrize("binding", ["user", "session", "conversation"])
async def test_foreign_binding_cannot_inspect_decide_or_recreate(tmp_path: Path, binding: str) -> None:
    store, peer = MemoryStore(), Peer()
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path)
    async with clients(peer) as (research, provider):
        pending = await coordinator.start(command("start"), research, provider, "synthetic")
        caller = Caller(
            CALLER.call_id,
            "foreign" if binding == "user" else CALLER.user_id,
            "foreign" if binding == "session" else CALLER.platform_session_id,
        )
        foreign = story6.Coordinator(
            store, caller, "foreign" if binding == "conversation" else "private-conversation", tmp_path
        )
        for operation in ("inspect", "decide", "start"):
            with pytest.raises(WorkflowError, match="does not belong"):
                if operation == "inspect":
                    await foreign.inspect(OPERATION)
                elif operation == "decide":
                    await foreign.decide(command("decide", pending), provider, "synthetic")
                else:
                    await foreign.start(command("start"), research, provider, "synthetic")
        assert len(peer.posts) == 2


@pytest.mark.parametrize("phase", ["start", "decide"])
async def test_lost_submission_remains_ambiguous_without_replay(tmp_path: Path, phase: str) -> None:
    store, peer = MemoryStore(), Peer()
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path)
    async with clients(peer) as (research, provider):
        if phase == "start":
            peer.fail_path = "/research/responses"
            with pytest.raises(OpenAIError):
                await coordinator.start(command("start"), research, provider, "synthetic")
            assert len(peer.posts) == 1
        else:
            pending = await coordinator.start(command("start"), research, provider, "synthetic")
            peer.fail_path = "/responses"
            with pytest.raises(OpenAIError):
                await coordinator.decide(command("decide", pending), provider, "synthetic")
            with pytest.raises(WorkflowError, match="Only a pending"):
                await coordinator.decide(command("decide", pending), provider, "synthetic")
            assert len(peer.posts) == 3
        attempts = len(peer.posts)
        assert (await coordinator.inspect(OPERATION)).phase == "ambiguous"
        assert (await coordinator.start(command("start"), research, provider, "synthetic")).phase == "ambiguous"
        assert len(peer.posts) == attempts


@pytest.mark.parametrize(
    "problem", ["wrong-tool", "changed-arguments", "invalid-json", "multiple", "executed", "no-pending"]
)
async def test_unexpected_pending_items_stop_without_auto_approval(tmp_path: Path, problem: str) -> None:
    store, peer = MemoryStore(), Peer()
    peer.pending_problem = problem
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path)
    async with clients(peer) as (research, provider):
        with pytest.raises(WorkflowError):
            await coordinator.start(command("start"), research, provider, "synthetic")
        reply = await coordinator.inspect(OPERATION)
        assert reply.phase == "ambiguous" and reply.pending is None
        assert reply.provider_responses_observed == 1
        assert len(peer.posts) == 2


@pytest.mark.parametrize("problem", ["rejected-call", "changed-arguments", "missing-file", "stale-reference"])
async def test_decision_validation_preserves_the_original_operation(tmp_path: Path, problem: str) -> None:
    store, peer = MemoryStore(), Peer()
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path)
    async with clients(peer) as (research, provider):
        pending = await coordinator.start(command("start"), research, provider, "synthetic")
        decision = command("decide", pending, approve=problem != "rejected-call")
        peer.rejected_call = problem == "rejected-call"
        peer.changed_arguments = problem == "changed-arguments"
        if problem == "missing-file":
            coordinator.sentinel(OPERATION).unlink()
        if problem == "stale-reference":
            decision = decision.model_copy(update={"approval_ref": "0" * 64})
        with pytest.raises(WorkflowError):
            await coordinator.decide(decision, provider, "synthetic")
        assert len(peer.posts) == (3 if problem in ("rejected-call", "changed-arguments") else 2)


async def test_conditional_decision_claim_prevents_double_submit(tmp_path: Path) -> None:
    store, peer = MemoryStore(), Peer()
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path)
    async with clients(peer) as (research, provider):
        pending = await coordinator.start(command("start"), research, provider, "synthetic")
        results = await asyncio.gather(
            coordinator.decide(command("decide", pending), provider, "synthetic"),
            coordinator.decide(command("decide", pending), provider, "synthetic"),
            return_exceptions=True,
        )
        assert any(isinstance(result, story6.Reply) for result in results)
        assert len(peer.posts) == 3
        assert (await coordinator.inspect(OPERATION)).phase == "completed"


@pytest.mark.parametrize("field", ["user_id", "conversation_id", "provider_response_id", "query", "tool"])
def test_commands_forbid_spoofed_identity_and_arbitrary_tool_arguments(field: str) -> None:
    with pytest.raises(ValidationError):
        story6.Command.model_validate({"operation": "start", "operation_id": OPERATION, field: "untrusted"})


@pytest.mark.parametrize("value", ["yes", "true", 1, None])
def test_decisions_require_a_strict_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        story6.Command.model_validate(
            {"operation": "decide", "operation_id": OPERATION, "approval_ref": "0" * 64, "approve": value}
        )


async def test_missing_operation_cannot_be_recreated_by_inspect(tmp_path: Path) -> None:
    store = MemoryStore()
    with pytest.raises(WorkflowError, match="missing"):
        await story6.Coordinator(store, CALLER, "conversation", tmp_path).inspect(UUID(str(OPERATION)))
    assert store.items == {}


async def test_retry_enabled_client_is_rejected_before_claim_or_network(tmp_path: Path) -> None:
    store, peer = MemoryStore(), Peer()
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path)
    async with clients(peer) as (research, provider):
        provider.max_retries = 2
        with pytest.raises(WorkflowError, match="disabled SDK retries"):
            await coordinator.start(command("start"), research, provider, "synthetic")
    assert not store.items and not peer.posts


def proxy(version: str = "1", **changes) -> story6.ToolboxRoute:
    values = {
        "project_endpoint": "https://synthetic.services.ai.azure.com/api/projects/poc",
        "version": version,
        "headers": {"Authorization": "Bearer synthetic-private-token", "x-agent-foundry-call-id": "private-call"},
    }
    return story6.ToolboxRoute(**(values | changes))


@pytest.mark.parametrize("approve", [True, False])
async def test_native_toolbox_pending_and_decision_keep_credentials_private(tmp_path: Path, approve: bool) -> None:
    store = MemoryStore()
    route = proxy()
    peer = Peer(route)
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=route)
    async with clients(peer) as (research, provider):
        pending = await coordinator.start(command("start"), research, provider, "synthetic")
        assert pending.mcp_route == "foundry-toolbox"
        assert pending.pending.tool_name == story6.TOOLBOX_TOOL
        fresh = proxy(
            headers={
                "Authorization": "Bearer synthetic-refreshed-token",
                "x-agent-foundry-call-id": "fresh-private-call",
            }
        )
        peer.proxy = fresh
        reopened = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=fresh)
        result = await reopened.decide(command("decide", pending, approve=approve), provider, "synthetic")
        assert result.phase == ("completed" if approve else "rejected")
        assert result.mcp_route == "foundry-toolbox"
        persisted = json.dumps({key: item.value for key, item in store.items.items()})
        public = result.model_dump_json() + pending.model_dump_json()
        for secret in (
            "synthetic-private-token",
            "synthetic-refreshed-token",
            "fresh-private-call",
            route.project_endpoint,
        ):
            assert secret not in persisted and secret not in public
        assert len(peer.posts) == 3


async def test_public_pending_continuation_does_not_switch_to_new_toolbox_configuration(tmp_path: Path) -> None:
    store, peer = MemoryStore(), Peer()
    original = story6.Coordinator(store, CALLER, "private-conversation", tmp_path)
    async with clients(peer) as (research, provider):
        pending = await original.start(command("start"), research, provider, "synthetic")
        configured = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=proxy())
        result = await configured.decide(command("decide", pending), provider, "synthetic")
        assert result.phase == "completed" and result.mcp_route == "public-learn"
        assert all("headers" not in body["tools"][0] for path, body in peer.posts if path == "/responses")


async def test_pending_toolbox_version_cannot_change_during_decision(tmp_path: Path) -> None:
    store, peer = MemoryStore(), Peer(proxy())
    coordinator = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=proxy())
    async with clients(peer) as (research, provider):
        pending = await coordinator.start(command("start"), research, provider, "synthetic")
        changed = story6.Coordinator(store, CALLER, "private-conversation", tmp_path, proxy=proxy("2"))
        with pytest.raises(WorkflowError, match="cannot switch routes"):
            await changed.decide(command("decide", pending), provider, "synthetic")
        assert (await coordinator.inspect(OPERATION)).phase == "pending"
        assert len(peer.posts) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"project_endpoint": "https://learn.microsoft.com/api/mcp"},
        {"project_endpoint": "https://evil.example/api/projects/poc"},
        {"project_endpoint": "http://synthetic.services.ai.azure.com/api/projects/poc"},
        {"project_endpoint": "https://synthetic.services.ai.azure.com/api/projects/poc?redirect=evil"},
        {"version": "../2"},
        {"version": "0"},
        {"version": "1" * 11},
        {"headers": {}},
        {"headers": {"Authorization": "Bearer token\ninjected", "x-agent-foundry-call-id": "call"}},
    ],
)
def test_proxy_credentials_are_confined_to_a_trusted_fixed_version_endpoint(changes) -> None:
    with pytest.raises(WorkflowError):
        story6.mcp_tool(proxy(**changes))
