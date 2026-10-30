"""Prepare/verify a local, isolated Story6 azd context; never build or deploy.

Preview is credential-free. --prepare reads only the selected local azd env,
copies allowlisted source and non-secret bindings into ignored artifacts, and
leaves the root three-agent manifest/environment untouched.
Exit codes: 0 prepared/verified/previewed, 2 invalid inputs or local I/O error.
"""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "story6-coordinator-agent"
REQUIRED_KEYS = (
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_TENANT_ID",
    "AZURE_LOCATION",
    "AZURE_RESOURCE_GROUP",
    "AZURE_AI_PROJECT_ID",
    "AZURE_AI_PROJECT_ENDPOINT",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME",
    "AZURE_CONTAINER_REGISTRY_NAME",
    "AZURE_CONTAINER_REGISTRY_ENDPOINT",
    "AZURE_CONTAINER_REGISTRY_RESOURCE_ID",
)
ENVIRONMENT = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
IGNORED_PARTS = {".azure", ".foundry", ".git", ".venv", "__pycache__"}


class PackageRecord(BaseModel):
    """Local integrity receipt, not a signature or evidence of Azure deployment."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    service: str = SERVICE
    environment: str
    toolbox_version: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,9}$")
    files: dict[str, str]
    context_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    root_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def digest(content: bytes) -> str:
    """Bind the exact reviewed bytes without copying credentials into the receipt."""
    return sha256(content).hexdigest()


def validate_environment(environment: str) -> None:
    """Reject path traversal and ambiguous environment names."""
    if ENVIRONMENT.fullmatch(environment) is None:
        raise ValueError("Select a lowercase POC environment name of at most 40 characters.")


def deployment_context(path: Path, environment: str) -> dict[str, str]:
    """Select resolved non-secret inputs; never copy login caches, AGENT_* or secrets."""
    validate_environment(environment)
    if not path.is_file():
        raise ValueError("The selected local azd environment file is missing.")
    values = dotenv_values(path, interpolate=False)
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise ValueError("Missing selected deployment inputs: " + ", ".join(missing))
    selected = {key: str(values[key]) for key in REQUIRED_KEYS}
    for key, value in selected.items():
        if any(fragment in value for fragment in ("\n", "\r", "${", "$(", "`")):
            raise ValueError(f"{key} must be a resolved single-line deployment value.")
    if values.get("AZURE_ENV_NAME") not in (None, environment):
        raise ValueError("The selected azd environment name does not match its stored binding.")
    subscription = str(UUID(selected["AZURE_SUBSCRIPTION_ID"]))
    UUID(selected["AZURE_TENANT_ID"])
    group = selected["AZURE_RESOURCE_GROUP"]
    prefix = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/"
    project = selected["AZURE_AI_PROJECT_ID"]
    project_parts = project[len(prefix) :].split("/") if project.casefold().startswith(prefix.casefold()) else []
    if (
        len(project_parts) != 5
        or project_parts[0].casefold() != "microsoft.cognitiveservices"
        or project_parts[1].casefold() != "accounts"
        or project_parts[3].casefold() != "projects"
        or not project_parts[2]
        or not project_parts[4]
    ):
        raise ValueError("Project ARM binding is outside the selected subscription/resource group.")
    endpoint = urlsplit(selected["AZURE_AI_PROJECT_ENDPOINT"])
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or not endpoint.hostname.endswith(".ai.azure.com")
        or endpoint.path.rstrip("/") != f"/api/projects/{project_parts[4]}"
        or endpoint.query
        or endpoint.fragment
        or endpoint.username
        or endpoint.password
        or endpoint.port not in (None, 443)
    ):
        raise ValueError("Foundry project endpoint does not match the selected HTTPS project binding.")
    alias = values.get("FOUNDRY_PROJECT_ENDPOINT")
    if alias and alias.rstrip("/") != selected["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/"):
        raise ValueError("FOUNDRY_PROJECT_ENDPOINT conflicts with the selected azd project endpoint.")
    registry = selected["AZURE_CONTAINER_REGISTRY_NAME"]
    registry_id = selected["AZURE_CONTAINER_REGISTRY_RESOURCE_ID"]
    if (
        not re.fullmatch(r"[a-z0-9]{5,50}", registry)
        or selected["AZURE_CONTAINER_REGISTRY_ENDPOINT"] != f"{registry}.azurecr.io"
        or registry_id.casefold() != f"{prefix}Microsoft.ContainerRegistry/registries/{registry}".casefold()
    ):
        raise ValueError("ACR name, endpoint and ARM scope must identify the same selected registry.")
    selected["AZURE_ENV_NAME"] = environment
    selected["FOUNDRY_PROJECT_ENDPOINT"] = selected["AZURE_AI_PROJECT_ENDPOINT"]
    toolbox_version = values.get("STORY6_TOOLBOX_VERSION") or ""
    if toolbox_version and not re.fullmatch(r"[1-9][0-9]{0,9}", toolbox_version):
        raise ValueError("STORY6_TOOLBOX_VERSION must be blank/public or a positive immutable version.")
    selected["STORY6_TOOLBOX_VERSION"] = toolbox_version
    return selected


def context_digest(context: dict[str, str]) -> str:
    """Detect binding drift without putting raw Azure identifiers in review output."""
    return digest(json.dumps(context, sort_keys=True).encode())


def payload_sources(root: Path) -> dict[str, Path]:
    """Copy only project metadata, Python source and the existing HTML package assets."""
    template = root / "experiments" / "story6"
    sources = {name: template / name for name in ("azure.yaml", "Dockerfile", ".dockerignore")}
    sources.update({name: root / name for name in ("pyproject.toml", "uv.lock", "LICENSE")})
    for directory in (root / "src" / "backend" / "app", root / "scripts"):
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            if (
                path.is_file()
                and path.suffix in {".py", ".html"}
                and not (set(relative.parts) & IGNORED_PARTS)
                and not any(part.startswith(".") for part in relative.parts)
            ):
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Source symlinks are not allowed in the isolated deployment package.")
                sources[relative.as_posix()] = path
    for name in (
        "src/backend/app/agents/story6/main.py",
        "src/backend/app/story6_host.py",
        "src/backend/app/services/story6_approval.py",
    ):
        if name not in sources:
            raise ValueError("Dedicated Story6 coordinator source is missing.")
    if any(not path.is_file() or path.is_symlink() for path in sources.values()):
        raise ValueError("A required package template/metadata file is missing or is a symlink.")
    return dict(sorted(sources.items()))


def safe_package_path(root: Path, package: Path) -> Path:
    """Constrain generated/private deployment state to this story's ignored artifacts."""
    resolved = package.resolve()
    allowed = (root / "artifacts" / "story-6").resolve()
    if resolved == allowed or not resolved.is_relative_to(allowed):
        raise ValueError("Story6 packages must be below artifacts/story-6; never target the root project.")
    return resolved


def prepare(root: Path, environment: str, output_dir: Path, *, toolbox_version: str | None = None) -> Path:
    """Create one immutable local context, without azd calls or cloud mutations."""
    validate_environment(environment)
    env_path = root / ".azure" / environment / ".env"
    if not env_path.resolve().is_relative_to((root / ".azure").resolve()):
        raise ValueError("Selected environment escapes the root azd configuration.")
    context = deployment_context(env_path, environment)
    if toolbox_version is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,9}", toolbox_version):
            raise ValueError("Select a positive immutable toolbox version.")
        context["STORY6_TOOLBOX_VERSION"] = toolbox_version
    sources = payload_sources(root)
    package = safe_package_path(root, output_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
    package.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, str] = {}
    for relative, source in sources.items():
        content = source.read_bytes()
        destination = package / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        hashes[relative] = digest(content)
    local_config = package / ".azure"
    local_env = local_config / environment / ".env"
    local_env.parent.mkdir(parents=True)
    (local_config / "config.json").write_text(
        json.dumps({"version": 1, "defaultEnvironment": environment}), encoding="utf-8"
    )
    local_env.write_text(
        "".join(f"{key}={json.dumps(value)}\n" for key, value in sorted(context.items())),
        encoding="utf-8",
    )
    record = PackageRecord(
        environment=environment,
        toolbox_version=toolbox_version,
        files=hashes,
        context_sha256=context_digest(context),
        root_manifest_sha256=digest((root / "azure.yaml").read_bytes()),
    )
    (package / "deployment-review.json").write_text(record.model_dump_json(indent=2), encoding="utf-8")
    verify(root, package)
    return package


def verify(root: Path, package: Path) -> PackageRecord:
    """Reject stale/tampered payload, context or root-manifest changes before review."""
    package = safe_package_path(root, package)
    record = PackageRecord.model_validate_json((package / "deployment-review.json").read_bytes())
    if record.schema_version != 1 or record.service != SERVICE:
        raise ValueError("Unsupported Story6 deployment receipt.")
    sources = payload_sources(root)
    if set(record.files) != set(sources):
        raise ValueError("Source inventory changed; prepare a new package instead of rebuilding stale inputs.")
    for relative, source in sources.items():
        staged = package / relative
        if (
            staged.is_symlink()
            or not staged.resolve().is_relative_to(package)
            or digest(staged.read_bytes()) != record.files[relative]
            or digest(source.read_bytes()) != record.files[relative]
        ):
            raise ValueError("Reviewed package/source bytes changed; prepare a new package.")
    for directory in (package / "src", package / "scripts"):
        for path in directory.rglob("*"):
            relative_path = path.relative_to(package)
            if path.is_file() and not (set(relative_path.parts) & IGNORED_PARTS):
                if relative_path.as_posix() not in record.files:
                    raise ValueError("Unreviewed source files appeared in the deployment context.")
    context = deployment_context(package / ".azure" / record.environment / ".env", record.environment)
    original = deployment_context(root / ".azure" / record.environment / ".env", record.environment)
    if record.toolbox_version is not None:
        original["STORY6_TOOLBOX_VERSION"] = record.toolbox_version
    if context_digest(context) != record.context_sha256 or context_digest(original) != record.context_sha256:
        raise ValueError("Deployment environment binding changed; review a new package.")
    if digest((root / "azure.yaml").read_bytes()) != record.root_manifest_sha256:
        raise ValueError("Root three-agent manifest changed after package preparation.")
    return record


def build_parser() -> argparse.ArgumentParser:
    """Preview by default; local preparation/verification never implies deploy approval."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--toolbox-version", help="Isolated package override only; root env is not modified.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--verify", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "story-6" / "deployment-context")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print safe review data only, never raw environment or credential values."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.toolbox_version and not args.prepare:
        parser.error("--toolbox-version is only valid with --prepare.")
    try:
        validate_environment(args.environment)
        if args.verify:
            record = verify(ROOT, args.verify)
            if record.environment != args.environment:
                raise ValueError("Selected environment does not match the reviewed package.")
            print(json.dumps({"verified": True, "service": record.service, "files": len(record.files)}))
        elif args.prepare:
            package = prepare(ROOT, args.environment, args.output_dir, toolbox_version=args.toolbox_version or None)
            print(json.dumps({"prepared": str(package), "service": SERVICE, "deployed": False}, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "mode": "preview",
                        "service": SERVICE,
                        "environment": args.environment,
                        "root_agents_changed": False,
                        "source_env_values_read": False,
                        "cloud_requests": 0,
                        "build": False,
                        "deployment": False,
                        "grants": False,
                        "prepare": "--prepare creates a separate local context under ignored artifacts only",
                    },
                    indent=2,
                )
            )
    except (OSError, ValueError) as error:
        print(f"Story6 package rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
