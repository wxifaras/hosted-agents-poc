"""Validate graph mathematics, evidence controls, missing values, and safe rendering."""

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError

from measure import PREFIX, RunInfo, Sample, diagnostics
from report import (
    Run,
    describe,
    distribution,
    findings,
    load_run,
    main,
    render,
    simple_comparison,
    summary_content,
    summary_markdown,
    validate_controls,
    values,
)


def make_run(condition: str = "baseline", *, readiness: float | None = 100) -> Run:
    info = RunInfo.model_validate(
        {
            "run_id": "test",
            "timestamp": "2026-11-09T00:00:00Z",
            "condition": condition,
            "project_hash": "project-hash",
            "agent": "story13",
            "agent_version": "1",
            "image": "registry/probe@sha256:" + "a" * 64,
            "cpu": "0.5",
            "memory": "1Gi",
            "configuration_hash": "same-config",
            "snapshots": condition != "baseline",
            "core_version": "2.3.0",
            "responses_version": "2.4.0",
            "samples": 1,
            "resume_samples": 0,
            "idle_seconds": 150,
        }
    )
    headers = {PREFIX + "session-start-type": "cold"}
    if readiness is not None:
        headers[PREFIX + "container-readiness-ms"] = str(int(readiness))
    _, timings, _ = diagnostics(headers)
    sample = Sample(
        trial=1,
        phase="cold",
        timestamp=info.timestamp,
        session_hash="session-hash",
        start_type="cold",
        passed=True,
        response_id="resp_test",
        headers=headers,
        timings=timings | {"client_complete_ms": 200, "session_create_ms": 50, "client_journey_ms": 250},
    )
    return Run(info, [sample])


def test_distribution_math_and_svg_accessibility() -> None:
    assert describe([10, 30, 20]) == "n=3; median 20.0; range 10.0-30.0 ms"
    svg = distribution("Measured <startup>", [("snapshot", [10, 30, 20]), ("missing", [])])
    root = ElementTree.fromstring(svg)
    assert root.attrib["role"] == "img"
    assert len(root.findall(".//{http://www.w3.org/2000/svg}circle")) == 3
    assert "Unavailable (n=0)" in svg and "&lt;startup&gt;" in svg


def test_missing_and_failed_samples_do_not_become_zero() -> None:
    run = make_run(readiness=None)
    assert values(run, "cold", "container-readiness-ms") == []
    assert values(run, "cold", "client_complete_ms") == [200]
    run.samples[0].passed = False
    run.samples[0].errors.append("Observed failure")
    assert values(run, "cold", "client_complete_ms") == []
    html, charts = render([run])
    assert "FAIL" in html and "Observed failure" in html and "NOT ACCEPTED" in html
    assert "INSUFFICIENT EVIDENCE" in html
    assert len(charts) == 5


def test_control_mismatches_are_rejected() -> None:
    baseline, snapshot = make_run(), make_run("snapshot")
    validate_controls([baseline, snapshot])
    snapshot.info.image = "different@sha256:" + "b" * 64
    with pytest.raises(ValueError, match="same digest"):
        validate_controls([baseline, snapshot])
    snapshot.info.image = baseline.info.image
    snapshot.info.cpu = "1"
    with pytest.raises(ValueError, match="controls differ"):
        validate_controls([baseline, snapshot])
    with pytest.raises(ValueError, match="distinct"):
        validate_controls([baseline, baseline])


def test_reduction_is_calculated_and_regression_not_hidden() -> None:
    baseline, snapshot = make_run(readiness=100), make_run("snapshot", readiness=150)
    assert any("-50.0% median reduction" in note for note in findings([baseline, snapshot]))


def test_three_condition_report_requires_an_actually_distinct_optimized_image() -> None:
    baseline, snapshot, optimized = make_run(), make_run("snapshot"), make_run("optimized")
    with pytest.raises(ValueError, match="distinct optimized image"):
        render([baseline, snapshot, optimized])
    optimized.info.image = "different-registry/alias@sha256:" + "a" * 64
    with pytest.raises(ValueError, match="distinct optimized image"):
        render([baseline, snapshot, optimized])
    optimized.info.image = "registry/probe@sha256:" + "b" * 64
    html, charts = render([baseline, snapshot, optimized])
    assert "INCOMPLETE: not all three" not in html
    assert "INSUFFICIENT EVIDENCE" in html
    assert len(charts) == 5


def test_untrusted_labels_are_escaped() -> None:
    run = make_run()
    run.info.image = '<script>alert("x")</script>@sha256:' + "a" * 64
    html, _ = render([run])
    assert "<script>" not in html and "&lt;script&gt;" in html


def write_run(path: Path, run: Run) -> None:
    path.write_text(
        run.info.model_dump_json() + "\n" + "\n".join(sample.model_dump_json() for sample in run.samples) + "\n",
        encoding="utf-8",
    )


def test_report_cli_writes_reusable_graphs_and_refuses_overwrite(tmp_path: Path) -> None:
    source, output = tmp_path / "run.jsonl", tmp_path / "report"
    write_run(source, make_run())
    assert main(["--input", str(source), "--output", str(output)]) == 0
    assert {path.name for path in output.iterdir()} == {
        "index.html",
        "evidence.json",
        "startup-distribution.svg",
        "readiness.svg",
        "cold-components.svg",
        "first-response-comparison.svg",
        "preparation-comparison.svg",
        "summary.md",
    }
    assert json.loads((output / "evidence.json").read_text())[0]["run"]["source"] == "hosted"
    summary = (output / "summary.md").read_text(encoding="utf-8")
    assert "## Summary" in summary and "## Conclusion" in summary
    assert main(["--input", str(source), "--output", str(output)]) == 2


def test_duplicate_and_falsified_evidence_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "run.jsonl"
    run = make_run()
    run.samples.append(run.samples[0])
    write_run(source, run)
    with pytest.raises(ValueError, match="Duplicate"):
        load_run(source)
    run.samples.pop()
    run.samples[0].timings["container-readiness-ms"] = 999
    write_run(source, run)
    with pytest.raises(ValueError, match="disagree"):
        load_run(source)


def test_imported_evidence_cannot_export_arbitrary_headers_or_nonfinite_values() -> None:
    payload = make_run().samples[0].model_dump()
    with pytest.raises(ValidationError, match="allowlisted"):
        Sample.model_validate(payload | {"headers": {"Authorization": "must-not-export"}})
    for bad in (float("nan"), float("inf"), -1, True):
        with pytest.raises(ValidationError):
            Sample.model_validate(payload | {"timings": {"client_complete_ms": bad}})


def test_initial_diagnostic_failures_remain_visible_without_polluting_comparison() -> None:
    baseline, diagnostic = make_run(), make_run()
    diagnostic.samples[0].passed = False
    diagnostic.samples[0].errors.append("Initial client capture failed")
    html, charts = render([baseline], [diagnostic])
    assert "INITIAL DIAGNOSTICS: 1" in html
    assert "baseline (diagnostic)" in html and "Initial client capture failed" in html
    assert charts["readiness.svg"].count("<circle") == 1
    diagnostic.samples[0].passed = True
    with pytest.raises(ValueError, match="failed attempts"):
        render([baseline], [diagnostic])


def complete_run(condition: str, wait_ms: float, readiness_ms: float) -> Run:
    """Make a complete synthetic campaign solely for report-math regression tests."""
    run = make_run(condition)
    run.info.samples = 10
    run.info.resume_samples = 1
    if condition == "optimized":
        run.info.image = "registry/probe@sha256:" + "b" * 64
    run.samples.clear()
    for trial in range(1, 11):
        phases = ("cold", "warm", "resume") if trial == 1 else ("cold", "warm")
        for phase in phases:
            headers = {PREFIX + "session-start-type": phase}
            if phase != "warm":
                headers[PREFIX + "container-readiness-ms"] = str(int(readiness_ms))
            _, timings, _ = diagnostics(headers)
            measured = wait_ms if phase == "cold" else 500
            run.samples.append(
                Sample(
                    trial=trial,
                    phase=phase,
                    timestamp=run.info.timestamp,
                    session_hash=f"session-{trial}",
                    response_id=f"resp_{condition}_{trial}_{phase}",
                    passed=True,
                    start_type=phase,
                    headers=headers,
                    timings=timings | {"client_complete_ms": measured, "client_journey_ms": measured},
                )
            )
    return run


def test_plain_summary_uses_measured_values_and_a_qualified_conclusion() -> None:
    runs = [
        complete_run("baseline", 6390.49, 4264),
        complete_run("snapshot", 1461.42, 135),
        complete_run("optimized", 1361.44, 123),
    ]
    bullets, conclusion = summary_content(runs)
    assert "77.1% less waiting" in " ".join(bullets)
    assert "6.39 seconds" in " ".join(bullets) and "1.46 seconds" in " ".join(bullets)
    assert "63 of 63 planned cloud checks passed" in " ".join(bullets)
    assert "main startup improvement" in conclusion
    markdown = summary_markdown(runs)
    assert "no AI model" in markdown and "local files do not survive idle/resume" in markdown
    html, charts = render(runs)
    assert "4.93 s" in html and "<h2>Summary</h2>" in html and "<h2>Conclusion</h2>" in html
    assert "Summary in plain English" not in html
    assert html.index('id="test-approach"') < html.index("1. Time to the first response")
    assert "<h2>What we tested</h2>" in html
    assert "<h2>How did we measure response times?</h2>" in html
    assert html.index('id="test-approach"') < html.index('id="response-time-measurement"')
    assert html.index('id="response-time-measurement"') < html.index('id="comparison-graphs"')
    assert "before local parsing and checks" in html
    assert "the two measurements overlap" in html
    assert "not as a randomized trial" in html
    assert "not AI-model time to the first token" in html
    assert "Architecture." in html and "SDK." in html
    assert "<code>2.3.0</code>" in html and "<code>2.4.0</code>" in html
    assert "enableMemorySnapshot" in html and "PYTHONNOUSERSITE=1" in html
    assert "SDK versions and compute stayed the same" in html
    assert "<details" in html and "Technical appendix" in html
    assert "first-response-comparison.svg" in charts


def test_incomplete_or_slower_results_do_not_get_an_optimistic_conclusion() -> None:
    _, conclusion = summary_content([make_run()])
    assert "incomplete" in conclusion and "main startup improvement" not in conclusion
    runs = [
        complete_run("baseline", 100, 100),
        complete_run("snapshot", 150, 100),
        complete_run("optimized", 150, 100),
    ]
    bullets, conclusion = summary_content(runs)
    assert "50.0% more waiting" in " ".join(bullets)
    assert "did not demonstrate" in conclusion


def test_simple_comparison_bars_are_scaled_from_zero_and_missing_is_not_zero() -> None:
    baseline, snapshot = make_run(), make_run("snapshot")
    baseline.samples[0].timings["client_journey_ms"] = 6000
    snapshot.samples[0].timings["client_journey_ms"] = 1500
    html, svg = simple_comparison("First response <test>", [baseline, snapshot], "client_journey_ms")
    assert "6.00 s" in html and "1.50 s" in html
    assert "width:100.000%" in html and "width:25.000%" in html
    root = ElementTree.fromstring(svg)
    bars = [
        node
        for node in root.findall(".//{http://www.w3.org/2000/svg}rect")
        if node.attrib.get("class") == "comparison-bar"
    ]
    assert [float(node.attrib["width"]) for node in bars] == [700, 175]
    assert "&lt;test&gt;" in svg and "<test>" not in svg
    snapshot.samples[0].timings["client_journey_ms"] = None
    html, svg = simple_comparison("Missing data", [snapshot], "client_journey_ms")
    assert "Not measured" in html and 'class="comparison-bar"' not in svg
