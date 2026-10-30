"""Generate Story6's final-results deep dive from retained sanitized evidence.

Run: python -m scripts.story6_report [--output DIRECTORY] [--overwrite].
No credentials, runners, private artifacts, cloud requests or tests are used.
The Markdown summary is authoritative; missing/invalid evidence fails closed.
Exit codes: 0 rendered (not an enforcement verdict), 1 I/O error, 2 invalid input.
"""

import argparse
import re
import sys
from collections import Counter
from collections.abc import Sequence
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import JsonValue

from scripts.generate_report import ReportError, pretty, read_bytes, read_object
from scripts.report_task_flows import build_task_flow
from scripts.story1_report import BundleRedactor, observed_fields

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path("docs") / "issue-6-explore-human-in-the-loop-tool-approval.md"
SNAPSHOTS = Path("docs") / "reports" / "story-6" / "human-in-the-loop-approval"
OUTPUT = SNAPSHOTS / "final-results"
EVIDENCE_SOURCE = Path("docs") / "validation" / "story6-final-evidence.json"
STATUSES = frozenset({"PASS", "FAIL", "BLOCKED", "SKIP"})

# Explicit evidence selection, not latest-run discovery or a deployment history.
BINDINGS = (
    ("rejection", "Human rejection prevents the selected action", "hosted"),
    ("pending-history", "Stored history before start and pending", "hosted"),
    ("pause-history", "Fresh-client discovery during approval pause", "hosted"),
    ("completion-history", "Stored decision and completed operation", "hosted"),
    ("foreign-approver", "Unauthorized user cannot decide the owner operation", "hosted"),
    ("owner-continuation", "Original authenticated owner continues", "hosted"),
    ("proxy-approval", "Hosted approval through authenticated toolbox", "hosted"),
    ("direct-access", "Direct token access and ordinary permission control", "direct"),
    ("entra-approval", "Hosted Entra MCP: approval executes once, without toolbox", "hosted"),
    ("entra-rejection", "Hosted Entra MCP: rejection prevents execution, without toolbox", "hosted"),
)
CRITERIA = (
    {
        "number": "1",
        "evidence": ["entra-approval", "entra-rejection", "rejection", "completion-history", "proxy-approval"],
        "flow": [
            "Requester invokes the dedicated Hosted coordinator.",
            "Coordinator calls unchanged Research for context.",
            "Native provider returns the proposed MCP tool and arguments without dispatching the pending call.",
            "Client reviews the actual request and explicitly approves or rejects.",
            "The original bound operation completes or rejects; provider-reported calls remain separately counted.",
            "Without toolbox, the custom Entra MCP independently records one approved effect and no rejected effect.",
        ],
        "task": (
            "Story6 MCP - Hosted approval test: start/inspect/approve; new start/reject. "
            "Match tool-server counts and receipts. Existing Step2 also reproduces public/toolbox cases."
        ),
    },
    {
        "number": "2",
        "evidence": ["pause-history", "completion-history"],
        "flow": [
            "A fresh client inspects the existing owner-bound operation.",
            "Coordinator reads authoritative pending state; inspection adds no model/tool execution.",
            "The explicit decision references that pending request, not a new operation.",
            "Logical operation phase, not generic response completion, determines whether review is still pending.",
        ],
        "task": "Step3 inspect; Step2 explicit decision. Polling, webhook and Event Grid assessment is included below.",
    },
    {
        "number": "3",
        "evidence": ["pending-history", "pause-history", "completion-history"],
        "flow": [
            "Read the actual empty conversation before submitting start.",
            "Read stored start/pending items and the original HOME-file hash.",
            "Inspect during the human pause using the same operation/session/conversation.",
            "Read the actual decision/completed items and verify the original file after continuation.",
        ],
        "task": "Step2 --capture-history, Step4 inspect, Step2 explicit decision; no injected process-loss test.",
    },
    {
        "number": "4",
        "evidence": ["direct-access", "proxy-approval"],
        "flow": [
            "Verify the isolated toolbox configuration and tools/list approval metadata.",
            "Prove the valid-token caller has ordinary direct tool permission.",
            "Send the fixed read-only tool call directly without any approval message.",
            "Record the returned result as an enforcement FAIL, not a denial or successful approval gate.",
            "Compare with an existing Consumer profile that lacks direct toolbox permission.",
        ],
        "task": (
            "Step5: fixed authenticated proxy, authorized direct control, "
            "no-approval call and Consumer permission control."
        ),
    },
    {
        "number": "5",
        "evidence": ["foreign-approver", "owner-continuation", "completion-history", "direct-access"],
        "flow": [
            "Verify two distinct actual cached CLI users and their existing Consumer access.",
            "User A creates a pending operation; User B reaches its own authorized coordinator context.",
            "B attempts foreign inspection/decision through A's session and B's own session.",
            "A verifies unchanged pending state, then explicitly continues with A's actual credential.",
            "Correlate the authenticated API caller, pinned bindings, stored pending/decision and reported invocation.",
        ],
        "task": (
            "Step6 with existing profiles; audit-enabled Steps2/4 for correlation; Step5 for proxy permission controls."
        ),
    },
)


def objects(value: JsonValue, label: str) -> list[dict[str, JsonValue]]:
    """Reject missing or incompatible JSON arrays rather than inventing empty evidence."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ReportError(f"{label} must be an array of objects.")
    return [item for item in value if isinstance(item, dict)]


def table(text: str, heading: str, headers: list[str]) -> list[list[str]]:
    """Read one existing Markdown table by its exact heading/header contract."""
    match = re.search(rf"^##?#?\s+{re.escape(heading)}\s*$", text, re.MULTILINE)
    if match is None:
        raise ReportError(f"Missing report section: {heading}.")
    section = re.split(r"^#{1,3}\s+", text[match.end() :], maxsplit=1, flags=re.MULTILINE)[0]
    lines = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    cells = [[value.strip() for value in line.strip("|").split("|")] for line in lines]
    if len(cells) < 3 or cells[0] != headers or any(len(row) != len(headers) for row in cells[2:]):
        raise ReportError(f"Incompatible {heading} table; review the summary contract.")
    return cells[2:]


def checked_run(
    source: dict[str, JsonValue], key: str, label: str, source_hash: str, redactor: BundleRedactor, kind: str
) -> dict[str, JsonValue]:
    """Re-sanitize retained exports using shared privacy/schema helpers, not raw dumps."""
    if source.get("error") is not None:
        raise ReportError("Selected evidence contains an input error.")
    provenance = source.get("provenance")
    if not isinstance(provenance, dict) or not re.fullmatch(r"story-6-step-[1-6]", str(provenance.get("scenario"))):
        raise ReportError("Selected evidence is not a recorded Story6 scenario.")
    mode = str(provenance.get("mode"))
    allowed = (
        {"hosted", "hosted-history-audit", "hosted-two-users"} if kind == "hosted" else {"authenticated-direct-mcp"}
    )
    if mode not in allowed:
        raise ReportError("Local/mock evidence cannot be relabeled as a final hosted or direct-call observation.")
    checks = objects(source.get("checks"), "Selected checks")
    if not checks:
        raise ReportError("Selected evidence has no checks.")
    projected: list[JsonValue] = []
    ordinals: set[int] = set()
    counts: Counter[str] = Counter()
    for check in checks:
        ordinal = check.get("ordinal")
        status = check.get("status")
        if (
            type(ordinal) is not int
            or ordinal < 1
            or ordinal in ordinals
            or not isinstance(status, str)
            or status not in STATUSES
        ):
            raise ReportError("Invalid/duplicate check ordinal or status.")
        if not isinstance(check.get("name"), str) or not isinstance(check.get("expected"), str):
            raise ReportError("A recorded check must include its name and expected result.")
        ordinals.add(ordinal)
        counts[str(status)] += 1
        projected.append(
            {
                "ordinal": ordinal,
                "name": redactor.text(str(check["name"])),
                "status": status,
                "expected": redactor.text(str(check["expected"])),
                "observed": observed_fields(check.get("observed"), redactor),
                "anchor": f"check-story6-{key}-{ordinal}",
            }
        )
    recorded_counts = source.get("counts")
    if recorded_counts != dict(counts):
        raise ReportError("Recorded status totals disagree with original checks.")
    references: list[JsonValue] = []
    for reference in objects(source.get("sources"), "Source references"):
        name, checksum = reference.get("name"), reference.get("sha256")
        if name not in {
            "manifest.json",
            "results.jsonl",
            "events.jsonl",
            "pending-results.jsonl",
            "mcp-health-before.json",
            "mcp-health-pending.json",
            "mcp-health-after.json",
            "mcp-tool-logs.jsonl",
            "mcp-health-reconciled.json",
        } or not isinstance(checksum, str):
            raise ReportError("Unexpected source reference in retained evidence.")
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ReportError("Source reference must retain its checksum.")
        references.append({"name": name, "sha256": checksum})
    if not references:
        raise ReportError("Selected evidence is missing its source receipts.")
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "counts": dict(counts),
        "checks": projected,
        "scope": redactor.text(str(source.get("scope", "Scope not supplied"))),
        "provenance": redactor.value(provenance),
        "agent_version": redactor.value(source.get("agent_version", "Not captured")),
        "completion": redactor.text(str(source.get("completion", "No finish-event evidence supplied"))),
        "sources": references,
        "snapshot_sha256": source_hash,
        "download": f"evidence/{key}.json",
        "task_flow": build_task_flow(provenance, objects(projected, "Checks"), prefix=f"story6-{key}"),
        "primary_criterion": next(str(item["number"]) for item in CRITERIA if key in item["evidence"]),
    }


def validate_final_findings(runs: list[JsonValue]) -> None:
    """Keep the reviewed narrative from contradicting its explicitly selected evidence."""
    indexed = {str(run["key"]): run for run in runs if isinstance(run, dict)}

    def observation(key: str, name: str) -> dict[str, JsonValue]:
        checks = objects(indexed[key]["checks"], "Checks")
        matches = [check for check in checks if check["name"] == name]
        if len(matches) != 1 or not isinstance(matches[0].get("observed"), dict):
            raise ReportError("Final finding is missing its exact recorded check.")
        return matches[0]

    for key, phase, calls in (("rejection", "rejected", 0), ("proxy-approval", "completed", 1)):
        check = observation(key, "Coordinator command")
        observed = check["observed"]
        if (
            not isinstance(observed, dict)
            or check["status"] != "PASS"
            or observed.get("phase") != phase
            or observed.get("mcp_calls_reported") != calls
        ):
            raise ReportError("Selected final tool outcome disagrees with the reviewed conclusion.")
        if key == "proxy-approval" and observed.get("mcp_route") != "foundry-toolbox":
            raise ReportError("Hosted proxy conclusion requires an actual authenticated toolbox-route observation.")
    direct = observation("direct-access", "Direct valid-token call without approval")
    observed = direct["observed"]
    if (
        not isinstance(observed, dict)
        or direct["status"] != "FAIL"
        or observed.get("unapproved_result_returned") is not True
    ):
        raise ReportError("The reviewed direct-call enforcement FAIL must remain backed by its original result.")
    expected_history = (
        ("pending-history", "Conversation history before command", 0),
        ("pending-history", "Conversation history after command", 2),
        ("pause-history", "Conversation history after command", 4),
        ("completion-history", "Conversation history after command", 6),
    )
    for key, name, count in expected_history:
        check = observation(key, name)
        observed = check["observed"]
        if (
            not isinstance(observed, dict)
            or check["status"] != "PASS"
            or observed.get("items") != count
            or observed.get("operation_mismatches") != 0
        ):
            raise ReportError("Final history finding lacks its actual stored-item evidence.")
    final = observation("completion-history", "Conversation history after command")["observed"]
    if not isinstance(final, dict) or final.get("decision_reference_matches_pending") is not True:
        raise ReportError("Stored decision is not established as matching the pending request.")
    for key in ("foreign-approver", "owner-continuation"):
        if any(check["status"] != "PASS" for check in objects(indexed[key]["checks"], "Checks")):
            raise ReportError("Owner/foreign-user conclusion disagrees with recorded access checks.")
    for key, phase, expected_before, expected_delta in (
        ("entra-approval", "completed", 0, 1),
        ("entra-rejection", "rejected", 1, 0),
    ):
        command = observation(key, "Coordinator command")
        pause = observation(key, "Entra MCP approval pause")
        receipts = observation(key, "Entra MCP execution evidence")
        result, pending, effects = command["observed"], pause["observed"], receipts["observed"]
        if (
            any(check["status"] != "PASS" for check in (command, pause, receipts))
            or not isinstance(result, dict)
            or not isinstance(pending, dict)
            or not isinstance(effects, dict)
            or result.get("mcp_route") != "entra-mcp"
            or result.get("phase") != phase
            or result.get("decision") is not bool(expected_delta)
            or result.get("mcp_calls_reported") != expected_delta
            or pending.get("phase") != "pending"
            or pending.get("server_total_before") != expected_before
            or pending.get("server_total_pending") != expected_before
            or pending.get("same_server_epoch") is not True
            or pending.get("case_id_matches_operation") is not True
            or effects.get("server_total_before") != expected_before
            or effects.get("server_total_after") != expected_before + expected_delta
            or effects.get("execution_delta") != expected_delta
            or effects.get("matching_receipts") != expected_delta
            or effects.get("same_server_epoch") is not True
            or effects.get("remote_execution_independently_verified") is not True
        ):
            raise ReportError("Custom Entra MCP conclusion requires actual terminal state and independent receipts.")


def build_report(root: Path) -> dict[str, JsonValue]:
    """Combine the final Markdown conclusions with explicitly selected sanitized observations."""
    markdown_bytes = read_bytes(root / REPORT)
    markdown = markdown_bytes.decode("utf-8")
    redactor = BundleRedactor()
    summary = table(
        markdown,
        "Summary",
        [
            "#",
            "Requirement",
            "Status",
            "What works",
            "What does not work, and why",
            "Next step",
        ],
    )
    if [row[0] for row in summary] != ["1", "2", "3", "4", "5"]:
        raise ReportError("Summary must cover the five issue criteria exactly once and in order.")
    runs: list[JsonValue] = []
    source_bytes = read_bytes(root / EVIDENCE_SOURCE)
    evidence = read_object(root / EVIDENCE_SOURCE)
    if (
        evidence.get("schema") != "story6-final-evidence-v1"
        or type(evidence.get("story")) is not int
        or evidence["story"] != 6
    ):
        raise ReportError("Select the retained Story6 final-evidence input, not another story or report.")
    source_hash = sha256(source_bytes).hexdigest()
    retained = objects(evidence.get("runs"), "Retained final observations")
    identities: set[str] = set()
    for key, label, kind in BINDINGS:
        if key in identities:
            raise ReportError("Duplicate final evidence binding.")
        identities.add(key)
        matches = [item for item in retained if item.get("key") == key]
        if len(matches) != 1:
            raise ReportError(f"Missing or duplicated final observation: {key}.")
        runs.append(checked_run(matches[0], key, label, source_hash, redactor, kind))
    if len(retained) != len(BINDINGS):
        raise ReportError("Retained evidence contains unrelated observations.")
    validate_final_findings(runs)
    criteria: list[JsonValue] = []
    for row, definition in zip(summary, CRITERIA, strict=True):
        criteria.append(
            {
                "number": row[0],
                "requirement": redactor.text(row[1]),
                "status": redactor.text(row[2]),
                "works": redactor.text(row[3]),
                "limitations": redactor.text(row[4]),
                "next_step": redactor.text(row[5]),
                "evidence": list(definition["evidence"]),
                "flow": list(definition["flow"]),
                "task": str(definition["task"]),
            }
        )
    # Keep public results central; dates/versions remain inside source details only.
    template = (
        files("scripts.report_assets").joinpath("story6.html").read_bytes()
        + files("scripts.report_assets").joinpath("task_flow.html").read_bytes()
    )
    return {
        "schema": "story6-final-results-v1",
        "story": 6,
        "title": "Story6 | Hosted tool approval - final results",
        "conclusion": (
            "Hosted approval, rejection, discovery and owner controls worked. "
            "Direct Entra MCP also paused, executed once after approval and did not execute after rejection. "
            "Direct authorized toolbox calls bypassed human approval. "
            "Universal approval enforcement is not achieved."
        ),
        "criteria": criteria,
        "runs": runs,
        "notification_headers": ["Mechanism", "What is established", "Limitation"],
        "notifications": [
            [redactor.text(cell) for cell in row]
            for row in table(markdown, "Notification availability", ["Mechanism", "What is established", "Limitation"])
        ],
        "ownership": [
            [redactor.text(cell) for cell in row]
            for row in table(markdown, "Capability ownership", ["Capability", "Owner / assessment"])
        ],
        "report_sha256": sha256(markdown_bytes).hexdigest(),
        "source_evidence": {EVIDENCE_SOURCE.as_posix(): source_hash},
        "template_sha256": sha256(template).hexdigest(),
        "counts": dict(
            Counter(
                str(check["status"])
                for run in runs
                if isinstance(run, dict)
                for check in objects(run["checks"], "Checks")
            )
        ),
        "limitations": [
            "Direct-call FAIL is an observed enforcement gap, not a failing report generator.",
            "Foundry toolbox proxy is authenticated; downstream Microsoft Learn is public and read-only.",
            (
                "The separate custom Entra MCP authenticates the workload; "
                "it does not enforce human approval on direct calls."
            ),
            "Conversation/operation lineage is not a joined distributed trace or downstream original-user delegation.",
            "Human chat review is distinct from the authenticated API decision caller.",
            "Recorded scenarios have separate source bindings; they are not a combined rerun on one deployment.",
            "No new cloud calls, tests or reconstructed history are generated by this report.",
        ],
    }


def generate_report(root: Path, output: Path, *, overwrite: bool = False) -> Path:
    """Write a standalone escaped report; never overwrite inputs, private paths or unrelated files."""
    root = root.resolve()
    output = output.absolute()
    resolved = output.resolve()
    if (
        output.is_symlink()
        or output.is_junction()
        or not (
            (resolved != root / "artifacts" and resolved.is_relative_to(root / "artifacts"))
            or resolved == (root / OUTPUT).resolve()
        )
    ):
        raise ReportError("Output must be below artifacts or the fixed Story6 final-results snapshot.")
    bundle = build_report(root)
    fingerprint = sha256(pretty(bundle).encode()).hexdigest()
    bundle["source_fingerprint"] = fingerprint
    expected = {"index.html", "sanitized-evidence.json", "evidence"}
    evidence_names = {f"{entry[0]}.json" for entry in BINDINGS}
    if output.exists():
        if any(path.name not in expected or path.is_symlink() or path.is_junction() for path in output.iterdir()):
            raise ReportError("Existing output contains unrelated files or links.")
        if not overwrite or read_object(output / "sanitized-evidence.json").get("source_fingerprint") != fingerprint:
            raise ReportError("Existing output requires --overwrite with unchanged Markdown, template and evidence.")
        if (
            not (output / "evidence").is_dir()
            or (output / "evidence").is_symlink()
            or (output / "evidence").is_junction()
        ):
            raise ReportError("Existing evidence directory is missing.")
        if any(
            path.name not in evidence_names or not path.is_file() or path.is_symlink() or path.is_junction()
            for path in (output / "evidence").iterdir()
        ):
            raise ReportError("Existing evidence contains unrelated files or links.")
    environment = Environment(
        loader=PackageLoader("scripts.report_assets", package_path=""),
        autoescape=True,
        undefined=StrictUndefined,
    )
    environment.filters["json_pretty"] = pretty
    html = environment.get_template("story6.html").render(**bundle)
    (output / "evidence").mkdir(parents=True, exist_ok=True)
    for run in bundle["runs"] if isinstance(bundle["runs"], list) else []:
        if isinstance(run, dict):
            (output / str(run["download"])).write_text(pretty(run), encoding="utf-8")
    (output / "sanitized-evidence.json").write_text(pretty(bundle), encoding="utf-8")
    result = output / "index.html"
    result.write_text(html, encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    """Keep the repeatable offline command inspectable without reading evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Generate only final retained results; no raw-artifact configuration is needed after cloning."""
    args = build_parser().parse_args(argv)
    try:
        print(generate_report(ROOT, args.output, overwrite=args.overwrite))
    except ReportError as error:
        print(f"Story6 report rejected: {BundleRedactor().text(str(error))}", file=sys.stderr)
        return 2
    except UnicodeError, ValueError:
        print("Story6 report rejected: malformed source evidence or Markdown.", file=sys.stderr)
        return 2
    except OSError:
        print("Story6 report I/O error; verify local files and permissions.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
