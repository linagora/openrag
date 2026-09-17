from __future__ import annotations

import json

import pytest
from core.retrieval.trace import canonical_fingerprint
from services.orchestrators.retrieval_snapshot_service import RetrievalSnapshotService


class _Partitions:
    async def get_partition_config(self, partition):
        return {
            "partition": partition,
            "created_at": "2026-09-16T12:00:00+00:00",
            "document_count": 2,
            "dimension": 768,
        }


class _Documents:
    async def list_indexed_documents(self, partition, *, before, after=None, limit=500):
        assert partition == "legal-rag-bench"
        assert limit == 1000
        return ["doc-1", "doc-2"] if after is None else []


class _ManyDocuments:
    def __init__(self):
        self.requested_limits = []

    async def list_indexed_documents(self, partition, *, before, after=None, limit=500):
        self.requested_limits.append(limit)
        start = int(after.removeprefix("doc-")) + 1 if after is not None else 0
        return [f"doc-{index:05d}" for index in range(start, min(start + limit, 10_002))]


class _Retrieval:
    @staticmethod
    def configuration_fingerprint(_partitions):
        return "retrieval-fingerprint"

    @staticmethod
    def public_retrieval_configuration(partitions):
        assert partitions == ["legal-rag-bench"]
        return {
            "hybrid": {"enabled": True, "fusion": "rrf"},
            "partitions": [
                {
                    "name": "legal-rag-bench",
                    "embedder": {"name": "embed", "model": "bge", "api_key": "must-not-leak"},
                    "retrieval": {"type": "single", "top_k": 60, "similarity_threshold": 0.2},
                    "reranker": {"name": "rerank", "model": "bge-reranker", "enabled": True, "top_n": 10},
                    "contextualizer": {"name": "chat", "model": "qwen", "prompt_name": "legal"},
                    "expansion": {"include_related": False, "include_ancestors": False},
                }
            ],
        }


@pytest.mark.asyncio
async def test_snapshot_is_allowlisted_and_fingerprinted():
    service = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents(),
        retrieval_service=_Retrieval(),
        version="2.2.1",
        commit="abc123",
    )

    snapshot = await service.snapshot("legal-rag-bench", include_document_ids=True)

    assert snapshot["configuration"]["reranker"]["top_n"] == 10
    assert snapshot["retrieval_configuration_fingerprint"] == "retrieval-fingerprint"
    assert snapshot["configuration"]["embedder"]["dimensions"] == 768
    assert snapshot["index"]["document_ids"] == ["doc-1", "doc-2"]
    assert snapshot["index"]["document_ids_truncated"] is False
    serialized = json.dumps(snapshot)
    assert "api_key" not in serialized
    assert "must-not-leak" not in serialized
    fingerprint_index = {
        key: value
        for key, value in snapshot["index"].items()
        if key not in {"document_ids", "document_ids_limit", "document_ids_truncated"}
    }
    assert snapshot["fingerprint"] == canonical_fingerprint(
        {"configuration": snapshot["configuration"], "index": fingerprint_index}
    )


@pytest.mark.asyncio
async def test_document_ids_are_opt_in_and_do_not_change_fingerprint():
    service = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents(),
        retrieval_service=_Retrieval(),
        version="2.2.1",
        commit=None,
    )

    compact = await service.snapshot("legal-rag-bench")
    expanded = await service.snapshot("legal-rag-bench", include_document_ids=True)

    assert "document_ids" not in compact["index"]
    assert compact["fingerprint"] == expanded["fingerprint"]


@pytest.mark.asyncio
async def test_document_ids_are_bounded_and_report_truncation():
    documents = _ManyDocuments()
    service = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=documents,
        retrieval_service=_Retrieval(),
        version="2.2.1",
        commit=None,
    )

    snapshot = await service.snapshot("legal-rag-bench", include_document_ids=True)

    assert len(snapshot["index"]["document_ids"]) == 10_000
    assert snapshot["index"]["document_ids_truncated"] is True
    assert snapshot["index"]["document_ids_limit"] == 10_000
    assert documents.requested_limits[-1] == 1
