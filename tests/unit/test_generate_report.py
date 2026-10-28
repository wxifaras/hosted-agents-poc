"""Offline report truthfulness, attachment integrity, escaping, privacy, portability and no-replay contracts."""

import base64
import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from scripts import generate_report as report
from scripts.report_api_catalog import api_catalog
from scripts.report_appendix import boundary_references, build_appendix, counter_references
from scripts.report_diagrams import architecture_svg, native_bypass_sequence_svg, sequence_svg

RUN_ID = "unit-run"


def evidence(
    tmp_path: Path,
    results: list[dict] | None = None,
    *,
    finished: bool = False,
    scenario: str = "consumer-native-ci",
    exit_code: int = 0,
) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "scenario": scenario,
                "started_at": "2026-10-26T12:00:00+00:00",
            }
        )
    )
    if results is None:
        results = []
    records = [{"run_id": RUN_ID, "timestamp": "2026-10-26T12:01:00+00:00", **record} for record in results]
    for name in ("results.jsonl", "events.jsonl"):
        (run / name).write_text("".join(json.dumps(record) + "\n" for record in records))
    if finished:
        (run / "conclusion.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "state": "finished",
                    "validation_exit_code": exit_code,
                    "finished_at": "2026-10-26T12:02:00+00:00",
                }
            )
        )
    return run


def bundle(run: Path, **kwargs) -> tuple[Path, dict]:
    path = report.generate_report(run, **kwargs)
    return path, json.loads((path.parent / "sanitized-evidence.json").read_text())


def test_partial_passing_checks_never_imply_complete_proof(tmp_path: Path) -> None:
    run = evidence(tmp_path, [{"step": "Consumer deployment and permissions", "status": "PASS"}])
    path, data = bundle(run)
    assert data["verdict"] == "INCOMPLETE"
    html = path.read_text()
    assert "NOT RECORDED" in html and "Native container-file listing/content" in html
    assert "No explicit completion record" in html


@pytest.mark.parametrize("status,verdict", [("FAIL", "FAIL"), ("BLOCKED", "BLOCKED"), ("SKIP", "INCOMPLETE")])
def test_failures_and_prerequisite_blocks_are_not_relabelled_pass(
    tmp_path: Path,
    status: str,
    verdict: str,
) -> None:
    run = evidence(tmp_path, [{"step": "test", "status": status}])
    _, data = bundle(run)
    assert data["verdict"] == verdict


def test_full_legacy_consumer_proof_is_scoped_and_completeness_is_disclosed(tmp_path: Path) -> None:
    names = ["Consumer deployment and permissions"]
    for actor in ("user1", "user2"):
        names.extend(
            actor + ": " + name
            for name in (
                "deployed runtime evidence",
                "original input binding",
                "original bytes",
                "original bytes",
                "analysis values",
                "PNG bytes",
                "foreign Hosted output download",
                "foreign Hosted output download",
                "foreign application inspect",
                "foreign application artifact",
                "foreign Hosted response",
                "native Files privilege boundary",
            )
        )
    run = evidence(tmp_path, [{"step": name, "status": "PASS"} for name in names])
    path, data = bundle(run)
    assert data["verdict"] == "PASS"
    assert "inferred completeness" in path.read_text()
    assert "NOT TESTED by the bounded consumer proof" in path.read_text()


def test_explicit_general_run_completion_and_durations(tmp_path: Path) -> None:
    run = evidence(
        tmp_path, [{"step": "unit", "status": "PASS", "duration_ms": 12.5}], scenario="another-validator", finished=True
    )
    path, data = bundle(run, title="Reusable validator")
    assert data["verdict"] == "PASS"
    assert "12.5 ms (recorded)" in path.read_text()
    assert "120.00 s" in path.read_text()


def test_conflicting_success_conclusion_is_rejected(tmp_path: Path) -> None:
    run = evidence(tmp_path, [{"step": "leak", "status": "FAIL"}], finished=True)
    with pytest.raises(report.ReportError, match="conflicts"):
        report.generate_report(run)


def test_template_escapes_html_and_all_downloads_are_sanitized(tmp_path: Path) -> None:
    sensitive = (
        '<script>alert("xss")</script> Bearer super-secret-token token=unshareable '
        "user@example.com cntr_12345678 file-secret "
        "11111111-1111-4111-8111-111111111111 https://private.example.com/path?sig=capability"
    )
    run = evidence(
        tmp_path,
        [
            {
                "step": "check",
                "status": "FAIL",
                "detailed_step": sensitive,
                "observed": {"Authorization": "secret-value", "detail": sensitive},
            }
        ],
    )
    path, data = bundle(run, title='<img src=x onerror="bad">')
    html = path.read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;img" in html
    for secret in (
        "super-secret-token",
        "unshareable",
        "user@example.com",
        "cntr_12345678",
        "file-secret",
        "11111111-1111-4111-8111-111111111111",
        "private.example.com",
        "capability",
        "secret-value",
    ):
        assert secret not in html and secret not in json.dumps(data)
    # Encoded downloads must not conceal raw sensitive originals.
    with ZipFile(path.parent / "report.zip") as archive:
        assert all(not name.startswith(("/", "..")) for name in archive.namelist())
        for name in archive.namelist():
            if name.endswith(".json"):
                assert "super-secret-token" not in archive.read(name).decode()


def test_truncated_final_line_requires_explicit_incomplete_option(tmp_path: Path) -> None:
    run = evidence(tmp_path, [{"step": "one", "status": "PASS"}])
    with (run / "events.jsonl").open("a") as stream:
        stream.write('{"unfinished":')
    with pytest.raises(report.ReportError, match="malformed"):
        report.generate_report(run)
    path, data = bundle(run, allow_incomplete=True)
    assert data["verdict"] == "INCOMPLETE" and "incomplete final record" in path.read_text()


def test_original_evidence_is_unchanged_and_exports_never_overwrite(tmp_path: Path) -> None:
    run = evidence(tmp_path, [{"step": "one", "status": "PASS"}])
    original = {path.name: path.read_bytes() for path in run.iterdir()}
    first, _ = bundle(run)
    second, _ = bundle(run)
    assert first != second
    assert all((run / name).read_bytes() == data for name, data in original.items())
    assert first.is_file() and second.is_file()


def test_report_rejects_unverified_attachment_paths_and_tampering(tmp_path: Path) -> None:
    run = evidence(tmp_path)
    manifest = {"attachments": [{"actor": "user1", "filename": "chart.png", "local_file": "../secret.png"}]}
    (run / "report-attachments.json").write_text(json.dumps(manifest))
    with pytest.raises(report.ReportError, match="outside"):
        report.generate_report(run)
    manifest["attachments"][0].update(local_file="report-artifacts/user1-chart.png", length=3, sha256="wrong")
    (run / "report-artifacts").mkdir()
    (run / "report-artifacts/user1-chart.png").write_bytes(b"bad")
    (run / "report-attachments.json").write_text(json.dumps(manifest))
    with pytest.raises(report.ReportError, match="original-byte"):
        report.generate_report(run)


def test_verified_original_png_embedded_and_missing_files_never_fetched(tmp_path: Path) -> None:
    run = evidence(tmp_path)
    data = report.PNG_HEADER + b"synthetic image"
    directory = run / "report-artifacts"
    directory.mkdir()
    (directory / "user1-chart.png").write_bytes(data)
    (run / "report-attachments.json").write_text(
        json.dumps(
            {
                "attachments": [
                    {
                        "actor": "user1",
                        "filename": "chart.png",
                        "local_file": "report-artifacts/user1-chart.png",
                        "length": len(data),
                        "sha256": report.sha256(data).hexdigest(),
                    }
                ]
            }
        )
    )
    html, _ = bundle(run)
    assert "data:image/png;base64," + base64.b64encode(data).decode() in html.read_text()
    assert "user1-chart.png" in html.read_text()
    assert "script src=" not in html.read_text() and "https://cdn" not in html.read_text()
    with ZipFile(html.parent / "report.zip") as archive:
        assert archive.read("user1-chart.png") == data


def test_python_svg_diagrams_are_accessible_and_offline() -> None:
    for value in (architecture_svg(), sequence_svg()):
        root = ElementTree.fromstring(value)
        assert root.tag.endswith("svg") and root.attrib["role"] == "img"
        assert root.find("{http://www.w3.org/2000/svg}title") is not None
        assert "under test" in value and "<script" not in value
        assert "href=" not in value


class ReportLinks(HTMLParser):
    """Collect actual HTML anchors, not base64-embedded JSON or SVG payloads."""

    def __init__(self) -> None:
        super().__init__()
        self.identifiers: list[str] = []
        self.targets: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if identifier := values.get("id"):
            self.identifiers.append(identifier)
        if target := values.get("href"):
            if target.startswith("#"):
                self.targets.append(target[1:])
        if tag == "img" and (source := values.get("src")):
            self.images.append(source)


@pytest.mark.parametrize("scenario", ["consumer-native-ci", "another-validator"])
def test_appendix_covers_boundaries_completion_and_every_html_anchor(tmp_path: Path, scenario: str) -> None:
    run = evidence(tmp_path, finished=True, scenario=scenario)
    completion = json.loads((run / "conclusion.json").read_text())
    completion.update(analysis_attempts=0, synthetic_output_capture=False, extension_field=None)
    (run / "conclusion.json").write_text(json.dumps(completion))
    path, data = bundle(run)
    html = path.read_text()
    parsed = ReportLinks()
    parsed.feed(html)
    assert len(parsed.identifiers) == len(set(parsed.identifiers))
    assert set(parsed.targets) <= set(parsed.identifiers)
    assert html.index('id="technical-appendix"') > html.index('id="downloads"')
    assert "Numbered code-call / validation flow" in html
    assert "Unknown field: no unit, SDK call" in html
    assert data["verdict"] == "INCOMPLETE"
    if scenario == "consumer-native-ci":
        boundaries, fields = build_appendix(
            [(name, "NOT ASSESSED", "Nothing inferred") for name in boundary_references()],
            completion,
            [],
            api_catalog(),
        )
        assert len(boundaries) == 9
        assert {field.name for field in fields} == set(counter_references()) | set(completion)
        for entry in [*boundaries, *fields]:
            assert f'id="{entry.identifier}"' in html
            assert f'href="#{entry.identifier}"' in html
        by_name = {field.name: field for field in fields}
        assert by_name["analysis_attempts"].value == "0"
        assert by_name["synthetic_output_capture"].value == "false"
        assert by_name["extension_field"].value == "null"
        assert by_name["foreign_probes"].value == "NOT RECORDED"
        assert not by_name["foreign_probes"].recorded
        assert "NOT ASSESSED - OPTIONAL" in html and "OUT OF SCOPE" in html
        assert "No application SDK method proves this boundary" in html
    else:
        assert 'id="appendix-sequence-native"' not in html
    assert "<script" not in html
    assert all(url.startswith("data:") for url in parsed.images)
    with ZipFile(path.parent / "report.zip") as archive:
        if scenario == "consumer-native-ci":
            svg = archive.read("native-bypass-sequence.svg").decode()
            assert "OPTIONAL native bypass" in svg
            sources = json.loads(archive.read("architecture-source.json"))
            assert len(sources["optional_native_bypass_sequence"]) == 12
        else:
            assert "native-bypass-sequence.svg" not in archive.namelist()
            assert "architecture-source.json" not in archive.namelist()
            assert parsed.images == []
            assert data["acceptance_policy"] == "recorded-evidence-only"
            assert "Consumer-facing isolation acceptance:" not in html


def test_completion_definitions_reference_existing_catalog_entries() -> None:
    assert counter_references()
    ids = {api["id"] for api in api_catalog()}
    for reference in [*boundary_references().values(), *counter_references().values()]:
        assert set(reference.apis) <= ids
        assert len(reference.flow) >= 2
        assert all("def " not in step and "import " not in step for step in reference.flow)


def test_optional_sequence_is_accessible_offline_and_explicitly_not_evidence() -> None:
    svg = native_bypass_sequence_svg()
    root = ElementTree.fromstring(svg)
    assert root.attrib["role"] == "img"
    assert root.find("{http://www.w3.org/2000/svg}title") is not None
    assert "reference design, not execution evidence" in svg
    assert "privileged-control" in svg and "OPTIONAL" in svg
    assert "href=" not in svg and "<script" not in svg


def test_appendix_values_and_unknown_field_names_are_redacted_and_escaped(tmp_path: Path) -> None:
    run = evidence(tmp_path, finished=True)
    conclusion = json.loads((run / "conclusion.json").read_text())
    conclusion["user@example.com"] = "<img src=x onerror=alert(1)> file-private-reference"
    conclusion["probe_private_key"] = "never-publish-this"
    (run / "conclusion.json").write_text(json.dumps(conclusion))
    path, _ = bundle(run)
    appendix = path.read_text().split('<section id="technical-appendix">', 1)[1]
    assert "user@example.com" not in appendix
    assert "never-publish-this" not in appendix
    assert "file-private-reference" not in appendix
    assert "<img src=x" not in appendix and "&lt;img" in appendix


def test_generator_has_no_cloud_runner_dependency_or_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket
    import subprocess

    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("Network prohibited"))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Subprocess prohibited"))
    run = evidence(tmp_path, [{"step": "check", "status": "FAIL"}])
    assert report.main(["--run-dir", str(run)]) == 0


def test_cli_missing_evidence_errors_without_success(tmp_path: Path) -> None:
    assert report.main(["--run-dir", str(tmp_path)]) != 0


def test_readiness_report_is_not_an_isolation_pass(tmp_path: Path) -> None:
    run = evidence(tmp_path, [{"step": "Consumer deployment and permissions", "status": "PASS"}], finished=True)
    conclusion = json.loads((run / "conclusion.json").read_text())
    conclusion["mode"] = "readiness"
    (run / "conclusion.json").write_text(json.dumps(conclusion))
    _, data = bundle(run)
    assert data["verdict"] == "READINESS ONLY"


def test_complete_consumer_scope_keeps_native_diagnostics_optional_and_platform_separate(tmp_path: Path) -> None:
    rows = [{"step": name, "check_id": key, "status": "PASS"} for key, name in report.expected_consumer_checks()]
    run = evidence(tmp_path, rows, finished=True)
    html, data = bundle(run)
    assert data["verdict"] == "PASS" and data["consumer_acceptance"] == "COMPLETE"
    assert not data["mandatory_missing"] and data["mandatory_total"] == 25
    assert data["advanced_native_acceptance"] == "NOT ASSESSED - OPTIONAL"
    text = html.read_text()
    assert "DOCUMENTED PLATFORM ASSURANCE" in text and "OUTSIDE Consumer acceptance" in text
    assert "VM / Hyper-V isolation" in text and "Chart visual quality" in text


def test_expanded_acceptance_needs_every_check_and_exact_counters(tmp_path: Path) -> None:
    rows = [
        {"step": name, "check_id": key, "status": "PASS"}
        for key, name in report.expected_consumer_checks(expanded=True)
    ]
    run = evidence(tmp_path, rows, finished=True)
    conclusion = json.loads((run / "conclusion.json").read_text())
    conclusion.update(suite="consumer-native-v2", analysis_attempts=2, foreign_probes=26, native_control_requests=28)
    (run / "conclusion.json").write_text(json.dumps(conclusion))
    _, data = bundle(run)
    assert data["consumer_acceptance"] == "COMPLETE" and not data["mandatory_missing"]
    conclusion["foreign_probes"] = 25
    (run / "conclusion.json").write_text(json.dumps(conclusion))
    _, data = bundle(run)
    assert data["consumer_acceptance"] == "COMPLETE"
    assert data["advanced_native_acceptance"] == "INCOMPLETE"


def test_missing_native_chart_download_cannot_be_hidden_by_other_passes(tmp_path: Path) -> None:
    rows = [
        {"step": name, "check_id": key, "status": "PASS"}
        for key, name in report.expected_consumer_checks(expanded=True)
        if key != "user2:native-output-content:chart.png"
    ]
    run = evidence(tmp_path, rows, finished=True)
    conclusion = json.loads((run / "conclusion.json").read_text())
    conclusion.update(suite="consumer-native-v2", analysis_attempts=2, foreign_probes=26, native_control_requests=28)
    (run / "conclusion.json").write_text(json.dumps(conclusion))
    _, data = bundle(run)
    assert data["consumer_acceptance"] == "COMPLETE" and not data["mandatory_missing"]
    assert data["advanced_native_acceptance"] == "INCOMPLETE"


@pytest.mark.parametrize("missing", ["user1:foreign-json", "user2:foreign-png", "user1:values", "user2:artifact"])
def test_required_consumer_checks_cannot_be_removed_to_force_complete(tmp_path: Path, missing: str) -> None:
    rows = [
        {"step": name, "check_id": key, "status": "PASS"}
        for key, name in report.expected_consumer_checks()
        if key != missing
    ]
    run = evidence(tmp_path, rows, finished=True)
    _, data = bundle(run)
    assert data["consumer_acceptance"] == "INCOMPLETE" and len(data["mandatory_missing"]) == 1


def test_optional_native_failure_does_not_erase_complete_consumer_scope(tmp_path: Path) -> None:
    rows = [{"step": name, "check_id": key, "status": "PASS"} for key, name in report.expected_consumer_checks()]
    rows.append(
        {
            "step": "user2: native output download denied",
            "check_id": "user2:native-output-content:chart.png",
            "status": "FAIL",
        }
    )
    run = evidence(tmp_path, rows, finished=True, exit_code=1)
    conclusion = json.loads((run / "conclusion.json").read_text())
    conclusion.update(suite="consumer-native-v2", analysis_attempts=2, foreign_probes=15)
    (run / "conclusion.json").write_text(json.dumps(conclusion))
    _, data = bundle(run)
    assert data["verdict"] == "FAIL" and data["consumer_acceptance"] == "COMPLETE"
    assert data["advanced_native_acceptance"] == "FAILED"


def test_legacy_direction_normalization_preserves_original_evidence(tmp_path: Path) -> None:
    run = evidence(tmp_path, [{"step": "user2: foreign Hosted output download", "status": "PASS"}])
    original = (run / "results.jsonl").read_bytes()
    html, _ = bundle(run)
    text = html.read_text()
    assert "user2: download user1" in text and "legacy label normalized" in text
    assert "Resource user:</strong> user1" in text
    assert (run / "results.jsonl").read_bytes() == original
    assert report.display_references({"step": "foreign file check"}, None, consumer=True)[1:] == (
        "unspecified",
        "unspecified",
    )
    assert (
        report.explicit_user_text(
            "The other user cannot read the owner's file.", "user2", "user1", "user2:foreign-json"
        )
        == "user2 cannot read user1's file."
    )
    assert report.explicit_user_text("owner file", "unspecified", "unspecified", None) == "owner file"


def test_sealed_test_control_data_never_enters_exported_evidence(tmp_path: Path) -> None:
    run = evidence(
        tmp_path,
        [{"step": "check", "status": "FAIL", "observed": {"sealed_probe_lineage": {"ciphertext": "private-capsule"}}}],
    )
    html, data = bundle(run)
    assert "private-capsule" not in html.read_text() and "private-capsule" not in json.dumps(data)


def test_legacy_download_explanation_has_no_fake_prompt_or_http_status(tmp_path: Path) -> None:
    run = evidence(
        tmp_path,
        [
            {
                "step": "user2: download user1's result.json denied",
                "status": "PASS",
                "observed": {"denied": True},
            }
        ],
    )
    path, _ = bundle(run)
    text = path.read_text()
    assert "No prompt is sent by this operation." in text and "Implementation explanation" in text
    assert "Per-check correlation was not recorded" in text
    assert "0 explicitly linked records" in text and "GET /api/projects" in text


def test_report_links_only_explicit_correlation_not_nearby_http(tmp_path: Path) -> None:
    from scripts.check_details import describe_check

    execution = {**describe_check("download", "user2:foreign-json"), "correlation_id": "op-one"}
    run = evidence(
        tmp_path,
        [
            {
                "step": "user2: download user1's result.json denied",
                "status": "PASS",
                "execution": execution,
                "check_id": "user2:foreign-json",
            }
        ],
    )
    with (run / "events.jsonl").open("a") as stream:
        for correlation, phase, status in (
            ("op-one", "positive_before", 200),
            ("op-unrelated", "negative_attempt", 401),
            ("op-one", "negative_attempt", 403),
            ("op-one", "positive_after", 200),
        ):
            stream.write(
                json.dumps(
                    {
                        "kind": "http",
                        "run_id": RUN_ID,
                        "correlation_id": correlation,
                        "control_phase": phase,
                        "status": status,
                        "actor": "user2" if status != 200 else "user1",
                    }
                )
                + "\n"
            )
    path, _ = bundle(run)
    text = path.read_text()
    checks = text.split('<section id="checks">', 1)[1].split('<section id="timeline">', 1)[0]
    assert re.search(r"positive_before[^<]*1 explicitly linked records", checks)
    assert re.search(r"negative_attempt[^<]*1 explicitly linked records", checks)
    assert '"status": 403' in unescape(checks) and '"status": 401' not in unescape(checks)
    assert '"status": 401' in unescape(text)


def test_backend_confirmed_prompt_is_not_replaced_by_a_planned_prompt(tmp_path: Path) -> None:
    run = evidence(
        tmp_path,
        [
            {
                "step": "user1: backend execution receipt",
                "status": "PASS",
                "observed": {"model_prompt": "Actual submitted synthetic prompt"},
            }
        ],
    )
    path, _ = bundle(run)
    assert "Recorded backend-confirmed submitted model prompt" in path.read_text()
    assert "Actual submitted synthetic prompt" in path.read_text()


def test_filenames_raw_identifiers_and_keys_are_not_leaked(tmp_path: Path) -> None:
    raw_id = "33333333-3333-4333-8333-333333333333"
    run = evidence(
        tmp_path,
        [{"step": "check " + raw_id, "status": "FAIL", "observed": {raw_id: {"native": "cntr_privateexample"}}}],
    )
    path, data = bundle(run)
    assert raw_id not in path.read_text() and raw_id not in json.dumps(data)


def test_oversized_evidence_and_cross_run_assertions_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = evidence(tmp_path, [{"step": "check", "status": "PASS", "run_id": "other-run"}])
    with pytest.raises(report.ReportError, match="different run"):
        report.generate_report(run)
    monkeypatch.setattr(report, "MAX_ATTACHMENT_BYTES", 1)
    # Explicit limit is exercised without loading an unbounded input.
    with pytest.raises(report.ReportError, match="size limit"):
        report.read_bytes(run / "manifest.json", 1)
