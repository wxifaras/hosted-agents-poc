"""Run explicit hosted step-2/3 identity experiments, without model calls.

Exit 0: every requested assertion passed; 1: failed assertions;
2: blocked tests or unavailable prerequisites. Two genuine Entra users are not
simulated by synthetic delegated labels.
"""

import argparse
import asyncio
import subprocess
from collections.abc import Sequence
from time import perf_counter
from uuid import uuid4

from app.services.identity_checks import IdentityCommand, IdentityMatrix, IdentityRunner
from app.services.workflows import WorkflowError
from azure.ai.projects import AIProjectClient
from azure.ai.projects.aio import AIProjectClient as AsyncAIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from openai import OpenAIError
from rich.table import Table

from scripts.common.http_details import http_details
from scripts.common.reporting import Status, ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import CliError, native_json, object_value
from scripts.run_scenario.hosted_workflow import verify_idle_timeout


def build_parser() -> argparse.ArgumentParser:
    """Do not include the paid scenario in ordinary local checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument(
        "--direct-control",
        action="store_true",
        help="Test delegated A/B directly as the Azure CLI principal; never grant missing impersonation permission.",
    )
    history = parser.add_mutually_exclusive_group()
    history.add_argument(
        "--response-chains",
        action="store_true",
        help="For nested comparison, omit explicit conversation creation and bind history with previous_response_id.",
    )
    history.add_argument(
        "--history-only",
        action="store_true",
        help="Use response chains and omit A's explicit sandbox binding only on B's history attempt.",
    )
    add_output_arguments(parser)
    return parser


async def direct_control(endpoint: str, *, history_only: bool = False) -> IdentityMatrix:
    """Contrast developer-to-Research with hosted Supervisor-to-Research, without new grants."""
    async with (
        AsyncAzureCliCredential(process_timeout=60) as credential,
        AsyncAIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
    ):
        runner = IdentityRunner(project, None, uuid4())
        try:
            await runner.variant("delegated-user", use_conversation=False, bind_b_history_session=not history_only)
        except (AzureError, OpenAIError, WorkflowError) as error:
            runner.failure("direct-control: delegated invocation", error)
        return runner.result


def main(argv: Sequence[str] | None = None) -> int:
    """Record exact mode/context/state/history outcomes through the existing Supervisor."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "hosted-identity-matrix", "hosted", args.output_dir, no_color=args.no_color, verbose=args.verbose
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
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
        ):
            services = ("research-agent",) if args.direct_control else ("supervisor-agent", "research-agent")
            for service in services:
                record = object_value(project.agents.get(service).as_dict())
                latest = object_value(object_value(record.get("versions")).get("latest"))
                rules = object_value(object_value(record.get("agent_endpoint")).get("version_selector")).get(
                    "version_selection_rules"
                )
                if latest.get("status") != "active" or rules != [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                ]:
                    raise ValueError(f"{service} must be active with exclusive @latest routing.")
                idle = verify_idle_timeout(project, service, str(latest.get("version")))
                run.result(
                    service,
                    "PASS",
                    expected="Active hosted target, no idle-policy change",
                    observed={"version": latest.get("version"), "idle_timeout_seconds": idle},
                )
            command = IdentityCommand(
                operation="compare-history-only"
                if args.history_only
                else "compare-chains"
                if args.response_chains
                else "compare",
                run_id=uuid4(),
            )
            target = (
                "research-agent / direct control" if args.direct_control else "supervisor-agent / identity experiments"
            )
            description = (
                "Direct delegated A/B control: presence, state, and history operations."
                if args.direct_control
                else command.wire()
            )
            number = run.start_request(target, "hosted", description)
            started = perf_counter()
            status: Status = "FAIL"
            try:
                if args.direct_control:
                    matrix = asyncio.run(
                        asyncio.wait_for(direct_control(endpoint, history_only=args.history_only), timeout=args.timeout)
                    )
                else:
                    with project.get_openai_client(
                        agent_name="supervisor-agent", timeout=args.timeout, max_retries=0
                    ) as client:
                        conversation = client.conversations.create()
                        raw = client.responses.with_raw_response.create(
                            input=command.wire(), conversation=conversation.id, stream=False, store=True
                        )
                        response = raw.parse()
                    run.result(
                        "Experiment transport",
                        "PASS" if response.status == "completed" and not response.error else "FAIL",
                        expected="Protocol-completed experiment collection, not automatic assertion success",
                        observed={
                            "http": http_details(
                                raw.http_request.method,
                                str(raw.http_request.url),
                                raw.http_request.headers,
                                status_code=raw.status_code,
                                response_headers=raw.headers,
                            ),
                            "response_id": response.id,
                            "status": response.status,
                            "error": response.error.model_dump() if response.error else None,
                        },
                    )
                    if response.status != "completed" or response.error:
                        raise ValueError("Identity experiment collection failed; inspect its protocol error.")
                    matrix = IdentityMatrix.model_validate_json(response.output_text)
                matrix.checks.append(
                    matrix.completeness_check(direct_control=args.direct_control, history_only=args.history_only)
                )
                table = Table(title="Call-context versus delegated-user behavior")
                for label in ("Check", "Status", "Observed"):
                    table.add_column(label)
                for check in matrix.checks:
                    run.result(
                        check.name,
                        check.status,
                        expected="Specific identity/state/history assertion",
                        observed=check.observed,
                    )
                    table.add_row(check.name, check.status, str(check.observed))
                run.console.print(table)
                run.console.print(
                    "Synthetic delegated labels are not two genuine Entra users. No model calls requested."
                )
                status = "PASS" if all(check.status == "PASS" for check in matrix.checks) else "FAIL"
            finally:
                run.end_request(number, target, status, (perf_counter() - started) * 1000)
    except TimeoutError:
        run.result(
            "Identity matrix",
            "FAIL",
            expected="Control completes within the requested deadline",
            observed="Identity control exceeded --timeout; no automatic replay or permission change was attempted.",
        )
    except (AzureError, OpenAIError, CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        run.result(
            "Identity matrix",
            "FAIL",
            expected="Bounded real hosted diagnostic/state/history results",
            observed=str(error),
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
