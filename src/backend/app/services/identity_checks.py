"""Opt-in, synthetic hosted identity experiments; never normal workflow authorization."""

import json
from collections import Counter
from collections.abc import Mapping
from hashlib import sha256
from inspect import isawaitable
from typing import Literal
from uuid import uuid4

from azure.ai.agentserver.core import AgentConfig, get_request_context
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.agentserver.responses import ResponseContext
from azure.ai.projects.aio import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from openai import APIStatusError, AsyncOpenAI, OpenAIError, omit
from openai.types.responses import Response
from pydantic import UUID4, BaseModel, ConfigDict, Field, JsonValue

from app.core.settings import AgentName, Settings
from app.services.egress_audit import audit_egress
from app.services.foundry_state import Caller, ContextPresence
from app.services.workflows import WorkflowError

IDENTITY_PREFIX = "POC_IDENTITY "
Mode = Literal["call-context", "delegated-user"]


class IdentityCommand(BaseModel):
    """Allow only bounded operations in a dedicated diagnostic namespace."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal[
        "compare",
        "compare-chains",
        "compare-history-only",
        "presence",
        "state-create",
        "state-read",
        "history-seed",
        "history-read",
    ]
    run_id: UUID4

    def wire(self) -> str:
        """Encode without accepting a client-supplied acting user or store name."""
        return IDENTITY_PREFIX + self.model_dump_json()


class IdentityReply(BaseModel):
    """Safe findings, not an identity or ownership credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    presence: ContextPresence
    expected_header_present: bool
    user_matches_expected: bool | None
    marker_found: bool | None = None
    marker_fingerprint: str | None = None
    history_marker: str | None = None
    history_contains_marker: bool | None = None


class Check(BaseModel):
    """One observed assertion with explicit non-success states."""

    name: str
    status: Literal["PASS", "FAIL", "BLOCKED"]
    observed: dict[str, JsonValue] = Field(default_factory=dict)


class IdentityMatrix(BaseModel):
    """A bounded comparison; synthetic delegated users are not genuine Entra users."""

    protocol: Literal["poc-identity-matrix-v1"] = "poc-identity-matrix-v1"
    checks: list[Check] = Field(default_factory=list)
    genuine_two_entra_users_tested: Literal[False] = False

    def completeness_check(self, *, direct_control: bool, history_only: bool = False) -> Check:
        """Require each core assertion once without hiding collected failures."""
        modes = ("delegated-user",) if direct_control else ("call-context", "delegated-user")
        required = {
            f"{mode}: {suffix}"
            for mode in modes
            for suffix in ("context", "A state continuity", "A history continuity")
        }
        required.update(
            f"delegated-user: {suffix}"
            for suffix in ("B cannot read A state", "B cannot continue A history", "A continues after B attempt")
        )
        cross_user = "call-context: cross-user tests"
        if not direct_control:
            required.add(cross_user)
        counts = Counter(check.name for check in self.checks)
        missing = sorted(required - counts.keys())
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        invalid = sorted(
            {
                check.name
                for check in self.checks
                if (check.name == cross_user and check.status != "BLOCKED")
                or (
                    history_only
                    and check.name == "delegated-user: B cannot continue A history"
                    and check.observed.get("explicit_session_requested") is not False
                )
            }
        )
        return Check(
            name="Matrix completeness",
            status="FAIL" if missing or duplicates or invalid else "PASS",
            observed={
                "missing_checks": list[JsonValue](missing),
                "duplicate_checks": list[JsonValue](duplicates),
                "invalid_checks": list[JsonValue](invalid),
            },
        )


def require_diagnostic_context(settings: Settings) -> None:
    """Keep the diagnostic separate from the unchanged strict workflow caller guard."""
    if not settings.poc_enable_identity_tests:
        raise WorkflowError("Hosted identity experiments are disabled.")
    context = get_request_context()
    if not AgentConfig.from_env().is_hosted or not context.call_id or not context.session_id:
        raise WorkflowError("Identity experiments require hosted call and session context.")


async def research_check(settings: Settings, command: IdentityCommand, context: ResponseContext) -> IdentityReply:
    """Exercise platform-authorized state/history without an application user override."""
    require_diagnostic_context(settings)
    caller = get_request_context()
    expected = context.client_headers.get("x-client-expected-caller")
    report = IdentityReply(
        presence=ContextPresence.inspect(context.conversation_id),
        expected_header_present=bool(expected),
        user_matches_expected=None if not expected or not caller.user_id else caller.user_id == expected,
    )
    if command.operation == "presence":
        return report
    if command.operation in ("state-create", "state-read"):
        async with DefaultAzureCredential() as credential:
            store_name = "hosted-agents-poc-identity-experiments-v1"
            store = (
                await FoundryStateStore.get_or_create(
                    store_name, credential=credential, user_isolation=True, item_ttl_seconds=3600
                )
                if command.operation == "state-create"
                else FoundryStateStore(store_name, credential=credential)
            )
            try:
                descriptor = await store.get()
                if descriptor.user_isolation is not True or descriptor.item_ttl_seconds != 3600:
                    raise WorkflowError("Identity experiment store has incompatible isolation/retention.")
                key = f"probe/{command.run_id}"
                if command.operation == "state-create":
                    await store.create_item(key, {"marker": uuid4().hex}, call_id=caller.call_id)
                item = await store.get_item(key, call_id=caller.call_id)
                marker = None if item is None else item.value.get("marker")
                if item is not None and not isinstance(marker, str):
                    raise WorkflowError("Identity experiment marker has an invalid schema.")
                return report.model_copy(
                    update={
                        "marker_found": item is not None,
                        "marker_fingerprint": sha256(marker.encode()).hexdigest() if isinstance(marker, str) else None,
                    }
                )
            finally:
                await store.aclose()
    marker = f"POC-IDENTITY-HISTORY-{command.run_id}"
    if command.operation == "history-seed":
        return report.model_copy(update={"history_marker": marker})
    if command.operation != "history-read":
        raise WorkflowError("Research cannot coordinate another identity experiment.")
    found = False
    for history_item in await context.get_history():
        if (
            not isinstance(history_item, Mapping)
            or history_item.get("type") != "message"
            or history_item.get("role") != "assistant"
        ):
            continue
        content = history_item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and marker in text:
                    found = True
    return report.model_copy(update={"history_contains_marker": found})


class IdentityRunner:
    """Use real agent-bound clients with exactly one forwarding mechanism per variant."""

    def __init__(
        self,
        project: AIProjectClient,
        caller: Caller | None,
        run_id: UUID4,
        *,
        expected_endpoint: str | None = None,
        expected_principal: str | None = None,
        expected_client: str | None = None,
    ) -> None:
        self.project = project
        self.caller = caller
        self.run_id = run_id
        namespace = sha256(f"{caller.user_id if caller else 'operator-control'}:{run_id}".encode()).hexdigest()
        self.labels = {"a": f"poc-{namespace}-a", "b": f"poc-{namespace}-b"}
        self.result = IdentityMatrix()
        self.expected_endpoint = expected_endpoint
        self.expected_principal = expected_principal
        self.expected_client = expected_client
        self.egress: list[dict[str, JsonValue]] = []

    def client(self, mode: Mode, actor: Literal["a", "b"] = "a") -> AsyncOpenAI:
        """Use verified nested context or explicit local operator control, never fake call IDs."""
        headers: dict[str, str] = {}
        if mode == "call-context":
            if actor != "a" or self.caller is None:
                raise WorkflowError("A second genuine call-context principal is unavailable.")
            headers["x-agent-foundry-call-id"] = self.caller.call_id
            headers["x-client-expected-caller"] = self.caller.user_id
        else:
            headers["x-ms-user-identity"] = self.labels[actor]
            headers["x-client-expected-caller"] = self.labels[actor]
        return self.project.get_openai_client(
            agent_name="research-agent", timeout=180, max_retries=0, default_headers=headers
        )

    def record(self, name: str, passed: bool, **observed: JsonValue) -> None:
        """Keep assertions and observations separate."""
        self.result.checks.append(Check(name=name, status="PASS" if passed else "FAIL", observed=observed))

    def inspect_egress(
        self,
        client: AsyncOpenAI,
        url: str,
        headers: Mapping[str, str],
        *,
        content: bytes,
        previous: str | None,
        session: str | None,
        status_code: int | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Audit only when authoritative instance metadata was resolved."""
        if self.expected_endpoint and self.expected_principal and self.expected_client:
            try:
                payload: object = json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise WorkflowError("Cannot inspect serialized diagnostic request bindings.") from error
            if not isinstance(payload, dict):
                raise WorkflowError("Serialized diagnostic request must be a JSON object.")
            audit = audit_egress(
                url,
                headers,
                {key: value for key, value in client.default_headers.items() if isinstance(value, str)},
                expected_endpoint=self.expected_endpoint,
                expected_principal=self.expected_principal,
                expected_client=self.expected_client,
            )
            audit["previous_response_binding_matches_request"] = (
                payload.get("previous_response_id") == previous if previous else "previous_response_id" not in payload
            )
            audit["session_binding_matches_request"] = (
                payload.get("agent_session_id") == session if session else "agent_session_id" not in payload
            )
            self.egress.append(audit)

    async def send(
        self,
        client: AsyncOpenAI,
        operation: Literal["presence", "state-create", "state-read", "history-seed", "history-read"],
        *,
        conversation: str | None = None,
        previous: str | None = None,
        session: str | None = None,
    ) -> tuple[IdentityReply, str, str]:
        """Invoke once with explicit history/sandbox bindings and no model work."""
        command = IdentityCommand(operation=operation, run_id=self.run_id)
        try:
            raw = await client.responses.with_raw_response.create(
                input=command.wire(),
                conversation=conversation if conversation else omit,
                previous_response_id=previous if previous else omit,
                stream=False,
                store=True,
                extra_body={"agent_session_id": session} if session else {},
            )
        except APIStatusError as error:
            self.inspect_egress(
                client,
                str(error.request.url),
                error.request.headers,
                content=error.request.content,
                previous=previous,
                session=session,
                status_code=error.status_code,
                response_headers=error.response.headers,
            )
            raise
        self.inspect_egress(
            client,
            str(raw.http_request.url),
            raw.http_request.headers,
            content=raw.http_request.content,
            previous=previous,
            session=session,
            status_code=raw.status_code,
            response_headers=raw.headers,
        )
        parsed: object = raw.parse(to=Response)
        response = await parsed if isawaitable(parsed) else parsed
        if not isinstance(response, Response):
            raise WorkflowError("Expected a non-streaming Responses result.")
        if response.status != "completed" or response.error:
            raise WorkflowError(f"Research identity operation {operation} did not complete.")
        reply = IdentityReply.model_validate_json(response.output_text)
        resolved_session = raw.headers.get("x-agent-session-id")
        if not resolved_session:
            extra = response.model_extra or {}
            candidate = extra.get("agent_session_id")
            resolved_session = candidate if isinstance(candidate, str) else None
        if not resolved_session:
            raise WorkflowError("Research identity response did not expose its resolved session.")
        return reply, response.id, resolved_session

    async def variant(self, mode: Mode, *, use_conversation: bool = True, bind_b_history_session: bool = True) -> None:
        """Probe context, independent state calls, and A-A-B-A history behavior."""
        async with self.client(mode) as client:
            conversation_id = (await client.conversations.create()).id if use_conversation else None
            probe, _, session = await self.send(client, "presence", conversation=conversation_id)
            flags = probe.presence.model_dump()
            self.record(
                f"{mode}: context",
                all(value for key, value in flags.items() if use_conversation or key != "conversation_id_present"),
                **flags,
                expected_header_present=probe.expected_header_present,
                user_matches_expected=probe.user_matches_expected,
                expected_identity_source="original-caller" if mode == "call-context" else "delegated-label",
                explicit_conversation_requested=use_conversation,
            )
            try:
                created, _, _ = await self.send(client, "state-create", conversation=conversation_id, session=session)
                read, _, _ = await self.send(client, "state-read", conversation=conversation_id, session=session)
                self.record(
                    f"{mode}: A state continuity",
                    created.marker_found is True
                    and read.marker_found is True
                    and bool(created.marker_fingerprint)
                    and created.marker_fingerprint == read.marker_fingerprint,
                )
                if mode == "delegated-user":
                    async with self.client(mode, "b") as other:
                        other_conversation_id = (await other.conversations.create()).id if use_conversation else None
                        other_read, _, _ = await self.send(other, "state-read", conversation=other_conversation_id)
                    self.record(
                        f"{mode}: B cannot read A state",
                        other_read.marker_found is False,
                        marker_found=other_read.marker_found,
                    )
            except (AzureError, OpenAIError, WorkflowError) as error:
                self.failure(f"{mode}: state authorization", error)
            try:
                seed, first, history_session = await self.send(client, "history-seed")
                read_history, second, _ = await self.send(
                    client, "history-read", previous=first, session=history_session
                )
                self.record(
                    f"{mode}: A history continuity",
                    bool(seed.history_marker) and read_history.history_contains_marker is True,
                )
                if mode == "delegated-user":
                    try:
                        async with self.client(mode, "b") as other:
                            reply, _, _ = await self.send(
                                other,
                                "history-read",
                                previous=second,
                                session=history_session if bind_b_history_session else None,
                            )
                        self.record(
                            f"{mode}: B cannot continue A history",
                            False,
                            request_was_accepted=True,
                            private_marker_seen=reply.history_contains_marker,
                            explicit_session_requested=bind_b_history_session,
                        )
                    except APIStatusError as error:
                        owner_confirmed = False
                        if error.status_code == 404 and error_code(error) == "not_found":
                            owner_response = await client.responses.retrieve(second)
                            owner_confirmed = (
                                owner_response.id == second
                                and owner_response.status == "completed"
                                and not owner_response.error
                            )
                        rejected = is_visibility_denial(error, owner_resource_confirmed=owner_confirmed)
                        self.record(
                            f"{mode}: B cannot continue A history",
                            rejected,
                            http_status=error.status_code,
                            error_code=error_code(error),
                            owner_resource_confirmed=owner_confirmed,
                            explicit_session_requested=bind_b_history_session,
                        )
                    resumed, owner_followup, resumed_session = await self.send(
                        client, "history-read", previous=second, session=history_session
                    )
                    self.record(
                        f"{mode}: A continues after B attempt",
                        resumed.history_contains_marker is True
                        and resumed_session == history_session
                        and bool(owner_followup)
                        and owner_followup != second,
                        history_marker_seen=resumed.history_contains_marker,
                        original_session_reused=resumed_session == history_session,
                        new_response_created=bool(owner_followup) and owner_followup != second,
                    )
            except (AzureError, OpenAIError, WorkflowError) as error:
                self.failure(f"{mode}: history authorization", error)
        if mode == "call-context":
            self.result.checks.append(
                Check(
                    name="call-context: cross-user tests",
                    status="BLOCKED",
                    observed={
                        "reason": "Requires a second genuine authenticated caller; not simulated with a user string."
                    },
                )
            )

    def failure(self, name: str, error: AzureError | OpenAIError | WorkflowError) -> None:
        """Expose safe error classification without raw identity-bearing SDK payloads."""
        observed: dict[str, JsonValue] = {"error_type": type(error).__name__}
        if isinstance(error, APIStatusError):
            observed.update(http_status=error.status_code, error_code=error_code(error))
            observed["impersonation_action_mentioned"] = (
                "useridentityimpersonation" in json.dumps(error.body).casefold()
            )
            observed["request_path"] = error.request.url.path
        elif isinstance(error, WorkflowError):
            observed["reason"] = str(error)
        self.result.checks.append(Check(name=name, status="FAIL", observed=observed))


def error_code(error: APIStatusError) -> str | None:
    """Read only the service error code, not potentially sensitive response messages."""
    body: object = error.body
    if isinstance(body, dict):
        nested = body.get("error", body)
        if isinstance(nested, dict) and isinstance(nested.get("code"), str):
            return str(nested["code"])
    return None


def is_visibility_denial(error: APIStatusError, *, owner_resource_confirmed: bool = False) -> bool:
    """An unrelated 403/404/5xx or transport error is not an isolation pass."""
    code = error_code(error)
    if error.status_code == 404 and code == "not_found":
        return owner_resource_confirmed and error.request.url.path.endswith("/responses")
    return error.status_code in (400, 403, 404) and code in {
        "session_not_accessible",
        "response_not_found",
        "conversation_not_found",
        "previous_response_not_found",
    }


async def identity_experiment(
    name: AgentName, settings: Settings, command: IdentityCommand, context: ResponseContext
) -> str:
    """Dispatch bounded diagnostics without touching normal registry mappings."""
    require_diagnostic_context(settings)
    coordinating = command.operation in ("compare", "compare-chains", "compare-history-only")
    if name == "research" and not coordinating:
        return (await research_check(settings, command, context)).model_dump_json()
    if name != "supervisor" or not coordinating:
        raise WorkflowError("Identity experiments allow only Supervisor coordination and Research probes.")
    caller = Caller.current()
    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(
            endpoint=str(settings.foundry_project_endpoint), credential=credential, allow_preview=True
        ) as project,
    ):
        result = IdentityMatrix()
        record = (await project.agents.get("supervisor-agent")).as_dict()
        instance = record.get("instance_identity")
        if not isinstance(instance, dict):
            raise WorkflowError("Cannot verify Supervisor instance identity for egress diagnostics.")
        principal, client_id = instance.get("principal_id"), instance.get("client_id")
        if not isinstance(principal, str) or not principal or not isinstance(client_id, str) or not client_id:
            raise WorkflowError("Supervisor instance metadata lacks principal/client IDs.")
        for mode in ("call-context", "delegated-user"):
            runner = IdentityRunner(
                project,
                caller,
                uuid4(),
                expected_endpoint=str(settings.foundry_project_endpoint).rstrip("/")
                + "/agents/research-agent/endpoint/protocols/openai",
                expected_principal=principal,
                expected_client=client_id,
            )
            try:
                await runner.variant(
                    mode,
                    use_conversation=command.operation == "compare",
                    bind_b_history_session=command.operation != "compare-history-only",
                )
            except (AzureError, OpenAIError, WorkflowError) as error:
                runner.failure(f"{mode}: invocation", error)
            if runner.egress:
                for key in runner.egress[0]:
                    values = [observation[key] for observation in runner.egress]
                    if all(type(value) is bool for value in values):
                        observed: JsonValue = all(values)
                    else:
                        observed = None
                    result.checks.append(
                        Check(
                            name=f"{mode}: egress {key}",
                            status="PASS" if observed is True else "BLOCKED" if observed is None else "FAIL",
                            observed={"all_requests_match": observed, "requests_checked": len(values)},
                        )
                    )
            result.checks.extend(runner.result.checks)
        return result.model_dump_json()
