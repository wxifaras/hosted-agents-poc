"""Bounded identity setup previews and reuse; no Graph requests."""

from copy import deepcopy
from uuid import UUID

import pytest

from scripts.deploy import story6_mcp_identity as identity

CALLER = UUID("33333333-3333-4333-8333-333333333333")
AUDIENCE = "22222222-2222-4222-8222-222222222222"
APPLICATION = {
    "id": "application-object",
    "appId": AUDIENCE,
    "signInAudience": "AzureADMyOrg",
    "api": {"requestedAccessTokenVersion": 2},
    "appRoles": [deepcopy(identity.ROLE)],
    "identifierUris": ["api://" + AUDIENCE],
}


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("principal_type", ["ManagedIdentity", "ServiceIdentity"])
def test_preview_does_not_write_and_exact_assignment_is_reused(monkeypatch, apply, principal_type) -> None:
    calls = []

    def graph(path, *, method="get", body=None):
        calls.append((path, method, body))
        if path.startswith(f"/servicePrincipals/{CALLER}?"):
            return {"id": str(CALLER), "servicePrincipalType": principal_type}
        if path.startswith("/applications?"):
            return {"value": [deepcopy(APPLICATION)]}
        if path.startswith("/servicePrincipals?"):
            return {"value": [{"id": "api-principal"}]}
        if path.endswith("/appRoleAssignments"):
            return {"value": [{"resourceId": "api-principal", "appRoleId": identity.ROLE_ID}]}
        pytest.fail(f"Unexpected Graph operation: {method} {path}")

    monkeypatch.setattr(identity, "graph", graph)
    result = identity.configure(CALLER, apply=apply)
    assert all(method == "get" for _, method, _ in calls)
    assert result["mode"] == ("applied" if apply else "preview")
    if apply:
        assert result["scope"] == f"api://{AUDIENCE}/.default"


def test_missing_identity_creates_only_exact_api_and_single_app_assignment(monkeypatch) -> None:
    writes = []

    def graph(path, *, method="get", body=None):
        if method != "get":
            writes.append((path, method, body))
            if path == "/applications":
                return {**deepcopy(APPLICATION), "identifierUris": []}
            if path == "/servicePrincipals":
                return {"id": "api-principal"}
            return {}
        if path.startswith(f"/servicePrincipals/{CALLER}?"):
            return {"id": str(CALLER), "servicePrincipalType": "ManagedIdentity"}
        return {"value": []}

    monkeypatch.setattr(identity, "graph", graph)
    identity.configure(CALLER, apply=True)
    assert [(path, method) for path, method, _ in writes] == [
        ("/applications", "post"),
        ("/applications/application-object", "patch"),
        ("/servicePrincipals", "post"),
        (f"/servicePrincipals/{CALLER}/appRoleAssignments", "post"),
    ]
    assert writes[-1][2] == {
        "principalId": str(CALLER),
        "resourceId": "api-principal",
        "appRoleId": identity.ROLE_ID,
    }


@pytest.mark.parametrize("problem", ["caller", "duplicate", "mismatch", "pagination"])
def test_conflicting_prerequisites_never_write(monkeypatch, problem) -> None:
    def graph(path, *, method="get", body=None):
        assert method == "get"
        if path.startswith("/servicePrincipals/"):
            return {
                "id": str(CALLER),
                "servicePrincipalType": "Application" if problem == "caller" else "ManagedIdentity",
            }
        app = deepcopy(APPLICATION)
        if problem == "mismatch":
            app["api"]["requestedAccessTokenVersion"] = 1
        return {
            "value": [app, app] if problem == "duplicate" else [app],
            **({"@odata.nextLink": "more"} if problem == "pagination" else {}),
        }

    monkeypatch.setattr(identity, "graph", graph)
    with pytest.raises(ValueError):
        identity.configure(CALLER, apply=True)
