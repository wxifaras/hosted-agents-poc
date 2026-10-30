"""Direct authenticated toolbox comparison: permission is not human approval.

Exactly two fixed read-only calls are possible. No automatic retries, SDK
reconnect wrapper, mutable target URLs, approval messages or policy changes.
"""

import asyncio
import json
from collections.abc import Callable

import httpx
from app.services.story6_approval import SEARCH_QUERY, TOOLBOX_NAME, TOOLBOX_TOOL
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import AzureCliCredential
from pydantic import JsonValue

from scripts.common.reporting import Status
from scripts.deploy.story6_package import ROOT, deployment_context
from scripts.deploy.story6_toolbox import verify_version
from scripts.run_scenario.native_user_isolation import ProfileCredential, prepare_user

Record = Callable[[str, Status, str, object], None]


class RPC:
    """Minimal bounded MCP-over-HTTP probe, accepting JSON or a single SSE result."""

    def __init__(self, client: httpx.AsyncClient, endpoint: str) -> None:
        self.client = client
        self.endpoint = endpoint
        self.next_id = 0

    async def request(self, method: str, params: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
        """One HTTP send; any ambiguous response stops rather than reconnects."""
        self.next_id += 1
        body: dict[str, JsonValue] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            body["params"] = params
        response = await self.client.post(self.endpoint, json=body)
        response.raise_for_status()
        if len(response.content) > 1024 * 1024:
            raise ValueError("MCP probe response exceeds its bounded evidence limit.")
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            candidates = [
                json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")
            ]
            messages = [item for item in candidates if isinstance(item, dict) and item.get("id") == self.next_id]
            if len(messages) != 1:
                raise ValueError("MCP event stream did not contain exactly one matching response.")
            value: object = messages[0]
        else:
            value = response.json()
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or value.get("id") != self.next_id:
            raise ValueError("MCP returned an incompatible response; no resend allowed.")
        session = response.headers.get("mcp-session-id")
        if session:
            self.client.headers["mcp-session-id"] = session
        result: dict[str, JsonValue] = value
        return result

    async def initialize(self) -> None:
        """Initialize once without receiving or granting any tool approval."""
        response = await self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "story6-authorized-direct-probe", "version": "1"},
            },
        )
        if "error" in response:
            raise ValueError("MCP initialization was rejected before the tool boundary.")
        result = response.get("result")
        protocol = result.get("protocolVersion") if isinstance(result, dict) else None
        if isinstance(protocol, str):
            self.client.headers["mcp-protocol-version"] = protocol
        initialized = await self.client.post(
            self.endpoint,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            },
        )
        initialized.raise_for_status()


def successful_result(response: dict[str, JsonValue]) -> bool:
    """A completed search result is not inferred from HTTP200 alone."""
    result = response.get("result")
    return (
        "error" not in response
        and isinstance(result, dict)
        and result.get("isError") is not True
        and isinstance(result.get("content"), list)
        and bool(result["content"])
    )


async def execute(environment: str, caller_user: str, record: Record, *, timeout: float) -> str:
    """Verify approval metadata, allowed direct calls and a separate permission control."""
    context = deployment_context(ROOT / ".azure" / environment / ".env", environment)
    endpoint = (
        context["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/") + f"/toolboxes/{TOOLBOX_NAME}/versions/1/mcp?api-version=v1"
    )
    async with (
        AzureCliCredential(process_timeout=60) as credential,
        AIProjectClient(
            endpoint=context["AZURE_AI_PROJECT_ENDPOINT"],
            credential=credential,
            allow_preview=True,
            retry_total=0,
            connection_timeout=15,
            read_timeout=25,
        ) as project,
    ):
        version = verify_version((await project.toolboxes.get_version(TOOLBOX_NAME, "1")).as_dict())
        record(
            "Approval-required proxy definition",
            "PASS",
            "Exact isolated immutable search-only toolbox",
            {"version": version, "require_approval": "always", "upstream": "public Learn"},
        )
        token = await credential.get_token("https://ai.azure.com/.default")
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json, text/event-stream"},
            timeout=timeout,
            follow_redirects=False,
            max_redirects=0,
        ) as http:
            rpc = RPC(http, endpoint)
            await rpc.initialize()
            listed = await rpc.request("tools/list")
            result = listed.get("result")
            tools = result.get("tools") if isinstance(result, dict) else None
            found = (
                [item for item in tools if isinstance(item, dict) and item.get("name") == TOOLBOX_TOOL]
                if isinstance(tools, list)
                else []
            )
            meta = found[0].get("_meta") if len(found) == 1 else None
            config = meta.get("tool_configuration") if isinstance(meta, dict) else None
            always = isinstance(config, dict) and config.get("require_approval") == "always"
            record(
                "Runtime MCP approval metadata",
                "PASS" if always else "FAIL",
                "tools/list advertises require_approval=always",
                {"metadata_matches": always, "authorization": "operator Entra token"},
            )
            if not always:
                raise ValueError("The direct route is not the expected approval-required tool.")
            control = await rpc.request("tools/call", {"name": TOOLBOX_TOOL, "arguments": {"query": SEARCH_QUERY}})
            allowed = successful_result(control)
            record(
                "Authorized direct tool control",
                "PASS" if allowed else "FAIL",
                "Valid-token caller has ordinary permission to run the fixed read-only tool",
                {"result_returned": allowed, "approval_message_submitted": False},
            )
            if not allowed:
                raise ValueError("Ordinary tool permission control failed; no approval enforcement verdict.")
            unapproved = await rpc.request("tools/call", {"name": TOOLBOX_TOOL, "arguments": {"query": SEARCH_QUERY}})
            executed = successful_result(unapproved)
            result = unapproved.get("result")
            error = unapproved.get("error")
            detail = json.dumps(error if error is not None else result).casefold()
            approval_denial = not executed and ("approval required" in detail or "approval_required" in detail)
            record(
                "Direct valid-token call without approval",
                "FAIL" if executed or not approval_denial else "PASS",
                "Approval gate blocks direct execution if enforced at this proxy boundary",
                {
                    "unapproved_result_returned": executed,
                    "approval_message_submitted": False,
                    "explicit_approval_denial": approval_denial,
                    "enforced_by": "not enforced by toolbox proxy"
                    if executed
                    else "explicit approval denial"
                    if approval_denial
                    else "inconclusive",
                    "authentication_is_not_approval": True,
                },
            )

    if caller_user:
        user = await asyncio.to_thread(prepare_user, caller_user, context["AZURE_TENANT_ID"])
        async with ProfileCredential(user) as user_credential:
            token = await user_credential.get_token("https://ai.azure.com/.default")
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json, text/event-stream"},
                timeout=timeout,
                follow_redirects=False,
                max_redirects=0,
            ) as http:
                try:
                    rpc = RPC(http, endpoint)
                    await rpc.initialize()
                    response = await rpc.request("tools/list")
                except httpx.HTTPStatusError as failure:
                    denied = failure.response.status_code == 403
                    record(
                        "Invoke-only user direct toolbox permission",
                        "PASS" if denied else "FAIL",
                        "Separate ordinary resource-authorization control, not approval denial",
                        {
                            "http_status": failure.response.status_code,
                            "permission_denied": denied,
                            "approval_enforcement_tested": False,
                            "actor": "existing Consumer user",
                        },
                    )
                else:
                    record(
                        "Invoke-only user direct toolbox permission",
                        "BLOCKED",
                        "Establish explicit denied ordinary tool/resource access",
                        {
                            "permission_denied": False,
                            "rpc_error": "error" in response,
                            "note": "Caller may have covering access; no user elevation or assumed denial.",
                        },
                    )
    else:
        record(
            "Denied access control",
            "BLOCKED",
            "An existing Consumer profile provides the permission control",
            "Supply --caller-user; no new roles or authentication are created.",
        )
    return version
