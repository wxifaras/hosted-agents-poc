"""Rich developer RBAC preview/apply using native Azure CLI commands.

Separate from azd provisioning. Exit codes: 0 passed, 1 failed, 2 blocked.
Only --apply grants roles. --verify-access writes/deletes one synthetic Blob.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from app.core.redaction import sanitize_text
from pydantic import JsonValue
from rich.table import Table

from scripts.common.deployments import resolve_deployment
from scripts.common.reporting import ValidationRun, add_output_arguments


class CliError(RuntimeError):
    """A native CLI failure, with credentials excluded from diagnostics."""


def command_prefix(name: str) -> list[str]:
    """Resolve native executables without invoking a command shell."""
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f"{name} is not installed. Install it and sign in manually.")
    path = Path(executable)
    if os.name == "nt" and path.suffix.casefold() in {".bat", ".cmd"}:
        python = path.parent.parent / "python.exe"
        if name != "az" or not python.is_file():
            raise ValueError(f"Cannot safely resolve the {name} Windows launcher.")
        return [str(python), "-IBm", "azure.cli"]
    return [str(path)]


def native_json(name: str, arguments: list[str], *, timeout: float = 90) -> JsonValue:
    """Use the existing az/azd login; never automatically log in or print tokens."""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"}
    try:
        result = subprocess.run(
            [*command_prefix(name), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        operation = "credential lookup" if name == "azd" else " ".join(arguments[:3])
        raise CliError(
            f"{name} {operation} timed out after {timeout:g}s. "
            "Earlier successful operations are not rolled back; inspect the recorded steps."
        ) from error
    if result.returncode:
        # Token-command diagnostics are not safe to echo, even when a CLI fails.
        details = (
            "Credential lookup failed; sign in manually."
            if name == "azd" and arguments[:2] == ["auth", "token"]
            else sanitize_text(result.stderr)
        )
        raise CliError(f"{name} exited with {result.returncode}: {details}")
    value: JsonValue = json.loads(result.stdout) if result.stdout.strip() else None
    return value


def az(arguments: list[str]) -> JsonValue:
    """Call Azure CLI with JSON output, not a second ARM/SDK abstraction."""
    return native_json("az", [*arguments, "--output", "json"])


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    """Reject malformed or missing CLI objects explicitly."""
    if not isinstance(value, dict):
        raise ValueError("Azure CLI returned no expected JSON object.")
    return value


@dataclass(frozen=True)
class Grant:
    """A specific verified role definition at a resource scope within this POC."""

    role: str
    scope: str
    definition_id: str
    present: bool


def verify_azd_user(principal: str) -> None:
    """Verify azd and az represent the same delegated user before native az grants."""
    token = object_value(
        native_json(
            "azd",
            ["auth", "token", "--scope", "https://graph.microsoft.com/.default", "--output", "json", "--no-prompt"],
        )
    )
    access_token = token.get("token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("azd did not return an access token; no identity can be verified.")
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.get(
            "https://graph.microsoft.com/v1.0/me?$select=id", headers={"Authorization": f"Bearer {access_token}"}
        )
    response.raise_for_status()
    if str(response.json().get("id", "")).casefold() != principal.casefold():
        raise ValueError("az and azd are logged in as different users. Align logins before granting access.")


def role_plan(resources: dict[str, JsonValue], principal: str, subscription: str, *, push: bool) -> list[Grant]:
    """Resolve current role definitions and existing inherited/group grants through az."""
    requested = [
        ("Storage Blob Data Contributor", "artifactContainerId"),
        ("Foundry User", "foundryAccountId"),
        ("Log Analytics Reader", "workspaceId"),
        ("Monitoring Reader", "applicationInsightsId"),
        ("Monitoring Metrics Publisher", "applicationInsightsId"),
    ]
    if resources.get("appConfigurationId"):
        requested.append(("App Configuration Data Reader", "appConfigurationId"))
    if push:
        registry = object_value(
            az(["resource", "show", "--ids", str(resources["registryId"]), "--api-version", "2025-04-01"])
        )
        mode = object_value(registry.get("properties")).get("roleAssignmentMode")
        if mode not in {None, "LegacyRegistryPermissions"}:
            raise ValueError("ACR uses repository ABAC; review repository-specific roles instead of AcrPush.")
        requested.append(("AcrPush", "registryId"))
    prefix = f"/subscriptions/{subscription}/resourceGroups/{resources['resourceGroup']}/providers/".casefold()
    plan = []
    for role, output_key in requested:
        scope = str(resources.get(output_key, ""))
        if not scope.casefold().startswith(prefix):
            raise ValueError(f"Refusing to grant {role} outside the selected POC resource group.")
        definitions = az(["role", "definition", "list", "--name", role, "--subscription", subscription])
        if not isinstance(definitions, list) or len(definitions) != 1:
            raise ValueError(f"Cannot resolve unique built-in role: {role}")
        definition = object_value(definitions[0])
        if definition.get("roleType") != "BuiltInRole" or definition.get("roleName") != role:
            raise ValueError(f"Unexpected role definition for {role}.")
        identifier = str(UUID(str(definition["name"])))
        existing = az(
            [
                "role",
                "assignment",
                "list",
                "--assignee",
                principal,
                "--include-groups",
                "--include-inherited",
                "--scope",
                scope,
                "--subscription",
                subscription,
            ]
        )
        if not isinstance(existing, list):
            raise ValueError("Role-assignment list is malformed.")
        accepted = {identifier}
        if role == "Storage Blob Data Contributor":
            accepted.add("b7e6dc6d-f1e8-4753-8033-0f276bb0955b")  # Blob Data Owner.
        if role == "Foundry User":
            accepted.add("c883944f-8b7b-4483-af10-35834be79c4a")  # Foundry Owner includes data actions.
        present = any(
            str(object_value(item).get("roleDefinitionId", "")).rsplit("/", 1)[-1].casefold() in accepted
            and not object_value(item).get("condition")
            for item in existing
        )
        plan.append(Grant(role, scope, identifier, present))
    return plan


def assignment_allowed(scope: str, subscription: str) -> bool:
    """Check role-assignment rights at each scope before creating any grants."""
    from fnmatch import fnmatchcase

    result = object_value(
        az(
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"https://management.azure.com{scope}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01",
                "--subscription",
                subscription,
            ]
        )
    )
    entries = result.get("value")
    if not isinstance(entries, list):
        raise ValueError("Effective permission response is malformed.")
    action = "microsoft.authorization/roleassignments/write"
    for entry in entries:
        permission = object_value(entry)
        actions, exclusions = permission.get("actions", []), permission.get("notActions", [])
        if not isinstance(actions, list) or not isinstance(exclusions, list):
            raise ValueError("Effective actions/notActions must be lists.")
        if any(fnmatchcase(action, str(pattern).casefold()) for pattern in actions) and not any(
            fnmatchcase(action, str(pattern).casefold()) for pattern in exclusions
        ):
            return True
    return False


def create_grant(
    grant: Grant,
    principal: str,
    subscription: str,
    *,
    principal_type: Literal["User", "ServicePrincipal"] = "User",
    assignment_name: str | None = None,
) -> None:
    """Use stable names; verify a concurrent duplicate before accepting it."""
    name = assignment_name or str(uuid5(NAMESPACE_URL, f"{grant.scope.casefold()}|{principal}|{grant.definition_id}"))
    try:
        az(
            [
                "role",
                "assignment",
                "create",
                "--name",
                name,
                "--assignee-object-id",
                principal,
                "--assignee-principal-type",
                principal_type,
                "--role",
                grant.definition_id,
                "--scope",
                grant.scope,
                "--subscription",
                subscription,
            ]
        )
    except CliError as error:
        if "RoleAssignmentExists" not in str(error):
            raise
        assignments = az(
            [
                "role",
                "assignment",
                "list",
                "--assignee",
                principal,
                "--scope",
                grant.scope,
                "--subscription",
                subscription,
            ]
        )
        if not isinstance(assignments, list) or not any(
            object_value(item).get("principalId") == principal
            and str(object_value(item).get("roleDefinitionId", "")).rsplit("/", 1)[-1] == grant.definition_id
            and str(object_value(item).get("scope", "")).casefold() == grant.scope.casefold()
            and not object_value(item).get("condition")
            for item in assignments
        ):
            raise CliError("Duplicate grant did not match the intended user/role/scope.") from error


def verify_monitoring(resources: dict[str, JsonValue], subscription: str, run: ValidationRun) -> None:
    """Use built-in az rest for an exact log-query assertion without CLI extension installation."""
    workspace = str(UUID(str(resources["workspaceCustomerId"])))
    run.console.print("Checking Monitoring access with native az rest (no log-analytics extension required)...")
    try:
        result = object_value(
            az(
                [
                    "rest",
                    "--method",
                    "post",
                    "--url",
                    f"https://api.loganalytics.io/v1/workspaces/{workspace}/query",
                    "--resource",
                    "https://api.loganalytics.io",
                    "--headers",
                    "Content-Type=application/json",
                    "--body",
                    json.dumps({"query": "print POCValidation=1"}),
                    "--subscription",
                    subscription,
                ]
            )
        )
        tables = result.get("tables")
        if (
            result.get("error")
            or not isinstance(tables, list)
            or not any(
                isinstance(table, dict)
                and table.get("name") == "PrimaryResult"
                and table.get("columns") == [{"name": "POCValidation", "type": "long"}]
                and table.get("rows") == [[1]]
                for table in tables
            )
        ):
            raise ValueError("Log query did not return the expected POCValidation=1 result.")
    except (CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        run.result(
            "Monitoring access",
            "FAIL",
            expected="Same-user Log Analytics query returns POCValidation=1",
            observed=str(error),
        )
        raise CliError(
            "Monitoring verification failed; earlier role grants and Blob checks remain recorded."
        ) from error
    run.result(
        "Monitoring access",
        "PASS",
        expected="Same-user Log Analytics query returns POCValidation=1",
        observed={"workspace_id": workspace, "POCValidation": 1},
    )


def verify_access(resources: dict[str, JsonValue], subscription: str, run: ValidationRun, *, timeout: float) -> None:
    """Use --auth-mode login for a checksum-verified synthetic Blob round trip and log query."""
    import hashlib

    blob = f"validation/{run.run_id}.txt"
    source, download = run.path / "synthetic-upload.txt", run.path / "synthetic-download.txt"
    source.write_text(f"Synthetic local RBAC validation: {run.run_id}", encoding="utf-8")
    common = [
        "--account-name",
        str(resources["storageAccountName"]),
        "--container-name",
        str(resources["artifactContainerName"]),
        "--name",
        blob,
        "--auth-mode",
        "login",
        "--subscription",
        subscription,
    ]
    uploaded = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                az(["storage", "blob", "upload", *common, "--file", str(source)])
                uploaded = True
                break
            except CliError as error:
                transient = any(
                    code in str(error)
                    for code in (
                        "AuthorizationPermissionMismatch",
                        "AuthorizationFailure",
                        "InsufficientAccountPermissions",
                        "TooManyRequests",
                    )
                )
                if not transient or time.monotonic() >= deadline:
                    raise
                run.console.print("Blob upload not yet authorized/reachable; retrying within propagation timeout.")
                time.sleep(5)
        az(["storage", "blob", "download", *common, "--file", str(download)])
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        if hashlib.sha256(download.read_bytes()).hexdigest() != expected:
            raise ValueError("Synthetic Blob checksum mismatch.")
        run.result(
            "Blob data access",
            "PASS",
            expected="Same-user write/read/checksum",
            observed={"blob": blob, "sha256": expected},
        )
        verify_monitoring(resources, subscription, run)
    finally:
        try:
            if uploaded:
                az(["storage", "blob", "delete", *common])
                run.result("Synthetic Blob cleanup", "PASS", expected="Delete only this run's Blob", observed=blob)
        finally:
            source.unlink(missing_ok=True)
            download.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """Offer credential-free help and explicit apply/access-test options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--credential-source", choices=("az", "azd"), default="az")
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--deployment", help="Override automatic latest-successful deployment discovery.")
    parser.add_argument("--expected-principal-id", type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--include-acr-push", action="store_true")
    parser.add_argument("--verify-access", action="store_true", help="Write/delete one synthetic Blob and query logs.")
    parser.add_argument("--timeout", type=float, default=180)
    add_output_arguments(parser)
    return parser


def execute(args: argparse.Namespace, run: ValidationRun) -> None:
    """Preview scoped grants and mutate only when explicitly requested."""
    if args.timeout <= 0 or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.environment):
        raise ValueError("--timeout must be positive and --environment a lowercase POC name of at most 40 characters.")
    account = object_value(az(["account", "show", "--subscription", args.subscription]))
    active_account = object_value(az(["account", "show"]))
    if account.get("tenantId") != active_account.get("tenantId"):
        raise ValueError(
            "The target subscription and active az login use different tenants. Align the CLI context first."
        )
    subscription = str(UUID(str(account["id"])))
    principal = str(UUID(str(az(["ad", "signed-in-user", "show", "--query", "id"]))))
    if args.expected_principal_id and str(args.expected_principal_id) != principal:
        raise ValueError("Current az user does not match --expected-principal-id.")
    if args.credential_source == "azd":
        verify_azd_user(principal)
    run.result(
        "Local identity",
        "PASS",
        expected="Verified selected CLI user",
        observed={
            "principal_id": principal,
            "source": args.credential_source,
            "subscription": subscription,
            "tenant": account.get("tenantId"),
        },
    )
    deployment = resolve_deployment(az, subscription, args.environment, args.deployment)
    run.result(
        "Deployment selection",
        "PASS",
        expected="Explicit or latest successful exact-environment deployment",
        observed={"environment": args.environment, "deployment": deployment},
    )
    resources = object_value(
        az(
            [
                "deployment",
                "sub",
                "show",
                "--subscription",
                subscription,
                "--name",
                deployment,
                "--query",
                "properties.outputs.resources.value",
            ]
        )
    )
    if resources.get("subscriptionId") != subscription or resources.get("resourceGroup") != f"rg-{args.environment}":
        raise ValueError("Deployed outputs do not match the selected POC subscription/resource group.")
    plan = role_plan(resources, principal, subscription, push=args.include_acr_push)
    table = Table(title="Local-user role plan (separate from deployment)")
    for name in ("Role", "Scope", "State"):
        table.add_column(name)
    for grant in plan:
        table.add_row(grant.role, grant.scope, "Present" if grant.present else "Missing")
    run.console.print(table)
    run.result(
        "Role plan",
        "PASS",
        expected="Explicit existing/missing scoped grants",
        observed={"apply": args.apply, "roles": [asdict(grant) for grant in plan]},
    )
    missing = [grant for grant in plan if not grant.present]
    if args.apply:
        for grant in missing:
            if not assignment_allowed(grant.scope, subscription):
                raise ValueError(f"Cannot assign at {grant.scope}. Ask an administrator to apply the displayed plan.")
        if az(["ad", "signed-in-user", "show", "--query", "id"]) != principal:
            raise ValueError("az identity changed after preview.")
        if args.credential_source == "azd":
            verify_azd_user(principal)
        for grant in missing:
            create_grant(grant, principal, subscription)
            run.result(grant.role, "PASS", expected="Idempotent scoped role assignment", observed=grant.scope)
    if args.apply or args.verify_access:
        verify_access(resources, subscription, run, timeout=args.timeout)
    run.console.print("Model inference, ACR push, and hosted State Store isolation were not tested by this helper.")


def main(argv: Sequence[str] | None = None) -> int:
    """Persist Rich/JSONL evidence and return nonzero for failed or blocked grants."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "developer-rbac",
        "apply" if args.apply else "preview",
        args.output_dir,
        no_color=args.no_color,
        verbose=args.verbose,
    )
    try:
        execute(args, run)
    except (CliError, httpx.HTTPError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        run.result(
            "Developer RBAC",
            "BLOCKED" if isinstance(error, ValueError) else "FAIL",
            expected="Verified local identity and scoped access",
            observed=str(error),
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
