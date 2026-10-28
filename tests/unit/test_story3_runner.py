"""Story 3 runner logic; agents, conversations, and model calls are replaced by in-memory fakes."""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from app.services.conversations import (
    ConversationReport,
    HistoryStats,
    Listing,
    MappingInfo,
    SubConversation,
)
from app.services.orchestration import Failure, HopContext

from scripts.common.reporting import MarkdownReport, ValidationRun
from scripts.run_scenario import story3_conversations as story3
from scripts.run_scenario import story4_common as common
from scripts.run_scenario.story1_data_analysis import Blocked

CORRELATION = uuid4()


def hop() -> HopContext:
    return HopContext(
        agent="supervisor",
        hosted_runtime=True,
        call_id_present=True,
        user_id_present=True,
        session_id_present=True,
        user_fingerprint="f",
        correlation_header_matches=True,
    )


def report(
    *,
    status: str = "completed",
    operation: str = "send",
    pattern: str = "separate",
    deliver: str = "result",
    sub: str | None = "conv_child",
    created: bool = True,
    found: bool = False,
    before: int | None = 0,
    after: int | None = 2,
    recalled: bool | None = None,
    history: HistoryStats | None = None,
    failure: Failure | None = None,
) -> ConversationReport:
    return ConversationReport(
        correlation_id=CORRELATION,
        operation=operation,
        status=status,
        pattern=pattern,
        identity="agent",
        deliver=deliver,
        target_agent="research-agent",
        supervisor=hop(),
        supervisor_conversation_id="conv_sup",
        supervisor_history=history or HistoryStats(),
        mapping=MappingInfo(key="conversation-map/x", found=found, created_now=created)
        if pattern == "separate"
        else None,
        sub_conversation=SubConversation(
            conversation_id=sub,
            created_now=created,
            created_identity="agent",
            created_correlation_id=CORRELATION,
            metadata_linked=True,
        )
        if sub
        else None,
        listing_before=Listing(items=before),
        listing_after=Listing(items=after),
        response_id="resp" if status == "completed" and operation == "send" else None,
        failure=failure,
        sub_answer_chars=10 if status == "completed" else 0,
        tokens_in_sub_answer=[] if recalled is None else [recalled],
    )


@pytest.mark.parametrize("number", range(1, 6))
def test_every_step_summary_renders_with_verified_documentation(tmp_path: Path, number: int) -> None:
    step_class = story3.step_classes()[number]
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    markdown = MarkdownReport(run.path / "r.md", step_class.title)
    args = SimpleNamespace(environment="e", timeout=1, output_dir=tmp_path, mode="verify")
    step = step_class(args, run, markdown)
    step.record("Prerequisite", "BLOCKED", "ready", "missing")
    step.conclude()
    text = (run.path / "r.md").read_text(encoding="utf-8")
    assert "**Acceptance verdict: Not evaluated.**" in text
    assert "learn.microsoft.com" in text
    assert step.task_label.startswith("Agents: Story 3 - ") and step.ac and step.issue == "#3"
    assert (step_class.spec.story, step_class.spec.number, step_class.number) == (3, number, number)
    assert "scripts.run_scenario.story3_conversations" in step.command_line()
    rows = [line for line in text.splitlines() if re.match(r"\| \d+ \| ", line)]
    assert rows and text.index("## How to read this report") < text.index("## Summary")
    assert "| Prerequisite | ⛔ | ready | missing |" in text and "| BLOCKED |" not in text
    assert '"status": "BLOCKED"' in (run.path / "results.jsonl").read_text(encoding="utf-8")
    for row in rows:
        assert re.match(r"\| \d+ \| (✅|❌|🟡|⛔|🚫|⚠️|📄|⏭️) [A-Z ]+ \| ", row), row
        assert "✅ PASS" not in row, f"A blocked run must not report PASS: {row}"


def test_status_icons_replace_check_words_in_markdown_only() -> None:
    from scripts.common.reporting import status_icon

    assert [status_icon(s) for s in ("PASS", "FAIL", "BLOCKED", "SKIP")] == ["✅", "❌", "⛔", "⏭️"]
    assert status_icon(None) == "not run" and status_icon(None, "not checked") == "not checked"
    assert status_icon("completed") == "completed"


def test_acceptance_criteria_are_verbatim_from_the_issue() -> None:
    assert story3.Step1.ac.startswith("Demonstrate a supervisor conversation and a separate sub-agent conversation.")
    assert story3.Step2.ac.endswith("its prior history is independently available.")
    assert "identify custom correlation required." in story3.Step3.ac
    assert story3.Step4.ac.startswith("Evaluate Agent A invoking Agent B through the Responses API")
    assert story3.Step5.ac.endswith("a placeholder, or full history.")


def test_outcome_classifiers() -> None:
    assert story3.capability_from(
        common.Exchange("x", "a", status="failed", error="Create an explicit Supervisor conversation")
    )
    assert not story3.capability_from(common.Exchange("x", "a", status="completed", text="chat"))
    assert story3.listing_outcome(Listing(items=3)) == "allowed (3 items)"
    denied = Listing(failure=Failure(stage="request", http_status=404, code="not_found", message="no"))
    assert story3.listing_outcome(denied) == "denied (HTTP 404, not_found)" and story3.listing_definitive(denied)
    server = Listing(failure=Failure(stage="request", http_status=500, message="boom"))
    assert not story3.listing_definitive(server) and not story3.listing_definitive(None)
    assert story3.report_outcome(report()) == "allowed"
    assert story3.report_outcome(report(status="denied")).startswith("denied by the Supervisor mapping")
    forbidden = Failure(stage="request", http_status=403, code="forbidden", message="no")
    failed = report(status="sub_agent_failed", failure=forbidden)
    assert story3.report_outcome(failed) == "denied (HTTP 403, forbidden)" and story3.definitive(failed)
    crashed = report(status="sub_agent_failed", failure=Failure(stage="request", http_status=500, message="x"))
    assert not story3.definitive(crashed) and not story3.definitive(None)
    assert story3.recalled(report(recalled=True)) and not story3.recalled(report(recalled=False))
    access = story3.Access(http_status=404, code="not_found")
    assert access.denied and not access.allowed and access.outcome() == "denied (HTTP 404, not_found)"
    assert story3.Access(items=[{}, {}]).outcome() == "allowed (2 items)"


def test_classify_received_distinguishes_result_placeholder_full_history_and_nothing() -> None:
    stored = HistoryStats(reply_token_hits=[1])
    assert story3.classify_received(report(), stored) == "result"
    assert story3.classify_received(report(), HistoryStats(reply_token_hits=[0])) == "placeholder"
    assert story3.classify_received(report(deliver="placeholder"), HistoryStats()) == "placeholder"
    full = HistoryStats(hop_inputs=1, other_assistant=1)
    assert story3.classify_received(report(pattern="shared", deliver="placeholder"), full) == "full history"
    assert story3.classify_received(report(status="sub_agent_failed"), full).startswith("nothing")
    assert story3.classify_received(None, None).startswith("nothing")


def test_separate_variant_requires_observed_delivery_not_the_requested_mode() -> None:
    delivered = report(recalled=True).model_copy(update={"sub_answer": "TOKEN"})
    assert story3.separate_variant_ok(delivered, "result", "result")
    assert not story3.separate_variant_ok(delivered, "placeholder", "result")
    assert not story3.separate_variant_ok(report(recalled=False), "result", "result")
    placeholder = report(deliver="placeholder", recalled=True)
    assert story3.separate_variant_ok(placeholder, "placeholder", "placeholder")
    assert not story3.separate_variant_ok(delivered, "placeholder", "placeholder")
    assert not story3.separate_variant_ok(None, "result", "result")


def test_server_errors_are_never_labelled_as_denials() -> None:
    assert story3.Access(http_status=503).outcome() == "error (HTTP 503)"
    crashed = Listing(failure=Failure(stage="request", http_status=500, message="boom"))
    assert story3.listing_outcome(crashed) == "error (HTTP 500)"
    failed = report(status="sub_agent_failed", failure=Failure(stage="request", http_status=502, message="x"))
    assert story3.report_outcome(failed) == "error (HTTP 502)"


def test_cli_rejects_mode_outside_step1_and_blocked_runs_are_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit):
        story3.main(["--step", "2", "--mode", "prepare-if-missing"])
    monkeypatch.setattr(common, "read_environment", MagicMock(side_effect=Blocked("no azd env")))
    code = story3.main(["--step", "4", "--environment", "env-a", "--output-dir", str(tmp_path), "--no-color"])
    assert code == 2
    index = (tmp_path / "story-3" / "index.md").read_text(encoding="utf-8")
    assert index.startswith("# Story 3 evidence index")
    (directory,) = (tmp_path / "story-3" / "step-04-shared-conversation-id").iterdir()
    link = f"step-04-shared-conversation-id/{directory.name}"
    assert f"Step 4 (shared conversation id) - **Not evaluated** - [{link}]({link}/report.md)" in index


def test_story3_baseline_guard_names_story3(tmp_path: Path) -> None:
    with pytest.raises(Blocked, match="Story 3 baseline"):
        common.load_baseline(tmp_path / common.BASELINE_FILE, "env", "Story 3")


def make_step(step_class: type[story3.Story3Step], tmp_path: Path) -> story3.Story3Step:
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    return step_class(
        SimpleNamespace(environment="e", timeout=1, output_dir=tmp_path), run, MarkdownReport(run.path / "r.md", "t")
    )


def test_step2_met_when_follow_up_reuses_and_recalls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    step = make_step(story3.Step2, tmp_path)
    monkeypatch.setattr(step, "new_conversation", lambda agent: f"conv_{uuid4().hex[:6]}")
    sends = iter(
        [
            report(),
            report(created=False, found=True, before=2, after=4, recalled=True, history=HistoryStats(items=3)),
        ]
    )

    def fake_send(label: str, conversation: str | None, message: str, **_kwargs: Any) -> Any:
        return common.Exchange(label, "supervisor-agent", status="completed"), next(sends)

    monkeypatch.setattr(step, "send", fake_send)
    monkeypatch.setattr(
        step,
        "inspect",
        lambda label, conversation, **_kwargs: (
            common.Exchange(label, "supervisor-agent", status="completed"),
            report(status="inspected", operation="inspect", sub=None, created=False, found=False),
        ),
    )
    step.body()
    assert step.compute_verdict() == "Met"
    assert step.statuses["Sub-agent recalled its own history"] == "PASS"


def test_step2_fails_when_sub_agent_history_leaks_into_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = make_step(story3.Step2, tmp_path)
    monkeypatch.setattr(step, "new_conversation", lambda agent: "conv")
    leaked = HistoryStats(hop_inputs=1, other_assistant=1)
    sends = iter([report(), report(created=False, found=True, before=2, after=4, recalled=True, history=leaked)])
    monkeypatch.setattr(step, "send", lambda label, *_a, **_k: (common.Exchange(label, "s"), next(sends)))
    monkeypatch.setattr(
        step,
        "inspect",
        lambda label, *_a, **_k: (common.Exchange(label, "s"), report(status="inspected", sub=None, found=True)),
    )
    step.body()
    assert step.statuses["Sub-agent history not copied into the Supervisor conversation"] == "FAIL"
    assert step.statuses["Another Supervisor conversation has no mapping"] == "FAIL"
    assert step.compute_verdict() == "Partially met"


def test_step5_classifies_every_variant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    step = make_step(story3.Step5, tmp_path)
    monkeypatch.setattr(step, "new_conversation", lambda agent: "conv")
    denied = Failure(stage="request", http_status=404, code="not_found", message="no")
    last: dict[str, str] = {}

    def fake_send(label: str, conversation: str | None, message: str, **kwargs: Any) -> Any:
        last["deliver"] = kwargs["deliver"]
        if kwargs["pattern"] == "shared" and kwargs["identity"] == "agent":
            result = report(status="sub_agent_failed", pattern="shared", sub=None, failure=denied)
        else:
            result = report(pattern=kwargs["pattern"], deliver=kwargs["deliver"], recalled=True)
            if kwargs["deliver"] == "result":
                result = result.model_copy(update={"sub_answer": "TOKEN"})
        return common.Exchange(label, "s", status="completed"), result

    def fake_inspect(label: str, conversation: str | None, **kwargs: Any) -> Any:
        assert kwargs["tokens"], "the later inspect must probe the sub-agent token"
        if kwargs["pattern"] == "shared":
            history = HistoryStats(hop_inputs=1, other_assistant=1, reply_token_hits=[0])
        else:
            history = HistoryStats(reply_token_hits=[1 if last["deliver"] == "result" else 0])
        return common.Exchange(label, "s"), report(status="inspected", pattern=kwargs["pattern"], history=history)

    monkeypatch.setattr(step, "send", fake_send)
    monkeypatch.setattr(step, "inspect", fake_inspect)
    step.body()
    assert [variant.received for variant in step.variants] == [
        "result",
        "placeholder",
        "nothing (sub-agent call did not complete)",
        "full history",
    ]
    assert step.compute_verdict() == "Met"
    _, _, findings, _ = step.summary()
    assert findings[1].outcome == "LIMITATION"
