"""Boolean-only inspection of our own diagnostic requests; not token validation."""

import base64
import binascii
import json
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import JsonValue


def audit_egress(
    url: str,
    headers: Mapping[str, str],
    expected_headers: Mapping[str, str],
    *,
    expected_endpoint: str,
    expected_principal: str,
    expected_client: str,
) -> dict[str, JsonValue]:
    """Compare final transport metadata without returning tokens, claims, or raw identities.

    Decoded claims describe the credential the SDK sent. They are not signature
    verification and must never authorize workflow access.
    """
    normalized = {key.casefold(): value for key, value in headers.items()}
    parsed = urlsplit(url)
    expected = urlsplit(expected_endpoint)
    result: dict[str, JsonValue] = {
        "target_matches_expected_research_endpoint": (
            parsed.scheme == expected.scheme
            and parsed.netloc == expected.netloc
            and parsed.path == expected.path.rstrip("/") + "/responses"
        ),
        "target_is_public_foundry_host": bool(parsed.hostname and parsed.hostname.endswith(".services.ai.azure.com")),
        "call_header_matches_client_configuration": (
            normalized.get("x-agent-foundry-call-id") == expected_headers.get("x-agent-foundry-call-id")
        ),
        "delegation_header_matches_client_configuration": (
            normalized.get("x-ms-user-identity") == expected_headers.get("x-ms-user-identity")
        ),
        "expected_caller_header_matches_client_configuration": (
            normalized.get("x-client-expected-caller") == expected_headers.get("x-client-expected-caller")
        ),
        "no_client_supplied_platform_user_header": "x-agent-user-id" not in normalized,
    }
    authorization = normalized.get("authorization", "")
    bearer = authorization[7:] if authorization.casefold().startswith("bearer ") else ""
    result["bearer_authorization_present"] = bool(bearer)
    result["credential_claims_readable"] = False
    result["credential_principal_matches_supervisor_instance"] = None
    result["credential_client_matches_supervisor_instance"] = None
    result["credential_audience_matches_foundry"] = None
    parts = bearer.split(".")
    if len(parts) != 3:
        return result
    try:
        payload: object = json.loads(
            base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True)
        )
    except ValueError, UnicodeDecodeError, binascii.Error:
        return result
    if not isinstance(payload, dict):
        return result
    result.update(
        credential_claims_readable=True,
        credential_principal_matches_supervisor_instance=payload.get("oid") == expected_principal,
        credential_client_matches_supervisor_instance=payload.get("appid", payload.get("azp")) == expected_client,
        credential_audience_matches_foundry=payload.get("aud") in ("https://ai.azure.com", "https://ai.azure.com/"),
    )
    return result
