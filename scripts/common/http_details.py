"""Allowlisted HTTP diagnostics shared by local and hosted smoke tests."""

from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from app.core.redaction import sanitize_text

REQUEST_HEADERS = frozenset({"accept", "content-type", "x-client-request-id"})
RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "x-request-id",
        "x-ms-request-id",
        "apim-request-id",
        "retry-after",
        "retry-after-ms",
        "x-ms-region",
        "traceparent",
        "x-ms-agent-name",
        "x-ms-agent-version",
    }
)


def selected_headers(headers: Mapping[str, str], allowed: frozenset[str]) -> dict[str, str]:
    """Never enumerate authentication, cookies, call IDs, or unapproved diagnostic headers into evidence."""
    return {key.lower(): sanitize_text(value) for key, value in headers.items() if key.lower() in allowed}


def http_details(
    method: str,
    uri: str,
    request_headers: Mapping[str, str],
    *,
    status_code: int | None = None,
    response_headers: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Describe the actual SDK/HTTP request, stripping userinfo, fragments, and non-allowlisted query values."""
    parsed = urlsplit(uri)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    authority = f"{hostname}:{parsed.port}" if parsed.port else hostname
    safe_uri = urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    query = {key: sanitize_text(value) for key, value in parse_qsl(parsed.query) if key == "api-version"}
    return {
        "method": method,
        "uri": sanitize_text(safe_uri),
        "query_parameters": query,
        "request_headers": selected_headers(request_headers, REQUEST_HEADERS),
        "status_code": status_code,
        "response_headers": selected_headers(response_headers or {}, RESPONSE_HEADERS),
    }
