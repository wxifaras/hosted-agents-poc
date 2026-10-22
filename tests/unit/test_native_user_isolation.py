"""Offline safeguards for genuine-user testing; no real CLI credentials or Azure calls."""

import json
import os
import subprocess
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import httpx2
import pytest
from app.services.foundry_state import ContextPresence
from app.services.identity_checks import IdentityCommand, IdentityReply
from openai import APIStatusError, AsyncOpenAI

from scripts.common.auth_profiles import ProfileError
from scripts.common.reporting import ValidationRun
from scripts.run_scenario import native_user_isolation as native

TENANT = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("wrong_graph_user", [False, True])
def test_preflight_confirms_actual_profile_identity_without_switching_operator(
    wrong_graph_user: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user, principal = "user1@example.com", str(uuid4())
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "operator"))
    monkeypatch.setattr(native, "profile_path", lambda *_args: tmp_path)
    monkeypatch.setattr(native, "command_prefix", lambda _name: ["synthetic-az"])
    metadata = MagicMock(
        side_effect=[
            {"tenant": TENANT, "username": user, "kind": "user"},
            {"id": principal, "username": "another@example.com" if wrong_graph_user else user},
        ]
    )
    monkeypatch.setattr(native, "profile_json", metadata)
    if wrong_graph_user:
        with pytest.raises(ProfileError, match="Graph user"):
            native.prepare_user(user, TENANT)
    else:
        assert native.prepare_user(user, TENANT).principal == principal
    assert all(call.args[2]["AZURE_CONFIG_DIR"] == str(tmp_path) for call in metadata.call_args_list)
    assert os.environ["AZURE_CONFIG_DIR"] == str(tmp_path / "operator")


async def test_credentials_use_distinct_child_environments_without_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "operator"))
    monkeypatch.setattr(native, "command_prefix", lambda _name: ["synthetic-az"])
    calls = MagicMock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"accessToken": "sensitive-token", "expires_on": int(time()) + 3600}), ""
        )
    )
    monkeypatch.setattr(native.subprocess, "run", calls)
    for i in (1, 2):
        user = native.NativeUser(f"user{i}@example.com", str(uuid4()), TENANT, tmp_path / str(i))
        credential = native.ProfileCredential(user)
        token = await credential.get_token("https://ai.azure.com/.default")
        assert token.token == "sensitive-token"
        assert await credential.get_token("https://ai.azure.com/.default") is token
        assert calls.call_args.kwargs["env"]["AZURE_CONFIG_DIR"] == str(user.path)
        assert "login" not in calls.call_args.args[0]
        await credential.close()
    assert calls.call_count == 2
    assert os.environ["AZURE_CONFIG_DIR"] == str(tmp_path / "operator")
    assert "sensitive-token" not in capsys.readouterr().out


async def test_native_token_failures_do_not_leak_sensitive_cli_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(native, "command_prefix", lambda _name: ["synthetic-az"])
    monkeypatch.setattr(
        native.subprocess,
        "run",
        MagicMock(return_value=subprocess.CompletedProcess([], 1, "sensitive-token", "sensitive-token")),
    )
    credential = native.ProfileCredential(native.NativeUser("a@example.com", str(uuid4()), TENANT, tmp_path))
    with pytest.raises(ProfileError) as error:
        await credential.get_token("https://ai.azure.com/.default")
    assert "sensitive-token" not in str(error.value)
    with pytest.raises(ProfileError):
        await credential.get_token("https://graph.microsoft.com/.default")


@pytest.mark.parametrize(
    ("denied", "owner_marker", "owner_session"),
    [
        (True, True, "owner-session"),
        (False, True, "owner-session"),
        (True, False, "owner-session"),
        (True, True, "wrong-session"),
    ],
)
async def test_real_user_sequence_preserves_negative_and_post_attempt_positive_checks(
    denied: bool, owner_marker: bool, owner_session: str, tmp_path: Path
) -> None:
    reply = IdentityReply(
        presence=ContextPresence(
            hosted_runtime=True,
            call_id_present=True,
            user_id_present=True,
            session_id_present=True,
            conversation_id_present=False,
        ),
        expected_header_present=False,
        user_matches_expected=None,
        marker_found=True,
        marker_fingerprint="owned-marker",
        history_marker="owned-history",
        history_contains_marker=True,
    )
    hidden = reply.model_copy(update={"marker_found": False, "marker_fingerprint": None})
    error = APIStatusError(
        "synthetic",
        response=httpx.Response(404, request=httpx.Request("POST", "https://example.invalid/responses")),
        body={"error": {"code": "not_found"}},
    )
    owner, other = AsyncMock(), AsyncMock()
    owner.responses.retrieve.return_value = SimpleNamespace(id="owner-second", status="completed", error=None)
    send = AsyncMock(
        side_effect=[
            (reply, "presence-a", "owner-session"),
            (reply, "presence-b", "other-session"),
            (reply, "create", "owner-session"),
            (reply, "read", "owner-session"),
            (hidden, "hidden", "other-session"),
            (reply, "seed", "owner-session"),
            (reply, "owner-second", "owner-session"),
            error if denied else (reply, "other-accepted-1", "owner-session"),
            error if denied else (reply, "other-accepted-2", "owner-session"),
            (reply.model_copy(update={"history_contains_marker": owner_marker}), "owner-final", owner_session),
            (reply, "final-state", "owner-session"),
        ]
    )
    run = ValidationRun("native-offline", "unit", tmp_path, no_color=True)
    await native.direction(owner, other, SimpleNamespace(send=send), run, "native")
    assert run.finish() == (0 if denied and owner_marker and owner_session == "owner-session" else 1)
    assert send.await_args_list[7].args == (other, "history-read")
    assert send.await_args_list[7].kwargs == {"previous": "owner-second", "session": None}
    assert send.await_args_list[8].kwargs == {"previous": "owner-second", "session": "owner-session"}
    assert send.await_args_list[9].args == (owner, "history-read")
    assert send.await_args_list[9].kwargs == {"previous": "owner-second", "session": "owner-session"}
    assert owner.responses.retrieve.await_count == (2 if denied else 0)


@pytest.mark.parametrize("extra_header", [None, "x-ms-user-identity", "x-agent-foundry-call-id", "x-agent-user-id"])
def test_actual_request_inspection_detects_non_native_headers_without_returning_tokens(
    extra_header: str | None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = ValidationRun("native-headers", "unit", tmp_path, no_color=True)
    client = MagicMock()
    runner = native.NativeRunner(MagicMock(), run, {id(client): "user1"})
    headers = {"authorization": "Bearer sensitive-token"}
    if extra_header:
        headers[extra_header] = "sensitive-identity"
    command = IdentityCommand(operation="history-read", run_id=uuid4())
    runner.inspect_egress(
        client,
        "https://example.invalid/responses",
        headers,
        content=json.dumps({"input": command.wire()}).encode(),
        previous="owned-response",
        session=None,
    )
    assert all(value is True for value in runner.egress[0].values()) is (extra_header is None)
    output = capsys.readouterr().out
    assert "sensitive-token" not in output and "sensitive-identity" not in output


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("status", [200, 403, 500])
async def test_exchange_details_are_real_sanitized_and_do_not_change_assertion_status(
    verbose: bool, status: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = IdentityReply(
        presence=ContextPresence(
            hosted_runtime=True,
            call_id_present=True,
            user_id_present=True,
            session_id_present=True,
            conversation_id_present=False,
        ),
        expected_header_present=False,
        user_matches_expected=None,
        history_marker="SYNTHETIC-CANARY",
        history_contains_marker=True,
    )

    def capture(request: httpx2.Request) -> httpx2.Response:
        headers = {
            "x-agent-session-id": "owned-session",
            "x-ms-request-id": "safe-trace-id",
            "set-cookie": "sensitive-cookie",
            "x-agent-foundry-call-id": "sensitive-call-id",
        }
        if status != 200:
            return httpx2.Response(
                status,
                headers=headers,
                json={
                    "error": {
                        "code": "session_not_accessible" if status == 403 else "server_error",
                        "message": "sensitive-server-message",
                    },
                },
            )
        return httpx2.Response(
            200,
            headers=headers,
            json={
                "id": "response-owned",
                "object": "response",
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "id": "message-owned",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": report.model_dump_json(), "annotations": []}],
                    }
                ],
            },
        )

    run = ValidationRun("native-exchanges", "unit", tmp_path, no_color=True, verbose=verbose)
    async with AsyncOpenAI(
        api_key="sensitive-token",
        base_url="https://example.invalid/openai",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture), trust_env=False),
        max_retries=0,
    ) as client:
        runner = native.NativeRunner(MagicMock(), run, {id(client): "user1"})
        if status == 200:
            actual, response_id, session = await runner.send(
                client, "history-read", previous="owned-previous", session="owned-session"
            )
            assert actual == report and response_id == "response-owned" and session == "owned-session"
        else:
            with pytest.raises(APIStatusError) as error:
                await runner.send(client, "history-read", previous="owned-previous", session="owned-session")
            assert error.value.status_code == status
    text = (run.path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]
    assert [event["kind"] for event in events] == ["request", "response"]
    request, response = [event["details"] for event in events]
    assert request["actor"] == "user1" and request["operation"] == "history-read"
    assert request["previous_response_id"] == "owned-previous"
    assert request["explicit_agent_session_id"] == "owned-session"
    assert request["http"]["uri"] == "https://example.invalid/openai/responses"
    assert response["http"]["status_code"] == status
    assert response["http"]["response_headers"]["x-ms-request-id"] == "safe-trace-id"
    if status == 200:
        assert response["diagnostic_reply"]["history_marker"] == "SYNTHETIC-CANARY"
        assert response["response_id"] == "response-owned"
    else:
        assert response["error_code"] == ("session_not_accessible" if status == 403 else "server_error")
        assert "diagnostic_reply" not in response
    output = capsys.readouterr().out
    assert ("REQUEST" in output and "RESPONSE" in output) is verbose
    for sensitive in ("sensitive-token", "sensitive-cookie", "sensitive-call-id", "sensitive-server-message"):
        assert sensitive not in output + text
    assert run.statuses == []


def test_native_task_prompts_for_verbose_or_summary_output() -> None:
    config = json.loads((Path(__file__).resolve().parents[2] / ".vscode" / "tasks.json").read_text())
    task = next(task for task in config["tasks"] if task["label"] == "Agents: Test native Entra user isolation")
    assert "${input:nativeIsolationVerbosity}" in task["args"]
    selection = next(item for item in config["inputs"] if item["id"] == "nativeIsolationVerbosity")
    assert selection["type"] == "pickString"
    assert [option["value"] for option in selection["options"]] == ["--verbose", "--no-verbose"]
    assert selection["default"] == "--verbose"
    for option in selection["options"]:
        parsed = native.build_parser().parse_args(["--users", "a@example.com,b@example.com", option["value"]])
        assert parsed.verbose is (option["value"] == "--verbose")
    assert native.build_parser().parse_args(["--users", "a@example.com,b@example.com"]).verbose is False
    assert "--run" in task["args"]
