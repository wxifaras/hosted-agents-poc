"""Shared Rich reporting with sanitized, incrementally persisted run evidence."""

import argparse
import json
import platform
import re
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.core.app_config_source import DEFAULT_ENV_FILE
from app.core.redaction import sanitize, sanitize_text
from rich.console import Console
from rich.markdown import Markdown
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
        self,
        scenario: str,
        mode: str,
        output_dir: Path,
        *,
        no_color: bool = False,
        verbose: bool = False,
        directory_label: str | None = None,
    ) -> None:
        started_at = datetime.now(UTC)
        self.run_id = uuid4().hex
        directory_name = (
            f"{started_at:%Y-%m-%d_%H-%M-%SZ}-{directory_label}-{self.run_id[:8]}" if directory_label else self.run_id
        )
        self.path = output_dir / directory_name
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
            "started_at": started_at.isoformat(),
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


def markdown_cell(value: object) -> str:
    """Sanitize one table cell and keep it on a single Markdown table row."""
    safe = sanitize(value)
    text = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=True)
    return text.replace("|", "\\|").replace("\r", "").replace("\n", "<br>") or "-"


SUMMARY_SLOT = "<!-- report-summary -->"


class MarkdownReport:
    """Incrementally persisted, sanitized Markdown evidence that renders in VS Code Preview."""

    def __init__(self, path: Path, title: str) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The reader-first summary is only known at the end; reserve its place directly under the title.
        self.path.write_text(f"# {sanitize_text(title)}\n\n{SUMMARY_SLOT}\n", encoding="utf-8")

    def summary(self, markdown: str) -> None:
        """Place the summary under the title; append it if the slot is gone."""
        text = self.path.read_text(encoding="utf-8")
        block = sanitize_text(markdown).rstrip() + "\n"
        text = text.replace(SUMMARY_SLOT, block, 1) if SUMMARY_SLOT in text else f"{text}\n{block}"
        self.path.write_text(text, encoding="utf-8")

    def _append(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(text)

    def heading(self, text: str, level: int = 2) -> None:
        self._append(f"\n{'#' * level} {sanitize_text(text)}\n")

    def paragraph(self, text: str) -> None:
        self._append(f"\n{sanitize_text(text)}\n")

    def bullets(self, items: list[str]) -> None:
        self._append("\n" + "".join(f"- {sanitize_text(item)}\n" for item in items))

    def table(self, headers: list[str], rows: list[list[object]]) -> None:
        lines = ["| " + " | ".join(markdown_cell(header) for header in headers) + " |"]
        lines.append("|" + "---|" * len(headers))
        lines.extend("| " + " | ".join(markdown_cell(cell) for cell in row) + " |" for row in rows)
        self._append("\n" + "\n".join(lines) + "\n")

    def code(self, text: str, language: str = "") -> None:
        safe = sanitize_text(text).rstrip()
        # A fence longer than any backtick run inside keeps nested fenced blocks intact.
        longest = max((len(run) for run in re.findall(r"`+", safe)), default=0)
        fence = "`" * max(3, longest + 1)
        self._append(f"\n{fence}{language}\n{safe}\n{fence}\n")

    def render(self, console: Console) -> None:
        """Show the same persisted Markdown in the terminal."""
        text = self.path.read_text(encoding="utf-8")
        console.print()
        console.rule("Markdown report")
        # Render off-screen first: a failed write would leave the glyphs in Rich's buffer and break any fallback.
        with console.capture() as capture:
            console.print(Markdown(text))
        try:
            capture.get().encode(console.encoding)
        except UnicodeEncodeError:
            # Legacy code-page consoles cannot draw Rich's Markdown glyphs; the source is still readable.
            console.print(text.encode("ascii", "replace").decode("ascii"), markup=False, highlight=False)
        else:
            console.print(Markdown(text))
        console.print(f"Markdown report: {self.path}  (open it and press Ctrl+Shift+V for VS Code Preview)")


def update_index(index: Path, entry: str, title: str = "Story 1 evidence index") -> None:
    """Append one linked report entry; earlier runs are never overwritten."""
    index.parent.mkdir(parents=True, exist_ok=True)
    if not index.exists():
        index.write_text(f"# {title}\n\n", encoding="utf-8")
    with index.open("a", encoding="utf-8") as stream:
        stream.write(f"- {sanitize_text(entry)}\n")
