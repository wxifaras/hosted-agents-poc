"""Reviewed Story 4 reference context, never execution or deployment evidence."""

from pydantic import JsonValue


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
