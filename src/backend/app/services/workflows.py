"""Opaque scope mappings and conversation bindings; IDs never grant access."""

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import UUID4, BaseModel, ConfigDict, Field

from app.core.settings import AgentName
from app.services.code_interpreter import ContainerId


class WorkflowError(ValueError):
    """An existing scope cannot be accessed safely."""


class MissingMapping(WorkflowError):
    """Required durable state is absent; it must not be regenerated."""


class StateConflict(WorkflowError):
    """A create-only or conditional write lost a server-side race."""


@dataclass(frozen=True)
class Item:
    """A JSON document and its server-issued concurrency token."""

    value: dict[str, object]
    etag: str


class Store(Protocol):
    """User-isolated JSON operations with server-side preconditions."""

    async def read(self, key: str) -> Item | None: ...

    async def create(self, key: str, value: dict[str, object]) -> None: ...

    async def replace(self, key: str, value: dict[str, object], etag: str) -> None: ...


class Record(BaseModel):
    """Reject incompatible persisted schemas rather than silently migrating."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1


class Owner(Record):
    """One random owner label in an already isolated platform-user partition."""

    owner_id: UUID4


class Session(Owner):
    """An independent random session label and verified-session lookup binding."""

    session_id: UUID4
    agent: AgentName
    platform_session_hash: str


class Child(Record):
    """Actual remote references, recorded before a specialist is invoked."""

    agent: Literal["research", "data_analysis"]
    workflow_id: UUID4
    conversation_id: str = Field(min_length=1)
    platform_session_id: str | None = None
    owner_id: UUID4 | None = None
    session_id: UUID4 | None = None
    response_id: str | None = None
    containers: list[ContainerId] = Field(default_factory=list)


class Workflow(Session):
    """A durable conversation binding and bounded specialist progress snapshot."""

    workflow_id: UUID4
    conversation_id: str = Field(min_length=1)
    children: dict[str, Child] = Field(default_factory=dict)
    progress: Literal["new", "running", "ready"] = "new"
    last_answer: str = ""
    last_containers: list[ContainerId] = Field(default_factory=list)


@dataclass(frozen=True)
class Binding:
    """A workflow snapshot used for conditional mutations."""

    workflow: Workflow
    etag: str


class Registry:
    """Operate only inside an already platform-authorized store partition."""

    def __init__(self, store: Store, agent: AgentName, platform_session_id: str, conversation_id: str) -> None:
        if not platform_session_id or not conversation_id:
            raise WorkflowError("Verified hosted session and conversation context are required.")
        self.store = store
        self.agent = agent
        self.session_hash = sha256(platform_session_id.encode("utf-8")).hexdigest()
        self.session_key = f"session/{agent}/{self.session_hash}"
        self.conversation_id = conversation_id

    async def _required(self, key: str) -> Item:
        item = await self.store.read(key)
        if item is None:
            raise MissingMapping(f"Required {key.split('/')[0]} mapping is missing; creation is not allowed here.")
        return item

    async def _create_once(self, key: str, value: Record) -> Item:
        try:
            await self.store.create(key, value.model_dump(mode="json"))
        except StateConflict:
            # Only a create-only conflict permits reading the committed winner.
            pass
        return await self._required(key)

    async def _scope(self, *, create: bool) -> Session:
        owner_item = await self.store.read("owner")
        if owner_item is None:
            if not create:
                raise MissingMapping("Owner mapping is missing; start a new workflow explicitly.")
            owner_item = await self._create_once("owner", Owner(owner_id=uuid4()))
        owner = Owner.model_validate(owner_item.value)
        session_item = await self.store.read(self.session_key)
        if session_item is None:
            if not create:
                raise MissingMapping("Session mapping is missing; continuation cannot create it.")
            session_item = await self._create_once(
                self.session_key,
                Session(
                    owner_id=owner.owner_id,
                    session_id=uuid4(),
                    agent=self.agent,
                    platform_session_hash=self.session_hash,
                ),
            )
        session = Session.model_validate(session_item.value)
        if (
            session.owner_id != owner.owner_id
            or session.agent != self.agent
            or session.platform_session_hash != self.session_hash
        ):
            raise WorkflowError("Persisted owner/session scope is inconsistent.")
        return session

    def _verify(self, item: Item, session: Session) -> Binding:
        workflow = Workflow.model_validate(item.value)
        if (
            workflow.owner_id != session.owner_id
            or workflow.session_id != session.session_id
            or workflow.agent != self.agent
            or workflow.platform_session_hash != self.session_hash
            or workflow.conversation_id != self.conversation_id
        ):
            raise WorkflowError("Workflow does not match the verified caller/session/conversation scope.")
        return Binding(workflow, item.etag)

    async def create(self, workflow_id: UUID4) -> Binding:
        """Only this operation may initialize absent owner/session mappings."""
        key = f"workflow/{workflow_id}"
        item = await self.store.read(key)
        session = await self._scope(create=item is None)
        if item is None:
            item = await self._create_once(
                key,
                Workflow(
                    **session.model_dump(),
                    workflow_id=workflow_id,
                    conversation_id=self.conversation_id,
                ),
            )
        binding = self._verify(item, session)
        if binding.workflow.workflow_id != workflow_id:
            raise WorkflowError("Persisted workflow key does not match its record.")
        return binding

    async def open(self, workflow_id: UUID4) -> Binding:
        """Lookup only, including after process restart or missing state."""
        session = await self._scope(create=False)
        item = await self._required(f"workflow/{workflow_id}")
        binding = self._verify(item, session)
        if binding.workflow.workflow_id != workflow_id:
            raise WorkflowError("Persisted workflow key does not match its record.")
        return binding

    async def save_child(self, binding: Binding, child: Child) -> Binding:
        """Commit a child reference only if the parent snapshot still exists."""
        if self.agent != "supervisor" or child.agent not in ("research", "data_analysis"):
            raise WorkflowError("Only the Supervisor may register specialist conversations.")
        children = {**binding.workflow.children, child.agent: child}
        previous = binding.workflow.children.get(child.agent)
        if previous and (
            previous.workflow_id != child.workflow_id or previous.conversation_id != child.conversation_id
        ):
            raise WorkflowError("A persisted child conversation cannot be replaced.")
        updated = binding.workflow.model_copy(update={"children": children})
        return await self._save(binding, updated)

    async def begin_turn(self, binding: Binding) -> Binding:
        """Claim a turn before outbound calls; ambiguous partial work fails closed."""
        if binding.workflow.progress == "running":
            raise WorkflowError("Workflow has an unfinished turn; automatic replay/recovery is not enabled.")
        return await self._save(binding, binding.workflow.model_copy(update={"progress": "running"}))

    async def finish_turn(self, binding: Binding, answer: str, containers: Sequence[str] = ()) -> Binding:
        """Publish completion only after required child references have committed."""
        return await self._save(
            binding,
            binding.workflow.model_copy(
                update={"progress": "ready", "last_answer": answer, "last_containers": list(containers)}
            ),
        )

    async def _save(self, binding: Binding, updated: Workflow) -> Binding:
        key = f"workflow/{updated.workflow_id}"
        await self.store.replace(key, updated.model_dump(mode="json"), binding.etag)
        return await self.open(updated.workflow_id)
