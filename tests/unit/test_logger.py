"""Verify structured redaction and transparent operation failures."""

import json
import logging
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from app.core.logger import JsonFormatter, Logger


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
