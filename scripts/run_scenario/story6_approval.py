"""Story6 previews, offline SDK qualification and evidence reporting.

Hosted public/proxy approval, direct proxy and two-user access paths are
implemented. Evidence retains failed enforcement checks independently of
successful execution of the evaluation.
Exit codes: 0 preview/report succeeded, 1 observed failure, 2 blocked/invalid.
"""

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from scripts.common.reporting import (
    REPORT_FILE,
    MarkdownReport,
    Status,
    StepSpec,
    ValidationRun,
    add_output_arguments,
    index_entry,
    update_index,
)

ISSUE_REPORT = "docs/issue-6-explore-human-in-the-loop-tool-approval.md"
SPECS = {
    1: StepSpec(6, 1, "approval-readiness"),
    2: StepSpec(6, 2, "approval-and-rejection"),
    3: StepSpec(6, 3, "pending-discovery"),
    4: StepSpec(6, 4, "pause-and-resume"),
    5: StepSpec(6, 5, "direct-mcp-boundary"),
    6: StepSpec(6, 6, "caller-and-approver-access"),
    7: StepSpec(6, 7, "evidence-report"),
}
GOALS = {
    1: "Qualify locked SDK approval contracts; verify the separately approved hosted baseline later.",
    2: "Demonstrate actual human approval/rejection on the new coordinator after unchanged Research context.",
    3: "Discover the original pending approval from a fresh client and submit one bound decision.",
    4: "Observe execution/history and one file across a short approval pause and continuation.",
    5: "Compare authorized direct MCP access with valid-token/no-approval execution or rejection.",
    6: "Verify allowed/denied tool access and one genuinely unauthorized approver.",
    7: "Render explicitly selected saved Story6 evidence offline; maintain the reviewed issue report.",
}
LIVE_BLOCKER = (
    "Hosted execution needs an explicit environment and case binding. "
    "Verify the isolated deployment and runtime permissions before live execution. "
    "The selected path is native Responses MCP, not the retrying ordinary wrapper. "
    "Hosted/proxy cases have separate version-bound evidence; a missing environment or actor "
    "binding is not permission to fabricate another case. No deployment, sign-in, grant or cloud "
    "request was attempted by this blocked invocation."
)


def build_parser() -> argparse.ArgumentParser:
    """Expose safe previews and explicitly offline qualification/report operations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, required=True, choices=sorted(SPECS))
    parser.add_argument("--execution", choices=("preview", "qualify", "run"), default="preview")
    parser.add_argument("--environment", default="not-selected")
    parser.add_argument("--max-seconds", type=float, default=120)
    parser.add_argument("--action", choices=("start", "inspect", "approve", "reject"), default="start")
    parser.add_argument("--case-state", default="")
    parser.add_argument(
        "--expected-mcp-route",
        choices=("public-learn", "foundry-toolbox", "entra-mcp"),
        help="Fail rather than validate a different deployed route.",
    )
    parser.add_argument(
        "--capture-history", action="store_true", help="Read actual conversation items and caller correlation."
    )
    parser.add_argument("--users", default="", help="Exactly two existing isolated CLI profile users for Step6.")
    parser.add_argument("--caller-user", default="", help="Existing Consumer CLI profile for Step5 permission control.")
    parser.add_argument("--bundle-config", type=Path)
    parser.add_argument("--overwrite", action="store_true", help="Existing renderer's unchanged-source overwrite only.")
    add_output_arguments(parser)
    return parser


def preview(args: argparse.Namespace) -> dict[str, object]:
    """Describe the selected operation without resolving settings or credentials."""
    return {
        "story": 6,
        "step": args.step,
        "goal": GOALS[args.step],
        "execution": "preview",
        "azure_requests": 0,
        "model_requests": 0,
        "mcp_requests": 0,
        "deployment": False,
        "role_changes": False,
        "hosted_ready": False,
        "local_qualification": "--step 1 --execution qualify (synthetic peers only)",
        "live_blocker": LIVE_BLOCKER,
        "issue_report": ISSUE_REPORT,
    }


def record_run(args: argparse.Namespace) -> int:
    """Record actual local observations or an explicit pre-network live blocker."""
    spec = SPECS[args.step]
    root = args.output_dir / "story-6"
    mode = "offline-sdk-qualification" if args.execution == "qualify" else "blocked-before-hosted-execution"
    run = ValidationRun(spec.scenario, mode, root, step=spec, no_color=args.no_color, verbose=args.verbose)
    report = MarkdownReport(run.path / REPORT_FILE, f"Story 6 / Step {spec.number} - {mode}")

    def record(name: str, status: Status, expected: str, observed: object) -> None:
        run.result(name, status, expected=expected, observed=observed)
        report.table(["Check", "Status", "Expected", "Observed"], [[name, status, expected, observed]])

    if args.execution == "qualify":
        from scripts.run_scenario.story6_qualification import package_versions, qualify

        report.paragraph(
            "Real SDK / synthetic peers only. No human, authentication or Foundry service behavior tested."
        )
        report.table(["Package", "Installed version"], [[name, value] for name, value in package_versions().items()])

        async def bounded() -> None:
            async with asyncio.timeout(args.max_seconds):
                for check in await qualify():
                    record(check.name, check.status, check.expected, check.observed)

        try:
            asyncio.run(bounded())
        except (OSError, ValueError, TimeoutError) as error:
            record(
                "Local qualification stopped",
                "FAIL",
                "Bounded synthetic SDK qualification completes",
                {"reason": type(error).__name__, "retry": False},
            )
    record(
        "Hosted prerequisite", "BLOCKED", "Approved authenticated fixture and hosted continuation path", LIVE_BLOCKER
    )
    verdict = "Local contract gap; hosted not evaluated" if "FAIL" in run.statuses else "Hosted not evaluated"
    report.summary(f"**{verdict}.** Local PASS checks are not hosted approval acceptance.")
    bundle_config = {
        "schema": "story-report-bundle-v1",
        "story": 6,
        "title": "Story6 client qualification - hosted not evaluated",
        "environment": "Offline; no Azure environment evaluated",
        "runs": [
            {
                "key": "qualification" if args.execution == "qualify" else f"blocked-step-{spec.number}",
                "label": spec.label(mode),
                "run_dir": str(run.path.resolve()),
                "scope": mode + "; no human or live service approval evidence",
                "agent_version": "Not captured by this local run",
                "kind": mode,
            }
        ],
        "decisions": [],
        "traceability": [
            {
                "requirement": "Issue 6 approval lifecycle and enforcement",
                "status": "Not evaluated on hosted agents",
                "summary": verdict,
                "limitations": LIVE_BLOCKER,
                "next_step": "Verify the isolated coordinator deployment/access and wire the live cases.",
                "run_keys": ["qualification" if args.execution == "qualify" else f"blocked-step-{spec.number}"],
            }
        ],
    }
    config_path = run.path / "bundle-config.json"
    config_path.write_text(json.dumps(bundle_config, indent=2), encoding="utf-8")
    report.paragraph(f"Offline HTML input: {config_path.name}. Maintainer-reviewed issue report: {ISSUE_REPORT}.")
    report.render(run.console)
    index = root / "index.md"
    update_index(index, index_entry(index, spec.label(mode), verdict, run.path), "Story 6 evidence index")
    run.console.print(f"Offline HTML bundle config: {config_path}")
    return run.finish()


def render_saved_report(args: argparse.Namespace) -> int:
    """Render only an explicitly selected Story6 bundle with the existing engine."""
    from scripts import story_report
    from scripts.generate_report import ReportError, read_object

    try:
        config = read_object(args.bundle_config)
        if config.get("schema") != "story-report-bundle-v1" or type(config.get("story")) is not int:
            raise ReportError("Select an explicit story-report-bundle-v1 Story6 configuration.")
        if config["story"] != 6:
            raise ReportError("Story6 cannot relabel another story's saved evidence.")
    except (OSError, ValueError) as error:
        print(f"Story6 report input rejected: {type(error).__name__}. Select a valid Story6 bundle config.")
        return 2
    output = args.output_dir / "story-6" / "html"
    command = ["--input", str(args.bundle_config), "--output", str(output)]
    if args.overwrite:
        command.append("--overwrite")
    result = story_report.main(command)
    if result == 0:
        print(f"Maintainer-reviewed Markdown issue report: {ISSUE_REPORT}")
        print("Rendering saved checks does not run tests or satisfy hosted acceptance.")
    return result


def hosted_case(args: argparse.Namespace) -> int:
    """Record a real command while keeping the private locator out of report exports."""
    from azure.core.exceptions import AzureError
    from openai import OpenAIError

    from scripts.deploy.grant_local_rbac import CliError
    from scripts.run_scenario import story6_hosted

    spec = SPECS[args.step]
    root = args.output_dir / "story-6"
    run = ValidationRun(spec.scenario, "hosted", root, step=spec, no_color=args.no_color, verbose=args.verbose)
    report = MarkdownReport(run.path / REPORT_FILE, f"Story 6 / Step {spec.number} - {args.action}")
    state_file = Path(args.case_state) if args.case_state else run.path / "case-state.json"
    version = "Not captured by this run"
    try:

        def capture(name: str, observed: dict[str, object]) -> None:
            mismatch = observed.get("operation_mismatches", 0)
            matches = observed.get("decision_reference_matches_pending")
            ok = mismatch == 0 and matches is not False
            run.result(
                name,
                "PASS" if ok else "FAIL",
                expected="Actual stored items and original caller/operation binding",
                observed=observed,
            )
            report.table(["Check", "Observed"], [[name, observed]])
            if not ok:
                raise ValueError("Stored history has a mismatched operation or approval binding; audit stopped.")

        async def bounded() -> tuple[dict[str, object], str]:
            async with asyncio.timeout(args.max_seconds):
                reply = await story6_hosted.execute(
                    args.environment,
                    args.action,
                    state_file,
                    timeout=args.max_seconds,
                    capture_history=args.capture_history,
                    expected_route=args.expected_mcp_route,
                    evidence=capture,
                )
                return story6_hosted.public_observation(reply), story6_hosted.load(state_file, args.environment).version

        observed, version = asyncio.run(bounded())
        run.result("Coordinator command", "PASS", expected=GOALS[args.step], observed=observed)
        report.table(["Observed", "Value"], [[key, value] for key, value in observed.items()])
    except (AzureError, OpenAIError, CliError, OSError, ValueError, TimeoutError) as error:
        from app.core.redaction import sanitize_text

        observed_error = {"reason": sanitize_text(str(error)), "exception": type(error).__name__, "retry": False}
        run.result("Coordinator command", "FAIL", expected="One bounded command; no replay", observed=observed_error)
        report.table(["Failure", "Details"], [["Coordinator command", observed_error]])
    report.summary(
        "Real hosted command only; pending is not approval and model-reported calls are not independent effects."
    )
    report.render(run.console)
    run.console.print(f"Private case locator (do not publish): {state_file}")
    index = root / "index.md"
    update_index(
        index, index_entry(index, spec.label(args.action), "Recorded command", run.path), "Story 6 evidence index"
    )
    write_hosted_bundle(run, args.environment, version, spec.label(args.action))
    return run.finish()


def write_hosted_bundle(run: ValidationRun, environment: str, version: str, label: str) -> None:
    """Bind only the saved run's standard evidence files, never its private locator."""
    config = {
        "schema": "story-report-bundle-v1",
        "story": 6,
        "title": "Story6 hosted command observations",
        "environment": environment,
        "runs": [
            {
                "key": "command",
                "label": label,
                "run_dir": str(run.path.resolve()),
                "scope": "One recorded command; not a full acceptance verdict.",
                "agent_version": version,
                "kind": "hosted-command",
            }
        ],
        "decisions": [],
        "traceability": [
            {
                "requirement": "Issue6 approval/state observations",
                "status": "Recorded command only",
                "summary": "Read the recorded phase; pending and transport completion are not approval success.",
                "limitations": (
                    "Remote effects are not independently observed; authenticated-tool criteria remain open."
                ),
                "next_step": "Review the original case and its actual pending decision.",
                "run_keys": ["command"],
            }
        ],
    }
    path = run.path / "bundle-config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    run.console.print(f"Offline HTML input: {path}")


def hosted_readiness(args: argparse.Namespace) -> int:
    """Record metadata/role presence only, never infer effective invocation rights."""
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import AzureError
    from azure.identity import AzureCliCredential

    from scripts.deploy.grant_local_rbac import CliError
    from scripts.deploy.story6_access import inspect_role, runtime_identity
    from scripts.deploy.story6_package import ROOT, deployment_context

    spec = SPECS[1]
    root = args.output_dir / "story-6"
    run = ValidationRun(spec.scenario, "hosted-metadata", root, step=spec, no_color=args.no_color)
    report = MarkdownReport(run.path / REPORT_FILE, "Story6 deployment/access readiness")
    try:
        context = deployment_context(ROOT / ".azure" / args.environment / ".env", args.environment)
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(
                endpoint=context["AZURE_AI_PROJECT_ENDPOINT"],
                credential=credential,
                allow_preview=True,
                retry_total=0,
                connection_timeout=15,
                read_timeout=25,
            ) as project,
        ):
            identity = runtime_identity(project)
            grant = inspect_role(context, identity)
        observed = {
            "version": identity.version,
            "status": "active",
            "role_present": grant.present,
            "effective_runtime_access_tested": False,
        }
        status: Status = "PASS" if grant.present else "BLOCKED"
        run.result(
            "Deployment/access metadata",
            status,
            expected="Active isolated coordinator and runtime role",
            observed=observed,
        )
        report.table(["Status", "Observed"], [[status, observed]])
    except (AzureError, CliError, OSError, ValueError) as error:
        run.result(
            "Deployment/access metadata", "FAIL", expected="Readable verified deployment", observed=type(error).__name__
        )
        report.paragraph(f"Readiness failed: {type(error).__name__}. No invocation attempted.")
    report.summary("Metadata/assignment presence only; not a hosted approval-flow verdict.")
    report.render(run.console)
    index = root / "index.md"
    update_index(index, index_entry(index, spec.label(), "Metadata recorded", run.path), "Story 6 evidence index")
    write_hosted_bundle(run, args.environment, "See metadata check", spec.label())
    return run.finish()


def hosted_user_access(args: argparse.Namespace) -> int:
    """Run distinct-profile access tests on the deployed host, not local identity doubles."""
    import subprocess

    from azure.core.exceptions import AzureError
    from openai import OpenAIError

    from scripts.common.auth_profiles import ProfileError
    from scripts.deploy.story6_package import ROOT
    from scripts.run_scenario import story6_users

    spec = SPECS[6]
    root = args.output_dir / "story-6"
    run = ValidationRun(spec.scenario, "hosted-two-users", root, step=spec, no_color=args.no_color)
    report = MarkdownReport(run.path / REPORT_FILE, "Story6 two-user hosted approval access")
    state_file = Path(args.case_state) if args.case_state else run.path / "case-state.json"
    if not state_file.resolve().is_relative_to((ROOT / "artifacts" / "story-6").resolve()):
        raise ValueError("Keep the private case locator under ignored artifacts/story-6.")
    version = "Not captured"

    def record(name: str, status: Status, expected: str, observed: object) -> None:
        run.result(name, status, expected=expected, observed=observed)
        report.table(["Check", "Status", "Expected", "Observed"], [[name, status, expected, observed]])

    try:

        async def bounded() -> str:
            async with asyncio.timeout(args.max_seconds):
                return await story6_users.execute(
                    args.environment,
                    args.users,
                    args.action,
                    state_file,
                    record,
                    timeout=min(args.max_seconds, 120),
                )

        version = asyncio.run(bounded())
    except (
        AzureError,
        OpenAIError,
        ProfileError,
        OSError,
        ValueError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        from app.core.redaction import sanitize_text

        record(
            "Two-user validation stopped",
            "FAIL",
            "Explicit allow/deny evidence, no replacement or retries",
            {"reason": sanitize_text(str(error)), "exception": type(error).__name__, "retry": False},
        )
    report.summary(
        "Two real authenticated profile users against the deployed coordinator. "
        "Session visibility and custom ownership are separate from protected-MCP approval enforcement. "
        "A pending owner case requires a separately chosen owner decision."
    )
    report.render(run.console)
    run.console.print(f"Private User A case locator (do not publish): {state_file}")
    index = root / "index.md"
    update_index(
        index,
        index_entry(index, spec.label(args.action), "Recorded two-user checks", run.path),
        "Story 6 evidence index",
    )
    write_hosted_bundle(run, args.environment, version, spec.label(args.action))
    return run.finish()


def direct_mcp_boundary(args: argparse.Namespace) -> int:
    """Keep actual direct-call enforcement failures visible rather than roll up success."""
    import subprocess

    import httpx
    from azure.core.exceptions import AzureError

    from scripts.common.auth_profiles import ProfileError
    from scripts.run_scenario import story6_boundary

    spec = SPECS[5]
    root = args.output_dir / "story-6"
    run = ValidationRun(spec.scenario, "authenticated-direct-mcp", root, step=spec, no_color=args.no_color)
    report = MarkdownReport(run.path / REPORT_FILE, "Story6 authenticated proxy approval boundary")
    version = "Not captured"

    def record(name: str, status: Status, expected: str, observed: object) -> None:
        run.result(name, status, expected=expected, observed=observed)
        report.table(["Check", "Status", "Expected", "Observed"], [[name, status, expected, observed]])

    try:

        async def bounded() -> str:
            async with asyncio.timeout(args.max_seconds):
                return await story6_boundary.execute(
                    args.environment, args.caller_user, record, timeout=min(args.max_seconds, 60)
                )

        version = asyncio.run(bounded())
    except (
        AzureError,
        ProfileError,
        httpx.HTTPError,
        OSError,
        ValueError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        record(
            "Direct probe stopped",
            "FAIL",
            "Explicit observed permission/approval boundary; no resend",
            {"exception": type(error).__name__, "retry": False},
        )
    report.summary(
        "Direct calls use the explicitly selected Entra-authenticated Foundry proxy; upstream Learn is public. "
        "A returned unapproved result is an enforcement gap and remains FAIL, even when evaluation was completed. "
        "No credential is sent directly to public Learn."
    )
    report.render(run.console)
    index = root / "index.md"
    update_index(
        index, index_entry(index, spec.label(), "Recorded direct boundary", run.path), "Story 6 evidence index"
    )
    write_hosted_bundle(run, args.environment, "No agent invocation; toolbox " + version, spec.label())
    return run.finish()


def main(argv: Sequence[str] | None = None) -> int:
    """Preview by default; never turn local results into a hosted success."""
    parser = build_parser()
    args = parser.parse_args(argv)
    max_seconds = 600 if args.step in (5, 6) else 120
    if not 0 < args.max_seconds <= max_seconds:
        parser.error(f"--max-seconds must be greater than zero and at most {max_seconds}.")
    if args.users:
        from scripts.common.auth_profiles import parse_users

        try:
            if args.step != 6 or len(parse_users(args.users)) != 2:
                raise ValueError("Use exactly two existing profile users with Step6 only.")
        except ValueError as error:
            parser.error(str(error))
        if args.execution == "run":
            if args.environment == "not-selected":
                parser.error("Step6 requires the explicit environment.")
            if args.action != "start" and not args.case_state:
                parser.error("Owner inspect/decisions require the existing --case-state.")
            if args.action == "start" and args.case_state:
                parser.error("Start creates a new two-user case; leave --case-state empty.")
    if args.caller_user:
        from scripts.common.auth_profiles import parse_users

        try:
            if args.step != 5 or len(parse_users(args.caller_user)) != 1:
                raise ValueError("Use one --caller-user only with Step5.")
        except ValueError as error:
            parser.error(str(error))
    if args.execution == "qualify" and args.step != 1:
        parser.error("Offline SDK qualification is available only for --step 1.")
    if args.bundle_config is not None and args.step != 7:
        parser.error("--bundle-config is only valid for --step 7.")
    if args.overwrite and (args.step != 7 or args.execution != "run"):
        parser.error("--overwrite is only valid for an explicit Step 7 report build.")
    if args.capture_history and args.step not in (2, 3, 4):
        parser.error("--capture-history applies only to the operator-owned Step2/3/4 case flow.")
    if args.execution == "run" and args.environment != "not-selected" and args.step in (2, 3, 4):
        if args.action != "start" and not args.case_state:
            parser.error("Inspect/approve/reject require the exact --case-state printed by start.")
        if args.action == "start" and (args.step != 2 or args.case_state):
            parser.error("Only Step 2 starts a fresh case; leave --case-state empty for start.")
    if args.execution == "preview":
        print(json.dumps(preview(args), indent=2))
        return 0
    if args.execution == "run" and args.environment != "not-selected" and args.step == 1:
        return hosted_readiness(args)
    if args.execution == "run" and args.step == 6 and args.users:
        return hosted_user_access(args)
    if args.execution == "run" and args.step == 5 and args.environment != "not-selected":
        return direct_mcp_boundary(args)
    if args.step == 7:
        if args.bundle_config is None:
            parser.error("Step 7 requires --bundle-config; unrelated runs are never auto-discovered.")
        return render_saved_report(args)
    if args.execution == "run" and args.environment != "not-selected" and args.step in (2, 3, 4):
        return hosted_case(args)
    return record_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
