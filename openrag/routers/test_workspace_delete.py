"""Unit tests for the `keep_files` option on DELETE workspace.

`routers.workspaces` transitively imports `utils.dependencies`, which spins
up Ray actors at import time (indexer, marker pool, semaphores, …). To avoid
that in a unit-test context, we stub `utils.dependencies` in `sys.modules`
*before* importing the router, then drive the real `delete_workspace`
handler end-to-end through FastAPI's `TestClient`.

See `routers/test_auth_router.py` for the same pattern applied to the auth
router.
"""

from __future__ import annotations

import sys
import types

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _RayMethodStub:
    """Mimics a Ray actor method: ``method.remote(...)`` returns an awaitable."""

    def __init__(self, name: str, fn, call_log: list):
        self._name = name
        self._fn = fn
        self._call_log = call_log

    async def remote(self, *args, **kwargs):
        self._call_log.append((self._name, args, kwargs))
        return self._fn(*args, **kwargs)


class _StubVectorDB:
    """Minimal Ray-actor stand-in for the workspace-deletion tests."""

    def __init__(self, orphaned_files: list[str] | None = None, failing_files: set[str] | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._orphaned_files = list(orphaned_files or [])
        self._failing_files = set(failing_files or [])
        self.get_workspace = _RayMethodStub("get_workspace", self._impl_get_workspace, self.calls)
        self.delete_workspace = _RayMethodStub("delete_workspace", self._impl_delete_workspace, self.calls)
        self.delete_file = _RayMethodStub("delete_file", self._impl_delete_file, self.calls)

    def _impl_get_workspace(self, workspace_id: str):
        return {"workspace_id": workspace_id, "partition_name": "p1"}

    def _impl_delete_workspace(self, workspace_id: str):
        return self._orphaned_files

    def _impl_delete_file(self, file_id: str, partition: str):
        if file_id in self._failing_files:
            raise RuntimeError(f"boom-{file_id}")
        return None


def _install_dependencies_stub():
    """Replace `utils.dependencies` with a stub so importing the router doesn't touch Ray."""
    stub = types.ModuleType("utils.dependencies")
    stub.get_vectordb = lambda: None  # overridden per-test via dependency_overrides
    stub.get_task_state_manager = lambda: None
    stub.get_serializer = lambda: None
    stub.get_indexer = lambda: None
    stub.get_marker_pool = lambda: None
    sys.modules["utils.dependencies"] = stub


_install_dependencies_stub()

import importlib  # noqa: E402

# Import the router module, forcing a fresh import so it picks up the stub above.
sys.modules.pop("routers.workspaces", None)
workspaces = importlib.import_module("routers.workspaces")


def _make_client(vectordb: _StubVectorDB) -> TestClient:
    app = FastAPI()
    app.include_router(workspaces.router)
    app.dependency_overrides[workspaces.get_vectordb] = lambda: vectordb
    app.dependency_overrides[workspaces.require_partition_owner] = lambda: {"id": 1, "is_admin": True}
    return TestClient(app)


class TestDeleteWorkspaceKeepFiles:
    def test_default_deletes_orphaned_files(self):
        """Byte-compatible default behavior: orphans are purged, kept_files is 0."""
        vectordb = _StubVectorDB(orphaned_files=["file-a", "file-b"])
        client = _make_client(vectordb)

        resp = client.delete("/partition/p1/workspaces/ws1")

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "deleted",
            "orphaned_files_deleted": 2,
            "orphaned_files_failed": [],
            "kept_files": 0,
        }
        delete_file_calls = [c for c in vectordb.calls if c[0] == "delete_file"]
        assert len(delete_file_calls) == 2

    def test_default_reports_failed_orphan_deletions(self):
        vectordb = _StubVectorDB(orphaned_files=["file-a", "file-b"], failing_files={"file-b"})
        client = _make_client(vectordb)

        resp = client.delete("/partition/p1/workspaces/ws1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["orphaned_files_deleted"] == 1
        assert body["orphaned_files_failed"] == ["file-b"]
        assert body["kept_files"] == 0

    def test_no_orphans_kept_files_zero(self):
        vectordb = _StubVectorDB(orphaned_files=[])
        client = _make_client(vectordb)

        resp = client.delete("/partition/p1/workspaces/ws1")

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "deleted",
            "orphaned_files_deleted": 0,
            "orphaned_files_failed": [],
            "kept_files": 0,
        }

    def test_keep_files_true_skips_deletion(self):
        vectordb = _StubVectorDB(orphaned_files=["file-a"])
        client = _make_client(vectordb)

        resp = client.delete("/partition/p1/workspaces/ws1", params={"keep_files": "true"})

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "deleted",
            "orphaned_files_deleted": 0,
            "orphaned_files_failed": [],
            "kept_files": 1,
        }
        assert not any(c[0] == "delete_file" for c in vectordb.calls)

    def test_keep_files_false_matches_default(self):
        vectordb = _StubVectorDB(orphaned_files=["file-a"])
        client = _make_client(vectordb)

        resp = client.delete("/partition/p1/workspaces/ws1", params={"keep_files": "false"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["kept_files"] == 0
        assert body["orphaned_files_deleted"] == 1

    def test_keep_files_true_with_no_orphans(self):
        vectordb = _StubVectorDB(orphaned_files=[])
        client = _make_client(vectordb)

        resp = client.delete("/partition/p1/workspaces/ws1", params={"keep_files": "true"})

        assert resp.status_code == 200
        assert resp.json() == {
            "status": "deleted",
            "orphaned_files_deleted": 0,
            "orphaned_files_failed": [],
            "kept_files": 0,
        }
