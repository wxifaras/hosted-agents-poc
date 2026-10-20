"""HTTP smoke diagnostics expose actual targets without logging credentials or unrestricted headers."""

import json
from pathlib import Path

import httpx
import pytest
from app.core.settings import Settings

from scripts.common.http_details import http_details
from scripts.run_scenario import trigger_agent


def test_http_snapshot_is_allowlisted_and_strips_sensitive_url_parts() -> None:
    details = http_details(
        "POST",
        "https://username:password@example.com/responses?api-version=v1&sig=secret-signature#secret-fragment",
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token",
            "Cookie": "secret-cookie",
            "api-key": "secret-key",
            "x-agent-foundry-call-id": "secret-call-id",
        },
        status_code=429,
        response_headers={
            "X-Request-ID": "request-id",
            "Retry-After": "10",
            "Content-Type": "application/json",
            "Set-Cookie": "secret-cookie",
            "x-agent-foundry-call-id": "secret-call-id",
            "X-Custom": "secret-custom",
        },
    )
    assert details == {
        "method": "POST",
        "uri": "https://example.com/responses",
        "query_parameters": {"api-version": "v1"},
        "request_headers": {"accept": "application/json", "content-type": "application/json"},
        "status_code": 429,
        "response_headers": {"x-request-id": "request-id", "retry-after": "10", "content-type": "application/json"},
    }
    assert "secret-" not in json.dumps(details)
    assert "password" not in json.dumps(details)


def test_transport_failure_has_no_invented_status() -> None:
    details = http_details("POST", "http://127.0.0.1:8089/responses", {})
    assert details["uri"] == "http://127.0.0.1:8089/responses"
    assert details["status_code"] is None
    assert details["response_headers"] == {}


def test_local_http_capture_matches_actual_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:9876/responses"
        return httpx.Response(
            200,
            json={"id": "response", "status": "completed", "output": []},
            headers={"Content-Type": "application/json", "x-request-id": "local-request"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(trigger_agent.httpx, "Client", lambda **_kwargs: client)
    monkeypatch.setattr(trigger_agent, "get_settings", lambda: Settings(research_port=9876))
    payload = trigger_agent.invoke_local("research", "test", timeout=5)
    assert payload["_http"] == {
        "method": "POST",
        "uri": "http://127.0.0.1:9876/responses",
        "query_parameters": {},
        "request_headers": {"accept": "*/*", "content-type": "application/json"},
        "status_code": 200,
        "response_headers": {"content-type": "application/json", "x-request-id": "local-request"},
    }


def test_local_error_reports_uri_status_and_safe_response_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:8089/responses",
        headers={"Content-Type": "application/json", "Authorization": "Bearer secret-token"},
    )
    response = httpx.Response(429, request=request, headers={"retry-after": "10", "set-cookie": "secret-cookie"})

    def fail(*args: object, **kwargs: object) -> None:
        response.raise_for_status()

    monkeypatch.setattr(trigger_agent, "invoke_local", fail)
    assert trigger_agent.main(["--agent", "research", "--output-dir", str(tmp_path), "--no-color"]) == 1
    event = json.loads(next(tmp_path.glob("*/results.jsonl")).read_text())
    assert event["observed"]["http"]["uri"] == str(request.url)
    assert event["observed"]["http"]["status_code"] == 429
    assert event["observed"]["http"]["response_headers"] == {"retry-after": "10"}
    console = capsys.readouterr().out
    assert "Request URI" in console
    assert "HTTP status" in console
    assert "secret-token" not in console
    assert "secret-cookie" not in console


def test_real_openai_raw_response_exposes_actual_request_without_network() -> None:
    import httpx2
    from openai import OpenAI

    def handle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "resp-offline",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "HOSTED-AGENTS-POC-OK", "annotations": []}],
                    }
                ],
            },
            headers={"x-request-id": "offline-request", "set-cookie": "private-cookie"},
        )

    with OpenAI(
        base_url="https://offline.invalid/agents/test/endpoint/protocols/openai/",
        api_key="private-auth",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
    ) as client:
        raw = client.responses.with_raw_response.create(input="test", stream=False)
        details = http_details(
            raw.http_request.method,
            str(raw.http_request.url),
            raw.http_request.headers,
            status_code=raw.status_code,
            response_headers=raw.headers,
        )
        assert raw.parse().output_text == "HOSTED-AGENTS-POC-OK"
    assert details["uri"] == "https://offline.invalid/agents/test/endpoint/protocols/openai/responses"
    assert details["status_code"] == 200
    assert details["response_headers"]["x-request-id"] == "offline-request"
    assert "private-auth" not in json.dumps(details)
    assert "private-cookie" not in json.dumps(details)
