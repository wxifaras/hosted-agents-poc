"""History evidence is based on actual items, with no private transcript export."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.services.story6_approval import Command, PendingApproval, Reply

from scripts.run_scenario import story6_history as history


def message(role, text):
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }


def pending(operation):
    return Reply(
        operation_id=operation,
        phase="pending",
        pending=PendingApproval(reference="1" * 64),
        decision=None,
        answer="",
        research_response_observed=True,
        research_user_context_present=False,
        provider_responses_observed=1,
        mcp_calls_reported=0,
        file_sha256="2" * 64,
        file_retained=True,
        same_process=True,
    )


def test_actual_item_shapes_link_pending_decision_and_reported_invocation_without_raw_text() -> None:
    operation = uuid4()
    request = pending(operation)
    completed = request.model_copy(
        update={
            "phase": "completed",
            "pending": None,
            "decision": True,
            "mcp_calls_reported": 1,
            "answer": "PRIVATE_MODEL_ANSWER",
            "provider_responses_observed": 2,
        }
    )
    items = [
        message("user", Command(operation="start", operation_id=operation).wire()),
        message("assistant", request.model_dump_json()),
        message(
            "user",
            Command(
                operation="decide", operation_id=operation, approval_ref=request.pending.reference, approve=True
            ).wire(),
        ),
        message("assistant", completed.model_dump_json()),
    ]
    result = history.summarize(items, str(operation))
    assert result["items"] == 4
    assert result["counts"] == {"user_items": 2, "assistant_items": 2, "start_commands": 1, "decide_commands": 1}
    assert result["observed_phases"] == ["pending", "completed"]
    assert result["reported_mcp_call_counts"] == [0, 1]
    assert result["observed_decisions"] == [True]
    assert result["operation_mismatches"] == 0
    assert result["decision_reference_matches_pending"] is True
    assert result["observed_pending_tools"] == ["microsoft_docs_search"]
    assert result["raw_history_exported"] is False
    serialized = json.dumps(result)
    for private in (str(operation), "1" * 64, "PRIVATE_MODEL_ANSWER"):
        assert private not in serialized
    assert history.summarize([], str(operation))["items"] == 0


def test_mismatched_operation_and_unknown_approval_reference_are_visible_failures() -> None:
    operation = uuid4()
    items = [
        message("assistant", pending(operation).model_dump_json()),
        message(
            "user",
            Command(
                operation="decide",
                operation_id=uuid4(),
                approval_ref="9" * 64,
                approve=True,
            ).wire(),
        ),
    ]
    result = history.summarize(items, str(operation))
    assert result["operation_mismatches"] == 1
    assert result["decision_reference_matches_pending"] is False


@pytest.mark.parametrize(
    "items",
    [
        [{"type": "message", "role": "assistant", "content": None}],
        [message("user", "POC_STORY6 {")],
        [message("assistant", '{"protocol":"poc-story6-approval-v1"}')],
    ],
)
def test_invalid_history_is_not_a_passing_empty_history(items) -> None:
    with pytest.raises(ValueError):
        history.summarize(items, str(uuid4()))


async def test_history_pagination_is_bounded_and_all_pages_are_counted() -> None:
    page2 = SimpleNamespace(data=[], has_next_page=lambda: False)
    page1 = SimpleNamespace(data=[], has_next_page=lambda: True, get_next_page=AsyncMock(return_value=page2))
    client = MagicMock()
    client.conversations.items.list = AsyncMock(return_value=page1)
    observed = await history.snapshot(client, "private-conversation", str(uuid4()))
    assert observed["history_pages"] == 2 and observed["items"] == 0
    page2.has_next_page = lambda: True
    with pytest.raises(ValueError, match="two-page"):
        await history.snapshot(client, "private-conversation", str(uuid4()))


async def test_identity_verification_and_aliases_do_not_export_profile_details(monkeypatch) -> None:
    def cli(arguments):
        if arguments[:2] == ["account", "show"]:
            return {"tenantId": "private-tenant", "user": {"type": "user", "name": "private@example.test"}}
        return {"id": "private-principal", "userPrincipalName": "private@example.test"}

    monkeypatch.setattr(history, "az", cli)
    assert await history.verified_operator("private-tenant") == "private-principal"
    first = history.alias("operation-one", "caller", "private-principal")
    assert first != history.alias("operation-two", "caller", "private-principal")
    assert "private" not in first
    with pytest.raises(ValueError, match="tenant"):
        await history.verified_operator("other-tenant")


async def test_real_responses_sdk_conversation_item_listing_uses_bound_conversation() -> None:
    import httpx2
    from openai import AsyncOpenAI

    operation = uuid4()
    request = pending(operation)
    calls = []

    def transport(wire_request):
        calls.append(wire_request)
        assert wire_request.method == "GET"
        assert wire_request.url.path == "/conversations/conversation-private/items"
        assert wire_request.url.params["order"] == "asc"
        items = [
            {
                "id": "message-user",
                "status": "completed",
                **message("user", Command(operation="start", operation_id=operation).wire()),
            },
            {"id": "message-assistant", "status": "completed", **message("assistant", request.model_dump_json())},
        ]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": items,
                "has_more": False,
                "first_id": "message-user",
                "last_id": "message-assistant",
            },
        )

    async with AsyncOpenAI(
        api_key="synthetic-not-a-credential",
        base_url="https://synthetic.invalid",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
    ) as client:
        result = await history.snapshot(client, "conversation-private", str(operation))
    assert result["items"] == 2 and result["observed_phases"] == ["pending"]
    assert result["operation_mismatches"] == 0 and len(calls) == 1
