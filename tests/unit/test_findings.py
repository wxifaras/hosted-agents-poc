"""Reader-first report summaries with Microsoft documentation, without Azure calls."""

import re
from argparse import Namespace
from pathlib import Path

from scripts.common.findings import (
    ADR_FILE,
    DOCS,
    ISOLATION_OPTIONS_ANCHOR,
    NO_DOC,
    OUTCOMES,
    Finding,
    checks_outcome,
    isolation_options_link,
    render_summary,
)
from scripts.common.reporting import SUMMARY_SLOT, MarkdownReport, ValidationRun
from scripts.run_scenario import story1_data_analysis as story
from scripts.run_scenario import story1_sandbox as sb

LEARN = "](https://learn.microsoft.com/"


def test_isolation_options_link_resolves_to_the_adr_section_from_a_report_folder() -> None:
    root = ADR_FILE.parents[2]
    report_dir = root / "artifacts" / "story-1" / "step-01-code-interpreter" / "20261026T161436Z-f959ae7e"
    link = isolation_options_link(report_dir)
    target = re.fullmatch(r"\[[^\]]+\]\(([^)#]+)#([^)]+)\)", link)
    assert target, link
    assert target.group(1) == "../../../../docs/adr/0001-hosted-agents-poc.md"
    assert (report_dir / target.group(1)).resolve() == ADR_FILE.resolve()
    heading = "### Per-user Code Interpreter isolation: options to investigate"
    assert heading in ADR_FILE.read_text(encoding="utf-8")
    assert target.group(2) == ISOLATION_OPTIONS_ANCHOR


def not_supported_rows_have_docs(text: str) -> None:
    rows = [line for line in text.splitlines() if line.startswith("| ") and "| Not supported |" in line]
    assert rows, "expected at least one Not supported finding"
    for row in rows:
        assert LEARN in row, f"Not supported without a Microsoft link: {row}"


def test_every_source_is_an_official_learn_page_with_a_quote() -> None:
    assert len(DOCS) >= 20
    for key, doc in DOCS.items():
        assert doc.url.startswith("https://learn.microsoft.com/"), key
        assert "?" not in doc.url and " " not in doc.url, key  # query strings would be redacted in reports
        assert doc.title and 10 < len(doc.quote) < 300, key
        assert 20 < len(doc.meaning) < 250 and doc.meaning.endswith("."), key
        assert doc.link() == f"[{doc.title}]({doc.url})"


def test_check_statuses_map_to_one_finding_outcome() -> None:
    assert checks_outcome("PASS", "PASS") == "PASS"
    assert checks_outcome("PASS", "FAIL") == "PARTIAL"
    assert checks_outcome("PASS", "SKIP") == "PARTIAL"
    assert checks_outcome("FAIL", "BLOCKED") == "FAIL"
    assert checks_outcome("BLOCKED", None) == "BLOCKED"
    assert checks_outcome(None) == checks_outcome() == checks_outcome("SKIP") == "NOT RUN"


def test_summary_renders_in_reading_order_with_links_and_explicit_gaps() -> None:
    text = render_summary(
        "Met",
        "Concluded.",
        ["First result."],
        [
            Finding(
                "Documented thing",
                "Worked",
                "Tested",
                "Foundry-managed",
                ("ci_sandbox", "aca_isolation"),
                outcome="PASS",
            ),
            Finding("Undocumented thing", "Observed", "Observed", "Foundry-managed", outcome="LIMITATION"),
            Finding(
                "Missing capability", "No", "Documented", "Not supported", ("ci_isolation",), outcome="NOT SUPPORTED"
            ),
        ],
        ["Follow up."],
    )
    order = [
        "## Summary",
        "**Acceptance verdict: Met.** Concluded.",
        "[How to read this report](#how-to-read-this-report)",
        "### Key results",
        "### Findings",
        "**Outcomes:** ✅ PASS: 1 · 🚫 NOT SUPPORTED: 1 · ⚠️ LIMITATION: 1",
        "| # | Outcome | Finding | What was found | Evidence | Owner | Microsoft documentation |",
        "### Gaps and follow-ups",
        "### Microsoft documentation referenced",
        "| Microsoft page | What Microsoft says | What it means here | Supports finding |",
        "### How to read this report",
        "Detailed evidence",
    ]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    assert "| 1 | ✅ PASS | Documented thing | Worked |" in text
    assert "| 3 | 🚫 NOT SUPPORTED | Missing capability |" in text
    assert NO_DOC in text and text.count(DOCS["ci_sandbox"].url) == 2  # findings cell + referenced list
    assert DOCS["ci_isolation"].quote in text and DOCS["ci_isolation"].meaning in text
    assert f"| {DOCS['ci_isolation'].meaning} | 3 |" in text
    legend = text[text.index("### How to read this report") :]
    for name, (_, meaning) in OUTCOMES.items():
        assert name in legend and meaning in legend
    for label in ("Not evaluated", "BLOCKED", "Tested", "Custom implementation (Dev Team)"):
        assert label in legend
    not_supported_rows_have_docs(text)
    same_page = render_summary(
        "Met",
        "",
        [],
        [Finding("F", "R", "Documented", "x", ("standard_files", "standard_threads"), outcome="DOCUMENTED")],
        [],
    )
    findings_row = next(line for line in same_page.splitlines() if line.startswith("| 1 |"))
    assert findings_row.count(DOCS["standard_files"].url) == 1


def test_summary_is_placed_directly_under_the_title(tmp_path: Path) -> None:
    report = MarkdownReport(tmp_path / "r.md", "Title")
    report.heading("Details")
    report.summary("## Summary\n\nDone.")
    text = report.path.read_text(encoding="utf-8")
    assert SUMMARY_SLOT not in text
    assert text.index("## Summary") < text.index("## Details") and text.startswith("# Title\n\n## Summary")
    report.summary("## Again")
    assert report.path.read_text(encoding="utf-8").rstrip().endswith("## Again")


def make(cls: type, tmp_path: Path, **extra: object) -> object:
    args = Namespace(
        environment="env",
        mode="verify",
        timeout=300,
        output_dir=tmp_path,
        options_toolbox="probe",
        idle_wait="wait",
        blob_checkpoint="write",
        **extra,
    )
    run = ValidationRun("t", "hosted", tmp_path, no_color=True)
    return cls(args, run, MarkdownReport(run.path / "r.md", "Title"))


def test_step1_summary_cites_the_documented_container_apps_backing(tmp_path: Path) -> None:
    step = make(story.Step1, tmp_path)
    assert isinstance(step, story.Step1)
    step.verdict = "Met"
    step.statuses = {"Hosted agent executes in managed sandbox": "PASS"}
    step.obs = {
        "agent_version": "6",
        "toolbox": "tb v1",
        "inputs_verified": 3,
        "python": "3.11.15",
        "hosted_seconds": 23.6,
        "container_info": {"memory_limit": "2g"},
        "png_bytes": 3028,
    }
    step.conclude()
    text = step.report.path.read_text(encoding="utf-8")
    assert "**Acceptance verdict: Met.**" in text
    assert DOCS["ci_sandbox"].url in text and "Hyper-V boundary" in text
    assert "3 of 3 mounted CSVs" in text
    not_supported_rows_have_docs(text)
    step.statuses["Prerequisite"] = "BLOCKED"
    step.verdict = "Not met"
    step.conclude()
    assert "Acceptance verdict: Not evaluated" in step.report.path.read_text(encoding="utf-8")


def test_execution_outcome_separates_completed_stopped_and_inconclusive() -> None:
    done = sb.ToolCall(text="story1-sleep-start\nstory1-sleep-finished", seconds=101.8)
    cut = sb.ToolCall(text="story1-sleep-start\n", is_error=True, seconds=90.4)
    reset = sb.ToolCall(error="[WinError 10054] connection reset", seconds=224.2)
    assert [sb.execution_outcome(c) for c in (done, cut, reset)] == ["completed", "stopped", "inconclusive"]
    assert "consistent with the documented 220 s maximum" in sb.runtime_text({"outcome": "completed", "seconds": 101.8})
    assert "consistent with the 90 s" in sb.runtime_text({"outcome": "stopped", "seconds": 90.4})
    text = sb.runtime_text({"outcome": "inconclusive", "seconds": 224.2, "error": "reset"})
    assert text.startswith("Inconclusive") and "220 s maximum" not in text


def test_step4_summary_and_matrix_link_every_behavior(tmp_path: Path) -> None:
    step = make(sb.Step4, tmp_path)
    assert isinstance(step, sb.Step4)
    step.statuses = {"Direct probe A": "PASS"}
    step.obs = {
        "direct_a": {
            "identity": {"user": "sandbox", "uid": 1001},
            "privileges": {"cap_eff": "0000000000000000", "no_new_privs": "1"},
            "limits": {"cgroup_memory_max": "2147483648"},
            "network": {"dns_public": {"outcome": "dns_failure"}},
            "platform_hint_env_names": ["SESSIONS_INSTANCE"],
        },
        "network_open": [],
        "per_call": {"memory_limit 4g": "ignored (enforced 2.0 GiB)"},
        "options_toolbox": {"network_policy allowlist [x]": "UserError[400, ... organization policy ...]"},
        "timeout": {"outcome": "completed", "seconds": 102.0, "finished_100s_sleep": True},
        "expires_after": {"minutes": 20},
        "idle": "expired after 21.5 min idle",
    }
    step.conclude()
    text = step.report.path.read_text(encoding="utf-8")
    assert text.index("## Summary") < text.index("## Boundary matrix")
    assert "consistent with the documented 220 s maximum" in text and DOCS["aca_runtime"].url in text
    assert "This matches the Responses API container docs (20 min)" in text
    assert "| 🚫 NOT SUPPORTED | Larger sandbox memory (memory_limit 4g) | Not applied:" in text
    assert "| 🚫 NOT SUPPORTED | Network allowlist (network_policy) to enable selected egress |" in text
    assert "| ⚠️ LIMITATION | Per-execution runtime limit |" in text
    assert "| ✅ PASS | Code runs unprivileged with a read-only root filesystem |" in text
    not_supported_rows_have_docs(text)
    matrix = text[text.index("## Boundary matrix") :]
    rows = [
        line
        for line in matrix.splitlines()
        if re.match(r"\| (Security|Session lifetime|Network|Configuration) \|", line)
    ]
    assert len(rows) == 14 and all(LEARN in row for row in rows)
