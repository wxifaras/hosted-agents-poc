"""Story 3 multi-agent conversation management: Supervisor-owned specialist conversations and shared IDs.

``POC_CONVERSE`` (Supervisor only) always runs inside the client's Supervisor conversation.

- ``separate``: one platform conversation per specialist, created by the Supervisor on the specialist's
  endpoint and mapped to the Supervisor conversation in the Supervisor's user-isolated Foundry state store.
  Follow-ups reuse only that mapping; a client-supplied specialist conversation ID that does not match it is
  denied before any outbound call.
- ``shared``: the Supervisor passes its own conversation ID to the specialist, to observe whether the specialist
  can read and append that history.
- ``inspect`` makes no model call. It reports the mapping, what the Supervisor hydrates from its own conversation,
  and whether the selected identity can list the mapped specialist conversation.

Specialists receive plain text prefixed with ``HOP_TAG``, so their normal turn hydrates their own conversation
history. Identities are never returned; probe tokens are reported only as booleans and counts.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Any, Final, Literal

from azure.ai.agentserver.responses import ResponseContext
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential
from openai import APIStatusError, AsyncOpenAI, BadRequestError, OpenAIError
from pydantic import UUID4, BaseModel, ConfigDict, Field, StringConstraints

from app.core.redaction import sanitize_text
from app.core.settings import AgentName, Settings
from app.services.foundry_state import open_store
from app.services.orchestration import ALLOWED_TARGETS, Failure, HopContext, Specialist, identity_headers
from app.services.workflows import StateConflict, Store, WorkflowError

CONVERSE_PREFIX = "POC_CONVERSE "
PROTOCOL: Final = "poc-conversation-v1"
# Prefix on every message the Supervisor forwards; finding it in a conversation proves a specialist input landed there.
HOP_TAG = "[via supervisor-agent]"
MAP_PREFIX = "conversation-map/"
MAP_STORE = "hosted-agents-poc-supervisor-registry-v1"
MAX_LISTED_ITEMS = 1000
CONVERSATION_ID = r"^[A-Za-z0-9_-]{1,200}$"
Pattern = Literal["separate", "shared"]
ConverseIdentity = Literal["agent", "call-context"]
Delivery = Literal["result", "placeholder"]
Status = Literal["completed", "sub_agent_failed", "denied", "inspected"]
ProbeToken = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9-]{4,40}$")]


def conversation_digest(conversation_id: str) -> str:
    """Lookup key for a conversation; not a credential and never an authorization check."""
    return sha256(f"story3|{conversation_id}".encode()).hexdigest()[:32]


class ConverseCommand(BaseModel):
    """Client request to the Supervisor. Identity always comes from platform context, never from these fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["send", "inspect"] = "send"
    message: str = Field(default="", max_length=4000)
    correlation_id: UUID4
    target: Specialist = "research"
    pattern: Pattern = "separate"
    identity: ConverseIdentity = "agent"
    deliver: Delivery = "result"
    sub_conversation_id: str | None = Field(default=None, pattern=CONVERSATION_ID)
    probe_tokens: list[ProbeToken] = Field(default_factory=list, max_length=5)

    def wire(self) -> str:
        return CONVERSE_PREFIX + self.model_dump_json()


class ChildConversation(BaseModel):
    """One Supervisor-created specialist conversation, recorded before the specialist is invoked."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    agent: str
    conversation_id: str = Field(min_length=1)
    created_identity: ConverseIdentity
    created_correlation_id: UUID4
    metadata_linked: bool
    turns: int = Field(default=0, ge=0)


class ConversationMap(BaseModel):
    """Supervisor conversation -> specialist conversations; stored in the caller's isolated partition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    supervisor_conversation_digest: str
    children: dict[Specialist, ChildConversation] = Field(default_factory=dict)


class MappingInfo(BaseModel):
    """Where the mapping lives and who owns it; the record content stays in the store."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    owner: Literal["supervisor-application"] = "supervisor-application"
    storage: Literal["foundry-state-store"] = "foundry-state-store"
    store: str = MAP_STORE
    key: str
    user_isolated: bool = True
    found: bool
    created_now: bool = False


class HistoryStats(BaseModel):
    """Counts of conversation items by origin; text is never returned."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    hop_inputs: int = 0
    protocol_replies: int = 0
    other_assistant: int = 0
    # Items containing each probe token, in probe order; positional so these counts never echo the token text.
    token_hits: list[int] = Field(default_factory=list)
    # The same counts restricted to Supervisor poc-conversation-v1 replies: what the Supervisor itself persisted.
    reply_token_hits: list[int] = Field(default_factory=list)


class SubConversation(BaseModel):
    """The mapped specialist conversation as the Supervisor sees it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    conversation_id: str
    created_now: bool
    created_identity: ConverseIdentity
    created_correlation_id: UUID4
    metadata_linked: bool
    metadata: dict[str, str] | None = None
    turns: int = 0


class Listing(BaseModel):
    """Whether the Supervisor's selected identity could list a conversation, and how many items it held."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: int | None = None
    failure: Failure | None = None


class ConversationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["poc-conversation-v1"] = PROTOCOL
    correlation_id: UUID4
    operation: Literal["send", "inspect"]
    status: Status
    pattern: Pattern
    identity: ConverseIdentity
    deliver: Delivery
    target_agent: str
    supervisor: HopContext
    supervisor_conversation_id: str
    supervisor_history: HistoryStats
    mapping: MappingInfo | None = None
    sub_conversation: SubConversation | None = None
    conversation_used: str | None = None
    listing_before: Listing | None = None
    listing_after: Listing | None = None
    response_id: str | None = None
    failure: Failure | None = None
    sub_answer_chars: int = 0
    tokens_in_sub_answer: list[bool] = Field(default_factory=list)
    sub_answer: str | None = None
    denial: str | None = None
    final_answer: str = ""


def item_text(item: Mapping[str, object]) -> str:
    content = item.get("content")
    parts = content if isinstance(content, list) else []
    return "\n".join(
        str(part.get("text")) for part in parts if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    )


def history_stats(items: Sequence[object], tokens: Sequence[str] = ()) -> HistoryStats:
    """Classify conversation items: client input, forwarded specialist input, Supervisor reply, or other output."""
    items_seen = users = assistants = hops = replies = others = 0
    hits = [0] * len(tokens)
    reply_hits = [0] * len(tokens)
    for item in items:
        if not isinstance(item, Mapping):
            continue
        items_seen += 1
        text = item_text(item)
        reply = False
        if item.get("type") == "message" and item.get("role") == "user":
            users += 1
            hops += text.startswith(HOP_TAG)
        elif item.get("type") == "message" and item.get("role") == "assistant":
            assistants += 1
            reply = f'"{PROTOCOL}"' in text
            if reply:
                replies += 1
            else:
                others += 1
        for index, token in enumerate(tokens):
            hits[index] += token in text
            reply_hits[index] += reply and token in text
    return HistoryStats(
        items=items_seen,
        user_messages=users,
        assistant_messages=assistants,
        hop_inputs=hops,
        protocol_replies=replies,
        other_assistant=others,
        token_hits=hits,
        reply_token_hits=reply_hits,
    )


def api_failure(error: Exception) -> Failure:
    """HTTP status and service error code when available; sanitized message only."""
    if isinstance(error, APIStatusError):
        body = error.body if isinstance(error.body, dict) else {}
        nested = body.get("error") if isinstance(body.get("error"), dict) else body
        code = str(nested.get("code")) if isinstance(nested, dict) and nested.get("code") else None
        return Failure(
            stage="request", http_status=error.status_code, code=code, message=sanitize_text(str(error))[:500]
        )
    return Failure(stage="request", message=sanitize_text(f"{type(error).__name__}: {error}")[:500])


async def list_items(client: AsyncOpenAI, conversation_id: str) -> Listing:
    """List a conversation as the client's principal; a denial is an observation, not an exception."""
    count = 0
    try:
        async for _ in client.conversations.items.list(conversation_id, limit=100):
            count += 1
            if count >= MAX_LISTED_ITEMS:
                break
    except OpenAIError as error:
        return Listing(failure=api_failure(error))
    return Listing(items=count)


async def read_metadata(client: AsyncOpenAI, conversation_id: str) -> dict[str, str] | None:
    try:
        conversation = await client.conversations.retrieve(conversation_id)
    except OpenAIError as _:
        return None
    metadata: object = conversation.metadata
    return {str(k): str(v) for k, v in metadata.items()} if isinstance(metadata, Mapping) else None


class ConversationMaps:
    """Read and conditionally update one Supervisor conversation's mapping record."""

    def __init__(self, store: Store, supervisor_conversation_id: str) -> None:
        self.store = store
        self.digest = conversation_digest(supervisor_conversation_id)
        self.key = MAP_PREFIX + self.digest
        self.etag: str | None = None

    async def load(self) -> ConversationMap | None:
        item = await self.store.read(self.key)
        if item is None:
            self.etag = None
            return None
        value = ConversationMap.model_validate(item.value)
        if value.supervisor_conversation_digest != self.digest:
            raise WorkflowError("Persisted conversation mapping does not match this Supervisor conversation.")
        self.etag = item.etag
        return value

    async def save(self, current: ConversationMap | None, target: Specialist, child: ChildConversation) -> None:
        """Create-only for a new record and ETag-guarded otherwise; a mapped child is never swapped for another."""
        previous = current.children.get(target) if current else None
        if previous and previous.conversation_id != child.conversation_id:
            raise WorkflowError("A mapped specialist conversation cannot be replaced.")
        children = {**(current.children if current else {}), target: child}
        document = ConversationMap(supervisor_conversation_digest=self.digest, children=children).model_dump(
            mode="json"
        )
        if current is None:
            try:
                await self.store.create(self.key, document)
            except StateConflict as error:
                raise WorkflowError("A concurrent request created this conversation mapping.") from error
        else:
            if not self.etag:
                raise WorkflowError("A server-issued ETag is required to update the mapping.")
            await self.store.replace(self.key, document, self.etag)


async def create_child(client: AsyncOpenAI, digest: str, correlation_id: str, target_agent: str) -> tuple[str, bool]:
    """A real platform conversation on the specialist endpoint, linked to its parent through metadata if accepted."""
    metadata = {
        "poc_story": "3",
        "parent_conversation_digest": digest,
        "correlation_id": correlation_id,
        "created_by": "supervisor-agent",
        "target_agent": target_agent,
    }
    try:
        conversation = await client.conversations.create(metadata=metadata)
        linked = True
    except BadRequestError:
        conversation = await client.conversations.create()
        linked = False
    if not conversation.id:
        raise WorkflowError("Specialist platform did not return a conversation ID.")
    return conversation.id, linked


@dataclass(frozen=True)
class Outcome:
    """One specialist turn as seen by the Supervisor."""

    response_id: str | None = None
    answer: str = ""
    failure: Failure | None = None


async def invoke(client: AsyncOpenAI, conversation_id: str, message: str) -> Outcome:
    """One plain-text turn in the given conversation; the specialist hydrates that history itself. No retries."""
    try:
        response = await client.responses.create(
            input=f"{HOP_TAG} {message}", conversation=conversation_id, stream=False
        )
    except OpenAIError as error:
        return Outcome(failure=api_failure(error))
    if response.status != "completed" or response.error:
        return Outcome(
            response_id=response.id,
            failure=Failure(
                stage="status",
                code=response.error.code if response.error else None,
                message=sanitize_text(response.error.message if response.error else str(response.status))[:500],
            ),
        )
    if not response.output_text.strip():
        return Outcome(response_id=response.id, failure=Failure(stage="report", message="Empty specialist answer."))
    return Outcome(response_id=response.id, answer=response.output_text)


@dataclass(frozen=True)
class Turn:
    """Fields every report of this turn shares."""

    command: ConverseCommand
    supervisor: HopContext
    conversation_id: str
    history: HistoryStats

    @property
    def target_agent(self) -> str:
        return ALLOWED_TARGETS[self.command.target]

    def report(self, status: Status, **update: Any) -> str:
        base = ConversationReport(
            correlation_id=self.command.correlation_id,
            operation=self.command.operation,
            status=status,
            pattern=self.command.pattern,
            identity=self.command.identity,
            deliver=self.command.deliver,
            target_agent=self.target_agent,
            supervisor=self.supervisor,
            supervisor_conversation_id=self.conversation_id,
            supervisor_history=self.history,
        )
        return base.model_copy(update=update).model_dump_json()

    def delivered(
        self, outcome: Outcome, used: str | None, before: Listing | None, after: Listing | None, **extra: Any
    ) -> str:
        """Placeholder delivery keeps the specialist's text out of the Supervisor conversation; result copies it."""
        command = self.command
        if outcome.failure is not None:
            status: Status = "sub_agent_failed"
            final = f"The {self.target_agent} call failed and the Supervisor did not retry: {outcome.failure.message}"
        elif command.deliver == "result":
            status = "completed"
            final = outcome.answer
        else:
            status = "completed"
            final = (
                f"Delegated to {self.target_agent} (response {outcome.response_id}); its answer was not copied here."
            )
        return self.report(
            status,
            conversation_used=used,
            listing_before=before,
            listing_after=after,
            response_id=outcome.response_id,
            failure=outcome.failure,
            sub_answer_chars=len(outcome.answer),
            tokens_in_sub_answer=[token in outcome.answer for token in command.probe_tokens],
            sub_answer=outcome.answer if command.deliver == "result" and outcome.failure is None else None,
            final_answer=final,
            **extra,
        )


ClientFactory = Callable[[], AsyncOpenAI]


async def shared_turn(turn: Turn, client_for: ClientFactory) -> str:
    """Pass the Supervisor's own conversation ID to the specialist and record what the specialist could do with it."""
    if turn.command.operation == "inspect":
        return turn.report("inspected")
    async with client_for() as specialist:
        before = await list_items(specialist, turn.conversation_id)
        outcome = await invoke(specialist, turn.conversation_id, turn.command.message)
        after = await list_items(specialist, turn.conversation_id)
    return turn.delivered(outcome, turn.conversation_id, before, after)


async def separate_turn(turn: Turn, client_for: ClientFactory, store: Store) -> str:
    """Reuse or create the mapped specialist conversation; never adopt a conversation the mapping does not hold."""
    command = turn.command
    maps = ConversationMaps(store, turn.conversation_id)
    current = await maps.load()
    child = current.children.get(command.target) if current else None
    mapping = MappingInfo(key=maps.key, found=child is not None)
    claimed = command.sub_conversation_id
    if claimed is not None and (child is None or child.conversation_id != claimed):
        return turn.report(
            "denied",
            mapping=mapping,
            denial="sub_conversation_id is not the specialist conversation mapped to this Supervisor conversation; "
            "no request was sent.",
            final_answer="Denied: unmapped specialist conversation.",
        )
    async with client_for() as specialist:
        if command.operation == "inspect":
            if child is None:
                return turn.report("inspected", mapping=mapping)
            listing = await list_items(specialist, child.conversation_id)
            metadata = await read_metadata(specialist, child.conversation_id) if listing.failure is None else None
            sub = sub_view(child, created_now=False, metadata=metadata)
            return turn.report("inspected", mapping=mapping, sub_conversation=sub, listing_before=listing)
        created = child is None
        if child is None:
            try:
                child_id, linked = await create_child(
                    specialist, maps.digest, str(command.correlation_id), turn.target_agent
                )
            except OpenAIError as error:
                return turn.delivered(Outcome(failure=api_failure(error)), None, None, None, mapping=mapping)
            child = ChildConversation(
                agent=turn.target_agent,
                conversation_id=child_id,
                created_identity=command.identity,
                created_correlation_id=command.correlation_id,
                metadata_linked=linked,
            )
            await maps.save(current, command.target, child)
            current = await maps.load()
            mapping = mapping.model_copy(update={"created_now": True})
        before = await list_items(specialist, child.conversation_id)
        outcome = await invoke(specialist, child.conversation_id, command.message)
        after = await list_items(specialist, child.conversation_id)
        if outcome.failure is None:
            child = child.model_copy(update={"turns": child.turns + 1})
            await maps.save(current, command.target, child)
    sub = sub_view(child, created_now=created)
    return turn.delivered(outcome, child.conversation_id, before, after, mapping=mapping, sub_conversation=sub)


def sub_view(child: ChildConversation, *, created_now: bool, metadata: dict[str, str] | None = None) -> SubConversation:
    return SubConversation(
        conversation_id=child.conversation_id,
        created_now=created_now,
        created_identity=child.created_identity,
        created_correlation_id=child.created_correlation_id,
        metadata_linked=child.metadata_linked,
        metadata=metadata,
        turns=child.turns,
    )


async def converse_turn(name: AgentName, settings: Settings, command: ConverseCommand, context: ResponseContext) -> str:
    """Supervisor-owned conversation mapping, specialist invocation, and history observations for one turn."""
    if name != "supervisor":
        raise WorkflowError("Only the Supervisor accepts conversation commands.")
    if not context.conversation_id:
        raise WorkflowError("Create an explicit Supervisor conversation before POC_CONVERSE.")
    if command.operation == "send" and not command.message.strip():
        raise WorkflowError("A send operation requires a nonempty message.")
    if command.pattern == "shared" and command.sub_conversation_id:
        raise WorkflowError("The shared pattern uses the Supervisor conversation; omit sub_conversation_id.")
    correlation = str(command.correlation_id)
    turn = Turn(
        command=command,
        supervisor=HopContext.observe(name, correlation, context.client_headers),
        conversation_id=context.conversation_id,
        history=history_stats(await context.get_history(), command.probe_tokens),
    )
    headers, _ = identity_headers(command.identity, correlation)
    timeout = max(settings.request_timeout_seconds, 240)
    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(
            endpoint=str(settings.foundry_project_endpoint), credential=credential, allow_preview=True
        ) as project,
    ):

        def client_for() -> AsyncOpenAI:
            return project.get_openai_client(
                agent_name=turn.target_agent, timeout=timeout, max_retries=0, default_headers=headers
            )

        if command.pattern == "shared":
            return await shared_turn(turn, client_for)
        async with open_store("supervisor", credential, create=True) as store:
            return await separate_turn(turn, client_for, store)
