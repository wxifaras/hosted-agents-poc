"""Verify public lifecycle hooks and the real local HTTP protocol without Azure."""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest
from azure.ai.agentserver.core import AgentSessionContext

from main import Probe, build_host
from measure import HostedResponse

MARKER = "a" * 32


@pytest.mark.asyncio
async def test_probe_warm_state_and_restore_identity(tmp_path: Path) -> None:
    probe = Probe(tmp_path / "marker")
    await probe.before_snapshot()
    initial_instance = probe.instance
    await probe.after_restore(AgentSessionContext("session", "restore"))
    assert probe.instance != initial_instance
    first = probe.reply(MARKER)
    second = probe.reply(MARKER)
    assert first["previous_memory"] is None and first["previous_file"] is None
    assert second["previous_memory"] == MARKER and second["previous_file"] == MARKER
    assert first["request"] != second["request"]
    assert first["instance"] == second["instance"]
    assert first["restores"] == 1
    with pytest.raises(RuntimeError, match="session state"):
        await probe.before_snapshot()


def test_invalid_marker_cannot_write_state(tmp_path: Path) -> None:
    probe = Probe(tmp_path / "marker")
    for marker in ("", "../secret", "a" * 33, "Z" * 32):
        with pytest.raises(ValueError, match="synthetic marker"):
            probe.reply(marker)
    assert not probe.marker_file.exists()
    assert probe.turns == 0


def test_host_registers_public_hooks(tmp_path: Path) -> None:
    assert build_host(Probe(tmp_path / "marker")) is not None


def test_real_local_readiness_and_responses(tmp_path: Path) -> None:
    """Local success is protocol evidence, never hosted cold-start evidence."""
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    for name in ("APPLICATIONINSIGHTS_CONNECTION_STRING", "FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT"):
        env.pop(name, None)
    with (tmp_path / "host.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(root / "main.py"), "--port", str(port)],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                deadline = monotonic() + 90
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail(f"Local host exited before readiness:\n{log.read()}")
                    try:
                        ready = client.get("/readiness")
                        if ready.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    if monotonic() > deadline:
                        log.seek(0)
                        pytest.fail(f"Local readiness timeout:\n{log.read()}")
                    sleep(0.1)
                replies = []
                for _ in range(2):
                    response = client.post("/responses", json={"input": MARKER, "stream": False})
                    assert response.status_code == 200, response.text
                    body = response.json()
                    parsed = HostedResponse.model_validate_json(response.content)
                    assert parsed.output_text
                    assert body["status"] == "completed", body
                    text = body["output"][0]["content"][0]["text"]
                    replies.append(json.loads(text))
                assert replies[0]["previous_memory"] is None
                assert replies[1]["previous_memory"] == MARKER
                assert replies[1]["previous_file"] == MARKER
                assert replies[0]["request"] != replies[1]["request"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
