"""Workspace request schemas.

Pulled out of ``api/routers/admin/workspaces.py`` so the request DTOs
live alongside the rest of the Phase 10E schema package. The router
imports these symbols rather than redefining them.
"""

import re

from pydantic import BaseModel, ConfigDict, field_validator

WORKSPACE_ID_RE = re.compile(r"[a-zA-Z0-9_-]+")


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    workspace_id: str
    display_name: str | None = None

    @field_validator("workspace_id")
    @classmethod
    def validate_workspace_id(cls, v: str) -> str:
        if not v or not WORKSPACE_ID_RE.fullmatch(v):
            raise ValueError(
                "workspace_id must be non-empty and contain only alphanumeric characters, hyphens, or underscores"
            )
        return v


class AddFilesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    file_ids: list[str]


__all__ = ["AddFilesRequest", "CreateWorkspaceRequest", "WORKSPACE_ID_RE"]
