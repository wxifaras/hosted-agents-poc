"""Offline least-privilege RBAC checks; grants and sign-ins are always mocked."""

import json
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import JsonValue

from scripts.common import auth_profiles as profiles

TENANT = "11111111-1111-4111-8111-111111111111"
USER1 = "user1@example.onmicrosoft.com"
USER2 = "user2@example.onmicrosoft.com"
SUBSCRIPTION = "22222222-2222-4222-8222-222222222222"
ROLE = "33333333-3333-4333-8333-333333333333"
PRINCIPALS = {USER1: "44444444-4444-4444-8444-444444444444", USER2: "55555555-5555-4555-8555-555555555555"}
PROJECT = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/poc/providers/Microsoft.CognitiveServices/accounts/poc/projects/dev"
)


@pytest.fixture
def rbac(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict[str, JsonValue], list[str], set[str]]:
    context: dict[str, JsonValue] = {
        "AZURE_TENANT_ID": TENANT,
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_AI_PROJECT_ID": PROJECT,
    }
    events: list[str] = []
    assigned: set[str] = set()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "operator"))
    monkeypatch.setattr(profiles, "native_json", lambda *_args: context)
    monkeypatch.setattr(profiles, "assignment_allowed", lambda *_args: True)
    monkeypatch.setattr(profiles, "authenticate", lambda user, *_args, **_kwargs: events.append("login:" + user))

    def azure(arguments: list[str]) -> JsonValue:
        if arguments[:2] == ["account", "show"]:
            events.append("operator")
            return {"tenantId": TENANT, "id": SUBSCRIPTION}
        if arguments[:3] == ["role", "definition", "list"]:
            return [
                {
                    "roleName": profiles.CONSUMER_ROLE,
                    "roleType": "BuiltInRole",
                    "name": ROLE,
                    "permissions": [
                        {
                            "actions": [],
                            "notActions": [],
                            "dataActions": [profiles.INTERACT_ACTION],
                            "notDataActions": [],
                        }
                    ],
                }
            ]
        if arguments[0] == "rest":
            user = unquote(urlsplit(arguments[arguments.index("--url") + 1]).path.rsplit("/", 1)[-1])
            events.append("resolve:" + user)
            return {"id": PRINCIPALS[user], "userPrincipalName": user, "accountEnabled": True, "userType": "Member"}
        if arguments[:3] == ["role", "assignment", "list"]:
            assert "--all" not in arguments
            assert "--include-inherited" in arguments and "--include-groups" in arguments
            assert arguments[arguments.index("--scope") + 1] == PROJECT
            principal = arguments[arguments.index("--assignee-object-id") + 1]
            events.append("read:" + principal)
            return (
                [
                    {
                        "principalId": principal,
                        "roleDefinitionId": "/roles/" + ROLE,
                        "scope": PROJECT,
                        "condition": None,
                    }
                ]
                if principal in assigned
                else []
            )
        raise AssertionError(f"Unexpected CLI call: {arguments}")

    def create(grant: profiles.Grant, principal: str, subscription: str, *, principal_type: str) -> None:
        assert (grant.role, grant.scope, grant.definition_id) == (profiles.CONSUMER_ROLE, PROJECT, ROLE)
        assert subscription == SUBSCRIPTION and principal_type == "User"
        events.append("grant:" + principal)
        assigned.add(principal)

    monkeypatch.setattr(profiles, "az", azure)
    monkeypatch.setattr(profiles, "create_grant", create)
    return context, events, assigned


@pytest.mark.parametrize(("apply", "existing"), [(False, False), (True, False), (True, True)])
def test_rbac_previews_or_applies_before_login_and_reuses_existing_grants(
    apply: bool, existing: bool, rbac: tuple[dict[str, JsonValue], list[str], set[str]], tmp_path: Path
) -> None:
    _, events, assigned = rbac
    if existing:
        assigned.update(PRINCIPALS.values())
    args = ["--users", f"{USER1},{USER2}", "--rbac", "consumer", "--output-dir", str(tmp_path / "evidence")]
    if apply:
        args.append("--apply")
    assert profiles.main(args) == 0
    grants = [event for event in events if event.startswith("grant:")]
    assert len(grants) == (2 if apply and not existing else 0)
    assert len([event for event in events if event.startswith("login:")]) == (2 if apply else 0)
    if grants:
        assert events.index("resolve:" + USER2) < events.index(grants[0])
        assert max(events.index(grant) for grant in grants) < events.index("login:" + USER1)
    if not apply:
        assert not assigned and not (tmp_path / "local").exists()


@pytest.mark.parametrize(
    "reason", ["disabled", "duplicate-object", "custom-role", "wildcard-role", "wrong-tenant", "account-scope"]
)
def test_invalid_rbac_plan_never_grants_or_starts_signin(
    reason: str,
    rbac: tuple[dict[str, JsonValue], list[str], set[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context, events, assigned = rbac
    original = profiles.az

    def changed(arguments: list[str]) -> JsonValue:
        value = original(arguments)
        if arguments[0] == "rest":
            record = profiles.object_value(value)
            if reason == "disabled":
                record["accountEnabled"] = False
            if reason == "duplicate-object":
                record["id"] = PRINCIPALS[USER1]
        if arguments[:3] == ["role", "definition", "list"]:
            assert isinstance(value, list)
            role = profiles.object_value(value[0])
            if reason == "custom-role":
                role["roleType"] = "CustomRole"
            if reason == "wildcard-role":
                role["permissions"] = [{"actions": [], "notActions": [], "dataActions": ["*"], "notDataActions": []}]
        return value

    monkeypatch.setattr(profiles, "az", changed)
    if reason == "wrong-tenant":
        context["AZURE_TENANT_ID"] = "66666666-6666-4666-8666-666666666666"
    if reason == "account-scope":
        context["AZURE_AI_PROJECT_ID"] = PROJECT.rsplit("/projects/", 1)[0]
    assert (
        profiles.main(
            [
                "--users",
                f"{USER1},{USER2}",
                "--tenant",
                TENANT,
                "--rbac",
                "consumer",
                "--apply",
                "--output-dir",
                str(tmp_path / "evidence"),
            ]
        )
        == 2
    )
    assert not assigned and not any(event.startswith(("grant:", "login:")) for event in events)


def test_no_assignment_permission_blocks_before_any_user_login(
    rbac: tuple[dict[str, JsonValue], list[str], set[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events, assigned = rbac
    monkeypatch.setattr(profiles, "assignment_allowed", lambda *_args: False)
    assert (
        profiles.main(
            [
                "--users",
                USER1,
                "--rbac",
                "consumer",
                "--apply",
                "--output-dir",
                str(tmp_path / "evidence"),
            ]
        )
        == 1
    )
    assert not assigned and not any(event.startswith("login:") for event in events)


@pytest.mark.parametrize(
    ("scope", "condition", "present"),
    [
        (PROJECT, None, True),
        (PROJECT.rsplit("/projects/", 1)[0], None, True),
        (PROJECT + "-other", None, False),
        (PROJECT, "restricted", None),
    ],
)
def test_only_unconditional_consumer_assignments_cover_the_project(
    scope: str, condition: str | None, present: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        profiles,
        "az",
        MagicMock(
            return_value=[
                {
                    "roleDefinitionId": "/roles/" + ROLE,
                    "scope": scope,
                    "condition": condition,
                }
            ]
        ),
    )
    if present is None:
        with pytest.raises(profiles.ProfileError, match="conditional"):
            profiles.consumer_present(PRINCIPALS[USER1], PROJECT, SUBSCRIPTION, ROLE)
    else:
        assert profiles.consumer_present(PRINCIPALS[USER1], PROJECT, SUBSCRIPTION, ROLE) is present


def test_unverified_assignment_is_not_claimed_as_access_and_does_not_start_login(
    rbac: tuple[dict[str, JsonValue], list[str], set[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, events, assigned = rbac
    monkeypatch.setattr(profiles, "consumer_present", lambda *_args: False)
    output = tmp_path / "evidence"
    assert (
        profiles.main(
            [
                "--users",
                USER1,
                "--rbac",
                "consumer",
                "--apply",
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert assigned == {PRINCIPALS[USER1]} and not any(event.startswith("login:") for event in events)
    rows = [json.loads(line) for line in (next(output.iterdir()) / "results.jsonl").read_text().splitlines()]
    assert not any(row["step"].startswith("Consumer RBAC readback:") for row in rows)
