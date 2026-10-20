"""Resolve timestamped subscription deployments without confusing environment names with operation names."""

import re
from collections.abc import Callable
from datetime import datetime

from pydantic import JsonValue


def resolve_deployment(
    read_json: Callable[[list[str]], JsonValue],
    subscription: str,
    environment: str,
    explicit: str | None = None,
) -> str:
    """Use an explicit name unchanged, or find the newest successful exact-environment deployment."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", environment):
        raise ValueError("Environment must be a lowercase POC name of at most 40 characters.")
    if explicit:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", explicit):
            raise ValueError("Invalid explicit deployment name.")
        return explicit
    query = (
        "[?properties.provisioningState=='Succeeded' && "
        f"properties.parameters.environmentName.value=='{environment}']."
        "{name:name,time:properties.timestamp,environment:properties.parameters.environmentName.value,"
        "state:properties.provisioningState}"
    )
    result = read_json(["deployment", "sub", "list", "--subscription", subscription, "--query", query])
    if not isinstance(result, list):
        raise ValueError("Azure returned a malformed deployment list.")
    candidates: list[tuple[datetime, str]] = []
    for entry in result:
        if not isinstance(entry, dict):
            raise ValueError("Azure returned a malformed deployment entry.")
        if entry.get("environment") != environment or entry.get("state") != "Succeeded":
            continue
        name, timestamp = entry.get("name"), entry.get("time")
        if not isinstance(name, str) or not isinstance(timestamp, str):
            raise ValueError("Matching deployment is missing its name or timestamp.")
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("Deployment timestamp has no timezone.")
        candidates.append((observed, name))
    if not candidates:
        raise ValueError(
            f"No successful subscription deployment found for environment '{environment}'. "
            "Check the subscription/environment or specify --deployment explicitly."
        )
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise ValueError("Latest deployment is ambiguous; specify --deployment explicitly.")
    return candidates[0][1]
