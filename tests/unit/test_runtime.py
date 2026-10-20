"""Prove actual SDK host startup/readiness without authenticating or invoking a model."""

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from app.core.settings import AGENT_NAMES, AgentName


@pytest.mark.parametrize(
    ("name", "debug"),
    [(name, False) for name in AGENT_NAMES] + [("supervisor", True)],
)
def test_actual_host_readiness(name: AgentName, debug: bool, tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/poc",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "not-invoked-in-this-test",
        "PORT": str(port),
        "POC_ENV_FILE": str(tmp_path / "unused.env"),
        "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
    }
    log = tmp_path / f"{name}.log"
    command = [sys.executable, "-m", f"app.agents.{name}.main"]
    matcher = None
    if debug:
        tasks = json.loads((Path(__file__).resolve().parents[2] / ".vscode" / "tasks.json").read_text())
        task = next(task for task in tasks["tasks"] if task["label"] == "Debug: Start Supervisor for Inspector")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            debug_port = listener.getsockname()[1]
        command = [
            sys.executable,
            *[argument.replace("127.0.0.1:5679", f"127.0.0.1:{debug_port}") for argument in task["args"]],
        ]
        matcher = task["problemMatcher"]["background"]["endsPattern"].replace("8088", str(port))
        environment.update(task["options"]["env"])
        environment["PORT"] = str(port)
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 35
            with httpx.Client(trust_env=False, timeout=1) as client:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        pytest.fail(log.read_text(encoding="utf-8"))
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/readiness")
                    except httpx.TransportError:
                        time.sleep(0.2)
                    else:
                        assert response.status_code == 200
                        if matcher:
                            assert re.search(matcher, log.read_text(encoding="utf-8")), (
                                "Inspector task readiness matcher must recognize actual SDK startup output"
                            )
                        return
            pytest.fail(f"Host did not become ready: {log.read_text(encoding='utf-8')}")
        finally:
            process.terminate()
            process.wait(timeout=10)
