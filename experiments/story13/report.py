"""Render a customer-friendly startup report with an auditable technical appendix.

Exit codes: 0 report rendered (not experiment acceptance), 2 invalid evidence/I/O.
No synthetic measurements, external scripts, fonts, CDNs, or network access.
"""

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from statistics import median

from pydantic import ValidationError

from measure import RunInfo, Sample, diagnostics

COLORS = ("#005a9e", "#008272", "#8764b8", "#ca5010", "#498205", "#a4262c", "#486860", "#7a4780", "#605e5c")
LABELS = {
    "baseline": "Without snapshots",
    "snapshot": "With memory snapshots",
    "optimized": "Snapshots + precompiled Python",
}
BAR_COLORS = {"baseline": "#566877", "snapshot": "#007f75", "optimized": "#2563eb"}
SCOPE = (
    "Real Microsoft Foundry Hosted Agent deployments, with no AI model or external tool calls. "
    "These results measure startup, not the full response time of a customer workflow."
)
STATE_LIMIT = (
    "With snapshots, changes to application memory and local files do not survive idle/resume. "
    "Store required state outside the agent container."
)


@dataclass(frozen=True)
class Run:
    """Validated controls and every recorded attempt, including failures."""

    info: RunInfo
    samples: list[Sample]


def load_run(path: Path) -> Run:
    """Read an explicit JSONL run; reject inconsistent or duplicate observations."""
    with path.open(encoding="utf-8") as stream:
        info = RunInfo.model_validate_json(stream.readline())
        samples = [Sample.model_validate_json(line) for line in stream if line.strip()]
    seen: set[tuple[int, str]] = set()
    for sample in samples:
        key = sample.trial, sample.phase
        if key in seen or sample.trial > info.samples:
            raise ValueError("Duplicate or out-of-budget trial in evidence.")
        if sample.phase == "resume" and sample.trial > info.resume_samples:
            raise ValueError("Resume attempt exceeds the approved run budget.")
        seen.add(key)
        _, native, start_type = diagnostics(sample.headers)
        if start_type != sample.start_type or any(sample.timings.get(key) != value for key, value in native.items()):
            raise ValueError("Stored timings/classification disagree with the native headers.")
        if sample.passed and (
            sample.errors
            or sample.start_type != sample.phase
            or sample.timings.get("client_complete_ms") is None
            or not sample.response_id
        ):
            raise ValueError("A passing sample lacks matching classification, timings, or response identity.")
        if not sample.passed and not sample.errors:
            raise ValueError("Failed attempts must explain their failure.")
    return Run(info, samples)


def validate_controls(runs: Sequence[Run]) -> None:
    """Refuse comparisons that mix projects, compute, SDKs, or incompatible images."""
    if not runs or len({run.info.condition for run in runs}) != len(runs):
        raise ValueError("Supply one run per distinct condition.")
    keys = ("project_hash", "agent", "cpu", "memory", "core_version", "responses_version", "configuration_hash")
    first = runs[0].info
    for run in runs:
        if any(getattr(run.info, key) != getattr(first, key) for key in keys):
            raise ValueError("Comparison controls differ: project, agent, compute, SDK, or version configuration.")
        if run.info.snapshots != (run.info.condition != "baseline"):
            raise ValueError("Snapshot flag does not match the declared condition.")
    images = {run.info.condition: run.info.image for run in runs}
    if "baseline" in images and "snapshot" in images and images["baseline"] != images["snapshot"]:
        raise ValueError("Baseline and snapshot must use the exact same digest-pinned image.")
    if "optimized" in images and any(
        images["optimized"].rsplit("@", 1)[-1] == image.rsplit("@", 1)[-1]
        for condition, image in images.items()
        if condition != "optimized"
    ):
        raise ValueError("The optimized condition must use a distinct optimized image digest.")


def values(run: Run, phase: str, metric: str) -> list[float]:
    """Only matching, passing attempts with a measured value enter the graphs."""
    return [
        value
        for sample in run.samples
        if sample.passed and sample.phase == phase and sample.start_type == phase
        if (value := sample.timings.get(metric)) is not None
    ]


def describe(data: Sequence[float]) -> str:
    """Show sample count, median, and range; never invent zero for absent evidence."""
    if not data:
        return "Unavailable (n=0)"
    return f"n={len(data)}; median {median(data):,.1f}; range {min(data):,.1f}-{max(data):,.1f} ms"


def cold_median(run: Run | None, metric: str) -> float | None:
    """Return a measured cold median, never a zero placeholder."""
    data = values(run, "cold", metric) if run is not None else []
    return median(data) if data else None


def review_notes(runs: Sequence[Run]) -> list[str]:
    """Keep incomplete or failed comparison warnings visible above the charts."""
    prefixes = ("INCOMPLETE:", "NOT ACCEPTED:", "INSUFFICIENT", "NOT VALIDATED:")
    return [note for note in findings(runs) if note.startswith(prefixes)]


def summary_content(runs: Sequence[Run], diagnostic_runs: Sequence[Run] = ()) -> tuple[list[str], str]:
    """Derive a plain-language summary and a qualified conclusion from the evidence."""
    by_condition = {run.info.condition: run for run in runs}
    before = cold_median(by_condition.get("baseline"), "client_journey_ms")
    after = cold_median(by_condition.get("snapshot"), "client_journey_ms")
    bullets = ["We tested real deployed Hosted Agents in Microsoft Foundry, not a local simulation."]
    if before is not None and after is not None and before > 0:
        change = (1 - after / before) * 100
        direction = "less" if change >= 0 else "more"
        bullets.append(
            f"The median first useful response was {before / 1000:.2f} seconds without snapshots "
            f"and {after / 1000:.2f} seconds with snapshots: {abs(change):.1f}% {direction} waiting."
        )
    else:
        bullets.append("There are not enough paired measurements to compare first-response time.")
    ready_before = cold_median(by_condition.get("baseline"), "container-readiness-ms")
    ready_after = cold_median(by_condition.get("snapshot"), "container-readiness-ms")
    if ready_before is not None and ready_after is not None:
        bullets.append(
            f"The agent application's preparation time was {ready_before / 1000:.3f} seconds without snapshots "
            f"and {ready_after / 1000:.3f} seconds with snapshots."
        )
    optimized = cold_median(by_condition.get("optimized"), "client_journey_ms")
    if optimized is not None and after is not None:
        bullets.append(
            f"Adding precompiled Python recorded {optimized / 1000:.2f} seconds, compared with "
            f"{after / 1000:.2f} seconds for snapshots alone. This small sample does not prove a repeatable extra gain."
        )
    planned = sum(run.info.samples * 2 + run.info.resume_samples for run in runs)
    passed = sum(sample.passed for run in runs for sample in run.samples)
    diagnostic_count = sum(len(run.samples) for run in diagnostic_runs)
    bullets.append(
        f"{passed} of {planned} planned cloud checks passed. "
        f"{diagnostic_count} earlier failed diagnostic attempts are disclosed separately, "
        "not counted as successful comparisons."
    )
    if review_notes(runs) or before is None or after is None:
        conclusion = (
            "The evidence is incomplete or needs review. Do not make a firm performance recommendation yet; "
            "resolve the missing measurements or failed checks first."
        )
    elif after < before:
        conclusion = (
            "Memory snapshots were the main startup improvement in this controlled test. "
            "They are a good candidate for agents that keep required state outside the container. "
            "Validate the customer's real model and tools next, before promising end-to-end response times."
        )
    else:
        conclusion = (
            "This test did not demonstrate a lower median first-response time with snapshots. "
            "Investigate the startup components before recommending them as a latency improvement."
        )
    return bullets, conclusion


def summary_markdown(runs: Sequence[Run], diagnostic_runs: Sequence[Run] = ()) -> str:
    """Provide a short downloadable summary suitable for an email or meeting notes."""
    bullets, conclusion = summary_content(runs, diagnostic_runs)
    return (
        "# Story 13 - Hosted-agent startup: simple summary\n\n## Summary\n\n"
        + "\n".join(f"- {bullet}" for bullet in bullets)
        + f"\n\n## Conclusion\n\n{conclusion}\n\n## Important limits\n\n"
        + f"- {SCOPE}\n- {STATE_LIMIT}\n"
        + "- Small, sequential samples do not establish p95, statistical significance, or a service guarantee.\n"
    )


def simple_comparison(title: str, runs: Sequence[Run], metric: str) -> tuple[str, str]:
    """Render the same median values as responsive HTML bars and a downloadable SVG."""
    series = [(run.info.condition, values(run, "cold", metric)) for run in runs]
    largest = max((median(data) for _, data in series if data), default=0) or 1
    response_time = metric == "client_journey_ms"
    unit_note = "Seconds until the first useful response" if response_time else "Milliseconds; 1,000 ms = 1 second"
    rows: list[str] = []
    descriptions: list[str] = []
    height = 100 + 96 * len(series)
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 {height}" role="img" '
        'aria-labelledby="title description">',
        f'<title id="title">{escape(title)}</title>',
        '<desc id="description">Shorter bars mean less time. Each bar is the median of the passing, '
        "native-confirmed cold starts. Missing data is not zero.</desc>",
        f'<rect width="960" height="{height}" fill="white"/>',
        '<g font-family="sans-serif" fill="#162b40">',
        f'<text x="24" y="30" font-size="24">{escape(title)}</text>',
        f'<text x="24" y="56" font-size="16">{escape(unit_note)}. Lower is better.</text>',
    ]
    for index, (condition, data) in enumerate(series):
        label = LABELS[condition]
        y = 90 + index * 96
        svg.append(f'<text x="24" y="{y}" font-size="20">{escape(label)}</text>')
        if not data:
            rows.append(f'<div class="bar-row"><strong>{escape(label)}</strong><p>Not measured</p></div>')
            descriptions.append(f"{label}: not measured")
            svg.append(f'<text x="24" y="{y + 36}" font-size="18">Not measured</text>')
            continue
        measured = median(data)
        value = f"{measured / 1000:.2f} s" if response_time else f"{measured:,.1f}".removesuffix(".0") + " ms"
        percentage = measured / largest * 100
        color = BAR_COLORS[condition]
        rows.append(
            f'<div class="bar-row"><div class="bar-label"><strong>{escape(label)}</strong>'
            f'<span>{value}</span></div><div class="bar-track" aria-hidden="true">'
            f'<div class="bar-fill" style="width:{percentage:.3f}%;background:{color}"></div></div>'
            f"<small>{len(data)} confirmed cold starts</small></div>"
        )
        descriptions.append(f"{label}: {value}, {len(data)} cold starts")
        length = measured / largest * 700
        svg.extend(
            [
                f'<rect class="comparison-bar" x="24" y="{y + 14}" width="{length:.3f}" height="26" '
                f'rx="5" fill="{color}"/>',
                f'<text x="{36 + length:.3f}" y="{y + 35}" font-size="22">{value}</text>',
                f'<text x="24" y="{y + 65}" font-size="15">{len(data)} confirmed cold starts</text>',
            ]
        )
    svg.append("</g></svg>")
    html = (
        f'<div class="simple-chart" role="img" aria-label="{escape(title + ". " + "; ".join(descriptions))}">'
        + "".join(rows)
        + f'</div><p class="chart-note">{escape(unit_note)}. Shorter bars mean less waiting.</p>'
    )
    return html, "\n".join(svg)


def distribution(title: str, series: Sequence[tuple[str, list[float]]]) -> str:
    """Draw raw observations, median ticks, and min/max whiskers as accessible SVG."""
    width, left, right, row = 1200, 205, 640, 56
    height = 115 + row * len(series)
    maximum = max((value for _, data in series for value in data), default=0) or 1
    scale = (right - left) / maximum
    nodes = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
        'aria-labelledby="title description">',
        f'<title id="title">{escape(title)}</title>',
        '<desc id="description">Each dot is one passing measured attempt. '
        "A thick tick marks the median; a whisker spans minimum to maximum. "
        "Missing data is unavailable, not zero.</desc>",
        f'<rect width="{width}" height="{height}" fill="white"/>',
        '<g font-family="sans-serif" font-size="12" fill="#242424">',
        f'<text x="12" y="24" font-size="18">{escape(title)}</text>',
    ]
    for index in range(5):
        value = maximum * index / 4
        x = left + value * scale
        nodes.append(f'<path d="M{x:.2f} 50 V{height - 35}" stroke="#e1dfdd"/>')
        nodes.append(f'<text x="{x:.2f}" y="{height - 12}" text-anchor="middle">{value:,.0f} ms</text>')
    for index, (label, data) in enumerate(series):
        y = 75 + index * row
        color = COLORS[index % len(COLORS)]
        nodes.append(f'<text x="12" y="{y + 4}">{escape(label)}</text>')
        if not data:
            nodes.append(f'<text x="{left}" y="{y + 4}">Unavailable (n=0)</text>')
            continue
        low, high, mid = (left + value * scale for value in (min(data), max(data), median(data)))
        nodes.append(f'<path d="M{low:.2f} {y} H{high:.2f}" stroke="{color}" stroke-width="2"/>')
        for number, value in enumerate(data):
            x = left + value * scale
            jitter = ((number % 5) - 2) * 3
            nodes.append(
                f'<circle cx="{x:.2f}" cy="{y + jitter}" r="4" fill="{color}" fill-opacity="0.7">'
                f"<title>{value:,.2f} ms</title></circle>"
            )
        nodes.append(f'<path d="M{mid:.2f} {y - 11} V{y + 11}" stroke="{color}" stroke-width="4"/>')
        nodes.append(f'<text x="665" y="{y + 4}">{escape(describe(data))}</text>')
    nodes.append("</g></svg>")
    return "\n".join(nodes)


def findings(runs: Sequence[Run]) -> list[str]:
    """Distinguish sufficient sampling, guardrail failures, and genuine reductions."""
    messages = [
        "This no-model probe isolates hosted startup, not customer workload, model, tool, or peer-agent latency.",
        "Native response-begin is time to response headers, not time to first useful body content.",
        "Client first-chunk is a transport observation, not model time-to-first-token. Native headers are best-effort.",
        "Cold session creation happens inside the timed POST; warm/resume reuse its returned session binding.",
        "No p95 or statistical significance is claimed from this small experiment.",
    ]
    present = {run.info.condition for run in runs}
    if present != {"baseline", "snapshot", "optimized"}:
        messages.append("INCOMPLETE: not all three planned conditions are present.")
    for run in runs:
        expected = run.info.samples * 2 + run.info.resume_samples
        failed = sum(not sample.passed for sample in run.samples)
        if len(run.samples) != expected or failed:
            messages.append(
                f"NOT ACCEPTED: {run.info.condition} recorded {len(run.samples)}/{expected} planned attempts, "
                f"including {failed} failures."
            )
        if len(values(run, "cold", "client_complete_ms")) < 10:
            messages.append(
                f"INSUFFICIENT EVIDENCE: {run.info.condition} has fewer than 10 confirmed passing cold starts."
            )
        if len(values(run, "cold", "container-readiness-ms")) < 10:
            messages.append(f"INSUFFICIENT READINESS DATA: {run.info.condition} has fewer than 10 readiness values.")
        if run.info.resume_samples == 0:
            messages.append(f"NOT VALIDATED: {run.info.condition} has no idle/resume guardrail budget.")
    baseline = next((run for run in runs if run.info.condition == "baseline"), None)
    if baseline:
        for run in runs:
            if run is baseline:
                continue
            for metric in ("container-readiness-ms", "client_journey_ms", "client_complete_ms"):
                before, after = values(baseline, "cold", metric), values(run, "cold", metric)
                if before and after and median(before) > 0:
                    reduction = (1 - median(after) / median(before)) * 100
                    messages.append(
                        f"{run.info.condition} vs baseline, cold {metric}: {reduction:+.1f}% median reduction "
                        f"(n={len(before)}/{len(after)}). Negative means slower; sampling warnings still apply."
                    )
    return messages


def render(
    runs: Sequence[Run], diagnostic_runs: Sequence[Run] = (), *, issue_html: str | None = None
) -> tuple[str, dict[str, str]]:
    """Build the entire report before writing anything, using measured data only."""
    validate_controls(runs)
    if any(
        run.info.project_hash != runs[0].info.project_hash
        or run.info.agent != runs[0].info.agent
        or any(sample.passed for sample in run.samples)
        for run in diagnostic_runs
    ):
        raise ValueError("Diagnostic inputs must be failed attempts for the same project and isolated agent.")
    ordered = sorted(runs, key=lambda run: ("baseline", "snapshot", "optimized").index(run.info.condition))
    series = [
        (f"{run.info.condition} / {phase}", values(run, phase, "client_journey_ms"))
        for run in ordered
        for phase in ("cold", "warm", "resume")
    ]
    readiness = [
        (f"{run.info.condition} / {phase}", values(run, phase, "container-readiness-ms"))
        for run in ordered
        for phase in ("cold", "resume")
    ]
    components = [
        (f"{run.info.condition} / {label}", values(run, "cold", metric))
        for run in ordered
        for label, metric in (
            ("platform", "platform-preprocessing-ms"),
            ("infrastructure", "infra-setup-ms"),
            ("readiness", "container-readiness-ms"),
            ("handling", "container-response-ms"),
        )
    ]
    charts = {
        "startup-distribution.svg": distribution("Client useful response: native cold, warm, and resume", series),
        "readiness.svg": distribution("Container readiness: cold and resume", readiness),
        "cold-components.svg": distribution("Native cold-start components (not stacked)", components),
    }
    response_chart, charts["first-response-comparison.svg"] = simple_comparison(
        "How long did the first response take?", ordered, "client_journey_ms"
    )
    preparation_chart, charts["preparation-comparison.svg"] = simple_comparison(
        "What changed inside the agent?", ordered, "container-readiness-ms"
    )
    analysis = findings(ordered)
    if diagnostic_runs:
        analysis.append(
            f"INITIAL DIAGNOSTICS: {sum(len(run.samples) for run in diagnostic_runs)} failed capture/methodology "
            "attempts are disclosed below and retained in evidence; they are not valid cold-start comparison samples."
        )
    notes = "".join(f"<li>{escape(note)}</li>" for note in analysis)
    controls = "".join(
        f"<tr><td>{run.info.condition}</td><td>{escape(run.info.agent_version)}</td>"
        f"<td>{escape(run.info.cpu)} / {escape(run.info.memory)}</td><td>{run.info.snapshots}</td>"
        f"<td><code>{escape(run.info.image)}</code></td></tr>"
        for run in ordered
    )
    rows = "".join(
        f"<tr><td>{run.info.condition}</td><td>{phase}</td>"
        f"<td>{escape(describe(values(run, phase, 'client_journey_ms')))}</td>"
        f"<td>{escape(describe(values(run, phase, 'client_complete_ms')))}</td>"
        f"<td>{escape(describe(values(run, phase, 'container-readiness-ms')))}</td></tr>"
        for run in ordered
        for phase in ("cold", "warm", "resume")
    )
    attempts = "".join(
        f"<tr><td>{run.info.condition}{' (diagnostic)' if diagnostic else ''}</td>"
        f"<td>{sample.trial}</td><td>{sample.phase}</td>"
        f"<td>{sample.start_type or 'unknown'}</td><td>{'PASS' if sample.passed else 'FAIL'}</td>"
        f"<td>{escape('; '.join(sample.errors))}</td></tr>"
        for run, diagnostic in [(run, False) for run in ordered] + [(run, True) for run in diagnostic_runs]
        for sample in run.samples
    )
    figures = "".join(
        f'<figure><img src="{name}" alt="{escape(title)}; raw observations, median and range">'
        f'<figcaption><a href="{name}">Open/download SVG</a></figcaption></figure>'
        for name, title in (
            ("startup-distribution.svg", "Client latency distributions"),
            ("readiness.svg", "Container readiness distributions"),
            ("cold-components.svg", "Native cold-start component distributions"),
        )
    )
    bullets, conclusion = summary_content(ordered, diagnostic_runs)
    summary_html = "".join(f"<li>{escape(bullet)}</li>" for bullet in bullets)
    review = review_notes(ordered)
    warning = (
        '<aside class="notice" role="status"><strong>Results need review before drawing a conclusion.</strong><ul>'
        + "".join(f"<li>{escape(note)}</li>" for note in review)
        + "</ul></aside>"
        if review
        else ""
    )
    by_condition = {run.info.condition: run for run in ordered}
    before = cold_median(by_condition.get("baseline"), "client_journey_ms")
    after = cold_median(by_condition.get("snapshot"), "client_journey_ms")
    change = (1 - after / before) * 100 if before is not None and before > 0 and after is not None else None
    change_value = f"{abs(change):.1f}%" if change is not None else "Not measured"
    change_label = (
        "less first-response waiting" if change is not None and change >= 0 else "more first-response waiting"
    )
    if change is None:
        change_label = "paired comparison unavailable"
    saved = before - after if before is not None and after is not None else None
    saved_value = f"{abs(saved) / 1000:.2f} s" if saved is not None else "Not measured"
    saved_label = "less wait at the median" if saved is not None and saved >= 0 else "more wait at the median"
    if saved is None:
        saved_label = "paired comparison unavailable"
    planned = sum(run.info.samples * 2 + run.info.resume_samples for run in ordered)
    passed = sum(sample.passed for run in ordered for sample in run.samples)
    diagnostic_count = sum(len(run.samples) for run in diagnostic_runs)
    friendly_rows: list[str] = []
    for run in ordered:
        first = cold_median(run, "client_journey_ms")
        ready = cold_median(run, "container-readiness-ms")
        first_label = f"{first / 1000:.2f} s" if first is not None else "Not measured"
        ready_label = f"{ready:,.1f}".removesuffix(".0") + " ms" if ready is not None else "Not measured"
        friendly_rows.append(
            f'<tr><th scope="row">{LABELS[run.info.condition]}</th><td>{first_label}</td>'
            f"<td>{ready_label}</td><td>{len(values(run, 'cold', 'client_journey_ms'))}</td></tr>"
        )
    comparison_table = "".join(friendly_rows)
    card_tone = " caution" if review or change is None or change < 0 else ""
    page_title = "Story 13 - Memory snapshots: simple results and conclusion"
    hero_title = "Memory snapshots:<br>a simple startup-results report"
    contents = ""
    technical_open = ""
    if issue_html is not None:
        page_title = "Story 13 - Detailed hosted-agent cold-start validation"
        hero_title = "Story 13:<br>Hosted-agent cold-start validation"
        technical_open = " open"
        contents = """<nav class="paper" aria-label="Report contents"><h2>Contents</h2>
<ul><li><a href="#report-summary">Summary and conclusion</a></li>
<li><a href="#test-approach">Architecture, SDKs, and optimization techniques</a></li>
<li><a href="#comparison-graphs">Comparison graphs</a></li>
<li><a href="#task-validation">Task walkthroughs and evidence boundaries</a></li>
<li><a href="#recorded-observations">Every recorded observation and timing</a></li>
<li><a href="#issue-details">Full issue scope, requirements, runbook, risks, and sources</a></li>
<li><a href="#technical">Detailed distributions and diagnostic failures</a></li></ul></nav>
<aside class="notice">Informational POC findings, not official Microsoft guidance or an SLA.
This publication reuses retained evidence; generating it did not rerun hosted validation.</aside>"""
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{page_title}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f3f6fa;color:#162b40;font:16px/1.6 "Segoe UI",system-ui,sans-serif}}
.wrap{{max-width:1120px;margin:auto;padding:32px 20px}}h1,h2,h3{{line-height:1.2}}
h1{{font-size:clamp(2rem,4vw,3rem);margin:.6rem 0}}
h2{{font-size:1.45rem;margin-top:0}}h3{{font-size:1.15rem}}p{{margin:.7rem 0}}a{{color:#0759b4}}
.hero{{padding:32px;border-radius:22px;background:linear-gradient(120deg,#102a43,#155e75);color:white}}
.eyebrow{{font-size:.85rem;letter-spacing:.08em;text-transform:uppercase}}.hero p{{max-width:75ch;color:#e3eff7}}
.cards,.columns{{display:grid;gap:18px;margin:22px 0}}.cards{{grid-template-columns:repeat(3,minmax(0,1fr))}}
.columns{{grid-template-columns:repeat(2,minmax(0,1fr))}}
.paper,.card{{background:white;border:1px solid #dce5ee;border-radius:16px;padding:24px;min-width:0}}
.card-value{{font-size:2.7rem;font-weight:750;line-height:1.2;color:#006c64}}.card p{{margin:.6rem 0 0}}
.card-value.caution{{color:#92520b}}
.muted,.chart-note,small{{color:#486175}}.chart-note{{font-size:.88rem}}
.paper{{margin:20px 0}}.columns .paper{{margin:0}}
.scope{{border-left:4px solid #2563eb;padding:16px 20px;background:#eaf2fe;border-radius:8px;margin:20px 0}}
.bar-row{{margin:24px 0}}
.bar-label{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:6px}}
.bar-label span{{font-size:1.2rem;font-weight:700}}
.bar-track{{height:20px;background:#e8eef5;border-radius:8px;overflow:hidden;margin:8px 0 2px}}
.bar-fill{{height:100%;border-radius:8px}}small{{font-size:.8rem}}figure{{margin:0}}img{{width:100%;height:auto}}
.notice{{padding:16px 20px;background:#fff7e6;border-left:4px solid #c78200;border-radius:8px;margin:20px 0}}
.conclusion{{background:#edf5fc;border-color:#bad2e8}}li{{margin:.55rem 0}}ul,ol{{padding-left:1.4rem}}
.download-links{{display:flex;flex-wrap:wrap;gap:12px;margin:22px 0}}
.download-links a{{padding:10px 16px;border:1px solid #aac2d9;border-radius:8px;background:white;text-decoration:none}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
td,th{{border-bottom:1px solid #dce5ee;padding:12px;text-align:left;vertical-align:top}}
thead{{background:#eef3f8}}.table-scroll{{overflow-x:auto}}code{{overflow-wrap:anywhere}}
pre{{overflow-x:auto;background:#eef3f8;padding:16px;border-radius:8px}}
details>summary{{font-size:1.15rem;font-weight:650;cursor:pointer}}.technical h2{{margin-top:24px}}
.technical table{{font-size:.85rem}}.technical figure{{margin:24px 0}}.footer{{font-size:.88rem;color:#486175}}
@media(max-width:780px){{
.cards,.columns{{grid-template-columns:1fr}}.wrap{{padding:16px 12px}}.hero,.paper,.card{{padding:20px}}
}}
@media print{{
body{{background:white;font-size:11pt}}.wrap{{max-width:none;padding:0}}
.hero{{background:#102a43;print-color-adjust:exact}}
.cards,.columns{{break-inside:avoid}}.paper{{box-shadow:none}}.technical,.download-links{{display:none}}}}
</style></head><body><main class="wrap">
<header class="hero"><div class="eyebrow">Story 13 &middot; Microsoft Foundry &middot; Real cloud test</div>
<h1>{hero_title}</h1>
<p>We compared three versions of a deployed Hosted Agent to see how quickly a new session
could return its first useful response.</p></header>
{contents}
{warning}
<section class="cards" aria-label="Key results">
<div class="card"><div class="card-value{card_tone}">{change_value}</div><p><strong>{change_label}</strong></p>
<small>Snapshots versus the same image without snapshots; observed medians.</small></div>
<div class="card"><div class="card-value{card_tone}">{saved_value}</div><p><strong>{saved_label}</strong></p>
<small>Difference between the two condition medians.</small></div>
<div class="card"><div class="card-value{card_tone}">{passed}/{planned}</div>
<p><strong>planned cloud checks passed</strong></p>
<small>Cold starts, warm calls, and return after idle.</small></div>
</section>
<aside class="scope"><strong>What these numbers mean.</strong> {escape(SCOPE)}</aside>
<section class="paper" id="report-summary"><h2>Summary</h2><ul>{summary_html}</ul>
<p class="muted"><strong>Cold start</strong> means the first request in a new session.
<strong>Warm</strong> means the agent is already running.
A <strong>median</strong> is the middle measured result, not a worst-case promise.</p></section>
<section class="paper" id="test-approach"><h2>What we tested</h2>
<p><strong>Architecture.</strong> One isolated Python Responses agent ran in a container hosted by Microsoft Foundry,
with CPU <code>{escape(ordered[0].info.cpu)}</code> and memory <code>{escape(ordered[0].info.memory)}</code>.
Separate deployed versions represented the configurations below.
No AI model, external tools, or multi-agent workflow was included, so the test isolates hosted startup.</p>
<p><strong>SDK.</strong> We used snapshot-capable Agent Server Core
<code>{escape(ordered[0].info.core_version)}</code> and Responses
<code>{escape(ordered[0].info.responses_version)}</code>. SDK versions and compute stayed the same across conditions;
the baseline and snapshot-only versions also used the same container image.</p>
<p><strong>Features and techniques compared:</strong></p><ul>
<li><strong>Baseline:</strong> normal container startup, with memory snapshots disabled.</li>
<li><strong>Native memory snapshots:</strong> Foundry's <code>enableMemorySnapshot</code> setting restores
initialized application state. SDK before-snapshot and after-restore hooks verified capture and restoration.</li>
<li><strong>Snapshots plus precompiled Python:</strong> the optimized image additionally compiled Python
application, dependency, and standard-library bytecode during the image build, not at startup.</li></ul>
<p class="muted">All images used a slim multistage build, build-time dependency installation, and disabled
user-site packages (<code>PYTHONNOUSERSITE=1</code>). We measured real cold starts, warm session reuse,
and resume after idle using Foundry's native latency diagnostics; no artificial startup delays were added.</p></section>
<section class="paper" id="response-time-measurement"><h2>How did we measure response times?</h2>
<p>Think of a stopwatch on the computer sending the request. We measured how long it took the
real hosted agent to send back its small, useful reply.</p>
<ol>
<li><strong>Start the clock before the request.</strong> The test runner started a precise timer
immediately before asking the hosted agent to respond. For a cold-start request, Foundry created
the new session as part of that request; we did not create it separately beforehand and accidentally warm it up.</li>
<li><strong>Stop when the complete reply has arrived.</strong> The main response-time number includes
the request trip, any startup work, the agent's small reply, and the trip back. It stops after the
response body is received, before local parsing and checks. It is not just the time to receive headers
or the first network bytes, and it is not AI-model time to the first token.</li>
<li><strong>Check what kind of start actually happened.</strong> Foundry's own diagnostics had to
confirm cold, warm, or resumed execution. The runner also checked the returned session, version,
reply, and synthetic state. A new session alone was not enough to call a result a confirmed cold start.
Failed or unconfirmed attempts were not treated as successful comparison samples,
and requests were not automatically retried.</li>
<li><strong>Record application preparation separately.</strong> Foundry also reported how long
the application took to become ready inside the container. That is the preparation graph, not the
whole response journey. We did not add it to the response time because the two measurements overlap.
Any later read-only checks for delayed diagnostics happened outside the recorded successful response time.</li>
<li><strong>Repeat and compare the middle result.</strong> Each option had ten confirmed cold starts,
ten warm calls, and one return after idle. The headline compares the median of the ten cold starts
for each option. A median is the middle result; the detailed graphs also show the spread.
The single resumed request is a continuity check, not a reliable estimate of typical resume latency.</li>
</ol>
<p class="muted"><strong>Keep the comparison fair.</strong> The baseline and snapshot-only options used
the same image, SDKs, and compute; only snapshots changed. The optimized option used a different,
precompiled image. Conditions ran one after another, not as a randomized trial. There was no AI model
or external tool call, so these results describe this probe's startup, not a complete customer workflow,
a worst-case response time, or a service guarantee.</p></section>
<section class="columns" id="comparison-graphs" aria-label="Simple comparison graphs">
<figure class="paper"><h2>1. Time to the first response</h2>{response_chart}
<figcaption class="chart-note">Same image, compute, and SDKs for the first two options; only snapshots changed.
<a href="first-response-comparison.svg">Download this graph</a>.</figcaption></figure>
<figure class="paper"><h2>2. Time preparing the application</h2>{preparation_chart}
<figcaption class="chart-note">This is the application's readiness portion, not all infrastructure or network time.
<a href="preparation-comparison.svg">Download this graph</a>.</figcaption></figure>
</section>
<section class="paper"><h2>The numbers at a glance</h2><div class="table-scroll">
<table><thead><tr><th>Option</th><th>First response<br>(median)</th>
<th>Application preparation<br>(median)</th><th>Cold starts</th></tr></thead>
<tbody>{comparison_table}</tbody></table></div>
<p class="chart-note">The extra Python preparation is a separate image-build optimization.
Its smaller observed difference is not proof of a repeatable additional gain.</p></section>
<section class="paper conclusion"><h2>Conclusion</h2><p>{escape(conclusion)}</p>
<h3>Recommended next steps</h3><ol><li>Check that essential state is stored outside the agent container.</li>
<li>Consider Foundry's native memory snapshots with a supported SDK.</li>
<li>Repeat the measurement with the customer's actual model, tools, and workload before production.</li></ol></section>
<aside class="notice"><strong>Important state limitation.</strong> {escape(STATE_LIMIT)}
This experiment does not validate customer conversation latency, cross-user authorization,
or production reliability.</aside>
<nav class="download-links" aria-label="Report downloads"><a href="summary.md" download>Download the short summary</a>
<a href="evidence.json" download>Download the measured evidence</a>
<a href="#technical">See the technical details</a></nav>
<p class="footer">Transparency: {diagnostic_count} earlier failed diagnostic attempts are retained separately.
Graphs use valid measurements only. The CLI labels snapshot enablement as preview;
these observations are not an SLA.</p>
{issue_html or ""}<details class="paper technical" id="technical"{technical_open}>
<summary>Technical appendix: method, all measurements, and initial diagnostic failures</summary>
<p>Report generation is not experiment acceptance.
Local tests and synthetic fixtures are not hosted performance evidence.</p>
<h2>Analysis and limitations</h2><ul>{notes}</ul>
<p>Graphs include only passing attempts with matching native start classification. Every failed attempt remains below.
Absent timing values are unavailable, never zero. Warm readiness is normally not emitted.</p>
<h2>Comparison controls</h2><div class="table-scroll"><table><thead><tr>
<th>Condition</th><th>Version</th><th>CPU / memory</th>
<th>Snapshots</th><th>Immutable image</th></tr></thead><tbody>{controls}</tbody></table></div>
<h2>Detailed distributions</h2>{figures}
<h2>Full timing summary</h2><div class="table-scroll"><table><thead><tr>
<th>Condition</th><th>Start</th><th>Client journey</th><th>POST complete</th>
<th>Container readiness</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Every recorded attempt</h2><table><thead><tr><th>Condition</th><th>Trial</th><th>Requested</th>
<th>Observed</th><th>Result</th><th>Failure detail</th></tr></thead><tbody>{attempts}</tbody></table>
</div><p><a href="evidence.json">Download sanitized run controls and observations</a></p></details>
</main></body></html>"""
    return html, charts


def build_parser() -> argparse.ArgumentParser:
    """Require explicit saved evidence and a new output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+", required=True, help="One JSONL run per condition.")
    parser.add_argument(
        "--diagnostic-input",
        type=Path,
        nargs="*",
        default=[],
        help="Earlier failed capture/methodology runs, disclosed but never graphed as valid samples.",
    )
    parser.add_argument("--output", type=Path, required=True, help="New report directory; never overwritten.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Write reusable HTML/SVG/JSON without contacting Azure."""
    args = build_parser().parse_args(argv)
    try:
        runs = [load_run(path) for path in args.input]
        diagnostic_runs = [load_run(path) for path in args.diagnostic_input]
        html, charts = render(runs, diagnostic_runs)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "index.html").write_text(html, encoding="utf-8")
        (args.output / "summary.md").write_text(summary_markdown(runs, diagnostic_runs), encoding="utf-8")
        for name, content in charts.items():
            (args.output / name).write_text(content, encoding="utf-8")
        evidence = [
            {
                "role": "diagnostic" if diagnostic else "comparison",
                "run": run.info.model_dump(),
                "samples": [sample.model_dump() for sample in run.samples],
            }
            for run, diagnostic in [(run, False) for run in runs] + [(run, True) for run in diagnostic_runs]
        ]
        (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2, allow_nan=False), encoding="utf-8")
    except (OSError, ValueError, ValidationError) as error:
        logging.getLogger(__name__).error("Cannot generate report: %s", error)
        return 2
    print(args.output / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
