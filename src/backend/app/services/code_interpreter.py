"""Return managed Code Interpreter container references with Data Analysis answers.

Generated files stay in the Microsoft-managed sandbox. The answer carries only the
container IDs; an authenticated client lists and downloads the files it is allowed to.
"""

import json
import re
from collections.abc import Iterable

from agent_framework import Message

ARTIFACTS_FENCE = "poc-artifacts-v1"
_CONTAINER_ID = re.compile(r"\bcntr_[A-Za-z0-9]{8,}\b")


def container_ids(messages: Iterable[Message]) -> list[str]:
    """Collect sandbox container IDs from tool results only, never from model-written text."""
    found: list[str] = []
    for message in messages:
        for content in message.contents:
            if content.type != "function_result":
                continue
            for match in _CONTAINER_ID.findall(str(content.result or "")):
                if match not in found:
                    found.append(match)
    return found


def with_artifacts(text: str, containers: list[str]) -> str:
    """Append a machine-readable reference block only when the sandbox was used."""
    if not containers:
        return text
    block = json.dumps({"containers": containers})
    return f"{text.rstrip()}\n\n```{ARTIFACTS_FENCE}\n{block}\n```"


def parse_artifacts(text: str) -> list[str]:
    """Read the container references a Data Analysis answer carries, if any."""
    match = re.search(rf"```{ARTIFACTS_FENCE}\s*(\{{.*?\}})\s*```", text, re.S)
    if not match:
        return []
    value = json.loads(match.group(1))
    containers = value.get("containers") if isinstance(value, dict) else None
    if not isinstance(containers, list):
        return []
    return [item for item in containers if isinstance(item, str) and _CONTAINER_ID.fullmatch(item)]
