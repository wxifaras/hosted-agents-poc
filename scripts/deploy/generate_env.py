"""Create local .env from the example and successful Bicep outputs using Rich.

Existing files require --update; only mapped deployment fields are changed.
Exit codes: 0 written/unchanged, 1 operational failure, 2 blocked inputs.
No Azure resources, role assignments, model calls, or telemetry changes are made.
"""

import argparse
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import JsonValue

from scripts.common.deployments import resolve_deployment
from scripts.common.reporting import ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import CliError, az, object_value

ROOT = Path(__file__).resolve().parents[2]
MAPPED_KEYS = ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_MODEL_DEPLOYMENT_NAME", "APP_CONFIG_ENDPOINT")


def output_string(outputs: dict[str, JsonValue], key: str) -> str:
    """Require an explicitly returned string output, including optional empty endpoints."""
    entry = object_value(outputs.get(key))
    value = entry.get("value")
    if entry.get("type") != "String" or not isinstance(value, str):
        raise ValueError(f"Bicep output {key} must be a string.")
    if any(character in value for character in "\r\n\x00"):
        raise ValueError(f"Bicep output {key} contains an invalid control character.")
    return value


def mapped_values(record: dict[str, JsonValue], subscription: str) -> dict[str, str]:
    """Reject failed deployments, wrong subscriptions, and inconsistent project output bindings."""
    properties = object_value(record.get("properties"))
    if properties.get("provisioningState") != "Succeeded":
        raise ValueError("Select a successful subscription deployment; partial/failed outputs cannot generate .env.")
    raw_outputs = object_value(properties.get("outputs"))
    # Azure CLI normalizes ARM response keys (AZURE_* can appear as azurE_*).
    outputs = {key.upper(): value for key, value in raw_outputs.items()}
    if len(outputs) != len(raw_outputs):
        raise ValueError("Deployment outputs contain ambiguous case-insensitive duplicate names.")
    if output_string(outputs, "AZURE_SUBSCRIPTION_ID").casefold() != subscription.casefold():
        raise ValueError("Bicep outputs do not match the selected subscription.")
    values = {key: output_string(outputs, key) for key in MAPPED_KEYS}
    project = urlsplit(values["FOUNDRY_PROJECT_ENDPOINT"])
    if project.scheme != "https" or not project.hostname or project.query or project.fragment or project.username:
        raise ValueError("Foundry project endpoint must be an HTTPS endpoint without credentials/query/fragment.")
    project_name = output_string(outputs, "AZURE_AI_PROJECT_NAME")
    if project.path.rstrip("/") != f"/api/projects/{project_name}":
        raise ValueError("Foundry endpoint does not match the deployed project name.")
    model = values["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    if not model or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise ValueError("Model deployment output is missing or invalid.")
    endpoint = values["APP_CONFIG_ENDPOINT"]
    if endpoint:
        appconfig = urlsplit(endpoint)
        if (
            appconfig.scheme != "https"
            or not appconfig.hostname
            or appconfig.username
            or appconfig.query
            or appconfig.fragment
        ):
            raise ValueError("App Configuration output is not a valid HTTPS endpoint.")
    return values


def render_env(text: str, values: dict[str, str]) -> str:
    """Preserve comments, local settings, and unknown fields; reject ambiguous mapped duplicates."""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    seen: set[str] = set()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if not match or match[1] not in values:
            continue
        key = match[1]
        if key in seen:
            raise ValueError(f"Duplicate deployment-mapped setting {key}; resolve it before updating.")
        seen.add(key)
        value = values[key].replace("\\", "\\\\").replace("'", "\\'")
        lines[index] = f"{key}='{value}'{newline}"
    missing = set(values) - seen
    if missing:
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines[-1] += newline
        lines.extend(f"{key}='{values[key]}'{newline}" for key in values if key in missing)
    return "".join(lines)


def write_env(target: Path, content: str, *, previous: bytes | None) -> None:
    """Refuse accidental replacement; use an atomic replace for explicitly requested updates."""
    encoded = content.encode("utf-8")
    if previous is None:
        with target.open("xb") as stream:
            stream.write(encoded)
        return
    if target.read_bytes() != previous:
        raise ValueError("The .env changed during generation; no update was applied.")
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".env-update-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
    try:
        if target.read_bytes() != previous:
            raise ValueError("The .env changed during generation; no update was applied.")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """Provide credential-free help and explicit deployment selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--deployment", help="Override automatic latest-successful deployment discovery.")
    parser.add_argument(
        "--update", action="store_true", help="Update only the three mapped fields in an existing .env."
    )
    add_output_arguments(parser)
    return parser


def execute(args: argparse.Namespace, run: ValidationRun) -> None:
    """Read deployment outputs through native az and create only the local configuration file."""
    target = ROOT / ".env"
    if target.is_symlink():
        raise ValueError("Refusing to modify a symlinked .env.")
    if target.exists() and not args.update:
        raise ValueError(".env already exists. Use --update to preserve local values and refresh only mapped fields.")
    previous = target.read_bytes() if target.exists() else None
    source = previous if previous is not None else (ROOT / ".env.example").read_bytes()
    account = object_value(az(["account", "show", "--subscription", args.subscription]))
    subscription = str(UUID(str(account["id"])))
    deployment = resolve_deployment(az, subscription, args.environment, args.deployment)
    run.result(
        "Deployment selection",
        "PASS",
        expected="Explicit or latest successful exact-environment deployment",
        observed={
            "environment": args.environment,
            "deployment": deployment,
            "selection": "explicit" if args.deployment else "latest successful",
        },
    )
    record = object_value(az(["deployment", "sub", "show", "--subscription", subscription, "--name", deployment]))
    values = mapped_values(record, subscription)
    properties = object_value(record.get("properties"))
    outputs = {key.upper(): value for key, value in object_value(properties.get("outputs")).items()}
    if output_string(outputs, "AZURE_ENV_NAME") != args.environment:
        raise ValueError("Selected deployment outputs do not match the requested environment.")
    run.result(
        "Deployment outputs",
        "PASS",
        expected="Successful foundation with verified configuration outputs",
        observed={"subscription": subscription, "deployment": deployment, "mapped": values},
    )
    content = render_env(source.decode("utf-8-sig"), values)
    write_env(target, content, previous=previous)
    run.result(
        "Local environment",
        "PASS",
        expected="Create from example or update mapped fields only",
        observed={
            "file": str(target),
            "action": "updated" if previous is not None else "created",
            "preserved": "Local defaults, identity, telemetry, and all unmapped settings",
        },
    )
    run.console.print("No hosted agents were deployed, no model call was made, and no roles were granted.")


def main(argv: Sequence[str] | None = None) -> int:
    """Render sanitized evidence without logging the existing .env or all deployment outputs."""
    args = build_parser().parse_args(argv)
    run = ValidationRun("generate-local-env", "local", args.output_dir, no_color=args.no_color, verbose=args.verbose)
    try:
        execute(args, run)
    except (CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        run.result(
            "Generate .env",
            "BLOCKED" if isinstance(error, ValueError) else "FAIL",
            expected="Explicit safe local configuration generation",
            observed=str(error),
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
