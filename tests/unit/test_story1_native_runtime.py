"""Offline native SDK/authorization tests; these do not claim live service isolation."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from app import hosting, runtime
from app.core.settings import Settings
from app.services import story1_native as ci
from app.services.foundry_state import Caller
from app.services.workflows import Item, StateConflict, WorkflowError
from openai import AsyncOpenAI
from openai.types.responses import Response
from pydantic import ValidationError

from scripts.common.synthetic_data import COLUMNS, build_dataset

PNG = b"\x89PNG\r\n\x1a\noriginal"


class MemoryStore:
    """Compare-and-swap storage double, not a platform isolation implementation."""

    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.revision = 0

    async def read(self, key: str) -> Item | None:
        return self.items.get(key)

    async def create(self, key: str, value: dict[str, object]) -> None:
        if key in self.items:
            raise StateConflict("exists")
        self.revision += 1
        self.items[key] = Item(value, str(self.revision))

    async def replace(self, key: str, value: dict[str, object], etag: str) -> None:
        if key not in self.items or self.items[key].etag != etag:
            raise StateConflict("changed")
        self.revision += 1
        self.items[key] = Item(value, str(self.revision))


def workflow(home: Path, store: MemoryStore | None = None, **overrides: str) -> ci.NativeWorkflow:
    return ci.NativeWorkflow(
        store or MemoryStore(),
        Caller("trusted-call", overrides.get("owner", "owner"), overrides.get("session", "session")),
        overrides.get("conversation", "conversation"),
        home,
        Settings(),
    )


def response(**overrides: object) -> Response:
    value = {
        "id": "resp_owned",
        "object": "response",
        "created_at": 1,
        "model": "unit",
        "status": "completed",
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "output": [
            {
                "type": "code_interpreter_call",
                "id": "ci_owned",
                "status": "completed",
                "container_id": "cntr_owned",
                "code": "# model-written code is never executed in this test",
                "outputs": [],
            },
            {
                "type": "message",
                "id": "msg_owned",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "original outputs; container is cntr_model_text_only",
                        "annotations": [
                            {
                                "type": "container_file_citation",
                                "container_id": "cntr_owned",
                                "file_id": f"file-{name}",
                                "filename": f"/mnt/data/{name}",
                                "start_index": 0,
                                "end_index": 1,
                            }
                            for name in ci.OUTPUTS
                        ],
                    }
                ],
            },
        ],
    }
    return Response.model_validate({**value, **overrides})


def output_bytes(input_hash: str) -> dict[str, bytes]:
    # Deliberately not a golden fixture answer: the runtime checks lineage/shape, the runner is the oracle.
    result = {
        "input_sha256": input_hash,
        "filtered_rows": 1,
        "by_category": {"Hardware": {"rows": 1, "units": 2, "revenue_cents": 10}},
        "total": {"rows": 1, "units": 2, "revenue_cents": 10},
        "telemetry": {
            "peak_rss_bytes": 100,
            "memory_limit_bytes": None,
            "cgroup_memory_peak_bytes": None,
            "unavailable": {"memory_limit_bytes": "not exposed", "cgroup_memory_peak_bytes": "not exposed"},
        },
    }
    return {
        "filtered.csv": (",".join(COLUMNS) + "\n").encode(),
        "by_category.csv": b"category,rows,units,revenue_cents\nHardware,1,2,10\n",
        "revenue_by_category.png": PNG,
        "result.json": json.dumps(result, indent=2).encode(),
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"owner": "foreign"},
        {"file_ids": ["foreign"]},
        {"container_id": "foreign"},
        {"prompt": "print(1)"},
        {"code": "print(1)"},
        {"rows": True},
        {"rows": 999},
        {"rows": 250_000},
        {"profile": "pressure", "rows": 1000},
    ],
)
def test_no_caller_selected_code_identity_native_ids_or_unbounded_fixture(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ci.NativeRequest.model_validate_json(json.dumps({"operation": "prepare", **extra}))


@pytest.mark.parametrize("operation", ["analyze", "inspect", "observe"])
def test_only_prepare_accepts_fixture_fields(operation: str) -> None:
    with pytest.raises(ValidationError):
        ci.NativeRequest.model_validate_json(
            json.dumps({"operation": operation, "workflow_id": str(uuid4()), "rows": 1000})
        )
    with pytest.raises(ValidationError):
        ci.NativeRequest.model_validate_json(json.dumps({"operation": operation}))


@pytest.mark.parametrize("field", ["owner", "session", "conversation"])
async def test_foreign_binding_fails_before_privileged_access(tmp_path: Path, field: str) -> None:
    store = MemoryStore()
    owner = workflow(tmp_path, store)
    prepared = await owner.prepare(ci.NativeRequest(operation="prepare"))
    other = workflow(tmp_path, store, **{field: "foreign"})
    client = SimpleNamespace(files=SimpleNamespace(create=AsyncMock()))
    request = ci.NativeRequest(operation="analyze", workflow_id=UUID(prepared.workflow_id))
    with pytest.raises(ci.NativeWorkflowDenied, match="not accessible"):
        await other.analyze(request, client, "unit")
    with pytest.raises(ci.NativeWorkflowDenied, match="not accessible"):
        await other.inspect(request)
    client.files.create.assert_not_called()
    assert await owner.inspect(request) == prepared
    assert store.revision == 1


@pytest.mark.parametrize("path", ["../x", "/absolute", "a\\x", "a/./b", "a/../b", "a:b", "a//b"])
def test_paths_cannot_escape_session_home(tmp_path: Path, path: str) -> None:
    with pytest.raises(WorkflowError):
        ci.home_path(tmp_path, path)


async def test_prepare_generates_original_seeded_input_and_pressure_is_backend_opt_in(tmp_path: Path) -> None:
    item = workflow(tmp_path)
    prepared = await item.prepare(ci.NativeRequest(operation="prepare"))
    assert prepared.input.sha256 == build_dataset(1000).sha256
    assert ci.home_path(tmp_path, prepared.input.path).read_bytes() == build_dataset(1000).content
    assert (
        await item.inspect(ci.NativeRequest(operation="inspect", workflow_id=UUID(prepared.workflow_id)))
    ) == prepared
    with pytest.raises(WorkflowError, match="not enabled"):
        await item.prepare(ci.NativeRequest(operation="prepare", rows=250_000, profile="pressure"))
    assert ci.PRESSURE_ROWS == (250_000, 500_000, 1_000_000)
    with pytest.raises(ValidationError):
        Settings(story1_native_max_output_bytes=50 * 1024 * 1024 + 1)


async def test_idle_observation_keeps_home_marker_but_reports_current_process_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    item = workflow(tmp_path, store)
    prepared = await item.prepare(ci.NativeRequest(operation="prepare"))
    request = ci.NativeRequest(operation="observe", workflow_id=UUID(prepared.workflow_id))
    before = await item.observe(request)
    assert before.observation == prepared.observation
    monkeypatch.setattr(ci, "PROCESS_BOOT_ID", str(uuid4()))
    after = await workflow(tmp_path, store).observe(request)
    assert after.observation.home_sentinel == before.observation.home_sentinel
    assert after.observation.process_boot_id != before.observation.process_boot_id
    with pytest.raises(ci.NativeWorkflowDenied):
        await workflow(tmp_path, store, session="other").observe(request)
    ci.home_path(tmp_path, f".story1-native/{prepared.workflow_id}/idle-sentinel.txt").unlink()
    with pytest.raises(WorkflowError, match="missing"):
        await item.observe(request)
    assert not ci.home_path(tmp_path, f".story1-native/{prepared.workflow_id}/idle-sentinel.txt").exists()


async def test_real_sdk_wire_upload_download_and_original_byte_receipts(tmp_path: Path) -> None:
    store = MemoryStore()
    item = workflow(tmp_path, store)
    prepared = await item.prepare(ci.NativeRequest(operation="prepare"))
    assert prepared.container_fingerprint is None
    originals = output_bytes(prepared.input.sha256)
    calls: list[tuple[str, str]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["x-agent-foundry-call-id"] == "trusted-local"
        if request.url.path == "/files":
            assert prepared.input.filename.encode() in request.content
            assert build_dataset(1000).content in request.content
            assert b"assistants" in request.content
            return httpx.Response(
                200,
                json={
                    "id": "file-input",
                    "object": "file",
                    "bytes": prepared.input.size_bytes,
                    "created_at": 1,
                    "filename": prepared.input.filename,
                    "purpose": "assistants",
                },
            )
        if request.url.path == "/responses":
            body = json.loads(request.content)
            assert body["tools"] == [
                {"type": "code_interpreter", "container": {"type": "auto", "file_ids": ["file-input"]}}
            ]
            assert body["store"] is False and body["stream"] is False
            assert body["max_tool_calls"] == 1
            assert body["input"] == ci.ANALYSIS_PROMPT
            assert prepared.input.sha256 not in body["input"]
            return httpx.Response(200, json=response().model_dump(mode="json"))
        for name, content in originals.items():
            if request.url.path == f"/containers/cntr_owned/files/file-{name}/content":
                return httpx.Response(200, content=content)
        raise AssertionError(f"Unexpected SDK operation: {request.method} {request.url.path}")

    async with AsyncOpenAI(
        api_key="synthetic-unit-key",
        base_url="https://unit.invalid",
        default_headers={"x-agent-foundry-call-id": "trusted-local"},
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    ) as client:
        request = ci.NativeRequest(operation="analyze", workflow_id=UUID(prepared.workflow_id))
        completed = await item.analyze(request, client, "unit")
        assert completed.status == "completed" and completed.result.input_sha256 == prepared.input.sha256
        assert completed.container_fingerprint == ci.digest("cntr_owned")
        assert completed.container_fingerprint != ci.digest("cntr_model_text_only")
        assert len(completed.artifacts) == 4
        for receipt in completed.artifacts:
            content = ci.home_path(tmp_path, receipt.path).read_bytes()
            assert content == originals[receipt.filename]
            assert receipt.sha256 == ci.digest(content) and receipt.size_bytes == len(content)
        public = completed.model_dump_json()
        assert all(secret not in public for secret in ("file-input", "cntr_owned", "resp_owned", "trusted-local"))
        assert (await item.inspect(request)) == completed
        reopened = await workflow(tmp_path, store).inspect(request)
        assert reopened.container_fingerprint == completed.container_fingerprint
        before = list(calls)
        with pytest.raises(WorkflowError, match="already submitted"):
            await item.analyze(request, client, "unit")
        assert calls == before
        ci.home_path(tmp_path, completed.artifacts[0].path).write_bytes(b"tampered")
        with pytest.raises(WorkflowError, match="original receipt"):
            await item.inspect(request)
    assert calls[:2] == [("POST", "/files"), ("POST", "/responses")]
    assert len(calls) == 6


@pytest.mark.parametrize("problem", ["foreign", "traversal", "missing", "conflict", "multiple", "incomplete"])
async def test_bad_native_citations_fail_before_download(problem: str) -> None:
    native = response()
    annotations = native.output[1].content[0].annotations
    if problem == "foreign":
        annotations[0].container_id = "cntr_foreign"
    elif problem == "traversal":
        annotations[0].filename = "/mnt/data/../filtered.csv"
    elif problem == "missing":
        annotations.pop()
    elif problem == "conflict":
        annotations.append(annotations[0].model_copy(update={"file_id": "different"}))
    elif problem == "multiple":
        native.output.append(native.output[0])
    else:
        native.status = "incomplete"
    client = MagicMock()
    with pytest.raises(WorkflowError):
        await ci.original_outputs(client, native, 1024)
    client.containers.files.content.with_streaming_response.retrieve.assert_not_called()


async def test_sdk_failure_is_one_shot_sanitized_and_inspectable(tmp_path: Path) -> None:
    item = workflow(tmp_path)
    prepared = await item.prepare(ci.NativeRequest(operation="prepare"))
    client = SimpleNamespace(files=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("secret-token"))))
    request = ci.NativeRequest(operation="analyze", workflow_id=UUID(prepared.workflow_id))
    with pytest.raises(WorkflowError, match="Automatic replay") as error:
        await item.analyze(request, client, "unit")
    assert "secret-token" not in str(error.value)
    assert (await item.inspect(request)).status == "failed"
    with pytest.raises(WorkflowError, match="already submitted"):
        await item.analyze(request, client, "unit")
    assert client.files.create.await_count == 1


async def test_input_replacement_between_inspection_and_submission_never_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = workflow(tmp_path)
    prepared = await item.prepare(ci.NativeRequest(operation="prepare"))
    inspect = item.inspect

    async def replacing_inspect(request):
        reply = await inspect(request)
        ci.home_path(tmp_path, reply.input.path).write_bytes(b"replaced")
        return reply

    monkeypatch.setattr(item, "inspect", replacing_inspect)
    client = SimpleNamespace(files=SimpleNamespace(create=AsyncMock()))
    with pytest.raises(WorkflowError, match="changed before submission"):
        await item.analyze(
            ci.NativeRequest(operation="analyze", workflow_id=UUID(prepared.workflow_id)), client, "unit"
        )
    client.files.create.assert_not_awaited()


async def test_concurrent_submission_cannot_upload_twice(tmp_path: Path) -> None:
    item = workflow(tmp_path)
    prepared = await item.prepare(ci.NativeRequest(operation="prepare"))

    async def failed_upload(**_kwargs):
        await asyncio.sleep(0)
        raise RuntimeError("synthetic transport failure")

    client = SimpleNamespace(files=SimpleNamespace(create=AsyncMock(side_effect=failed_upload)))
    request = ci.NativeRequest(operation="analyze", workflow_id=UUID(prepared.workflow_id))
    failures = await asyncio.gather(
        item.analyze(request, client, "unit"), item.analyze(request, client, "unit"), return_exceptions=True
    )
    assert all(isinstance(value, WorkflowError) for value in failures)
    assert client.files.create.await_count == 1
    assert (await item.inspect(request)).status == "failed"


@pytest.mark.parametrize("problem", ["limit", "empty", "png", "csv", "hash", "json"])
async def test_bad_original_bytes_fail_without_publishing_receipts(tmp_path: Path, problem: str) -> None:
    item = workflow(tmp_path)
    prepared = await item.prepare(ci.NativeRequest(operation="prepare"))
    originals = output_bytes(prepared.input.sha256)
    if problem == "limit":
        item.settings = Settings(story1_native_max_output_bytes=1024)
        originals["revenue_by_category.png"] = PNG + b"x" * 2048
    elif problem == "empty":
        originals["filtered.csv"] = b""
    elif problem == "png":
        originals["revenue_by_category.png"] = b"not a PNG"
    elif problem == "csv":
        originals["by_category.csv"] = b"arbitrary,columns\n"
    elif problem == "hash":
        originals["result.json"] = output_bytes("a" * 64)["result.json"]
    else:
        originals["result.json"] = b"{}"

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files":
            return httpx.Response(
                200,
                json={
                    "id": "file-input",
                    "object": "file",
                    "bytes": prepared.input.size_bytes,
                    "created_at": 1,
                    "filename": prepared.input.filename,
                    "purpose": "assistants",
                },
            )
        if request.url.path == "/responses":
            return httpx.Response(200, json=response().model_dump(mode="json"))
        for name, content in originals.items():
            if request.url.path.endswith(f"/files/file-{name}/content"):
                return httpx.Response(200, content=content)
        raise AssertionError("Unexpected SDK request")

    async with AsyncOpenAI(
        api_key="unit",
        base_url="https://unit.invalid",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    ) as client:
        request = ci.NativeRequest(operation="analyze", workflow_id=UUID(prepared.workflow_id))
        with pytest.raises(WorkflowError, match="Automatic replay"):
            await item.analyze(request, client, "unit")
    failed = await item.inspect(request)
    assert failed.status == "failed" and failed.artifacts == []


@pytest.mark.parametrize("bad_metric", [-1, True, 1.5, "100"])
def test_telemetry_requires_numeric_byte_counts_and_explicit_unavailable(bad_metric: object) -> None:
    raw = json.loads(output_bytes("a" * 64)["result.json"])
    raw["telemetry"]["peak_rss_bytes"] = bad_metric
    with pytest.raises(ValidationError):
        ci.AnalysisResult.model_validate(raw)
    raw["telemetry"]["peak_rss_bytes"] = None
    with pytest.raises(ValidationError):
        ci.AnalysisResult.model_validate(raw)
    raw["telemetry"]["unavailable"]["peak_rss_bytes"] = "not exposed"
    assert ci.AnalysisResult.model_validate(raw).telemetry.peak_rss_bytes is None


@pytest.mark.parametrize("problem", ["disabled", "foreign-agent", "session", "headers", "conversation", "owner"])
async def test_context_guard_precedes_downstream_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ci.Caller, "current", lambda: Caller("call", "owner", "session"))
    monkeypatch.setattr(
        ci.AgentConfig, "from_env", lambda: SimpleNamespace(session_id="foreign" if problem == "session" else "session")
    )
    monkeypatch.setattr(
        ci,
        "get_request_context",
        lambda: SimpleNamespace(
            platform_headers=lambda: (
                {"authorization": "not-trusted"} if problem == "headers" else {"x-agent-foundry-call-id": "call"}
            )
        ),
    )
    project = MagicMock(side_effect=AssertionError("Must not construct a privileged client"))
    monkeypatch.setattr(ci, "AIProjectClient", project)
    store = MemoryStore()

    @asynccontextmanager
    async def open_store(*_args, **_kwargs):
        yield store

    monkeypatch.setattr(ci, "open_store", open_store)
    settings = Settings(
        foundry_project_endpoint="https://unit.invalid/api/projects/p",
        azure_ai_model_deployment_name="unit",
        story1_native_enabled=problem != "disabled",
    )
    with pytest.raises(WorkflowError):
        await ci.native_turn(
            "supervisor" if problem == "foreign-agent" else "data_analysis",
            settings,
            ci.NativeRequest(operation="analyze", workflow_id=uuid4()),
            None if problem == "conversation" else "conversation",
        )
    project.assert_not_called()


async def test_authorized_native_turn_uses_request_local_headers_no_retries_and_sdk_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    caller = Caller("trusted-call", "owner", "session")
    monkeypatch.setattr(ci.Caller, "current", lambda: caller)
    monkeypatch.setattr(ci.AgentConfig, "from_env", lambda: SimpleNamespace(session_id="session"))
    monkeypatch.setattr(
        ci,
        "get_request_context",
        lambda: SimpleNamespace(platform_headers=lambda: {"x-agent-foundry-call-id": "trusted-call"}),
    )
    store = MemoryStore()
    prepared = await workflow(tmp_path, store).prepare(ci.NativeRequest(operation="prepare"))

    @asynccontextmanager
    async def open_store(*_args, **_kwargs):
        yield store

    monkeypatch.setattr(ci, "open_store", open_store)
    credential = MagicMock()
    credential.__aenter__ = AsyncMock(return_value=credential)
    credential.__aexit__ = AsyncMock()
    monkeypatch.setattr(ci, "DefaultAzureCredential", lambda: credential)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock()
    project = MagicMock()
    project.__aenter__ = AsyncMock(return_value=project)
    project.__aexit__ = AsyncMock()
    project.get_openai_client.return_value = client
    factory = MagicMock(return_value=project)
    monkeypatch.setattr(ci, "AIProjectClient", factory)
    analyze = AsyncMock(return_value=prepared)
    monkeypatch.setattr(ci.NativeWorkflow, "analyze", analyze)
    settings = Settings(
        foundry_project_endpoint="https://unit.invalid/api/projects/p",
        azure_ai_model_deployment_name="unit",
        story1_native_enabled=True,
    )
    await ci.native_turn(
        "data_analysis",
        settings,
        ci.NativeRequest(operation="analyze", workflow_id=UUID(prepared.workflow_id)),
        "conversation",
    )
    factory.assert_called_once_with(str(settings.foundry_project_endpoint), credential, allow_preview=True)
    project.get_openai_client.assert_called_once_with(
        default_headers={"x-agent-foundry-call-id": "trusted-call"},
        max_retries=0,
        timeout=300,
    )
    analyze.assert_awaited_once()


@pytest.mark.parametrize("kind", ["native", "denied", "invalid", "background", "recovery", "chat"])
async def test_native_routing_preserves_plain_chat_and_existing_dispatch(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    server = SimpleNamespace()

    def register(handler):
        server.handler = handler
        return handler

    server.response_handler = register
    monkeypatch.setattr(hosting, "ResponsesAgentServerHost", lambda **_kwargs: server)
    turn = AsyncMock(return_value='{"protocol":"poc-story1-native-v1"}')
    if kind == "denied":
        turn.side_effect = ci.NativeWorkflowDenied("Workflow is not accessible in this caller/session/conversation.")
    monkeypatch.setattr(hosting, "native_turn", turn)
    agent = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(text="plain chat", usage_details=None, messages=[]))
    )
    hosting.build_host("data_analysis", Settings(), agent, lambda **_kwargs: None)
    text = "hello" if kind == "chat" else ci.NATIVE_PREFIX + ('{"operation":"prepare"}' if kind != "invalid" else "{}")
    context = SimpleNamespace(
        response_id="response",
        conversation_id="conversation",
        client_headers={},
        is_recovery=kind == "recovery",
        get_input_text=AsyncMock(return_value=text),
        get_history=AsyncMock(return_value=[]),
    )
    stream = await server.handler({"background": kind == "background"}, context, asyncio.Event())
    events = [event async for event in stream]
    if kind == "denied":
        assert events[-1]["type"] == "response.failed"
        assert events[-1]["response"]["error"]["code"] == "native_workflow_denied"
        agent.run.assert_not_awaited()
    elif kind in ("invalid", "background", "recovery"):
        assert events[-1]["type"] == "response.failed"
        turn.assert_not_awaited()
    elif kind == "native":
        assert turn.await_args.args[2].operation == "prepare"
        agent.run.assert_not_awaited()
    else:
        turn.assert_not_awaited()
        agent.run.assert_awaited_once()


def test_data_analysis_has_no_toolbox_fallback_and_research_is_unchanged() -> None:
    settings = Settings(toolbox_endpoint="https://unit.invalid/toolboxes/research/versions/1/mcp")
    credential = MagicMock()
    assert runtime.data_analysis_tools("data_analysis", settings, credential) == []
    assert runtime.research_tools("data_analysis", settings, credential) == []
    assert len(runtime.research_tools("research", settings, credential)) == 1
