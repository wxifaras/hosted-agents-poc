"""Send a smoke request to an explicitly running local MAF Responses host.

This is not a durable workflow API. Exit codes: 0 completed, 1 failed response,
2 configuration/transport prerequisite blocked. No hosted user isolation is claimed.
"""

import argparse
import json
from collections.abc import Sequence
from time import perf_counter

import httpx
from app.core.redaction import sanitize, sanitize_text
from app.core.settings import AGENT_NAMES, AgentName, get_settings
from azure.core.exceptions import AzureError
from pydantic import ValidationError
from rich.table import Table

from scripts.common.http_details import http_details
from scripts.common.reporting import Status, ValidationRun, add_output_arguments

_SMOKE_MARKER = "HOSTED-AGENTS-POC-OK"


def response_summary(payload: dict[str, object]) -> dict[str, object]:
    """Expose visible assistant text and metrics, never opaque or hidden reasoning state."""
    text: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            text.extend(
                part["text"]
                for part in content
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str)
            )
    metrics: dict[str, int] = {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            count = usage.get(key)
            if type(count) is int and count >= 0:
                metrics[key] = count
        for key, field in (("output_tokens_details", "reasoning_tokens"), ("input_tokens_details", "cached_tokens")):
            details = usage.get(key)
            if isinstance(details, dict):
                count = details.get(field)
                if type(count) is int and count >= 0:
                    metrics[field] = count
    summary: dict[str, object] = {
        key: payload.get(key)
        for key in (
            "id",
            "response_id",
            "status",
            "agent_session_id",
            "conversation_id",
            "model",
        )
        if key in payload
    }
    summary["text"] = "\n".join(text)
    summary["usage"] = metrics or None
    error = payload.get("error")
    if isinstance(error, dict):
        summary["error"] = {key: error.get(key) for key in ("code", "message") if key in error}
    return summary


def build_parser() -> argparse.ArgumentParser:
    """Define explicit local-only invocation without Azure discovery."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=AGENT_NAMES, default="supervisor")
    parser.add_argument("--message", default=f"Reply with exactly: {_SMOKE_MARKER}")
    parser.add_argument("--timeout", type=float, default=60)
    add_output_arguments(parser)
    return parser


def invoke_local(name: AgentName, message: str, *, timeout: float) -> dict[str, object]:
    """Invoke the actual Responses HTTP endpoint with bounded transport time."""
    if timeout <= 0:
        raise ValueError("--timeout must be positive.")
    settings = get_settings()
    url = f"http://127.0.0.1:{settings.agent_port(name)}"
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.post(f"{url}/responses", json={"input": message, "stream": False})
        response.raise_for_status()
        payload: object = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Responses endpoint returned a non-object JSON payload.")
    result = {str(key): value for key, value in payload.items()}
    result["_http"] = http_details(
        response.request.method,
        str(response.request.url),
        response.request.headers,
        status_code=response.status_code,
        response_headers=response.headers,
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Record a real model-backed local response without simulating cloud success."""
    args = build_parser().parse_args(argv)
    run = ValidationRun("smoke-invocation", "local", args.output_dir, no_color=args.no_color, verbose=args.verbose)
    record_smoke(run, args.agent, args.message, timeout=args.timeout)
    return run.finish()


def record_smoke(run: ValidationRun, agent: AgentName, message: str, *, timeout: float) -> None:
    """Record one actual invocation in a standalone or multi-agent smoke run."""
    request = {"agent": agent, "message": sanitize_text(message)}
    request_number = run.start_request(agent, "local", message)
    started = perf_counter()
    observed: dict[str, object] = {"agent": agent, "request": request}
    status: Status
    try:
        payload = invoke_local(agent, message, timeout=timeout)
    except httpx.HTTPStatusError as error:
        step, status, expected = "Request", "FAIL", "Successful Responses request"
        observed.update(http_status=error.response.status_code, error=str(error))
        observed["http"] = http_details(
            error.request.method,
            str(error.request.url),
            error.request.headers,
            status_code=error.response.status_code,
            response_headers=error.response.headers,
        )
    except httpx.RequestError as error:
        step, status, expected = "Request", "BLOCKED", "Reachable running host"
        observed["error"] = str(error)
        try:
            request_object = error.request
        except RuntimeError:
            request_object = None  # Some manually constructed transport errors have no attached request.
        if request_object is not None:
            observed["http"] = http_details(
                request_object.method,
                str(request_object.url),
                request_object.headers,
            )
    except ValidationError as error:
        step, status, expected = "Request", "BLOCKED", "Configured running host"
        observed["errors"] = error.errors(include_input=False, include_url=False)
    except (httpx.HTTPError, AzureError, ValueError, RuntimeError, ImportError) as error:
        step, status, expected = "Request", "BLOCKED", "Configured running host"
        observed["error"] = str(error)
    else:
        summary = response_summary(payload)
        complete = (
            payload.get("status") == "completed"
            and isinstance(payload.get("id"), str)
            and bool(payload.get("id"))
            and bool(summary["text"])
            and not payload.get("error")
        )
        if message == f"Reply with exactly: {_SMOKE_MARKER}":
            complete = complete and str(summary["text"]).strip() == _SMOKE_MARKER
        step, status = "Response", "PASS" if complete else "FAIL"
        expected = "Completed response with assistant text (exact marker for the default smoke prompt)"
        observed["response"] = summary
        if isinstance(payload.get("_http"), dict):
            observed["http"] = payload["_http"]
    duration_ms = (perf_counter() - started) * 1000
    observed["duration_ms"] = duration_ms
    table = smoke_table(agent, message, status, expected, observed)
    run.result(step, status, expected=expected, observed=observed, summary_table=table)
    run.end_request(request_number, agent, status, duration_ms)


def smoke_table(
    agent: AgentName,
    message: str,
    status: Status,
    expected: str,
    observed: dict[str, object],
) -> Table:
    """Present one readable agent result while JSONL retains the structured record."""
    table = Table(title=f"{agent} - expected versus observed", show_lines=True)
    table.add_column("Field", no_wrap=True)
    table.add_column("Expected", ratio=1)
    table.add_column("Observed", ratio=2, overflow="fold")

    def add(field: str, wanted: str, actual: object) -> None:
        safe = sanitize(actual)
        text = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=True)
        table.add_row(field, wanted, text)

    add("Validation", expected, status)
    transport = observed.get("http")
    if isinstance(transport, dict):
        add("HTTP method", "POST", transport.get("method"))
        add("Request URI", "-", transport.get("uri"))
        if transport.get("query_parameters"):
            add("Query parameters", "-", transport["query_parameters"])
        add("HTTP status", "2xx", transport.get("status_code") or "No response")
        for field, label in (("request_headers", "Request header"), ("response_headers", "Response header")):
            headers = transport.get(field)
            if isinstance(headers, dict):
                for key, value in headers.items():
                    add(label, str(key), value)
    if observed.get("service"):
        add("Hosted service", "-", observed["service"])
    deployment = observed.get("deployment")
    if isinstance(deployment, dict):
        add("Agent version", "Active", deployment.get("version"))
    add("Message sent", "-", message)
    response = observed.get("response")
    if isinstance(response, dict):
        default_prompt = message == f"Reply with exactly: {_SMOKE_MARKER}"
        add("Answer", _SMOKE_MARKER if default_prompt else "Nonempty assistant text", response.get("text") or "(none)")
        add("Response status", "completed", response.get("status"))
        add("Response ID", "Present", response.get("id"))
        if response.get("agent_session_id"):
            add("Session ID", "-", response["agent_session_id"])
        usage = response.get("usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                add(str(key), "-", value)
        else:
            add("Token usage", "-", "Unavailable")
        error = response.get("error")
        if isinstance(error, dict):
            add("Error code", "None", error.get("code"))
            add("Error message", "None", error.get("message"))
    for key in ("http_status", "error", "errors"):
        if key in observed:
            add(key, "None", observed[key])
    duration = observed["duration_ms"]
    add("Duration", "-", f"{duration:.0f} ms" if isinstance(duration, (float, int)) else duration)
    return table


if __name__ == "__main__":
    raise SystemExit(main())
