"""Preview/apply hosted model access, Data Analysis telemetry publishing, and project Insights reading."""

import argparse
import json
import subprocess
from collections.abc import Mapping, Sequence
from uuid import UUID, uuid5

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from scripts.common.reporting import ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import (
    CliError,
    Grant,
    assignment_allowed,
    az,
    create_grant,
    native_json,
    object_value,
)
from scripts.run_scenario.hosted_smoke import SERVICES

IMPERSONATION_ACTION = (
    "Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action"
)
TELEMETRY_ROLE = "Monitoring Metrics Publisher"
INSIGHTS_READER_ROLE = "Monitoring Reader"


def grant_insights_role(
    context: Mapping[str, object],
    principal: str,
    run: ValidationRun,
    *,
    role_name: str,
    subject: str,
    apply: bool,
) -> None:
    """Preview/apply one identity's role at this POC's Application Insights resource only."""
    subscription = str(context["AZURE_SUBSCRIPTION_ID"])
    group = str(context.get("AZURE_RESOURCE_GROUP", ""))
    scope = str(context.get("AZURE_APPLICATION_INSIGHTS_ID", ""))
    prefix = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Insights/components/"
    if not group or not scope.casefold().startswith(prefix.casefold()) or scope.count("/") != prefix.count("/"):
        raise ValueError("Selected azd environment lacks a consistent POC Application Insights scope.")
    definitions = az(["role", "definition", "list", "--name", role_name, "--subscription", subscription])
    if not isinstance(definitions, list) or len(definitions) != 1:
        raise ValueError(f"Cannot uniquely resolve {role_name}.")
    role = object_value(definitions[0])
    if role.get("roleName") != role_name or role.get("roleType") != "BuiltInRole":
        raise ValueError(f"Expected the built-in {role_name} role.")
    identifier = str(UUID(str(role["name"])))
    assignments = az(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal,
            "--fill-principal-name",
            "false",
            "--fill-role-definition-name",
            "false",
            "--include-inherited",
            "--scope",
            scope,
            "--subscription",
            subscription,
        ]
    )
    if not isinstance(assignments, list):
        raise ValueError("Expected a role-assignment list.")
    present = any(
        str(object_value(item).get("roleDefinitionId", "")).endswith("/" + identifier)
        and not object_value(item).get("condition")
        for item in assignments
    )
    observed = {"principal_id": principal, "scope": scope, "role": role_name, "present": present, "apply": apply}
    run.result(
        f"{subject} telemetry",
        "PASS",
        expected="Verified identity and resource-scoped Application Insights access plan",
        observed=observed,
    )
    if apply and not present:
        if not assignment_allowed(scope, subscription):
            raise ValueError("Cannot assign roles at Application Insights; administrator action is required.")
        assignment_name: str | None = None
        if role_name == INSIGHTS_READER_ROLE:
            # Match Bicep guid(scope, principalId, roleId), so later provisioning reuses this assignment.
            assignment_name = str(
                uuid5(UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830"), f"{scope}-{principal}-{identifier}")
            )
        create_grant(
            Grant(role_name, scope, identifier, False),
            principal,
            subscription,
            principal_type="ServicePrincipal",
            assignment_name=assignment_name,
        )
        run.result(
            f"{subject} telemetry grant",
            "PASS",
            expected=f"Scoped {role_name} assignment",
            observed={"principal_id": principal, "scope": scope},
        )


def grant_data_analysis_telemetry(
    context: Mapping[str, object], principal: str, run: ValidationRun, *, apply: bool
) -> None:
    """Let only Data Analysis publish Entra-authenticated telemetry."""
    grant_insights_role(
        context, principal, run, role_name=TELEMETRY_ROLE, subject=SERVICES["data_analysis"], apply=apply
    )


def grant_project_insights(context: Mapping[str, object], run: ValidationRun, *, apply: bool) -> None:
    """Resolve the selected project's managed identity and grant it Insights read access."""
    account = str(context["AZURE_AI_ACCOUNT_ID"])
    project = str(context.get("AZURE_AI_PROJECT_ID", ""))
    if not project.startswith(account + "/projects/") or project.count("/") != account.count("/") + 2:
        raise ValueError("Insights requires the exact selected Foundry project ARM scope.")
    identity = az(
        [
            "resource",
            "show",
            "--ids",
            project,
            "--api-version",
            "2025-06-01",
            "--query",
            "identity.principalId",
            "--subscription",
            str(context["AZURE_SUBSCRIPTION_ID"]),
        ]
    )
    principal = str(UUID(str(identity)))
    grant_insights_role(context, principal, run, role_name=INSIGHTS_READER_ROLE, subject="Foundry project", apply=apply)


def grant_supervisor_delegation(
    context: Mapping[str, object], principal: str, run: ValidationRun, *, apply: bool
) -> None:
    """Preview/apply the single custom action for Supervisor at this exact project."""
    account = str(context["AZURE_AI_ACCOUNT_ID"])
    project = str(context.get("AZURE_AI_PROJECT_ID", ""))
    subscription = str(context["AZURE_SUBSCRIPTION_ID"])
    if not project.startswith(account + "/projects/") or project.count("/") != account.count("/") + 2:
        raise ValueError("Delegation requires the exact selected Foundry project ARM scope.")
    name = f"POC Supervisor Delegation {account.rsplit('/', 1)[-1]}"
    definitions = az(["role", "definition", "list", "--name", name, "--subscription", subscription])
    if not isinstance(definitions, list) or len(definitions) > 1:
        raise ValueError("Cannot uniquely resolve the POC custom delegation role.")
    definition: dict[str, object] = {
        "Name": name,
        "IsCustom": True,
        "Description": "POC Supervisor verified end-user delegation; no model or storage access.",
        "Actions": [],
        "NotActions": [],
        "DataActions": [IMPERSONATION_ACTION],
        "NotDataActions": [],
        "AssignableScopes": [account],
    }
    if apply and not assignment_allowed(project, subscription):
        raise ValueError("Administrator permission is required to assign the project-scoped delegation role.")
    if not definitions and apply:
        definitions = [az(["role", "definition", "create", "--role-definition", json.dumps(definition)])]
    identifier: str | None = None
    present = False
    if definitions:
        role = object_value(definitions[0])
        expected = {"actions": [], "notActions": [], "dataActions": [IMPERSONATION_ACTION], "notDataActions": []}
        permissions = role.get("permissions")
        if role.get("roleType") != "CustomRole" or not isinstance(permissions, list) or len(permissions) != 1:
            raise ValueError("Existing custom role has unexpected permissions; refusing to reuse it.")
        permission = object_value(permissions[0])
        if (
            any(permission.get(key) != value for key, value in expected.items())
            or permission.get("condition") is not None
            or permission.get("conditionVersion") is not None
            or set(permission) - set(expected) - {"condition", "conditionVersion"}
        ):
            raise ValueError("Existing custom role has unexpected permissions; refusing to reuse it.")
        if role.get("assignableScopes") != [account]:
            raise ValueError("Existing custom role has unexpected assignable scopes.")
        identifier = str(UUID(str(role["name"])))
        assignments = az(
            [
                "role",
                "assignment",
                "list",
                "--assignee-object-id",
                principal,
                "--fill-principal-name",
                "false",
                "--fill-role-definition-name",
                "false",
                "--scope",
                project,
                "--subscription",
                subscription,
            ]
        )
        if not isinstance(assignments, list):
            raise ValueError("Expected project role assignments.")
        present = any(
            str(object_value(item).get("roleDefinitionId", "")).endswith("/" + identifier)
            and str(object_value(item).get("scope", "")).casefold() == project.casefold()
            and not object_value(item).get("condition")
            for item in assignments
        )
        if apply and not present:
            create_grant(
                Grant(name, project, identifier, False), principal, subscription, principal_type="ServicePrincipal"
            )
    run.result(
        "Supervisor delegation",
        "PASS",
        expected="Only the actual Supervisor identity, only this project, only UserIdentityImpersonation",
        observed={
            "principal_id": principal,
            "scope": project,
            "data_action": IMPERSONATION_ACTION,
            "role_definition_exists": bool(definitions),
            "assignment_present_before": present,
            "apply": apply,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    """Keep agent permissions explicit and independent from local-user grants."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--delegation",
        action="store_true",
        help="Also preview/apply only the Supervisor's explicit project-scoped user-delegation action.",
    )
    add_output_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Resolve deployed identities and preview/apply their resource-scoped model and telemetry roles."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "hosted-agent-rbac",
        "apply" if args.apply else "preview",
        args.output_dir,
        no_color=args.no_color,
        verbose=args.verbose,
    )
    try:
        context = object_value(
            native_json("azd", ["env", "get-values", "--environment", args.environment, "--output", "json"])
        )
        subscription = str(UUID(str(context.get("AZURE_SUBSCRIPTION_ID"))))
        group = str(context.get("AZURE_RESOURCE_GROUP", ""))
        scope = str(context.get("AZURE_AI_ACCOUNT_ID", ""))
        prefix = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.CognitiveServices/accounts/"
        if not scope.casefold().startswith(prefix.casefold()) or scope.count("/") != prefix.count("/"):
            raise ValueError("Selected azd environment lacks a consistent POC Foundry account scope.")
        current = object_value(az(["account", "show", "--subscription", subscription]))
        if current.get("tenantId") != context.get("AZURE_TENANT_ID"):
            raise ValueError("CLI target tenant does not match the azd environment.")
        definitions = az(["role", "definition", "list", "--name", "Foundry User", "--subscription", subscription])
        if not isinstance(definitions, list) or len(definitions) != 1:
            raise ValueError("Cannot uniquely resolve Foundry User.")
        role = object_value(definitions[0])
        if role.get("roleName") != "Foundry User" or role.get("roleType") != "BuiltInRole":
            raise ValueError("Expected the built-in Foundry User role.")
        identifier = str(UUID(str(role["name"])))
        missing: list[tuple[str, str]] = []
        principals: dict[str, str] = {}
        for service in SERVICES.values():
            endpoint = str(context.get("FOUNDRY_PROJECT_ENDPOINT", ""))
            if not endpoint.startswith("https://"):
                raise ValueError("Missing HTTPS Foundry project endpoint.")
            with (
                AzureCliCredential(process_timeout=60) as credential,
                AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
            ):
                definition = object_value(project.agents.get(service).as_dict())
            principal = str(UUID(str(object_value(definition.get("instance_identity")).get("principal_id"))))
            principals[service] = principal
            assignments = az(
                [
                    "role",
                    "assignment",
                    "list",
                    "--assignee-object-id",
                    principal,
                    "--fill-principal-name",
                    "false",
                    "--fill-role-definition-name",
                    "false",
                    "--include-inherited",
                    "--scope",
                    scope,
                    "--subscription",
                    subscription,
                ]
            )
            if not isinstance(assignments, list):
                raise ValueError("Expected a role-assignment list.")
            present = any(
                str(object_value(item).get("roleDefinitionId", "")).endswith("/" + identifier)
                and not object_value(item).get("condition")
                for item in assignments
            )
            run.result(
                service,
                "PASS",
                expected="Actual deployed instance identity and exact scoped model access plan",
                observed={
                    "principal_id": principal,
                    "scope": scope,
                    "role": "Foundry User",
                    "present": present,
                    "apply": args.apply,
                },
            )
            if not present:
                missing.append((service, principal))
        if args.apply and missing:
            if not assignment_allowed(scope, subscription):
                raise ValueError("Cannot assign roles at the Foundry account; administrator action is required.")
            for service, principal in missing:
                create_grant(
                    Grant("Foundry User", scope, identifier, False),
                    principal,
                    subscription,
                    principal_type="ServicePrincipal",
                )
                run.result(
                    service + " grant",
                    "PASS",
                    expected="Scoped Foundry User assignment",
                    observed={"principal_id": principal, "scope": scope},
                )
        grant_project_insights(context, run, apply=args.apply)
        grant_data_analysis_telemetry(context, principals[SERVICES["data_analysis"]], run, apply=args.apply)
        run.console.print(
            "Insights reading is limited to the Foundry project identity at its Application Insights resource."
        )
        if args.delegation:
            grant_supervisor_delegation(context, principals[SERVICES["supervisor"]], run, apply=args.apply)
            run.console.print(
                "Delegation is limited to Supervisor at this project. Telemetry publishing is limited to "
                "Data Analysis at its Application Insights resource. No Blob roles added."
            )
        else:
            run.console.print(
                "Telemetry publishing is limited to Data Analysis at its Application Insights resource. "
                "No Blob roles, user impersonation permission, or orchestration access added."
            )
    except (AzureError, CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        run.result(
            "Agent permissions",
            "BLOCKED" if isinstance(error, ValueError) else "FAIL",
            expected="Verified instance identities and scoped roles",
            observed=str(error),
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
