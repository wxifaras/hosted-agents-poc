"""Run independent local host smoke requests; not a hosted delegation/recovery test.

Exit codes: 0 all requests completed, 1 a response failed, 2 prerequisites blocked.
"""

import argparse
from collections.abc import Sequence

from app.core.settings import AGENT_NAMES

from scripts.common.reporting import ValidationRun, add_output_arguments
from scripts.run_scenario.trigger_agent import record_smoke


def build_parser() -> argparse.ArgumentParser:
    """Expose only the actually implemented smoke scenario and local mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("smoke",), default="smoke")
    parser.add_argument("--mode", choices=("local",), default="local")
    parser.add_argument("--timeout", type=float, default=60)
    add_output_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Collect independent responses from all three running hosts."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "independent-host-smoke", args.mode, args.output_dir, no_color=args.no_color, verbose=args.verbose
    )
    for agent in AGENT_NAMES:
        record_smoke(run, agent, "Reply with exactly: HOSTED-AGENTS-POC-OK", timeout=args.timeout)
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
