"""Invoke all three independent Foundry-hosted agents once with Rich evidence.

Uses the selected azd project endpoint and Azure CLI user credential, not localhost.
Exit codes: 0 all real hosted responses pass, 1 observed failure, 2 prerequisites blocked.
This does not prove delegation, owner mappings, isolation, or crash recovery.
"""

import argparse
import subprocess
from collections.abc import Sequence
from time import perf_counter
from typing import Literal

from app.core.settings import AgentName
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential
from openai import APIConnectionError, APIStatusError, OpenAIError

from scripts.common.http_details import http_details
from scripts.common.reporting import ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import CliError, native_json, object_value
from scripts.run_scenario.trigger_agent import response_summary, smoke_table

SERVICES: dict[AgentName, str] = {
    "supervisor": "supervisor-agent",
    "research": "research-agent",
    "data_analysis": "data-analysis-agent",
}
MESSAGE = "Reply with exactly: HOSTED-AGENTS-POC-OK"


def build_parser() -> argparse.ArgumentParser:
    """Define explicit paid hosted verification, separate from the local scenario."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--timeout", type=float, default=180)
    add_output_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read native azd deployment context and validate real assistant text from each hosted agent."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "independent-host-smoke", "hosted", args.output_dir, no_color=args.no_color, verbose=args.verbose
    )
    if args.timeout <= 0:
        run.result("Configuration", "BLOCKED", expected="Positive timeout", observed="--timeout must be positive.")
        return run.finish()
    try:
        context = object_value(
            native_json("azd", ["env", "get-values", "--environment", args.environment, "--output", "json"])
        )
        endpoint = context.get("FOUNDRY_PROJECT_ENDPOINT")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("azd environment must supply an HTTPS FOUNDRY_PROJECT_ENDPOINT.")
    except (CliError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        run.result("Hosted prerequisites", "BLOCKED", expected="Existing azd Foundry project", observed=str(error))
        return run.finish()
    with (
        AzureCliCredential() as credential,
        AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
    ):
        for agent, service in SERVICES.items():
            observed: dict[str, object] = {
                "agent": agent,
                "service": service,
                "project_endpoint": endpoint,
                "request": {"agent": agent, "message": MESSAGE},
            }
            started = perf_counter()
            request_number = run.start_request(service, "hosted", MESSAGE)
            try:
                agent_record = object_value(project.agents.get(service).as_dict())
                definition = object_value(object_value(agent_record.get("versions")).get("latest"))
                if definition.get("status") != "active":
                    raise ValueError(f"{service} latest version is not active.")
                rules = object_value(object_value(agent_record.get("agent_endpoint")).get("version_selector")).get(
                    "version_selection_rules"
                )
                if rules != [{"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}]:
                    raise ValueError(
                        f"{service} endpoint is not routed exclusively to @latest; verify routing manually."
                    )
                observed["deployment"] = {
                    "name": definition.get("name"),
                    "version": definition.get("version"),
                    "status": definition.get("status"),
                    "id": definition.get("id"),
                }
                with project.get_openai_client(agent_name=service, timeout=args.timeout, max_retries=0) as client:
                    raw = client.responses.with_raw_response.create(input=MESSAGE, stream=False)
                    observed["http"] = http_details(
                        raw.http_request.method,
                        str(raw.http_request.url),
                        raw.http_request.headers,
                        status_code=raw.status_code,
                        response_headers=raw.headers,
                    )
                    response = raw.parse()
                payload: dict[str, object] = response.model_dump(mode="json")
                summary = response_summary(payload)
                observed["response"] = summary
                passed = (
                    summary.get("status") == "completed"
                    and summary.get("id")
                    and (str(summary["text"]).strip() == "HOSTED-AGENTS-POC-OK")
                    and not summary.get("error")
                )
                status: Literal["PASS", "FAIL"] = "PASS" if passed else "FAIL"
            except APIStatusError as error:
                status = "FAIL"
                observed["http"] = http_details(
                    error.request.method,
                    str(error.request.url),
                    error.request.headers,
                    status_code=error.status_code,
                    response_headers=error.response.headers,
                )
                observed["error"] = str(error)
            except APIConnectionError as error:
                status = "FAIL"
                observed["http"] = http_details(error.request.method, str(error.request.url), error.request.headers)
                observed["error"] = str(error)
            except (CliError, AzureError, OpenAIError, ValueError, OSError, subprocess.TimeoutExpired) as error:
                status = "FAIL"
                observed["error"] = str(error)
            duration_ms = (perf_counter() - started) * 1000
            observed["duration_ms"] = duration_ms
            expected = "Real hosted response with exact HOSTED-AGENTS-POC-OK marker"
            run.result(
                service,
                status,
                expected=expected,
                observed=observed,
                summary_table=smoke_table(agent, MESSAGE, status, expected, observed),
            )
            run.end_request(request_number, service, status, duration_ms)
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
