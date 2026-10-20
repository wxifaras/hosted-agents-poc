"""Fail-closed FoundryStateStore adapter with explicit original call context."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from azure.ai.agentserver.core import AgentConfig, get_request_context
from azure.ai.agentserver.core.storage import (
    FoundryStateStore,
    FoundryStorageConflictError,
    FoundryStorageNotFoundError,
    FoundryStoragePreconditionError,
    JSONValue,
)
from azure.core.credentials_async import AsyncTokenCredential
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from app.core.settings import AgentName
from app.services.workflows import Item, MissingMapping, StateConflict, WorkflowError

_DOCUMENT = TypeAdapter(dict[str, JsonValue])
MAX_DOCUMENT_BYTES = 200_000


class ContextPresence(BaseModel):
    """Presence-only diagnostics; never contains identities or authorization material."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    hosted_runtime: bool
    call_id_present: bool
    user_id_present: bool
    session_id_present: bool
    conversation_id_present: bool

    @classmethod
    def inspect(cls, conversation_id: str | None) -> ContextPresence:
        """Observe ambient SDK context without authorizing access or touching state."""
        context = get_request_context()
        return cls(
            hosted_runtime=bool(AgentConfig.from_env().is_hosted),
            call_id_present=bool(context and context.call_id),
            user_id_present=bool(context and context.user_id),
            session_id_present=bool(context and context.session_id),
            conversation_id_present=bool(conversation_id),
        )


@dataclass(frozen=True, repr=False)
class Caller:
    """Verified ambient context; never log or expose the opaque call ID."""

    call_id: str
    user_id: str
    platform_session_id: str

    @classmethod
    def current(cls) -> Caller:
        """Reject local SDK JSON fallback and incomplete hosted context."""
        context = get_request_context()
        if (
            not AgentConfig.from_env().is_hosted
            or not context
            or not context.call_id
            or not context.user_id
            or not context.session_id
        ):
            checks = ContextPresence.inspect(None).model_dump(exclude={"conversation_id_present"})
            missing = ", ".join(key for key, present in checks.items() if not present)
            raise WorkflowError(
                f"Stateful commands require verified Foundry hosted caller/session context. Missing checks: {missing}."
            )
        return cls(context.call_id, context.user_id, context.session_id)


class FoundryStore:
    """Bind every item operation to the original, verified platform call."""

    def __init__(self, store: FoundryStateStore, caller: Caller) -> None:
        self.store = store
        self.caller = caller

    async def read(self, key: str) -> Item | None:
        """Read within platform user isolation, never a user ID override."""
        item = await self.store.get_item(key, call_id=self.caller.call_id)
        return None if item is None else Item(item.value, item.etag)

    @staticmethod
    def _document(value: dict[str, object]) -> dict[str, JSONValue]:
        document = _DOCUMENT.validate_python(value)
        if len(json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) >= MAX_DOCUMENT_BYTES:
            raise WorkflowError("Registry document exceeds the application 200 KB guard.")
        return document

    async def create(self, key: str, value: dict[str, object]) -> None:
        """Use server-side create-only semantics, not a process-local lock."""
        try:
            await self.store.create_item(key, self._document(value), call_id=self.caller.call_id)
        except FoundryStorageConflictError as error:
            raise StateConflict("Create-only registry conflict.") from error

    async def replace(self, key: str, value: dict[str, object], etag: str) -> None:
        """Never resurrect a deleted document or overwrite a concurrent turn."""
        if not etag:
            raise WorkflowError("A server-issued ETag is required for registry mutation.")
        try:
            await self.store.set_item(key, self._document(value), if_match=etag, call_id=self.caller.call_id)
        except (FoundryStorageConflictError, FoundryStoragePreconditionError) as error:
            raise StateConflict("Registry changed concurrently; reopen before retrying.") from error


@asynccontextmanager
async def open_store(
    agent: AgentName, credential: AsyncTokenCredential, *, create: bool
) -> AsyncIterator[FoundryStore]:
    """Verify isolation and non-expiring TTL even when a named store exists."""
    caller = Caller.current()
    name = f"hosted-agents-poc-{agent}-registry-v1"
    if create:
        store = await FoundryStateStore.get_or_create(
            name, credential=credential, user_isolation=True, item_ttl_seconds=-1
        )
    else:
        store = FoundryStateStore(name, credential=credential)
    try:
        try:
            descriptor = await store.get()
        except FoundryStorageNotFoundError as error:
            raise MissingMapping("Registry store is missing; continuation cannot create it.") from error
        if descriptor.user_isolation is not True or descriptor.item_ttl_seconds != -1:
            raise WorkflowError("Registry must use user isolation and non-expiring items; refusing incompatible store.")
        yield FoundryStore(store, caller)
    finally:
        await store.aclose()
