"""Unit tests for :class:`WorkspaceService` (Phase 8B.2)."""

from __future__ import annotations

import pytest
from services.orchestrators.workspace_service import WorkspaceService


class FakeWorkspaceRepo:
    def __init__(self, *, workspace=None, orphaned=None):
        self._workspace = workspace
        self._orphaned = orphaned if orphaned is not None else []
        self.created: list[tuple] = []
        self.added: list[tuple[str, list[str]]] = []
        self.removed: list[tuple[str, str]] = []
        self.removed_from_all: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    async def get_workspace_dict(self, workspace_id: str):
        return self._workspace

    async def list_workspaces_dict(self, partition: str) -> list[dict]:
        return [{"workspace_id": "w1", "partition_name": partition}]

    async def create_workspace_legacy(self, workspace_id, partition, user_id, display_name):
        self.created.append((workspace_id, partition, user_id, display_name))

    async def get_existing_file_ids(self, partition: str, file_ids):
        return [f for f in file_ids if f != "ghost"]

    async def add_files_to_workspace(self, workspace_id: str, file_ids):
        self.added.append((workspace_id, file_ids))
        return []

    async def remove_file_from_workspace(self, workspace_id: str, file_id: str) -> bool:
        self.removed.append((workspace_id, file_id))
        return True

    async def list_workspace_files(self, workspace_id: str) -> list[str]:
        return ["f1", "f2"]

    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        return ["w1", "w2"]

    async def delete_workspace(self, workspace_id: str) -> list[str]:
        self.deleted.append(workspace_id)
        return list(self._orphaned)

    async def remove_file_from_all_workspaces(self, file_id: str, partition: str) -> None:
        self.removed_from_all.append((file_id, partition))


class FakeDocumentRepo:
    def __init__(self, *, fail_on: set[str] | None = None):
        self._fail_on = fail_on or set()
        self.removed: list[tuple[str, str]] = []

    async def remove_file_from_partition(self, file_id: str, partition: str) -> bool:
        if file_id in self._fail_on:
            raise RuntimeError(f"boom:{file_id}")
        self.removed.append((file_id, partition))
        return True


class FakeVectorStore:
    def __init__(self, ids_by_file=None):
        self._ids_by_file = ids_by_file or {}
        self.deleted: list[list[str]] = []

    async def query_ids_by_filter(self, collection, filters):
        return list(self._ids_by_file.get(filters.get("file_id"), []))

    async def delete(self, ids, collection="default") -> int:
        self.deleted.append(list(ids))
        return len(ids)


def _svc(*, wrepo=None, drepo=None, vstore=None, collection="vdb") -> WorkspaceService:
    return WorkspaceService(
        workspace_repo=wrepo or FakeWorkspaceRepo(),
        document_repo=drepo or FakeDocumentRepo(),
        vector_store=vstore or FakeVectorStore(),
        collection=collection,
    )


# --------------------------------------------------------------------------- #
# delegations
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_workspace_delegates():
    wrepo = FakeWorkspaceRepo()
    await _svc(wrepo=wrepo).create_workspace("w1", "p", 5, "Disp")
    assert wrepo.created == [("w1", "p", 5, "Disp")]


@pytest.mark.asyncio
async def test_get_existing_file_ids_filters():
    out = await _svc().get_existing_file_ids("p", ["a", "ghost", "b"])
    assert set(out) == {"a", "b"}


@pytest.mark.asyncio
async def test_remove_file_and_list_and_workspaces():
    wrepo = FakeWorkspaceRepo()
    svc = _svc(wrepo=wrepo)
    assert await svc.remove_file("w1", "f1") is True
    assert await svc.list_files("w1") == ["f1", "f2"]
    assert await svc.get_file_workspaces("f1", "p") == ["w1", "w2"]


# --------------------------------------------------------------------------- #
# cross-cutting delete_workspace
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_workspace_no_orphans():
    wrepo = FakeWorkspaceRepo(orphaned=[])
    drepo = FakeDocumentRepo()
    vstore = FakeVectorStore()
    out = await _svc(wrepo=wrepo, drepo=drepo, vstore=vstore).delete_workspace("p", "w1")
    assert out == {"orphaned_files_deleted": 0, "orphaned_files_failed": []}
    assert wrepo.deleted == ["w1"]
    assert vstore.deleted == []
    assert drepo.removed == []


@pytest.mark.asyncio
async def test_delete_workspace_cleans_orphans_vectors_and_rows():
    wrepo = FakeWorkspaceRepo(orphaned=["fA", "fB"])
    drepo = FakeDocumentRepo()
    vstore = FakeVectorStore(ids_by_file={"fA": ["c1", "c2"], "fB": []})
    out = await _svc(wrepo=wrepo, drepo=drepo, vstore=vstore).delete_workspace("p", "w1")

    assert out == {"orphaned_files_deleted": 2, "orphaned_files_failed": []}
    # fA had chunks -> a delete call; fB had none -> no delete call.
    assert vstore.deleted == [["c1", "c2"]]
    assert set(drepo.removed) == {("fA", "p"), ("fB", "p")}
    assert set(wrepo.removed_from_all) == {("fA", "p"), ("fB", "p")}


@pytest.mark.asyncio
async def test_delete_workspace_collects_per_file_failures():
    wrepo = FakeWorkspaceRepo(orphaned=["good", "bad"])
    drepo = FakeDocumentRepo(fail_on={"bad"})
    vstore = FakeVectorStore(ids_by_file={"good": ["c1"], "bad": ["c2"]})
    out = await _svc(wrepo=wrepo, drepo=drepo, vstore=vstore).delete_workspace("p", "w1")

    assert out["orphaned_files_deleted"] == 1
    assert out["orphaned_files_failed"] == ["bad"]
