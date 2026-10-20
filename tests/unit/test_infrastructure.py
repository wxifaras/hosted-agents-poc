"""Native Bicep and PowerShell contract tests; mocks must never contact Azure."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.common.reporting import ValidationRun
from scripts.deploy import grant_local_rbac as rbac

ROOT = Path(__file__).resolve().parents[2]
SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def powershell(command: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    if not shutil.which("pwsh"):
        pytest.skip("PowerShell 7 is required for native infrastructure tooling tests.")
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> dict:
    if not shutil.which("az"):
        pytest.skip("Azure CLI with Bicep is required for template compilation.")
    output = tmp_path_factory.mktemp("bicep") / "main.json"
    result = powershell(
        "az bicep build --file $env:POC_TEST_TEMPLATE --outfile $env:POC_TEST_OUTPUT; exit $LASTEXITCODE",
        {"POC_TEST_TEMPLATE": str(ROOT / "infra" / "main.bicep"), "POC_TEST_OUTPUT": str(output)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def all_resources(template: dict) -> list[dict]:
    result = []
    for resource in template.get("resources", []):
        result.append(resource)
        nested = resource.get("properties", {}).get("template")
        if nested:
            result.extend(all_resources(nested))
    return result


def test_storage_is_perimeter_controlled_not_anonymous(compiled: dict) -> None:
    resources = all_resources(compiled)
    storage = next(item for item in resources if item["type"] == "Microsoft.Storage/storageAccounts")
    properties = storage["properties"]
    assert properties["publicNetworkAccess"] == "SecuredByPerimeter"
    assert properties["allowBlobPublicAccess"] is False
    assert properties["allowSharedKeyAccess"] is False
    assert properties["networkAcls"] == {
        "defaultAction": "Deny",
        "bypass": "None",
        "ipRules": [],
        "virtualNetworkRules": [],
    }
    association = next(item for item in resources if item["type"].endswith("/resourceAssociations"))
    assert association["properties"]["accessMode"] == "Enforced"
    rules = [item for item in resources if item["type"].endswith("/accessRules")]
    assert len(rules) == 1
    assert rules[0]["properties"] == {"direction": "Inbound", "addressPrefixes": ["0.0.0.0/0"]}
    container = next(item for item in resources if item["type"].endswith("/containers"))
    assert container["properties"]["publicAccess"] == "None"


def test_foundation_has_no_unapproved_components_or_user_admin_grants(compiled: dict) -> None:
    resources = all_resources(compiled)
    assert not any("capabilityHosts" in item["type"] for item in resources)
    assert not any(
        provider in item["type"]
        for item in resources
        for provider in ("Microsoft.DocumentDB", "Microsoft.Search", "Microsoft.ContainerService")
    )
    model = next(item for item in resources if item["type"] == "Microsoft.CognitiveServices/accounts/deployments")
    assert model["properties"]["model"] == {
        "format": "OpenAI",
        "name": "[parameters('modelName')]",
        "version": "[parameters('modelVersion')]",
    }
    assert "modelDeploymentName" in model["name"]
    assert model["sku"]["name"] == "[parameters('modelSku')]"
    roles = [item for item in resources if item["type"] == "Microsoft.Authorization/roleAssignments"]
    assert len(roles) == 2
    assert all(item["properties"]["principalType"] == "ServicePrincipal" for item in roles)
    assert "AZURE_AI_PROJECT_ENDPOINT" in compiled["outputs"]
    assert not any("CONNECTION_STRING" in name for name in compiled["outputs"])


def test_templates_require_only_deployment_target_inputs(compiled: dict) -> None:
    assert set(compiled["parameters"]) == {
        "environmentName",
        "location",
        "enableAppConfiguration",
        "modelCapacity",
        "modelName",
        "modelVersion",
        "modelDeploymentName",
        "modelSku",
        "acrSku",
        "storageSku",
        "logRetentionDays",
        "logDailyQuotaGb",
        "blobSoftDeleteDays",
    }
    parameters = json.loads((ROOT / "infra" / "main.parameters.json").read_text())["parameters"]
    assert set(parameters) == set(compiled["parameters"]) - {"enableAppConfiguration"}
    for name, default in (
        ("modelName", "gpt-5-mini"),
        ("modelVersion", "2025-08-07"),
        ("modelDeploymentName", "gpt-5-mini"),
    ):
        assert compiled["parameters"][name]["defaultValue"] == default
        assert parameters[name]["value"] == default
    assert parameters["modelCapacity"]["value"] == 10
    preview = json.loads((ROOT / "infra" / "preview.parameters.json").read_text())["parameters"]
    assert preview["modelCapacity"]["value"] == 10
    assert compiled["parameters"]["modelCapacity"]["defaultValue"] == 1


@pytest.mark.parametrize(
    ("name", "default", "allowed", "bounds"),
    [
        ("modelSku", "GlobalStandard", ["GlobalStandard", "DataZoneStandard"], None),
        ("acrSku", "Basic", ["Basic", "Standard", "Premium"], None),
        ("storageSku", "Standard_LRS", ["Standard_LRS", "Standard_ZRS"], None),
        ("logRetentionDays", 30, [30, 60, 90, 180, 365], None),
        ("logDailyQuotaGb", 1, None, (1, 10)),
        ("blobSoftDeleteDays", 7, None, (1, 365)),
    ],
)
def test_operational_parameter_defaults_and_constraints(
    compiled: dict,
    name: str,
    default: object,
    allowed: list | None,
    bounds: tuple[int, int] | None,
) -> None:
    definition = compiled["parameters"][name]
    assert definition["defaultValue"] == default
    if allowed:
        assert definition["allowedValues"] == allowed
    if bounds:
        assert (definition["minValue"], definition["maxValue"]) == bounds
    for file in ("main.parameters.json", "preview.parameters.json"):
        values = json.loads((ROOT / "infra" / file).read_text())["parameters"]
        assert values[name]["value"] == default
    foundation = next(item for item in compiled["resources"] if item["type"] == "Microsoft.Resources/deployments")
    assert foundation["properties"]["parameters"][name]["value"] == f"[parameters('{name}')]"


def test_operational_parameters_reach_actual_resources(compiled: dict) -> None:
    foundation = next(item for item in compiled["resources"] if item["type"] == "Microsoft.Resources/deployments")
    nested = foundation["properties"]["template"]["resources"]
    modules = {item["name"]: item for item in nested if item["type"] == "Microsoft.Resources/deployments"}
    for module, names in {
        "foundry": ("modelSku",),
        "monitoring": ("logRetentionDays", "logDailyQuotaGb"),
        "artifact-storage": ("storageSku", "blobSoftDeleteDays"),
    }.items():
        for name in names:
            assert modules[module]["properties"]["parameters"][name]["value"] == f"[parameters('{name}')]"
    resources = all_resources(compiled)
    registry = next(item for item in resources if item["type"] == "Microsoft.ContainerRegistry/registries")
    assert registry["sku"]["name"] == "[parameters('acrSku')]"
    storage = next(item for item in resources if item["type"] == "Microsoft.Storage/storageAccounts")
    assert storage["sku"]["name"] == "[parameters('storageSku')]"
    blobs = next(item for item in resources if item["type"].endswith("/blobServices"))
    assert blobs["properties"]["deleteRetentionPolicy"]["days"] == "[parameters('blobSoftDeleteDays')]"
    workspace = next(item for item in resources if item["type"] == "Microsoft.OperationalInsights/workspaces")
    assert workspace["properties"]["retentionInDays"] == "[parameters('logRetentionDays')]"
    assert workspace["properties"]["workspaceCapping"]["dailyQuotaGb"] == "[parameters('logDailyQuotaGb')]"


def test_model_parameters_flow_through_modules_and_outputs(compiled: dict) -> None:
    foundation = next(item for item in compiled["resources"] if item["type"] == "Microsoft.Resources/deployments")
    inner = foundation["properties"]["template"]
    foundry = next(
        item
        for item in inner["resources"]
        if item["type"] == "Microsoft.Resources/deployments" and item["name"] == "foundry"
    )
    for key in ("modelName", "modelVersion", "modelDeploymentName"):
        expression = f"[parameters('{key}')]"
        assert foundation["properties"]["parameters"][key]["value"] == expression
        assert foundry["properties"]["parameters"][key]["value"] == expression
    assert inner["outputs"]["resources"]["value"]["modelDeploymentName"] == "[parameters('modelDeploymentName')]"


def test_foundry_child_writes_are_serialized(compiled: dict) -> None:
    resources = all_resources(compiled)
    project = next(item for item in resources if item["type"] == "Microsoft.CognitiveServices/accounts/projects")
    assert any("Microsoft.CognitiveServices/accounts/deployments" in value for value in project["dependsOn"])
    connections = [item for item in resources if item["type"].endswith("/projects/connections")]
    insights = next(item for item in connections if "application-insights" in item["name"])
    assert any("container-registry" in value for value in insights["dependsOn"])


def test_env_example_has_an_explicit_output_or_local_value_source(compiled: dict) -> None:
    keys = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    direct_outputs = {"FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_MODEL_DEPLOYMENT_NAME", "APP_CONFIG_ENDPOINT"}
    local_values = {
        "ENVIRONMENT",
        "SUPERVISOR_PORT",
        "RESEARCH_PORT",
        "DATA_ANALYSIS_PORT",
        "REQUEST_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "APP_CONFIG_KEY_FILTER",
        "APP_CONFIG_LABEL_FILTER",
        "AZURE_CLIENT_ID",
    }
    assert keys == direct_outputs | local_values | {"APPLICATIONINSIGHTS_CONNECTION_STRING"}
    for name in direct_outputs | {"AZURE_APPLICATION_INSIGHTS_ID"}:
        assert compiled["outputs"][name]["type"] == "string"
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in compiled["outputs"]
    assert "AZURE_CLIENT_ID" not in compiled["outputs"]
    assert "ENVIRONMENT" not in compiled["outputs"]


def test_output_metadata_and_optional_appconfig_endpoint(compiled: dict) -> None:
    metadata = {
        "AZURE_SUBSCRIPTION_ID",
        "AZURE_ENV_NAME",
        "AZURE_LOCATION",
        "AZURE_RESOURCE_GROUP",
        "AZURE_TENANT_ID",
        "AZURE_AI_ACCOUNT_ID",
        "AZURE_AI_ACCOUNT_NAME",
        "AZURE_AI_PROJECT_ID",
        "AZURE_AI_PROJECT_NAME",
        "AZURE_CONTAINER_REGISTRY_NAME",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID",
        "AZURE_STORAGE_ACCOUNT_ID",
        "AZURE_STORAGE_ACCOUNT_NAME",
        "AZURE_STORAGE_BLOB_ENDPOINT",
        "AZURE_STORAGE_CONTAINER_ID",
        "AZURE_STORAGE_CONTAINER_NAME",
        "AZURE_LOG_ANALYTICS_WORKSPACE_ID",
        "AZURE_LOG_ANALYTICS_CUSTOMER_ID",
        "AZURE_APP_CONFIGURATION_ID",
    }
    assert metadata <= compiled["outputs"].keys()
    assert all(compiled["outputs"][name]["type"] == "string" for name in metadata)
    foundation = next(item for item in compiled["resources"] if item["type"] == "Microsoft.Resources/deployments")
    resources = foundation["properties"]["template"]["outputs"]["resources"]["value"]
    endpoint = resources["appConfigurationEndpoint"]
    assert endpoint.startswith("[if(parameters('enableAppConfiguration'),")
    assert endpoint.endswith(", '')]")


def test_native_tasks_and_separate_rbac_are_not_deployment_hooks() -> None:
    tasks = json.loads((ROOT / ".vscode" / "tasks.json").read_text())["tasks"]
    native = {task["label"]: task for task in tasks if task["label"].startswith(("Infrastructure:", "Access:"))}
    assert native["Infrastructure: Preview changes (Azure CLI)"]["command"] == "az"
    assert "--verbose" in native["Infrastructure: Preview changes (Azure CLI)"]["args"]
    assert native["Infrastructure: Preview changes (azd)"]["args"] == ["provision", "--preview"]
    assert native["Infrastructure: Apply changes (azd)"]["args"] == ["provision"]
    assert "--apply" not in native["Access: Preview developer permissions"]["args"]
    assert "--apply" in native["Access: Apply developer permissions"]["args"]
    assert "grant_local_rbac" not in (ROOT / "azure.yaml").read_text()
    for name in ("deploy_infrastructure.py", "grant_local_rbac.ps1", "_azure.py"):
        assert not (ROOT / "scripts" / name).exists()


@pytest.mark.parametrize("name", ["check_provision_inputs"])
def test_powershell_syntax(name: str) -> None:
    result = powershell(
        "$tokens=$null; $errors=$null; "
        "$null=[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:POC_TEST_SCRIPT,[ref]$tokens,[ref]$errors); "
        "if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }",
        {"POC_TEST_SCRIPT": str(ROOT / "scripts" / "deploy" / f"{name}.ps1")},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("environment", "subscription", "location", "success"),
    [
        ("", SUBSCRIPTION, "northcentralus", False),
        ("hosted-agents-poc-dev", "", "northcentralus", False),
        ("hosted-agents-poc-dev", SUBSCRIPTION, "", False),
        ("Invalid_Name", SUBSCRIPTION, "northcentralus", False),
        ("hosted-agents-poc-dev", SUBSCRIPTION, "unsupported-region", False),
        ("hosted-agents-poc-dev", SUBSCRIPTION, "northcentralus", True),
        ("hosted-agents-poc-dev", SUBSCRIPTION, "eastus", True),
        ("hosted-agents-poc-dev", SUBSCRIPTION, "westus", True),
    ],
)
def test_deployment_target_checks(environment: str, subscription: str, location: str, success: bool) -> None:
    result = powershell(
        "& $env:POC_TEST_SCRIPT",
        {
            "POC_TEST_SCRIPT": str(ROOT / "scripts" / "deploy" / "check_provision_inputs.ps1"),
            "AZURE_ENV_NAME": environment,
            "AZURE_SUBSCRIPTION_ID": subscription,
            "AZURE_LOCATION": location,
        },
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr


def resource_outputs(*, outside_scope: bool = False) -> dict:
    prefix = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-hosted-agents-poc-dev/providers/"
    resources = {
        "subscriptionId": SUBSCRIPTION,
        "resourceGroup": "rg-hosted-agents-poc-dev",
        "artifactContainerId": prefix
        + "Microsoft.Storage/storageAccounts/poc/blobServices/default/containers/artifacts",
        "foundryAccountId": prefix + "Microsoft.CognitiveServices/accounts/poc",
        "workspaceId": prefix + "Microsoft.OperationalInsights/workspaces/poc",
        "applicationInsightsId": prefix + "Microsoft.Insights/components/poc",
        "appConfigurationId": "",
    }
    if outside_scope:
        resources["artifactContainerId"] = (
            f"/subscriptions/{OTHER}/resourceGroups/other/providers/Microsoft.Storage/poc"
        )
    return resources


def fake_cli(
    monkeypatch: pytest.MonkeyPatch, *, outside_scope: bool = False, existing: bool = False
) -> list[list[str]]:
    calls: list[list[str]] = []

    def invoke(arguments: list[str]) -> object:
        calls.append(arguments)
        if arguments[:2] == ["account", "show"]:
            return {"id": SUBSCRIPTION, "tenantId": OTHER}
        if arguments[:2] == ["ad", "signed-in-user"]:
            return PRINCIPAL
        if arguments[:3] == ["deployment", "sub", "list"]:
            return [
                {
                    "name": "hosted-agents-poc-dev-123",
                    "time": "2026-10-20T18:00:00Z",
                    "environment": "hosted-agents-poc-dev",
                    "state": "Succeeded",
                }
            ]
        if arguments[:3] == ["deployment", "sub", "show"]:
            return resource_outputs(outside_scope=outside_scope)
        if arguments[:3] == ["role", "definition", "list"]:
            return [
                {"roleName": arguments[arguments.index("--name") + 1], "roleType": "BuiltInRole", "name": SUBSCRIPTION}
            ]
        if arguments[:3] == ["role", "assignment", "list"]:
            return [{"roleDefinitionId": f"/roles/{SUBSCRIPTION}", "condition": None}] if existing else []
        if arguments[0] == "rest":
            return {"value": [{"actions": ["*/read"], "notActions": []}]}
        raise AssertionError(f"Unexpected native call: {arguments}")

    monkeypatch.setattr(rbac, "az", invoke)
    return calls


def test_rbac_preview_never_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_cli(monkeypatch)
    assert rbac.main(["--subscription", "test", "--output-dir", str(tmp_path), "--no-color"]) == 0
    assert not any(call[:3] == ["role", "assignment", "create"] or call[0] == "storage" for call in calls)


def test_rbac_apply_prechecks_permission_before_any_grant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_cli(monkeypatch)
    assert rbac.main(["--subscription", "test", "--output-dir", str(tmp_path), "--apply"]) == 2
    assert not any(call[:3] == ["role", "assignment", "create"] for call in calls)


def test_rbac_rejects_wrong_principal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_cli(monkeypatch)
    assert (
        rbac.main(
            ["--subscription", "test", "--output-dir", str(tmp_path), "--expected-principal-id", OTHER, "--apply"]
        )
        == 2
    )
    assert not any(call[:3] == ["role", "assignment", "create"] for call in calls)


def test_rbac_rejects_scope_outside_poc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_cli(monkeypatch, outside_scope=True)
    assert rbac.main(["--subscription", "test", "--output-dir", str(tmp_path), "--apply"]) == 2
    assert not any(call[:3] == ["role", "assignment", "create"] for call in calls)


def test_existing_roles_are_not_regranted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_cli(monkeypatch, existing=True)
    monkeypatch.setattr(rbac, "verify_access", lambda *args, **kwargs: None)
    assert rbac.main(["--subscription", "test", "--output-dir", str(tmp_path), "--apply"]) == 0
    assert not any(call[:3] == ["role", "assignment", "create"] for call in calls)


def test_azd_identity_mismatch_blocks_all_grants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = fake_cli(monkeypatch)

    def mismatch(_principal: str) -> None:
        raise ValueError("az and azd are logged in as different users.")

    monkeypatch.setattr(rbac, "verify_azd_user", mismatch)
    assert (
        rbac.main(["--subscription", "test", "--output-dir", str(tmp_path), "--credential-source", "azd", "--apply"])
        == 2
    )
    assert not any(call[:3] == ["role", "assignment", "create"] for call in calls)


def test_target_tenant_mismatch_blocks_grants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def invoke(arguments: list[str]) -> object:
        assert arguments[:2] == ["account", "show"]
        return {"id": SUBSCRIPTION, "tenantId": PRINCIPAL if "--subscription" in arguments else OTHER}

    monkeypatch.setattr(rbac, "az", invoke)
    assert rbac.main(["--subscription", "test", "--output-dir", str(tmp_path), "--apply"]) == 2


def test_grant_help_does_not_require_azure_login() -> None:
    with pytest.raises(SystemExit) as result:
        rbac.main(["--help"])
    assert result.value.code == 0


def test_role_creation_uses_stable_names_and_exact_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(rbac, "az", lambda arguments: calls.append(arguments))
    scope = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/poc/providers/Microsoft.Storage/storageAccounts/poc"
    grant = rbac.Grant("test-data-role", scope, OTHER, False)
    rbac.create_grant(grant, PRINCIPAL, SUBSCRIPTION)
    rbac.create_grant(grant, PRINCIPAL, SUBSCRIPTION)
    assert calls[0] == calls[1]
    assert calls[0][calls[0].index("--scope") + 1] == scope
    assert calls[0][calls[0].index("--assignee-object-id") + 1] == PRINCIPAL
    assert calls[0][calls[0].index("--role") + 1] == OTHER


@pytest.mark.parametrize("matching", [True, False])
def test_duplicate_race_is_accepted_only_for_matching_unconditioned_assignment(
    monkeypatch: pytest.MonkeyPatch,
    matching: bool,
) -> None:
    scope = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/poc/providers/Microsoft.Storage/storageAccounts/poc"

    def invoke(arguments: list[str]) -> object:
        if arguments[:3] == ["role", "assignment", "create"]:
            raise rbac.CliError("RoleAssignmentExists")
        return [
            {
                "principalId": PRINCIPAL if matching else OTHER,
                "scope": scope,
                "roleDefinitionId": f"/roles/{OTHER}",
                "condition": None,
            }
        ]

    monkeypatch.setattr(rbac, "az", invoke)
    grant = rbac.Grant("test-role", scope, OTHER, False)
    if matching:
        rbac.create_grant(grant, PRINCIPAL, SUBSCRIPTION)
    else:
        with pytest.raises(rbac.CliError, match="did not match"):
            rbac.create_grant(grant, PRINCIPAL, SUBSCRIPTION)


def test_blob_verification_uses_login_and_cleans_only_its_own_blob(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    uploaded = b""

    def invoke(arguments: list[str]) -> object:
        nonlocal uploaded
        calls.append(arguments)
        if arguments[:3] == ["storage", "blob", "upload"]:
            uploaded = Path(arguments[arguments.index("--file") + 1]).read_bytes()
        if arguments[:3] == ["storage", "blob", "download"]:
            Path(arguments[arguments.index("--file") + 1]).write_bytes(uploaded)
        return {
            "tables": [{"name": "PrimaryResult", "columns": [{"name": "POCValidation", "type": "long"}], "rows": [[1]]}]
        }

    monkeypatch.setattr(rbac, "az", invoke)
    resources = {
        **resource_outputs(),
        "storageAccountName": "unit-test",
        "artifactContainerName": "artifacts",
        "workspaceCustomerId": OTHER,
    }
    run = ValidationRun("rbac-test", "unit", tmp_path)
    rbac.verify_access(resources, SUBSCRIPTION, run, timeout=1)
    storage = [call for call in calls if call[0] == "storage"]
    assert [call[2] for call in storage] == ["upload", "download", "delete"]
    assert all(call[call.index("--auth-mode") + 1] == "login" for call in storage)
    assert all(call[call.index("--name") + 1] == f"validation/{run.run_id}.txt" for call in storage)
    assert not (run.path / "synthetic-upload.txt").exists()
    assert not (run.path / "synthetic-download.txt").exists()


def test_monitoring_uses_native_rest_and_exact_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []

    def invoke(arguments: list[str]) -> dict:
        calls.append(arguments)
        return {
            "tables": [{"name": "PrimaryResult", "columns": [{"name": "POCValidation", "type": "long"}], "rows": [[1]]}]
        }

    monkeypatch.setattr(rbac, "az", invoke)
    run = ValidationRun("monitoring", "unit", tmp_path)
    rbac.verify_monitoring({"workspaceCustomerId": OTHER}, SUBSCRIPTION, run)
    assert calls[0][0] == "rest"
    assert calls[0][calls[0].index("--resource") + 1] == "https://api.loganalytics.io"
    assert json.loads(calls[0][calls[0].index("--body") + 1]) == {"query": "print POCValidation=1"}
    assert run.finish() == 0


def test_monitoring_timeout_records_specific_failed_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def timeout(_arguments: list[str]) -> None:
        raise rbac.CliError("az rest timed out")

    monkeypatch.setattr(rbac, "az", timeout)
    run = ValidationRun("monitoring", "unit", tmp_path)
    with pytest.raises(rbac.CliError, match="Monitoring verification failed"):
        rbac.verify_monitoring({"workspaceCustomerId": OTHER}, SUBSCRIPTION, run)
    result = json.loads((run.path / "results.jsonl").read_text())
    assert result["step"] == "Monitoring access"
    assert result["status"] == "FAIL"
    assert "timed out" in result["observed"]


@pytest.mark.parametrize("response", [{}, {"tables": []}, {"tables": [{"rows": [[0]]}]}])
def test_monitoring_rejects_invalid_success_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: dict,
) -> None:
    monkeypatch.setattr(rbac, "az", lambda _arguments: response)
    with pytest.raises(rbac.CliError, match="Monitoring verification failed"):
        rbac.verify_monitoring(
            {"workspaceCustomerId": OTHER}, SUBSCRIPTION, ValidationRun("monitoring", "unit", tmp_path)
        )
