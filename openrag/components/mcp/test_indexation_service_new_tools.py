"""
Unit tests for the new IndexationService methods added in the second batch:
  - list_my_tasks
  - get_task_logs
  - get_chunk_by_id
  - delete_file
  - update_file_metadata
  - copy_file
  - index_url

Ray is stubbed out via sys.modules before any openrag import (same pattern as
test_indexation_service.py).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub utils.dependencies BEFORE any openrag import
# ---------------------------------------------------------------------------
_stub_deps = ModuleType("utils.dependencies")
_stub_deps.get_vectordb = MagicMock()
_stub_deps.get_task_state_manager = MagicMock()
_stub_deps.get_indexer = MagicMock()
sys.modules.setdefault("utils.dependencies", _stub_deps)

import pytest
from components.mcp.indexation_service import IndexationService  # noqa: E402

# ---------------------------------------------------------------------------
# Fake helpers (same pattern as test_indexation_service.py)
# ---------------------------------------------------------------------------


class _SyncRemoteCall:
    def __init__(self, fn):
        self._fn = fn

    async def remote(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _FakeVectorDB:
    def __init__(
        self,
        file_exists_result: bool = True,
        chunks_by_file: dict | None = None,
        chunk_by_id: dict | None = None,
    ):
        self._file_exists_result = file_exists_result
        self._chunks_by_file = chunks_by_file or {}
        self._chunk_by_id = chunk_by_id or {}

        self.file_exists = _SyncRemoteCall(lambda file_id, partition: self._file_exists_result)
        self.get_file_chunks = _SyncRemoteCall(
            lambda partition=None, file_id=None, include_id=False: self._chunks_by_file.get((partition, file_id), [])
        )
        self.get_chunk_by_id = _SyncRemoteCall(lambda chunk_id: self._chunk_by_id.get(chunk_id))


class _FakeIndexer:
    def __init__(self):
        self.deleted = []
        self.updated = []
        self.copied = []

        self.delete_file = _SyncRemoteCall(lambda file_id, partition: self.deleted.append((file_id, partition)))
        self.update_file_metadata = _SyncRemoteCall(
            lambda file_id, metadata, partition, user=None: self.updated.append((file_id, metadata, partition))
        )
        self.copy_file = _SyncRemoteCall(
            lambda file_id, metadata, partition, user=None: self.copied.append((file_id, metadata, partition))
        )

        # add_file returns a fake task reference (not a coroutine, just sync)
        _self = self

        class _AddFileRemote:
            def remote(self, path, metadata, partition, user=None):
                return SimpleNamespace(
                    task_id=lambda: SimpleNamespace(hex=lambda: "deadbeef1234"),
                )

        self.add_file = _AddFileRemote()


class _FakeTaskStateManager:
    def __init__(
        self,
        all_info: dict | None = None,
        details: dict | None = None,
        states: dict | None = None,
        errors: dict | None = None,
    ):
        self._all_info = all_info or {}
        self._details = details or {}
        self._states = states or {}
        self._errors = errors or {}
        self._set_state_calls: list = []
        self._set_ref_calls: list = []

        self.get_all_info = _SyncRemoteCall(lambda: self._all_info)
        self.get_all_user_info = _SyncRemoteCall(
            lambda user_id: {
                tid: info for tid, info in self._all_info.items() if info.get("details", {}).get("user_id") == user_id
            }
        )
        self.get_details = _SyncRemoteCall(lambda task_id: self._details.get(task_id))
        self.get_state = _SyncRemoteCall(lambda task_id: self._states.get(task_id))
        self.get_error = _SyncRemoteCall(lambda task_id: self._errors.get(task_id))

        # Capture set calls so tests can assert on them
        self.set_state = _SyncRemoteCall(lambda task_id, state: self._set_state_calls.append((task_id, state)))
        self.set_object_ref = _SyncRemoteCall(lambda task_id, ref: self._set_ref_calls.append((task_id, ref)))


@pytest.fixture()
def patch_getters(monkeypatch):
    def _setup(vectordb=None, task_state_manager=None, indexer=None):
        import components.mcp.indexation_service as svc_mod

        if vectordb is not None:
            monkeypatch.setattr(svc_mod, "get_vectordb", lambda: vectordb)
        if task_state_manager is not None:
            monkeypatch.setattr(svc_mod, "get_task_state_manager", lambda: task_state_manager)
        if indexer is not None:
            monkeypatch.setattr(svc_mod, "get_indexer", lambda: indexer)

    return _setup


# ===========================================================================
# list_my_tasks
# ===========================================================================


@pytest.mark.asyncio
async def test_list_my_tasks_returns_all_for_noauth(patch_getters):
    info = {
        "t1": {"state": "COMPLETED", "error": None, "details": {"user_id": 1}},
        "t2": {"state": "QUEUED", "error": None, "details": {"user_id": 2}},
    }
    tsm = _FakeTaskStateManager(all_info=info)
    patch_getters(task_state_manager=tsm)

    result = await IndexationService().list_my_tasks(user_id=None)

    assert result["count"] == 2
    task_ids = {t["task_id"] for t in result["tasks"]}
    assert task_ids == {"t1", "t2"}


@pytest.mark.asyncio
async def test_list_my_tasks_filters_by_user(patch_getters):
    info = {
        "t1": {"state": "COMPLETED", "error": None, "details": {"user_id": 7}},
        "t2": {"state": "FAILED", "error": "oops", "details": {"user_id": 99}},
    }
    tsm = _FakeTaskStateManager(all_info=info)
    patch_getters(task_state_manager=tsm)

    result = await IndexationService().list_my_tasks(user_id=7)

    assert result["count"] == 1
    assert result["tasks"][0]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_list_my_tasks_active_filter(patch_getters):
    info = {
        "t1": {"state": "QUEUED", "error": None, "details": {"user_id": 1}},
        "t2": {"state": "COMPLETED", "error": None, "details": {"user_id": 1}},
        "t3": {"state": "INSERTING", "error": None, "details": {"user_id": 1}},
    }
    tsm = _FakeTaskStateManager(all_info=info)
    patch_getters(task_state_manager=tsm)

    result = await IndexationService().list_my_tasks(user_id=None, task_status="active")

    states = {t["state"] for t in result["tasks"]}
    assert "COMPLETED" not in states
    assert states.issubset({"QUEUED", "SERIALIZING", "CHUNKING", "INSERTING"})


@pytest.mark.asyncio
async def test_list_my_tasks_failed_includes_error(patch_getters):
    info = {
        "t1": {"state": "FAILED", "error": "something broke", "details": {"user_id": 1}},
    }
    tsm = _FakeTaskStateManager(all_info=info)
    patch_getters(task_state_manager=tsm)

    result = await IndexationService().list_my_tasks(user_id=None)

    assert result["tasks"][0]["error"] == "something broke"


@pytest.mark.asyncio
async def test_list_my_tasks_exact_state_filter(patch_getters):
    info = {
        "t1": {"state": "COMPLETED", "error": None, "details": {}},
        "t2": {"state": "FAILED", "error": "x", "details": {}},
    }
    tsm = _FakeTaskStateManager(all_info=info)
    patch_getters(task_state_manager=tsm)

    result = await IndexationService().list_my_tasks(user_id=None, task_status="failed")

    assert result["count"] == 1
    assert result["tasks"][0]["state"] == "FAILED"


# ===========================================================================
# get_task_logs
# ===========================================================================


def _write_log_lines(path: Path, records: list[dict]) -> None:
    with open(path, "w") as fh:
        for rec in records:
            fh.write(json.dumps({"record": rec}) + "\n")


def _make_log_record(task_id: str, message: str, level: str = "INFO") -> dict:
    return {
        "time": {"repr": "2024-01-01 00:00:00"},
        "level": {"name": level},
        "message": message,
        "extra": {"task_id": task_id},
    }


@pytest.mark.asyncio
async def test_get_task_logs_returns_lines(patch_getters, tmp_path):
    log_file = tmp_path / "app.json"
    _write_log_lines(
        log_file,
        [
            _make_log_record("t1", "started"),
            _make_log_record("t1", "chunking"),
            _make_log_record("other-task", "noise"),
        ],
    )
    tsm = _FakeTaskStateManager(details={"t1": {"user_id": 5}})
    patch_getters(task_state_manager=tsm)

    result = await IndexationService().get_task_logs(task_id="t1", user_id=5, log_file=log_file)

    assert result["count"] == 2
    assert all("t1" not in line or "started" in line or "chunking" in line for line in result["logs"])


@pytest.mark.asyncio
async def test_get_task_logs_max_lines(patch_getters, tmp_path):
    log_file = tmp_path / "app.json"
    _write_log_lines(log_file, [_make_log_record("t1", f"msg {i}") for i in range(20)])
    tsm = _FakeTaskStateManager(details={"t1": {"user_id": 1}})
    patch_getters(task_state_manager=tsm)

    result = await IndexationService().get_task_logs(task_id="t1", user_id=None, log_file=log_file, max_lines=5)

    assert result["count"] == 5


@pytest.mark.asyncio
async def test_get_task_logs_not_found_raises(patch_getters, tmp_path):
    log_file = tmp_path / "app.json"
    tsm = _FakeTaskStateManager()
    patch_getters(task_state_manager=tsm)

    with pytest.raises(KeyError, match="not found"):
        await IndexationService().get_task_logs(task_id="ghost", user_id=1, log_file=log_file)


@pytest.mark.asyncio
async def test_get_task_logs_wrong_user_raises(patch_getters, tmp_path):
    log_file = tmp_path / "app.json"
    tsm = _FakeTaskStateManager(details={"t1": {"user_id": 99}})
    patch_getters(task_state_manager=tsm)

    with pytest.raises(PermissionError, match="permission"):
        await IndexationService().get_task_logs(task_id="t1", user_id=1, log_file=log_file)


@pytest.mark.asyncio
async def test_get_task_logs_missing_log_file_raises(patch_getters, tmp_path):
    tsm = _FakeTaskStateManager(details={"t1": {"user_id": 1}})
    patch_getters(task_state_manager=tsm)

    with pytest.raises(FileNotFoundError):
        await IndexationService().get_task_logs(task_id="t1", user_id=None, log_file=tmp_path / "nonexistent.json")


# ===========================================================================
# get_chunk_by_id
# ===========================================================================


@pytest.mark.asyncio
async def test_get_chunk_by_id_returns_chunk(patch_getters):
    chunk = SimpleNamespace(
        page_content="hello world",
        metadata={"partition": "p1", "file_id": "f1"},
    )
    vdb = _FakeVectorDB(chunk_by_id={"chunk-1": chunk})
    patch_getters(vectordb=vdb)

    result = await IndexationService().get_chunk_by_id("chunk-1", allowed_partitions=["p1"])

    assert result["chunk_id"] == "chunk-1"
    assert result["page_content"] == "hello world"
    assert result["metadata"]["partition"] == "p1"


@pytest.mark.asyncio
async def test_get_chunk_by_id_admin_sees_any_partition(patch_getters):
    chunk = SimpleNamespace(
        page_content="secret",
        metadata={"partition": "restricted"},
    )
    vdb = _FakeVectorDB(chunk_by_id={"chunk-x": chunk})
    patch_getters(vectordb=vdb)

    result = await IndexationService().get_chunk_by_id("chunk-x", allowed_partitions=["all"])
    assert result["chunk_id"] == "chunk-x"


@pytest.mark.asyncio
async def test_get_chunk_by_id_not_found_raises(patch_getters):
    vdb = _FakeVectorDB(chunk_by_id={})
    patch_getters(vectordb=vdb)

    with pytest.raises(KeyError, match="not found"):
        await IndexationService().get_chunk_by_id("missing", allowed_partitions=["p1"])


@pytest.mark.asyncio
async def test_get_chunk_by_id_wrong_partition_raises(patch_getters):
    chunk = SimpleNamespace(
        page_content="nope",
        metadata={"partition": "private"},
    )
    vdb = _FakeVectorDB(chunk_by_id={"c1": chunk})
    patch_getters(vectordb=vdb)

    with pytest.raises(PermissionError, match="Access denied"):
        await IndexationService().get_chunk_by_id("c1", allowed_partitions=["public"])


@pytest.mark.asyncio
async def test_get_chunk_by_id_no_auth_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())

    with pytest.raises(PermissionError, match="Authentication context is missing"):
        await IndexationService().get_chunk_by_id("c1", allowed_partitions=None)


# ===========================================================================
# delete_file
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_file_success(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=True)
    idx = _FakeIndexer()
    patch_getters(vectordb=vdb, indexer=idx)

    result = await IndexationService().delete_file(partition="p1", file_id="f1", allowed_partitions=["p1"])

    assert result["file_id"] == "f1"
    assert ("f1", "p1") in idx.deleted


@pytest.mark.asyncio
async def test_delete_file_not_found_raises(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=False)
    patch_getters(vectordb=vdb, indexer=_FakeIndexer())

    with pytest.raises(FileNotFoundError):
        await IndexationService().delete_file(partition="p1", file_id="ghost", allowed_partitions=["p1"])


@pytest.mark.asyncio
async def test_delete_file_unauthorized_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB(), indexer=_FakeIndexer())

    with pytest.raises(PermissionError, match="Access denied"):
        await IndexationService().delete_file(partition="secret", file_id="f1", allowed_partitions=["public"])


# ===========================================================================
# update_file_metadata
# ===========================================================================


@pytest.mark.asyncio
async def test_update_file_metadata_success(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=True)
    idx = _FakeIndexer()
    patch_getters(vectordb=vdb, indexer=idx)

    result = await IndexationService().update_file_metadata(
        partition="p1",
        file_id="f1",
        metadata={"author": "Alice"},
        allowed_partitions=["p1"],
    )

    assert result["file_id"] == "f1"
    assert len(idx.updated) == 1
    assert idx.updated[0][1]["author"] == "Alice"


@pytest.mark.asyncio
async def test_update_file_metadata_not_found_raises(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=False)
    patch_getters(vectordb=vdb, indexer=_FakeIndexer())

    with pytest.raises(FileNotFoundError):
        await IndexationService().update_file_metadata(
            partition="p1", file_id="ghost", metadata={}, allowed_partitions=["p1"]
        )


@pytest.mark.asyncio
async def test_update_file_metadata_unauthorized_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB(), indexer=_FakeIndexer())

    with pytest.raises(PermissionError):
        await IndexationService().update_file_metadata(
            partition="secret", file_id="f1", metadata={}, allowed_partitions=["public"]
        )


@pytest.mark.asyncio
async def test_update_file_metadata_dest_partition_unauthorized_raises(patch_getters):
    """Moving to a partition the user has no access to should raise."""
    vdb = _FakeVectorDB(file_exists_result=True)
    patch_getters(vectordb=vdb, indexer=_FakeIndexer())

    with pytest.raises(PermissionError):
        await IndexationService().update_file_metadata(
            partition="p1",
            file_id="f1",
            metadata={"partition": "restricted"},
            allowed_partitions=["p1"],
        )


# ===========================================================================
# copy_file
# ===========================================================================


@pytest.mark.asyncio
async def test_copy_file_success(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=True)
    idx = _FakeIndexer()
    patch_getters(vectordb=vdb, indexer=idx)

    result = await IndexationService().copy_file(
        source_partition="src",
        source_file_id="f1",
        dest_partition="dst",
        dest_file_id="f1-copy",
        allowed_partitions=["src", "dst"],
    )

    assert result["dest_file_id"] == "f1-copy"
    assert len(idx.copied) == 1
    file_id, metadata, partition = idx.copied[0]
    assert file_id == "f1"
    assert metadata["partition"] == "dst"
    assert metadata["file_id"] == "f1-copy"


@pytest.mark.asyncio
async def test_copy_file_source_not_found_raises(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=False)
    patch_getters(vectordb=vdb, indexer=_FakeIndexer())

    with pytest.raises(FileNotFoundError):
        await IndexationService().copy_file(
            source_partition="src",
            source_file_id="ghost",
            dest_partition="dst",
            dest_file_id="new",
            allowed_partitions=["src", "dst"],
        )


@pytest.mark.asyncio
async def test_copy_file_no_source_access_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB(), indexer=_FakeIndexer())

    with pytest.raises(PermissionError):
        await IndexationService().copy_file(
            source_partition="private",
            source_file_id="f1",
            dest_partition="dst",
            dest_file_id="new",
            allowed_partitions=["dst"],
        )


@pytest.mark.asyncio
async def test_copy_file_no_dest_access_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB(), indexer=_FakeIndexer())

    with pytest.raises(PermissionError):
        await IndexationService().copy_file(
            source_partition="src",
            source_file_id="f1",
            dest_partition="private",
            dest_file_id="new",
            allowed_partitions=["src"],
        )


@pytest.mark.asyncio
async def test_copy_file_extra_metadata_merged(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=True)
    idx = _FakeIndexer()
    patch_getters(vectordb=vdb, indexer=idx)

    await IndexationService().copy_file(
        source_partition="src",
        source_file_id="f1",
        dest_partition="dst",
        dest_file_id="f2",
        allowed_partitions=["all"],
        extra_metadata={"author": "Bob"},
    )

    _, metadata, _ = idx.copied[0]
    assert metadata["author"] == "Bob"
    assert metadata["file_id"] == "f2"
    assert metadata["partition"] == "dst"


# ===========================================================================
# index_url
# ===========================================================================


@pytest.mark.asyncio
async def test_index_url_success(patch_getters, tmp_path, monkeypatch):
    # Fake a file download by writing a real temp file
    downloaded = tmp_path / "doc.pdf"
    downloaded.write_bytes(b"%PDF fake")

    def fake_urlretrieve(url, dest):
        Path(dest).write_bytes(b"%PDF fake")

    monkeypatch.setattr("components.mcp.indexation_service.urllib.request.urlretrieve", fake_urlretrieve)

    vdb = _FakeVectorDB(file_exists_result=False)
    idx = _FakeIndexer()
    tsm = _FakeTaskStateManager()
    patch_getters(vectordb=vdb, indexer=idx, task_state_manager=tsm)

    result = await IndexationService().index_url(
        url="https://example.com/report.pdf",
        partition="p1",
        file_id="report-2024",
        allowed_partitions=["p1"],
        task_state_manager_ref=tsm,
    )

    assert result["file_id"] == "report-2024"
    assert result["partition"] == "p1"
    assert "task_id" in result
    # task state was set
    assert len(tsm._set_state_calls) == 1
    assert tsm._set_state_calls[0] == ("deadbeef1234", "QUEUED")


@pytest.mark.asyncio
async def test_index_url_invalid_scheme_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB(file_exists_result=False))

    with pytest.raises(ValueError, match="http"):
        await IndexationService().index_url(
            url="ftp://example.com/file.txt",
            partition="p1",
            file_id="f1",
            allowed_partitions=["p1"],
        )


@pytest.mark.asyncio
async def test_index_url_already_exists_raises(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=True)
    patch_getters(vectordb=vdb)

    with pytest.raises(FileExistsError):
        await IndexationService().index_url(
            url="https://example.com/doc.pdf",
            partition="p1",
            file_id="existing",
            allowed_partitions=["p1"],
        )


@pytest.mark.asyncio
async def test_index_url_unauthorized_raises(patch_getters, monkeypatch):
    # partition exists in the DB but caller has no membership → PermissionError
    monkeypatch.setattr("components.mcp.indexation_service.get_user_id", lambda: 1)
    patch_getters(vectordb=_FakeVectorDBWithPartitions(file_exists_result=False, partition_exists_result=True))

    with pytest.raises(PermissionError):
        await IndexationService().index_url(
            url="https://example.com/doc.pdf",
            partition="secret",
            file_id="f1",
            allowed_partitions=["public"],
        )


@pytest.mark.asyncio
async def test_index_url_download_failure_raises(patch_getters, monkeypatch):
    def bad_urlretrieve(url, dest):
        raise OSError("network error")

    monkeypatch.setattr("components.mcp.indexation_service.urllib.request.urlretrieve", bad_urlretrieve)

    vdb = _FakeVectorDB(file_exists_result=False)
    patch_getters(vectordb=vdb)

    with pytest.raises(RuntimeError, match="Failed to download"):
        await IndexationService().index_url(
            url="https://example.com/doc.pdf",
            partition="p1",
            file_id="f1",
            allowed_partitions=["p1"],
        )


# ===========================================================================
# _ensure_partition_exists / auto-creation
# ===========================================================================


class _FakeVectorDBWithPartitions(_FakeVectorDB):
    """Extended fake that supports partition_exists and create_partition."""

    def __init__(self, *, file_exists_result: bool = False, partition_exists_result: bool = False, **kwargs):
        super().__init__(file_exists_result=file_exists_result, **kwargs)
        self._partition_exists_result = partition_exists_result
        self._created_partitions: list[dict] = []

        self.partition_exists = _SyncRemoteCall(lambda partition: self._partition_exists_result)
        self.create_partition = _SyncRemoteCall(
            lambda partition, user_id: self._created_partitions.append({"partition": partition, "user_id": user_id})
        )


@pytest.mark.asyncio
async def test_index_url_auto_creates_missing_partition(patch_getters, monkeypatch):
    """When the target partition does not exist, index_url should create it."""

    def fake_urlretrieve(url, dest):
        Path(dest).write_bytes(b"%PDF fake")

    monkeypatch.setattr("components.mcp.indexation_service.urllib.request.urlretrieve", fake_urlretrieve)
    monkeypatch.setattr("components.mcp.indexation_service.get_user_id", lambda: 42)

    vdb = _FakeVectorDBWithPartitions(file_exists_result=False, partition_exists_result=False)
    idx = _FakeIndexer()
    tsm = _FakeTaskStateManager()
    patch_getters(vectordb=vdb, indexer=idx, task_state_manager=tsm)

    allowed = ["existing"]
    result = await IndexationService().index_url(
        url="https://example.com/report.pdf",
        partition="food",
        file_id="report-001",
        allowed_partitions=allowed,
        task_state_manager_ref=tsm,
    )

    # Partition was auto-created with the correct user
    assert len(vdb._created_partitions) == 1
    assert vdb._created_partitions[0] == {"partition": "food", "user_id": 42}

    # "food" was appended to allowed_partitions so the access check passed
    assert "food" in allowed

    # Indexation was started
    assert result["partition"] == "food"
    assert result["file_id"] == "report-001"
    assert "task_id" in result


@pytest.mark.asyncio
async def test_index_url_no_auto_create_when_partition_exists(patch_getters, monkeypatch):
    """When the partition exists but the user has no membership, raise PermissionError (no auto-creation)."""

    monkeypatch.setattr("components.mcp.indexation_service.get_user_id", lambda: 42)

    # partition_exists_result=True → the partition is in the DB, caller just has no membership
    vdb = _FakeVectorDBWithPartitions(file_exists_result=False, partition_exists_result=True)
    patch_getters(vectordb=vdb)

    with pytest.raises(PermissionError, match="Access denied for partition: secret"):
        await IndexationService().index_url(
            url="https://example.com/doc.pdf",
            partition="secret",
            file_id="f1",
            allowed_partitions=["other"],
        )

    # No partition was created
    assert vdb._created_partitions == []


@pytest.mark.asyncio
async def test_index_url_admin_skips_auto_create(patch_getters, monkeypatch):
    """Admin users (allowed_partitions=["all"]) bypass auto-creation entirely."""

    def fake_urlretrieve(url, dest):
        Path(dest).write_bytes(b"%PDF fake")

    monkeypatch.setattr("components.mcp.indexation_service.urllib.request.urlretrieve", fake_urlretrieve)
    monkeypatch.setattr("components.mcp.indexation_service.get_user_id", lambda: 1)

    vdb = _FakeVectorDBWithPartitions(file_exists_result=False, partition_exists_result=False)
    idx = _FakeIndexer()
    tsm = _FakeTaskStateManager()
    patch_getters(vectordb=vdb, indexer=idx, task_state_manager=tsm)

    result = await IndexationService().index_url(
        url="https://example.com/report.pdf",
        partition="any-partition",
        file_id="f-admin",
        allowed_partitions=["all"],
        task_state_manager_ref=tsm,
    )

    # Admin: no auto-creation needed
    assert vdb._created_partitions == []
    assert result["partition"] == "any-partition"
