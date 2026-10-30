"""Preview/apply one MCP API registration and one workload app-role assignment.

No client secret, user grant, login, build or deployment. Exit 0: success;
exit 2: invalid inputs or blocked CLI/Graph prerequisite. Inspect partial
operations after an error; never assume a failed command made no changes.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from pydantic import JsonValue

from scripts.deploy.grant_local_rbac import CliError, az, object_value
from scripts.deploy.story6_access import runtime_identity
from scripts.deploy.story6_package import ROOT, deployment_context

NAME = "story6-approval-mcp"
ROLE_ID = str(uuid5(NAMESPACE_URL, "hosted-agents-poc/story6-mcp/Story6.Invoke"))
GRAPH = "https://graph.microsoft.com/v1.0"
ROLE: dict[str, JsonValue] = {
    "id": ROLE_ID,
    "allowedMemberTypes": ["Application"],
    "description": "Invoke the Story6 synthetic MCP action.",
    "displayName": "Invoke Story6 MCP",
    "isEnabled": True,
    "value": "Story6.Invoke",
}


def graph(path: str, *, method: str = "get", body: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    """Use the existing operator login for an exact first-party Graph request."""
    arguments = ["rest", "--method", method, "--url", GRAPH + path]
    if body is not None:
        arguments += ["--headers", "Content-Type=application/json", "--body", json.dumps(body)]
    value = az(arguments)
    return {} if value is None and method == "patch" else object_value(value)


def rows(path: str) -> list[dict[str, JsonValue]]:
    """Read the small exact-name/principal query; do not silently truncate."""
    result = graph(path)
    value = result.get("value")
    if not isinstance(value, list) or result.get("@odata.nextLink"):
        raise ValueError("Unexpected/paged Graph response; inspect before changing this POC registration.")
    return [object_value(item) for item in value]


def configure(caller_id: UUID, *, apply: bool) -> dict[str, JsonValue]:
    """Allow only the explicit workload principal, reusing an exact registration."""
    principal = graph(f"/servicePrincipals/{caller_id}?$select=id,servicePrincipalType")
    if principal.get("id") != str(caller_id) or principal.get("servicePrincipalType") not in (
        "ManagedIdentity",
        "ServiceIdentity",
    ):
        raise ValueError("Caller must be the verified Story6 coordinator workload-identity object ID.")
    applications = rows(f"/applications?$filter=displayName eq '{NAME}'")
    if len(applications) > 1:
        raise ValueError("Multiple Story6 MCP registrations exist; do not guess which to change.")
    application = applications[0] if applications else None
    if application is not None:
        roles = application.get("appRoles")
        role_matches = (
            isinstance(roles, list)
            and len(roles) == 1
            and isinstance(roles[0], dict)
            and all(roles[0].get(key) == value for key, value in ROLE.items())
        )
        if (
            application.get("signInAudience") != "AzureADMyOrg"
            or not role_matches
            or object_value(application.get("api")).get("requestedAccessTokenVersion") != 2
        ):
            raise ValueError("Existing same-name API has different settings; it will not be overwritten.")
    if not apply:
        return {
            "mode": "preview",
            "api": NAME,
            "registration_present": application is not None,
            "caller_id": str(caller_id),
            "app_role": "Story6.Invoke",
            "changes_applied": False,
        }
    if application is None:
        application = graph(
            "/applications",
            method="post",
            body={
                "displayName": NAME,
                "signInAudience": "AzureADMyOrg",
                "api": {"requestedAccessTokenVersion": 2},
                "appRoles": [ROLE],
            },
        )
    audience = str(UUID(str(application["appId"])))
    uri = f"api://{audience}"
    identifiers = application.get("identifierUris")
    if identifiers not in ([], [uri]):
        raise ValueError("Existing API identifier URI conflicts with the expected custom audience.")
    if identifiers == []:
        graph(f"/applications/{application['id']}", method="patch", body={"identifierUris": [uri]})
    principals = rows(f"/servicePrincipals?$filter=appId eq '{audience}'")
    if len(principals) > 1:
        raise ValueError("Multiple API service principals returned.")
    resource = principals[0] if principals else graph("/servicePrincipals", method="post", body={"appId": audience})
    assignments = rows(f"/servicePrincipals/{caller_id}/appRoleAssignments")
    if not any(item.get("resourceId") == resource["id"] and item.get("appRoleId") == ROLE_ID for item in assignments):
        graph(
            f"/servicePrincipals/{caller_id}/appRoleAssignments",
            method="post",
            body={"principalId": str(caller_id), "resourceId": resource["id"], "appRoleId": ROLE_ID},
        )
    return {"mode": "applied", "audience": audience, "scope": uri + "/.default", "caller_id": str(caller_id)}


def main(argv: Sequence[str] | None = None) -> int:
    """Preview by default; --apply is an explicit identity-write action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        context = deployment_context(ROOT / ".azure" / args.environment / ".env", args.environment)
        with (
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
            target = runtime_identity(project)
            if args.apply and runtime_identity(project) != target:
                raise ValueError("Coordinator identity/version changed; preview again.")
            print(json.dumps(configure(UUID(target.principal), apply=args.apply), indent=2))
    except (AzureError, CliError, OSError, ValueError, KeyError) as error:
        print(f"Story6 MCP identity setup blocked: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
