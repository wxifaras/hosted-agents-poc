"""Bounded real hosted mapping/delegation proof; no fault injection.

Creates one Supervisor workflow, inspects/replays it without inference, continues
both existing child conversations, then verifies unknown-workflow rejection.
Exit codes: 0 all checks pass, 1 observed failure, 2 prerequisite failure.
"""

import argparse
import subprocess
from collections.abc import Sequence
from time import perf_counter
from uuid import uuid4

from app.services.delegation import Command, Report
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.core.pipeline import PipelineResponse
from azure.core.rest import HttpRequest, HttpResponse
from azure.identity import AzureCliCredential
from openai import APIStatusError, OpenAI, OpenAIError
from pydantic import JsonValue, ValidationError
from rich.table import Table

from scripts.common.http_details import http_details
from scripts.common.reporting import Status, ValidationRun, add_output_arguments
from scripts.deploy.grant_local_rbac import CliError, native_json, object_value


def build_parser() -> argparse.ArgumentParser:
    """Keep this paid hosted scenario separate from smoke and ordinary checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="hosted-agents-poc-dev")
    parser.add_argument("--timeout", type=float, default=600)
    add_output_arguments(parser)
    return parser


def conversation_table(report: Report) -> Table:
    """Render root and child scopes separately without printing verified user IDs."""
    table = Table(title="Persisted workflow and distinct conversations")
    for label in ("Agent", "Workflow", "Conversation", "Opaque owner / session", "Last response"):
        table.add_column(label)
    table.add_row(
        report.agent,
        str(report.workflow_id),
        report.conversation_id,
        f"{report.owner_id}\n{report.session_id}",
        "(current Supervisor response)",
    )
    for name, child in report.children.items():
        table.add_row(
            name,
            str(child.workflow_id),
            child.conversation_id,
            f"{child.owner_id}\n{child.session_id}",
            child.response_id or "(missing)",
        )
    return table


def verify_idle_timeout(project: AIProjectClient, service: str, version: str) -> int:
    """Read the actual server field, including fields unknown to this pinned SDK."""
    snapshots: list[JsonValue] = []

    def capture(response: PipelineResponse[HttpRequest, HttpResponse]) -> None:
        snapshots.append(response.http_response.json())

    project.agents.get_version(service, version, raw_response_hook=capture)
    if len(snapshots) != 1:
        raise ValueError("Cannot uniquely inspect raw deployed-version metadata.")
    definition = object_value(object_value(snapshots[0]).get("definition"))
    configuration = object_value(definition.get("session_configuration"))
    value = configuration.get("idle_timeout_seconds")
    if type(value) is not int or value != 120:
        raise ValueError(f"{service}: actual deployed session idle timeout must be exactly 120 seconds.")
    return value


def invoke(
    client: OpenAI,
    command: Command,
    conversation_id: str,
    run: ValidationRun,
    *,
    platform_session_id: str | None = None,
    expect_missing: bool = False,
) -> Report | None:
    """Capture actual wire URI/status and distinguish expected rejection from failure."""
    label = "supervisor / " + ("missing-workflow" if expect_missing else command.operation)
    number = run.start_request(label, "hosted", command.wire())
    started = perf_counter()
    status: Status = "FAIL"
    extra_body: dict[str, object] = {}
    if platform_session_id:
        extra_body["agent_session_id"] = platform_session_id
    try:
        raw = client.responses.with_raw_response.create(
            input=command.wire(), conversation=conversation_id, stream=False, extra_body=extra_body
        )
        http = http_details(
            raw.http_request.method,
            str(raw.http_request.url),
            raw.http_request.headers,
            status_code=raw.status_code,
            response_headers=raw.headers,
        )
        response = raw.parse()
        if expect_missing:
            error = response.error.model_dump(mode="json") if response.error else None
            rejected = (
                response.status == "failed" and error is not None and "mapping is missing" in str(error).casefold()
            )
            run.result(
                label,
                "PASS" if rejected else "FAIL",
                expected="Explicit missing-workflow failure, not a new mapping or a successful response",
                observed={"http": http, "response_id": response.id, "status": response.status, "error": error},
            )
            status = "PASS" if rejected else "FAIL"
            return None
        if response.status != "completed" or response.error:
            run.result(
                label,
                "FAIL",
                expected="Completed hosted workflow protocol response",
                observed={
                    "http": http,
                    "platform_session_id": raw.headers.get("x-agent-session-id"),
                    "conversation_id": conversation_id,
                    "response_id": response.id,
                    "status": response.status,
                    "error": response.error.model_dump(mode="json") if response.error else None,
                },
            )
            raise ValueError(f"Hosted workflow response failed: {response.error}")
        report = Report.model_validate_json(response.output_text)
        if report.workflow_id != command.workflow_id or report.conversation_id != conversation_id:
            raise ValueError("Hosted workflow returned unexpected workflow/conversation bindings.")
        run.result(
            label,
            "PASS",
            expected="Completed real hosted workflow command with persisted scope references",
            observed={"http": http, "response_id": response.id, "report": report.model_dump(mode="json")},
            summary_table=conversation_table(report),
        )
        run.console.print(f"Answer: {report.answer}")
        status = "PASS"
        return report
    except APIStatusError as error:
        run.result(
            label,
            "FAIL",
            expected="A real hosted workflow protocol response, not an HTTP failure",
            observed={
                "http": http_details(
                    error.request.method,
                    str(error.request.url),
                    error.request.headers,
                    status_code=error.status_code,
                    response_headers=error.response.headers,
                ),
                "platform_session_id": error.response.headers.get("x-agent-session-id"),
                "conversation_id": conversation_id,
                "error": str(error),
            },
        )
        raise
    finally:
        run.end_request(number, label, status, (perf_counter() - started) * 1000)


def verify_relationships(first: Report, continued: Report) -> None:
    """Check exact IDs and history answers, not just a successful HTTP response."""
    if first.progress != "ready" or continued.progress != "ready":
        raise ValueError("Workflow turn did not reach committed ready state.")
    if (
        first.owner_id != continued.owner_id
        or first.session_id != continued.session_id
        or first.platform_session_id != continued.platform_session_id
    ):
        raise ValueError("Root opaque mappings or actual hosted session changed.")
    expected = {"research", "data_analysis"}
    if set(first.children) != expected or set(continued.children) != expected:
        raise ValueError("Both specialists must have persisted child relationships.")
    if len({first.conversation_id, *(child.conversation_id for child in first.children.values())}) != 3:
        raise ValueError("Expected exactly three distinct platform conversations.")
    for name, child in first.children.items():
        current = continued.children[name]
        if (
            child.workflow_id != current.workflow_id
            or child.conversation_id != current.conversation_id
            or child.platform_session_id != current.platform_session_id
            or child.owner_id != current.owner_id
            or child.session_id != current.session_id
            or not current.response_id
            or child.response_id == current.response_id
            or not current.owner_id
            or not current.session_id
            or not current.platform_session_id
        ):
            raise ValueError(f"{name}: child scope changed, is incomplete, or was not actually continued.")
    if continued.answer.strip() != "research: ALPHA-POC-7\n\ndata_analysis: ALPHA-POC-7":
        raise ValueError("Both specialists must recall the first-turn token from their own platform history.")


def main(argv: Sequence[str] | None = None) -> int:
    """Perform bounded synthetic hosted calls only in this explicit scenario."""
    args = build_parser().parse_args(argv)
    run = ValidationRun(
        "durable-mappings-and-delegation", "hosted", args.output_dir, no_color=args.no_color, verbose=args.verbose
    )
    try:
        if args.timeout <= 0:
            raise ValueError("--timeout must be positive.")
        environment = object_value(
            native_json("azd", ["env", "get-values", "--environment", args.environment, "--output", "json"])
        )
        endpoint = environment.get("FOUNDRY_PROJECT_ENDPOINT")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ValueError("An existing HTTPS Foundry project endpoint is required.")
        with (
            AzureCliCredential(process_timeout=60) as credential,
            AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
            project.get_openai_client(agent_name="supervisor-agent", timeout=args.timeout, max_retries=0) as client,
        ):
            for service in ("supervisor-agent", "research-agent", "data-analysis-agent"):
                record = object_value(project.agents.get(service).as_dict())
                latest = object_value(object_value(record.get("versions")).get("latest"))
                rules = object_value(object_value(record.get("agent_endpoint")).get("version_selector")).get(
                    "version_selection_rules"
                )
                if latest.get("status") != "active" or rules != [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                ]:
                    raise ValueError(f"{service} must be active and routed exclusively to @latest.")
                timeout = verify_idle_timeout(project, service, str(latest.get("version")))
                run.result(
                    service + " deployment",
                    "PASS",
                    expected="Active hosted version and @latest routing",
                    observed={
                        "version": latest.get("version"),
                        "id": latest.get("id"),
                        "idle_timeout_seconds": timeout,
                    },
                )
            conversation = client.conversations.create()
            workflow_id = uuid4()
            create = Command(
                operation="create",
                workflow_id=workflow_id,
                message="Remember the synthetic POC token ALPHA-POC-7 for this conversation. Reply exactly: READY",
            )
            first = invoke(client, create, conversation.id, run)
            if first is None:
                raise ValueError("Creation returned no workflow report.")
            inspect = Command(operation="inspect", workflow_id=workflow_id)
            inspected = invoke(client, inspect, conversation.id, run, platform_session_id=first.platform_session_id)
            replay = invoke(client, create, conversation.id, run, platform_session_id=first.platform_session_id)
            if inspected != first or replay != first:
                raise ValueError("Read-only inspection or idempotent creation changed persisted scope/results.")
            continued = invoke(
                client,
                Command(
                    operation="continue",
                    workflow_id=workflow_id,
                    message="What is the synthetic POC token I asked you to remember? Reply with only that token.",
                ),
                conversation.id,
                run,
                platform_session_id=first.platform_session_id,
            )
            if continued is None:
                raise ValueError("Continuation returned no workflow report.")
            verify_relationships(first, continued)
            run.result(
                "Durable mapping reuse and child history",
                "PASS",
                expected="Same random scopes and three distinct conversations; both specialists remember ALPHA-POC-7",
                observed={
                    "root_workflow_id": str(workflow_id),
                    "child_count": len(continued.children),
                    "root_mappings_reused": True,
                    "child_mappings_reused": True,
                    "independent_histories_recalled_token": True,
                },
            )
            invoke(
                client,
                Command(operation="inspect", workflow_id=uuid4()),
                conversation.id,
                run,
                platform_session_id=first.platform_session_id,
                expect_missing=True,
            )
        run.console.print(
            "Not tested: two-principal isolation, sandbox restart/idle continuity, crash recovery, Blob authorization."
        )
    except (
        AzureError,
        OpenAIError,
        CliError,
        ValueError,
        ValidationError,
        OSError,
        subprocess.TimeoutExpired,
    ) as error:
        run.result(
            "Hosted workflow",
            "FAIL",
            expected="Real durable state and verified delegated specialist calls",
            observed=str(error),
        )
    return run.finish()


if __name__ == "__main__":
    raise SystemExit(main())
