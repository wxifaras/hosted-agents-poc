"""Validate local imports or configured model prerequisites; never invokes Azure models.

Exit codes: 0 all checks passed, 1 failed assertion, 2 blocked configuration.
"""

import argparse
import importlib
import sys
from collections.abc import Sequence

from app.core.settings import get_settings
from azure.core.exceptions import AzureError
from pydantic import ValidationError

from scripts.common.reporting import ValidationRun, add_output_arguments

MODULES = (
    "agent_framework",
    "agent_framework.foundry",
    "agent_framework_foundry_hosting",
    "azure.ai.agentserver.core.storage",
    "app.agents.supervisor.main",
    "app.agents.research.main",
    "app.agents.data_analysis.main",
)


def build_parser() -> argparse.ArgumentParser:
    """Define credential-free help and local/configured preflight selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "configured"), default="local")
    add_output_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Check the actual interpreter/imports and optionally validate model configuration."""
    args = build_parser().parse_args(argv)
    run = ValidationRun("environment", args.mode, args.output_dir, no_color=args.no_color, verbose=args.verbose)
    run.result(
        "Python minor",
        "PASS" if sys.version_info[:2] == (3, 14) else "FAIL",
        expected="Python 3.14",
        observed=sys.version,
    )
    for module in MODULES:
        try:
            importlib.import_module(module)
        except ImportError as error:
            run.result(module, "BLOCKED", expected="Import succeeds", observed=str(error))
        else:
            run.result(module, "PASS", expected="Import succeeds", observed="Imported without starting a host")
    if args.mode == "configured":
        try:
            settings = get_settings()
            settings.require_model()
        except ValidationError as error:
            run.result(
                "Configuration",
                "BLOCKED",
                expected="Valid model settings",
                observed=error.errors(include_input=False, include_url=False),
            )
        except (AzureError, ValueError, RuntimeError, ImportError) as error:
            run.result("Configuration", "BLOCKED", expected="Valid model settings", observed=str(error))
        else:
            run.result(
                "Configuration",
                "PASS",
                expected="Valid model settings",
                observed={
                    "endpoint": str(settings.foundry_project_endpoint),
                    "deployment": settings.azure_ai_model_deployment_name,
                    "note": "Configuration only; no Azure authentication or model call tested.",
                },
            )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
