"""Story6 plan diagrams map only exact saved assertions, not inferred trace steps."""

import xml.etree.ElementTree as ET

import pytest

from scripts.report_task_flows import build_task_flow


def check(name: str, status: str = "PASS", ordinal: int = 1) -> dict[str, object]:
    return {"name": name, "status": status, "ordinal": ordinal}


def test_direct_tool_flow_keeps_enforcement_failure_and_permission_pass_separate() -> None:
    data = build_task_flow(
        {"scenario": "story-6-step-5", "mode": "authenticated-direct-mcp"},
        [
            check("Approval-required proxy definition"),
            check("Runtime MCP approval metadata", ordinal=2),
            check("Authorized direct tool control", ordinal=3),
            check("Direct valid-token call without approval", "FAIL", 4),
            check("Invoke-only user direct toolbox permission", ordinal=5),
        ],
        prefix="proxy",
    )
    assert data["available"] is True
    nodes = {node["key"]: node for node in data["nodes"]}
    assert nodes["direct"]["evidence_status"] == "FAIL"
    assert nodes["permission"]["evidence_status"] == "PASS"
    assert nodes["direct"]["references"][0]["anchor"] == "check-proxy-4"
    svg = ET.fromstring(data["svg"])
    assert svg.tag.endswith("svg") and svg.attrib["role"] == "img"
    assert "not an execution trace" in data["notice"]


def test_missing_checks_stay_not_recorded_and_near_matches_are_not_used() -> None:
    data = build_task_flow(
        {"scenario": "story-6-step-2", "mode": "hosted"},
        [check("Coordinator command (not the exact assertion)")],
        prefix="pending",
    )
    assert all(node["evidence_status"] == "NOT RECORDED" for node in data["nodes"])
    assert all(node["references"] == [] for node in data["nodes"])


def test_audit_and_owner_continuation_use_their_own_reviewed_flows() -> None:
    audit = build_task_flow(
        {"scenario": "story-6-step-4", "mode": "hosted"},
        [check("Conversation history before command")],
        prefix="history",
    )
    owner = build_task_flow(
        {"scenario": "story-6-step-6", "mode": "hosted-two-users"},
        [check("User A owner continuation")],
        prefix="owner",
    )
    assert audit["definition"] == "story-6-history-audit"
    assert owner["definition"] == "story-6-owner-continuation"


def test_custom_entra_flow_links_actual_pause_decision_and_receipt_checks() -> None:
    flow = build_task_flow(
        {"scenario": "story-6-step-2", "mode": "hosted"},
        [
            check("Entra MCP approval pause"),
            check("Coordinator command", ordinal=2),
            check("Entra MCP execution evidence", ordinal=3),
        ],
        prefix="entra",
    )
    assert flow["definition"] == "story-6-entra-mcp"
    assert [node["key"] for node in flow["nodes"]] == ["pending", "decision", "receipts"]
    assert all(node["evidence_status"] == "PASS" for node in flow["nodes"])


@pytest.mark.parametrize("mode", ["offline-sdk-qualification", "blocked-before-hosted-execution", "unknown"])
def test_local_or_unknown_modes_do_not_acquire_a_hosted_execution_flow(mode: str) -> None:
    data = build_task_flow(
        {"scenario": "story-6-step-2", "mode": mode},
        [check("Coordinator command")],
        prefix="not-hosted",
    )
    assert data["available"] is False
