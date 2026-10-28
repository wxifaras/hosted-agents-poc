"""Shared offline story selection, truthful scope and compatibility contracts."""

import json
import socket
from pathlib import Path

import pytest

from scripts import story_report
from scripts.generate_report import ReportError


def saved_bundle(tmp_path: Path, story: object = 4) -> tuple[Path, Path, Path]:
    """Create explicitly bound synthetic evidence, never discover unrelated runs."""
    run = tmp_path / "source"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({"run_id": "recorded-run", "scenario": "story-4-step-1"}))
    records = [
        {
            "run_id": "recorded-run",
            "step": "Agents reused - before",
            "status": "PASS",
            "observed": {"supervisor-agent": "14", "research-agent": "9", "data-analysis-agent": "9"},
        },
        {
            "run_id": "recorded-run",
            "step": "Agents reused - after",
            "status": "PASS",
            "observed": {"supervisor-agent": "15", "research-agent": "10", "data-analysis-agent": "10"},
        },
    ]
    for filename in ("results.jsonl", "events.jsonl"):
        (run / filename).write_text("".join(json.dumps(record) + "\n" for record in records))
    config = {
        "schema": "story-report-bundle-v1",
        "story": story,
        "runs": [{"key": "routing", "label": "Routing", "run_dir": str(run), "scope": "Version transition"}],
        "decisions": [],
        "traceability": [
            {
                "requirement": "Delegation",
                "status": "Scoped",
                "summary": "Recorded routing, not native execution",
                "limitations": "No original-user proof",
                "next_step": "Native adapter pending",
                "run_keys": ["routing"],
            }
        ],
    }
    source = tmp_path / "config.json"
    source.write_text(json.dumps(config))
    return source, run, tmp_path / "artifacts" / "report"


def test_story4_reference_is_not_native_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("No network"))
    source, _, output = saved_bundle(tmp_path)
    assert story_report.main(["--input", str(source), "--output", str(output)]) == 0
    html = (output / "index.html").read_text()
    data = json.loads((output / "sanitized-evidence.json").read_text())
    assert data["story"] == 4
    assert data["runs"][0]["counts"] == {"PASS": 2}
    assert data["runs"][0]["additional_event_count"] == 0
    assert data["runs"][0]["completion"] == "Recorded checks, no finish-event recorded."
    assert data["runs"][0]["checks"][0]["observed"]["data-analysis-agent"] == "9"
    assert data["runs"][0]["checks"][1]["observed"]["data-analysis-agent"] == "10"
    assert data["traceability"][0]["limitations"] == "No original-user proof"
    for expected in ("Existing Supervisor", "Existing Research", "text-only", "adapter pending", "reference context"):
        assert expected in html
    for forbidden in ('id="memory"', 'id="lifecycle"', "POC_STORY1_NATIVE", "poc-consumer-ci", "75%"):
        assert forbidden not in html
    assert data["memory"] == data["lifecycle_timeline"] == []
    assert len(data["native_api_catalog"]) == 6
    assert story_report.main(["--input", str(source), "--output", str(output), "--overwrite"]) == 0


def test_future_story_is_neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    source, run, output = saved_bundle(tmp_path, 42)
    (run / "manifest.json").write_text(json.dumps({"run_id": "recorded-run", "scenario": "story-42-step-1"}))
    story_report.generate_bundle(source, output)
    html = (output / "index.html").read_text()
    data = json.loads((output / "sanitized-evidence.json").read_text())
    assert data["native_api_catalog"] == data["memory"] == data["lifecycle_timeline"] == []
    assert "no reviewed story-specific architecture" in html
    for forbidden in ('id="architecture"', 'id="native-api"', 'id="memory"', "75%", "Code Interpreter"):
        assert forbidden not in html


def test_story3_context_preserves_failure_outcomes_and_private_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("No network"))
    source, run, output = saved_bundle(tmp_path, 3)
    (run / "manifest.json").write_text(json.dumps({"run_id": "recorded-run", "scenario": "story-3-step-3"}))
    records = [
        {
            "run_id": "recorded-run",
            "step": "Client lists the Supervisor-created sub-agent conversation",
            "status": "FAIL",
            "observed": {
                "observed": "allowed (2 items)",
                "enforced_by": "Foundry (conversation ownership)",
                "mapping": {
                    "owner": "supervisor-application",
                    "user_isolated": True,
                    "found": True,
                    "key": "conversation-map/PRIVATE_MAPPING",
                },
                "history": {
                    "items": 2,
                    "hop_inputs": 0,
                    "other_assistant": 0,
                    "messages": ["PRIVATE_HISTORY"],
                    "token_hits": [1],
                },
                "supervisor_conversation_id": "conv_PRIVATE_CONVERSATION",
                "answer": "PRIVATE_MODEL_ANSWER",
            },
        },
        {
            "run_id": "recorded-run",
            "step": "Explicit failure outcome",
            "status": "PASS",
            "observed": {
                "outcome": "denied (HTTP 404, not_found)",
                "received": "nothing (sub-agent call did not complete)",
                "failure": {"http_status": 404, "code": "not_found", "message": "PRIVATE_ERROR"},
                "note": "conv_PRIVATE_FREE_TEXT caresp_PRIVATE_RESPONSE resp_PRIVATE_RESPONSE",
            },
        },
    ]
    for filename in ("results.jsonl", "events.jsonl"):
        (run / filename).write_text("".join(json.dumps(record) + "\n" for record in records))
    originals = {path.name: path.read_bytes() for path in run.iterdir()}
    assert story_report.main(["--input", str(source), "--output", str(output)]) == 0
    html = (output / "index.html").read_text()
    data = json.loads((output / "sanitized-evidence.json").read_text())
    assert data["runs"][0]["counts"] == {"FAIL": 1, "PASS": 1}
    check = data["runs"][0]["checks"][0]
    assert check["observed"]["observed"] == "allowed (2 items)"
    assert check["observed"]["mapping"] == {
        "owner": "supervisor-application",
        "user_isolated": True,
        "found": True,
    }
    assert check["observed"]["history"] == {"items": 2, "hop_inputs": 0, "other_assistant": 0}
    assert data["runs"][0]["checks"][1]["observed"]["failure"] == {"http_status": 404, "code": "not_found"}
    assert data["memory"] == data["lifecycle_timeline"] == []
    assert len(data["native_api_catalog"]) == 6
    for expected in (
        "Story 3 conversation architecture",
        "POC_CONVERSE",
        "poc-conversation-v1",
        "allowed (2 items)",
        "HTTP 404",
        "does not mean shared history worked",
        "least-privileged",
        "NOT AN EXECUTION TRACE",
    ):
        assert expected in html
    exports = "\n".join(path.read_text() for path in output.rglob("*") if path.is_file())
    for forbidden in ("PRIVATE_", "POC_STORY1_NATIVE", 'id="memory"', 'id="lifecycle"'):
        assert forbidden not in exports
    assert originals == {path.name: path.read_bytes() for path in run.iterdir()}


@pytest.mark.parametrize("story", [None, "4", True, 0, -1, 4.5])
def test_story_requires_positive_integer(tmp_path: Path, story: object) -> None:
    source, _, output = saved_bundle(tmp_path, story)
    with pytest.raises(ReportError, match="positive integer"):
        story_report.generate_bundle(source, output)


def test_story_mismatch_requires_explicit_cross_story_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source, run, output = saved_bundle(tmp_path)
    (run / "manifest.json").write_text(json.dumps({"run_id": "recorded-run", "scenario": "story-1-step-3"}))
    assert story_report.main(["--input", str(source), "--output", str(output)]) == 2
    data = json.loads((output / "sanitized-evidence.json").read_text())
    assert data["errors"] == ["routing"]
    assert data["runs"][0]["checks"] == []
    config = json.loads(source.read_text())
    config["runs"][0]["kind"] = "cross-story-reference"
    source.write_text(json.dumps(config))
    assert story_report.main(["--input", str(source), "--output", str(output.parent / "explicit")]) == 0


@pytest.mark.parametrize("field", ["chart", "summary_source"])
def test_non_story1_rejects_story1_only_sources(tmp_path: Path, field: str) -> None:
    source, _, output = saved_bundle(tmp_path)
    config = json.loads(source.read_text())
    config[field] = "not-used"
    source.write_text(json.dumps(config))
    with pytest.raises(ReportError, match="Story 1-only"):
        story_report.generate_bundle(source, output)


def test_shared_cli_accepts_legacy_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    source, run, output = saved_bundle(tmp_path)
    config = json.loads(source.read_text())
    config["schema"] = "story1-report-bundle-v1"
    config.pop("story")
    source.write_text(json.dumps(config))
    (run / "manifest.json").write_text(json.dumps({"run_id": "recorded-run", "scenario": "story-1-step-3"}))
    assert story_report.main(["--input", str(source), "--output", str(output)]) == 0
    assert "POC_STORY1_NATIVE" in (output / "index.html").read_text()


def test_story4_nested_privacy_escaping_and_originals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    source, run, output = saved_bundle(tmp_path)
    attack = '<script>alert("user")</script>'
    private = (
        "person@example.com https://private.example/path /home/person/secret.csv "
        "C:\\Users\\person\\secret.csv \\\\server\\share\\secret.csv "
        "thread_private123 " + "a" * 16
    )
    config = json.loads(source.read_text())
    config["title"] = attack + private
    source.write_text(json.dumps(config))
    records = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
    records[0]["expected"] = private
    records[0]["observed"] = {
        "note": [{"note": private, "user_fingerprint": "b" * 16, "answer": "UNREVIEWED_USER_TEXT"}],
        "tool_calls": [{"name": attack, "succeeded": True}],
    }
    (run / "results.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    originals = {path.name: path.read_bytes() for path in run.iterdir()}
    story_report.generate_bundle(source, output)
    exports = "\n".join(path.read_text() for path in output.rglob("*") if path.is_file())
    html = (output / "index.html").read_text()
    assert "<script>" not in html and "&lt;script&gt;" in html
    for forbidden in (
        "person@example.com",
        "private.example",
        "/home/person",
        "C:\\Users\\person",
        "\\\\server\\share",
        "private123",
        "a" * 16,
        "b" * 16,
        "UNREVIEWED_USER_TEXT",
        "user_fingerprint",
    ):
        assert forbidden not in exports
    assert {path.name: path.read_bytes() for path in run.iterdir()} == originals


def test_unhashable_schema_is_explicit_input_error(tmp_path: Path) -> None:
    source, _, output = saved_bundle(tmp_path)
    config = json.loads(source.read_text())
    config["schema"] = ["story-report-bundle-v1"]
    source.write_text(json.dumps(config))
    assert story_report.main(["--input", str(source), "--output", str(output)]) == 2


def test_distinct_opaque_run_ids_keep_distinct_private_aliases(tmp_path: Path) -> None:
    source, _, _ = saved_bundle(tmp_path)
    config = json.loads(source.read_text())
    for ordinal, identifier in enumerate(("a" * 32, "b" * 32)):
        folder = tmp_path / f"source-{ordinal}"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps({"run_id": identifier, "scenario": "story-4-step-1"}))
        record = {"run_id": identifier, "step": "Recorded check", "status": "PASS"}
        (folder / "results.jsonl").write_text(json.dumps(record) + "\n")
        entry = {**config["runs"][0], "key": f"run-{ordinal}", "run_dir": str(folder)}
        if ordinal == 0:
            config["runs"] = [entry]
        else:
            config["runs"].append(entry)
    config["traceability"] = []
    data = story_report.build_bundle(config)
    assert len({entry["run_id"] for entry in data["runs"]}) == 2
    assert "a" * 32 not in json.dumps(data) and "b" * 32 not in json.dumps(data)


@pytest.mark.parametrize(
    ("schema", "kind", "exit_code"),
    [
        ("story1-report-bundle-v1", "integration", 0),
        ("story-report-bundle-v1", "integration", 2),
        ("story-report-bundle-v1", "cross-story-reference", 0),
    ],
)
def test_legacy_story1_cross_story_reference_retains_original_scope_and_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: str, kind: str, exit_code: int
) -> None:
    monkeypatch.chdir(tmp_path)
    source, run, output = saved_bundle(tmp_path)
    config = json.loads(source.read_text())
    config.update(schema=schema, story=1)
    config["runs"][0].update(kind=kind, scope="Story 4 Step 6 Supervisor integration reference")
    source.write_text(json.dumps(config))
    (run / "manifest.json").write_text(json.dumps({"run_id": "recorded-run", "scenario": "story-4-step-6"}))
    record = {"run_id": "recorded-run", "step": "Native integration", "status": "BLOCKED"}
    for name in ("results.jsonl", "events.jsonl"):
        (run / name).write_text(json.dumps(record) + "\n")
    assert story_report.main(["--input", str(source), "--output", str(output)]) == exit_code
    data = json.loads((output / "sanitized-evidence.json").read_text())
    binding = data["runs"][0]
    if exit_code == 0:
        assert data["errors"] == []
        assert binding["counts"] == {"BLOCKED": 1}
        assert binding["kind"] == kind
        assert binding["scope"] == config["runs"][0]["scope"]
        assert binding["provenance"]["scenario"] == "story-4-step-6"
        assert any("Cross-story reference:" in warning for warning in binding["warnings"])
        assert "Cross-story reference:" in (output / "index.html").read_text()
        assert binding["completion"] == "Recorded checks, no finish-event recorded."
    else:
        assert data["errors"] == ["routing"]
        assert binding["counts"] == {}
        assert binding["checks"] == []
        assert "kind cross-story-reference" in binding["error"]
