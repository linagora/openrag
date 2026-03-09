"""
Unit tests for IndexationService.

All external dependencies (Ray actors) are replaced with lightweight fake
objects that follow the same async .remote() call pattern used in production,
matching the style of the existing test_service.py / test_ray_indexer_adapter.py.

Ray is never imported: a stub module is inserted into sys.modules *before* any
openrag import so that ``utils.dependencies`` (which calls ray.get_actor at
module load time) never touches a real Ray cluster.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub out utils.dependencies BEFORE importing anything from openrag so that
# the module-level Ray actor calls in that file are never executed.
# ---------------------------------------------------------------------------
_stub_deps = ModuleType("utils.dependencies")
_stub_deps.get_vectordb = MagicMock()
_stub_deps.get_task_state_manager = MagicMock()
_stub_deps.get_indexer = MagicMock()
sys.modules.setdefault("utils.dependencies", _stub_deps)

import pytest
from components.mcp.indexation_service import IndexationService

# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------


def _make_chunk(text: str, file_id: str, partition: str, chunk_id: int = 1, **extra):
    """Return a SimpleNamespace that looks like a LangChain Document."""
    return SimpleNamespace(
        page_content=text,
        metadata={"_id": chunk_id, "file_id": file_id, "partition": partition, **extra},
    )


class _RemoteCall:
    """Wraps a coroutine so it can be awaited via `.remote()`."""

    def __init__(self, coro_fn):
        self._fn = coro_fn

    async def remote(self, *args, **kwargs):
        return await self._fn(*args, **kwargs)


class _SyncRemoteCall:
    """Wraps a sync callable so it can be awaited via `.remote()`."""

    def __init__(self, fn):
        self._fn = fn

    async def remote(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _FakeVectorDB:
    """Minimal fake of the MilvusDB Ray actor surface used by IndexationService."""

    def __init__(
        self,
        partitions: list[dict] | None = None,
        files_by_partition: dict[str, list[dict]] | None = None,
        chunks_by_file: dict[tuple[str, str], list] | None = None,
        file_exists_result: bool = True,
    ):
        self._partitions = partitions or []
        self._files_by_partition = files_by_partition or {}
        self._chunks_by_file = chunks_by_file or {}
        self._file_exists_result = file_exists_result

        # Expose each method as a _SyncRemoteCall / _RemoteCall
        self.list_partitions = _SyncRemoteCall(lambda: self._partitions)
        self.list_partition_files = _SyncRemoteCall(
            lambda partition, limit=None: {
                "files": (self._files_by_partition.get(partition, []))[:limit]
                if limit is not None
                else self._files_by_partition.get(partition, [])
            }
        )
        self.file_exists = _SyncRemoteCall(lambda file_id, partition: self._file_exists_result)
        self.get_file_chunks = _SyncRemoteCall(
            lambda partition, file_id, include_id=False: self._chunks_by_file.get((partition, file_id), [])
        )


class _FakeTaskStateManager:
    def __init__(self, states: dict | None = None, details: dict | None = None, errors: dict | None = None):
        self._states = states or {}
        self._details = details or {}
        self._errors = errors or {}

        self.get_state = _SyncRemoteCall(lambda task_id: self._states.get(task_id))
        self.get_details = _SyncRemoteCall(lambda task_id: self._details.get(task_id))
        self.get_error = _SyncRemoteCall(lambda task_id: self._errors.get(task_id))


# ---------------------------------------------------------------------------
# Fixture: service with injected fakes
# ---------------------------------------------------------------------------


def _make_service(vectordb=None, task_state_manager=None):
    """
    Return an IndexationService whose internal actor calls are redirected to
    the supplied fakes via monkeypatching the module-level getter functions.
    """
    svc = IndexationService()
    svc._test_vectordb = vectordb
    svc._test_tsm = task_state_manager
    return svc


# We patch the getter functions at the *service module* level so that
# ``get_vectordb()`` and ``get_task_state_manager()`` return our fakes.


@pytest.fixture()
def patch_getters(monkeypatch):
    """
    Returns a helper that patches the actor-getter functions used by
    IndexationService for a single test.
    """

    def _setup(vectordb=None, task_state_manager=None):
        import components.mcp.indexation_service as svc_mod

        if vectordb is not None:
            monkeypatch.setattr(svc_mod, "get_vectordb", lambda: vectordb)
        if task_state_manager is not None:
            monkeypatch.setattr(svc_mod, "get_task_state_manager", lambda: task_state_manager)

    return _setup


# ---------------------------------------------------------------------------
# list_partitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_partitions_admin_sees_all(patch_getters):
    vdb = _FakeVectorDB(partitions=[{"partition": "a"}, {"partition": "b"}])
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.list_partitions(allowed_partitions=["all"])

    assert result["count"] == 2
    assert {p["partition"] for p in result["partitions"]} == {"a", "b"}


@pytest.mark.asyncio
async def test_list_partitions_filters_by_membership(patch_getters):
    vdb = _FakeVectorDB(partitions=[{"partition": "a"}, {"partition": "b"}, {"partition": "c"}])
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.list_partitions(allowed_partitions=["a", "c"])

    assert result["count"] == 2
    assert {p["partition"] for p in result["partitions"]} == {"a", "c"}


@pytest.mark.asyncio
async def test_list_partitions_no_auth_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(PermissionError, match="Authentication context is missing"):
        await svc.list_partitions(allowed_partitions=None)


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_returns_files(patch_getters):
    files = [
        {"file_id": "f1", "filename": "report.pdf", "partition": "p1"},
        {"file_id": "f2", "filename": "notes.txt", "partition": "p1"},
    ]
    vdb = _FakeVectorDB(files_by_partition={"p1": files})
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.list_files(partition="p1", allowed_partitions=["p1"])

    assert result["partition"] == "p1"
    assert result["count"] == 2
    assert result["files"] == files


@pytest.mark.asyncio
async def test_list_files_respects_limit(patch_getters):
    files = [{"file_id": f"f{i}", "filename": f"doc{i}.pdf", "partition": "p1"} for i in range(10)]
    vdb = _FakeVectorDB(files_by_partition={"p1": files})
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.list_files(partition="p1", allowed_partitions=["all"], limit=3)

    assert result["count"] == 3


@pytest.mark.asyncio
async def test_list_files_denies_unauthorized_partition(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(PermissionError, match="Access denied"):
        await svc.list_files(partition="secret", allowed_partitions=["public"])


@pytest.mark.asyncio
async def test_list_files_no_auth_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(PermissionError, match="Authentication context is missing"):
        await svc.list_files(partition="p1", allowed_partitions=None)


# ---------------------------------------------------------------------------
# get_file_info
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_info_returns_metadata_and_chunk_count(patch_getters):
    chunks = [
        _make_chunk("chunk 1", "f1", "p1", chunk_id=10, filename="doc.pdf"),
        _make_chunk("chunk 2", "f1", "p1", chunk_id=11, filename="doc.pdf"),
    ]
    vdb = _FakeVectorDB(chunks_by_file={("p1", "f1"): chunks}, file_exists_result=True)
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.get_file_info(partition="p1", file_id="f1", allowed_partitions=["p1"])

    assert result["partition"] == "p1"
    assert result["file_id"] == "f1"
    assert result["chunk_count"] == 2
    assert result["metadata"]["filename"] == "doc.pdf"


@pytest.mark.asyncio
async def test_get_file_info_not_found_raises(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=False)
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    with pytest.raises(FileNotFoundError, match="not found in partition"):
        await svc.get_file_info(partition="p1", file_id="missing", allowed_partitions=["p1"])


@pytest.mark.asyncio
async def test_get_file_info_denies_unauthorized_partition(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(PermissionError, match="Access denied"):
        await svc.get_file_info(partition="secret", file_id="f1", allowed_partitions=["public"])


# ---------------------------------------------------------------------------
# get_file_chunks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_file_chunks_returns_all_chunks(patch_getters):
    chunks = [
        _make_chunk("hello world", "f1", "p1", chunk_id=1),
        _make_chunk("foo bar", "f1", "p1", chunk_id=2),
    ]
    vdb = _FakeVectorDB(chunks_by_file={("p1", "f1"): chunks}, file_exists_result=True)
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.get_file_chunks(partition="p1", file_id="f1", allowed_partitions=["p1"])

    assert result["partition"] == "p1"
    assert result["file_id"] == "f1"
    assert result["total_chunks"] == 2
    assert result["offset"] == 0
    assert result["has_more"] is False
    assert result["chunks"][0]["content"] == "hello world"
    assert result["chunks"][0]["chunk_id"] == 1
    # _id should not leak into the metadata dict
    assert "_id" not in result["chunks"][0]["metadata"]
    assert result["chunks"][1]["content"] == "foo bar"


@pytest.mark.asyncio
async def test_get_file_chunks_pagination(patch_getters):
    """offset/limit returns the correct slice and sets has_more correctly."""
    chunks = [_make_chunk(f"chunk {i}", "f1", "p1", chunk_id=i) for i in range(5)]
    vdb = _FakeVectorDB(chunks_by_file={("p1", "f1"): chunks}, file_exists_result=True)
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    page1 = await svc.get_file_chunks(partition="p1", file_id="f1", allowed_partitions=["p1"], offset=0, limit=2)
    assert len(page1["chunks"]) == 2
    assert page1["has_more"] is True
    assert page1["total_chunks"] == 5

    page3 = await svc.get_file_chunks(partition="p1", file_id="f1", allowed_partitions=["p1"], offset=4, limit=2)
    assert len(page3["chunks"]) == 1
    assert page3["has_more"] is False


@pytest.mark.asyncio
async def test_get_file_chunks_limit_minus_one(patch_getters):
    """limit=-1 returns all chunks from offset with has_more=False."""
    chunks = [_make_chunk(f"chunk {i}", "f1", "p1", chunk_id=i) for i in range(5)]
    vdb = _FakeVectorDB(chunks_by_file={("p1", "f1"): chunks}, file_exists_result=True)
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.get_file_chunks(partition="p1", file_id="f1", allowed_partitions=["p1"], offset=0, limit=-1)
    assert len(result["chunks"]) == 5
    assert result["has_more"] is False

    result_mid = await svc.get_file_chunks(partition="p1", file_id="f1", allowed_partitions=["p1"], offset=2, limit=-1)
    assert len(result_mid["chunks"]) == 3
    assert result_mid["has_more"] is False


@pytest.mark.asyncio
async def test_get_file_chunks_not_found_raises(patch_getters):
    vdb = _FakeVectorDB(file_exists_result=False)
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    with pytest.raises(FileNotFoundError):
        await svc.get_file_chunks(partition="p1", file_id="missing", allowed_partitions=["p1"])


@pytest.mark.asyncio
async def test_get_file_chunks_denies_unauthorized_partition(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(PermissionError):
        await svc.get_file_chunks(partition="secret", file_id="f1", allowed_partitions=["other"])


# ---------------------------------------------------------------------------
# fuzzy_search_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fuzzy_search_finds_close_match(patch_getters):
    files = [
        {"file_id": "f1", "filename": "annual_report_2024.pdf", "partition": "p1"},
        {"file_id": "f2", "filename": "meeting_notes.docx", "partition": "p1"},
    ]
    vdb = _FakeVectorDB(files_by_partition={"p1": files})
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.fuzzy_search_files(
        query="annual report",
        allowed_partitions=["p1"],
        partition="p1",
    )

    assert result["query"] == "annual report"
    assert result["count"] >= 1
    # The annual report file should be the top hit
    assert result["files"][0]["file_id"] == "f1"
    assert 0.0 <= result["files"][0]["score"] <= 1.0


@pytest.mark.asyncio
async def test_fuzzy_search_sorted_by_score_descending(patch_getters):
    files = [
        {"file_id": "f1", "filename": "report.pdf", "partition": "p1"},
        {"file_id": "f2", "filename": "report_2024.pdf", "partition": "p1"},
        {"file_id": "f3", "filename": "completely_different.txt", "partition": "p1"},
    ]
    vdb = _FakeVectorDB(files_by_partition={"p1": files})
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.fuzzy_search_files(query="report", allowed_partitions=["p1"], partition="p1", cutoff=0.0)

    scores = [f["score"] for f in result["files"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_fuzzy_search_respects_limit(patch_getters):
    files = [{"file_id": f"f{i}", "filename": f"report_{i}.pdf", "partition": "p1"} for i in range(10)]
    vdb = _FakeVectorDB(files_by_partition={"p1": files})
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.fuzzy_search_files(
        query="report",
        allowed_partitions=["p1"],
        partition="p1",
        cutoff=0.0,
        limit=3,
    )

    assert result["count"] <= 3


@pytest.mark.asyncio
async def test_fuzzy_search_cutoff_filters_low_scores(patch_getters):
    files = [
        {"file_id": "f1", "filename": "zzz_totally_unrelated.txt", "partition": "p1"},
    ]
    vdb = _FakeVectorDB(files_by_partition={"p1": files})
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.fuzzy_search_files(
        query="annual report",
        allowed_partitions=["p1"],
        partition="p1",
        cutoff=0.9,  # very strict
    )

    assert result["count"] == 0


@pytest.mark.asyncio
async def test_fuzzy_search_across_all_partitions(patch_getters):
    files_p1 = [{"file_id": "f1", "filename": "budget.xlsx", "partition": "p1"}]
    files_p2 = [{"file_id": "f2", "filename": "budget_forecast.xlsx", "partition": "p2"}]
    vdb = _FakeVectorDB(
        partitions=[{"partition": "p1"}, {"partition": "p2"}],
        files_by_partition={"p1": files_p1, "p2": files_p2},
    )
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.fuzzy_search_files(
        query="budget",
        allowed_partitions=["all"],
        partition=None,
        cutoff=0.3,
    )

    found_ids = {f["file_id"] for f in result["files"]}
    assert "f1" in found_ids
    assert "f2" in found_ids


@pytest.mark.asyncio
async def test_fuzzy_search_empty_query_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(ValueError, match="Query cannot be empty"):
        await svc.fuzzy_search_files(query="   ", allowed_partitions=["p1"])


@pytest.mark.asyncio
async def test_fuzzy_search_no_auth_raises(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(PermissionError, match="Authentication context is missing"):
        await svc.fuzzy_search_files(query="report", allowed_partitions=None)


@pytest.mark.asyncio
async def test_fuzzy_search_denies_unauthorized_partition(patch_getters):
    patch_getters(vectordb=_FakeVectorDB())
    svc = IndexationService()

    with pytest.raises(PermissionError, match="Access denied"):
        await svc.fuzzy_search_files(query="report", allowed_partitions=["public"], partition="secret")


@pytest.mark.asyncio
async def test_fuzzy_search_matches_file_id_field(patch_getters):
    """fuzzy_search_files should also match against the file_id field."""
    files = [{"file_id": "annual-report-2024", "filename": "untitled.bin", "partition": "p1"}]
    vdb = _FakeVectorDB(files_by_partition={"p1": files})
    patch_getters(vectordb=vdb)
    svc = IndexationService()

    result = await svc.fuzzy_search_files(
        query="annual report",
        allowed_partitions=["p1"],
        partition="p1",
        cutoff=0.3,
    )

    assert result["count"] >= 1
    assert result["files"][0]["file_id"] == "annual-report-2024"


# ---------------------------------------------------------------------------
# get_task_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_task_status_completed(patch_getters):
    tsm = _FakeTaskStateManager(
        states={"task-1": "COMPLETED"},
        details={"task-1": {"user_id": 42, "filename": "doc.pdf"}},
    )
    patch_getters(task_state_manager=tsm)
    svc = IndexationService()

    result = await svc.get_task_status(task_id="task-1", user_id=42)

    assert result["task_id"] == "task-1"
    assert result["task_state"] == "COMPLETED"
    assert result["details"]["user_id"] == 42
    assert "error" not in result


@pytest.mark.asyncio
async def test_get_task_status_failed_includes_error(patch_getters):
    tsm = _FakeTaskStateManager(
        states={"task-2": "FAILED"},
        details={"task-2": {"user_id": 7}},
        errors={"task-2": "Traceback ...\nValueError: something went wrong"},
    )
    patch_getters(task_state_manager=tsm)
    svc = IndexationService()

    result = await svc.get_task_status(task_id="task-2", user_id=7)

    assert result["task_state"] == "FAILED"
    assert "ValueError" in result["error"]


@pytest.mark.asyncio
async def test_get_task_status_not_found_raises(patch_getters):
    patch_getters(task_state_manager=_FakeTaskStateManager())
    svc = IndexationService()

    with pytest.raises(KeyError, match="not found"):
        await svc.get_task_status(task_id="nonexistent", user_id=1)


@pytest.mark.asyncio
async def test_get_task_status_wrong_user_raises(patch_getters):
    tsm = _FakeTaskStateManager(
        states={"task-3": "QUEUED"},
        details={"task-3": {"user_id": 99}},
    )
    patch_getters(task_state_manager=tsm)
    svc = IndexationService()

    with pytest.raises(PermissionError, match="do not have permission"):
        await svc.get_task_status(task_id="task-3", user_id=1)


@pytest.mark.asyncio
async def test_get_task_status_no_auth_skips_ownership_check(patch_getters):
    """When user_id is None (no-auth mode), ownership check is skipped."""
    tsm = _FakeTaskStateManager(
        states={"task-4": "COMPLETED"},
        details={"task-4": {"user_id": 99}},
    )
    patch_getters(task_state_manager=tsm)
    svc = IndexationService()

    # Should NOT raise even though user_id doesn't match task owner
    result = await svc.get_task_status(task_id="task-4", user_id=None)
    assert result["task_state"] == "COMPLETED"
