"""Reviewed story reference context, never execution or deployment evidence."""

from pydantic import JsonValue


def story3_api_catalog() -> list[JsonValue]:
    """Return symbolic call shapes for conversation mapping and history checks."""
    entries = [
        (
            "Client creates and invokes a Supervisor conversation",
            "Authenticated operator",
            "Hosted turn for send; no model for inspect",
            "client.conversations.create(); client.responses.create(input=command.wire(), "
            "conversation=supervisor_conversation, stream=False)",
            "scripts.run_scenario.story3_conversations.Story3Step",
            "POC_CONVERSE / poc-conversation-v1 is separate from Story 4 delegation. "
            "Missing conversation context is rejected before state access or outbound calls.",
        ),
        (
            "Separate child conversation and application-owned mapping",
            "Supervisor backend / user-isolated state store",
            "Conversation metadata and durable mapping; no model",
            "await client.conversations.create(metadata=parent_metadata); await separate_turn(turn, client_for, store)",
            "app.services.conversations.separate_turn",
            "Supervisor stores and reuses a distinct Research conversation. Parent metadata is custom, "
            "not a native parent-child relationship. Creation and mapping persistence are not atomic.",
        ),
        (
            "Research follow-up in its mapped conversation",
            "Supervisor backend calling Research",
            "Research model turn with its own hydrated history",
            "await client.responses.create(input=hop_message, conversation=child_conversation, stream=False)",
            "app.services.conversations.invoke",
            "The application does not copy Research turns into Supervisor history. Result or placeholder "
            "delivery is explicit. Recall checks and stored-item counts are separate observations.",
        ),
        (
            "Conversation access and mapping guards",
            "Operator or Supervisor agent / forwarded call context",
            "No model for history listing or inspect",
            "client.conversations.items.list(conversation); identity_headers(mode, correlation_id)",
            "app.services.conversations.list_items",
            "Allowed reads, explicit HTTP denials, and transport/service errors are distinct. "
            "An elevated operator is not a least-privileged second-user isolation test. "
            "Application correlation is not distributed-trace proof.",
        ),
        (
            "Shared conversation ID experiment",
            "Supervisor backend and same-principal operator control",
            "Attempted Research turn; later model-free Supervisor inspect",
            "await shared_turn(turn, client_for); conversation=supervisor_conversation",
            "app.services.conversations.shared_turn",
            "The Supervisor ID is supplied to Research. Recording an explicit 404 can satisfy the "
            "evaluation check without demonstrating shared history, successful delivery, or its root cause.",
        ),
        (
            "Classify result, placeholder, or full history",
            "Validation runner / later Supervisor inspection",
            "No additional model for classification",
            "classify_received(report, later.supervisor_history)",
            "scripts.run_scenario.story3_conversations.classify_received",
            "Classification uses saved outcomes and history counters, not the requested delivery mode alone. "
            "Streaming, background delivery, recovery, and Data Analysis files are not tested here.",
        ),
    ]
    return [
        dict(zip(("operation", "caller", "activity", "shape", "source", "scope"), entry, strict=True))
        for entry in entries
    ]


def story4_api_catalog() -> list[JsonValue]:
    """Return symbolic call shapes for the existing three-agent delegation design."""
    entries = [
        (
            "Client invokes Supervisor or direct Research",
            "Authenticated caller",
            "Hosted turn",
            "client.responses.create(input=command, stream=False)",
            "scripts.run_scenario.story4_common.Story4Step",
            "Direct Research bypasses Supervisor. No original-user propagation is inferred.",
        ),
        (
            "Supervisor selects a specialist",
            "Supervisor application and routing model",
            "Model tool selection",
            "tools=[delegate_to_research, delegate_to_data_analysis]",
            "app.services.orchestration",
            "The application controls the allow-list and invocation; tool selection alone is not specialist execution.",
        ),
        (
            "Specialist invocation",
            "Supervisor backend",
            "Specialist Hosted turn",
            "await client.responses.create(input=subtask.wire(), stream=False)",
            "app.services.orchestration",
            "POC_DELEGATE / POC_SUBTASK use existing Research and Data Analysis endpoints. "
            "Data Analysis is text-only in this path; native Code Interpreter adapter is pending.",
        ),
        (
            "Background invocation and polling",
            "Supervisor backend",
            "Hosted submission followed by metadata polling",
            "await client.responses.create(input=subtask.wire(), background=True, store=True, stream=False); "
            "await client.responses.retrieve(response_id)",
            "app.services.orchestration",
            "Only saved checks establish actual polling, failure handling and timing.",
        ),
        (
            "Research MCP tool",
            "Research backend / configured toolbox",
            "Tool call when selected",
            "MCPStreamableHTTPTool(...); microsoft_docs_search(...)",
            "app.runtime.research_tools",
            "The public documentation tool is not proof of end-user OAuth or a protected enterprise tool boundary.",
        ),
        (
            "Identity and application correlation",
            "Trusted platform context and Supervisor backend",
            "Headers/context, not native analysis",
            "identity_headers(mode, correlation_id)",
            "app.services.orchestration.identity_headers",
            "Agent, call-context and delegated-user modes are distinct. An existing impersonation grant does not "
            "prove denial without that grant. Application correlation is not actual distributed-trace evidence.",
        ),
    ]
    return [
        dict(zip(("operation", "caller", "activity", "shape", "source", "scope"), entry, strict=True))
        for entry in entries
    ]
