"""Verify publication completeness, standalone assets, and fail-closed redaction."""

import base64
import json
import re
from pathlib import Path

import pytest

from measure import PREFIX, RunInfo, Sample, diagnostics
from publish import build_snapshot, issue_document, main, public_evidence, repository_link, validate_public_values
from report import Run


def recordings() -> tuple[list[Run], list[Run]]:
    """Create synthetic local fixtures, never claimed as actual hosted evidence."""
    runs: list[Run] = []
    for index, condition in enumerate(("baseline", "snapshot", "optimized"), 1):
        image_hash = "b" * 64 if condition == "optimized" else "a" * 64
        info = RunInfo.model_validate(
            {
                "run_id": f"{index:032x}",
                "timestamp": "2026-11-09T23:00:00+00:00",
                "condition": condition,
                "project_hash": "e" * 64,
                "agent": "private-probe",
                "agent_version": str(index),
                "image": "private-registry.azurecr.io/probe@sha256:" + image_hash,
                "cpu": "0.5",
                "memory": "1Gi",
                "configuration_hash": "f" * 64,
                "snapshots": condition != "baseline",
                "core_version": "2.3.0",
                "responses_version": "2.4.0",
                "samples": 1,
                "resume_samples": 1,
                "idle_seconds": 210,
            }
        )
        samples: list[Sample] = []
        for phase in ("cold", "warm", "resume"):
            headers = {PREFIX + "session-start-type": phase, PREFIX + "container-readiness-ms": "100"}
            _, timings, _ = diagnostics(headers)
            samples.append(
                Sample(
                    trial=1,
                    phase=phase,
                    timestamp=info.timestamp,
                    session_hash=f"{index + 10:064x}",
                    response_id=f"caresp_private_{condition}_{phase}",
                    passed=True,
                    start_type=phase,
                    headers=headers,
                    timings=timings | {"client_complete_ms": 6000 / index, "client_journey_ms": 6000 / index},
                )
            )
        runs.append(Run(info, samples))
    diagnostic = Sample(
        trial=1,
        phase="cold",
        timestamp=runs[0].info.timestamp,
        session_hash="d" * 64,
        errors=["Invocation/diagnostic failure: ValidationError; no POST retry attempted."],
        timings={"client_complete_ms": 5000},
    )
    return runs, [Run(runs[0].info, [diagnostic])]


def source_document(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    source = repo / "docs" / "issue-13.md"
    source.parent.mkdir(parents=True)
    agent = repo / "experiments" / "story13" / "main.py"
    agent.parent.mkdir(parents=True)
    agent.write_text("# Fixture only.\n")
    source.write_text(
        "# Story 13 - Fixture\n\n## Requirements\n\n"
        "| ID | Requirement |\n| --- | --- |\n| FR-1301 | Native snapshots |\n\n"
        "## Acceptance Criteria\n\nAC-1301: fixture evidence only.\n\n"
        "[Agent source](../experiments/story13/main.py)\n\n"
        "```powershell\nuv run python measure.py --execution preview\n```\n\n"
        "<script>alert('not executable')</script>\n",
        encoding="utf-8",
    )
    return repo, source


def test_public_payload_preserves_numbers_but_not_raw_bindings() -> None:
    runs, diagnostic = recordings()
    payload = public_evidence(runs, diagnostic)
    text = json.dumps(payload)
    assert payload["recorded_distinct_session_bindings"] == 4
    for private in ("private-registry", "private-probe", "caresp_private", "e" * 64, "f" * 64):
        assert private not in text
    assert "client_journey_ms" in text and "6000.0" in text
    assert text.count('"passed": true') == 9 and text.count('"passed": false') == 1
    exported_runs = payload["runs"]
    assert isinstance(exported_runs, list)
    assert exported_runs[0]["image_alias"] == exported_runs[1]["image_alias"]
    assert exported_runs[0]["image_alias"] != exported_runs[2]["image_alias"]


def test_markdown_preserves_all_sections_tables_and_safe_code(tmp_path: Path) -> None:
    repo, source = source_document(tmp_path)
    html = issue_document(source, repo, "main")
    assert 'id="issue-requirements"' in html and 'id="issue-acceptance-criteria"' in html
    assert '<div class="table-scroll"><table>' in html
    assert "uv run python measure.py" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "github.com/wxifaras/hosted-agents-poc/blob/main/experiments/story13/main.py" in html


def test_unknown_diagnostic_text_blocks_publication(tmp_path: Path) -> None:
    repo, source = source_document(tmp_path)
    runs, diagnostic = recordings()
    diagnostic[0].samples[0].errors = ["unreviewed secret must never be published"]
    with pytest.raises(ValueError, match="sanitization review"):
        build_snapshot(runs, diagnostic, source, repo)


def test_snapshot_is_standalone_and_retains_all_evidence(tmp_path: Path) -> None:
    repo, source = source_document(tmp_path)
    runs, diagnostic = recordings()
    files = build_snapshot(runs, diagnostic, source, repo)
    assert len(files) == 8
    html = files["index.html"]
    assert "Detailed hosted-agent cold-start validation" in html
    assert "Full issue 13 scope and runbook" in html and "FR-1301" in html and "AC-1301" in html
    assert "Task walkthroughs and evidence boundaries" in html
    assert "Every recorded observation and timing" in html
    assert 'id="technical" open' in html
    assert not re.search(r'(?:src|href)="(?:[^"]+\.svg|evidence\.json|summary\.md)"', html)
    assert html.count('src="data:image/svg+xml;base64,') == 3
    match = re.search(r'href="data:application/json;base64,([^"]+)"', html)
    assert match
    assert base64.b64decode(match[1]).decode() == files["sanitized-evidence.json"]
    for text in files.values():
        assert "private-registry" not in text and "caresp_private" not in text
        assert "e" * 64 not in text and "f" * 64 not in text


def test_publication_requires_all_three_conditions(tmp_path: Path) -> None:
    repo, source = source_document(tmp_path)
    runs, _ = recordings()
    with pytest.raises(ValueError, match="all three"):
        build_snapshot(runs[:2], [], source, repo)


def test_source_links_cannot_escape_repository_or_publish_machine_paths(tmp_path: Path) -> None:
    repo, source = source_document(tmp_path)
    with pytest.raises(ValueError, match="repository"):
        repository_link("../../outside.py", source, repo, "main")
    with pytest.raises(ValueError, match="HTTPS"):
        repository_link("file:///C:/Users/private/file.py", source, repo, "main")
    assert repository_link("#acceptance-criteria", source, repo, "main") == "#issue-acceptance-criteria"


def test_metadata_must_use_reviewed_version_and_timestamp_formats() -> None:
    runs, _ = recordings()
    runs[0].info.core_version = "unsupported-private-value"
    with pytest.raises(ValueError, match="SDK"):
        validate_public_values(runs)
    runs[0].info.core_version = "2.3.0"
    runs[0].info.timestamp = "not-a-timestamp"
    with pytest.raises(ValueError, match="timestamp"):
        validate_public_values(runs)


def test_publication_cli_never_overwrites_a_snapshot(tmp_path: Path) -> None:
    repo, source = source_document(tmp_path)
    runs, diagnostic = recordings()
    paths: list[Path] = []
    for index, run in enumerate([*runs, *diagnostic]):
        path = tmp_path / f"recording-{index}.jsonl"
        path.write_text(
            run.info.model_dump_json() + "\n" + "\n".join(sample.model_dump_json() for sample in run.samples) + "\n"
        )
        paths.append(path)
    output = repo / "docs" / "reports" / "story-13" / "fixture"
    args = [
        "--input",
        *[str(path) for path in paths[:3]],
        "--diagnostic-input",
        str(paths[3]),
        "--issue-source",
        str(source),
        "--output",
        str(output),
    ]
    assert main(args, repository_root=repo) == 0
    assert len(list(output.iterdir())) == 8
    assert main(args, repository_root=repo) == 2
