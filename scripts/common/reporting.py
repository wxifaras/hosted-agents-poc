"""Shared Rich reporting with sanitized, incrementally persisted run evidence."""

import argparse
import json
import platform
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.core.app_config_source import DEFAULT_ENV_FILE
from app.core.redaction import sanitize, sanitize_text
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

Status = Literal["PASS", "FAIL", "BLOCKED", "SKIP"]


def add_output_arguments(parser: argparse.ArgumentParser) -> None:
    """Add consistent noninteractive presentation options."""
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ENV_FILE.parent / "artifacts")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="Show complete sanitized step details.")


class ValidationRun:
    """Write each observed result immediately and render its matching Rich record."""

    def __init__(
        self, scenario: str, mode: str, output_dir: Path, *, no_color: bool = False, verbose: bool = False
    ) -> None:
        self.run_id = uuid4().hex
        self.path = output_dir / self.run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.console = Console(no_color=no_color, markup=False, highlight=False)
        self.verbose = verbose
        self.statuses: list[Status] = []
        self.request_count = 0
        self.table = Table(title="Expected versus observed")
        for column in ("Step", "Status", "Expected", "Observed"):
            self.table.add_column(column)
        manifest = {
            "run_id": self.run_id,
            "scenario": scenario,
            "mode": mode,
            "started_at": datetime.now(UTC).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "project_version": version("hosted-agents-poc"),
            "maf_version": version("agent-framework-core"),
            "agentserver_version": version("azure-ai-agentserver-responses"),
        }
        (self.path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.console.rule(f"{scenario} | {mode} | {self.run_id}")
        self.console.print_json(data=manifest)

    def start_request(self, agent: str, mode: str, message: str) -> int:
        """Open a numbered, sanitized visual section without changing the evidence contract."""
        self.request_count += 1
        number = self.request_count
        self.console.print()
        self.console.rule(Text(f"START {number:02d} | {agent} | {mode}", style="bold cyan"), style="cyan")
        target = f"hosted {agent}" if mode == "hosted" else agent
        self.console.print(
            Panel(
                Text(f"Sending to {target}: {sanitize_text(message)}"),
                title="Request message",
                border_style="cyan",
                padding=(0, 1),
            )
        )
        return number

    def end_request(self, number: int, agent: str, status: Status, duration_ms: float) -> None:
        """Close the visual section with the actual outcome and observed elapsed time."""
        style = {"PASS": "green", "FAIL": "red", "BLOCKED": "yellow", "SKIP": "yellow"}[status]
        self.console.rule(
            Text(f"END {number:02d} | {agent} | {status} | {duration_ms / 1000:.2f}s", style=f"bold {style}"),
            style=style,
        )
        self.console.print()

    def result(
        self, step: str, status: Status, *, expected: str, observed: object, summary_table: Table | None = None
    ) -> None:
        """Persist and display one real observation, never a success placeholder."""
        event = sanitize(
            {
                "run_id": self.run_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "step": step,
                "status": status,
                "expected": expected,
                "observed": observed,
            }
        )
        for name in ("events.jsonl", "results.jsonl"):
            with (self.path / name).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
        self.statuses.append(status)
        self.console.print(f"{status}: {step}")
        if summary_table is None:
            text = json.dumps(sanitize(observed), ensure_ascii=True)
            self.table.add_row(step, status, expected, text if self.verbose else text[:500])
            self.console.print_json(data=event)
        else:
            self.console.print(summary_table)
            if self.verbose:
                self.console.print_json(data=event)

    def finish(self) -> int:
        """Return 0 for all-pass, 1 for failures/skips, or 2 for blocked prerequisites."""
        if not self.statuses:
            raise ValueError("A validation run cannot pass without observations.")
        if self.table.row_count:
            self.console.print(self.table)
        if self.request_count:
            counts = " | ".join(
                f"{status}: {self.statuses.count(status)}"
                for status in ("PASS", "FAIL", "BLOCKED")
                if status in self.statuses
            )
            self.console.rule(Text(f"RUN FINISHED | {counts}", style="bold"))
        self.console.print(f"Evidence: {self.path}")
        if "FAIL" in self.statuses or "SKIP" in self.statuses:
            return 1
        return 2 if "BLOCKED" in self.statuses else 0
