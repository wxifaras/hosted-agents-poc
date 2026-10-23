"""Verify structured redaction and transparent operation failures."""

import json
import logging
from contextlib import nullcontext
from unittest.mock import MagicMock, Mock

import pytest
from app import runtime
from app.core import logger as logging_module
from app.core.logger import JsonFormatter, Logger
from app.core.settings import get_settings


def test_formatter_redacts_sdk_and_application_messages() -> None:
    record = logging.LogRecord("agent", logging.INFO, "", 1, "token=private-value", (), None)
    record.details = {"credential": "another-private-value"}
    rendered = JsonFormatter().format(record)
    assert "private-value" not in rendered
    assert json.loads(rendered)["details"]["credential"] == "[REDACTED]"


def test_operation_failure_propagates() -> None:
    with pytest.raises(ValueError, match="original failure"):
        with Logger("test").trace_operation("example"):
            raise ValueError("original failure")


def test_span_attributes_redact_sensitive_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    span = Mock()
    tracer = Mock()
    tracer.start_as_current_span.return_value = nullcontext(span)
    logger = Logger("test")
    monkeypatch.setattr(logger, "tracer", tracer)
    with logger.trace_operation("example", token="private-value", phase="parsed"):
        pass
    span.set_attribute.assert_any_call("token", "[REDACTED]")
    span.set_attribute.assert_any_call("phase", "parsed")


def _isolate_logging(monkeypatch: pytest.MonkeyPatch) -> Mock:
    configure = Mock()
    monkeypatch.setattr(logging_module, "configure_observability", configure)
    monkeypatch.setattr(logging_module, "_telemetry_service", None)
    logging_api = Mock(wraps=logging)
    logging_api.getLogger.return_value = Mock(handlers=[])
    monkeypatch.setattr(logging_module, "logging", logging_api)
    return configure


@pytest.mark.parametrize("service", ["supervisor", "research", "data_analysis"])
def test_telemetry_uses_hosting_sdk_once_without_sensitive_data(monkeypatch: pytest.MonkeyPatch, service: str) -> None:
    configure = _isolate_logging(monkeypatch)
    logging_module.configure_logging(service)
    configure.assert_not_called()
    logging_module.configure_logging(service, level="DEBUG", connection_string="test-connection")
    logging_module.configure_logging(service, connection_string="test-connection")
    configure.assert_called_once_with(
        connection_string="test-connection", log_level="DEBUG", enable_sensitive_data=False
    )


def test_telemetry_configuration_failure_is_not_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    configure = _isolate_logging(monkeypatch)
    configure.side_effect = ValueError("invalid telemetry configuration")
    with pytest.raises(ValueError, match="invalid telemetry configuration"):
        logging_module.configure_logging("data_analysis", connection_string="invalid")
    assert logging_module._telemetry_service is None


@pytest.mark.parametrize(("auth_mode", "expected"), [("entra", "entra"), (None, None)])
def test_entra_auth_mode_reaches_sdk_before_export_is_configured(
    monkeypatch: pytest.MonkeyPatch, auth_mode: str | None, expected: str | None
) -> None:
    configure = _isolate_logging(monkeypatch)
    environ: dict[str, str] = {}
    monkeypatch.setattr(logging_module.os, "environ", environ)
    seen: list[str | None] = []
    configure.side_effect = lambda **_kwargs: seen.append(environ.get("APPLICATIONINSIGHTS_AUTH_MODE"))
    logging_module.configure_logging("data_analysis", auth_mode=auth_mode)
    assert environ == {}
    logging_module.configure_logging("data_analysis", connection_string="test-connection", auth_mode=auth_mode)
    assert seen == [expected]


@pytest.mark.parametrize(
    ("configured", "platform", "expected"),
    [(None, "platform", "platform"), ("configured", "platform", "configured"), (None, None, None)],
)
def test_host_callback_prefers_configured_connection_then_platform_value(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, platform: str | None, expected: str | None
) -> None:
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/poc")
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "not-invoked")
    if configured:
        monkeypatch.setenv("POC_APPLICATIONINSIGHTS_CONNECTION_STRING", configured)
    monkeypatch.setenv("POC_APPLICATIONINSIGHTS_AUTH_MODE", "entra")
    get_settings.cache_clear()
    callbacks = []
    monkeypatch.setattr(runtime, "DefaultAzureCredential", MagicMock())
    monkeypatch.setattr(runtime, "build_agent", Mock())
    monkeypatch.setattr(runtime, "build_host", lambda *args: callbacks.append(args[3]) or Mock())
    configure_logging = Mock()
    monkeypatch.setattr(runtime, "configure_logging", configure_logging)
    runtime.run_agent("data_analysis")
    callbacks[0](connection_string=platform, log_level=None, enable_sensitive_data=True)
    assert configure_logging.call_args.args == ("data_analysis",)
    assert configure_logging.call_args.kwargs["connection_string"] == expected
    assert configure_logging.call_args.kwargs["auth_mode"] == "entra"
