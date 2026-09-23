"""HTTP-level tests for the workspace admin router.

Route-level concerns only — request parsing, what reaches the service and
what comes back in the response body. The orphan-cleanup decision itself
lives in :class:`services.orchestrators.workspace_service.WorkspaceService`
(see ``tests/unit/services/orchestrators/test_workspace_service.py``), and
``tests/unit/api/routers/admin/test_admin_workspaces.py`` covers the
request schemas without importing this router.
"""

from __future__ import annotations

import pytest
from api.dependencies.auth import require_partition_owner
from api.routers.admin import workspaces
from di.providers import get_workspace_service
from fastapi import FastAPI


class _FakeWorkspaceService:
    """Records the delete call and echoes a fixed result."""

    def __init__(self, *, orphaned: list[str] | None = None, partition: str = "p1") -> None:
        self._orphaned = orphaned if orphaned is not None else []
        self._partition = partition
        self.delete_calls: list[tuple[str, str, bool]] = []
        self.get_calls: list[tuple[str, str]] = []

    async def get_workspace(self, partition: str, workspace_id: str) -> dict | None:
        self.get_calls.append((partition, workspace_id))
        if partition != self._partition:
            return None
        return {"workspace_id": workspace_id, "partition_name": partition}

    async def delete_workspace(self, partition: str, workspace_id: str, keep_files: bool = False) -> dict:
        self.delete_calls.append((partition, workspace_id, keep_files))
        if keep_files:
            return {
                "orphaned_files_deleted": 0,
                "orphaned_files_failed": [],
                "kept_files": len(self._orphaned),
            }
        return {
            "orphaned_files_deleted": len(self._orphaned),
            "orphaned_files_failed": [],
            "kept_files": 0,
        }


def _build_app(service: _FakeWorkspaceService) -> FastAPI:
    app = FastAPI()
    app.include_router(workspaces.router)
    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[require_partition_owner] = lambda: {"id": 1, "is_admin": True}
    return app


pytestmark = pytest.mark.asyncio


class TestDeleteWorkspace:
    async def test_default_purges_orphans(self, async_client_factory):
        service = _FakeWorkspaceService(orphaned=["file-a", "file-b"])
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete("/partition/p1/workspaces/ws1")

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "deleted",
            "orphaned_files_deleted": 2,
            "orphaned_files_failed": [],
            "kept_files": 0,
        }
        assert service.delete_calls == [("p1", "ws1", False)]

    async def test_keep_files_true_is_forwarded(self, async_client_factory):
        service = _FakeWorkspaceService(orphaned=["file-a"])
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete("/partition/p1/workspaces/ws1", params={"keep_files": "true"})

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "deleted",
            "orphaned_files_deleted": 0,
            "orphaned_files_failed": [],
            "kept_files": 1,
        }
        assert service.delete_calls == [("p1", "ws1", True)]

    async def test_same_id_in_another_partition_is_not_found(self, async_client_factory):
        # ``ws1`` exists in p1 only. workspace_id is unique per partition, so
        # the lookup must be keyed on the path's partition and never find p1's.
        service = _FakeWorkspaceService(orphaned=["file-a"], partition="p1")
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete("/partition/p2/workspaces/ws1")

        assert resp.status_code == 404
        assert resp.json() == {"detail": "Workspace not found"}
        assert service.get_calls == [("p2", "ws1")]
        assert service.delete_calls == []
