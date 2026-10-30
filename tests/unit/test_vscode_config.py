"""Validate editor task wiring without requiring an interactive VS Code session."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_task_names_are_grouped_and_limitations_are_explicit() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {task["label"]: task for task in document["tasks"]}
    categories = {"Setup", "Quality", "Agents", "Infrastructure", "Access", "Debug"}
    for label in tasks:
        category, separator, action = label.partition(": ")
        assert separator and category in categories and action
    preview = tasks["Agents: Preview hosted deployment (unsupported)"]
    assert "unsupported/skipped" in preview["detail"]
    assert preview["args"][:3] == ["deploy", "--all", "--preview"]
    for label in (
        "Agents: Test hosted workflow state and delegation",
        "Agents: Deploy and test hosted delegation",
    ):
        assert "Currently blocked" in tasks[label]["detail"]
    assert tasks["Quality: Run unit tests"]["group"] == {"kind": "test", "isDefault": True}
    assert tasks["Quality: Run all local checks"]["group"] == {"kind": "build", "isDefault": True}


def test_task_dependency_graph_and_inputs() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {task["label"]: task for task in document["tasks"]}
    assert len(tasks) == len(document["tasks"])
    assert document["options"]["cwd"] == "${workspaceFolder}"
    inputs = {entry["id"] for entry in document["inputs"]}

    def visit(label: str, ancestors: set[str]) -> None:
        assert label in tasks
        assert label not in ancestors, f"Cyclic task dependency: {label}"
        dependencies = tasks[label].get("dependsOn", [])
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        for dependency in dependencies:
            visit(dependency, ancestors | {label})

    for label, task in tasks.items():
        visit(label, set())
        for argument in task.get("args", []) if isinstance(task.get("args", []), list) else []:
            for input_id in re.findall(r"\$\{input:([^}]+)\}", argument):
                assert input_id in inputs


def test_setup_is_frozen_sequential_and_preserves_extras() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {task["label"]: task for task in document["tasks"]}
    setup = tasks["Setup: Prepare local environment"]
    assert setup["dependsOrder"] == "sequence"
    assert setup["dependsOn"] == ["Setup: Sync dependencies", "Setup: Check Python environment"]
    sync = tasks["Setup: Sync dependencies"]
    assert sync["type"] == "process"
    assert sync["command"] == "uv"
    assert sync["args"] == ["sync", "--frozen", "--group", "validation", "--inexact"]
    assert "--all-extras" in tasks["Setup: Sync dependencies with optional integrations"]["args"]
    for task in tasks.values():
        if task.get("command") == "uv" and task["args"][0] == "run":
            assert "--no-sync" in task["args"]
            assert "--frozen" in task["args"]


def test_live_smoke_task_is_explicit_and_separate_from_setup_and_checks() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {task["label"]: task for task in document["tasks"]}
    label = "Agents: Smoke test all local agents"
    task = tasks[label]
    assert task["type"] == "process"
    assert task["command"] == "uv"
    assert task["args"] == [
        "run",
        "--frozen",
        "--no-sync",
        "--group",
        "validation",
        "python",
        "-m",
        "scripts.run_scenario.run_scenarios",
        "--scenario",
        "smoke",
        "--mode",
        "local",
    ]
    assert "dependsOn" not in task
    assert "runOptions" not in task
    assert label not in tasks["Setup: Prepare local environment"]["dependsOn"]
    assert label not in tasks["Quality: Run all local checks"]["dependsOn"]


def test_debug_launch_owns_processes_and_pins_distinct_local_ports() -> None:
    document = json.loads((ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8"))
    configs = document["configurations"]
    assert configs[0]["name"] == "Supervisor host"
    direct = [config for config in configs if config["request"] == "launch"]
    assert {config["module"] for config in direct} == {
        "app.agents.supervisor.main",
        "app.agents.research.main",
        "app.agents.data_analysis.main",
    }
    assert {config["env"]["PORT"] for config in direct} == {"8088", "8089", "8090"}
    for config in direct:
        assert config["type"] == "debugpy"
        assert config["python"] == "${workspaceFolder}\\.venv\\Scripts\\python.exe"
        assert config["env"]["ENVIRONMENT"] == "development"
        assert config["cwd"] == "${workspaceFolder}"
        assert "preLaunchTask" not in config
    tasks = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    labels = {task["label"] for task in tasks}
    for config in configs:
        if "preLaunchTask" in config:
            assert config["preLaunchTask"] in labels
        assert "postDebugTask" not in config  # Never terminate all workspace tasks.


def test_inspector_task_does_not_wait_for_attachment_before_readiness() -> None:
    document = json.loads((ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8"))
    tasks = {task["label"]: task for task in document["tasks"]}
    host = tasks["Debug: Start Supervisor for Inspector"]
    assert host["type"] == "process"
    assert "--wait-for-client" not in host["args"]
    assert host["options"]["env"] == {"PORT": "8088", "ENVIRONMENT": "development"}
    assert tasks["Debug: Open Supervisor Inspector"]["dependsOn"] == ["Debug: Start Supervisor for Inspector"]
    inputs = {item["id"]: item for item in document["inputs"]}
    inspector = inputs["openSupervisorInspector"]
    assert inspector["type"] == "command"
    assert inspector["command"] == "ai-mlstudio.openTestTool"
    assert inspector["args"]["port"] == 8088
    launch = json.loads((ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8"))
    attach = next(config for config in launch["configurations"] if config["request"] == "attach")
    assert attach["connect"] == {"host": "127.0.0.1", "port": 5679}
