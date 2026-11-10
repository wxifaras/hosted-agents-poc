"""Publish a standalone, sanitized Story 13 snapshot from retained evidence.

No Azure calls, runner execution, Git commits, or network publication.
Exit codes: 0 snapshot generated, 2 invalid evidence/source or I/O failure.
Existing output directories are never overwritten.
"""

import argparse
import base64
import json
import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from report import LABELS, Run, load_run, render, summary_markdown, validate_controls

REPOSITORY = "https://github.com/wxifaras/hosted-agents-poc"
REVIEWED_ERRORS = frozenset(
    {
        "Invocation/diagnostic failure: ValidationError; no POST retry attempted.",
        "Expected cold; native classification was warm.",
    }
)


def timestamp(value: str) -> str:
    """Normalize only valid, timezone-aware evidence timestamps."""
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid evidence timestamp; publication blocked.") from error
    if observed.tzinfo is None:
        raise ValueError("Evidence timestamps must include a timezone.")
    return observed.astimezone(UTC).isoformat()


def validate_public_values(runs: Sequence[Run]) -> None:
    """Fail closed on unreviewed failure text or non-version control values."""
    for run in runs:
        if not re.fullmatch(r"[1-9][0-9]*", run.info.agent_version):
            raise ValueError("Only numeric immutable agent versions can be published.")
        for value in (run.info.core_version, run.info.responses_version):
            if not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?", value):
                raise ValueError("Unsupported SDK version format in publication.")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", run.info.cpu):
            raise ValueError("Unsupported CPU format in publication.")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:Mi|Gi)", run.info.memory):
            raise ValueError("Unsupported memory format in publication.")
        timestamp(run.info.timestamp)
        for sample in run.samples:
            timestamp(sample.timestamp)
            if any(error not in REVIEWED_ERRORS for error in sample.errors):
                raise ValueError("Unreviewed failure text requires sanitization review before publication.")


def public_evidence(runs: Sequence[Run], diagnostic_runs: Sequence[Run]) -> dict[str, object]:
    """Export a reviewed allowlist; replace private bindings with report-local aliases."""
    sessions: dict[str, str] = {}
    images: dict[str, str] = {}
    published: list[dict[str, object]] = []
    for run, role in [(run, "comparison") for run in runs] + [(run, "diagnostic") for run in diagnostic_runs]:
        image = images.setdefault(run.info.image, f"image-{len(images) + 1}")
        samples: list[dict[str, object]] = []
        for sample in run.samples:
            session = None
            if sample.session_hash != "unavailable":
                session = sessions.setdefault(sample.session_hash, f"session-{len(sessions) + 1:03d}")
            samples.append(
                {
                    "trial": sample.trial,
                    "phase": sample.phase,
                    "timestamp": timestamp(sample.timestamp),
                    "session_alias": session,
                    "observed_start": sample.start_type,
                    "passed": sample.passed,
                    "errors": list(sample.errors),
                    "timings_ms": dict(sample.timings),
                    "native_latency_headers": dict(sample.headers),
                }
            )
        published.append(
            {
                "role": role,
                "condition": run.info.condition,
                "agent_version": run.info.agent_version,
                "recording_timestamp": timestamp(run.info.timestamp),
                "image_alias": image,
                "cpu": run.info.cpu,
                "memory": run.info.memory,
                "snapshots": run.info.snapshots,
                "core_version": run.info.core_version,
                "responses_version": run.info.responses_version,
                "planned_cold_sessions": run.info.samples,
                "planned_resume_checks": run.info.resume_samples,
                "idle_wait_seconds": run.info.idle_seconds,
                "samples": samples,
            }
        )
    return {
        "schema": "story13-published-evidence-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_binding": "Retained hosted records; implementation is a working-tree snapshot, not a commit binding.",
        "privacy": (
            "Project/session hashes, response IDs, run IDs, registry URLs, image digests, and local paths omitted."
        ),
        "controls": (
            "Original project, agent, SDK, compute, configuration, and image comparisons validated before redaction."
        ),
        "recorded_distinct_session_bindings": len(sessions),
        "runs": published,
    }


def heading_slug(value: str) -> str:
    """Make deterministic report-local anchors for the source document."""
    return "issue-" + re.sub(r"-+", "-", re.sub(r"[^a-z0-9 -]", "", value.lower()).replace(" ", "-")).strip("-")


def repository_link(href: str, source: Path, repository_root: Path, branch: str) -> str:
    """Resolve source-relative links without publishing local filesystem locations."""
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Only HTTPS external source links are allowed.")
        return href
    if not parsed.path and parsed.fragment:
        return "#" + heading_slug(parsed.fragment)
    target = (source.parent / unquote(parsed.path)).resolve()
    if not target.is_relative_to(repository_root.resolve()) or not target.exists():
        raise ValueError("Source link is missing or leaves the repository.")
    relative = target.relative_to(repository_root.resolve()).as_posix()
    kind = "tree" if target.is_dir() else "blob"
    result = f"{REPOSITORY}/{kind}/{quote(branch, safe='/')}/{quote(relative, safe='/')}"
    return result + ("#" + quote(parsed.fragment, safe="-") if parsed.fragment else "")


def rewrite_links(tokens: Sequence[Token], source: Path, repository_root: Path, branch: str) -> None:
    """Rewrite Markdown links recursively; code blocks remain literal examples."""
    for token in tokens:
        if token.type == "link_open":
            href = token.attrGet("href")
            if href is not None:
                if not isinstance(href, str):
                    raise ValueError("Source link targets must be strings.")
                token.attrSet("href", repository_link(href, source, repository_root, branch))
        if token.children:
            rewrite_links(token.children, source, repository_root, branch)


def issue_document(source: Path, repository_root: Path, branch: str) -> str:
    """Render the complete issue/runbook with tables, safe code, a contents list, and anchors."""
    if not source.resolve().is_relative_to(repository_root.resolve()):
        raise ValueError("Issue source must be inside the repository.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch):
        raise ValueError("Invalid source branch.")
    text = source.read_text(encoding="utf-8")
    if not re.match(r"# Story 13\b", text):
        raise ValueError("The source document must describe Story 13.")
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    tokens = markdown.parse(text)
    headings: list[tuple[str, str]] = []
    counts: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            label = tokens[index + 1].content
            base = heading_slug(label)
            count = counts.get(base, 0)
            counts[base] = count + 1
            anchor = base + (f"-{count}" if count else "")
            token.attrSet("id", anchor)
            if token.tag in ("h1", "h2"):
                headings.append((label, anchor))
        if token.type in ("heading_open", "heading_close"):
            token.tag = f"h{min(int(token.tag[1:]) + 1, 6)}"
    rewrite_links(tokens, source, repository_root, branch)
    body = markdown.renderer.render(tokens, markdown.options, {})
    body = body.replace("<table>", '<div class="table-scroll"><table>').replace("</table>", "</table></div>")
    contents = "".join(f'<li><a href="#{anchor}">{escape(label)}</a></li>' for label, anchor in headings)
    return (
        '<section class="paper" id="issue-details"><h2>Full issue 13 scope and runbook</h2>'
        "<p>The following is the retained project document, including requirements, acceptance criteria, "
        "SDK findings, execution instructions, risks, approval boundaries, and source references. "
        "Planning and local checks are not additional hosted measurements.</p>"
        f'<nav aria-label="Issue document contents"><ul>{contents}</ul></nav>{body}</section>'
    )


def task_walkthroughs(runs: Sequence[Run]) -> str:
    """Describe the reviewed measurement plan, not an invented request execution trace."""
    cards: list[str] = []
    for run in runs:
        counts = {
            phase: sum(sample.passed and sample.start_type == phase and sample.phase == phase for sample in run.samples)
            for phase in ("cold", "warm", "resume")
        }
        cards.append(
            f"<h3>{LABELS[run.info.condition]} - version {escape(run.info.agent_version)}</h3>"
            "<ol><li>Verify active version, compute, SDKs, snapshot flag, and fixed endpoint routing.</li>"
            "<li>POST without a session ID: create a real cold session; "
            "validate its returned version and clean state.</li>"
            "<li>Reuse that session for a warm request; validate its own retained synthetic marker.</li>"
            f"<li>For the configured resume sample, wait {run.info.idle_seconds:g} seconds without invoking. "
            "Require a native resume classification, not just elapsed quiet time.</li>"
            "<li>Repeat the approved cold/warm budget; persist every attempt without POST retries.</li></ol>"
            f"<p>Recorded passing observations: cold {counts['cold']}/{run.info.samples}, "
            f"warm {counts['warm']}/{run.info.samples}, resume {counts['resume']}/{run.info.resume_samples}. "
            f"Snapshots: {'enabled' if run.info.snapshots else 'disabled'}.</p>"
        )
    return (
        '<section class="paper" id="task-validation"><h2>Task walkthroughs and evidence boundaries</h2>'
        "<p>These are reviewed task steps linked to recorded outcomes, not reconstructed call-stack traces.</p>"
        + "".join(cards)
        + "<h3>Acceptance coverage</h3><ul>"
        "<li>AC-1301: local readiness/protocol behavior is supported by the referenced local tests, "
        "not inferred from timings.</li>"
        "<li>AC-1302/AC-1303: original controls are validated before publication; "
        "native classifications and counts are retained.</li>"
        "<li>AC-1304: passing records include synthetic state/instance checks; "
        "they do not prove cross-user authorization.</li>"
        "<li>AC-1305: graphs, detailed measurements, and disclosures are generated from retained records.</li>"
        "<li>AC-1306: approval and preservation of other stories are operator/local-test evidence in the runbook, "
        "not conclusions established by latency alone.</li></ul></section>"
    )


def observation_ledger(runs: Sequence[Run], diagnostic_runs: Sequence[Run]) -> str:
    """Show every timing and failure without exposing private response or session identifiers."""
    rows: list[str] = []
    for run, role in [(run, "comparison") for run in runs] + [(run, "diagnostic") for run in diagnostic_runs]:
        for sample in run.samples:
            timings = escape(json.dumps(sample.timings, indent=2, allow_nan=False))
            headers = escape(json.dumps(sample.headers, indent=2))
            rows.append(
                f"<tr><td>{role}</td><td>{run.info.condition} / v{escape(run.info.agent_version)}</td>"
                f"<td>{sample.trial} / {sample.phase}</td><td>{sample.start_type or 'unavailable'}</td>"
                f"<td>{'PASS' if sample.passed else 'FAIL'}</td><td>{escape('; '.join(sample.errors))}"
                f"<details><summary>All timings and native diagnostics</summary><pre>{timings}</pre>"
                f"<pre>{headers}</pre></details></td></tr>"
            )
    return (
        '<section class="paper" id="recorded-observations"><h2>Every recorded observation and timing</h2>'
        "<p>Milliseconds are retained exactly. Missing values are null, not zero. Diagnostic failures are not "
        "included in valid performance distributions. "
        "Private bindings are available only in the retained local records.</p>"
        '<div class="table-scroll"><table><thead><tr><th>Evidence role</th><th>Condition/version</th>'
        "<th>Trial/requested start</th><th>Native start</th><th>Result</th><th>Details</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def data_uri(content: str, mime: str) -> str:
    """Embed reviewed assets so HTML Preview needs no relative asset requests."""
    return f"data:{mime};base64," + base64.b64encode(content.encode("utf-8")).decode("ascii")


def audit_publication(files: dict[str, str], runs: Sequence[Run]) -> None:
    """Reject leaked raw bindings or machine locations before writing any snapshot."""
    private: set[str] = set()
    for run in runs:
        private.update(
            (
                run.info.project_hash,
                run.info.configuration_hash,
                run.info.run_id,
                run.info.image,
                run.info.image.split("/", 1)[0],
            )
        )
        for sample in run.samples:
            private.add(sample.session_hash)
            if sample.response_id:
                private.add(sample.response_id)
    for content in files.values():
        if any(value in content for value in private if len(value) >= 16):
            raise ValueError("Raw infrastructure or execution binding detected; publication blocked.")
        if re.search(r"\b[A-Z]:[\\/]|\\\\[A-Za-z0-9]", content):
            raise ValueError("Absolute machine path detected; publication blocked.")


def build_snapshot(
    runs: Sequence[Run], diagnostic_runs: Sequence[Run], source: Path, repository_root: Path, branch: str = "main"
) -> dict[str, str]:
    """Reuse the existing report renderer, add complete documentation, and sanitize all exports."""
    validate_controls(runs)
    if {run.info.condition for run in runs} != {"baseline", "snapshot", "optimized"}:
        raise ValueError("A published Story 13 snapshot must include all three comparison conditions.")
    validate_public_values([*runs, *diagnostic_runs])
    supplement = task_walkthroughs(runs) + observation_ledger(runs, diagnostic_runs)
    supplement += issue_document(source, repository_root, branch)
    html, charts = render(runs, diagnostic_runs, issue_html=supplement)
    aliases: dict[str, str] = {}
    for run in [*runs, *diagnostic_runs]:
        alias = aliases.setdefault(run.info.image, f"image-{len(aliases) + 1}")
        html = html.replace(escape(run.info.image), alias)
    exported = json.dumps(public_evidence(runs, diagnostic_runs), indent=2, allow_nan=False)
    summary = summary_markdown(runs, diagnostic_runs)
    assets = {
        **{name: (svg, "image/svg+xml", name) for name, svg in charts.items()},
        "evidence.json": (exported, "application/json", "sanitized-evidence.json"),
        "summary.md": (summary, "text/markdown", "summary.md"),
    }
    for name, (content, mime, filename) in assets.items():
        uri = data_uri(content, mime)
        html = html.replace(f'src="{name}"', f'src="{uri}"')
        html = re.sub(
            r'href="' + re.escape(name) + r'"(?:\s+download(?:="[^"]*")?)?',
            f'href="{uri}" download="{filename}"',
            html,
        )
    files = {"index.html": html, "sanitized-evidence.json": exported, "summary.md": summary, **charts}
    audit_publication(files, [*runs, *diagnostic_runs])
    return files


def build_parser() -> argparse.ArgumentParser:
    """Require explicit retained recordings and a new repository snapshot directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--diagnostic-input", type=Path, nargs="*", default=[])
    parser.add_argument("--issue-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--branch", default="main", help="Branch for repository source links; does not push anything.")
    return parser


def main(argv: Sequence[str] | None = None, *, repository_root: Path | None = None) -> int:
    """Generate an offline publication without executing runners or calling Azure."""
    args = build_parser().parse_args(argv)
    try:
        runs = [load_run(path) for path in args.input]
        diagnostics = [load_run(path) for path in args.diagnostic_input]
        root = repository_root if repository_root is not None else Path(__file__).resolve().parents[2]
        files = build_snapshot(runs, diagnostics, args.issue_source, root, args.branch)
        args.output.mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            (args.output / name).write_text(content, encoding="utf-8")
    except (OSError, ValueError) as error:
        logging.getLogger(__name__).error("Cannot publish Story 13 report: %s", error)
        return 2
    print(args.output / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
