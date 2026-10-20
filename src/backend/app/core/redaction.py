"""Sanitize diagnostics before they reach console, evidence, or telemetry."""

import re
from collections.abc import Mapping

from pydantic import JsonValue

_SENSITIVE = re.compile(
    r"password|secret|token|credential|authorization|connection.?string|call.?id|api.?key|signature|encrypted_content",
    re.I,
)
_BEARER = re.compile(r"\bBearer\s+\S+", re.I)
_ASSIGNMENT = re.compile(
    r"""((?:password|secret|token|api[_-]?key|call[_-]?id|authorization)["']?\s*[=:]\s*["']?)[^\s,;"']+""",
    re.I,
)
_URL_QUERY = re.compile(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", re.I)
_TOKEN_COUNTS = frozenset({"input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"})
_PRESENCE_FLAGS = frozenset(
    {"hosted_runtime", "call_id_present", "user_id_present", "session_id_present", "conversation_id_present"}
)


def sanitize_text(value: str) -> str:
    """Remove common embedded credentials and URL query strings."""
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _ASSIGNMENT.sub(r"\1[REDACTED]", value)
    return _URL_QUERY.sub(r"\1?[REDACTED]", value)


def sanitize(value: object) -> JsonValue:
    """Return JSON-safe diagnostics with sensitive mapping values removed."""
    if isinstance(value, Mapping):
        return {
            str(key): (
                item
                if (str(key) in _TOKEN_COUNTS and type(item) is int and item >= 0)
                or (str(key) in _PRESENCE_FLAGS and type(item) is bool)
                else "[REDACTED]"
                if _SENSITIVE.search(str(key))
                else sanitize(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(str(value))
