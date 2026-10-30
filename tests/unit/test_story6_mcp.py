"""Real MCP HTTP/JWT boundary tests with local signing keys; no Azure calls."""

import json
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from app import story6_mcp as fixture
from cryptography.hazmat.primitives.asymmetric import rsa

TENANT = UUID("11111111-1111-4111-8111-111111111111")
AUDIENCE = UUID("22222222-2222-4222-8222-222222222222")
CALLER = UUID("33333333-3333-4333-8333-333333333333")
URL = "https://story6.synthetic.azurecontainerapps.io/mcp"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def settings() -> fixture.McpSettings:
    return fixture.McpSettings(tenant_id=TENANT, audience=AUDIENCE, caller_id=CALLER, url=URL)


def token(key, **overrides) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": settings().issuer,
            "aud": str(AUDIENCE),
            "tid": str(TENANT),
            "oid": str(CALLER),
            "ver": "2.0",
            "iat": now,
            "nbf": now - 1,
            "exp": now + 60,
            "roles": [fixture.INVOKE_ROLE],
            **overrides,
        },
        key,
        algorithm="RS256",
        headers={"kid": "synthetic"},
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://ai.azure.com"},
        {"iss": "https://wrong.invalid"},
        {"exp": 1},
        {"oid": str(uuid4())},
        {"tid": str(uuid4())},
        {"ver": "1.0"},
        {"scp": "user_impersonation"},
    ],
)
async def test_real_signed_invalid_claims_are_rejected(monkeypatch, signing_key, overrides) -> None:
    verifier = fixture.EntraVerifier(settings())
    monkeypatch.setattr(
        verifier.keys, "get_signing_key_from_jwt", lambda _: SimpleNamespace(key=signing_key.public_key())
    )
    assert await verifier.verify_token(token(signing_key, **overrides)) is None


async def test_invalid_signature_and_key_failure_are_denied(monkeypatch, signing_key) -> None:
    verifier = fixture.EntraVerifier(settings())
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(verifier.keys, "get_signing_key_from_jwt", lambda _: SimpleNamespace(key=other.public_key()))
    assert await verifier.verify_token(token(signing_key)) is None

    def unavailable(_: str):
        raise jwt.PyJWKClientError("Synthetic signing-key failure")

    monkeypatch.setattr(verifier.keys, "get_signing_key_from_jwt", unavailable)
    assert await verifier.verify_token(token(signing_key)) is None


async def test_real_mcp_auth_dispatch_and_independent_receipts(monkeypatch, signing_key, caplog) -> None:
    monkeypatch.setattr(
        fixture.PyJWKClient,
        "get_signing_key_from_jwt",
        lambda self, _: SimpleNamespace(key=signing_key.public_key()),
    )
    ledger = fixture.Receipts()
    server = fixture.build_server(settings(), ledger)
    app = server.streamable_http_app()
    with caplog.at_level("INFO", logger="story6-mcp"):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=URL.removesuffix("/mcp"),
                headers={"Accept": "application/json, text/event-stream"},
            ) as client,
        ):
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            assert (await client.post("/mcp", json=request)).status_code == 401
            no_role = {"Authorization": "Bearer " + token(signing_key, roles=[])}
            assert (await client.post("/mcp", json=request, headers=no_role)).status_code == 403
            wrong_audience = {"Authorization": "Bearer " + token(signing_key, aud="https://ai.azure.com")}
            assert (await client.post("/mcp", json=request, headers=wrong_audience)).status_code == 401
            assert ledger.total == 0
            auth = {"Authorization": "Bearer " + token(signing_key)}
            listed = await client.post("/mcp", json=request, headers=auth)
            assert listed.status_code == 200
            assert [item["name"] for item in listed.json()["result"]["tools"]] == ["story6_record_action"]
            case = str(uuid4())
            call = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "story6_record_action", "arguments": {"case_id": case}},
            }
            assert (await client.post("/mcp", json=call)).status_code == 401
            assert ledger.total == 0
            for number in (1, 2):
                result = await client.post("/mcp", json=call, headers=auth)
                assert result.status_code == 200
                content = result.json()["result"]
                assert not content.get("isError")
                receipt = json.loads(content["content"][0]["text"])
                assert receipt["execution"] == number and receipt["epoch"] == ledger.epoch
            assert ledger.total == 2 and ledger.counts[case] == 2
            health = (await client.get("/health")).json()
            assert health == {"ready": True, "epoch": ledger.epoch, "total": 2}
            call["params"]["arguments"]["case_id"] = "not-a-uuid"
            invalid = (await client.post("/mcp", json=call, headers=auth)).json()
            assert invalid["result"]["isError"]
            assert ledger.total == 2
    assert caplog.text.count('"event": "story6_action_executed"') == 2
    assert auth["Authorization"] not in caplog.text


def test_receipt_bound_fails_instead_of_eviction() -> None:
    ledger = fixture.Receipts()
    ledger.total = 1000
    with pytest.raises(ValueError, match="limit"):
        ledger.record(uuid4())
    assert ledger.total == 1000 and not ledger.counts


def test_fixture_settings_load_only_explicit_environment(monkeypatch) -> None:
    for key, value in {
        "TENANT_ID": str(TENANT),
        "AUDIENCE": str(AUDIENCE),
        "CALLER_ID": str(CALLER),
        "URL": URL,
    }.items():
        monkeypatch.setenv("STORY6_MCP_" + key, value)
    loaded = fixture.McpSettings.model_validate({})
    assert loaded == settings()


def test_isolated_tool_template_has_one_replica_and_no_agent_changes() -> None:
    from scripts.deploy.grant_local_rbac import native_json

    root = Path(__file__).resolve().parents[2]
    template = native_json(
        "az", ["bicep", "build", "--file", str(root / "experiments" / "story6-mcp" / "main.bicep"), "--stdout"]
    )
    resources = template["resources"]
    app = next(item for item in resources if item["type"] == "Microsoft.App/containerApps")
    properties = app["properties"]
    assert properties["template"]["scale"] == {"minReplicas": 1, "maxReplicas": 1}
    assert properties["configuration"]["activeRevisionsMode"] == "Single"
    assert properties["configuration"]["ingress"]["allowInsecure"] is False
    assert properties["configuration"]["ingress"]["targetPort"] == 8092
    container = properties["template"]["containers"][0]
    assert len(properties["template"]["containers"]) == 1
    assert {item["name"] for item in container["env"]} == {
        "STORY6_MCP_URL",
        "STORY6_MCP_TENANT_ID",
        "STORY6_MCP_AUDIENCE",
        "STORY6_MCP_CALLER_ID",
    }
    assert all(not item["type"].startswith("Microsoft.CognitiveServices") for item in resources)
