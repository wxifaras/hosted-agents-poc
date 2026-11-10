"""Use real SDK clients over an in-memory HTTP transport; never call Foundry."""

import argparse
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from measure import (
    PREFIX,
    HostedResponse,
    ProbeReply,
    collect,
    diagnostics,
    endpoint_matches,
    invoke,
    main,
    resolve_endpoint,
    run_info,
    state_errors,
)
from report import load_run

MARKER = "a" * 32


def reply(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "story13-probe-v1",
        "previous_memory": None,
        "previous_file": None,
        "turn": 1,
        "restores": 1,
        "instance": "1" * 32,
        "request": "2" * 32,
        "core_version": "2.3.0",
        "responses_version": "2.4.0",
    }
    return result | changes


def response_body(**changes: object) -> dict[str, object]:
    return {
        "id": "resp_test",
        "created_at": 1,
        "model": "story13-no-model",
        "object": "response",
        "status": "completed",
        "agent_session_id": "private-session",
        "parallel_tool_calls": False,
        "tool_choice": "none",
        "tools": [],
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(reply(**changes)), "annotations": []}],
            }
        ],
    }


def test_missing_diagnostics_are_not_zero_or_inferred() -> None:
    headers, timings, start = diagnostics({})
    assert headers == {} and start is None
    assert all(value is None for value in timings.values())


def test_native_no_tool_response_omits_openai_model_tool_fields() -> None:
    payload = response_body()
    payload.pop("tool_choice")
    payload.pop("tools")
    assert HostedResponse.model_validate(payload).output_text


@pytest.mark.parametrize("value", ["-1", "nan", "1.5", "inf", "9" * 400])
def test_invalid_native_timing_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        diagnostics({PREFIX + "container-readiness-ms": value})


def test_case_insensitive_allowlist() -> None:
    headers, timings, start = diagnostics(
        {
            "X-MS-Debug-Latency-Session-Start-Type": "cold",
            PREFIX + "container-readiness-ms": "0",
            "Authorization": "secret",
        }
    )
    assert start == "cold" and timings["container-readiness-ms"] == 0
    assert "Authorization" not in headers and "secret" not in str(headers)


def test_state_guardrails_and_post_restore_identifiers() -> None:
    cold = ProbeReply.model_validate(reply())
    assert state_errors(cold, "cold", MARKER, True, None) == []
    warm = ProbeReply.model_validate(reply(previous_memory=MARKER, previous_file=MARKER, turn=2, request="3" * 32))
    assert state_errors(warm, "warm", MARKER, True, cold) == []
    resumed = ProbeReply.model_validate(reply(instance="4" * 32, request="5" * 32))
    assert state_errors(resumed, "resume", MARKER, True, warm) == []
    assert state_errors(warm, "resume", MARKER, True, warm)
    assert state_errors(warm, "cold", MARKER, True, None)


def test_real_sdk_transport_does_not_retry_posts_or_persist_marker() -> None:
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        assert json.loads(request.content)["agent_session_id"] == "private-session"
        assert request.headers[PREFIX + "enabled"] == "true"
        return httpx.Response(
            200,
            json=response_body(),
            headers={PREFIX + "session-start-type": "cold", PREFIX + "total-last-byte-ms": "5"},
        )

    with OpenAI(
        api_key="test",
        base_url="http://test",
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        max_retries=0,
    ) as client:
        sample, parsed, session = invoke(client, "private-session", MARKER, "cold", 1, True, None)
    assert sample.passed and parsed is not None
    assert calls == ["POST"]
    saved = sample.model_dump_json()
    assert MARKER not in saved and "private-session" not in saved and "test-key" not in saved
    assert sample.timings["container-readiness-ms"] is None
    assert session == "private-session"


def test_delayed_headers_use_get_only_and_not_client_complete_time() -> None:
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        headers = (
            {PREFIX + "session-start-type": "cold"}
            if request.method == "POST"
            else {PREFIX + "session-start-type": "cold", PREFIX + "total-last-byte-ms": "6"}
        )
        return httpx.Response(200, json=response_body(), headers=headers)

    with (
        OpenAI(
            api_key="test",
            base_url="http://test",
            http_client=httpx.Client(transport=httpx.MockTransport(transport)),
            max_retries=0,
        ) as client,
        patch("measure.sleep"),
    ):
        sample, _, _ = invoke(client, "private-session", MARKER, "cold", 1, True, None)
    assert sample.passed and calls == ["POST", "GET"]


def test_wrong_classification_fails_instead_of_counting_as_cold() -> None:
    def transport(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_body(),
            headers={PREFIX + "session-start-type": "warm", PREFIX + "total-last-byte-ms": "5"},
        )

    with OpenAI(
        api_key="test",
        base_url="http://test",
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        max_retries=0,
    ) as client:
        sample, _, _ = invoke(client, "private-session", MARKER, "cold", 1, True, None)
    assert not sample.passed and sample.start_type == "warm"


def test_failed_post_records_failure_without_retry() -> None:
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(503, json={"error": {"message": "unavailable", "type": "server_error"}})

    with OpenAI(
        api_key="test",
        base_url="http://test",
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        max_retries=0,
    ) as client:
        sample, result, _ = invoke(client, "private-session", MARKER, "cold", 1, False, None)
    assert calls == ["POST"] and result is None and not sample.passed
    assert sample.errors and sample.timings["client_complete_ms"] is not None


def test_preview_never_authenticates_or_writes(tmp_path: Path) -> None:
    output = tmp_path / "run.jsonl"
    with patch("measure.AzureCliCredential", side_effect=AssertionError("Must not authenticate")):
        assert (
            main(
                [
                    "--project-endpoint",
                    "https://example.services.ai.azure.com/api/projects/test",
                    "--version",
                    "1",
                    "--condition",
                    "baseline",
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
    assert not output.exists()


def test_version_readiness_and_snapshot_configuration() -> None:
    args = argparse.Namespace(
        condition="baseline",
        project_endpoint="https://test",
        agent="story13",
        version="1",
        samples=10,
        resume_samples=1,
        idle_seconds=150,
    )
    record: dict[str, object] = {
        "status": "active",
        "version": "1",
        "definition": {"cpu": "0.5", "memory": "1Gi", "container_configuration": {"image": "test@sha256:" + "a" * 64}},
    }
    assert run_info(args, record).snapshots is False
    with pytest.raises(ValueError, match="active"):
        run_info(args, record | {"status": "creating"})
    args.condition = "snapshot"
    with pytest.raises(ValueError, match="snapshot flag"):
        run_info(args, record)


def test_collection_persists_all_phases_and_full_cold_journey(tmp_path: Path) -> None:
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        payload = json.loads(request.content)
        marker = payload["input"]
        phase = ("cold", "warm", "resume")[len(calls)]
        if phase == "cold":
            assert "agent_session_id" not in payload
        else:
            assert payload["agent_session_id"] == "private-session"
        calls.append(phase)
        changes: dict[str, object] = {"request": f"{len(calls):032x}"}
        if phase == "warm":
            changes.update(previous_memory=marker, previous_file=marker, turn=2)
        if phase == "resume":
            changes.update(instance="9" * 32)
        return httpx.Response(
            200,
            json=response_body(**changes),
            headers={PREFIX + "session-start-type": phase, PREFIX + "total-last-byte-ms": "6"},
        )

    args = argparse.Namespace(
        condition="snapshot",
        project_endpoint="https://test",
        agent="story13",
        version="1",
        samples=1,
        resume_samples=1,
        idle_seconds=150,
        timeout=30,
        output=tmp_path / "run.jsonl",
    )
    with (
        OpenAI(
            api_key="test",
            base_url="http://test",
            http_client=httpx.Client(transport=httpx.MockTransport(transport)),
            max_retries=0,
        ) as client,
        patch("measure.AzureCliCredential"),
        patch("measure.AIProjectClient") as project_type,
        patch("measure.sleep") as waiting,
    ):
        project = project_type.return_value.__enter__.return_value
        project.get_openai_client.return_value.__enter__.return_value = client
        project.agents.get_version.return_value.as_dict.return_value = {
            "status": "active",
            "version": "1",
            "definition": {
                "cpu": "0.5",
                "memory": "1Gi",
                "enable_memory_snapshot": True,
                "container_configuration": {"image": "test@sha256:" + "a" * 64},
            },
        }
        project.agents.get.return_value.as_dict.return_value = {
            "agent_endpoint": {
                "version_selector": {
                    "version_selection_rules": [{"type": "FixedRatio", "agent_version": "1", "traffic_percentage": 100}]
                }
            }
        }
        project.agents.get_session.return_value.as_dict.return_value = {"version_indicator": {"agent_version": "1"}}
        assert collect(args) == 0
        project.agents.create_session.assert_not_called()
        project.agents.get_session.assert_called_once_with("story13", "private-session")
        waiting.assert_called_once_with(150)
    assert calls == ["cold", "warm", "resume"]
    run = load_run(args.output)
    assert len(run.samples) == 3 and all(sample.passed for sample in run.samples)
    cold = run.samples[0].timings
    assert cold["client_journey_ms"] == cold["client_complete_ms"]
    assert "private-session" not in args.output.read_text()


def test_latest_or_split_endpoint_cannot_claim_a_version_pinned_cold_start() -> None:
    record = {
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                ]
            }
        }
    }
    assert not endpoint_matches(record, "1")
    assert not endpoint_matches({}, "1")


def test_task_preview_needs_no_endpoint_environment_or_authentication(tmp_path: Path) -> None:
    with (
        patch("measure.subprocess.run", side_effect=AssertionError("Preview must not launch azd")),
        patch("measure.AzureCliCredential", side_effect=AssertionError("Preview must not authenticate")),
    ):
        assert (
            main(
                [
                    "--version",
                    "1",
                    "--condition",
                    "baseline",
                    "--execution",
                    "preview",
                    "--output",
                    str(tmp_path / "preview.jsonl"),
                ]
            )
            == 0
        )
    assert not list(tmp_path.iterdir())


def test_task_verify_dispatches_without_writing_or_collecting(tmp_path: Path) -> None:
    output = tmp_path / "existing.jsonl"
    output.write_text("Existing evidence must remain unchanged.")
    with (
        patch("measure.verify_version", return_value=0) as verify,
        patch("measure.collect", side_effect=AssertionError("Verify must not invoke")),
    ):
        assert (
            main(
                [
                    "--project-endpoint",
                    "https://example.services.ai.azure.com/api/projects/test",
                    "--version",
                    "1",
                    "--condition",
                    "baseline",
                    "--execution",
                    "verify",
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
    verify.assert_called_once()
    assert output.read_text() == "Existing evidence must remain unchanged."


def test_task_environment_resolution_is_local_scoped_and_rejects_conflicting_aliases() -> None:
    values = {
        "FOUNDRY_PROJECT_ENDPOINT": "https://example/project",
        "AZURE_AI_PROJECT_ENDPOINT": "https://example/project",
    }
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(values))
    with patch("measure.subprocess.run", return_value=result) as process:
        assert resolve_endpoint(None, "story13-test") == "https://example/project"
    assert process.call_args.kwargs["cwd"] == Path(__file__).resolve().parents[1]
    assert process.call_args.args[0] == [
        "azd",
        "env",
        "get-values",
        "--environment",
        "story13-test",
        "--output",
        "json",
    ]
    values["AZURE_AI_PROJECT_ENDPOINT"] = "https://different/project"
    with (
        patch(
            "measure.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(values)),
        ),
        pytest.raises(ValueError, match="matching"),
    ):
        resolve_endpoint(None, "story13-test")
