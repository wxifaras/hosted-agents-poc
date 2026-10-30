"""Runtime access scope tests; no Azure calls or real permission changes."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.deploy import story6_access as access
from scripts.deploy.grant_local_rbac import Grant

PRINCIPAL = "11111111-1111-4111-8111-111111111111"
ROLE_ID = "22222222-2222-4222-8222-222222222222"
CONTEXT = {
    "AZURE_SUBSCRIPTION_ID": "33333333-3333-4333-8333-333333333333",
    "AZURE_AI_PROJECT_ID": (
        "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"
    ),
}


def test_identity_lookup_cannot_target_an_original_agent() -> None:
    project = MagicMock()
    project.agents.get.return_value.as_dict.return_value = {
        "name": access.SERVICE,
        "versions": {"latest": {"version": "1", "status": "active"}},
        "instance_identity": {"principal_id": PRINCIPAL},
    }
    assert access.runtime_identity(project) == access.RuntimeIdentity(PRINCIPAL, "1")
    project.agents.get.assert_called_once_with("story6-coordinator-agent")
    project.agents.get.return_value.as_dict.return_value["name"] = "supervisor-agent"
    with pytest.raises(ValueError, match="dedicated"):
        access.runtime_identity(project)


@pytest.mark.parametrize("present", [False, True])
def test_preview_never_assigns_and_existing_grants_are_reused(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    calls = []

    def cli(arguments):
        calls.append(arguments)
        if arguments[:3] == ["role", "definition", "list"]:
            return [{"roleName": "Foundry User", "roleType": "BuiltInRole", "name": ROLE_ID}]
        return [{"roleDefinitionId": "/roles/" + ROLE_ID}] if present else []

    monkeypatch.setattr(access, "az", cli)
    write = MagicMock(side_effect=AssertionError("No writes in preview."))
    monkeypatch.setattr(access, "create_grant", write)
    identity = access.RuntimeIdentity(PRINCIPAL, "1")
    grant = access.inspect_role(CONTEXT, identity)
    assert grant.present is present
    assert not access.apply_if_missing(CONTEXT, identity, grant, apply=False)
    assert all("create" not in call for call in calls)
    assert grant.scope.endswith("/accounts/acct") and "/projects/" not in grant.scope
    if present:
        assert not access.apply_if_missing(CONTEXT, identity, grant, apply=True)
    write.assert_not_called()


def test_apply_is_one_scoped_service_principal_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    write = MagicMock()
    monkeypatch.setattr(access, "create_grant", write)
    monkeypatch.setattr(access, "assignment_allowed", lambda scope, subscription: True)
    grant = Grant("Foundry User", CONTEXT["AZURE_AI_PROJECT_ID"].rsplit("/", 2)[0], ROLE_ID, False)
    identity = access.RuntimeIdentity(PRINCIPAL, "1")
    assert access.apply_if_missing(CONTEXT, identity, grant, apply=True)
    write.assert_called_once_with(grant, PRINCIPAL, CONTEXT["AZURE_SUBSCRIPTION_ID"], principal_type="ServicePrincipal")


def test_missing_assignment_authority_blocks_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    write = MagicMock()
    monkeypatch.setattr(access, "create_grant", write)
    monkeypatch.setattr(access, "assignment_allowed", lambda scope, subscription: False)
    with pytest.raises(ValueError, match="administrator"):
        access.apply_if_missing(
            CONTEXT,
            access.RuntimeIdentity(PRINCIPAL, "1"),
            Grant("Foundry User", "account-scope", ROLE_ID, False),
            apply=True,
        )
    write.assert_not_called()


def test_conditional_grants_are_not_silently_broadened(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            [{"roleName": "Foundry User", "roleType": "BuiltInRole", "name": ROLE_ID}],
            [{"roleDefinitionId": "/roles/" + ROLE_ID, "condition": "existing policy"}],
        ]
    )
    monkeypatch.setattr(access, "az", lambda arguments: next(responses))
    with pytest.raises(ValueError, match="conditional"):
        access.inspect_role(CONTEXT, access.RuntimeIdentity(PRINCIPAL, "1"))


def test_access_tasks_are_separate_from_deployment_and_preview_is_read_only() -> None:
    root = Path(__file__).resolve().parents[2]
    tasks = {item["label"]: item for item in json.loads((root / ".vscode/tasks.json").read_text())["tasks"]}
    preview = tasks["Access: Preview Story6 coordinator runtime permissions"]
    apply = tasks["Access: Apply Story6 coordinator runtime permissions"]
    assert "--apply" not in preview["args"] and "dependsOn" not in preview
    assert "--apply" in apply["args"]
    assert apply["dependsOn"] == ["Access: Preview Story6 coordinator runtime permissions"]
    assert tasks["Agents: Deploy Story6 coordinator"]["dependsOn"] == ["Agents: Verify Story6 deployment package"]
