"""Unit tests for workspace_id validation logic.

Imports ``CreateWorkspaceRequest`` from ``api.schemas.admin.workspaces``
directly. That schema module is dependency-light (only ``pydantic``) so
this no longer pulls in the heavy router module (Ray, LangChain, etc.).
"""

import pytest
from api.schemas.admin.workspaces import CreateWorkspaceRequest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Valid workspace_id values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workspace_id",
    [
        "myworkspace",
        "my-workspace",
        "my_workspace",
        "ws123",
        "WS-ABC_01",
        "a",  # single character
        "A1-b2_C3",
    ],
)
def test_valid_workspace_ids(workspace_id):
    req = CreateWorkspaceRequest(workspace_id=workspace_id)
    assert req.workspace_id == workspace_id


# ---------------------------------------------------------------------------
# Invalid workspace_id values — should raise ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workspace_id",
    [
        "ws/slash",  # slash — would break URL routing
        "ws slash",  # space
        "ws.dot",  # dot
        "ws@at",  # at-sign
        "ws#hash",  # hash
        "",  # empty string
        "ws/deep/path",  # multiple slashes
        "ws\nnewline",  # newline
    ],
)
def test_invalid_workspace_ids(workspace_id):
    with pytest.raises(ValidationError):
        CreateWorkspaceRequest(workspace_id=workspace_id)


def test_display_name_is_optional():
    req = CreateWorkspaceRequest(workspace_id="valid-id")
    assert req.display_name is None


def test_display_name_can_contain_any_chars():
    req = CreateWorkspaceRequest(workspace_id="valid-id", display_name="My Workspace / Team")
    assert req.display_name == "My Workspace / Team"
