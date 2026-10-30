"""Sanitized actual conversation observations, not reconstructed trace events."""

import asyncio
from collections import Counter
from collections.abc import Sequence
from hashlib import sha256

from app.services.story6_approval import PREFIX, Command, Reply
from openai import AsyncOpenAI
from pydantic import JsonValue, ValidationError

from scripts.deploy.grant_local_rbac import az, object_value


async def verified_operator(tenant: str) -> str:
    """Resolve the actual CLI user through authenticated Graph, never token decoding."""
    account = object_value(await asyncio.to_thread(az, ["account", "show"]))
    user = account.get("user")
    if account.get("tenantId") != tenant or not isinstance(user, dict) or user.get("type") != "user":
        raise ValueError("History audit requires the selected tenant's authenticated CLI user.")
    graph = object_value(
        await asyncio.to_thread(
            az,
            [
                "ad",
                "signed-in-user",
                "show",
                "--query",
                "{id:id,userPrincipalName:userPrincipalName}",
            ],
        )
    )
    principal = graph.get("id")
    graph_name = graph.get("userPrincipalName")
    if (
        not isinstance(principal, str)
        or not principal
        or not isinstance(graph_name, str)
        or str(user.get("name", "")).casefold() != graph_name.casefold()
    ):
        raise ValueError("Authenticated Graph user identity is unavailable.")
    return principal


def alias(operation: str, kind: str, value: str) -> str:
    """Use per-operation aliases; they convey no authority and reveal no raw identifiers."""
    return sha256(f"story6-audit|{operation}|{kind}|{value}".encode()).hexdigest()


def summarize(items: Sequence[object], operation: str) -> dict[str, JsonValue]:
    """Count actual stored command/reply items and validate their application bindings."""
    counts: Counter[str] = Counter()
    approvals: set[str] = set()
    decision_refs: list[str] = []
    phases: list[str] = []
    decisions: list[bool] = []
    calls: list[int] = []
    tools: set[str] = set()
    mismatches = 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Conversation item is not an object.")
        if item.get("type") != "message":
            counts["non_message_items"] += 1
            continue
        role = item.get("role")
        counts["user_items" if role == "user" else "assistant_items" if role == "assistant" else "other_items"] += 1
        content = item.get("content")
        if not isinstance(content, list):
            raise ValueError("Conversation message content is incompatible.")
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("Conversation content part is not an object.")
            if part.get("type") not in ("input_text", "output_text"):
                continue
            text = part.get("text")
            if not isinstance(text, str) or len(text) > 40_000:
                raise ValueError("Conversation text exceeds its bounded expected shape.")
            try:
                if role == "user" and text.startswith(PREFIX):
                    command = Command.model_validate_json(text[len(PREFIX) :])
                    mismatches += str(command.operation_id) != operation
                    counts[f"{command.operation}_commands"] += 1
                    if command.approval_ref is not None:
                        decision_refs.append(command.approval_ref)
                    if command.approve is not None:
                        decisions.append(command.approve)
                elif role == "assistant" and '"poc-story6-approval-v1"' in text:
                    reply = Reply.model_validate_json(text)
                    mismatches += str(reply.operation_id) != operation
                    phases.append(reply.phase)
                    calls.append(reply.mcp_calls_reported)
                    if reply.pending is not None:
                        approvals.add(reply.pending.reference)
                        tools.add(reply.pending.tool_name)
                else:
                    counts["unclassified_text_parts"] += 1
            except ValidationError as error:
                raise ValueError(
                    "Stored Story6 item could not be validated; history evidence is inconclusive."
                ) from error
    return {
        "items": len(items),
        "counts": dict(counts),
        "observed_phases": [str(phase) for phase in phases],
        "observed_decisions": [bool(decision) for decision in decisions],
        "reported_mcp_call_counts": [int(count) for count in calls],
        "operation_mismatches": mismatches,
        "pending_approval_count": len(approvals),
        "observed_pending_tools": [str(tool) for tool in sorted(tools)],
        "decision_reference_matches_pending": all(value in approvals for value in decision_refs)
        if decision_refs
        else None,
        "raw_history_exported": False,
        "joined_distributed_trace_verified": False,
        "downstream_original_user_verified": False,
    }


async def snapshot(client: AsyncOpenAI, conversation: str, operation: str) -> dict[str, JsonValue]:
    """Read at most two pages; never silently truncate history or create new items."""
    page = await client.conversations.items.list(conversation, limit=100, order="asc")
    items: list[object] = [item.model_dump(mode="json") for item in page.data]
    pages = 1
    while page.has_next_page():
        if pages >= 2:
            raise ValueError("Conversation exceeds the two-page audit guard; evidence is not silently truncated.")
        page = await page.get_next_page()
        items.extend(item.model_dump(mode="json") for item in page.data)
        pages += 1
    observed = summarize(items, operation)
    observed["history_pages"] = pages
    return observed
