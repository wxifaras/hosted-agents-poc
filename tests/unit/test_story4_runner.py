"""Story 4 runner logic; Azure CLI, agents, and model calls are replaced by in-memory fakes."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from app.services.orchestration import (
    DelegationReport,
    Failure,
    HopContext,
    Invocation,
    Routing,
    SubTaskReport,
    ToolCall,
    fingerprint,
)

from scripts.common.reporting import MarkdownReport, ValidationRun
from scripts.run_scenario import story4_agent_patterns as patterns
from scripts.run_scenario import story4_common as common
from scripts.run_scenario import story4_identity as identity
from scripts.run_scenario.story1_data_analysis import AgentSnapshot, Blocked

CORRELATION = uuid4()
SALT = str(CORRELATION)


def hop(agent: str, user: str | None = None) -> HopContext:
    return HopContext(
        agent=agent,
        hosted_runtime=True,
        call_id_present=True,
        user_id_present=user is not None,
        session_id_present=True,
        user_fingerprint=fingerprint(user, SALT),
        correlation_header_matches=True,
    )


def delegation(
    *,
    selected: str | None = "research",
    status: str = "completed",
    answer: str = "answer TOKEN",
    final: str = "final TOKEN",
    mode: str = "sync",
    seen: tuple[str, ...] = ("completed",),
    failure: Failure | None = None,
    calls: list[ToolCall] | None = None,
    decided_by: str = "supervisor-model-tool-call",
    tool_calls_made: int = 1,
    sub_user: str | None = None,
) -> DelegationReport:
    sub = (
        SubTaskReport(
            agent=selected,
            correlation_id=CORRELATION,
            context=hop(selected, sub_user),
            answer=answer,
            tool_calls=calls or [],
        )
        if selected and status == "completed"
        else None
    )
    return DelegationReport(
        correlation_id=CORRELATION,
        status=status,
        routing=Routing(requested="auto", selected=selected, decided_by=decided_by, model_tool_calls=tool_calls_made),
        supervisor=hop("supervisor", "operator-oid"),
        invocation=Invocation(
            target_agent=f"{(selected or 'research').replace('_', '-')}-agent",
            mode=mode,
            identity="call-context",
            headers_sent=["x-agent-foundry-call-id"],
            status=seen[-1],
            statuses_seen=list(seen),
        ),
        sub_agent=sub,
        failure=failure,
        final_answer=final,
    )


def test_routing_case_checks_require_route_delivery_and_token() -> None:
    checks = patterns.routing_case_checks("research", "TOKEN", delegation())
    assert all(ok for ok, _ in checks.values())
    wrong = patterns.routing_case_checks("data_analysis", "TOKEN", delegation())
    assert not wrong["Routing decision"][0] and not wrong["Sub-agent -> Supervisor delivery"][0]
    no_sum = patterns.routing_case_checks("data_analysis", "TOKEN", delegation(selected="data_analysis"))
    assert not no_sum["Sub-agent -> Supervisor delivery"][0]
    with_sum = patterns.routing_case_checks(
        "data_analysis", "TOKEN", delegation(selected="data_analysis", answer="2870 TOKEN")
    )
    assert all(ok for ok, _ in with_sum.values())
    assert not patterns.routing_case_checks("research", "TOKEN", delegation(final="lost"))[
        "Supervisor -> user delivery"
    ][0]
    assert not patterns.routing_case_checks("research", "TOKEN", delegation(tool_calls_made=2))["Routing decision"][0]
    assert patterns.routing_case_checks("research", "TOKEN", None) == {}


def test_flow_checks_cover_sync_background_and_failures() -> None:
    assert patterns.flow_check(delegation(), "TOKEN", False, "sync")[0]
    assert not patterns.flow_check(delegation(), "TOKEN", False, "background")[0]
    background = delegation(mode="background", seen=("queued", "in_progress", "completed"))
    assert patterns.flow_check(background, "TOKEN", False, "background")[0]
    simulated = Failure(stage="status", code="server_error", message=patterns.SIMULATED_FAILURE)
    failed = delegation(status="sub_agent_failed", seen=("failed",), failure=simulated)
    assert patterns.flow_check(failed, "TOKEN", True, "sync")[0]
    assert not patterns.flow_check(failed, "TOKEN", False, "sync")[0]
    assert not patterns.flow_check(None, "TOKEN", False, "sync")[0]


def test_matching_research_toolbox_and_capability_probe() -> None:
    metadata = patterns.research_toolbox_metadata()
    versions = [
        {"version": "1", "metadata": metadata},
        {"version": "3", "metadata": {**metadata, "allowed_tools": "*"}},
        {"version": "2", "metadata": metadata},
    ]
    ref = patterns.matching_research_toolbox(versions, "https://p/api/projects/x")
    assert (
        ref is not None
        and ref.version == "2"
        and ref.endpoint.endswith("/toolboxes/story4-research-mcp/versions/2/mcp?api-version=v1")
    )
    assert patterns.matching_research_toolbox([{"version": "1", "metadata": {}}], "https://p") is None
    assert patterns.capability_from(common.Exchange("x", "a", status="failed", error=patterns.SIMULATED_FAILURE))
    assert not patterns.capability_from(common.Exchange("x", "a", status="completed", text="chat answer"))


def test_identity_classification_uses_only_salted_fingerprints() -> None:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"oid": "operator-oid", "tid": "tenant"}).encode()).decode().rstrip("=")
    )
    claims = common.token_claims(f"header.{payload}.signature")
    assert claims == {"oid": "operator-oid", "tid": "tenant"}
    assert common.token_claims("not-a-jwt") == {}
    candidates = common.identity_candidates(claims, {"instance_identity": {"principal_id": "sup-mi", "client_id": "c"}})
    assert "tenant:operator-oid" in candidates["operator (original user)"]
    assert candidates["Supervisor managed identity"] == ["sup-mi", "c"]
    assert common.classify_user(None, SALT, candidates).startswith("absent")
    assert common.classify_user(fingerprint("operator-oid", SALT), SALT, candidates) == "operator (original user)"
    assert common.classify_user(fingerprint("sup-mi", SALT), SALT, candidates) == "Supervisor managed identity"
    assert common.classify_user(fingerprint("x", SALT), SALT, candidates, {"same": fingerprint("x", SALT)}) == "same"
    assert common.classify_user(fingerprint("x", SALT), SALT, candidates) == "present, unrecognized principal"
    report = delegation(sub_user="operator-oid")
    assert identity.research_principal(report, CORRELATION, candidates) == "original user (same as Supervisor hop)"
    assert identity.research_principal(delegation(sub_user=None), CORRELATION, candidates).startswith("absent")


def test_impersonation_permission_needs_the_exact_action(monkeypatch: pytest.MonkeyPatch) -> None:
    context = {"AZURE_AI_PROJECT_ID": "/p", "AZURE_SUBSCRIPTION_ID": "s"}
    action = "Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action"
    roles = {
        "wildcard": {"roleName": "Wildcard", "permissions": [{"dataActions": ["Microsoft.CognitiveServices/*"]}]},
        "custom": {"roleName": "POC Supervisor Delegation", "permissions": [{"dataActions": [action.lower()]}]},
        "excluded": {
            "roleName": "Excluded",
            "permissions": [{"dataActions": [action], "notDataActions": ["*/UserIdentityImpersonation/action"]}],
        },
        "reader": {"roleName": "Reader", "permissions": [{"dataActions": []}]},
    }
    assigned: list[str] = []

    def fake_az(arguments: list[str]) -> Any:
        if arguments[:2] == ["role", "assignment"]:
            return [
                {"roleDefinitionId": f"/defs/{name}", "roleDefinitionName": roles[name]["roleName"]}
                for name in assigned
            ]
        return [roles[arguments[arguments.index("--name") + 1]]]

    monkeypatch.setattr(common, "az", fake_az)
    assigned[:] = ["reader", "excluded", "wildcard"]
    assert common.impersonation_granted(context, "sup") == (False, [])
    assigned[:] = ["reader", "custom"]
    assert common.impersonation_granted(context, "sup") == (True, ["POC Supervisor Delegation"])
    assert common.role_names(context, "sup") == ["POC Supervisor Delegation", "Reader"]


@pytest.mark.parametrize("number", range(1, 8))
def test_every_step_summary_renders_with_verified_documentation(tmp_path: Path, number: int) -> None:
    step_class = patterns.step_classes()[number]
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    report = MarkdownReport(run.path / "r.md", step_class.title)
    args = SimpleNamespace(environment="e", timeout=1, output_dir=tmp_path, mode="verify")
    step = step_class(args, run, report)
    step.record("Prerequisite", "BLOCKED", "ready", "missing")
    step.conclude()
    text = (run.path / "r.md").read_text(encoding="utf-8")
    assert "**Acceptance verdict: Not evaluated.**" in text
    assert "learn.microsoft.com" in text
    assert step.task_label.startswith("Agents: Story 4 - ") and step.ac
    assert (step_class.spec.story, step_class.spec.number, step_class.number) == (4, number, number)


def test_outcome_and_tool_call_classification() -> None:
    assert identity.outcome(delegation()) == "allowed"
    denied = Failure(stage="request", http_status=403, code="PermissionDenied", message="no")
    assert (
        identity.outcome(delegation(status="sub_agent_failed", failure=denied)) == "denied (HTTP 403, PermissionDenied)"
    )
    assert identity.outcome(None) == "no report"
    calls = [ToolCall(name="microsoft_learn.microsoft_docs_search", succeeded=True)]
    assert identity.tool_call_ok(delegation(calls=calls))[0]
    assert not identity.tool_call_ok(delegation(calls=[ToolCall(name="microsoft_docs_search", succeeded=False)]))[0]


def test_story1_evidence_takes_the_latest_entry_per_step(tmp_path: Path) -> None:
    index = tmp_path / "index.md"
    index.write_text(
        "# Story 1 evidence index\n\n"
        "- 2026-10-24 10:00 UTC - Step 1 (verify) - **Not met** - [a](a/01.md)\n"
        "- 2026-10-24 11:00 UTC - Step 1 (verify) - **Met** - [b](b/01.md)\n"
        "- 2026-10-24 12:00 UTC - Step 2 (analysis and outputs) - **Met** - [c](c/02.md)\n",
        encoding="utf-8",
    )
    evidence = identity.story1_evidence(index)
    assert evidence["Step 1"]["verdict"] == "Met" and evidence["Step 1"]["name"] == "b"
    assert evidence["Step 2"]["link"] == "c/02.md"
    assert identity.story1_evidence(tmp_path / "missing.md") == {}


def test_baseline_round_trip_and_environment_guard(tmp_path: Path) -> None:
    snapshot = AgentSnapshot("research-agent", "2", "active", "p", True, "https://x/toolboxes/t/versions/1/mcp")
    path = tmp_path / common.BASELINE_FILE
    common.write_baseline(path, "env-a", "run", {"research-agent": snapshot}, "https://endpoint")
    assert common.load_baseline(path, "env-a") == {"research-agent": snapshot}
    assert identity.research_toolbox_endpoint(tmp_path) == "https://endpoint"
    with pytest.raises(Blocked, match="env-a"):
        common.load_baseline(path, "env-b")
    with pytest.raises(Blocked, match="missing"):
        common.load_baseline(tmp_path / "nope.json", "env-a")


def test_cli_rejects_mode_outside_step1_and_blocked_runs_are_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit):
        patterns.main(["--step", "2", "--mode", "prepare-if-missing"])
    monkeypatch.setattr(common, "read_environment", MagicMock(side_effect=Blocked("no azd env")))
    code = patterns.main(["--step", "7", "--environment", "env-a", "--output-dir", str(tmp_path), "--no-color"])
    assert code == 2
    index = (tmp_path / "story-4" / "index.md").read_text(encoding="utf-8")
    assert index.startswith("# Story 4 evidence index")
    (directory,) = (tmp_path / "story-4" / "step-07-direct-invocation").iterdir()
    link = f"step-07-direct-invocation/{directory.name}"
    assert f"Step 7 (direct invocation) - **Not evaluated** - [{link}]({link}/report.md)" in index
    assert (directory / "report.md").is_file()


def test_step7_verdict_and_supervisor_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    report = MarkdownReport(run.path / "r.md", "t")
    step = patterns.Step7(SimpleNamespace(environment="e", timeout=1, output_dir=tmp_path), run, report)
    monkeypatch.setattr(step, "conversation", lambda agent: f"conv_{agent}")
    replies = iter(["STORED", "the code is S4B-XYZ"])

    def fake_call(label: str, agent: str, text: str, **_kwargs: Any) -> common.Exchange:
        step.requests.append([len(step.requests) + 1, label, agent, "completed", "resp", 0.1])
        return common.Exchange(label, agent, status="completed", text=next(replies))

    monkeypatch.setattr(step, "hosted_call", fake_call)
    monkeypatch.setattr(patterns, "marker", lambda _prefix: "S4B-XYZ")
    step.body()
    assert step.compute_verdict() == "Met"
    assert step.statuses["Supervisor (Agent A) not invoked"] == "PASS"
