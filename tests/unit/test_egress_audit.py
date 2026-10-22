"""Safe diagnostics of synthetic outbound credentials, never authorization tests."""

import base64
import json

import pytest
from app.services.egress_audit import audit_egress

ENDPOINT = "https://example.services.ai.azure.com/api/projects/poc/agents/research-agent/endpoint/protocols/openai"


def synthetic_bearer(**claims: object) -> str:
    """Make deliberately unsigned test data, not a usable credential."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "Bearer e30." + payload + ".synthetic"


def inspect(headers: dict[str, str], *, url: str = ENDPOINT + "/responses") -> dict[str, object]:
    return audit_egress(
        url,
        headers,
        {"x-agent-foundry-call-id": "sensitive-call", "x-client-expected-caller": "sensitive-user"},
        expected_endpoint=ENDPOINT,
        expected_principal="sensitive-principal",
        expected_client="sensitive-client",
    )


def test_audit_returns_only_booleans_not_raw_claims_headers_or_credential() -> None:
    bearer = synthetic_bearer(oid="sensitive-principal", appid="sensitive-client", aud="https://ai.azure.com")
    report = inspect(
        {
            "Authorization": bearer,
            "x-agent-foundry-call-id": "sensitive-call",
            "x-client-expected-caller": "sensitive-user",
        }
    )
    assert all(value is True for value in report.values()), report
    assert "sensitive" not in json.dumps(report)
    assert bearer not in json.dumps(report)


@pytest.mark.parametrize("authorization", ["", "Basic synthetic", "Bearer opaque", "Bearer e30.!.synthetic"])
def test_unreadable_claims_remain_explicitly_unknown(authorization: str) -> None:
    report = inspect({"authorization": authorization})
    assert report["credential_claims_readable"] is False
    assert report["credential_principal_matches_supervisor_instance"] is None
    assert report["credential_client_matches_supervisor_instance"] is None
    assert report["credential_audience_matches_foundry"] is None


def test_mismatched_actual_principal_client_audience_and_stripped_headers_are_visible() -> None:
    report = inspect(
        {
            "authorization": synthetic_bearer(
                oid="different-principal", azp="different-client", aud="https://other.invalid"
            ),
            "x-agent-user-id": "client-must-not-supply-this",
        },
        url="https://internal-proxy.invalid/responses",
    )
    for field in (
        "target_matches_expected_research_endpoint",
        "target_is_public_foundry_host",
        "call_header_matches_client_configuration",
        "expected_caller_header_matches_client_configuration",
        "no_client_supplied_platform_user_header",
        "credential_principal_matches_supervisor_instance",
        "credential_client_matches_supervisor_instance",
        "credential_audience_matches_foundry",
    ):
        assert report[field] is False, {field: report[field]}


def test_an_unexpected_delegation_header_is_not_hidden_by_missing_expected_header() -> None:
    report = inspect({"x-ms-user-identity": "unexpected-user"})
    assert report["delegation_header_matches_client_configuration"] is False
