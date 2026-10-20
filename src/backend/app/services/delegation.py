"""Explicit POC commands and supported caller-delegated specialist calls."""

import re
from typing import Literal

from azure.ai.projects.aio import AIProjectClient
from openai import AsyncOpenAI
from pydantic import UUID4, BaseModel, ConfigDict, Field

from app.core.redaction import sanitize_text
from app.core.settings import AgentName
from app.services.foundry_state import Caller, ContextPresence
from app.services.workflows import Child, Workflow, WorkflowError

COMMAND_PREFIX = "POC_WORKFLOW "
CONTEXT_PREFIX = "POC_CONTEXT "
SPECIALISTS: dict[Literal["research", "data_analysis"], str] = {
    "research": "research-agent",
    "data_analysis": "data-analysis-agent",
}


class Command(BaseModel):
    """Public labels and task text only; never accept ownership/auth fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["create", "inspect", "continue"]
    workflow_id: UUID4
    message: str = Field(default="", max_length=4000)

    def wire(self) -> str:
        """Encode a deterministic command, outside model-generated tool arguments."""
        return COMMAND_PREFIX + self.model_dump_json()


class ContextCommand(BaseModel):
    """Read-only direct probe or one Supervisor-to-Research hop."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["direct", "research"] = "direct"

    def wire(self) -> str:
        """Keep the diagnostic outside ordinary model prompts."""
        return CONTEXT_PREFIX + self.model_dump_json()


class ContextReport(BaseModel):
    """A bounded diagnostic response with no actual context identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["poc-context-v1"] = "poc-context-v1"
    agent: AgentName
    presence: ContextPresence
    caller_matches_expected: bool | None
    research: ContextReport | None = None


class Report(BaseModel):
    """Readable IDs and results; no caller ID, call ID, credentials, or hidden reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["poc-workflow-v1"] = "poc-workflow-v1"
    agent: AgentName
    workflow_id: UUID4
    owner_id: UUID4
    session_id: UUID4
    conversation_id: str
    platform_session_id: str
    progress: Literal["new", "running", "ready"]
    caller_propagation_verified: bool | None = None
    answer: str
    children: dict[str, Child]

    @classmethod
    def from_workflow(cls, workflow: Workflow, caller: Caller, caller_matches: bool | None) -> Report:
        """Expose partition labels without treating them as proof of access."""
        return cls(
            agent=workflow.agent,
            workflow_id=workflow.workflow_id,
            owner_id=workflow.owner_id,
            session_id=workflow.session_id,
            conversation_id=workflow.conversation_id,
            platform_session_id=caller.platform_session_id,
            progress=workflow.progress,
            caller_propagation_verified=caller_matches,
            answer=workflow.last_answer,
            children=workflow.children,
        )


class SpecialistClient:
    """Call agent-bound Responses endpoints as the verified original end user."""

    def __init__(self, project: AIProjectClient, caller: Caller, timeout: float) -> None:
        self.project = project
        self.caller = caller
        self.timeout = timeout

    def _client(self, agent: Literal["research", "data_analysis"]) -> AsyncOpenAI:
        if not re.fullmatch(r"[A-Za-z0-9._:@-]{1,256}", self.caller.user_id):
            raise WorkflowError("Verified user ID is incompatible with the documented delegation-header format.")
        return self.project.get_openai_client(
            agent_name=SPECIALISTS[agent],
            timeout=self.timeout,
            max_retries=0,
            default_headers={
                "x-agent-foundry-call-id": self.caller.call_id,
                "x-ms-user-identity": self.caller.user_id,
                # Verification only. Authorization always comes from platform context.
                "x-client-expected-caller": self.caller.user_id,
            },
        )

    async def create_conversation(self, agent: Literal["research", "data_analysis"]) -> str:
        """Get an actual platform conversation ID, never a synthesized child ID."""
        async with self._client(agent) as client:
            conversation = await client.conversations.create()
        if not conversation.id:
            raise WorkflowError("Specialist platform did not return a conversation ID.")
        return conversation.id

    async def probe_research(self) -> ContextReport:
        """Use the same initial client/conversation path, without model or state work."""
        conversation_id = await self.create_conversation("research")
        async with self._client("research") as client:
            response = await client.responses.create(
                input=ContextCommand().wire(), conversation=conversation_id, stream=False, extra_body={}
            )
        if response.status != "completed" or response.error:
            detail = sanitize_text(response.error.message) if response.error else str(response.status)
            raise WorkflowError(f"Research context probe failed: {detail}")
        report = ContextReport.model_validate_json(response.output_text)
        if report.agent != "research" or report.research is not None:
            raise WorkflowError("Expected exactly one Research diagnostic hop.")
        return report

    async def invoke(self, child: Child, message: str, *, create: bool) -> tuple[Child, str]:
        """Reuse persisted remote scope on continuation; never retry model requests."""
        if not create and (
            not child.platform_session_id or not child.owner_id or not child.session_id or not child.response_id
        ):
            raise WorkflowError("Specialist continuation requires complete persisted child mappings.")
        command = Command(operation="create" if create else "continue", workflow_id=child.workflow_id, message=message)
        extra_body: dict[str, object] = {}
        if child.platform_session_id:
            extra_body["agent_session_id"] = child.platform_session_id
        async with self._client(child.agent) as client:
            response = await client.responses.create(
                input=command.wire(), conversation=child.conversation_id, stream=False, extra_body=extra_body
            )
        if response.status != "completed" or response.error:
            detail = sanitize_text(response.error.message) if response.error else str(response.status)
            raise WorkflowError(
                f"{child.agent} hosted response {response.id} failed: {detail}; parent remains incomplete."
            )
        report = Report.model_validate_json(response.output_text)
        if (
            report.agent != child.agent
            or report.workflow_id != child.workflow_id
            or report.conversation_id != child.conversation_id
            or report.progress != "ready"
            or report.caller_propagation_verified is not True
        ):
            raise WorkflowError("Specialist result or verified caller propagation did not match its binding.")
        if not create and (
            report.platform_session_id != child.platform_session_id
            or report.owner_id != child.owner_id
            or report.session_id != child.session_id
        ):
            raise WorkflowError("Specialist returned different durable scope during continuation.")
        return (
            child.model_copy(
                update={
                    "platform_session_id": report.platform_session_id,
                    "owner_id": report.owner_id,
                    "session_id": report.session_id,
                    "response_id": response.id,
                }
            ),
            report.answer,
        )
