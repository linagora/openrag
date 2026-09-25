"""Unit tests for :class:`WorkspaceService` (Phase 8B.2)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from core.models.workspace import Workspace
from core.utils.exceptions import AmbiguousWorkspaceError
from services.orchestrators.workspace_service import WorkspaceService


class FakeWorkspaceRepo:
    @asynccontextmanager
    async def cleanup_session(self, file_id, partition):
        yield self

    def __init__(self, *, workspaces=None, orphaned=None, files=None):
        # ``workspaces``: every workspace the fake knows about, across partitions.
        self._workspaces: list[Workspace] = list(workspaces or [])
        self._orphaned = orphaned if orphaned is not None else []
        self._files = files if files is not None else ["f1", "f2"]
        self.created: list[tuple] = []
        self.added: list[tuple[str, str, list[str]]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.listed: list[tuple[str, str]] = []
        self.find_calls: list[tuple[str, list[str] | None]] = []
        self.removed_from_all: list[tuple[str, str]] = []
        self.finalized: list[tuple[str, str]] = []
        self.cleanup_started: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.retry_claimed: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str, bool]] = []

    async def get_workspace_dict(self, partition: str, workspace_id: str):
        for ws in self._workspaces:
            if ws.partition == partition and ws.workspace_id == workspace_id:
                return {"workspace_id": ws.workspace_id, "partition_name": ws.partition}
        return None

    async def find_workspaces(self, workspace_id: str, partitions: list[str] | None) -> list[Workspace]:
        self.find_calls.append((workspace_id, partitions))
        return [
            ws
            for ws in self._workspaces
            if ws.workspace_id == workspace_id and (partitions is None or ws.partition in partitions)
        ]

    async def list_workspaces_dict(self, partition: str) -> list[dict]:
        return [{"workspace_id": "w1", "partition_name": partition}]

    async def create_workspace_legacy(self, workspace_id, partition, user_id, display_name):
        self.created.append((workspace_id, partition, user_id, display_name))

    async def get_existing_file_ids(self, partition: str, file_ids):
        return [f for f in file_ids if f != "ghost"]

    async def add_files_to_workspace(self, partition: str, workspace_id: str, file_ids):
        self.added.append((partition, workspace_id, file_ids))
        return []

    async def remove_file_from_workspace(self, partition: str, workspace_id: str, file_id: str) -> bool:
        self.removed.append((partition, workspace_id, file_id))
        return True

    async def list_workspace_files(self, partition: str, workspace_id: str) -> list[str]:
        self.listed.append((partition, workspace_id))
        return list(self._files)

    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        return ["w1", "w2"]

    async def delete_workspace(self, partition: str, workspace_id: str, *, keep_files: bool = False) -> list[str]:
        self.deleted.append((partition, workspace_id, keep_files))
        return list(self._orphaned)

    async def remove_file_from_all_workspaces(self, file_id: str, partition: str) -> None:
        self.removed_from_all.append((file_id, partition))

    async def finalize_claimed_file_cleanup(self, file_id: str, partition: str) -> bool:
        self.finalized.append((file_id, partition))
        return True

    async def start_claimed_file_cleanup(self, file_id: str, partition: str) -> bool:
        self.cleanup_started.append((file_id, partition))
        return True

    async def release_claimed_file_cleanup(self, file_id: str, partition: str) -> None:
        self.released.append((file_id, partition))

    async def mark_cleanup_failed(self, file_id: str, partition: str) -> bool:
        self.failed.append((file_id, partition))
        return True

    async def claim_failed_file_cleanup(self, file_id: str, partition: str) -> bool:
        self.retry_claimed.append((file_id, partition))
        return True


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
    def __init__(self, ids_by_file=None, *, fail_on: set[str] | None = None):
        self._ids_by_file = ids_by_file or {}
        self._fail_on = fail_on or set()
        self.deleted: list[list[str]] = []

    async def query_ids_by_filter(self, collection, filters):
        return list(self._ids_by_file.get(filters.get("file_id"), []))

    async def delete(self, ids, collection="default") -> int:
        if any(failed_file_id in chunk_id for failed_file_id in self._fail_on for chunk_id in ids):
            raise RuntimeError("vector cleanup failed")
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
async def test_get_workspace_is_keyed_on_partition_and_id():
    wrepo = FakeWorkspaceRepo(workspaces=[Workspace(workspace_id="w1", partition="p1")])
    svc = _svc(wrepo=wrepo)
    assert await svc.get_workspace("p1", "w1") == {"workspace_id": "w1", "partition_name": "p1"}
    # Same id, other partition: a different (here non-existent) workspace.
    assert await svc.get_workspace("p2", "w1") is None


@pytest.mark.asyncio
async def test_remove_file_and_list_and_workspaces():
    wrepo = FakeWorkspaceRepo()
    svc = _svc(wrepo=wrepo)
    assert await svc.remove_file("p", "w1", "f1") is True
    assert await svc.list_files("p", "w1") == ["f1", "f2"]
    assert await svc.get_file_workspaces("f1", "p") == ["w1", "w2"]
    assert wrepo.removed == [("p", "w1", "f1")]
    assert wrepo.listed == [("p", "w1")]


@pytest.mark.asyncio
async def test_add_files_forwards_partition():
    wrepo = FakeWorkspaceRepo()
    assert await _svc(wrepo=wrepo).add_files("p", "w1", ["f1"]) == []
    assert wrepo.added == [("p", "w1", ["f1"])]


# --------------------------------------------------------------------------- #
# cross-cutting delete_workspace
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_workspace_no_orphans():
    wrepo = FakeWorkspaceRepo(orphaned=[])
    drepo = FakeDocumentRepo()
    vstore = FakeVectorStore()
    out = await _svc(wrepo=wrepo, drepo=drepo, vstore=vstore).delete_workspace("p", "w1")
    assert out == {"orphaned_files_deleted": 0, "orphaned_files_failed": [], "kept_files": 0}
    assert wrepo.deleted == [("p", "w1", False)]
    assert vstore.deleted == []
    assert drepo.removed == []


@pytest.mark.asyncio
async def test_delete_workspace_cleans_orphans_vectors_and_rows():
    wrepo = FakeWorkspaceRepo(orphaned=["fA", "fB"])
    drepo = FakeDocumentRepo()
    vstore = FakeVectorStore(ids_by_file={"fA": ["c1", "c2"], "fB": []})
    out = await _svc(wrepo=wrepo, drepo=drepo, vstore=vstore).delete_workspace("p", "w1")

    assert out == {"orphaned_files_deleted": 2, "orphaned_files_failed": [], "kept_files": 0}
    # fA had chunks -> a delete call; fB had none -> no delete call.
    assert vstore.deleted == [["c1", "c2"]]
    assert drepo.removed == []
    assert set(wrepo.finalized) == {("fA", "p"), ("fB", "p")}
    assert set(wrepo.cleanup_started) == {("fA", "p"), ("fB", "p")}


@pytest.mark.asyncio
async def test_delete_workspace_collects_per_file_failures():
    wrepo = FakeWorkspaceRepo(orphaned=["good", "bad"])
    drepo = FakeDocumentRepo()
    vstore = FakeVectorStore(ids_by_file={"good": ["good-c1"], "bad": ["bad-c2"]}, fail_on={"bad"})
    out = await _svc(wrepo=wrepo, drepo=drepo, vstore=vstore).delete_workspace("p", "w1")

    assert out["orphaned_files_deleted"] == 1
    assert out["orphaned_files_failed"] == ["bad"]
    assert out["kept_files"] == 0
    # The vector deletion started, so releasing the claim could make the
    # catalog row attachable even though its vectors may already be gone.
    assert wrepo.released == []
    assert wrepo.failed == [("bad", "p")]


@pytest.mark.asyncio
async def test_delete_workspace_releases_claim_when_start_fails():
    class StartFailureRepo(FakeWorkspaceRepo):
        async def start_claimed_file_cleanup(self, file_id: str, partition: str) -> bool:
            return False

    wrepo = StartFailureRepo(orphaned=["bad"])
    vstore = FakeVectorStore(ids_by_file={"bad": ["bad-c1"]})
    result = await _svc(wrepo=wrepo, vstore=vstore).delete_workspace("p", "w1")
    assert result["orphaned_files_failed"] == ["bad"]
    assert wrepo.released == [("bad", "p")]
    assert wrepo.failed == []


@pytest.mark.asyncio
async def test_retry_failed_file_cleanup_reclaims_and_finalizes():
    wrepo = FakeWorkspaceRepo()
    vstore = FakeVectorStore(ids_by_file={"bad": ["bad-c1"]})
    svc = _svc(wrepo=wrepo, vstore=vstore)
    assert await svc.retry_failed_file_cleanup("bad", "p") is True
    assert wrepo.retry_claimed == [("bad", "p")]
    assert wrepo.cleanup_started == []
    assert wrepo.finalized == [("bad", "p")]


@pytest.mark.asyncio
async def test_retry_failed_file_cleanup_returns_false_when_not_claimed():
    class NoRetryRepo(FakeWorkspaceRepo):
        async def claim_failed_file_cleanup(self, file_id: str, partition: str) -> bool:
            return False

    assert await _svc(wrepo=NoRetryRepo()).retry_failed_file_cleanup("bad", "p") is False


@pytest.mark.asyncio
async def test_delete_workspace_marks_claim_failed_when_finalization_fails():
    class FinalizeFailureRepo(FakeWorkspaceRepo):
        async def finalize_claimed_file_cleanup(self, file_id: str, partition: str) -> bool:
            raise RuntimeError("database unavailable")

    wrepo = FinalizeFailureRepo(orphaned=["bad"])
    vstore = FakeVectorStore(ids_by_file={"bad": ["bad-c1"]})
    result = await _svc(wrepo=wrepo, vstore=vstore).delete_workspace("p", "w1")
    assert result["orphaned_files_failed"] == ["bad"]
    assert wrepo.failed == [("bad", "p")]


@pytest.mark.asyncio
async def test_retry_cleanup_is_safe_after_partial_vector_deletion():
    class PartialVectorStore(FakeVectorStore):
        def __init__(self):
            super().__init__(ids_by_file={"bad": ["chunk-a", "chunk-b"]})
            self.remaining = ["chunk-a", "chunk-b"]
            self.attempts = 0

        async def query_ids_by_filter(self, collection, filters):
            return list(self.remaining)

        async def delete(self, ids, collection="default"):
            self.attempts += 1
            self.remaining = [chunk_id for chunk_id in self.remaining if chunk_id not in ids[:1]]
            if self.attempts == 1:
                raise RuntimeError("vector store timed out after deleting one chunk")
            return len(ids)

    wrepo = FakeWorkspaceRepo(orphaned=["bad"])
    vstore = PartialVectorStore()
    svc = _svc(wrepo=wrepo, vstore=vstore)
    first = await svc.delete_workspace("p", "w1")
    assert first["orphaned_files_failed"] == ["bad"]
    assert await svc.retry_failed_file_cleanup("bad", "p") is True
    assert vstore.remaining == []


# --------------------------------------------------------------------------- #
# keep_files — opt out of the orphan cleanup
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("during_delete", [False, True])
async def test_cancelled_cleanup_propagates_and_preserves_recovery_state(during_delete):
    class CancelledStore(FakeVectorStore):
        async def query_ids_by_filter(self, collection, filters):
            if not during_delete:
                raise asyncio.CancelledError()
            return ["chunk"]

        async def delete(self, ids, collection="default"):
            raise asyncio.CancelledError()

    repo = FakeWorkspaceRepo(orphaned=["f"])
    with pytest.raises(asyncio.CancelledError):
        await _svc(wrepo=repo, vstore=CancelledStore()).delete_workspace("p", "ws")
    assert repo.finalized == []
    assert repo.failed == ([("f", "p")] if during_delete else [])
    assert repo.released == ([] if during_delete else [("f", "p")])


@pytest.mark.asyncio
async def test_delete_workspace_keep_files_skips_file_deletion():
    """The workspace still goes, but the orphans stay indexed and are counted."""
    wrepo = FakeWorkspaceRepo(orphaned=["fA", "fB"])
    drepo = FakeDocumentRepo()
    vstore = FakeVectorStore(ids_by_file={"fA": ["c1"], "fB": ["c2"]})
    svc = _svc(wrepo=wrepo, drepo=drepo, vstore=vstore)

    out = await svc.delete_workspace("p", "w1", keep_files=True)

    assert out == {"orphaned_files_deleted": 0, "orphaned_files_failed": [], "kept_files": 2}
    assert wrepo.deleted == [("p", "w1", True)]
    # No file touched: no vector delete, no catalog row removal, no detach.
    assert vstore.deleted == []
    assert drepo.removed == []
    assert wrepo.removed_from_all == []


@pytest.mark.asyncio
async def test_delete_workspace_keep_files_with_no_orphans():
    wrepo = FakeWorkspaceRepo(orphaned=[])
    out = await _svc(wrepo=wrepo).delete_workspace("p", "w1", keep_files=True)
    assert out == {"orphaned_files_deleted": 0, "orphaned_files_failed": [], "kept_files": 0}


@pytest.mark.asyncio
async def test_delete_workspace_keep_files_false_matches_default():
    wrepo = FakeWorkspaceRepo(orphaned=["fA"])
    drepo = FakeDocumentRepo()
    vstore = FakeVectorStore(ids_by_file={"fA": ["c1"]})
    out = await _svc(wrepo=wrepo, drepo=drepo, vstore=vstore).delete_workspace("p", "w1", keep_files=False)
    assert out == {"orphaned_files_deleted": 1, "orphaned_files_failed": [], "kept_files": 0}
    assert vstore.deleted == [["c1"]]


# --------------------------------------------------------------------------- #
# resolve_scope — the workspace scope resolver (issue #706)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_scope_returns_partition_and_file_ids():
    wrepo = FakeWorkspaceRepo(
        workspaces=[Workspace(workspace_id="w1", partition="p1")],
        files=["f1", "f2"],
    )
    scope = await _svc(wrepo=wrepo).resolve_scope("w1", ["p1"])
    assert scope is not None
    assert scope.workspace_id == "w1"
    assert scope.partition == "p1"
    assert scope.file_ids == ["f1", "f2"]
    # The lookup is restricted to the allowed partitions, and the file list
    # is read from the partition that matched.
    assert wrepo.find_calls == [("w1", ["p1"])]
    assert wrepo.listed == [("p1", "w1")]


@pytest.mark.asyncio
async def test_resolve_scope_none_when_workspace_missing():
    wrepo = FakeWorkspaceRepo()
    assert await _svc(wrepo=wrepo).resolve_scope("ghost", ["p1"]) is None


@pytest.mark.asyncio
async def test_resolve_scope_none_when_partition_not_allowed():
    # Workspace exists but belongs to a partition the caller has no access
    # to — must look identical to "not found", never reveal the partition.
    wrepo = FakeWorkspaceRepo(workspaces=[Workspace(workspace_id="w1", partition="other")])
    assert await _svc(wrepo=wrepo).resolve_scope("w1", ["p1"]) is None


@pytest.mark.asyncio
async def test_resolve_scope_accepts_all_sentinel():
    wrepo = FakeWorkspaceRepo(workspaces=[Workspace(workspace_id="w1", partition="any-partition")], files=["f1"])
    scope = await _svc(wrepo=wrepo).resolve_scope("w1", ["all"])
    assert scope is not None
    assert scope.partition == "any-partition"
    # "all" means every partition: the repo is asked without a partition filter.
    assert wrepo.find_calls == [("w1", None)]


@pytest.mark.asyncio
async def test_resolve_scope_empty_workspace_returns_empty_file_ids():
    wrepo = FakeWorkspaceRepo(workspaces=[Workspace(workspace_id="w1", partition="p1")], files=[])
    scope = await _svc(wrepo=wrepo).resolve_scope("w1", ["p1"])
    assert scope is not None
    assert scope.file_ids == []


@pytest.mark.asyncio
async def test_resolve_scope_same_id_in_another_partition_does_not_interfere():
    # workspace_id is unique per partition only: "w1" in a partition the
    # caller cannot search must neither be picked nor make "w1" ambiguous.
    wrepo = FakeWorkspaceRepo(
        workspaces=[
            Workspace(workspace_id="w1", partition="p1"),
            Workspace(workspace_id="w1", partition="other"),
        ],
    )
    scope = await _svc(wrepo=wrepo).resolve_scope("w1", ["p1"])
    assert scope is not None
    assert scope.partition == "p1"


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [["p1", "p2"], ["all"]])
async def test_resolve_scope_raises_when_id_matches_several_searchable_partitions(allowed):
    wrepo = FakeWorkspaceRepo(
        workspaces=[
            Workspace(workspace_id="w1", partition="p2"),
            Workspace(workspace_id="w1", partition="p1"),
        ],
    )
    with pytest.raises(AmbiguousWorkspaceError) as excinfo:
        await _svc(wrepo=wrepo).resolve_scope("w1", allowed)
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "WORKSPACE_AMBIGUOUS"
    assert excinfo.value.extra["partitions"] == ["p1", "p2"]
    assert "p1, p2" in str(excinfo.value)
    # Never silently picks one.
    assert wrepo.listed == []
