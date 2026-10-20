"""Compare direct and nested hosted context without models or workflow-state writes.

Exit codes: 0 complete context and matching caller, 1 observed failure,
2 invalid invocation or prerequisite failure.
"""

import argparse
import subprocess
from collections.abc import Sequence
from time import perf_counter

from app.services.delegation import ContextCommand, ContextReport
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from openai import APIStatusError, OpenAI, OpenAIError
from rich.table import Table

from scripts.common.http_details import http_details
from scripts.common.reporting import Status, ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import CliError, native_json, object_value
from scripts.run_scenario.hosted_workflow import verify_idle_timeout


def build_parser() -> argparse.ArgumentParser:
    """Expose the single-hop diagnostic separately from full workflow validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--timeout", type=float, default=240)
    add_output_arguments(parser)
    return parser


def invoke(client: OpenAI, agent: str, command: ContextCommand, run: ValidationRun) -> ContextReport:
    """Send one explicit probe and record only safe HTTP metadata and boolean results."""
    number = run.start_request(agent, "hosted", command.wire())
    started = perf_counter()
    status: Status = "FAIL"
    try:
        conversation = client.conversations.create()
        raw = client.responses.with_raw_response.create(
            input=command.wire(), conversation=conversation.id, stream=False
        )
        response = raw.parse()
        http = http_details(
            raw.http_request.method,
            str(raw.http_request.url),
            raw.http_request.headers,
            status_code=raw.status_code,
            response_headers=raw.headers,
        )
        if response.status != "completed" or response.error:
            run.result(
                agent,
                "FAIL",
                expected="Read-only context probe response",
                observed={"http": http, "error": response.error.model_dump() if response.error else None},
            )
            raise ValueError("Context probe did not complete; inspect the recorded protocol error.")
        report = ContextReport.model_validate_json(response.output_text)
        if report.agent != agent:
            raise ValueError("Context probe came from an unexpected agent.")
        run.result(
            agent,
            "PASS",
            expected="Diagnostic executed; booleans observed, not proof of workflow authorization",
            observed={"http": http, "diagnostic": report.model_dump(mode="json")},
        )
        status = "PASS"
        return report
    except APIStatusError as error:
        run.result(
            agent,
            "FAIL",
            expected="Successful hosted context-probe transport",
            observed={
                "http": http_details(
                    error.request.method,
                    str(error.request.url),
                    error.request.headers,
                    status_code=error.status_code,
                    response_headers=error.response.headers,
                ),
                "error": str(error),
            },
        )
        raise
    finally:
        run.end_request(number, agent, status, (perf_counter() - started) * 1000)


def compare(direct: ContextReport, supervisor: ContextReport, run: ValidationRun) -> bool:
    """Show exact missing checks without returning raw identities or call IDs."""
    nested = supervisor.research
    if direct.agent != "research" or supervisor.agent != "supervisor" or nested is None or nested.agent != "research":
        raise ValueError("Expected direct Research and exactly one Supervisor-to-Research probe.")
    table = Table(title="Verified context presence: direct versus nested")
    for column in ("Check", "Supervisor", "Research direct", "Research nested"):
        table.add_column(column)
    root_flags = supervisor.presence.model_dump()
    direct_flags = direct.presence.model_dump()
    nested_flags = nested.presence.model_dump()
    for key, value in root_flags.items():
        table.add_row(
            key,
            "present" if value else "MISSING",
            "present" if direct_flags[key] else "MISSING",
            "present" if nested_flags[key] else "MISSING",
        )
    table.add_row("Expected caller matches", "n/a", "n/a", str(nested.caller_matches_expected))
    passed = all(root_flags.values()) and all(direct_flags.values()) and all(nested_flags.values())
    passed = passed and nested.caller_matches_expected is True
    run.result(
        "Direct versus nested context",
        "PASS" if passed else "FAIL",
        expected="Every required context field present and exact nested caller match",
        observed={
            "direct": direct.model_dump(mode="json"),
            "supervisor": supervisor.model_dump(mode="json"),
            "missing_in_nested": [key for key, value in nested_flags.items() if not value],
            "caller_matches_expected": nested.caller_matches_expected,
            "model_inference_requested": False,
            "workflow_state_writes_requested": False,
        },
        summary_table=table,
    )
    return bool(passed)


def main(argv: Sequence[str] | None = None) -> int:
    """Run only the two caller-context requests plus the single nested diagnostic."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "hosted-context-comparison", "hosted", args.output_dir, no_color=args.no_color, verbose=args.verbose
    )
    if args.timeout <= 0:
        run.result("Configuration", "BLOCKED", expected="Positive timeout", observed="--timeout must be positive.")
        return run.finish()
    try:
        environment = object_value(
            native_json("azd", ["env", "get-values", "--environment", args.environment, "--output", "json"])
        )
        endpoint = environment.get("FOUNDRY_PROJECT_ENDPOINT")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("Selected environment must supply an HTTPS Foundry project endpoint.")
        reports: dict[str, ContextReport] = {}
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
        ):
            for agent, operation in (("research", "direct"), ("supervisor", "research")):
                service = agent + "-agent"
                record = object_value(project.agents.get(service).as_dict())
                latest = object_value(object_value(record.get("versions")).get("latest"))
                rules = object_value(object_value(record.get("agent_endpoint")).get("version_selector")).get(
                    "version_selection_rules"
                )
                if latest.get("status") != "active" or rules != [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                ]:
                    raise ValueError(f"{service} must be active and routed exclusively to @latest.")
                idle_timeout = verify_idle_timeout(project, service, str(latest.get("version")))
                run.result(
                    service + " deployment",
                    "PASS",
                    expected="Active version with 120-second idle timeout",
                    observed={"version": latest.get("version"), "idle_timeout_seconds": idle_timeout},
                )
                command = ContextCommand.model_validate({"operation": operation})
                with project.get_openai_client(agent_name=service, timeout=args.timeout, max_retries=0) as client:
                    reports[agent] = invoke(client, agent, command, run)
        compare(reports["research"], reports["supervisor"], run)
    except (AzureError, OpenAIError, CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        run.result(
            "Context diagnostic", "FAIL", expected="Bounded direct/nested presence comparison", observed=str(error)
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
