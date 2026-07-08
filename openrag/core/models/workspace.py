"""Workspace domain model.

A workspace is a named subset of files within a partition, used to scope
search and chat to a curated document set. Workspaces share a partition's
files (no copy) and a single file may belong to many workspaces.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Workspace(BaseModel):
    """A named subset of files within a partition."""

    workspace_id: str
    partition: str
    display_name: str | None = None
    created_by: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = ["Workspace"]
