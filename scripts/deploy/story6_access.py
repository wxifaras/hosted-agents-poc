"""Preview/apply the existing POC runtime role to the new coordinator only.

No old-agent, user, storage, registry, telemetry or impersonation grants.
Preview is read-only. --apply requires the operator's role-assignment rights.
Exit codes: 0 inspected/applied, 2 blocked or invalid.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from scripts.deploy.grant_local_rbac import (
    CliError,
    Grant,
    assignment_allowed,
    az,
    create_grant,
    object_value,
)
from scripts.deploy.story6_package import ROOT, SERVICE, deployment_context

ROLE = "Foundry User"


@dataclass(frozen=True)
class RuntimeIdentity:
    """Verified deployed target; never accept an arbitrary principal from CLI input."""

    principal: str
    version: str


def runtime_identity(project: AIProjectClient) -> RuntimeIdentity:
    """Resolve only the active dedicated coordinator's actual identity."""
    data = project.agents.get(SERVICE).as_dict()
    latest = object_value(object_value(data.get("versions")).get("latest"))
    identity = object_value(data.get("instance_identity"))
    if data.get("name") != SERVICE or latest.get("status") != "active":
        raise ValueError("The dedicated Story6 coordinator must be active before runtime access is applied.")
    version = latest.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Coordinator version is unavailable.")
    return RuntimeIdentity(str(UUID(str(identity.get("principal_id")))), version)


def inspect_role(context: dict[str, str], identity: RuntimeIdentity) -> Grant:
    """Use the verified built-in role and selected account scope, never role-name guesses."""
    scope = context["AZURE_AI_PROJECT_ID"].rsplit("/", 2)[0]
    subscription = context["AZURE_SUBSCRIPTION_ID"]
    definitions = az(["role", "definition", "list", "--name", ROLE, "--subscription", subscription])
    if not isinstance(definitions, list) or len(definitions) != 1:
        raise ValueError("Cannot uniquely resolve the built-in Foundry User role.")
    definition = object_value(definitions[0])
    if definition.get("roleType") != "BuiltInRole" or definition.get("roleName") != ROLE:
        raise ValueError("Expected the built-in Foundry User role.")
    identifier = str(UUID(str(definition.get("name"))))
    assignments = az(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            identity.principal,
            "--scope",
            scope,
            "--include-inherited",
            "--fill-principal-name",
            "false",
            "--subscription",
            subscription,
        ]
    )
    if not isinstance(assignments, list):
        raise ValueError("Role-assignment inventory is unavailable.")
    relevant = [
        object_value(item)
        for item in assignments
        if str(object_value(item).get("roleDefinitionId", "")).endswith("/" + identifier)
    ]
    if any(item.get("condition") for item in relevant):
        raise ValueError("An existing conditional grant requires review; no unconditional grant will be added.")
    return Grant(ROLE, scope, identifier, bool(relevant))


def apply_if_missing(context: dict[str, str], identity: RuntimeIdentity, grant: Grant, *, apply: bool) -> bool:
    """Reuse the stable-name grant helper; never broaden another actor's access."""
    if not apply or grant.present:
        return False
    if not assignment_allowed(grant.scope, context["AZURE_SUBSCRIPTION_ID"]):
        raise ValueError("Operator cannot assign roles at the selected Foundry account; administrator action required.")
    create_grant(grant, identity.principal, context["AZURE_SUBSCRIPTION_ID"], principal_type="ServicePrincipal")
    return True


def build_parser() -> argparse.ArgumentParser:
    """Keep cloud writes behind an explicit apply option."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Inspect one target and optionally grant its missing runtime role."""
    args = build_parser().parse_args(argv)
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
            identity = runtime_identity(project)
            grant = inspect_role(context, identity)
            if args.apply and runtime_identity(project) != identity:
                raise ValueError("Coordinator identity/version changed during review; inspect again before applying.")
            applied = apply_if_missing(context, identity, grant, apply=args.apply)
            if applied and not inspect_role(context, identity).present:
                raise ValueError(
                    "Assignment write returned but readback is not visible; inspect, do not automatically reapply."
                )
        print(
            json.dumps(
                {
                    "agent": SERVICE,
                    "version": identity.version,
                    "role": ROLE,
                    "scope": "selected Foundry account",
                    "present": grant.present or applied,
                    "applied": applied,
                    "old_agents_changed": False,
                    "effective_runtime_access_tested": False,
                },
                indent=2,
            )
        )
    except (AzureError, CliError, OSError, ValueError) as error:
        print(f"Story6 runtime access stopped: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
