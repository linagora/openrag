"""HTTP-level tests for the workspace admin router.

Route-level concerns only — request parsing, what reaches the service and
what comes back in the response body. The orphan-cleanup decision itself
lives in :class:`services.orchestrators.workspace_service.WorkspaceService`
(see ``tests/unit/services/orchestrators/test_workspace_service.py``), and
``tests/unit/api/routers/admin/test_admin_workspaces.py`` covers the
request schemas without importing this router.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from api.dependencies.auth import require_partition_editor, require_partition_owner, require_partition_viewer
from api.routers.admin import workspaces
from di.providers import get_workspace_service
from fastapi import FastAPI


class _FakeWorkspaceService:
    """Records the delete call and echoes a fixed result."""

    def __init__(
        self,
        *,
        orphaned: list[str] | None = None,
        partition: str = "p1",
        changed: bool = False,
    ) -> None:
        self._orphaned = orphaned if orphaned is not None else []
        self._partition = partition
        self._changed = changed
        self.delete_calls: list[tuple[str, str, bool]] = []
        self.expected_created_at: datetime | None = None
        self.expected_created_at_is_null = False
        self.get_calls: list[tuple[str, str]] = []
        self.add_calls: list[tuple[str, str, list[str], datetime | None, bool]] = []
        self.add_result: list[str] | None = []
        self.remove_result: bool | None = True
        self.remove_calls: list[tuple[str, str, str, datetime | None, bool]] = []
        self.list_result: list[str] | None = ["f1"]
        self.list_calls: list[tuple[str, str, datetime | None, bool]] = []

    async def get_workspace(self, partition: str, workspace_id: str) -> dict | None:
        self.get_calls.append((partition, workspace_id))
        if partition != self._partition:
            return None
        return {"workspace_id": workspace_id, "partition_name": partition}

    async def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> list[str]:
        return list(file_ids)

    async def list_files(
        self,
        partition: str,
        workspace_id: str,
        *,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> list[str] | None:
        self.list_calls.append((partition, workspace_id, expected_created_at, expected_created_at_is_null))
        return self.list_result

    async def add_files(
        self,
        partition: str,
        workspace_id: str,
        file_ids: list[str],
        *,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> list[str] | None:
        self.add_calls.append((partition, workspace_id, file_ids, expected_created_at, expected_created_at_is_null))
        return self.add_result

    async def remove_file(
        self,
        partition: str,
        workspace_id: str,
        file_id: str,
        *,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> bool | None:
        self.remove_calls.append((partition, workspace_id, file_id, expected_created_at, expected_created_at_is_null))
        return self.remove_result

    async def delete_workspace(
        self,
        partition: str,
        workspace_id: str,
        keep_files: bool = False,
        *,
        expected_created_at: datetime | None = None,
        expected_created_at_is_null: bool = False,
    ) -> dict | None:
        self.delete_calls.append((partition, workspace_id, keep_files))
        self.expected_created_at = expected_created_at
        self.expected_created_at_is_null = expected_created_at_is_null
        if self._changed:
            return None
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

    async def get_service():
        return service

    async def partition_manager():
        return {"id": 1, "is_admin": True}

    app.dependency_overrides[get_workspace_service] = get_service
    app.dependency_overrides[require_partition_owner] = partition_manager
    app.dependency_overrides[require_partition_editor] = partition_manager
    app.dependency_overrides[require_partition_viewer] = partition_manager
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
        assert service.expected_created_at is None

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

    async def test_stale_confirmation_returns_conflict(self, async_client_factory):
        service = _FakeWorkspaceService(changed=True)
        expected_created_at = datetime(2025, 1, 10, 9, 0)
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete(
                "/partition/p1/workspaces/ws1",
                params={"expected_created_at": expected_created_at.isoformat()},
            )

        assert resp.status_code == 409
        assert "confirm deletion again" in resp.json()["detail"]
        assert service.expected_created_at == expected_created_at

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

    async def test_offset_bearing_confirmation_is_rejected_before_the_repository(self, async_client_factory):
        # ``workspaces.created_at`` is a timezone-naive timestamp: an
        # offset-bearing value must 422 here rather than fail the asyncpg bind
        # with a 500.
        service = _FakeWorkspaceService()
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete(
                "/partition/p1/workspaces/ws1",
                params={"expected_created_at": "2025-01-10T09:00:00+00:00"},
            )

        assert resp.status_code == 422
        assert resp.json() == {"detail": "expected_created_at must be a naive timestamp without a UTC offset"}
        assert service.delete_calls == []

    async def test_naive_confirmation_round_trips_to_the_service(self, async_client_factory):
        service = _FakeWorkspaceService(changed=True)
        expected_created_at = datetime(2025, 1, 10, 9, 0)
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete(
                "/partition/p1/workspaces/ws1",
                params={"expected_created_at": "2025-01-10T09:00:00"},
            )

        assert resp.status_code == 409
        assert service.expected_created_at == expected_created_at

    async def test_null_timestamp_confirmation_round_trips_to_the_service(self, async_client_factory):
        service = _FakeWorkspaceService(changed=True)
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete(
                "/partition/p1/workspaces/ws1",
                params={"expected_created_at_is_null": "true"},
            )

        assert resp.status_code == 409
        assert service.expected_created_at_is_null is True


class TestAddFilesToWorkspace:
    async def test_add_forwards_expected_created_at_to_the_service(self, async_client_factory):
        service = _FakeWorkspaceService()
        expected_created_at = datetime(2025, 1, 10, 9, 0)
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.post(
                "/partition/p1/workspaces/ws1/files",
                json={"file_ids": ["f1", "f2"], "expected_created_at": "2025-01-10T09:00:00"},
            )

        assert resp.status_code == 200
        assert resp.json() == {"status": "added", "file_ids": ["f1", "f2"]}
        assert service.add_calls == [("p1", "ws1", ["f1", "f2"], expected_created_at, False)]

    async def test_add_without_confirmation_sends_none(self, async_client_factory):
        service = _FakeWorkspaceService()
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.post(
                "/partition/p1/workspaces/ws1/files",
                json={"file_ids": ["f1"]},
            )

        assert resp.status_code == 200
        assert service.add_calls == [("p1", "ws1", ["f1"], None, False)]

    async def test_null_timestamp_confirmation_is_forwarded(self, async_client_factory):
        service = _FakeWorkspaceService()
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.post(
                "/partition/p1/workspaces/ws1/files",
                json={"file_ids": ["f1"], "expected_created_at_is_null": True},
            )

        assert resp.status_code == 200
        assert service.add_calls == [("p1", "ws1", ["f1"], None, True)]

    async def test_stale_add_confirmation_returns_conflict(self, async_client_factory):
        service = _FakeWorkspaceService()
        service.add_result = None
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.post(
                "/partition/p1/workspaces/ws1/files",
                json={"file_ids": ["f1"], "expected_created_at": "2025-01-10T09:00:00"},
            )

        assert resp.status_code == 409
        assert "changed after it was loaded" in resp.json()["detail"]

    async def test_offset_bearing_add_confirmation_is_rejected_before_the_repository(self, async_client_factory):
        service = _FakeWorkspaceService()
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.post(
                "/partition/p1/workspaces/ws1/files",
                json={"file_ids": ["f1"], "expected_created_at": "2025-01-10T09:00:00+00:00"},
            )

        assert resp.status_code == 422
        assert resp.json() == {"detail": "expected_created_at must be a naive timestamp without a UTC offset"}
        assert service.add_calls == []


class TestRemoveFileFromWorkspace:
    async def test_stale_removal_confirmation_returns_conflict(self, async_client_factory):
        service = _FakeWorkspaceService()
        service.remove_result = None
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.delete(
                "/partition/p1/workspaces/ws1/files/f1",
                params={"expected_created_at_is_null": "true"},
            )

        assert resp.status_code == 409
        assert service.remove_calls == [("p1", "ws1", "f1", None, True)]


class TestListWorkspaceFiles:
    async def test_forwards_expected_generation(self, async_client_factory):
        service = _FakeWorkspaceService()
        expected_created_at = datetime(2025, 1, 10, 9, 0)
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.get(
                "/partition/p1/workspaces/ws1/files",
                params={"expected_created_at": expected_created_at.isoformat()},
            )

        assert resp.status_code == 200
        assert resp.json() == {"file_ids": ["f1"]}
        assert service.list_calls == [("p1", "ws1", expected_created_at, False)]

    async def test_null_generation_forwards_explicit_null_check(self, async_client_factory):
        service = _FakeWorkspaceService()
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.get(
                "/partition/p1/workspaces/ws1/files",
                params={"expected_created_at_is_null": "true"},
            )

        assert resp.status_code == 200
        assert service.list_calls == [("p1", "ws1", None, True)]

    async def test_stale_generation_returns_conflict(self, async_client_factory):
        service = _FakeWorkspaceService()
        service.list_result = None
        async with async_client_factory(_build_app(service)) as client:
            resp = await client.get(
                "/partition/p1/workspaces/ws1/files",
                params={"expected_created_at": "2025-01-10T09:00:00"},
            )

        assert resp.status_code == 409
        assert "Refresh and retry" in resp.json()["detail"]
