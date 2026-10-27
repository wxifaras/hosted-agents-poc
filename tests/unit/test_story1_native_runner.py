"""Native validation contracts without credentials, service calls, or generated execution."""

import csv
import hashlib
import io
import json
import struct
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from scripts.common.reporting import REPORT_FILE, MarkdownReport, ValidationRun
from scripts.common.synthetic_data import COLUMNS, build_dataset, expected_aggregates, generate_rows
from scripts.run_scenario import story1_native as native
from scripts.run_scenario.story1_data_analysis import BY_CATEGORY_COLUMNS, PNG_SIGNATURE, total_of


def original_outputs() -> dict[str, bytes]:
    dataset = build_dataset(1000)
    expected = expected_aggregates(generate_rows(1000))
    filtered = io.StringIO(newline="")
    writer = csv.DictWriter(filtered, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    rows = csv.DictReader(io.StringIO(dataset.content.decode()))
    selected = [row for row in rows if row["status"] == "completed" and row["region"] == "West"]
    writer.writerows(selected)
    by_category = io.StringIO(newline="")
    grouped = csv.writer(by_category, lineterminator="\n")
    grouped.writerow(BY_CATEGORY_COLUMNS)
    grouped.writerows([name, value.rows, value.units, value.revenue_cents] for name, value in expected.items())
    return {
        "result.json": json.dumps(
            {
                "input_sha256": dataset.sha256,
                "filtered_rows": len(selected),
                "by_category": {name: asdict(value) for name, value in expected.items()},
                "total": asdict(total_of(expected)),
                "telemetry": {
                    "peak_rss_bytes": 100,
                    "memory_limit_bytes": 1000,
                    "cgroup_memory_peak_bytes": 120,
                    "unavailable": {},
                },
            }
        ).encode(),
        "filtered.csv": filtered.getvalue().encode(),
        "by_category.csv": by_category.getvalue().encode(),
        "revenue_by_category.png": PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1000, 600),
    }


def test_original_outputs_match_independent_oracle() -> None:
    assert all(native.verify_outputs(build_dataset(1000), original_outputs()).values())


def test_corrupt_numeric_cell_cannot_pass_row_identity_check() -> None:
    files = original_outputs()
    text = files["filtered.csv"].decode()
    rows = text.splitlines()
    cells = rows[1].split(",")
    cells[4] = "999999"
    rows[1] = ",".join(cells)
    files["filtered.csv"] = ("\n".join(rows) + "\n").encode()
    assert not native.verify_outputs(build_dataset(1000), files)["filtered_csv"]


@pytest.mark.parametrize("filename", ["extra.csv", "../result.json"])
def test_unapproved_output_inventory_rejected(filename: str) -> None:
    files = original_outputs()
    files[filename] = b"x"
    with pytest.raises(ValueError, match="inventory"):
        native.verify_outputs(build_dataset(1000), files)


def test_truncated_png_does_not_count_as_valid_output() -> None:
    files = original_outputs()
    files["revenue_by_category.png"] = PNG_SIGNATURE
    assert not native.verify_outputs(build_dataset(1000), files)["png"]


def test_memory_target_uses_interpreter_peak_and_observed_limit() -> None:
    reading = native.capacity_reading(
        {"peak_rss_bytes": 768, "memory_limit_bytes": 1024, "cgroup_memory_peak_bytes": 900}
    )
    assert reading.pressure_fraction == 0.75
    assert native.capacity_reading({"peak_rss_bytes": 768}).pressure_fraction is None
    assert native.capacity_reading({"peak_rss_bytes": True, "memory_limit_bytes": 1024}).pressure_fraction is None
    assert native.capacity_reading({"peak_rss_bytes": "768", "memory_limit_bytes": 1024}).pressure_fraction is None


def test_finite_pressure_profile_does_not_mutate_baseline() -> None:
    assert native.profile_sizes("baseline") == (1000, 10_000, 100_000)
    assert native.profile_sizes("pressure") == (250_000, 500_000, 1_000_000)


def test_chart_review_requires_hash_and_actual_grouped_values() -> None:
    dataset = build_dataset(1000)
    png = original_outputs()["revenue_by_category.png"]
    expected = expected_aggregates(generate_rows(1000))
    review = {
        "accepted": True,
        "png_sha256": hashlib.sha256(png).hexdigest(),
        "categories": sorted(expected),
        "revenue_cents": [value.revenue_cents for value in expected.values()],
    }
    assert native.validate_chart_review(dataset, png, review)
    assert not native.validate_chart_review(dataset, png + b"different-original", review)
    review["revenue_cents"] = [0] * len(expected)
    assert not native.validate_chart_review(dataset, png, review)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--step", "3", "--timeout", "nan"],
        ["--step", "3", "--timeout", "0"],
        ["--step", "3", "--max-seconds", "1900"],
        ["--step", "2", "--profile", "pressure"],
        ["--step", "2", "--hosted-idle", "observe"],
        ["--step", "2", "--review-run", "previous"],
    ],
)
def test_unsupported_or_unbounded_options_rejected(arguments: list[str]) -> None:
    parser = native.build_parser()
    with pytest.raises(SystemExit):
        native.validate_arguments(parser, parser.parse_args(arguments))


def test_preview_is_explicit_about_side_effects_and_redesign_gate() -> None:
    args = native.build_parser().parse_args(["--step", "3", "--profile", "pressure"])
    result = native.preview(args)
    assert args.execution == "preview"
    assert result["rows"] == [250_000, 500_000, 1_000_000]
    assert result["service_deletion"] is False
    assert result["deployment"] is False
    assert "sponsor approval" in str(result["optional_probe_policy"])


def test_preview_never_constructs_azure_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Preview attempted Azure client/credential construction.")

    monkeypatch.setattr(native, "AIProjectClient", forbidden)
    monkeypatch.setattr(native, "AzureCliCredential", forbidden)
    assert native.main(["--step", "1"]) == 0


def test_integer_csv_formatting_is_semantic_not_a_serialization_requirement() -> None:
    assert native.same_csv_row({"units": "19.0"}, {"units": "19"})
    assert not native.same_csv_row({"units": "19.5"}, {"units": "19"})
    assert not native.same_csv_row({"units": "NaN"}, {"units": "19"})
    assert not native.same_csv_row({"units": ""}, {"units": "19"})
    assert native.same_csv_row({"units": ""}, {"units": ""})


def make_validation(tmp_path: Path, step: int = 2) -> native.NativeValidation:
    args = native.build_parser().parse_args(["--step", str(step), "--execution", "run"])
    args.output_dir = tmp_path / "story-1"
    run = ValidationRun("native-test", "hosted", args.output_dir, no_color=True)
    return native.NativeValidation(args, run, MarkdownReport(run.path / REPORT_FILE, "Native test"))


def receipts(files: dict[str, bytes], workflow: str) -> list[dict[str, object]]:
    return [
        {
            "artifact_id": str(uuid4()),
            "filename": name,
            "path": f"story1-native/{workflow}/outputs/{name}",
            "media_type": "image/png" if name.endswith(".png") else "application/octet-stream",
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        for name, body in files.items()
    ]


@pytest.mark.asyncio
async def test_fixed_native_protocol_to_original_session_files(tmp_path: Path) -> None:
    from app.services.story1_native import NativeReply, NativeRequest

    dataset = build_dataset(1000)
    workflow = str(uuid4())
    files = original_outputs()
    prepared = {
        "protocol": native.PROTOCOL,
        "workflow_id": workflow,
        "status": "prepared",
        "profile": "baseline",
        "input": {
            "filename": dataset.filename,
            "path": f"story1-native/{workflow}/input.csv",
            "sha256": dataset.sha256,
            "size_bytes": dataset.size_bytes,
            "rows": dataset.rows,
        },
        "artifacts": [],
        "result": None,
        "observation": None,
    }
    complete = {
        **prepared,
        "status": "completed",
        "artifacts": receipts(files, workflow),
        "result": json.loads(files["result.json"]),
    }
    response_texts = [NativeReply.model_validate(value).model_dump_json() for value in (prepared, complete)]
    hosted = Mock()

    async def create(**kwargs: object) -> Mock:
        text = str(kwargs["input"])
        command = NativeRequest.model_validate_json(text.removeprefix(native.COMMAND_PREFIX))
        assert kwargs["conversation"] == "conversation"
        assert kwargs["extra_body"] == {"agent_session_id": "session"}
        assert command.operation in ("prepare", "analyze")
        raw = Mock()
        raw.parse.return_value = SimpleNamespace(status="completed", error=None, output_text=response_texts.pop(0))
        return raw

    hosted.responses.with_raw_response.create = AsyncMock(side_effect=create)
    project = Mock()

    async def download(agent: str, session: str, *, path: str) -> object:
        assert agent == "data-analysis-agent" and session == "session"

        async def chunks():
            yield files[path.split("/")[-1]]

        return chunks()

    project.agents.download_session_file = AsyncMock(side_effect=download)
    actor = native.NativeActor(project, hosted, "session", "conversation")
    validation = make_validation(tmp_path)
    binding, original, result = await validation.analyze(actor, dataset, save_to=validation.run.path / "outputs")
    assert binding["workflow_id"] == workflow and original == files
    assert result["input_sha256"] == dataset.sha256
    assert hosted.responses.with_raw_response.create.await_count == 2
    assert project.agents.download_session_file.await_count == 4
    assert validation.run.statuses == ["PASS"]


@pytest.mark.asyncio
async def test_changed_original_bytes_fail_receipt_validation() -> None:
    project = Mock()

    async def download(*args: object, **kwargs: object) -> object:
        async def chunks():
            yield b"wrong bytes"

        return chunks()

    project.agents.download_session_file = AsyncMock(side_effect=download)
    actor = native.NativeActor(project, Mock(), "session", "conversation")
    original = {"result.json": b"original"}
    with pytest.raises(ValueError, match="receipt"):
        await native.download_originals(actor, receipts(original, str(uuid4())))


@pytest.mark.asyncio
async def test_capacity_stops_at_first_failure_without_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    validation = make_validation(tmp_path, 3)
    attempted: list[int] = []

    async def analyze(*args: object, **kwargs: object) -> object:
        dataset = args[1]
        assert isinstance(dataset, native.Dataset)
        attempted.append(dataset.rows)
        raise native.NativeFailure("native_execution_failed")

    monkeypatch.setattr(validation, "analyze", analyze)
    await validation.capacity(native.NativeActor(Mock(), Mock(), "session", "conversation"))
    assert attempted == [1000]
    assert validation.run.statuses == ["FAIL", "BLOCKED"]


def test_baseline_cannot_accept_toolbox_or_version_drift(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    current = {"data-analysis-agent": {"version": "2"}}
    path.write_text(json.dumps({"environment": "env", "execution": "toolbox", "agents": current}))
    with pytest.raises(native.Blocked, match="baseline differs"):
        native.baseline_guard(path, "env", current)
    path.write_text(json.dumps({"environment": "env", "execution": native.PROTOCOL, "agents": current}))
    native.baseline_guard(path, "env", current)
    with pytest.raises(native.Blocked, match="baseline differs"):
        native.baseline_guard(path, "env", {"data-analysis-agent": {"version": "3"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_status", [403, 404, 500])
async def test_blob_failure_never_reconstructs_from_local_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, storage_status: int
) -> None:
    import httpx

    from scripts.run_scenario import story1_sandbox as sandbox

    validation = make_validation(tmp_path, 5)
    validation.version = "7"
    result = json.loads(original_outputs()["result.json"])
    calls = []

    async def analyze(*args: object, **kwargs: object) -> tuple[dict[str, object], dict[str, bytes], dict[str, object]]:
        calls.append("analysis")
        return {"workflow_id": str(uuid4()), "container_fingerprint": "a" * 64}, original_outputs(), result

    class Store:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def put(self, name: str, data: bytes) -> httpx.Response:
            return httpx.Response(201)

        def get(self, name: str) -> httpx.Response:
            return httpx.Response(storage_status)

        def delete(self, name: str) -> None:
            raise AssertionError("Default validation must retain the Blob checkpoint.")

    monkeypatch.setattr(validation, "analyze", analyze)
    monkeypatch.setattr(sandbox, "BlobStore", Store)
    fresh_actor = AsyncMock(side_effect=AssertionError("Failed Blob read must not create a replacement session."))
    monkeypatch.setattr(native, "create_actor", fresh_actor)
    credential = Mock()
    credential.get_token = AsyncMock(return_value=SimpleNamespace(token="test-token"))
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        await validation.recovery(
            native.NativeActor(Mock(), Mock(), "original-session", "conversation"),
            credential,
            {"AZURE_STORAGE_BLOB_ENDPOINT": "https://example.blob.core.windows.net/"},
            stack,
        )
    assert calls == ["analysis"]
    fresh_actor.assert_not_called()
    assert validation.run.statuses and all(status != "PASS" for status in validation.run.statuses)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_container", ["a" * 64, "b" * 64])
async def test_blob_reconstruction_requires_a_distinct_native_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, new_container: str
) -> None:
    import httpx

    from scripts.run_scenario import story1_sandbox as sandbox

    validation = make_validation(tmp_path, 5)
    validation.version = "7"
    result = json.loads(original_outputs()["result.json"])
    containers = iter(["a" * 64, new_container])

    async def analyze(*args: object, **kwargs: object) -> tuple[dict[str, object], dict[str, bytes], dict[str, object]]:
        return {"workflow_id": str(uuid4()), "container_fingerprint": next(containers)}, original_outputs(), result

    class Store:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.original = b""

        def put(self, name: str, data: bytes) -> httpx.Response:
            self.original = data
            return httpx.Response(201)

        def get(self, name: str) -> httpx.Response:
            return httpx.Response(200, content=self.original)

        def delete(self, name: str) -> None:
            raise AssertionError("No checkpoint deletion is approved.")

    monkeypatch.setattr(validation, "analyze", analyze)
    monkeypatch.setattr(sandbox, "BlobStore", Store)
    fresh = native.NativeActor(Mock(), Mock(), "fresh-session", "fresh-conversation")
    monkeypatch.setattr(native, "create_actor", AsyncMock(return_value=fresh))
    credential = Mock()
    credential.get_token = AsyncMock(return_value=SimpleNamespace(token="test-token"))
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        await validation.recovery(
            native.NativeActor(Mock(), Mock(), "original-session", "conversation"),
            credential,
            {"AZURE_STORAGE_BLOB_ENDPOINT": "https://example.blob.core.windows.net/"},
            stack,
        )
    assert validation.run.statuses == (["PASS"] if new_container != "a" * 64 else ["FAIL"])


def test_offline_review_uses_original_bytes_without_new_service_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation = make_validation(tmp_path)
    validation.args.environment = "env"
    validation.before = {"data-analysis-agent": {"version": "7"}}
    files = original_outputs()
    original = validation.args.output_dir / "step-02-analysis-and-outputs" / "original-native"
    (original / "outputs").mkdir(parents=True)
    for name, body in files.items():
        (original / "outputs" / name).write_bytes(body)
    (original / "analysis-context.json").write_text(
        json.dumps(
            {
                "protocol": native.PROTOCOL,
                "environment": "env",
                "agents": validation.before,
                "rows": 1000,
                "original_sha256": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
            }
        )
    )
    (validation.args.output_dir / native.NATIVE_BASELINE).write_text(
        json.dumps({"execution": native.PROTOCOL, "environment": "env", "agents": validation.before})
    )
    expected = expected_aggregates(generate_rows(1000))
    receipt = tmp_path / "review.json"
    receipt.write_text(
        json.dumps(
            {
                "accepted": True,
                "png_sha256": hashlib.sha256(files["revenue_by_category.png"]).hexdigest(),
                "categories": sorted(expected),
                "revenue_cents": [value.revenue_cents for value in expected.values()],
            }
        )
    )
    validation.args.review_run = original
    validation.args.chart_review = receipt
    monkeypatch.setattr(native, "AIProjectClient", Mock(side_effect=AssertionError("Offline review is not live.")))
    native.offline_chart_review(validation)
    native.write_analysis_evidence(validation)
    assert validation.run.statuses == ["PASS", "PASS"]
    assert (validation.args.output_dir / "native-analysis-evidence.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", ["unknown", "None", ""])
async def test_readiness_does_not_certify_an_unknown_deployed_identity(principal: str) -> None:
    record = {
        "name": "supervisor-agent",
        "instance_identity": {"principal_id": principal},
        "versions": {"latest": {"version": "7", "status": "active", "definition": {}}},
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [
                    {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                ]
            }
        },
    }
    project = Mock()
    project.agents.get = AsyncMock(return_value=SimpleNamespace(as_dict=lambda: record))
    with pytest.raises(native.Blocked, match="known deployed identity"):
        await native.snapshots(project)


@pytest.mark.asyncio
async def test_readiness_uses_native_flag_and_hashes_identity() -> None:
    principal = str(uuid4())

    async def get(name: str) -> SimpleNamespace:
        record = {
            "name": name,
            "instance_identity": {"principal_id": principal},
            "versions": {
                "latest": {
                    "version": "7",
                    "status": "active",
                    "definition": {"environment_variables": {"STORY1_NATIVE_ENABLED": "true"}},
                }
            },
            "agent_endpoint": {
                "version_selector": {
                    "version_selection_rules": [
                        {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100}
                    ]
                }
            },
        }
        return SimpleNamespace(as_dict=lambda: record)

    project = Mock()
    project.agents.get = AsyncMock(side_effect=get)
    observed, version = await native.snapshots(project)
    assert version == "7" and len(observed) == 3
    assert principal not in json.dumps(observed)
