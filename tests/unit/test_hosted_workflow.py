"""Offline checks for exact hosted evidence expectations and least-privilege grants."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from app.services.delegation import Report
from app.services.workflows import Child

from scripts.common.reporting import ValidationRun
from scripts.deploy import grant_agent_rbac
from scripts.run_scenario.hosted_workflow import conversation_table, verify_idle_timeout, verify_relationships


@pytest.mark.parametrize("timeout", [120, 1200, 120.0, None])
def test_idle_policy_checks_actual_raw_version_field(timeout: object) -> None:
    def get_version(service: str, version: str, *, raw_response_hook: object) -> None:
        assert service == "research-agent" and version == "3"
        raw_response_hook(
            SimpleNamespace(
                http_response=SimpleNamespace(
                    json=lambda: {"definition": {"session_configuration": {"idle_timeout_seconds": timeout}}}
                )
            )
        )

    project = SimpleNamespace(agents=SimpleNamespace(get_version=get_version))
    if type(timeout) is int and timeout == 120:
        assert verify_idle_timeout(project, "research-agent", "3") == 120
    else:
        with pytest.raises(ValueError):
            verify_idle_timeout(project, "research-agent", "3")


def reports() -> tuple[Report, Report]:
    first = Report(
        agent="supervisor",
        workflow_id=uuid4(),
        owner_id=uuid4(),
        session_id=uuid4(),
        conversation_id="root",
        platform_session_id="root-session",
        progress="ready",
        answer="ready",
        children={
            name: Child(
                agent=name,
                workflow_id=uuid4(),
                owner_id=uuid4(),
                session_id=uuid4(),
                conversation_id=name + "-conversation",
                platform_session_id=name + "-session",
                response_id=name + "-first-response",
            )
            for name in ("research", "data_analysis")
        },
    )
    continued = first.model_copy(
        update={
            "answer": "research: ALPHA-POC-7\n\ndata_analysis: ALPHA-POC-7",
            "children": {
                name: child.model_copy(update={"response_id": name + "-second-response"})
                for name, child in first.children.items()
            },
        }
    )
    return first, continued


def test_real_evidence_requires_exact_ids_distinct_conversations_and_history() -> None:
    first, continued = reports()
    verify_relationships(first, continued)
    assert len(conversation_table(first).rows) == 3
    for field, value in (
        ("owner_id", uuid4()),
        ("session_id", uuid4()),
        ("answer", "I think the token was ALPHA-POC-7"),
        ("platform_session_id", "different"),
    ):
        with pytest.raises(ValueError):
            verify_relationships(first, continued.model_copy(update={field: value}))
    children = deepcopy(continued.children)
    children["research"] = children["research"].model_copy(update={"conversation_id": first.conversation_id})
    with pytest.raises(ValueError):
        verify_relationships(first, continued.model_copy(update={"children": children}))
    with pytest.raises(ValueError):
        verify_relationships(first, first)


def test_delegation_preview_never_writes_and_apply_is_supervisor_project_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    account = f"/subscriptions/{uuid4()}/resourceGroups/poc/providers/Microsoft.CognitiveServices/accounts/poc"
    project = account + "/projects/poc"
    context = {
        "AZURE_AI_ACCOUNT_ID": account,
        "AZURE_AI_PROJECT_ID": project,
        "AZURE_SUBSCRIPTION_ID": account.split("/")[2],
    }
    role_id = str(uuid4())
    principal = str(uuid4())
    created_definition = {
        "roleType": "CustomRole",
        "name": role_id,
        "assignableScopes": [account],
        "permissions": [
            {
                "actions": [],
                "notActions": [],
                "dataActions": [grant_agent_rbac.IMPERSONATION_ACTION],
                "notDataActions": [],
                "condition": None,
                "conditionVersion": None,
            }
        ],
    }
    calls: list[list[str]] = []

    def az(arguments: list[str]) -> object:
        calls.append(arguments)
        if arguments[:3] == ["role", "definition", "create"]:
            return created_definition
        return []

    monkeypatch.setattr(grant_agent_rbac, "az", az)
    monkeypatch.setattr(grant_agent_rbac, "assignment_allowed", lambda *_args: True)
    create_grant = MagicMock()
    monkeypatch.setattr(grant_agent_rbac, "create_grant", create_grant)
    run = ValidationRun("fake-grant", "unit", tmp_path, no_color=True)
    grant_agent_rbac.grant_supervisor_delegation(context, principal, run, apply=False)
    assert not any(call[:3] == ["role", "definition", "create"] for call in calls)
    create_grant.assert_not_called()
    grant_agent_rbac.grant_supervisor_delegation(context, principal, run, apply=True)
    grant = create_grant.call_args.args[0]
    assert grant.scope == project
    assert grant.definition_id == role_id
    assert create_grant.call_args.args[1] == principal
    assert create_grant.call_args.kwargs == {"principal_type": "ServicePrincipal"}


def test_delegation_refuses_existing_broader_role(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    account = "/subscriptions/sub/resourceGroups/poc/providers/Microsoft.CognitiveServices/accounts/poc"
    monkeypatch.setattr(grant_agent_rbac, "az", lambda _args: [{"roleType": "CustomRole", "permissions": []}])
    create = MagicMock()
    monkeypatch.setattr(grant_agent_rbac, "create_grant", create)
    run = ValidationRun("fake-grant", "unit", tmp_path, no_color=True)
    with pytest.raises(ValueError, match="unexpected permissions"):
        grant_agent_rbac.grant_supervisor_delegation(
            {
                "AZURE_AI_ACCOUNT_ID": account,
                "AZURE_AI_PROJECT_ID": account + "/projects/poc",
                "AZURE_SUBSCRIPTION_ID": "sub",
            },
            str(uuid4()),
            run,
            apply=False,
        )
    create.assert_not_called()
