from types import SimpleNamespace

import pytest
from routers.audit_utils import summarize_audit_run

from rag_audit.openrag_adapter import from_openrag_documents
from rag_audit.openrag_job import _discover_partitions
from rag_audit.openrag_runner import execute_openrag_audit_run


class _RemoteMethod:
    def __init__(self, func):
        self._func = func

    async def remote(self, *args, **kwargs):
        return await self._func(*args, **kwargs)


class _FakeVectorDB:
    def __init__(self, *, chunks=None, files=None):
        self.chunks = chunks or []
        self.files = files or []
        self.created = []
        self.updated = []
        self.cleaned = []
        self.create_audit_run = _RemoteMethod(self._create_audit_run)
        self.list_all_chunk = _RemoteMethod(self._list_all_chunk)
        self.list_partition_files = _RemoteMethod(self._list_partition_files)
        self.update_audit_run = _RemoteMethod(self._update_audit_run)
        self.cleanup_audit_runs = _RemoteMethod(self._cleanup_audit_runs)

    async def _create_audit_run(self, partition, config):
        partition_id = len(self.created) + 100
        run = {
            "run_id": f"run-{len(self.created) + 1}",
            "partition": partition,
            "partition_name": partition,
            "partition_id": partition_id,
            "status": "running",
        }
        self.created.append({"partition": partition, "partition_id": partition_id, "config": config})
        return run

    async def _list_all_chunk(self, partition, include_embedding=False):
        return self.chunks

    async def _list_partition_files(self, partition, limit=None):
        return {"files": self.files}

    async def _update_audit_run(self, run_id, **fields):
        updated = {"run_id": run_id, **fields}
        self.updated.append(updated)
        return updated

    async def _cleanup_audit_runs(self, partition, retention_days):
        self.cleaned.append({"partition": partition, "retention_days": retention_days})
        return 0


class _FakeIndexer:
    def __init__(self):
        self.asearch = _RemoteMethod(self._asearch)

    async def _asearch(self, **kwargs):
        return []


class _FakePartitionVectorDB:
    def __init__(self, rows):
        self.list_partitions = _RemoteMethod(self._list_partitions)
        self.rows = rows

    async def _list_partitions(self):
        return self.rows


def _chunk(file_id: str, chunk_id: str, text: str):
    return SimpleNamespace(
        page_content=text,
        metadata={
            "_id": chunk_id,
            "file_id": file_id,
            "partition": "docs",
            "filename": f"{file_id}.txt",
            "source": f"/data/{file_id}.txt",
            "created_at": "2026-01-01T00:00:00+00:00",
            "page": 1,
        },
    )


def test_openrag_adapter_groups_chunks_and_file_metadata():
    chunks = [
        _chunk("file-a", "2", "Second chunk"),
        _chunk("file-a", "1", "# First chunk"),
        _chunk("file-b", "3", "Only chunk"),
    ]
    files = [
        {
            "file_id": "file-a",
            "filename": "file-a.txt",
            "partition": "docs",
            "source": "/data/file-a.txt",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]

    documents, audit_chunks = from_openrag_documents(chunks, files)

    assert {doc.id for doc in documents} == {"file-a", "file-b"}
    assert [chunk.document_id for chunk in audit_chunks] == ["file-a", "file-a", "file-b"]
    assert audit_chunks[0].heading_path == "First chunk"
    assert all(chunk.content_hash for chunk in audit_chunks)


def test_audit_summary_omits_detailed_payloads():
    summary = summarize_audit_run(
        {
            "run_id": "run-1",
            "partition": "docs",
            "partition_name": "docs",
            "partition_id": 42,
            "status": "completed",
            "overall_score": 75.8,
            "overall_grade": "B",
            "document_count": 9,
            "chunk_count": 42,
            "result": {
                "axis_results": [
                    {
                        "axis": "hygiene",
                        "score": 80.0,
                        "duration_seconds": 1.2,
                        "metrics": {
                            "total_docs": 9,
                            "total_chunks": 42,
                            "sub_scores": {"uniqueness": 90.0},
                        },
                        "chart_data": {"large": "payload"},
                        "details": {"verbose": "payload"},
                    }
                ]
            },
        }
    )

    assert summary["overall_score"] == 75.8
    assert summary["partition_name"] == "docs"
    assert summary["partition_id"] == 42
    assert summary["axes"] == [
        {
            "axis": "hygiene",
            "score": 80.0,
            "duration_seconds": 1.2,
            "metrics": {
                "sub_scores": {"uniqueness": 90.0},
                "total_docs": 9,
                "total_chunks": 42,
                "total_queries": None,
            },
        }
    ]
    assert "result" not in summary
    assert "chart_data" not in summary["axes"][0]
    assert "details" not in summary["axes"][0]


@pytest.mark.asyncio
async def test_discover_partitions_returns_every_partition():
    vectordb = _FakePartitionVectorDB(
        [
            {"partition": "docs"},
            {"partition": "support"},
            {"partition": ""},
            {},
        ]
    )

    assert await _discover_partitions(vectordb=vectordb) == ["docs", "support"]


@pytest.mark.asyncio
async def test_execute_openrag_audit_run_skips_empty_partition():
    vectordb = _FakeVectorDB()

    result = await execute_openrag_audit_run(
        partition="empty",
        vectordb=vectordb,
        indexer=_FakeIndexer(),
        retention_days=7,
    )

    assert result["status"] == "skipped"
    assert vectordb.updated[0]["document_count"] == 0
    assert vectordb.updated[0]["chunk_count"] == 0
    assert vectordb.cleaned == []


@pytest.mark.asyncio
async def test_execute_openrag_audit_run_persists_completed_result():
    chunks = [
        _chunk("file-a", "1", "Alpha topic content with useful terms and enough text for scoring."),
        _chunk("file-a", "2", "More alpha topic content with useful terms and enough text for scoring."),
        _chunk("file-b", "3", "Beta topic content with different terms and enough text for scoring."),
        _chunk("file-b", "4", "More beta topic content with different terms and enough text for scoring."),
        _chunk("file-c", "5", "Gamma topic content with other terms and enough text for scoring."),
    ]
    files = [
        {
            "file_id": f"file-{name}",
            "filename": f"file-{name}.txt",
            "partition": "docs",
            "source": f"/data/file-{name}.txt",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        for name in ("a", "b", "c")
    ]
    vectordb = _FakeVectorDB(chunks=chunks, files=files)

    result = await execute_openrag_audit_run(
        partition="docs",
        vectordb=vectordb,
        indexer=_FakeIndexer(),
        config={"retrievability": {"max_queries": 0}},
        retention_days=30,
    )

    assert result["status"] == "completed"
    assert vectordb.created[0]["partition"] == "docs"
    assert vectordb.created[0]["partition_id"] == 100
    assert result["overall_score"] is not None
    assert result["overall_grade"] in {"A", "B", "C", "D", "E"}
    assert result["result_json"]["axis_results"]
    assert vectordb.cleaned == [{"partition": "docs", "retention_days": 30}]
