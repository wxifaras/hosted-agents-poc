"""Offline packaging isolation, binding integrity and credential-exclusion tests."""

import json
from pathlib import Path
from shutil import copyfile

import pytest
import yaml
from dotenv import dotenv_values

from scripts.deploy import story6_package as package

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = "hosted-agents-poc-dev"
SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
PREFIX = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-poc/providers/"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("azure.yaml", "pyproject.toml", "uv.lock", "LICENSE"):
        copyfile(ROOT / name, root / name)
    template = root / "experiments" / "story6"
    template.mkdir(parents=True)
    for name in ("azure.yaml", "Dockerfile", ".dockerignore"):
        copyfile(ROOT / "experiments" / "story6" / name, template / name)
    for relative in (
        "src/backend/app/agents/story6/main.py",
        "src/backend/app/story6_host.py",
        "src/backend/app/services/story6_approval.py",
        "scripts/__init__.py",
        "scripts/report_assets/index.html",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic test fixture", encoding="utf-8")
    values = {
        "AZURE_ENV_NAME": ENVIRONMENT,
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_LOCATION": "northcentralus",
        "AZURE_RESOURCE_GROUP": "rg-poc",
        "AZURE_AI_PROJECT_ID": PREFIX + "Microsoft.CognitiveServices/accounts/acct/projects/proj",
        "AZURE_AI_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/proj",
        "FOUNDRY_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/proj",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5-mini",
        "AZURE_CONTAINER_REGISTRY_NAME": "acrpoc1",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "acrpoc1.azurecr.io",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID": PREFIX + "Microsoft.ContainerRegistry/registries/acrpoc1",
        "AZURE_CLIENT_SECRET": "TEST_SECRET_SHOULD_NOT_COPY",
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "TEST_TELEMETRY_SHOULD_NOT_COPY",
        "AGENT_SUPERVISOR_AGENT_NAME": "supervisor-agent",
        "AGENT_RESEARCH_AGENT_VERSION": "10",
    }
    config = root / ".azure"
    env_file = config / ENVIRONMENT / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()), encoding="utf-8")
    (config / "config.json").write_text(json.dumps({"version": 1, "defaultEnvironment": ENVIRONMENT}))
    (root / ".env").write_text("ROOT_SECRET=TEST_SECRET_SHOULD_NOT_COPY\n")
    (root / "scripts" / ".env").write_text("SCRIPT_SECRET=TEST_SECRET_SHOULD_NOT_COPY\n")
    return root


def test_isolated_package_keeps_original_manifest_env_and_secrets_out_of_build(workspace: Path) -> None:
    root_manifest = (workspace / "azure.yaml").read_bytes()
    source_env = workspace / ".azure" / ENVIRONMENT / ".env"
    original_env = source_env.read_bytes()
    prepared = package.prepare(workspace, ENVIRONMENT, workspace / "artifacts/story-6/packages")
    assert (workspace / "azure.yaml").read_bytes() == root_manifest
    assert source_env.read_bytes() == original_env
    assert package.verify(workspace, prepared).service == "story6-coordinator-agent"
    manifest = yaml.safe_load((prepared / "azure.yaml").read_text())
    assert set(manifest["services"]) == {"story6-coordinator-agent"}
    assert "infra" not in manifest and "hooks" not in manifest
    service = manifest["services"]["story6-coordinator-agent"]
    assert service["host"] == "azure.ai.agent" and service["kind"] == "hosted"
    assert service["docker"] == {"path": "Dockerfile", "context": ".", "remoteBuild": True}
    assert service["env"]["STORY6_ENABLED"] == "true"
    assert service["env"]["STORY6_REQUEST_TIMEOUT_SECONDS"] == "120"
    assert "RESEARCH_TOOLBOX_ENDPOINT" not in service["env"]
    docker = (prepared / "Dockerfile").read_text()
    assert 'CMD ["/app/.venv/bin/python", "-m", "app.agents.story6.main"]' in docker
    assert "COPY . ." not in docker and "EXPOSE 8088" in docker
    ignored = (prepared / ".dockerignore").read_text()
    assert ignored.splitlines()[0] == "**"
    assert "**/.azure/" in ignored and "**/.env" in ignored
    staged_env = (prepared / ".azure" / ENVIRONMENT / ".env").read_text()
    assert "TEST_SECRET" not in staged_env and "TEST_TELEMETRY" not in staged_env and "AGENT_" not in staged_env
    assert not (prepared / ".env").exists() and not (prepared / "scripts/.env").exists()
    record = (prepared / "deployment-review.json").read_text()
    assert SUBSCRIPTION not in record and TENANT not in record
    assert dotenv_values(prepared / ".azure" / ENVIRONMENT / ".env")["AZURE_ENV_NAME"] == ENVIRONMENT


@pytest.mark.parametrize("change", ["staged-source", "source", "root-manifest", "context", "extra-file"])
def test_integrity_gate_rejects_drift(workspace: Path, change: str) -> None:
    prepared = package.prepare(workspace, ENVIRONMENT, workspace / "artifacts/story-6/packages")
    if change == "staged-source":
        (prepared / "src/backend/app/story6_host.py").write_text("# changed")
    elif change == "source":
        (workspace / "src/backend/app/story6_host.py").write_text("# changed")
    elif change == "root-manifest":
        with (workspace / "azure.yaml").open("a") as stream:
            stream.write("\n# changed\n")
    elif change == "context":
        with (prepared / ".azure" / ENVIRONMENT / ".env").open("a") as stream:
            stream.write('AZURE_AI_MODEL_DEPLOYMENT_NAME="changed"\n')
    else:
        (prepared / "src/backend/app/unreviewed.json").write_text("{}")
    with pytest.raises(ValueError):
        package.verify(workspace, prepared)


@pytest.mark.parametrize(
    "assignment",
    [
        'AZURE_ENV_NAME="different"',
        'AZURE_AI_PROJECT_ENDPOINT="http://acct.services.ai.azure.com/api/projects/proj"',
        'AZURE_AI_PROJECT_ENDPOINT="https://acct.services.ai.azure.com/api/projects/other"',
        'FOUNDRY_PROJECT_ENDPOINT="https://other.services.ai.azure.com/api/projects/proj"',
        'AZURE_CONTAINER_REGISTRY_NAME="otherregistry"',
        'AZURE_CONTAINER_REGISTRY_ENDPOINT="https://acrpoc1.azurecr.io"',
        'AZURE_AI_PROJECT_ID="/subscriptions/foreign/resourceGroups/rg-poc/projects/proj"',
        'AZURE_AI_MODEL_DEPLOYMENT_NAME="${UNRESOLVED}"',
    ],
)
def test_inconsistent_target_bindings_fail_before_writing(workspace: Path, assignment: str) -> None:
    with (workspace / ".azure" / ENVIRONMENT / ".env").open("a") as stream:
        stream.write(assignment + "\n")
    with pytest.raises(ValueError):
        package.prepare(workspace, ENVIRONMENT, workspace / "artifacts/story-6/packages")
    assert not (workspace / "artifacts").exists()


def test_preview_does_not_read_environment_or_write_files(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(package, "ROOT", workspace)
    monkeypatch.setattr(
        package, "dotenv_values", lambda *args, **kwargs: pytest.fail("No environment read in preview.")
    )
    assert package.main(["--environment", ENVIRONMENT]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["cloud_requests"] == 0 and not observed["source_env_values_read"]
    assert not observed["deployment"] and not observed["grants"]
    assert not (workspace / "artifacts").exists()


def test_prepare_and_verify_cli_never_deploy(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(package, "ROOT", workspace)
    assert package.main(["--environment", ENVIRONMENT, "--prepare"]) == 0
    prepared = next((workspace / "artifacts/story-6/deployment-context").iterdir())
    assert package.main(["--environment", ENVIRONMENT, "--verify", str(prepared)]) == 0


@pytest.mark.parametrize("environment", ["../other", "", "UPPER", "a" * 41])
def test_environment_path_traversal_is_rejected(workspace: Path, environment: str) -> None:
    with pytest.raises(ValueError):
        package.prepare(workspace, environment, workspace / "artifacts/story-6/packages")


def test_output_cannot_replace_the_original_workspace(workspace: Path) -> None:
    with pytest.raises(ValueError, match="below artifacts/story-6"):
        package.prepare(workspace, ENVIRONMENT, workspace)


def test_toolbox_mode_is_staged_without_modifying_root_environment(workspace: Path) -> None:
    source = workspace / ".azure" / ENVIRONMENT / ".env"
    before = source.read_bytes()
    prepared = package.prepare(workspace, ENVIRONMENT, workspace / "artifacts/story-6/packages", toolbox_version="1")
    assert source.read_bytes() == before
    assert package.verify(workspace, prepared).toolbox_version == "1"
    assert dotenv_values(prepared / ".azure" / ENVIRONMENT / ".env")["STORY6_TOOLBOX_VERSION"] == "1"


def test_editor_package_tasks_have_no_deploy_or_permission_dependencies() -> None:
    document = json.loads((ROOT / ".vscode/tasks.json").read_text())
    tasks = {item["label"]: item for item in document["tasks"]}
    labels = (
        "Agents: Preview Story6 deployment package",
        "Agents: Prepare isolated Story6 deployment package",
        "Agents: Verify Story6 deployment package",
    )
    for label in labels:
        task = tasks[label]
        assert "dependsOn" not in task
        assert task["command"] == "uv"
        assert "scripts.deploy.story6_package" in task["args"]
        assert "--no-sync" in task["args"] and "--frozen" in task["args"]
        assert not any(argument in {"deploy", "provision", "--apply", "--all"} for argument in task["args"])


def test_deploy_task_targets_only_the_verified_new_coordinator() -> None:
    document = json.loads((ROOT / ".vscode/tasks.json").read_text())
    task = next(item for item in document["tasks"] if item["label"] == "Agents: Deploy Story6 coordinator")
    assert task["command"] == "azd"
    assert task["args"] == [
        "--cwd",
        "${input:story6PackageDir}",
        "deploy",
        "story6-coordinator-agent",
        "--environment",
        "${input:infraEnvironment}",
        "--no-prompt",
    ]
    assert task["dependsOn"] == ["Agents: Verify Story6 deployment package"]
    assert "verified isolated package" in task["detail"]
    assert "--all" not in task["args"] and "provision" not in task["args"]
    assert "runOptions" not in task and "env" not in task.get("options", {})
