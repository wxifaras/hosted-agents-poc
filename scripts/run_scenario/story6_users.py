"""Two real CLI-profile users against the existing hosted coordinator.

Platform session denial and custom approval ownership are separate observations.
No sign-in, role change, redeployment or owner decision is performed implicitly.
"""

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.services.story6_approval import SEARCH_QUERY, Command
from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import VersionRefIndicator
from azure.identity.aio import AzureCliCredential
from openai import APIStatusError, AsyncOpenAI
from pydantic import JsonValue

from scripts.common.auth_profiles import parse_users
from scripts.common.reporting import Status
from scripts.deploy.story6_package import ROOT, SERVICE, deployment_context
from scripts.run_scenario.native_user_isolation import NativeUser, ProfileCredential, prepare_user
from scripts.run_scenario.story6_hosted import Case, load, public_observation, save, send

Record = Callable[[str, Status, str, object], None]
MISSING_OPERATION = "Story6 operation is missing; continuation cannot create it."


@dataclass(frozen=True)
class Probe:
    """Only explicit service/application denials can support an access verdict."""

    outcome: str
    boundary: str
    code: str | None = None
    http_status: int | None = None

    @property
    def denied(self) -> bool:
        return self.outcome == "denied"

    def observed(self) -> dict[str, JsonValue]:
        return {
            "outcome": self.outcome,
            "boundary": self.boundary,
            "code": self.code,
            "http_status": self.http_status,
        }


async def probe(client: AsyncOpenAI, case: Case, command: Command) -> Probe:
    """Keep 401, transport failures and generic failed responses out of denial passes."""
    try:
        response = await client.responses.create(
            input=command.wire(),
            conversation=case.conversation_id,
            extra_body={"agent_session_id": case.session_id},
            stream=False,
        )
    except APIStatusError as error:
        body = error.body
        details = body.get("error", body) if isinstance(body, dict) else {}
        code = details.get("code") if isinstance(details, dict) else None
        safe_code = (
            code
            if isinstance(code, str) and code in {"session_not_accessible", "not_found", "Forbidden", "Unauthorized"}
            else None
        )
        denied = error.status_code == 403 and safe_code == "session_not_accessible"
        return Probe("denied" if denied else "inconclusive", "Foundry session", safe_code, error.status_code)
    if response.status == "failed" and response.error is not None:
        response_code = str(response.error.code)
        if response_code == "story6_request_failed" and MISSING_OPERATION in response.error.message:
            return Probe("denied", "coordinator user-isolated operation", "operation_not_visible")
        return Probe("inconclusive", "coordinator response", response_code)
    return Probe("unexpected_acceptance", "coordinator response")


async def new_case(project: AIProjectClient, client: AsyncOpenAI, environment: str, version: str, path: Path) -> Case:
    """Write intent before each create; never replace a missing/ambiguous binding."""
    if path.exists():
        raise ValueError("A two-user case already exists; inspect it rather than replacing it.")
    case = Case(environment=environment, version=version, operation_id=uuid4(), phase="session_submitting")
    save(path, case)
    session = await project.agents.create_session(SERVICE, version_indicator=VersionRefIndicator(agent_version=version))
    if not session.agent_session_id:
        raise ValueError("The user did not receive an explicit Hosted session.")
    case.session_id = session.agent_session_id
    case.phase = "conversation_submitting"
    save(path, case)
    conversation = await client.conversations.create()
    case.conversation_id = conversation.id
    case.phase = "ready"
    save(path, case)
    return case


async def users_and_version(environment: str, users: str) -> tuple[dict[str, str], list[NativeUser], str]:
    """Reuse cached profile verification; operator metadata is not the test identity."""
    context = deployment_context(ROOT / ".azure" / environment / ".env", environment)
    names = parse_users(users)
    if len(names) != 2:
        raise ValueError("Story6 Step6 requires exactly two existing isolated CLI users.")
    profiles = [await asyncio.to_thread(prepare_user, name, context["AZURE_TENANT_ID"]) for name in names]
    if profiles[0].principal == profiles[1].principal or profiles[0].path == profiles[1].path:
        raise ValueError("The test requires two distinct authenticated principals and CLI caches.")
    async with (
        AzureCliCredential(process_timeout=60) as credential,
        AIProjectClient(
            endpoint=context["AZURE_AI_PROJECT_ENDPOINT"],
            credential=credential,
            allow_preview=True,
            retry_total=0,
            connection_timeout=15,
            read_timeout=25,
        ) as project,
    ):
        latest = (await project.agents.get(SERVICE)).as_dict()["versions"]["latest"]
    if latest.get("status") != "active":
        raise ValueError("The dedicated coordinator is not active.")
    return context, profiles, str(latest["version"])


async def execute(
    environment: str, users: str, action: str, state_file: Path, record: Record, *, timeout: float
) -> str:
    """Test foreign access once, or submit the owner's separately chosen terminal decision."""
    context, profiles, version = await users_and_version(environment, users)
    record(
        "Two authenticated isolated profiles",
        "PASS",
        "Distinct cached Entra users without login/fallback",
        {"distinct_principals": True, "distinct_profile_paths": True, "actors": ["User A", "User B"]},
    )
    async with AsyncExitStack() as stack:
        clients: list[AsyncOpenAI] = []
        projects: list[AIProjectClient] = []
        for user in profiles:
            credential = await stack.enter_async_context(ProfileCredential(user))
            project = await stack.enter_async_context(
                AIProjectClient(
                    endpoint=context["AZURE_AI_PROJECT_ENDPOINT"],
                    credential=credential,
                    allow_preview=True,
                    retry_total=0,
                    connection_timeout=15,
                    read_timeout=timeout,
                )
            )
            projects.append(project)
            clients.append(
                await stack.enter_async_context(
                    project.get_openai_client(
                        agent_name=SERVICE,
                        max_retries=0,
                        timeout=timeout,
                        default_headers={},
                    )
                )
            )
        if action != "start":
            case = load(state_file, environment)
            if case.version != version or case.last_reply is None or case.last_reply.pending is None:
                raise ValueError("The original owner case must still have its version-bound pending approval.")
            if case.last_reply.pending.arguments != {"query": SEARCH_QUERY}:
                raise ValueError("The saved pending query differs from the fixed public Learn query.")
            if action == "inspect":
                command = Command(operation="inspect", operation_id=case.operation_id)
            elif action in ("approve", "reject") and case.phase == "pending":
                command = Command(
                    operation="decide",
                    operation_id=case.operation_id,
                    approval_ref=case.last_reply.pending.reference,
                    approve=action == "approve",
                )
                case.phase = "decision_submitted"
                save(state_file, case)
            else:
                raise ValueError("Only a pending owner case accepts a decision; do not replay.")
            response = await send(clients[0], case, command)
            if response.file_sha256 != case.last_reply.file_sha256 or not response.file_retained:
                raise ValueError("Original owner file changed or is missing; inspect without replay.")
            case.phase, case.last_reply = response.phase, response
            save(state_file, case)
            expected = "pending" if action == "inspect" else "completed" if action == "approve" else "rejected"
            record(
                "User A owner continuation",
                "PASS" if response.phase == expected else "FAIL",
                f"Original owner's operation is {expected}",
                public_observation(response),
            )
            return version

        owner = await new_case(projects[0], clients[0], environment, version, state_file)
        owner.phase = "start_submitted"
        save(state_file, owner)
        pending = await send(clients[0], owner, Command(operation="start", operation_id=owner.operation_id))
        owner.phase, owner.last_reply = pending.phase, pending
        save(state_file, owner)
        if pending.phase != "pending" or pending.pending is None or pending.mcp_calls_reported != 0:
            raise ValueError("User A did not obtain a zero-reported-call pending operation; stop.")
        record(
            "User A pending positive control",
            "PASS",
            "Real pending request, zero reported MCP calls",
            public_observation(pending),
        )

        other = await new_case(projects[1], clients[1], environment, version, state_file.with_name("user-b-state.json"))
        control = await probe(clients[1], other, Command(operation="inspect", operation_id=other.operation_id))
        reached = control.denied and control.boundary == "coordinator user-isolated operation"
        record(
            "User B reaches coordinator ownership boundary",
            "PASS" if reached else "BLOCKED",
            "Authenticated B reaches app handling in B's own session; missing B operation is explicit",
            control.observed(),
        )
        if not reached:
            raise ValueError("User B's positive route control did not reach coordinator ownership; no denial pass.")

        inspect = Command(operation="inspect", operation_id=owner.operation_id)
        decision = Command(
            operation="decide",
            operation_id=owner.operation_id,
            approval_ref=pending.pending.reference,
            approve=True,
        )
        for name, binding, command in (
            ("User B inspects A's session", owner, inspect),
            ("User B attempts decision in A's session", owner, decision),
            ("User B inspects A's operation in B's session", other, inspect),
            ("User B attempts A's decision in B's session", other, decision),
        ):
            result = await probe(clients[1], binding, command)
            record(
                name,
                "PASS" if result.denied else "FAIL",
                "Explicit session/operation visibility denial, not authentication/transport failure",
                result.observed(),
            )
            if not result.denied:
                raise ValueError(
                    "Foreign access was accepted or inconclusive; stop and inspect the original owner case."
                )
        after = await send(clients[0], owner, inspect)
        unchanged = (
            after.phase == "pending"
            and after.pending == pending.pending
            and after.mcp_calls_reported == 0
            and after.decision is None
            and after.file_sha256 == pending.file_sha256
            and after.file_retained
        )
        owner.phase, owner.last_reply = after.phase, after
        save(state_file, owner)
        record(
            "User A pending unchanged after foreign attempts",
            "PASS" if unchanged else "FAIL",
            "Same pending binding/file, no decision, zero reported MCP calls",
            public_observation(after),
        )
        if not unchanged:
            raise ValueError("Owner operation changed after foreign access attempts.")
    return version
