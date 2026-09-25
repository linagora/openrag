from __future__ import annotations

import json

import pytest
from core.ports.document_repo import IndexedCorpusState
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
    def __init__(self, digest="corpus-digest", count=2, document_ids=("doc-1", "doc-2")):
        self.digest = digest
        self.count = count
        self.document_ids = document_ids

    async def get_indexed_corpus_state(self, partition, *, document_ids_limit=0):
        assert partition == "legal-rag-bench"
        ids = self.document_ids[:document_ids_limit] if document_ids_limit else ()
        return IndexedCorpusState(
            count=self.count,
            digest=self.digest,
            document_ids=ids,
            document_ids_truncated=len(self.document_ids) > len(ids) if document_ids_limit else False,
        )

    async def list_indexed_documents(self, partition, *, before, after=None, limit=500):
        raise AssertionError("snapshot IDs must come from the corpus-state transaction")


class _ManyDocuments:
    async def get_indexed_corpus_state(self, partition, *, document_ids_limit=0):
        assert partition == "legal-rag-bench"
        return IndexedCorpusState(
            count=10_002,
            digest="many-corpus-digest",
            document_ids=tuple(f"doc-{index:05d}" for index in range(document_ids_limit)),
            document_ids_truncated=True,
        )

    async def list_indexed_documents(self, partition, *, before, after=None, limit=500):
        raise AssertionError("snapshot IDs must come from the corpus-state transaction")


class _Retrieval:
    def __init__(self, prompt_hash="prompt-hash-a"):
        self.prompt_hash = prompt_hash

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
                    "retrieval": {
                        "type": "multiQuery",
                        "top_k": 60,
                        "similarity_threshold": 0.2,
                        "rrf_k": 42,
                        "k_queries": 3,
                        "combine": False,
                        "with_surrounding_chunks": True,
                        "allow_filterless_fallback": True,
                        "hyde_prompt_name": None,
                        "multi_query_prompt_name": "legal-multi-query",
                    },
                    "reranker": {"name": "rerank", "model": "bge-reranker", "enabled": True, "top_n": 10},
                    "contextualizer": {"name": "chat", "model": "qwen", "prompt_name": "legal"},
                    "expansion": {"include_related": False, "include_ancestors": False},
                }
            ],
        }

    async def resolved_public_retrieval_configuration(self, partitions):
        public = self.public_retrieval_configuration(partitions)
        public["partitions"][0]["retrieval"]["query_expansion_prompt"] = {
            "type": "multi_query",
            "name": "legal-multi-query",
            "source": "named",
            "content_hash": "multi-query-hash",
        }
        public["contextualizer_prompt"] = {
            "name": "legal",
            "source": "named",
            "content_hash": self.prompt_hash,
        }
        return public


@pytest.mark.asyncio
async def test_snapshot_is_allowlisted_and_fingerprinted():
    retrieval = _Retrieval()
    service = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents(),
        retrieval_service=retrieval,
        version="2.2.1",
        commit="abc123",
    )

    snapshot = await service.snapshot("legal-rag-bench", include_document_ids=True)

    assert snapshot["configuration"]["reranker"]["top_n"] == 10
    assert snapshot["configuration"]["retrieval"] == {
        "type": "multiQuery",
        "top_k": 60,
        "similarity_threshold": 0.2,
        "rrf_k": 42,
        "k_queries": 3,
        "combine": False,
        "with_surrounding_chunks": True,
        "allow_filterless_fallback": True,
        "hyde_prompt_name": None,
        "multi_query_prompt_name": "legal-multi-query",
        "query_expansion_prompt": {
            "type": "multi_query",
            "name": "legal-multi-query",
            "source": "named",
            "content_hash": "multi-query-hash",
        },
    }
    assert snapshot["retrieval_configuration_fingerprint"] == canonical_fingerprint(
        await retrieval.resolved_public_retrieval_configuration(["legal-rag-bench"])
    )
    assert snapshot["configuration"]["embedder"]["dimensions"] == 768
    assert snapshot["index"]["indexed_corpus_count"] == 2
    assert snapshot["index"]["document_ids"] == ["doc-1", "doc-2"]
    assert snapshot["index"]["document_ids_truncated"] is False
    assert snapshot["index"]["indexed_corpus_digest"] == "corpus-digest"
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
async def test_prompt_hash_changes_retrieval_and_snapshot_fingerprints_without_exposing_content():
    first = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents(),
        retrieval_service=_Retrieval("prompt-hash-a"),
    )
    second = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents(),
        retrieval_service=_Retrieval("prompt-hash-b"),
    )

    first_snapshot = await first.snapshot("legal-rag-bench")
    second_snapshot = await second.snapshot("legal-rag-bench")

    assert first_snapshot["configuration"]["contextualizer"]["prompt"] == {
        "name": "legal",
        "source": "named",
        "content_hash": "prompt-hash-a",
    }
    assert (
        first_snapshot["retrieval_configuration_fingerprint"] != second_snapshot["retrieval_configuration_fingerprint"]
    )
    assert first_snapshot["fingerprint"] != second_snapshot["fingerprint"]
    assert "private contextualizer instructions" not in json.dumps(first_snapshot)


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


@pytest.mark.asyncio
async def test_complete_corpus_digest_changes_same_sized_index_identity():
    first = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents("digest-a"),
        retrieval_service=_Retrieval(),
    )
    second = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents("digest-b"),
        retrieval_service=_Retrieval(),
    )

    first_snapshot = await first.snapshot("legal-rag-bench")
    second_snapshot = await second.snapshot("legal-rag-bench")

    assert first_snapshot["index"]["fingerprint"] != second_snapshot["index"]["fingerprint"]
    assert first_snapshot["fingerprint"] != second_snapshot["fingerprint"]


@pytest.mark.asyncio
async def test_snapshot_uses_count_from_the_same_corpus_state_as_digest():
    service = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents(count=7),
        retrieval_service=_Retrieval(),
    )

    snapshot = await service.snapshot("legal-rag-bench")

    assert snapshot["index"]["indexed_corpus_count"] == 7


@pytest.mark.asyncio
async def test_snapshot_normalizes_blank_commit_metadata(monkeypatch):
    monkeypatch.setenv("OPENRAG_COMMIT", "")
    service = RetrievalSnapshotService(
        partition_service=_Partitions(),
        document_repo=_Documents(),
        retrieval_service=_Retrieval(),
    )

    snapshot = await service.snapshot("legal-rag-bench")

    assert snapshot["configuration"]["openrag"]["commit"] is None
