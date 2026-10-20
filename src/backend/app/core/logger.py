"""Structured stdout logging and optional, explicitly initialized Azure telemetry."""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from time import perf_counter

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import Span, Status, StatusCode

from app.core.redaction import sanitize

_telemetry_service: str | None = None


class JsonFormatter(logging.Formatter):
    """Render application and SDK diagnostics through the same redaction boundary."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "details": getattr(record, "details", {}),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(sanitize(payload), ensure_ascii=True)


def configure_logging(service: str, *, level: str = "INFO", connection_string: str | None = None) -> None:
    """Configure once at startup; requested telemetry errors are not swallowed."""
    global _telemetry_service
    root = logging.getLogger()
    if not any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
    if connection_string and _telemetry_service is None:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
        except ImportError as error:
            raise RuntimeError("Azure telemetry requires: uv sync --extra telemetry") from error
        from agent_framework.observability import enable_instrumentation

        configure_azure_monitor(
            connection_string=connection_string,
            resource=Resource.create({"service.name": f"{service}-agent", "service.version": "0.1.0"}),
            enable_live_metrics=False,
        )
        enable_instrumentation(enable_sensitive_data=False)
        _telemetry_service = service


class Logger:
    """Reference-style facade with no import-time configuration or network activity."""

    def __init__(self, name: str) -> None:
        self.logger = logging.getLogger(name)
        self.tracer = trace.get_tracer(name)

    def info(self, message: str, **context: object) -> None:
        """Write a sanitized structured lifecycle event."""
        self.logger.info(message, extra={"details": sanitize(context)})

    def error(self, message: str, **context: object) -> None:
        """Write sanitized error details without exposing raw credential context."""
        self.logger.error(message, extra={"details": sanitize(context)})

    @contextmanager
    def trace_operation(self, operation: str, **context: str | int | float | bool) -> Iterator[Span]:
        """Trace an operation and propagate its original exception to the caller."""
        started = perf_counter()
        with self.tracer.start_as_current_span(
            operation, record_exception=False, set_status_on_exception=False
        ) as span:
            safe_context = sanitize(context)
            if isinstance(safe_context, dict):
                for key, value in safe_context.items():
                    if isinstance(value, (str, int, float, bool)):
                        span.set_attribute(key, value)
            try:
                yield span
            except Exception as error:
                span.set_status(Status(StatusCode.ERROR, type(error).__name__))
                self.error("Operation failed", operation=operation, error_type=type(error).__name__)
                raise
            else:
                span.set_status(Status(StatusCode.OK))
            finally:
                span.set_attribute("duration_ms", (perf_counter() - started) * 1000)
