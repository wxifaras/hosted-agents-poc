"""One synthetic Entra-protected MCP tool; human approval stays in the caller."""

import asyncio
import json
import logging
from collections import Counter
from uuid import UUID, uuid4

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services.story6_approval import EntraRoute

LOGGER = logging.getLogger("story6-mcp")
INVOKE_ROLE = "Story6.Invoke"


class McpSettings(BaseSettings):
    """Non-secret configuration for the isolated single-process fixture."""

    model_config = SettingsConfigDict(env_prefix="STORY6_MCP_", extra="ignore")
    tenant_id: UUID
    audience: UUID
    caller_id: UUID
    url: str
    port: int = Field(default=8092, ge=1, le=65535)

    @property
    def issuer(self) -> str:
        """Accept only this tenant's v2 access tokens."""
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"


class EntraVerifier:
    """Validate signed workload claims; roles are enforced by MCP middleware."""

    def __init__(self, settings: McpSettings) -> None:
        self.settings = settings
        self.keys = PyJWKClient(
            f"https://login.microsoftonline.com/{settings.tenant_id}/discovery/v2.0/keys",
            timeout=10,
        )

    def decode(self, token: str) -> dict[str, object]:
        """Validate signature, audience, issuer and lifetime using tenant JWKS."""
        key = self.keys.get_signing_key_from_jwt(token)
        claims: dict[str, object] = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=str(self.settings.audience),
            issuer=self.settings.issuer,
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "ver"]},
        )
        return claims

    async def verify_token(self, token: str) -> AccessToken | None:
        """Reject invalid tokens without logging credentials or private claims."""
        try:
            claims = await asyncio.to_thread(self.decode, token)
        except jwt.PyJWTError:
            LOGGER.warning("Story6 MCP authentication rejected: invalid token or unavailable signing key")
            return None
        if (
            claims.get("tid") != str(self.settings.tenant_id)
            or claims.get("oid") != str(self.settings.caller_id)
            or claims.get("ver") != "2.0"
            or "scp" in claims
        ):
            LOGGER.warning("Story6 MCP authentication rejected: workload principal mismatch")
            return None
        roles = claims.get("roles")
        scopes = [role for role in roles if isinstance(role, str)] if isinstance(roles, list) else []
        expires = claims.get("exp")
        if not isinstance(expires, int):
            LOGGER.warning("Story6 MCP authentication rejected: invalid expiry")
            return None
        return AccessToken(
            token=token,
            client_id=str(self.settings.caller_id),
            scopes=scopes,
            expires_at=expires,
        )


class Receipts:
    """Bounded in-memory counts; repeated executions are intentionally not hidden."""

    def __init__(self) -> None:
        self.epoch = str(uuid4())
        self.counts: Counter[str] = Counter()
        self.total = 0

    def record(self, case_id: UUID) -> dict[str, str | int]:
        """Append one synthetic effect and log its independently observable count."""
        if self.total >= 1000:
            raise ValueError("Story6 fixture receipt limit reached; end this validation run.")
        case = str(case_id)
        self.total += 1
        self.counts[case] += 1
        receipt: dict[str, str | int] = {
            "event": "story6_action_executed",
            "case_id": case,
            "epoch": self.epoch,
            "execution": self.counts[case],
            "total": self.total,
        }
        LOGGER.info("%s", json.dumps(receipt, sort_keys=True))
        return receipt


def build_server(settings: McpSettings, receipts: Receipts | None = None) -> FastMCP:
    """Build the existing SDK's authenticated Streamable HTTP server."""
    route = EntraRoute(settings.url, f"api://{settings.audience}/.default", {})
    route.endpoint()
    ledger = receipts if receipts is not None else Receipts()
    server = FastMCP(
        "Story6 synthetic action",
        host="0.0.0.0",
        port=settings.port,
        stateless_http=True,
        json_response=True,
        token_verifier=EntraVerifier(settings),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(settings.url),
            validate_token_resource=False,  # Entra audience is validated against the API client ID.
            required_scopes=[INVOKE_ROLE],
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[settings.url.removeprefix("https://").removesuffix("/mcp")],
            allowed_origins=[settings.url.removesuffix("/mcp")],
        ),
    )

    @server.tool()
    def story6_record_action(case_id: UUID) -> dict[str, str | int]:
        """Record one harmless synthetic action for the exact approved case."""
        return ledger.record(case_id)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ready": True, "epoch": ledger.epoch, "total": ledger.total})

    server.custom_route("/health", methods=["GET"])(health)
    return server


def main() -> None:
    """Start one worker; configuration failure is explicit before listening."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build_server(McpSettings.model_validate({})).run(transport="streamable-http")


if __name__ == "__main__":
    main()
