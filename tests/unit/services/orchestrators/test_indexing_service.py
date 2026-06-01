"""Unit tests for :class:`IndexingService` (Phase 8D.1)."""

from __future__ import annotations

import pytest
from services.orchestrators.indexing_service import IndexingService


class FakeDocumentRepo:
    def __init__(self, *, exists: bool = False, raise_on_check: bool = False):
        self._exists = exists
        self._raise = raise_on_check

    async def file_exists_in_partition(self, file_id: str, partition: str) -> bool:
        if self._raise:
            raise RuntimeError("boom")
        return self._exists


class FakeWorkspaceRepo:
    def __init__(self, *, workspace=None):
        self._workspace = workspace

    async def get_workspace_dict(self, workspace_id: str):
        return self._workspace


class FakeDispatcher:
    def __init__(self):
        self.dispatched: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.updated: list[tuple] = []
        self.copied: list[tuple] = []
        self.cancelled: list[str] = []
        self.cancel_result = True

    async def dispatch_indexing(self, *, path, metadata, partition, user, workspace_ids, replace):
        self.dispatched.append(
            {
                "path": path,
                "metadata": metadata,
                "partition": partition,
                "user": user,
                "workspace_ids": workspace_ids,
                "replace": replace,
            }
        )
        return "task-abc"

    async def delete_file(self, file_id, partition):
        self.deleted.append((file_id, partition))

    async def update_file_metadata(self, file_id, metadata, partition, user):
        self.updated.append((file_id, metadata, partition, user))

    async def copy_file(self, file_id, metadata, partition, user):
        self.copied.append((file_id, metadata, partition, user))

    async def get_task_state(self, task_id):
        return "QUEUED"

    async def get_task_error(self, task_id):
        return "trace"

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return self.cancel_result


def _service(*, doc=None, ws=None, disp=None):
    return IndexingService(
        document_repo=doc or FakeDocumentRepo(),
        workspace_repo=ws or FakeWorkspaceRepo(),
        dispatcher=disp or FakeDispatcher(),
    )


@pytest.mark.asyncio
async def test_file_exists_passthrough():
    svc = _service(doc=FakeDocumentRepo(exists=True))
    assert await svc.file_exists("f1", "p1") is True


@pytest.mark.asyncio
async def test_file_exists_swallows_errors():
    svc = _service(doc=FakeDocumentRepo(raise_on_check=True))
    assert await svc.file_exists("f1", "p1") is False


@pytest.mark.asyncio
async def test_get_workspace_passthrough():
    ws = {"workspace_id": "w1", "partition_name": "p1"}
    svc = _service(ws=FakeWorkspaceRepo(workspace=ws))
    assert await svc.get_workspace("w1") == ws


@pytest.mark.asyncio
async def test_add_file_builds_metadata_and_dispatches(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("hello world")
    disp = FakeDispatcher()
    svc = _service(disp=disp)

    task_id = await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="p1",
        metadata={"author": "alice"},
        sanitized_filename="doc.txt",
        original_filename="Doc Original.txt",
        user={"id": 7},
        workspace_ids=["w1"],
    )

    assert task_id == "task-abc"
    assert len(disp.dispatched) == 1
    sent = disp.dispatched[0]
    assert sent["path"] == str(f)
    assert sent["partition"] == "p1"
    assert sent["workspace_ids"] == ["w1"]
    assert sent["replace"] is False
    md = sent["metadata"]
    assert md["author"] == "alice"
    assert md["source"] == str(f)
    assert md["filename"] == "doc.txt"
    assert md["original_filename"] == "Doc Original.txt"
    assert md["file_id"] == "f1"
    assert md["file_size"] == "11.00 B"


@pytest.mark.asyncio
async def test_replace_sets_replace_flag(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("x")
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.add_file(
        file_path=str(f),
        file_id="f1",
        partition="p1",
        metadata={},
        sanitized_filename="doc.txt",
        original_filename="doc.txt",
        user=None,
        replace=True,
    )
    assert disp.dispatched[0]["replace"] is True


@pytest.mark.asyncio
async def test_delete_file_delegates():
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.delete_file("f1", "p1")
    assert disp.deleted == [("f1", "p1")]


@pytest.mark.asyncio
async def test_update_metadata_injects_file_id():
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.update_metadata("f1", {"author": "bob"}, "p1", {"id": 1})
    file_id, md, partition, user = disp.updated[0]
    assert file_id == "f1"
    assert md == {"author": "bob", "file_id": "f1"}
    assert partition == "p1"
    assert user == {"id": 1}


@pytest.mark.asyncio
async def test_copy_file_sets_target_fields():
    disp = FakeDispatcher()
    svc = _service(disp=disp)
    await svc.copy_file(
        source_file_id="src",
        source_partition="p-src",
        target_file_id="dst",
        target_partition="p-dst",
        metadata={"k": "v"},
        user={"id": 2},
    )
    file_id, md, partition, user = disp.copied[0]
    assert file_id == "src"
    assert partition == "p-src"
    assert md == {"k": "v", "file_id": "dst", "partition": "p-dst"}
    assert user == {"id": 2}


@pytest.mark.asyncio
async def test_task_state_and_error_passthrough():
    svc = _service()
    assert await svc.get_task_state("t1") == "QUEUED"
    assert await svc.get_task_error("t1") == "trace"


@pytest.mark.asyncio
async def test_cancel_task_passthrough():
    disp = FakeDispatcher()
    disp.cancel_result = False
    svc = _service(disp=disp)
    assert await svc.cancel_task("t1") is False
    assert disp.cancelled == ["t1"]
