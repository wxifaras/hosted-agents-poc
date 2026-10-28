"""Offline profile setup checks; no real login, password collection, or Azure calls."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.common import auth_profiles as profiles

TENANT = "11111111-1111-4111-8111-111111111111"
USER1 = "user1@example.onmicrosoft.com"
USER2 = "user2@example.onmicrosoft.com"


def cli_result(value: object = None, *, code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["synthetic-az"], code, json.dumps(value), "sensitive-cli-diagnostics")


def account(user: str = USER1, tenant: str = TENANT) -> dict[str, str]:
    return {"username": user, "tenant": tenant, "kind": "user"}


@pytest.fixture
def local_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    local = tmp_path / "local"
    developer = tmp_path / "developer-cli"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(developer))
    monkeypatch.setattr(profiles, "command_prefix", lambda _name: ["synthetic-az"])
    return local, developer


@pytest.mark.parametrize(
    "users",
    [
        "",
        "user1",
        f"{USER1},",
        f"{USER1}, {USER1.upper()}",
        "../user@example.com",
        "user:p@ss@example.com",
        "x\n@example.com",
    ],
)
def test_invalid_user_lists_fail_before_authentication(users: str) -> None:
    with pytest.raises(ValueError):
        profiles.parse_users(users)


def test_user_list_is_trimmed_and_case_insensitive() -> None:
    assert profiles.parse_users(f" {USER1.upper()} , {USER2} ") == [USER1, USER2]


def test_preview_creates_no_profile_or_login(
    local_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = MagicMock(side_effect=AssertionError("A real login or CLI call must not run in this test."))
    monkeypatch.setattr(profiles.subprocess, "run", native)
    assert (
        profiles.main(["--users", f"{USER1},{USER2}", "--tenant", TENANT, "--output-dir", str(tmp_path / "evidence")])
        == 0
    )
    assert not local_paths[0].exists()
    assert os.environ["AZURE_CONFIG_DIR"] == str(local_paths[1])
    native.assert_not_called()


def test_apply_isolates_profiles_and_never_collects_passwords(
    local_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = MagicMock(
        side_effect=[
            cli_result([]),
            cli_result(),
            cli_result(account(USER1)),
            cli_result([]),
            cli_result(),
            cli_result(account(USER2)),
        ]
    )
    monkeypatch.setattr(profiles.subprocess, "run", native)
    code = profiles.main(
        ["--users", f"{USER1},{USER2}", "--tenant", TENANT, "--apply", "--output-dir", str(tmp_path / "evidence")]
    )
    assert code == 0
    assert native.call_count == 6
    first, second = profiles.profile_path(TENANT, USER1), profiles.profile_path(TENANT, USER2)
    assert first != second and first.is_dir() and second.is_dir()
    for index, call in enumerate(native.call_args_list):
        assert call.kwargs["env"]["AZURE_CONFIG_DIR"] == str(first if index < 3 else second)
        assert call.kwargs["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"] == "no"
        command = call.args[0]
        assert not any(
            flag in command for flag in ("--password", "--username", "--service-principal", "--use-device-code")
        )
        if index in (1, 4):
            assert command == [
                "synthetic-az",
                "login",
                "--tenant",
                TENANT,
                "--allow-no-subscriptions",
                "--output",
                "none",
            ]
            assert "capture_output" not in call.kwargs
            assert "input" not in call.kwargs and "stdin" not in call.kwargs
        else:
            assert call.kwargs["capture_output"] is True
    assert os.environ["AZURE_CONFIG_DIR"] == str(local_paths[1])
    assert not local_paths[1].exists()


@pytest.mark.parametrize(
    "wrong", [account(USER2), account(tenant="22222222-2222-4222-8222-222222222222"), {"kind": "servicePrincipal"}]
)
def test_wrong_existing_profile_never_starts_login(
    wrong: dict[str, str], local_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = MagicMock(side_effect=[cli_result([wrong])])
    monkeypatch.setattr(profiles.subprocess, "run", native)
    code = profiles.main(["--users", USER1, "--tenant", TENANT, "--apply", "--output-dir", str(tmp_path / "evidence")])
    assert code == 1 and native.call_count == 1
    assert os.environ["AZURE_CONFIG_DIR"] == str(local_paths[1])


def test_wrong_interactive_account_is_not_reported_as_verified(
    local_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = MagicMock(side_effect=[cli_result([]), cli_result(), cli_result(account(USER2))])
    monkeypatch.setattr(profiles.subprocess, "run", native)
    output = tmp_path / "evidence"
    assert profiles.main(["--users", USER1, "--tenant", TENANT, "--apply", "--output-dir", str(output)]) == 1
    results = [json.loads(line) for line in (next(output.iterdir()) / "results.jsonl").read_text().splitlines()]
    assert not any(result["step"].startswith("CLI authentication:") for result in results)


@pytest.mark.parametrize("timeout", [False, True])
def test_login_failure_preserves_completed_profiles_without_exporting_cli_diagnostics(
    timeout: bool,
    local_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failure = (
        subprocess.TimeoutExpired(["synthetic-az", "login"], 600, output="secret-token", stderr="secret-token")
        if timeout
        else cli_result("secret-token", code=1)
    )
    native = MagicMock(side_effect=[cli_result([]), cli_result(), cli_result(account(USER1)), cli_result([]), failure])
    monkeypatch.setattr(profiles.subprocess, "run", native)
    output = tmp_path / "evidence"
    assert (
        profiles.main(["--users", f"{USER1},{USER2}", "--tenant", TENANT, "--apply", "--output-dir", str(output)]) == 1
    )
    assert native.call_count == 5
    assert profiles.profile_path(TENANT, USER1).is_dir()
    evidence = (next(output.iterdir()) / "results.jsonl").read_text()
    assert f"CLI authentication: {USER1}" in evidence
    assert f"CLI authentication: {USER2}" not in evidence
    assert "secret-token" not in evidence + capsys.readouterr().out
    assert "sensitive-cli-diagnostics" not in evidence


def test_environment_resolution_and_profile_paths_are_safe(
    local_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lookup = MagicMock(return_value={"AZURE_TENANT_ID": TENANT})
    monkeypatch.setattr(profiles, "native_json", lookup)
    assert profiles.main(["--users", USER1, "--output-dir", str(tmp_path / "evidence")]) == 0
    lookup.assert_called_once_with(
        "azd", ["env", "get-values", "--environment", "hosted-agents-poc-dev", "--output", "json"]
    )
    assert not local_paths[0].exists()
    path = profiles.profile_path(TENANT, USER1)
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(path))
    with pytest.raises(ValueError, match="overlap"):
        profiles.profile_path(TENANT, USER1)
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(local_paths[1]))
    monkeypatch.setenv("LOCALAPPDATA", str(profiles.DEFAULT_ENV_FILE.parent))
    with pytest.raises(ValueError, match="repository"):
        profiles.profile_path(TENANT, USER1)


def test_auth_task_only_prompts_for_usernames_and_is_not_part_of_local_checks() -> None:
    config = json.loads((Path(__file__).resolve().parents[2] / ".vscode" / "tasks.json").read_text())
    tasks = {task["label"]: task for task in config["tasks"]}
    task = tasks["Access: Create isolated user auth profiles"]
    assert task["type"] == "process" and task["command"] == "uv"
    assert "--apply" in task["args"] and "${input:authProfileUsers}" in task["args"]
    assert "--password" not in task["args"]
    assert "scripts.common.auth_profiles" in task["args"]
    assert task["args"][task["args"].index("--rbac") + 1] == "${input:authProfileRbac}"
    rbac_input = next(item for item in config["inputs"] if item["id"] == "authProfileRbac")
    assert rbac_input["options"] == ["consumer", "none"] and rbac_input["default"] == "consumer"
    assert all(not item.get("password") for item in config["inputs"])
    assert task["label"] not in tasks["Quality: Run all local checks"]["dependsOn"]
    assert task["label"] not in tasks["Setup: Prepare local environment"]["dependsOn"]


def test_consumer_grant_preview_is_repeatable_and_never_applies_or_signs_in() -> None:
    config = json.loads((Path(__file__).resolve().parents[2] / ".vscode" / "tasks.json").read_text())
    tasks = {task["label"]: task for task in config["tasks"]}
    task = tasks["Access: Preview isolated user Consumer grants"]
    assert task["command"] == "uv"
    assert "scripts.common.auth_profiles" in task["args"]
    assert "--frozen" in task["args"] and "--no-sync" in task["args"]
    assert task["args"][task["args"].index("--rbac") + 1] == "consumer"
    assert "${input:authProfileUsers}" in task["args"]
    assert "${input:infraEnvironment}" in task["args"]
    assert "--apply" not in task["args"] and "dependsOn" not in task
    assert task["label"] not in tasks["Quality: Run all local checks"]["dependsOn"]
    assert task["label"] not in tasks["Setup: Prepare local environment"]["dependsOn"]
