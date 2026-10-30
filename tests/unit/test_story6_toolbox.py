"""Isolated toolbox contract tests; no Azure writes or ordinary MCP wrapper."""

from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import ResourceNotFoundError

from scripts.deploy import story6_toolbox as toolbox


def version(number="1"):
    return {"name": toolbox.TOOLBOX_NAME, "version": number, "tools": [toolbox.source_tool().as_dict()]}


def test_preview_does_not_create_and_apply_creates_only_the_fixed_name() -> None:
    project = MagicMock()
    project.toolboxes.list_versions.side_effect = ResourceNotFoundError("Synthetic missing toolbox.")
    project.toolboxes.create_version.return_value.as_dict.return_value = version()
    assert toolbox.ensure(project, apply=False) == (None, False)
    project.toolboxes.create_version.assert_not_called()
    assert toolbox.ensure(project, apply=True) == ("1", True)
    call = project.toolboxes.create_version.call_args
    assert call.args == ("story6-approval-mcp",)
    assert call.kwargs["tools"][0].as_dict()["require_approval"] == "always"
    assert call.kwargs["tools"][0].as_dict()["allowed_tools"] == ["microsoft_docs_search"]


def test_existing_exact_version_is_reused_and_conflicts_are_not_overwritten() -> None:
    project = MagicMock()
    stored = MagicMock()
    stored.as_dict.return_value = version()
    project.toolboxes.list_versions.return_value = [stored]
    assert toolbox.ensure(project, apply=True) == ("1", False)
    project.toolboxes.create_version.assert_not_called()
    mismatched = version()
    mismatched["tools"][0]["require_approval"] = "never"
    stored.as_dict.return_value = mismatched
    with pytest.raises(ValueError, match="differs"):
        toolbox.ensure(project, apply=True)
    project.toolboxes.create_version.assert_not_called()
