"""Real-SDK qualification against synthetic peers; never a hosted approval test."""

import socket

import pytest

from scripts.run_scenario import story6_qualification as qualification


async def test_real_sdk_qualification_preserves_approval_binding_and_exposes_retry_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_socket(*args: object, **kwargs: object) -> None:
        pytest.fail("Offline qualification must not connect a socket.")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    checks = await qualification.qualify()
    assert len(checks) == 8
    assert [check.name for check in checks if check.status == "FAIL"] == [
        "Ordinary MCP wrapper single-send requirement"
    ]
    assert sum(check.status == "PASS" for check in checks) == 7
    retry = checks[-1].observed
    assert retry["call_attempts"] == 2 and retry["reconnections"] == 1
    assert retry["remote_effects_known"] is False
    for check in checks:
        assert check.observed["evidence"].startswith(("local SDK", "real SDK"))


def test_native_mcp_configuration_is_not_a_framework_approval_flag() -> None:
    configuration = qualification.provider_tool_configuration()
    assert configuration["require_approval"] == "always"
    assert configuration["type"] == "mcp"
    assert configuration["allowed_tools"] == [qualification.TOOL_NAME]
    assert "approval_mode" not in configuration


def test_qualification_records_installed_versions() -> None:
    versions = qualification.package_versions()
    assert set(versions) == set(qualification.PACKAGES)
    assert all(versions.values())
