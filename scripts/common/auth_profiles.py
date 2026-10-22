"""Plan/create isolated CLI profiles, with optional project-scoped Consumer RBAC.

Passwords/MFA are handled by Azure CLI's Microsoft sign-in UI, never this script.
Exit 0: plan/authentication checks passed; 1: sign-in or identity mismatch;
2: invalid configuration, missing prerequisites, or canceled operation.
Authentication is not proof of Foundry permissions or user isolation.
"""

import argparse
import json
import os
import re
import subprocess
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from app.core.app_config_source import DEFAULT_ENV_FILE
from pydantic import JsonValue

from scripts.common.reporting import ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import (
    CliError,
    Grant,
    assignment_allowed,
    az,
    command_prefix,
    create_grant,
    native_json,
    object_value,
)

CONSUMER_ROLE = "Foundry Agent Consumer"
INTERACT_ACTION = "Microsoft.CognitiveServices/accounts/AIServices/endpoints/interact/action"


class ProfileError(RuntimeError):
    """Authentication, identity verification, or requested RBAC setup failed."""


def build_parser() -> argparse.ArgumentParser:
    """Keep help credential-free and require explicit approval for interactive login."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", required=True, help="Comma-separated full user principal names, not passwords.")
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument(
        "--tenant", help="Tenant GUID; otherwise read AZURE_TENANT_ID from the selected azd environment."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply requested Consumer RBAC, create CLI directories, and start Microsoft sign-in.",
    )
    parser.add_argument("--login-timeout", type=int, default=600)
    parser.add_argument(
        "--rbac",
        choices=("none", "consumer"),
        default="none",
        help="Plan/apply only project-scoped Foundry Agent Consumer before user login.",
    )
    add_output_arguments(parser)
    return parser


def parse_users(value: str) -> list[str]:
    """Reject empty, duplicate, and malformed sign-in names before creating profiles."""
    users = [part.strip().casefold() for part in value.split(",")]
    if not users or any(not re.fullmatch(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", user) for user in users):
        raise ValueError("Enter comma-separated full sign-in names, for example user1@tenant.onmicrosoft.com.")
    if len(set(users)) != len(users):
        raise ValueError("Each user must appear only once; usernames are case-insensitive.")
    return users


def profile_path(tenant: str, user: str) -> Path:
    """Keep CLI credential caches local, outside the repository and active developer profile."""
    local = os.environ.get("LOCALAPPDATA")
    if not local or not Path(local).is_absolute():
        raise ValueError("LOCALAPPDATA must identify this Windows user's absolute local application-data directory.")
    target = (Path(local) / "HostedAgentsPoc" / "Auth" / tenant / sha256(user.encode()).hexdigest()).resolve()
    active = Path(os.environ.get("AZURE_CONFIG_DIR") or Path.home() / ".azure").expanduser().resolve()
    if (
        target.is_relative_to(DEFAULT_ENV_FILE.parent.resolve())
        or target.is_relative_to(active)
        or active.is_relative_to(target)
    ):
        raise ValueError("An auth profile must be outside the repository and must not overlap the active CLI profile.")
    return target


def profile_json(prefix: list[str], arguments: list[str], environment: dict[str, str]) -> JsonValue:
    """Read only CLI account metadata under an explicit child-process profile."""
    result = subprocess.run(
        [*prefix, *arguments, "--output", "json"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise ProfileError(f"Azure CLI account lookup failed with exit {result.returncode}; no profile was verified.")
    value: JsonValue = json.loads(result.stdout)
    return value


def verify_account(value: JsonValue, user: str, tenant: str) -> None:
    """Check the CLI account metadata; never use a profile name as identity proof."""
    record = object_value(value)
    username, actual_tenant = record.get("username"), record.get("tenant")
    if (
        not isinstance(username, str)
        or username.casefold() != user
        or not isinstance(actual_tenant, str)
        or actual_tenant.casefold() != tenant
        or record.get("kind") != "user"
    ):
        raise ProfileError(
            "CLI user/tenant does not match the requested profile. "
            "No cache was cleared automatically. If needed, sign out only in that isolated profile and retry manually."
        )


def authenticate(user: str, tenant: str, path: Path, *, timeout: int) -> None:
    """Open Microsoft sign-in without collecting credentials or modifying the parent environment."""
    prefix = command_prefix("az")
    environment = {
        **os.environ,
        "AZURE_CONFIG_DIR": str(path),
        "PYTHONIOENCODING": "utf-8",
        "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
    }
    path.mkdir(parents=True, exist_ok=True)
    cached = profile_json(
        prefix,
        ["account", "list", "--all", "--query", "[].{tenant:tenantId,username:user.name,kind:user.type}"],
        environment,
    )
    if not isinstance(cached, list):
        raise ProfileError("Azure CLI did not return the expected cached-account list.")
    for account in cached:
        verify_account(account, user, tenant)
    result = subprocess.run(
        [*prefix, "login", "--tenant", tenant, "--allow-no-subscriptions", "--output", "none"],
        env=environment,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise ProfileError(
            f"Microsoft sign-in failed with Azure CLI exit {result.returncode}; no profile was verified."
        )
    account = profile_json(
        prefix, ["account", "show", "--query", "{tenant:tenantId,username:user.name,kind:user.type}"], environment
    )
    verify_account(account, user, tenant)


def consumer_present(principal: str, scope: str, subscription: str, role_id: str) -> bool:
    """Recognize only an unconditional Consumer grant covering the exact project."""
    assignments = az(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal,
            "--include-groups",
            "--include-inherited",
            "--scope",
            scope,
            "--subscription",
            subscription,
            "--fill-principal-name",
            "false",
            "--fill-role-definition-name",
            "false",
        ]
    )
    if not isinstance(assignments, list):
        raise ValueError("Expected an Azure role-assignment list.")
    present = False
    for item in assignments:
        assignment = object_value(item)
        if str(assignment.get("roleDefinitionId", "")).rsplit("/", 1)[-1].casefold() != role_id:
            continue
        assigned_scope = assignment.get("scope")
        if not isinstance(assigned_scope, str) or not (
            scope.casefold() == assigned_scope.casefold()
            or scope.casefold().startswith(assigned_scope.casefold().rstrip("/") + "/")
        ):
            continue
        if assignment.get("condition"):
            raise ProfileError(
                "An existing Consumer grant is conditional; review it instead of adding an unconditional grant."
            )
        present = True
    return present


def configure_consumer(
    users: list[str], tenant: str, context: dict[str, JsonValue], run: ValidationRun, *, apply: bool
) -> None:
    """Resolve all users before writing; grant only the verified invoke-only role at one project."""
    subscription = str(UUID(str(context.get("AZURE_SUBSCRIPTION_ID"))))
    scope = context.get("AZURE_AI_PROJECT_ID")
    if (
        not isinstance(scope, str)
        or not re.fullmatch(
            rf"/subscriptions/{re.escape(subscription)}/resourceGroups/[^/?#\s]+"
            r"/providers/Microsoft\.CognitiveServices/accounts/[^/?#\s]+/projects/[^/?#\s]+",
            scope,
            re.IGNORECASE,
        )
        or context.get("AZURE_TENANT_ID") != tenant
    ):
        raise ValueError("RBAC requires the selected environment's exact Foundry project scope and matching tenant.")
    operator = object_value(az(["account", "show", "--subscription", subscription]))
    if operator.get("tenantId") != tenant or str(operator.get("id", "")).casefold() != subscription:
        raise ValueError("The operator CLI subscription/tenant does not match the selected environment.")
    definitions = az(["role", "definition", "list", "--name", CONSUMER_ROLE, "--subscription", subscription])
    if not isinstance(definitions, list) or len(definitions) != 1:
        raise ValueError("Cannot uniquely resolve Foundry Agent Consumer.")
    role = object_value(definitions[0])
    permissions = role.get("permissions")
    if (
        role.get("roleName") != CONSUMER_ROLE
        or role.get("roleType") != "BuiltInRole"
        or not isinstance(permissions, list)
        or len(permissions) != 1
    ):
        raise ValueError("Expected the built-in invoke-only Consumer role.")
    permission = object_value(permissions[0])
    if (
        permission.get("actions") != []
        or permission.get("notActions") != []
        or permission.get("dataActions") != [INTERACT_ACTION]
        or permission.get("notDataActions") != []
        or permission.get("condition") is not None
        or permission.get("conditionVersion") is not None
    ):
        raise ValueError(
            "Consumer role permissions differ from the expected invoke-only contract; review before granting."
        )
    role_id = str(UUID(str(role.get("name"))))
    plans: list[tuple[str, str, Grant]] = []
    for user in users:
        record = object_value(
            az(
                [
                    "rest",
                    "--method",
                    "get",
                    "--url",
                    f"https://graph.microsoft.com/v1.0/users/{quote(user, safe='')}?"
                    "$select=id,userPrincipalName,accountEnabled,userType",
                    "--resource",
                    "https://graph.microsoft.com/",
                    "--subscription",
                    subscription,
                ]
            )
        )
        upn = record.get("userPrincipalName")
        if (
            not isinstance(upn, str)
            or upn.casefold() != user
            or record.get("accountEnabled") is not True
            or record.get("userType") != "Member"
        ):
            raise ValueError("Each requested principal must be an enabled Member user matching its full sign-in name.")
        principal = str(UUID(str(record.get("id"))))
        grant = Grant(CONSUMER_ROLE, scope, role_id, consumer_present(principal, scope, subscription, role_id))
        plans.append((user, principal, grant))
    if len({principal for _, principal, _ in plans}) != len(plans):
        raise ValueError("Requested usernames resolved to the same Entra object; no grants were applied.")
    for user, principal, grant in plans:
        run.result(
            f"Consumer RBAC plan: {user}",
            "PASS",
            expected="Only invoke-only Consumer access, only at the selected Foundry project",
            observed={
                "user": user,
                "principal_id": principal,
                "role": grant.role,
                "scope": scope,
                "present_before": grant.present,
                "apply": apply,
            },
        )
    if apply:
        if any(not grant.present for _, _, grant in plans) and not assignment_allowed(scope, subscription):
            raise ProfileError(
                "The operator lacks role-assignment permission at the selected project; no grants were applied."
            )
        for user, principal, grant in plans:
            if not grant.present:
                create_grant(grant, principal, subscription, principal_type="User")
            if not consumer_present(principal, scope, subscription, role_id):
                raise ProfileError(
                    "Consumer assignment was not visible on readback; earlier grants remain. No automatic retry."
                )
            run.result(
                f"Consumer RBAC readback: {user}",
                "PASS",
                expected="Unconditional Consumer assignment covers the project",
                observed={"created": not grant.present, "scope": scope, "effective_invocation_tested": False},
            )
        run.console.print(
            "RBAC assignment readback is not data-plane authorization proof; propagation may take time. "
            "Other existing roles were not removed or reduced. Earlier grants remain if later sign-in fails."
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Plan safely by default; authenticate only when the operator explicitly runs --apply."""
    args = build_parser().parse_args(argv)
    run = ValidationRun("auth-profiles", "operator", args.output_dir, no_color=args.no_color, verbose=args.verbose)
    try:
        users = parse_users(args.users)
        if args.login_timeout <= 0:
            raise ValueError("--login-timeout must be positive.")
        tenant = args.tenant
        config: dict[str, JsonValue] = {}
        if tenant is None or args.rbac == "consumer":
            config = object_value(
                native_json("azd", ["env", "get-values", "--environment", args.environment, "--output", "json"])
            )
            if tenant is None:
                tenant = config.get("AZURE_TENANT_ID")
        if not isinstance(tenant, str):
            raise ValueError("The selected environment must contain AZURE_TENANT_ID, or provide --tenant.")
        tenant = str(UUID(tenant))
        profiles = [(user, profile_path(tenant, user)) for user in users]
        if len({path for _, path in profiles}) != len(profiles):
            raise ValueError("Requested profiles resolve to the same directory; refusing to mix user caches.")
        run.console.print(
            "Passwords and MFA belong only in Microsoft's sign-in window. Select the requested account explicitly.\n"
            "No agent calls, azd sign-in, or credential copying. "
            "Profile verification does not validate Foundry access."
        )
        for user, path in profiles:
            run.result(
                f"Profile plan: {user}",
                "PASS",
                expected="Isolated local CLI directory; no password handling",
                observed={"tenant": tenant, "user": user, "azure_config_dir": str(path), "apply": args.apply},
            )
        if args.rbac == "consumer":
            configure_consumer(users, tenant, config, run, apply=args.apply)
        if args.apply:
            for user, path in profiles:
                run.console.print(
                    f"Select {user} in Microsoft sign-in. Earlier completed profiles are retained on failure."
                )
                authenticate(user, tenant, path, timeout=args.login_timeout)
                run.result(
                    f"CLI authentication: {user}",
                    "PASS",
                    expected="Requested user and tenant match CLI account metadata",
                    observed={"user": user, "tenant": tenant, "foundry_access_tested": False},
                )
                escaped = str(path).replace("'", "''")
                run.console.print(f"In a separate PowerShell terminal: $env:AZURE_CONFIG_DIR = '{escaped}'")
        else:
            run.console.print(
                "Preview only. No grants, profile caches, or sign-ins were created. "
                "Use --apply for the requested RBAC and sign-in."
            )
    except ProfileError as error:
        run.result(
            "Profile/RBAC setup",
            "FAIL",
            expected="Requested setup completes without widening permissions",
            observed=str(error),
        )
    except subprocess.TimeoutExpired:
        run.result(
            "Authentication timeout",
            "FAIL",
            expected="Complete within the configured deadline",
            observed="CLI timed out. Earlier grants/profiles remain; no automatic retry or cache cleanup.",
        )
    except (ValueError, OSError, CliError) as error:
        run.result(
            "Configuration", "BLOCKED", expected="Valid users, tenant, local paths, and CLI tools", observed=str(error)
        )
    except KeyboardInterrupt:
        run.result(
            "Authentication",
            "BLOCKED",
            expected="Operator completes sign-in",
            observed="Canceled; earlier grants/profiles retained.",
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
