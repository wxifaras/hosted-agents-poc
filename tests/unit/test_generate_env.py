"""Local environment generation tests use temporary files and mocked Azure outputs."""

import argparse
import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from scripts.common.reporting import ValidationRun
from scripts.deploy import generate_env as generator

SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"


def record() -> dict:
    values = {
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_AI_PROJECT_NAME": "test-project",
        "AZURE_ENV_NAME": "hosted-agents-poc-dev",
        "FOUNDRY_PROJECT_ENDPOINT": "https://unit.services.ai.azure.com/api/projects/test-project",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "test-model",
        "APP_CONFIG_ENDPOINT": "",
    }
    return {
        "properties": {
            "provisioningState": "Succeeded",
            "outputs": {key: {"type": "String", "value": value} for key, value in values.items()},
        }
    }


def test_maps_only_approved_outputs() -> None:
    assert set(generator.mapped_values(record(), SUBSCRIPTION)) == set(generator.MAPPED_KEYS)


def test_accepts_azure_cli_camelcased_output_keys() -> None:
    payload = record()
    outputs = payload["properties"]["outputs"]
    payload["properties"]["outputs"] = {key.lower(): value for key, value in outputs.items()}
    assert generator.mapped_values(payload, SUBSCRIPTION)["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == "test-model"


@pytest.mark.parametrize("failure", ["state", "subscription", "missing", "newline", "endpoint", "project"])
def test_invalid_outputs_fail_explicitly(failure: str) -> None:
    payload = record()
    outputs = payload["properties"]["outputs"]
    if failure == "state":
        payload["properties"]["provisioningState"] = "Failed"
    elif failure == "subscription":
        outputs["AZURE_SUBSCRIPTION_ID"]["value"] = "other-subscription"
    elif failure == "missing":
        del outputs["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    elif failure == "newline":
        outputs["AZURE_AI_MODEL_DEPLOYMENT_NAME"]["value"] = "model\nSECRET=value"
    elif failure == "endpoint":
        outputs["FOUNDRY_PROJECT_ENDPOINT"]["value"] = "http://unsafe/api/projects/test-project"
    else:
        outputs["AZURE_AI_PROJECT_NAME"]["value"] = "other-project"
    with pytest.raises(ValueError):
        generator.mapped_values(payload, SUBSCRIPTION)


def test_preserves_local_settings_and_comments() -> None:
    text = "# local\r\nENVIRONMENT=development\r\nFOUNDRY_PROJECT_ENDPOINT=old\r\n"
    text += "APPLICATIONINSIGHTS_CONNECTION_STRING=keep-local-value\r\nAZURE_CLIENT_ID=\r\nCUSTOM=custom\r\n"
    values = generator.mapped_values(record(), SUBSCRIPTION)
    rendered = generator.render_env(text, values)
    assert "# local\r\nENVIRONMENT=development\r\n" in rendered
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING=keep-local-value\r\n" in rendered
    assert "CUSTOM=custom\r\n" in rendered
    assert generator.render_env(rendered, values) == rendered
    assert rendered.count("FOUNDRY_PROJECT_ENDPOINT=") == 1


def test_duplicate_mapped_keys_fail() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        generator.render_env("APP_CONFIG_ENDPOINT=\nexport APP_CONFIG_ENDPOINT=old\n", {"APP_CONFIG_ENDPOINT": ""})


def test_safe_writes_and_concurrent_change_guard(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    generator.write_env(target, "A=first\n", previous=None)
    with pytest.raises(FileExistsError):
        generator.write_env(target, "A=overwritten\n", previous=None)
    original = target.read_bytes()
    target.write_text("A=changed\n")
    with pytest.raises(ValueError, match="changed"):
        generator.write_env(target, "A=lost\n", previous=original)
    generator.write_env(target, "A=updated\n", previous=target.read_bytes())
    assert target.read_text() == "A=updated\n"
    assert not list(tmp_path.glob(".env-update-*"))


def test_create_update_and_refuse_implicit_overwrite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text(
        "ENVIRONMENT=development\nFOUNDRY_PROJECT_ENDPOINT=\nAZURE_AI_MODEL_DEPLOYMENT_NAME=\n"
        "APP_CONFIG_ENDPOINT=\nAPPLICATIONINSIGHTS_CONNECTION_STRING=\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def az(arguments: list[str]) -> dict:
        calls.append(arguments)
        return {"id": SUBSCRIPTION} if arguments[0] == "account" else record()

    monkeypatch.setattr(generator, "az", az)
    args = argparse.Namespace(
        subscription="test", deployment="successful", environment="hosted-agents-poc-dev", update=False
    )
    run = ValidationRun("test-env", "unit", tmp_path / "evidence")
    generator.execute(args, run)
    assert dotenv_values(tmp_path / ".env")["AZURE_AI_MODEL_DEPLOYMENT_NAME"] == "test-model"
    assert "ENVIRONMENT=development" in (tmp_path / ".env").read_text()
    with pytest.raises(ValueError, match="already exists"):
        generator.execute(args, run)
    args.update = True
    generator.execute(args, run)
    assert all(call[:2] in (["account", "show"], ["deployment", "sub"]) for call in calls)
    evidence = (run.path / "results.jsonl").read_text()
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in evidence
    assert json.loads(evidence.splitlines()[-1])["status"] == "PASS"


def test_help_requires_no_azure() -> None:
    with pytest.raises(SystemExit) as result:
        generator.main(["--help"])
    assert result.value.code == 0
