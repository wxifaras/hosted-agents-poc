"""Task-plan SVGs preserve recorded assertions without inferring execution."""

import json
import re
import socket
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from scripts import report_task_flows as flows
from scripts.generate_report import generate_report
from scripts.story_report import generate_bundle


def test_recorded_badges_are_exact_links_not_inferred_actions() -> None:
    checks = [
        {"name": "Agents reused - before", "status": "PASS", "ordinal": 1},
        {"name": "Synchronous delegation", "status": "PASS", "ordinal": 2},
        {"name": "Background (async) delegation", "status": "BLOCKED", "ordinal": 3},
        {"name": "Synchronous delegation maybe", "status": "PASS", "ordinal": 4},
    ]
    data = flows.build_task_flow({"scenario": "story-4-step-2"}, checks, prefix="background")
    assert data["available"] is True
    nodes = data["nodes"]
    assert isinstance(nodes, list)
    success = next(node for node in nodes if isinstance(node, dict) and node["key"] == "success")
    assert success["evidence_status"] == "MIXED"
    assert success["counts"] == {"PASS": 1, "BLOCKED": 1}
    assert [item["anchor"] for item in success["references"]] == ["check-background-2", "check-background-3"]
    failure = next(node for node in nodes if isinstance(node, dict) and node["key"] == "failure")
    assert failure["evidence_status"] == "NOT RECORDED" and failure["references"] == []
    assert "NOT AN EXECUTION TRACE" in data["svg"]
    assert "MIXED" in data["svg"]
    assert "Synchronous delegation maybe" not in data["svg"]


@pytest.mark.parametrize("status", ["FAIL", "BLOCKED", "SKIP"])
def test_negative_recorded_status_is_not_changed_to_pass(status: str) -> None:
    data = flows.build_task_flow(
        {"scenario": "story-4-step-7"},
        [{"name": "Direct response from Research", "status": status}],
        prefix="direct",
    )
    node = next(node for node in data["nodes"] if node["key"] == "first")
    assert node["evidence_status"] == status
    assert node["counts"] == {status: 1}


@pytest.mark.parametrize("number", [1, 2, 3, 4, 5, 7])
def test_reviewed_story4_tasks_use_one_accessible_renderer(number: int) -> None:
    result = flows.build_task_flow({"scenario": f"story-4-step-{number}"}, [], prefix=f"run-{number}")
    assert result["available"]
    root = ET.fromstring(result["svg"])
    assert root.attrib["role"] == "img" and root.attrib["aria-labelledby"]
    assert root.find("{http://www.w3.org/2000/svg}title") is not None
    assert root.find("{http://www.w3.org/2000/svg}desc") is not None
    assert all(node["evidence_status"] == "NOT RECORDED" for node in result["nodes"])
    assert "COMPLETE" not in result["svg"]


@pytest.mark.parametrize("number", [1, 2, 3, 4, 5])
def test_story3_tasks_preserve_blockers_without_claiming_unrecorded_actions(number: int) -> None:
    data = flows.build_task_flow(
        {"scenario": f"story-3-step-{number}"},
        [{"name": "Prerequisite", "status": "BLOCKED", "ordinal": 1}],
        prefix=f"story3-{number}",
    )
    assert data["definition"] == f"story-3-step-{number}"
    root = ET.fromstring(data["svg"])
    assert root.attrib["role"] == "img" and root.attrib["aria-labelledby"]
    for node in data["nodes"]:
        assert node["evidence_status"] == ("BLOCKED" if node["key"] == "prerequisite" else "NOT RECORDED")


@pytest.mark.parametrize(
    "number,name,node",
    [
        (1, "Mapping re-read from the state store", "mapping"),
        (2, "Prior history stored in the sub-agent conversation", "followup"),
        (3, "Client lists the Supervisor-created sub-agent conversation", "reads"),
        (4, "B invoked with A's conversation ID (call-context identity)", "shared"),
        (5, "Variant shared / placeholder / call-context", "shared"),
    ],
)
def test_story3_selectors_link_exact_recorded_names(number: int, name: str, node: str) -> None:
    data = flows.build_task_flow(
        {"scenario": f"story-3-step-{number}"},
        [
            {"name": name, "status": "FAIL", "ordinal": 1},
            {"name": name + " unrecorded suffix", "status": "PASS", "ordinal": 2},
        ],
        prefix="conversation",
    )
    matched = next(item for item in data["nodes"] if item["key"] == node)
    assert matched["counts"] == {"FAIL": 1}
    assert [ref["anchor"] for ref in matched["references"]] == ["check-conversation-1"]


@pytest.mark.parametrize(
    "provenance",
    [
        {"scenario": "story-42-step-1"},
        {"scenario": "consumer-native-ci"},
        {"scenario": "story-4-step-6"},
        {"scenario": "story-4-step-1", "variant": "future-contract"},
        {"scenario": "story-1-step-2", "variant": "native-future"},
        {"scenario": "story-1-step-3"},
        {"scenario": "story-3-step-6"},
        {"scenario": "story-3-step-1", "variant": "future-contract"},
        {},
    ],
)
def test_unreviewed_tasks_and_variants_do_not_get_an_inferred_graph(provenance: dict[str, object]) -> None:
    result = flows.build_task_flow(provenance, [], prefix="unknown")
    assert result["available"] is False
    assert "svg" not in result and "No execution flow is inferred" in result["notice"]


def test_native_and_offline_review_variants_are_not_confused() -> None:
    before = flows.build_task_flow(
        {"scenario": "story-1-step-2", "variant": "native"},
        [{"name": "Retained analytical originals", "status": "PASS"}],
        prefix="review",
    )
    assert before["definition"] == "story-1-step-2-review"
    assert "native interpreter" not in before["svg"].lower()
    native = flows.build_task_flow(
        {"scenario": "story-1-step-3", "variant": "native"},
        [
            {"name": "1,000,000 rows: analytical originals", "status": "PASS"},
            {"name": "1,000,000 rows: interpreter memory", "status": "PASS"},
            {"name": "Memory-pressure target", "status": "BLOCKED"},
        ],
        prefix="pressure",
    )
    assert native["definition"] == "story-1-step-3-native"
    target = next(node for node in native["nodes"] if node["key"] == "target")
    assert target["evidence_status"] == "BLOCKED"
    assert "75%" in " ".join(native["notes"])
    hosted = flows.build_task_flow({"scenario": "story-1-step-5", "variant": "native-hosted-only"}, [], prefix="hosted")
    blob = flows.build_task_flow(
        {"scenario": "story-1-step-5", "variant": "native"},
        [{"name": "Blob-backed reconstruction", "status": "PASS"}],
        prefix="blob",
    )
    assert hosted["definition"] == "story-1-step-5-hosted"
    assert blob["definition"] == "story-1-step-5-blob"
    assert "zero native analyses" in " ".join(hosted["notes"])
    supervisor = flows.build_task_flow(
        {"scenario": "story-4-step-6", "variant": "native"},
        [{"name": "Optional native Supervisor integration", "status": "BLOCKED"}],
        prefix="supervisor",
    )
    assert supervisor["definition"] == "story-4-step-6-native"
    assert "not an implemented" in " ".join(supervisor["notes"])


def test_svg_escapes_text_and_uses_unique_local_ids() -> None:
    attack = '<script>alert("bad")</script>'
    flow = flows.TaskFlow(
        "test",
        attack,
        (flows.FlowStep("first", attack, "Actor & boundary"), flows.FlowStep("second", "Finish", "Runner")),
        ("source",),
        edges=(flows.FlowEdge("first", "second", "<unsafe> & label"),),
    )
    first = flows.render_task_svg(flow, {"first": attack}, prefix="first-run")
    second = flows.render_task_svg(flow, {}, prefix="second-run")
    root = ET.fromstring(first)
    assert not root.findall(".//{http://www.w3.org/2000/svg}script")
    assert "&lt;script&gt;" in first and "&amp; boundary" in first
    ids1 = set(re.findall(r'\bid="([^"]+)"', first))
    ids2 = set(re.findall(r'\bid="([^"]+)"', second))
    assert ids1.isdisjoint(ids2)
    assert not re.search(r'(?:href|src)="https?://', first)


@pytest.mark.parametrize(
    "flow,prefix",
    [
        (flows.TaskFlow("empty", "Empty", (), ()), "run"),
        (flows.STORY4[1], 'bad" onload="alert(1)'),
        (
            flows.TaskFlow(
                "duplicate", "Duplicate", (flows.FlowStep("a", "A", "A"), flows.FlowStep("a", "B", "B")), ()
            ),
            "run",
        ),
        (
            flows.TaskFlow(
                "edge", "Edge", (flows.FlowStep("a", "A", "A"),), (), edges=(flows.FlowEdge("a", "missing"),)
            ),
            "run",
        ),
    ],
)
def test_malformed_graphs_and_unsafe_ids_are_rejected(flow: flows.TaskFlow, prefix: str) -> None:
    with pytest.raises(ValueError):
        flows.render_task_svg(flow, {}, prefix=prefix)


@pytest.mark.parametrize("status", ["UNKNOWN", {}, None])
def test_invalid_status_is_not_hidden(status: object) -> None:
    with pytest.raises(ValueError, match="supported recorded status"):
        flows.build_task_flow(
            {"scenario": "story-4-step-7"},
            [{"name": "Direct response from Research", "status": status}],
            prefix="direct",
        )


def save_run(tmp_path: Path, scenario: str = "story-4-step-7") -> Path:
    run = tmp_path / "source"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"run_id": "unit-run", "scenario": scenario}))
    records = [{"run_id": "unit-run", "step": "Direct response from Research", "status": "PASS", "observed": {}}]
    data = "".join(json.dumps(row) + "\n" for row in records)
    for name in ("results.jsonl", "events.jsonl"):
        (run / name).write_text(data)
    return run


def test_bundle_and_single_run_share_the_flow_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("No network"))
    run = save_run(tmp_path)
    config = tmp_path / "input.json"
    config.write_text(
        json.dumps(
            {
                "schema": "story-report-bundle-v1",
                "story": 4,
                "runs": [{"key": "direct", "label": "Direct", "run_dir": str(run), "scope": "Unit evidence"}],
                "decisions": [],
                "traceability": [],
            }
        )
    )
    report = generate_bundle(config, tmp_path / "artifacts" / "report")
    html = report.read_text()
    assert "What this task validates" in html and "CODE-DEFINED PLAN" in html
    assert 'href="#check-direct-1"' in html and 'id="check-direct-1"' in html
    assert "NOT RECORDED" in html and "does not produce a task-level acceptance verdict" in html
    data = json.loads((report.parent / "sanitized-evidence.json").read_text())
    assert data["runs"][0]["counts"] == {"PASS": 1}
    assert data["runs"][0]["task_flow"]["available"] is True
    assert generate_bundle(config, report.parent, overwrite=True) == report
    single = generate_report(run, title="Story 4 single", archive=False)
    single_html = single.read_text()
    assert "What this task validates" in single_html and "CODE-DEFINED PLAN" in single_html
    single_data = json.loads((single.parent / "sanitized-evidence.json").read_text())
    reference = single_data["task_flow"]["nodes"][2]["references"][0]["anchor"]
    assert f'href="#{reference}"' in single_html and f'id="{reference}"' in single_html


def test_invalid_run_has_no_task_success_diagram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "input.json"
    config.write_text(
        json.dumps(
            {
                "schema": "story-report-bundle-v1",
                "story": 4,
                "runs": [{"key": "missing", "label": "Missing", "run_dir": "missing", "scope": "Unavailable"}],
                "decisions": [],
                "traceability": [],
            }
        )
    )
    report = generate_bundle(config, tmp_path / "artifacts" / "report")
    data = json.loads((report.parent / "sanitized-evidence.json").read_text())
    assert data["errors"] == ["missing"]
    assert data["runs"][0]["task_flow"]["available"] is False
    assert "CODE-DEFINED PLAN" not in report.read_text()
