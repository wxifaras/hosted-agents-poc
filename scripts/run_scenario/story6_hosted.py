"""Pinned-session Story6 client; decisions require an explicit operator action.

Private case locators stay under ignored artifacts. No tokens or provider
continuation identifiers are exported, and submissions are never replayed.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.services.story6_approval import SEARCH_QUERY, Command, Reply
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import VersionRefIndicator
from azure.identity.aio import AzureCliCredential
from openai import AsyncOpenAI
from pydantic import UUID4, BaseModel, ConfigDict

from scripts.deploy.story6_package import ROOT, SERVICE, deployment_context


class Case(BaseModel):
    """Private locator, never an authorization substitute or portable evidence."""

    model_config = ConfigDict(extra="forbid")
    environment: str
    version: str
    operation_id: UUID4
    session_id: str | None = None
    conversation_id: str | None = None
    phase: str = "new"
    last_reply: Reply | None = None
    audit_principal_hash: str | None = None
    audit_history: bool = False


def save(path: Path, case: Case) -> None:
    """Persist intent before submission; never erase ambiguous references."""
    path.write_text(case.model_dump_json(indent=2), encoding="utf-8")


def load(path: Path, environment: str) -> Case:
    """Accept an explicit local Story6 locator only, with bounded input size."""
    resolved = path.resolve()
    if not resolved.is_relative_to((ROOT / "artifacts" / "story-6").resolve()):
        raise ValueError("Select the exact private case-state.json under artifacts/story-6.")
    if resolved.stat().st_size > 200_000:
        raise ValueError("Case locator exceeds the local size guard.")
    case = Case.model_validate_json(resolved.read_bytes())
    if case.environment != environment:
        raise ValueError("Case environment does not match the selected environment.")
    return case


def public_observation(reply: Reply) -> dict[str, object]:
    """Export observed state without private session/conversation/approval references."""
    observed: dict[str, object] = reply.model_dump(mode="json", exclude={"operation_id", "pending", "answer"})
    if reply.pending is not None:
        observed["pending"] = {
            "server_label": reply.pending.server_label,
            "tool_name": reply.pending.tool_name,
            "fixed_query_matches": reply.pending.arguments == {"query": SEARCH_QUERY},
        }
    return observed


async def send(client: AsyncOpenAI, case: Case, command: Command) -> Reply:
    """Reuse the pinned session/conversation pattern already used by Story 1."""
    if not case.session_id or not case.conversation_id:
        raise ValueError("Session/conversation binding is incomplete; do not recreate or resubmit.")
    response = await client.responses.create(
        input=command.wire(),
        conversation=case.conversation_id,
        extra_body={"agent_session_id": case.session_id},
        stream=False,
    )
    if response.status != "completed" or response.error is not None:
        code = response.error.code if response.error else response.status
        message = response.error.message if response.error else "Incomplete response."
        raise ValueError(f"Coordinator request failed ({code}): {message}")
    reply = Reply.model_validate_json(response.output_text)
    if reply.operation_id != case.operation_id:
        raise ValueError("Coordinator replied with a mismatched operation reference.")
    return reply


async def execute(
    environment: str,
    action: Literal["start", "inspect", "approve", "reject"],
    state_file: Path,
    *,
    timeout: float,
    capture_history: bool = False,
    evidence: Callable[[str, dict[str, object]], None] | None = None,
) -> Reply:
    """Start once, inspect, or submit the operator's explicit bound decision."""
    if not state_file.resolve().is_relative_to((ROOT / "artifacts" / "story-6").resolve()):
        raise ValueError("Keep the private case locator under ignored artifacts/story-6.")
    context = deployment_context(ROOT / ".azure" / environment / ".env", environment)
    case = None if action == "start" else load(state_file, environment)
    async with (
        AzureCliCredential(process_timeout=60) as credential,
        AIProjectClient(
            endpoint=context["AZURE_AI_PROJECT_ENDPOINT"],
            credential=credential,
            allow_preview=True,
            retry_total=0,
            connection_timeout=15,
            read_timeout=timeout,
        ) as project,
        project.get_openai_client(agent_name=SERVICE, max_retries=0, timeout=timeout) as client,
    ):
        latest = (await project.agents.get(SERVICE)).as_dict()["versions"]["latest"]
        if latest.get("status") != "active":
            raise ValueError("Dedicated coordinator is not active.")
        version = str(latest["version"])
        if case is None:
            if state_file.exists():
                raise ValueError("Start requires a new case locator; never replace an existing operation.")
            case = Case(environment=environment, version=version, operation_id=uuid4(), phase="session_submitting")
            save(state_file, case)
            session = await project.agents.create_session(
                SERVICE, version_indicator=VersionRefIndicator(agent_version=version)
            )
            if not session.agent_session_id:
                raise ValueError("Platform did not return a session binding.")
            case.session_id = session.agent_session_id
            case.phase = "conversation_submitting"
            save(state_file, case)
            conversation = await client.conversations.create()
            case.conversation_id = conversation.id
            case.phase = "start_submitted"
            save(state_file, case)
            command = Command(operation="start", operation_id=case.operation_id)
        else:
            if case.version != version:
                raise ValueError("Coordinator version drifted; do not silently switch a pending operation.")
            if action == "inspect":
                command = Command(operation="inspect", operation_id=case.operation_id)
            else:
                if case.phase != "pending" or case.last_reply is None or case.last_reply.pending is None:
                    raise ValueError("Only a saved pending case can accept a decision; inspect instead of replaying.")
                if case.last_reply.pending.arguments != {"query": SEARCH_QUERY}:
                    raise ValueError("The saved approval differs from the fixed public Learn query.")
                command = Command(
                    operation="decide",
                    operation_id=case.operation_id,
                    approval_ref=case.last_reply.pending.reference,
                    approve=action == "approve",
                )
                case.phase = "decision_submitted"
                # Persist below only after the history/caller precondition succeeds.
        auditing = capture_history or case.audit_history
        if auditing:
            from scripts.run_scenario.story6_history import alias, snapshot, verified_operator

            if not case.conversation_id or not case.session_id:
                raise ValueError("Cannot audit without the actual session/conversation binding.")
            principal = await verified_operator(context["AZURE_TENANT_ID"])
            principal_hash = alias(str(case.operation_id), "caller", principal)
            if case.audit_principal_hash is not None and case.audit_principal_hash != principal_hash:
                raise ValueError("Audit caller differs from the original authenticated requester.")
            case.audit_principal_hash = principal_hash
            case.audit_history = True
            if evidence is not None:
                evidence(
                    "Authenticated caller correlation",
                    {
                        "caller_alias": principal_hash,
                        "conversation_alias": alias(str(case.operation_id), "conversation", case.conversation_id),
                        "session_alias": alias(str(case.operation_id), "session", case.session_id),
                        "operation_alias": alias(str(case.operation_id), "operation", str(case.operation_id)),
                        "same_authenticated_requester": True,
                        "identity_evidence": "Authenticated CLI account and Graph signed-in-user",
                        "correlation_is_authorization": False,
                    },
                )
                evidence(
                    "Conversation history before command",
                    dict(await snapshot(client, case.conversation_id, str(case.operation_id))),
                )
        save(state_file, case)
        reply = await send(client, case, command)
        if case.last_reply is not None and reply.file_sha256 != case.last_reply.file_sha256:
            raise ValueError("The original pause-file hash changed; do not continue the case.")
        case.phase = reply.phase
        case.last_reply = reply
        save(state_file, case)
        if auditing and evidence is not None:
            from scripts.run_scenario.story6_history import snapshot

            if case.conversation_id is None:
                raise ValueError("The audited conversation binding is missing.")
            evidence(
                "Conversation history after command",
                dict(await snapshot(client, case.conversation_id, str(case.operation_id))),
            )
            evidence(
                "Operation and reported invocation binding",
                {
                    "mcp_route": reply.mcp_route,
                    "phase": reply.phase,
                    "decision": reply.decision,
                    "provider_responses_observed": reply.provider_responses_observed,
                    "mcp_calls_reported": reply.mcp_calls_reported,
                    "file_retained": reply.file_retained,
                    "file_sha256": reply.file_sha256,
                    "research_user_context_present": reply.research_user_context_present,
                    "independent_tool_invocation_trace_verified": False,
                },
            )
        if action == "start" and reply.phase != "pending":
            raise ValueError("Start did not produce a pending approval; the original operation is retained.")
        if action in ("approve", "reject"):
            expected = "completed" if action == "approve" else "rejected"
            if reply.phase != expected:
                raise ValueError("Decision did not reach its expected terminal state; no replay is allowed.")
        return reply
