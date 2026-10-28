"""Offline aggregate truthfulness, privacy, provenance and portable output contracts."""

import base64
import json
from hashlib import sha256
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts import story1_report as report


@pytest.fixture
def bundle_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    monkeypatch.chdir(tmp_path)
    run = tmp_path / "source" / "run"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "recorded-run-1",
                "scenario": "story-1-step-3",
                "started_at": "2026-10-27T20:00:00Z",
                "project_version": "0.1.0",
                "maf_version": "1.19.0",
                "agentserver_version": "2.2.0b1",
            }
        )
    )
    record = {
        "run_id": "recorded-run-1",
        "step": "1,000,000 rows: interpreter memory",
        "status": "PASS",
        "timestamp": "2026-10-27T20:01:00Z",
        "expected": "Recorded memory",
        "observed": {
            "peak_rss_bytes": 844726272,
            "memory_limit_bytes": 2147483648,
            "cgroup_memory_peak_bytes": 912261120,
            "pressure_fraction": 0.39336,
            "end_to_end_seconds": 18.25,
        },
    }
    for name in ("results.jsonl", "events.jsonl"):
        (run / name).write_text(json.dumps(record) + "\n")
    config = {
        "schema": "story1-report-bundle-v1",
        "title": "Story 1",
        "environment": "deliberate-local-label",
        "runs": [
            {
                "key": "pressure",
                "label": "Pressure original",
                "run_dir": str(run),
                "scope": "Native memory",
                "agent_version": "9",
                "kind": "capacity",
            }
        ],
        "decisions": [
            {
                "area": "Pressure",
                "status": "Accepted for POC",
                "detail": "39.34%; not evaluated at 75%",
                "evidence_keys": ["pressure"],
            }
        ],
        "traceability": [
            {
                "requirement": "5. Pressure",
                "status": "Accepted",
                "summary": "Original BLOCKED retained",
                "run_keys": ["pressure"],
            }
        ],
    }
    input_path = tmp_path / "config.json"
    input_path.write_text(json.dumps(config))
    return input_path, run, tmp_path / "artifacts" / "html-report"


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value))


def _result(run: Path, **updates: object) -> None:
    record = json.loads((run / "results.jsonl").read_text().splitlines()[0])
    record.update(updates)
    (run / "results.jsonl").write_text(json.dumps(record) + "\n")


def test_original_statuses_and_measured_envelope_are_separate(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, run, output = bundle_input
    _result(run, step="Memory-pressure target", status="BLOCKED", observed={"target_reached": False})
    path = report.generate_bundle(input_path, output)
    data = _read(output / "sanitized-evidence.json")
    assert data["runs"][0]["counts"] == {"BLOCKED": 1}
    assert data["runs"][0]["completion"] == "Recorded checks, no finish-event recorded."
    assert data["sponsor_decisions"][0]["decision"] == "Accepted for POC"
    assert "COMPLETE" not in data["acceptance"]
    assert "not evaluated at 75%" in path.read_text()
    assert "Supervisor remains blocked" in path.read_text()


def test_numerical_memory_not_summed_and_mirrored_events_not_double_counted(
    bundle_input: tuple[Path, Path, Path],
) -> None:
    input_path, run, output = bundle_input
    original = (run / "results.jsonl").read_text()
    (run / "results.jsonl").write_text(original * 2)
    path = report.generate_bundle(input_path, output)
    data = _read(output / "sanitized-evidence.json")
    assert data["runs"][0]["counts"] == {"PASS": 1}
    assert data["runs"][0]["additional_event_count"] == 0
    assert len(data["memory"]) == 1
    assert data["memory"][0]["pressure_percent"] == 39.336
    assert data["memory"][0]["end_to_end_seconds"] == 18.25
    assert "not execution duration" in path.read_text()


def test_xss_and_nested_private_values_never_escape(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, run, output = bundle_input
    attack = '<script>alert(1)</script><img src=x onerror="alert(1)">'
    private = (
        "person@example.com https://private.example/path?sig=credential "
        "C:\\Users\\someone\\private.csv /home/person/private.csv \\\\server\\share\\secret.csv "
        "00000000-1111-2222-3333-444444444444 workflow_native123 " + "a" * 64
    )
    _result(
        run,
        step=attack,
        expected=private,
        observed={
            "note": [attack, {"note": private, "principal_sha256": "b" * 64, "model_code": "private source"}],
            "session_sha256": "c" * 64,
            "unknown": "do-not-export",
            "principal_sha256": "d" * 64,
        },
    )
    path = report.generate_bundle(input_path, output)
    exports = "\n".join(p.read_text() for p in output.rglob("*") if p.is_file())
    assert "<script>" not in path.read_text()
    assert "&lt;script&gt;" in path.read_text()
    for forbidden in (
        "person@example.com",
        "private.example",
        "someone",
        "/home/person",
        "\\\\server\\share",
        "native123",
        "00000000-1111-2222-3333-444444444444",
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "principal_sha256",
        "session_sha256",
        "do-not-export",
        "private source",
    ):
        assert forbidden not in exports


@pytest.mark.parametrize(
    "damage",
    [
        "missing-manifest",
        "missing-results",
        "bad-manifest",
        "invalid-status",
        "result-run-id",
        "event-run-id",
        "conclusion-run-id",
        "truncated-results",
        "version-mismatch",
    ],
)
def test_invalid_evidence_is_explicit_and_never_success(
    bundle_input: tuple[Path, Path, Path],
    damage: str,
) -> None:
    input_path, run, output = bundle_input
    if damage == "missing-manifest":
        (run / "manifest.json").unlink()
    elif damage == "missing-results":
        (run / "results.jsonl").unlink()
    elif damage == "bad-manifest":
        (run / "manifest.json").write_text("{")
    elif damage == "invalid-status":
        _result(run, status="COMPLETE")
    elif damage == "result-run-id":
        _result(run, run_id="different")
    elif damage == "event-run-id":
        (run / "events.jsonl").write_text('{"run_id":"different"}\n')
    elif damage == "conclusion-run-id":
        _write(run / "conclusion.json", {"run_id": "different", "state": "finished"})
    elif damage == "truncated-results":
        (run / "results.jsonl").write_text('{"run_id":')
    elif damage == "version-mismatch":
        _result(run, step="Native deployment binding", observed={"agents": {"data-analysis-agent": {"version": "8"}}})
    assert report.main(["--input", str(input_path), "--output", str(output)]) == 2
    data = _read(output / "sanitized-evidence.json")
    assert data["errors"] == ["pressure"]
    assert data["runs"][0]["checks"] == []
    assert data["runs"][0]["counts"] == {}
    assert "INVALID EVIDENCE" in data["runs"][0]["completion"]


def test_events_are_not_exported_as_raw_downloads(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, run, output = bundle_input
    with (run / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"run_id": "recorded-run-1", "model_code": "UNREVIEWED_MODEL_SOURCE"}) + "\n")
    report.generate_bundle(input_path, output)
    data = _read(output / "sanitized-evidence.json")
    assert data["runs"][0]["additional_event_count"] == 1
    assert data["runs"][0]["additional_events"] == []
    assert "UNREVIEWED_MODEL_SOURCE" not in json.dumps(data)
    assert not list(output.rglob("*.jsonl"))


def test_duplicate_binding_and_unknown_traceability_fail_closed(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, _, output = bundle_input
    config = _read(input_path)
    config["runs"].append({**config["runs"][0], "key": "duplicate"})
    _write(input_path, config)
    with pytest.raises(report.ReportError, match="Duplicate run_dir"):
        report.generate_bundle(input_path, output)
    config["runs"].pop()
    config["traceability"][0]["run_keys"] = ["missing"]
    _write(input_path, config)
    with pytest.raises(report.ReportError, match="Traceability"):
        report.generate_bundle(input_path, output)
    assert not output.exists()


def test_existing_output_requires_unchanged_sources_and_explicit_overwrite(
    bundle_input: tuple[Path, Path, Path],
) -> None:
    input_path, run, output = bundle_input
    before = {p.name: p.read_bytes() for p in run.iterdir()}
    report.generate_bundle(input_path, output)
    with pytest.raises(report.ReportError, match="Output exists"):
        report.generate_bundle(input_path, output)
    assert report.generate_bundle(input_path, output, overwrite=True).is_file()
    assert before == {p.name: p.read_bytes() for p in run.iterdir()}
    _result(run, expected="changed source")
    with pytest.raises(report.ReportError, match="unchanged"):
        report.generate_bundle(input_path, output, overwrite=True)


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.hrefs: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "a":
            self.hrefs.append(str(values["href"]))
        if tag == "img":
            self.images.append(str(values["src"]))


def test_relative_links_provenance_packaged_template_and_no_network(
    bundle_input: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import subprocess

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("No network"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No subprocess"))
    input_path, run, output = bundle_input
    path = report.generate_bundle(input_path, output)
    links = Links()
    links.feed(path.read_text())
    for href in links.hrefs:
        assert href[1:] in links.ids if href.startswith("#") else (output / href).is_file()
    data = _read(output / "sanitized-evidence.json")
    assert data["runs"][0]["run_id"] == "recorded-run-1"
    assert data["runs"][0]["sources"][0]["sha256"] == sha256((run / "manifest.json").read_bytes()).hexdigest()
    assert report.SOURCE_COMMIT in path.read_text()
    assert files("scripts.report_assets").joinpath("story1.html").is_file()
    assert "Content-Security-Policy" in path.read_text()
    assert "<script" not in path.read_text()


def _approved_chart(input_path: Path, run: Path) -> bytes:
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    (run / "chart.png").write_bytes(image)
    _write(
        run / "approval.json",
        {
            "accepted": True,
            "png_sha256": sha256(image).hexdigest(),
            "categories": ["Category A"],
            "revenue_cents": [123],
        },
    )
    config = _read(input_path)
    config["chart"] = {"run_key": "pressure", "path": "chart.png", "review_path": "approval.json"}
    _write(input_path, config)
    return image


def test_only_exact_approved_synthetic_png_is_exported(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, run, output = bundle_input
    image = _approved_chart(input_path, run)
    path = report.generate_bundle(input_path, output)
    assert (output / "evidence" / "approved-chart.png").read_bytes() == image
    assert sha256(image).hexdigest() in path.read_text()
    links = Links()
    links.feed(path.read_text())
    assert all(src.startswith("data:image/png;base64,") for src in links.images)
    for href in links.hrefs:
        assert href[1:] in links.ids if href.startswith("#") else (output / href).is_file()
    receipt = _read(run / "approval.json")
    receipt["png_sha256"] = "0" * 64
    _write(run / "approval.json", receipt)
    with pytest.raises(report.ReportError, match="SHA256"):
        report.generate_bundle(input_path, output, overwrite=True)


def test_chart_paths_cannot_escape_source_binding(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, run, output = bundle_input
    _approved_chart(input_path, run)
    config = _read(input_path)
    config["chart"]["path"] = "../chart.png"
    _write(input_path, config)
    with pytest.raises(report.ReportError, match="relative"):
        report.generate_bundle(input_path, output)


def test_summary_extracts_only_thirteen_current_rows(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, _, output = bundle_input
    numbers = ["1", "2", "3", "4", "4a", "4b", "5", "6", "7", "8", "8a", "8b", "8c"]
    summary = input_path.parent / "summary.md"
    summary.write_text(
        "## Summary\n\n| # | Requirement | Status | What works | Limitations | Next step |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(f"| {number} | R{number} | Scoped | measured | not inferred | next |" for number in numbers)
        + "\n## Historical\n| old | do not include |\n"
    )
    config = _read(input_path)
    config.pop("traceability")
    config["summary_source"] = str(summary)
    _write(input_path, config)
    report.generate_bundle(input_path, output)
    data = _read(output / "sanitized-evidence.json")
    assert len(data["traceability"]) == 13
    assert "do not include" not in json.dumps(data)
    assert data["summary_sha256"] == sha256(summary.read_bytes()).hexdigest()
    assert data["traceability"][6]["run_keys"] == ["pressure"]


def test_report_cannot_write_outside_ignored_artifacts(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, run, _ = bundle_input
    with pytest.raises(report.ReportError, match="artifacts"):
        report.generate_bundle(input_path, run / "report")


def test_copied_run_with_same_id_cannot_be_counted_twice(bundle_input: tuple[Path, Path, Path]) -> None:
    import shutil

    input_path, run, output = bundle_input
    copied = run.parent / "copied-run"
    shutil.copytree(run, copied)
    config = _read(input_path)
    config["runs"].append({**config["runs"][0], "key": "copied", "run_dir": str(copied)})
    _write(input_path, config)
    with pytest.raises(report.ReportError, match="Duplicate run_id"):
        report.generate_bundle(input_path, output)


def _lifecycle_input(input_path: Path, run: Path) -> list[dict]:
    config = _read(input_path)
    config["runs"][0]["kind"] = "lifecycle"
    _write(input_path, config)
    before = {
        "status": "active",
        "agent_version": "9",
        "created_at": 1700000000,
        "last_accessed_at": 1700000010,
        "stopped_at": None,
    }
    idle = {**before, "status": "idle", "last_accessed_at": 1700000500, "stopped_at": 1700000300}
    after = {**before, "last_accessed_at": 1700000800}
    records = [
        {"step": "Platform state before quiet interval", "observed": before},
        {
            "step": "Platform state before resume",
            "observed": {
                "quiet_seconds": 17.5,
                "samples": [
                    {**before, "quiet_seconds": 13.25},
                    {**idle, "quiet_seconds": 17.5},
                ],
            },
        },
        {
            "step": "Platform idle boundary witness",
            "observed": {
                "before": before,
                "idle_before_resume": idle,
                "after_resume": after,
                "same_session_binding": True,
                "forced_lifecycle_change": False,
            },
        },
        {
            "step": "Optional Hosted marker observation",
            "observed": {
                "quiet_seconds": 17.5,
                "process_changed": True,
                "home_retained": True,
            },
        },
    ]
    records = [
        {"run_id": "recorded-run-1", "status": "PASS", "expected": "Recorded lifecycle condition", **record}
        for record in records
    ]
    _write_records(run, records)
    return records


def _write_records(run: Path, records: list[dict]) -> None:
    for name in ("results.jsonl", "events.jsonl"):
        (run / name).write_text("".join(json.dumps(record) + "\n" for record in records))


def test_native_catalog_uses_reviewed_protocol_and_separates_privileges(
    bundle_input: tuple[Path, Path, Path],
) -> None:
    input_path, _, output = bundle_input
    path = report.generate_bundle(input_path, output)
    data = _read(output / "sanitized-evidence.json")
    catalog = data["native_api_catalog"]
    assert len(catalog) == 9
    shapes = "\n".join(entry["shape"] for entry in catalog)
    for required in (
        "VersionRefIndicator(agent_version=version)",
        "hosted.conversations.create()",
        "hosted.responses.with_raw_response.create",
        "POC_STORY1_NATIVE",
        'extra_body={"agent_session_id": own_session}',
        'purpose="assistants"',
        '"type":"code_interpreter"',
        '"type":"auto"',
        "max_tool_calls=1",
        "native.containers.files.content.with_streaming_response.retrieve",
        "project.agents.download_session_file",
        "project.agents.get_session",
        "Authenticated PUT",
    ):
        assert required in shapes
    assert all("Consumer" not in entry["shape"] for entry in catalog)
    assert "POC_CONSUMER_CI" not in path.read_text()
    native_output = next(entry for entry in catalog if entry["operation"] == "Native original output retrieval")
    assert native_output["caller"] == "Authorized backend only"
    assert "no model submission" in native_output["activity"]
    assert "code/reference context" in path.read_text()


def test_native_diagram_is_accessible_offline_and_explicitly_reference_context(
    bundle_input: tuple[Path, Path, Path],
) -> None:
    input_path, _, output = bundle_input
    html = report.generate_bundle(input_path, output).read_text()
    svg = html[html.index("<svg ") : html.index("</svg>") + len("</svg>")]
    root = ElementTree.fromstring(svg)
    assert root.attrib["role"] == "img"
    assert root.find("{http://www.w3.org/2000/svg}title") is not None
    assert root.find("{http://www.w3.org/2000/svg}desc") is not None
    assert "Existing Supervisor" in svg and "BLOCKED" in svg
    assert "Existing Research" in svg and "Existing Data Analysis" in svg
    assert "SessionFiles-mediated" in svg and "Separate runner PUT/GET" in svg
    assert "href=" not in svg and "<script" not in svg and "mermaid" not in html.lower()
    assert "not platform observations" in html


def test_lifecycle_timeline_is_derived_and_keeps_runner_and_server_clocks_separate(
    bundle_input: tuple[Path, Path, Path],
) -> None:
    input_path, run, output = bundle_input
    _lifecycle_input(input_path, run)
    path = report.generate_bundle(input_path, output)
    timeline = _read(output / "sanitized-evidence.json")["lifecycle_timeline"][0]
    assert timeline["status"] == "RECORDED" and timeline["notes"] == []
    assert [row["status"] for row in timeline["rows"]] == ["active", "active", "idle", "active"]
    assert [row["quiet_seconds"] for row in timeline["rows"]] == [None, 13.25, 17.5, None]
    assert timeline["rows"][2]["server_metadata"]["stopped_at"] == 1700000300
    assert timeline["markers"] == {"process_changed": True, "home_retained": True}
    assert "different clocks" in path.read_text()
    assert "332.861" not in path.read_text() and "301.988" not in path.read_text()


@pytest.mark.parametrize(
    "damage",
    [
        "missing-samples",
        "malformed-sample",
        "bad-state",
        "bad-quiet",
        "bad-server-time",
        "missing-witness",
        "wrong-version",
        "missing-marker",
        "wrong-binding",
        "wrong-witness-state",
    ],
)
def test_lifecycle_timeline_invalid_or_missing_observations_remain_partial(
    bundle_input: tuple[Path, Path, Path],
    damage: str,
) -> None:
    input_path, run, output = bundle_input
    records = _lifecycle_input(input_path, run)
    if damage == "missing-samples":
        records[1]["observed"].pop("samples")
    elif damage == "malformed-sample":
        records[1]["observed"]["samples"][0] = ["not a metadata object"]
    elif damage == "bad-state":
        records[1]["observed"]["samples"][0]["status"] = {"untrusted": "<script>bad</script>"}
    elif damage == "bad-quiet":
        records[1]["observed"]["samples"][0]["quiet_seconds"] = -1
    elif damage == "bad-server-time":
        records[1]["observed"]["samples"][0]["created_at"] = "not Unix seconds"
    elif damage == "missing-witness":
        records.pop(2)
    elif damage == "wrong-version":
        records[1]["observed"]["samples"][0]["agent_version"] = "8"
    elif damage == "missing-marker":
        records[-1]["observed"].pop("home_retained")
    elif damage == "wrong-binding":
        records[2]["observed"]["same_session_binding"] = False
    elif damage == "wrong-witness-state":
        records[2]["observed"]["after_resume"]["status"] = "idle"
    _write_records(run, records)
    path = report.generate_bundle(input_path, output)
    timeline = _read(output / "sanitized-evidence.json")["lifecycle_timeline"][0]
    assert timeline["status"] == "PARTIAL" and timeline["notes"]
    assert "<script>" not in path.read_text()


def test_timeline_projection_excludes_nested_identity_values(bundle_input: tuple[Path, Path, Path]) -> None:
    input_path, run, output = bundle_input
    records = _lifecycle_input(input_path, run)
    records[1]["observed"]["samples"][0].update(
        {
            "principal_sha256": "f" * 64,
            "endpoint": "https://private.invalid/resource?signature=secret",
            "note": "person@example.com C:\\Users\\privateperson\\state.json",
        }
    )
    _write_records(run, records)
    path = report.generate_bundle(input_path, output)
    exported = json.dumps(_read(output / "sanitized-evidence.json")) + path.read_text()
    assert "f" * 64 not in exported
    assert "private.invalid" not in exported
    assert "person@example.com" not in exported and "privateperson" not in exported
    timeline = _read(output / "sanitized-evidence.json")["lifecycle_timeline"][0]
    assert "note" not in timeline["rows"][1]["server_metadata"]


def test_no_final_lifecycle_binding_does_not_promote_historical_runs(
    bundle_input: tuple[Path, Path, Path],
) -> None:
    input_path, run, output = bundle_input
    _lifecycle_input(input_path, run)
    config = _read(input_path)
    config["runs"][0]["kind"] = "historical-observation"
    _write(input_path, config)
    path = report.generate_bundle(input_path, output)
    assert _read(output / "sanitized-evidence.json")["lifecycle_timeline"] == []
    assert "No explicit final lifecycle binding supplied" in path.read_text()
