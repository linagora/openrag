"""Unit tests for workspace_id validation logic.

Tests the regex rule directly to avoid importing the full router module
(which pulls in Ray, LangChain, and other heavy dependencies).
"""

import re

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

# Mirror of the rule in routers/workspaces.py — kept in sync manually.
_WORKSPACE_ID_RE = re.compile(r"[a-zA-Z0-9_-]+")


class CreateWorkspaceRequest(BaseModel):
    """Minimal copy of CreateWorkspaceRequest for isolated unit testing."""

    model_config = ConfigDict(extra="allow")

    workspace_id: str
    display_name: str | None = None

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, v: str) -> str:
        if not v or not _WORKSPACE_ID_RE.fullmatch(v):
            raise ValueError(
                "workspace_id must be non-empty and contain only alphanumeric characters, hyphens, or underscores"
            )
        return v


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
