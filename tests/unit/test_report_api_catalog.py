"""API reference links explain implementation without manufacturing recorded execution."""

import json
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from zipfile import ZipFile

from scripts.check_details import describe_check
from scripts.generate_report import generate_report
from scripts.report_api_catalog import api_catalog, related_api_ids


def test_catalog_groups_have_unique_safe_ids_and_no_live_identifiers() -> None:
    catalog = api_catalog()
    assert len({entry["id"] for entry in catalog}) == len(catalog)
    assert {entry["group"] for entry in catalog} == {
        "Consumer calls",
        "Backend calls",
        "Privileged-control calls",
        "Consumer native bypass attempts",
    }
    for entry in catalog:
        assert entry["sdk"] and entry["explanation"] and entry["source"]
        assert entry["method"] in {"GET", "POST", "PUT", "Local construction"}
        assert "\n" not in entry["sdk"]
        assert "```" not in entry["explanation"]
        assert "role assignment" not in entry["sdk"] and "azd" not in entry["sdk"]


def test_download_references_sdk_method_and_correct_api_not_native_model() -> None:
    name, key = "user2: download user1's result.json denied", "user2:foreign-json"
    ids = related_api_ids(name, key, describe_check(name, key))
    assert ids == ["consumer-download"]
    api = next(entry for entry in api_catalog() if entry["id"] == ids[0])
    assert api["sdk"] == "actor.project.agents.download_session_file(...)"
    assert api["method"] == "GET" and "/sessions/{session}/files/content?path=" in api["route"]
    assert "No prompt is sent" in api["explanation"]


def test_native_bypass_and_privileged_controls_use_distinct_catalog_entries() -> None:
    name, key = "user1: native output-content from user2", "user1:native-output-content:chart.png"
    ids = related_api_ids(name, key, describe_check(name, key))
    assert ids == ["bypass-output-content", "control-output-content"]
    entries = {entry["id"]: entry for entry in api_catalog()}
    assert entries[ids[0]]["group"] == "Consumer native bypass attempts"
    assert entries[ids[1]]["group"] == "Privileged-control calls"
    assert entries[ids[0]]["sdk"] == entries[ids[1]]["sdk"]
    assert "Consumer-only" in entries[ids[0]]["caller"] and "privileged-control" in entries[ids[1]]["caller"]


def test_local_values_reference_upstream_apis_but_metadata_omits_administration() -> None:
    assert related_api_ids("user1: analysis values", "user1:values", describe_check("user1: analysis values")) == [
        "backend-analysis",
        "backend-output",
        "consumer-download",
    ]
    assert (
        related_api_ids(
            "Consumer deployment and permissions", "deployment", describe_check("Consumer deployment and permissions")
        )
        == []
    )
    assert related_api_ids("unknown general validator", None, describe_check("unknown general validator")) == []


def test_analyze_references_outer_command_and_backend_calls_separately() -> None:
    ids = related_api_ids("user1: analyze", None, describe_check("user1: analyze"))
    assert ids == ["consumer-command", "backend-client", "backend-upload", "backend-analysis", "backend-output"]
    assert related_api_ids("user1: inspect", None, describe_check("user1: inspect")) == ["consumer-command"]


class Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        identifier, href = attributes.get("id"), attributes.get("href")
        if identifier:
            self.ids.add(identifier)
        if tag == "a" and href and href.startswith("#"):
            self.links.append(href[1:])


def test_generated_catalog_and_check_links_are_valid_and_reference_only(tmp_path: Path) -> None:
    run_id = "unit-catalog"
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "scenario": "consumer-native-ci",
                "started_at": "2026-10-26T12:00:00Z",
            }
        )
    )
    record = {
        "run_id": run_id,
        "step": "user2: download user1's result.json denied",
        "status": "PASS",
        "observed": {"denied": True},
    }
    for name in ("results.jsonl", "events.jsonl"):
        (tmp_path / name).write_text(json.dumps(record) + "\n")
    report = generate_report(tmp_path)
    html = report.read_text(encoding="utf-8")
    parsed = Anchors()
    parsed.feed(html)
    assert set(parsed.links).issubset(parsed.ids)
    assert 'href="#api-catalog"' in html
    checks = html.split('<section id="checks">', 1)[1].split('<section id="timeline">', 1)[0]
    assert 'href="#api-consumer-download"' in checks
    assert "actor.project.agents.download_session_file(...)" in checks
    assert "Implementation reference, not recorded execution." in html
    assert "does not mean a" in html and "request ran or passed" in html
    assert "not a captured request" in checks
    assert "Actual HTTP" not in unescape(checks) or "not reconstructed" in checks
    assert "<script" not in html and "<pre><code" not in html
    bundle = json.loads((report.parent / "sanitized-evidence.json").read_text())
    assert bundle["consumer_acceptance"] == "INCOMPLETE"
    with ZipFile(report.parent / "report.zip") as archive:
        catalog = json.loads(archive.read("api-catalog.json"))
        assert len(catalog) == len(api_catalog())
        assert "index.html" in archive.namelist()
