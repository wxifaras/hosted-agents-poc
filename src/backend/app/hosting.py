"""Public Responses handler: MAF text turns plus explicit hosted workflow commands."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from uuid import uuid4

from agent_framework import Agent, Message
from agent_framework.foundry import FoundryChatOptions
from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponseObject,
    ResponsesAgentServerHost,
    TextResponse,
)
from azure.ai.agentserver.responses.models import ResponseStreamEvent, ResponseUsage
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from pydantic import ValidationError

from app.core.logger import Logger
from app.core.settings import AgentName, Settings
from app.services.code_interpreter import container_ids, with_artifacts
from app.services.delegation import (
    COMMAND_PREFIX,
    CONTEXT_PREFIX,
    SPECIALISTS,
    Command,
    ContextCommand,
    ContextReport,
    Report,
    SpecialistClient,
)
from app.services.foundry_state import Caller, ContextPresence, open_store
from app.services.identity_checks import IDENTITY_PREFIX, IdentityCommand, identity_experiment
from app.services.orchestration import (
    DELEGATE_PREFIX,
    SUBTASK_PREFIX,
    DelegateCommand,
    SubTask,
    delegate_turn,
    subtask_turn,
)
from app.services.story1_native import NATIVE_PREFIX, NativeRequest, NativeWorkflowDenied, native_turn
from app.services.workflows import Child, Registry, WorkflowError


def _visible_text(text: str, role: str) -> str:
    if role == "user" and text.startswith(COMMAND_PREFIX):
        return Command.model_validate_json(text[len(COMMAND_PREFIX) :]).message
    if role == "assistant" and text.startswith("{") and '"poc-workflow-v1"' in text:
        value: object = json.loads(text)
        if isinstance(value, dict) and value.get("protocol") == "poc-workflow-v1":
            return Report.model_validate(value).answer
    return text


def _count(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkflowError("Model SDK returned an invalid usage count.")
    return value


async def failed_response(
    request: CreateResponse, context: ResponseContext, message: str, code: str = "invalid_prompt"
) -> AsyncIterator[ResponseStreamEvent]:
    """Expose an explicit protocol failure rather than a success-shaped error answer."""
    stream = ResponseEventStream(response_id=context.response_id, request=request)
    yield stream.emit_created()
    yield stream.emit_failed(code=code, message=message)


def history_messages(items: Sequence[object]) -> list[Message]:
    """Translate visible platform history only; never send registry/auth data to MAF."""
    messages: list[Message] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise WorkflowError("Unsupported platform history item shape.")
        if item.get("type") != "message":
            continue
        role = item.get("role")
        if role not in ("assistant", "user"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise WorkflowError("Unsupported platform message content.")
        for part in content:
            if isinstance(part, Mapping) and part.get("type") in ("input_text", "output_text"):
                text = part.get("text")
                if not isinstance(text, str):
                    raise WorkflowError("Platform text content is not a string.")
                visible = _visible_text(text, role)
                if visible:
                    messages.append(Message(role=role, contents=[visible]))
    return messages


async def workflow_turn(
    name: AgentName,
    settings: Settings,
    agent: Agent[FoundryChatOptions],
    command: Command,
    context: ResponseContext,
) -> str:
    """Authorize from platform context, then read/create mappings and run a bounded turn."""
    if context.is_recovery:
        raise WorkflowError("Automatic workflow recovery is not enabled; missing mappings must not be recreated.")
    caller = Caller.current()
    if not context.conversation_id:
        raise WorkflowError("Create an explicit platform conversation before a stateful request.")
    expected_caller = context.client_headers.get("x-client-expected-caller")
    caller_matches = None if expected_caller is None else expected_caller == caller.user_id
    if caller_matches is False:
        raise WorkflowError("Delegated platform user does not match the verified Supervisor caller.")
    if command.operation != "inspect" and not command.message.strip():
        raise WorkflowError("Create/continue requires a nonempty task message.")
    history = history_messages(await context.get_history())
    async with (
        DefaultAzureCredential() as credential,
        open_store(name, credential, create=command.operation == "create") as store,
    ):
        registry = Registry(store, name, caller.platform_session_id, context.conversation_id)
        binding = (
            await registry.create(command.workflow_id)
            if command.operation == "create"
            else await registry.open(command.workflow_id)
        )
        if command.operation == "inspect" or (command.operation == "create" and binding.workflow.progress == "ready"):
            return Report.from_workflow(binding.workflow, caller, caller_matches).model_dump_json()
        if command.operation == "continue" and binding.workflow.progress != "ready":
            raise WorkflowError("Only a completed workflow turn can be continued.")
        binding = await registry.begin_turn(binding)
        containers: list[str] = []
        if name == "supervisor":
            answers: list[str] = []
            async with AIProjectClient(
                endpoint=str(settings.foundry_project_endpoint), credential=credential, allow_preview=True
            ) as project:
                specialists = SpecialistClient(project, caller, timeout=max(settings.request_timeout_seconds, 180))
                for specialist in SPECIALISTS:
                    child = binding.workflow.children.get(specialist)
                    if child is None:
                        if command.operation != "create":
                            raise WorkflowError("Persisted child mapping is missing; continuation cannot create it.")
                        conversation_id = await specialists.create_conversation(specialist)
                        if conversation_id == context.conversation_id or any(
                            ref.conversation_id == conversation_id for ref in binding.workflow.children.values()
                        ):
                            raise WorkflowError("Root and specialist conversations must be distinct.")
                        child = Child(agent=specialist, workflow_id=uuid4(), conversation_id=conversation_id)
                        binding = await registry.save_child(binding, child)
                    updated, answer = await specialists.invoke(
                        child, command.message, create=command.operation == "create"
                    )
                    binding = await registry.save_child(binding, updated)
                    answers.append(f"{specialist}: {answer}")
                    containers.extend(item for item in updated.containers if item not in containers)
            answer = "\n\n".join(answers)
        else:
            response = await agent.run(messages=[*history, Message("user", [command.message])], stream=False)
            answer = response.text
            if not answer.strip():
                raise WorkflowError("Specialist model returned no visible answer.")
            containers = container_ids(response.messages)
        binding = await registry.finish_turn(binding, answer, containers)
        return Report.from_workflow(binding.workflow, caller, caller_matches).model_dump_json()


async def context_probe(
    name: AgentName, settings: Settings, command: ContextCommand, context: ResponseContext
) -> ContextReport:
    """Report booleans only; forwarding still requires a verified Supervisor caller."""
    ambient = get_request_context()
    expected = context.client_headers.get("x-client-expected-caller")
    matches = None if not expected or not ambient.user_id else expected == ambient.user_id
    report = ContextReport(
        agent=name,
        presence=ContextPresence.inspect(context.conversation_id),
        caller_matches_expected=matches,
    )
    if command.operation == "direct":
        return report
    if name != "supervisor":
        raise WorkflowError("Only Supervisor may forward a single Research context probe.")
    caller = Caller.current()
    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(
            endpoint=str(settings.foundry_project_endpoint), credential=credential, allow_preview=True
        ) as project,
    ):
        research = await SpecialistClient(project, caller, timeout=180).probe_research()
    return report.model_copy(update={"research": research})


def build_host(
    name: AgentName,
    settings: Settings,
    agent: Agent[FoundryChatOptions],
    configure_observability: Callable[..., None],
) -> ResponsesAgentServerHost:
    """Use the supported protocol handler seam instead of overriding SDK internals."""
    server = ResponsesAgentServerHost(configure_observability=configure_observability)

    @server.response_handler
    async def handle(
        request: CreateResponse, context: ResponseContext, cancellation: asyncio.Event
    ) -> TextResponse | AsyncIterator[ResponseStreamEvent]:
        if cancellation.is_set():
            raise asyncio.CancelledError
        text = await context.get_input_text() or ""
        if text.startswith(NATIVE_PREFIX):
            try:
                if request.get("background") or context.is_recovery:
                    raise WorkflowError("Native analysis does not support background or automatic recovery.")
                native_request = NativeRequest.model_validate_json(text[len(NATIVE_PREFIX) :])
                result = await native_turn(name, settings, native_request, context.conversation_id)
            except NativeWorkflowDenied as error:
                return failed_response(request, context, str(error), code="native_workflow_denied")
            except WorkflowError as error:
                return failed_response(request, context, str(error), code="server_error")
            except ValidationError:
                return failed_response(request, context, "Invalid native Story 1 command or output schema.")
            except TimeoutError:
                return failed_response(request, context, "Native analysis timed out; automatic replay is forbidden.")
            return TextResponse(context, request, text=result)
        if text.startswith(DELEGATE_PREFIX):
            try:
                if request.get("background") or context.is_recovery:
                    raise WorkflowError(
                        "Client-to-Supervisor delegation runs synchronously; background is not enabled."
                    )
                delegate = DelegateCommand.model_validate_json(text[len(DELEGATE_PREFIX) :])
                result = await delegate_turn(name, settings, agent, delegate, context)
            except WorkflowError as error:
                Logger(name).error("Delegation rejected", agent=name, reason=str(error))
                return failed_response(request, context, str(error))
            except ValidationError:
                Logger(name).error("Invalid delegation schema", agent=name)
                return failed_response(request, context, "Invalid POC delegation command.")
            return TextResponse(context, request, text=result)
        if text.startswith(SUBTASK_PREFIX):
            try:
                if context.is_recovery:
                    raise WorkflowError("Sub-task recovery is not enabled.")
                subtask = SubTask.model_validate_json(text[len(SUBTASK_PREFIX) :])
                result = await subtask_turn(name, agent, subtask, context)
            except WorkflowError as error:
                Logger(name).error("Sub-task failed", agent=name, reason=str(error))
                return failed_response(request, context, str(error), code="server_error")
            except ValidationError:
                Logger(name).error("Invalid sub-task schema", agent=name)
                return failed_response(request, context, "Invalid POC sub-task.")
            return TextResponse(context, request, text=result)
        if text.startswith(IDENTITY_PREFIX):
            try:
                if request.get("background") or context.is_recovery:
                    raise WorkflowError("Identity experiments do not support background/recovery.")
                experiment = IdentityCommand.model_validate_json(text[len(IDENTITY_PREFIX) :])
                result = await identity_experiment(name, settings, experiment, context)
            except WorkflowError as error:
                Logger(name).error("Identity experiment rejected", agent=name, reason=str(error))
                return failed_response(request, context, str(error))
            except ValidationError:
                Logger(name).error("Invalid identity experiment schema", agent=name)
                return failed_response(request, context, "Invalid identity experiment command/report.")
            return TextResponse(context, request, text=result)
        if text.startswith(CONTEXT_PREFIX):
            try:
                if request.get("background") or context.is_recovery:
                    raise WorkflowError("Background/recovery context probes are not supported.")
                probe_command = ContextCommand.model_validate_json(text[len(CONTEXT_PREFIX) :])
                report = await context_probe(name, settings, probe_command, context)
            except WorkflowError as error:
                Logger(name).error("Context probe rejected", agent=name, reason=str(error))
                return failed_response(request, context, str(error))
            except ValidationError:
                Logger(name).error("Invalid context probe schema", agent=name)
                return failed_response(request, context, "Invalid POC context probe schema.")
            return TextResponse(context, request, text=report.model_dump_json())
        if text.startswith(COMMAND_PREFIX):
            try:
                if request.get("background"):
                    raise WorkflowError("Stateful background/recovery execution is not enabled in this increment.")
                command = Command.model_validate_json(text[len(COMMAND_PREFIX) :])
                result = await workflow_turn(name, settings, agent, command, context)
            except WorkflowError as error:
                Logger(name).error("Stateful workflow request rejected", agent=name, reason=str(error))
                return failed_response(request, context, str(error))
            except ValidationError:
                Logger(name).error("Invalid stateful workflow schema", agent=name)
                return failed_response(
                    request, context, "Invalid POC workflow command or incompatible registry schema."
                )
            return TextResponse(context, request, text=result)
        messages = history_messages(await context.get_history())
        messages.append(Message("user", [text]))
        response = await agent.run(messages=messages, stream=False)

        def configure(result: ResponseObject) -> None:
            result["model"] = settings.azure_ai_model_deployment_name
            usage = response.usage_details
            if usage:
                result["usage"] = ResponseUsage(
                    input_tokens=_count(usage.get("input_token_count")),
                    output_tokens=_count(usage.get("output_token_count")),
                    total_tokens=_count(usage.get("total_token_count")),
                    input_tokens_details={
                        "cached_tokens": _count(usage.get("cached_input_token_count")),
                        "cache_write_tokens": _count(usage.get("cache_write_token_count")),
                    },
                    output_tokens_details={"reasoning_tokens": _count(usage.get("reasoning_token_count"))},
                )

        return TextResponse(
            context, request, text=with_artifacts(response.text, container_ids(response.messages)), configure=configure
        )

    return server
