"""Story 4 stateless orchestration: Supervisor routing and delegation to specialist sub-agents.

``POC_DELEGATE`` (Supervisor only) routes one task to one specialist, either by an explicit target or by
the Supervisor model's tool call, then invokes that specialist's agent-bound Responses endpoint with a
selected identity mode and returns a structured report. ``POC_SUBTASK`` (specialists only) runs the task
and reports what the specialist observed about its caller. Neither command reads or writes the workflow
registry, so the strict ``POC_WORKFLOW`` caller guard is unchanged. Identities are never returned, only
salted fingerprints that a client holding the correlation ID can compare.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from hashlib import sha256
from time import perf_counter
from typing import Literal

from agent_framework import Agent, Message, tool
from agent_framework.foundry import FoundryChatOptions
from azure.ai.agentserver.core import AgentConfig, get_request_context
from azure.ai.agentserver.responses import ResponseContext
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError
from pydantic import UUID4, BaseModel, ConfigDict, Field, ValidationError

from app.core.redaction import sanitize_text
from app.core.settings import AgentName, Settings
from app.services.code_interpreter import container_ids
from app.services.workflows import WorkflowError

DELEGATE_PREFIX = "POC_DELEGATE "
SUBTASK_PREFIX = "POC_SUBTASK "
CORRELATION_HEADER = "x-client-correlation-id"
Specialist = Literal["research", "data_analysis"]
IdentityMode = Literal["agent", "call-context", "delegated-user"]
InvocationMode = Literal["sync", "background"]
# Application-owned allow-list: the Supervisor never invokes an agent that is not listed here.
ALLOWED_TARGETS: dict[Specialist, str] = {"research": "research-agent", "data_analysis": "data-analysis-agent"}
SIMULATED_FAILURE = "Simulated sub-agent failure requested by the Story 4 test"
TERMINAL = frozenset({"completed", "failed", "cancelled", "incomplete"})
ROUTER_PROMPT = (
    "Route the user task below to exactly one specialist by calling exactly one tool, once. "
    "Call delegate_to_research for explanations, background, definitions, or documentation research. "
    "Call delegate_to_data_analysis for calculations, statistics, code execution, or data/file analysis. "
    "Pass the user task verbatim as the task argument. After the tool returns, reply to the user with the "
    "specialist's answer and keep every exact token it contains. Never answer the task yourself.\n\nUser task:\n"
)


def fingerprint(value: str | None, salt: str) -> str | None:
    """A per-correlation salted digest: comparable within one run, never reversible to an identity."""
    return sha256(f"{salt}|{value}".encode()).hexdigest()[:16] if value else None


def delegated_label(user_id: str) -> str:
    """Stable application-user label derived from the verified platform user, never from the request body."""
    return "poc-" + sha256(f"story4|{user_id}".encode()).hexdigest()[:32]


class HopContext(BaseModel):
    """What one hop observed about its caller: presence flags and salted fingerprints only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent: AgentName
    hosted_runtime: bool
    call_id_present: bool
    user_id_present: bool
    session_id_present: bool
    user_fingerprint: str | None
    correlation_header_matches: bool

    @classmethod
    def observe(cls, agent: AgentName, correlation_id: str, headers: Mapping[str, str]) -> HopContext:
        context = get_request_context()
        user_id = context.user_id if context else None
        return cls(
            agent=agent,
            hosted_runtime=bool(AgentConfig.from_env().is_hosted),
            call_id_present=bool(context and context.call_id),
            user_id_present=bool(user_id),
            session_id_present=bool(context and context.session_id),
            user_fingerprint=fingerprint(user_id, correlation_id),
            correlation_header_matches=headers.get(CORRELATION_HEADER) == correlation_id,
        )


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    succeeded: bool | None


class SubTask(BaseModel):
    """The only fields a Supervisor sends a specialist; identity always comes from platform context."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    task: str = Field(min_length=1, max_length=4000)
    correlation_id: UUID4
    simulate_failure: bool = False

    def wire(self) -> str:
        return SUBTASK_PREFIX + self.model_dump_json()


class SubTaskReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["poc-subtask-v1"] = "poc-subtask-v1"
    agent: AgentName
    correlation_id: UUID4
    context: HopContext
    answer: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    containers: list[str] = Field(default_factory=list)


class DelegateCommand(BaseModel):
    """Client request to the Supervisor. ``target`` is checked against the allow-list, not the schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    task: str = Field(min_length=1, max_length=4000)
    correlation_id: UUID4
    target: str = Field(default="auto", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    identity: IdentityMode = "call-context"
    invocation: InvocationMode = "sync"
    simulate_failure: bool = False

    def wire(self) -> str:
        return DELEGATE_PREFIX + self.model_dump_json()


class Routing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    requested: str
    selected: Specialist | None
    decided_by: Literal["explicit-target", "supervisor-model-tool-call", "supervisor-allow-list"]
    controller: Literal["supervisor-application"] = "supervisor-application"
    model_tool_calls: int = 0


class Invocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mechanism: Literal["responses-api-agent-endpoint"] = "responses-api-agent-endpoint"
    target_agent: str
    mode: InvocationMode
    identity: IdentityMode
    headers_sent: list[str]
    response_id: str | None = None
    status: str | None = None
    statuses_seen: list[str] = Field(default_factory=list)
    polls: int = 0
    elapsed_ms: int = 0


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: Literal["request", "status", "report"]
    http_status: int | None = None
    code: str | None = None
    message: str
    handled_by: Literal["supervisor-application"] = "supervisor-application"
    retried: Literal[False] = False


class DelegationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["poc-delegation-v1"] = "poc-delegation-v1"
    correlation_id: UUID4
    status: Literal["completed", "sub_agent_failed", "denied", "no_delegation"]
    routing: Routing
    supervisor: HopContext
    delegated_label_fingerprint: str | None = None
    invocation: Invocation | None = None
    sub_agent: SubTaskReport | None = None
    failure: Failure | None = None
    final_answer: str = ""


def identity_headers(mode: IdentityMode, correlation_id: str) -> tuple[dict[str, str], str | None]:
    """Exactly one forwarding mechanism per mode, all derived from verified ambient platform context."""
    headers = {CORRELATION_HEADER: correlation_id}
    context = get_request_context()
    if mode == "call-context":
        if not context or not context.call_id:
            raise WorkflowError("call-context mode needs the verified platform call ID, which is absent.")
        headers["x-agent-foundry-call-id"] = context.call_id
        return headers, None
    if mode == "delegated-user":
        if not context or not context.user_id:
            raise WorkflowError("delegated-user mode needs the verified platform user ID, which is absent.")
        label = delegated_label(context.user_id)
        headers["x-ms-user-identity"] = label
        return headers, fingerprint(label, correlation_id)
    return headers, None


def tool_calls(messages: Iterable[Message]) -> list[ToolCall]:
    """Function/MCP tool calls the specialist actually made, from tool messages only."""
    names: dict[str, str] = {}
    outcomes: dict[str, bool] = {}
    order: list[str] = []
    for message in messages:
        for content in message.contents:
            if content.type in ("function_call", "mcp_server_tool_call") and content.call_id:
                names[content.call_id] = str(getattr(content, "name", None) or getattr(content, "tool_name", ""))
                order.append(content.call_id)
            elif content.type in ("function_result", "mcp_server_tool_result") and content.call_id:
                outcomes[content.call_id] = not getattr(content, "exception", None)
    return [ToolCall(name=names[call], succeeded=outcomes.get(call)) for call in dict.fromkeys(order)]


async def subtask_turn(
    name: AgentName, agent: Agent[FoundryChatOptions], subtask: SubTask, context: ResponseContext
) -> str:
    """Run one specialist sub-task; a requested failure fails before any model call."""
    if name not in ALLOWED_TARGETS:
        raise WorkflowError("Only Research and Data Analysis accept sub-tasks.")
    correlation = str(subtask.correlation_id)
    hop = HopContext.observe(name, correlation, context.client_headers)
    if subtask.simulate_failure:
        raise WorkflowError(f"{SIMULATED_FAILURE} (correlation {correlation}).")
    response = await agent.run(messages=[Message("user", [subtask.task])], stream=False)
    if not response.text.strip():
        raise WorkflowError("Specialist model returned no visible answer.")
    return SubTaskReport(
        agent=name,
        correlation_id=subtask.correlation_id,
        context=hop,
        answer=response.text,
        tool_calls=tool_calls(response.messages),
        containers=container_ids(response.messages),
    ).model_dump_json()


ClientFactory = Callable[[Specialist], AsyncOpenAI]


class Delegator:
    """Invoke one allow-listed specialist and turn every outcome into a report field, never a retry."""

    def __init__(self, command: DelegateCommand, client_for: ClientFactory, headers: list[str], timeout: float) -> None:
        self.command = command
        self.client_for = client_for
        self.headers = headers
        self.timeout = timeout
        self.invocation: Invocation | None = None
        self.sub_agent: SubTaskReport | None = None
        self.failure: Failure | None = None

    async def invoke(self, target: Specialist, task: str) -> None:
        subtask = SubTask(
            task=task, correlation_id=self.command.correlation_id, simulate_failure=self.command.simulate_failure
        )
        started = perf_counter()
        statuses: list[str] = []
        polls = 0
        response_id: str | None = None
        try:
            async with self.client_for(target) as client:
                if self.command.invocation == "sync":
                    response = await client.responses.create(input=subtask.wire(), stream=False)
                else:
                    response = await client.responses.create(
                        input=subtask.wire(), stream=False, background=True, store=True
                    )
                response_id = response.id
                statuses.append(str(response.status))
                while response.status not in TERMINAL:
                    if perf_counter() - started > self.timeout:
                        raise WorkflowError(f"Background sub-agent response did not finish in {self.timeout:.0f}s.")
                    await asyncio.sleep(1)
                    polls += 1
                    response = await client.responses.retrieve(response.id)
                    if not statuses or statuses[-1] != str(response.status):
                        statuses.append(str(response.status))
        except APIStatusError as error:
            body = error.body if isinstance(error.body, dict) else {}
            nested = body.get("error") if isinstance(body.get("error"), dict) else body
            self.failure = Failure(
                stage="request",
                http_status=error.status_code,
                code=str(nested.get("code")) if isinstance(nested, dict) and nested.get("code") else None,
                message=sanitize_text(str(error))[:500],
            )
        except (APITimeoutError, APIConnectionError, OpenAIError, WorkflowError) as error:
            self.failure = Failure(stage="request", message=sanitize_text(f"{type(error).__name__}: {error}")[:500])
        else:
            if response.status != "completed" or response.error:
                self.failure = Failure(
                    stage="status",
                    code=response.error.code if response.error else None,
                    message=sanitize_text(response.error.message if response.error else str(response.status))[:500],
                )
            else:
                try:
                    report = SubTaskReport.model_validate_json(response.output_text)
                    if report.agent != target or report.correlation_id != self.command.correlation_id:
                        raise WorkflowError("Sub-agent report does not match the invoked target or correlation.")
                    self.sub_agent = report
                except (ValidationError, WorkflowError) as error:
                    self.failure = Failure(stage="report", message=sanitize_text(str(error))[:500])
        self.invocation = Invocation(
            target_agent=ALLOWED_TARGETS[target],
            mode=self.command.invocation,
            identity=self.command.identity,
            headers_sent=self.headers,
            response_id=response_id,
            status=statuses[-1] if statuses else None,
            statuses_seen=statuses,
            polls=polls,
            elapsed_ms=round((perf_counter() - started) * 1000),
        )


async def route_with_model(
    agent: Agent[FoundryChatOptions],
    task: str,
    invoke: Callable[[Specialist, str], Awaitable[None]],
    delegator: Delegator,
) -> tuple[Specialist | None, int, str]:
    """Let the Supervisor model choose the specialist through a tool call; only the first call executes."""
    chosen: list[Specialist] = []
    calls = 0

    async def delegate(target: Specialist, specialist_task: str) -> str:
        nonlocal calls
        calls += 1
        if chosen:
            return "Delegation limit reached: exactly one specialist call is allowed per request."
        chosen.append(target)
        await invoke(target, specialist_task)
        if delegator.sub_agent is not None:
            return delegator.sub_agent.answer
        return f"The specialist failed: {delegator.failure.message if delegator.failure else 'unknown'}"

    @tool(name="delegate_to_research", description="Send the task to the Research specialist agent.")
    async def delegate_to_research(task: str) -> str:
        return await delegate("research", task)

    @tool(name="delegate_to_data_analysis", description="Send the task to the Data Analysis specialist agent.")
    async def delegate_to_data_analysis(task: str) -> str:
        return await delegate("data_analysis", task)

    response = await agent.run(
        messages=[Message("user", [ROUTER_PROMPT + task])],
        stream=False,
        tools=[delegate_to_research, delegate_to_data_analysis],
    )
    return (chosen[0] if chosen else None), calls, response.text


async def delegate_turn(
    name: AgentName,
    settings: Settings,
    agent: Agent[FoundryChatOptions],
    command: DelegateCommand,
    context: ResponseContext,
) -> str:
    """Supervisor-owned routing, invocation, failure handling, and result delivery for one task."""
    if name != "supervisor":
        raise WorkflowError("Only the Supervisor accepts delegation commands.")
    correlation = str(command.correlation_id)
    supervisor = HopContext.observe(name, correlation, context.client_headers)
    if command.target != "auto" and command.target not in ALLOWED_TARGETS:
        return DelegationReport(
            correlation_id=command.correlation_id,
            status="denied",
            routing=Routing(requested=command.target, selected=None, decided_by="supervisor-allow-list"),
            supervisor=supervisor,
            final_answer=f"Denied: '{command.target}' is not an allow-listed specialist; no request was sent.",
        ).model_dump_json()
    headers, label_fingerprint = identity_headers(command.identity, correlation)
    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(
            endpoint=str(settings.foundry_project_endpoint), credential=credential, allow_preview=True
        ) as project,
    ):

        def client_for(target: Specialist) -> AsyncOpenAI:
            return project.get_openai_client(
                agent_name=ALLOWED_TARGETS[target],
                timeout=max(settings.request_timeout_seconds, 240),
                max_retries=0,
                default_headers=headers,
            )

        delegator = Delegator(command, client_for, sorted(headers), timeout=max(settings.request_timeout_seconds, 240))
        if command.target == "auto":
            selected, calls, model_answer = await route_with_model(agent, command.task, delegator.invoke, delegator)
            routing = Routing(
                requested="auto", selected=selected, decided_by="supervisor-model-tool-call", model_tool_calls=calls
            )
        else:
            target: Specialist = "research" if command.target == "research" else "data_analysis"
            await delegator.invoke(target, command.task)
            routing = Routing(requested=command.target, selected=target, decided_by="explicit-target")
            model_answer = ""
    if routing.selected is None:
        status: Literal["completed", "sub_agent_failed", "no_delegation"] = "no_delegation"
        final = model_answer
    elif delegator.failure is not None:
        status = "sub_agent_failed"
        final = (
            f"The {routing.selected} specialist failed and the Supervisor did not retry: {delegator.failure.message}"
        )
    else:
        status = "completed"
        final = model_answer or (delegator.sub_agent.answer if delegator.sub_agent else "")
    return DelegationReport(
        correlation_id=command.correlation_id,
        status=status,
        routing=routing,
        supervisor=supervisor,
        delegated_label_fingerprint=label_fingerprint,
        invocation=delegator.invocation,
        sub_agent=delegator.sub_agent,
        failure=delegator.failure,
        final_answer=final,
    ).model_dump_json()
