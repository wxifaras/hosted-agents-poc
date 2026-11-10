"""No-model Responses host with synthetic process/file probes for Story 13."""

import argparse
import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from azure.ai.agentserver.core import AgentSessionContext
from azure.ai.agentserver.responses import ResponseContext, ResponsesAgentServerHost, TextResponse

LOGGER = logging.getLogger(__name__)


@dataclass
class Probe:
    """Keep only synthetic state; restored baselines must start without a marker."""

    marker_file: Path
    marker: str | None = None
    turns: int = 0
    restores: int = 0
    instance: str = field(default_factory=lambda: uuid4().hex)

    def reply(self, marker: str) -> dict[str, object]:
        """Observe previous state, then write one bounded synthetic marker."""
        if not re.fullmatch(r"[a-f0-9]{32}", marker):
            raise ValueError("The input must be one 32-character lowercase hexadecimal synthetic marker.")
        previous_file = self.marker_file.read_text(encoding="ascii") if self.marker_file.exists() else None
        previous = self.marker
        self.marker_file.write_text(marker, encoding="ascii")
        self.marker = marker
        self.turns += 1
        return {
            "schema": "story13-probe-v1",
            "previous_memory": previous,
            "previous_file": previous_file,
            "turn": self.turns,
            "restores": self.restores,
            "instance": self.instance,
            "request": uuid4().hex,
            "core_version": version("azure-ai-agentserver-core"),
            "responses_version": version("azure-ai-agentserver-responses"),
        }

    async def before_snapshot(self) -> None:
        """Reject a contaminated capture rather than sharing session-specific state."""
        if self.marker is not None or self.marker_file.exists():
            raise RuntimeError("Snapshot preparation contains synthetic session state; capture must not continue.")
        LOGGER.info("story13 before_snapshot: clean startup baseline")

    async def after_restore(self, _context: AgentSessionContext) -> None:
        """Regenerate instance identity without manually erasing state under test."""
        self.instance = uuid4().hex
        self.restores += 1
        LOGGER.info("story13 after_restore: initialization complete")


def build_host(probe: Probe) -> ResponsesAgentServerHost:
    """Use public SDK hooks and leave background recovery/steering disabled."""
    host = ResponsesAgentServerHost()
    host.before_snapshot_handler(probe.before_snapshot)
    host.after_restore_handler(probe.after_restore)

    @host.response_handler
    async def respond(
        request: Mapping[str, object], context: ResponseContext, cancellation: asyncio.Event
    ) -> TextResponse:
        if cancellation.is_set():
            raise asyncio.CancelledError
        marker = await context.get_input_text() or ""
        return TextResponse(context, request, text=json.dumps(probe.reply(marker)))

    return host


def build_parser() -> argparse.ArgumentParser:
    """Default local testing to loopback; containers explicitly select all interfaces."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8093")))
    return parser


def main() -> int:
    """Start ordinary initialization without model calls or artificial startup work."""
    args = build_parser().parse_args()
    if not 1 <= args.port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = perf_counter()
    host = build_host(Probe(Path.home() / ".story13-synthetic-marker"))
    LOGGER.info("story13 host constructed in %.3f ms; SDK startup/readiness follows", (perf_counter() - started) * 1000)
    host.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
