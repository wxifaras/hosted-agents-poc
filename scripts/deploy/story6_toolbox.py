"""Preview/create only the isolated approval-required Story6 Learn toolbox.

Versions are reused only when the exact expected configuration matches.
No connection, old toolbox, promotion, role or agent deployment is modified.
"""

import argparse
import json
import sys
from collections.abc import Sequence

from app.services.story6_approval import LEARN_URL, TOOL_NAME, TOOLBOX_NAME
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPToolboxTool
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import AzureCliCredential

from scripts.deploy.story6_package import ROOT, deployment_context


def source_tool() -> MCPToolboxTool:
    """Keep the upstream public, read-only and free of stored credentials."""
    return MCPToolboxTool(
        server_label="microsoft_learn",
        server_url=LEARN_URL,
        allowed_tools=[TOOL_NAME],
        require_approval="always",
    )


def verify_version(data: dict[str, object]) -> str:
    """Never adopt an unrelated or differently configured toolbox."""
    if data.get("name") != TOOLBOX_NAME or data.get("tools") != [source_tool().as_dict()]:
        raise ValueError("Existing Story6 toolbox differs from the fixed approval-required search contract.")
    version = data.get("version")
    if not isinstance(version, str) or not version.isdecimal() or int(version) < 1:
        raise ValueError("Toolbox did not expose a positive immutable version.")
    return version


def ensure(project: AIProjectClient, *, apply: bool) -> tuple[str | None, bool]:
    """Reuse exact versions or create once only after the explicit apply action."""
    try:
        versions = [item.as_dict() for item in project.toolboxes.list_versions(TOOLBOX_NAME)]
    except ResourceNotFoundError:
        versions = []
    if versions:
        checked = [verify_version(item) for item in versions]
        return max(checked, key=int), False
    if not apply:
        return None, False
    result = project.toolboxes.create_version(
        TOOLBOX_NAME,
        tools=[source_tool()],
        description="Story6 isolated approval-required public Learn search; runtime owns approval enforcement",
        metadata={"story": "6", "purpose": "approval-boundary", "upstream": "public-learn"},
    ).as_dict()
    return verify_version(result), True


def main(argv: Sequence[str] | None = None) -> int:
    """Preview is read-only; apply cannot modify the old Story4 toolbox."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
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
            version, created = ensure(project, apply=args.apply)
        print(
            json.dumps(
                {
                    "toolbox": TOOLBOX_NAME,
                    "version": version,
                    "created": created,
                    "require_approval": "always",
                    "upstream": "public Learn",
                    "old_resources_changed": False,
                    "runtime_enforcement_tested": False,
                },
                indent=2,
            )
        )
    except (AzureError, OSError, ValueError) as error:
        print(f"Story6 toolbox preparation stopped ({type(error).__name__}); inspect before retrying.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
