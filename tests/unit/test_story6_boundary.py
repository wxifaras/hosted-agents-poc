"""Direct proxy qualification with synthetic transports, not service verdicts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from scripts.deploy.story6_toolbox import source_tool
from scripts.run_scenario import story6_boundary as boundary


@pytest.mark.parametrize("sse", [False, True])
async def test_rpc_accepts_one_matching_json_or_sse_response_without_resend(sse: bool) -> None:
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        response = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}}
        if sse:
            return httpx.Response(
                200,
                text="event: message\ndata: " + json.dumps(response) + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        rpc = boundary.RPC(http, "https://synthetic.invalid/mcp")
        assert await rpc.request("tools/list") == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    assert len(calls) == 1


async def test_ambiguous_send_is_not_replayed() -> None:
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadError("Synthetic lost result.", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(httpx.ReadError):
            await boundary.RPC(http, "https://synthetic.invalid/mcp").request("tools/call", {"name": "synthetic"})
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"result": {"content": [{"text": "Permission denied."}], "isError": True}},
        {"error": {"code": -32000}},
        {"result": {"content": []}},
    ],
)
def test_http_success_does_not_imply_tool_success(response) -> None:
    assert boundary.successful_result(response) is False


async def test_unapproved_execution_is_a_failure_while_resource_denial_is_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_http = httpx.AsyncClient
    methods = []

    def handle(request):
        assert request.url.host == "synthetic.services.ai.azure.com"
        if request.headers["authorization"] == "Bearer consumer-synthetic":
            return httpx.Response(403, json={"error": {"code": "Forbidden"}})
        body = json.loads(request.content)
        methods.append(body["method"])
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        elif body["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": boundary.TOOLBOX_TOOL,
                        "_meta": {"tool_configuration": {"require_approval": "always"}},
                    }
                ]
            }
        else:
            assert body["method"] == "tools/call"
            assert set(body["params"]) == {"name", "arguments"}
            assert body["params"]["arguments"] == {"query": boundary.SEARCH_QUERY}
            result = {"content": [{"type": "text", "text": "Synthetic public docs."}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def client(**kwargs):
        return original_http(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(boundary.httpx, "AsyncClient", client)
    monkeypatch.setattr(
        boundary,
        "deployment_context",
        lambda *args: {
            "AZURE_AI_PROJECT_ENDPOINT": "https://synthetic.services.ai.azure.com/api/projects/poc",
            "AZURE_TENANT_ID": "synthetic-tenant",
        },
    )
    operator = MagicMock()
    operator.__aenter__.return_value = operator
    operator.get_token = AsyncMock(return_value=SimpleNamespace(token="operator-synthetic"))
    monkeypatch.setattr(boundary, "AzureCliCredential", MagicMock(return_value=operator))
    project = MagicMock()
    project.__aenter__.return_value = project
    project.toolboxes.get_version = AsyncMock(
        return_value=SimpleNamespace(
            as_dict=lambda: {
                "name": boundary.TOOLBOX_NAME,
                "version": "1",
                "tools": [source_tool().as_dict()],
            }
        )
    )
    monkeypatch.setattr(boundary, "AIProjectClient", MagicMock(return_value=project))
    consumer = MagicMock()
    consumer.__aenter__.return_value = consumer
    consumer.get_token = AsyncMock(return_value=SimpleNamespace(token="consumer-synthetic"))
    monkeypatch.setattr(boundary, "ProfileCredential", MagicMock(return_value=consumer))
    monkeypatch.setattr(boundary, "prepare_user", lambda *args: SimpleNamespace())
    records = []
    assert await boundary.execute("poc", "not-saved-to-evidence", lambda *args: records.append(args), timeout=15) == "1"
    assert [item[1] for item in records] == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert records[3][3]["unapproved_result_returned"] is True
    assert records[4][3]["permission_denied"] is True
    assert records[4][3]["approval_enforcement_tested"] is False
    assert methods.count("tools/call") == 2
