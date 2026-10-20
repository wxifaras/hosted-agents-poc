"""Regression tests for environment-scoped timestamped deployment selection."""

import pytest

from scripts.common.deployments import resolve_deployment

ENVIRONMENT = "hosted-agents-poc-dev"


def entry(name: str, timestamp: str, *, environment: str = ENVIRONMENT, state: str = "Succeeded") -> dict:
    return {"name": name, "time": timestamp, "environment": environment, "state": state}


def test_chooses_latest_successful_exact_environment_not_lexicographic_name() -> None:
    rows = [
        entry("z-old", "2026-10-19T18:00:00Z"),
        entry("a-latest", "2026-10-20T18:00:00Z"),
        entry("wrong-environment", "2026-10-21T18:00:00Z", environment="other"),
        entry("failed", "2026-10-22T18:00:00Z", state="Failed"),
    ]
    calls = []

    def read(arguments: list[str]) -> list:
        calls.append(arguments)
        return rows

    assert resolve_deployment(read, "subscription", ENVIRONMENT) == "a-latest"
    assert calls[0][:3] == ["deployment", "sub", "list"]
    assert "properties.parameters.environmentName.value" in calls[0][-1]


def test_explicit_override_is_not_replaced_or_queried() -> None:
    def read(_arguments: list[str]) -> None:
        raise AssertionError("Explicit selection must not trigger discovery.")

    assert resolve_deployment(read, "subscription", ENVIRONMENT, "chosen-123") == "chosen-123"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [entry("wrong", "2026-10-20T18:00:00Z", environment="other")],
        [entry("failed", "2026-10-20T18:00:00Z", state="Failed")],
    ],
)
def test_no_successful_match_is_explicit_failure(rows: list) -> None:
    with pytest.raises(ValueError, match="No successful"):
        resolve_deployment(lambda _args: rows, "subscription", ENVIRONMENT)


def test_ambiguous_latest_requires_override() -> None:
    rows = [entry("one", "2026-10-20T18:00:00Z"), entry("two", "2026-10-20T18:00:00Z")]
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_deployment(lambda _args: rows, "subscription", ENVIRONMENT)


def test_bad_environment_cannot_be_interpolated_into_query() -> None:
    with pytest.raises(ValueError, match="Environment"):
        resolve_deployment(lambda _args: [], "subscription", "name' || 'other")
