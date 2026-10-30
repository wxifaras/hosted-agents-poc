"""Offline Story6 qualification with real SDKs and explicitly synthetic peers.

No sockets, credentials, Azure resources or human decisions are used. Results
characterize client behavior, never Foundry approval enforcement.
"""

import json
from dataclasses import dataclass, field
from importlib.metadata import version

import httpx
import httpx2
from agent_framework import Agent, AgentSession, Content, MCPStreamableHTTPTool, Message
from agent_framework.exceptions import ChatClientInvalidRequestException
from agent_framework.foundry import FoundryChatClient
from agent_framework.openai import OpenAIChatClient
from anyio import ClosedResourceError
from mcp import ClientSession
from mcp.types import CallToolResult, TextContent
from openai import AsyncOpenAI
from pydantic import JsonValue

from scripts.common.reporting import Status

TOOL_NAME = "publish_mock_result"
PACKAGES = (
    "agent-framework-core",
    "agent-framework-foundry",
    "agent-framework-foundry-hosting",
    "azure-ai-projects",
    "azure-ai-agentserver-responses",
    "mcp",
    "openai",
    "httpx",
    "httpx2",
)


@dataclass(frozen=True)
class Qualification:
    """One local observation; its owner and evidence strength remain explicit."""

    name: str
    status: Status
    expected: str
    observed: dict[str, JsonValue]


def package_versions() -> dict[str, str]:
    """Read installed versions without resolving credentials or contacting a feed."""
    return {package: version(package) for package in PACKAGES}


def provider_tool_configuration() -> dict[str, JsonValue]:
    """Exercise the locked Foundry SDK's native remote-MCP configuration factory."""
    tool = FoundryChatClient.get_mcp_tool(
        name="story6-synthetic",
        url="https://synthetic.invalid/mcp",
        approval_mode="always_require",
        allowed_tools=[TOOL_NAME],
    )
    serialized: dict[str, JsonValue] = json.loads(json.dumps(tool.as_dict()))
    return serialized


@dataclass
class SyntheticPeer:
    """A synthetic model/MCP peer reached only through httpx.MockTransport."""

    provider_approval: bool = False
    model_requests: list[dict[str, JsonValue]] = field(default_factory=list)
    mcp_calls: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Return fixed SDK wire responses; never open a real connection."""
        if request.url.host != "synthetic.invalid":
            raise ValueError("Offline qualification accepts only its synthetic peer.")
        if request.url.path != "/mcp":
            raise ValueError("Unexpected synthetic route.")
        if request.method == "DELETE":
            return httpx.Response(204)
        body: dict[str, JsonValue] = json.loads(request.content)
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        result: JsonValue
        if method == "ping":
            result = {}
        elif method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "story6-synthetic", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": "Publish a synthetic receipt only.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"marker": {"type": "string"}},
                            "required": ["marker"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            self.mcp_calls += 1
            result = {"content": [{"type": "text", "text": "synthetic receipt"}], "isError": False}
        else:
            raise ValueError(f"Unexpected synthetic MCP method: {method}.")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def model_response(self, request: httpx2.Request) -> httpx2.Response:
        """Supply one approval/tool request, then a completed text response."""
        if request.url.host != "synthetic.invalid" or request.url.path != "/responses":
            raise ValueError("Unexpected synthetic model route.")
        body: dict[str, JsonValue] = json.loads(request.content)
        self.model_requests.append(body)
        output: list[dict[str, JsonValue]]
        if len(self.model_requests) == 1:
            if self.provider_approval:
                output = [
                    {
                        "type": "mcp_approval_request",
                        "id": "approval-synthetic",
                        "name": TOOL_NAME,
                        "server_label": "story6-synthetic",
                        "arguments": '{"marker":"synthetic"}',
                    }
                ]
            else:
                output = [
                    {
                        "type": "function_call",
                        "id": "function-synthetic",
                        "call_id": "call-synthetic",
                        "name": TOOL_NAME,
                        "arguments": '{"marker":"synthetic"}',
                        "status": "completed",
                    }
                ]
        else:
            output = [
                {
                    "type": "message",
                    "id": "message-synthetic",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Synthetic completion.", "annotations": []}],
                }
            ]
        return httpx2.Response(
            200,
            json={
                "id": f"resp_synthetic_{len(self.model_requests)}",
                "object": "response",
                "created_at": 1,
                "model": "synthetic-model",
                "status": "completed",
                "output": output,
            },
        )


def restored_pending(request: Content) -> Content:
    """Round-trip the real SDK's approval content through JSON, not chat text."""
    return Content.from_dict(json.loads(json.dumps(request.to_dict())))


async def local_approval_case(*, approve: bool, wrong_session: bool = False) -> dict[str, JsonValue]:
    """Exercise framework MCP approval, serialization and continuation offline."""
    peer = SyntheticPeer()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(peer.handle), trust_env=False) as mcp_http,
        AsyncOpenAI(
            api_key="synthetic-not-a-credential",
            base_url="https://synthetic.invalid",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(peer.model_response), trust_env=False),
        ) as api,
        MCPStreamableHTTPTool(
            name="story6-synthetic",
            url="https://synthetic.invalid/mcp",
            approval_mode="always_require",
            load_prompts=False,
            allowed_tools=[TOOL_NAME],
            http_client=mcp_http,
        ) as mcp_tool,
    ):
        client = OpenAIChatClient(model="synthetic-model", async_client=api)
        agent = Agent(client=client, name="story6-local-qualification", tools=[mcp_tool])
        session = agent.create_session()
        pending = await agent.run("Publish the synthetic marker.", session=session)
        requests = pending.user_input_requests
        if len(requests) != 1:
            raise ValueError("The locked SDK did not return exactly one local approval request.")
        before = peer.mcp_calls
        serialized = json.loads(json.dumps(session.to_dict()))
        continued_session = AgentSession() if wrong_session else AgentSession.from_dict(serialized)
        decision = restored_pending(requests[0]).to_function_approval_response(approve)
        outcome = "continued"
        try:
            await agent.run(Message("user", [decision]), session=continued_session)
        except ChatClientInvalidRequestException:
            if not wrong_session:
                raise
            outcome = "unbound response removed; SDK rejected empty continuation"
        return {
            "evidence": "real SDK / synthetic model and MCP peer",
            "owner": "Agent Framework / SDK-managed",
            "pending_requests": len(requests),
            "calls_before_decision": before,
            "calls_after_decision": peer.mcp_calls,
            "session_round_trip": not wrong_session,
            "wrong_session": wrong_session,
            "outcome": outcome,
            "human_approval_tested": False,
            "foundry_enforcement_tested": False,
        }


async def provider_approval_case(*, approve: bool) -> dict[str, JsonValue]:
    """Decode and re-encode native MCP approval using the real Responses client."""
    peer = SyntheticPeer(provider_approval=True)
    async with AsyncOpenAI(
        api_key="synthetic-not-a-credential",
        base_url="https://synthetic.invalid",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(peer.model_response), trust_env=False),
    ) as api:
        client = OpenAIChatClient(model="synthetic-model", async_client=api)
        agent = Agent(client=client, name="story6-provider-wire")
        session = agent.create_session()
        response = await agent.run("Request a synthetic remote operation.", session=session)
        if len(response.user_input_requests) != 1:
            raise ValueError("The provider approval item was not preserved by the SDK.")
        pending = restored_pending(response.user_input_requests[0])
        await agent.run(
            Message("user", [pending.to_function_approval_response(approve)]),
            session=AgentSession.from_dict(json.loads(json.dumps(session.to_dict()))),
        )
        decision_input = peer.model_requests[-1].get("input")
        decisions = (
            [item for item in decision_input if isinstance(item, dict) and item.get("type") == "mcp_approval_response"]
            if isinstance(decision_input, list)
            else []
        )
        matched = len(decisions) == 1 and decisions[0].get("approval_request_id") == "approval-synthetic"
        return {
            "evidence": "real SDK / synthetic Responses wire",
            "decision_bound": matched and decisions[0].get("approve") is approve,
            "pending_visible_as_text": bool(response.text),
            "provider_execution_tested": False,
            "foundry_enforcement_tested": False,
        }


async def direct_wrapper_case() -> dict[str, JsonValue]:
    """Show that an agent's local approval mode does not gate direct SDK calls."""
    peer = SyntheticPeer()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(peer.handle), trust_env=False) as http,
        MCPStreamableHTTPTool(
            name="story6-synthetic",
            url="https://synthetic.invalid/mcp",
            approval_mode="always_require",
            load_prompts=False,
            http_client=http,
        ) as wrapper,
    ):
        await wrapper.call_tool(TOOL_NAME, marker="synthetic")
    return {
        "evidence": "real SDK / synthetic MCP peer, no authentication",
        "calls_without_agent_approval": peer.mcp_calls,
        "live_valid_token_bypass_tested": False,
    }


async def retry_case() -> dict[str, JsonValue]:
    """Exercise the installed wrapper's resend using a synthetic session double."""
    from unittest.mock import AsyncMock, MagicMock, patch

    session = MagicMock(spec=ClientSession)
    session.call_tool = AsyncMock(
        side_effect=[
            ClosedResourceError(),
            CallToolResult(content=[TextContent(type="text", text="synthetic receipt")]),
        ]
    )
    wrapper = MCPStreamableHTTPTool(name="story6-retry", url="https://synthetic.invalid/mcp", session=session)
    with patch.object(wrapper, "connect", new_callable=AsyncMock) as reconnect:
        await wrapper.call_tool(TOOL_NAME, marker="synthetic")
        return {
            "evidence": "real SDK wrapper / synthetic session double",
            "call_attempts": session.call_tool.await_count,
            "reconnections": reconnect.await_count,
            "remote_effects_known": False,
        }


async def qualify() -> list[Qualification]:
    """Run bounded, credential-free client-contract experiments."""
    checks: list[Qualification] = []
    configuration = provider_tool_configuration()
    checks.append(
        Qualification(
            "Native MCP approval configuration",
            "PASS" if configuration.get("require_approval") == "always" else "FAIL",
            "Locked Foundry SDK encodes require_approval=always",
            {"evidence": "local SDK serialization", "configuration": configuration},
        )
    )
    for approve in (True, False):
        observed = await local_approval_case(approve=approve)
        expected_calls = 1 if approve else 0
        ok = observed["calls_before_decision"] == 0 and observed["calls_after_decision"] == expected_calls
        checks.append(
            Qualification(
                f"Framework MCP {'approval' if approve else 'rejection'} and session round-trip",
                "PASS" if ok else "FAIL",
                f"Zero calls while pending; {expected_calls} synthetic call(s) after decision",
                observed,
            )
        )
        provider = await provider_approval_case(approve=approve)
        checks.append(
            Qualification(
                f"Provider MCP {'approve' if approve else 'reject'} wire round-trip",
                "PASS" if provider["decision_bound"] is True else "FAIL",
                "Structured pending item survives and the decision keeps its approval binding",
                provider,
            )
        )
    unbound = await local_approval_case(approve=True, wrong_session=True)
    checks.append(
        Qualification(
            "Framework rejects an unbound decision",
            "PASS" if unbound["calls_after_decision"] == 0 else "FAIL",
            "A different local session does not execute the pending tool",
            unbound,
        )
    )
    direct = await direct_wrapper_case()
    checks.append(
        Qualification(
            "Direct SDK call is outside the framework agent gate",
            "PASS" if direct["calls_without_agent_approval"] == 1 else "FAIL",
            "Characterize direct SDK dispatch separately; not a live authenticated bypass result",
            direct,
        )
    )
    retry = await retry_case()
    checks.append(
        Qualification(
            "Ordinary MCP wrapper single-send requirement",
            "PASS" if retry["call_attempts"] == 1 else "FAIL",
            "At most one underlying call attempt after an ambiguous connection loss",
            retry,
        )
    )
    return checks
