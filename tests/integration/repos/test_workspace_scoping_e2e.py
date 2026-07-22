"""End-to-end regression test for issue #706 — workspace scoping.

Exercises the real fix across the two systems that matter: Postgres (workspace
membership, via ``WorkspaceService.resolve_scope``) and Milvus (the actual
vector search, via ``VectorStoreSearcher``). No LLM/embedder service is
needed — embeddings are hand-crafted and deterministic, same pattern as
``test_milvus_store_integration.py``.

Scenario (matches the issue's end-to-end verification plan):
    * partition A contains ``included-file`` and ``excluded-file``
    * partition B contains a *different* file sharing the same ``file_id`` as
      the included one — proving file_id-only filtering can't leak across
      partitions once the workspace's owning partition is enforced
    * a workspace in partition A contains only ``included-file``

A workspace-scoped, cross-partition ("all") search must return only the
included file's chunks. Removing the file from the workspace must then
return zero results — never fall back to the full partition.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator

import pytest
from core.config.infrastructure import VectorDBConfig
from core.embeddings.embedder import Embedder
from core.models.catalog import DocumentRecord
from core.models.chunk import Chunk
from core.models.workspace import Workspace
from services.orchestrators.workspace_service import WorkspaceService
from services.storage.milvus_store import MilvusVectorStore
from services.storage.postgres_store import PostgresStore
from services.storage.vector_store_searcher import VectorStoreSearcher

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_DIM = 4
_QUERY_VEC = [1.0, 0.0, 0.0, 0.0]


class _FixedEmbedder(Embedder):
    """Always embeds to the same vector — this test is about scoping, not
    ranking quality, so every chunk is an equally-good nearest neighbor and
    filtering is the only thing that can exclude one."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(_QUERY_VEC) for _ in texts]

    async def embed_single(self, text: str) -> list[float]:
        return list(_QUERY_VEC)

    @property
    def dimension(self) -> int:
        return _DIM


def _milvus_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def milvus_host_port() -> tuple[str, int]:
    host = os.getenv("OPENRAG_TEST_VDB_HOST", "localhost")
    port = int(os.getenv("OPENRAG_TEST_VDB_PORT", "19530"))
    return host, port


@pytest.fixture(scope="module")
def _live_milvus(milvus_host_port: tuple[str, int]) -> None:
    host, port = milvus_host_port
    if not _milvus_reachable(host, port):
        pytest.skip(f"Milvus not reachable at {host}:{port} — skipping integration tests")


@pytest.fixture
async def milvus_store(_live_milvus: None, milvus_host_port: tuple[str, int]) -> Iterator[MilvusVectorStore]:
    host, port = milvus_host_port
    collection = f"itest_ws706_{uuid.uuid4().hex[:12]}"
    config = VectorDBConfig(host=host, port=port, collection_name=collection, hybrid_search=False, schema_version=1)
    store = MilvusVectorStore(config)
    await store.initialize(_DIM)
    try:
        yield store
    finally:
        await store.drop_collection(collection)


@pytest.fixture
def searcher(milvus_store: MilvusVectorStore) -> VectorStoreSearcher:
    return VectorStoreSearcher(
        vector_store=milvus_store,
        embedder=_FixedEmbedder(),
        document_repo=None,  # not exercised — no related/ancestor expansion here
        collection=milvus_store._collection_name,
    )


def _chunk(partition: str, file_id: str, text: str) -> Chunk:
    return Chunk(document_id=file_id, text=text, partition=partition, embedding=list(_QUERY_VEC))


async def _seed_document(postgres_store: PostgresStore, partition: str, file_id: str) -> None:
    await postgres_store.document_repo.create_document(
        DocumentRecord(id=f"{partition}-{file_id}", file_id=file_id, partition=partition, filename=f"{file_id}.txt"),
    )


class TestWorkspaceScopingAcrossPartitions:
    async def test_workspace_scoped_search_excludes_sibling_and_cross_partition_file(
        self,
        postgres_store: PostgresStore,
        milvus_store: MilvusVectorStore,
        searcher: VectorStoreSearcher,
    ):
        part_a, part_b = f"a-{uuid.uuid4().hex[:8]}", f"b-{uuid.uuid4().hex[:8]}"
        await postgres_store.partition_repo.create_partition(part_a)
        await postgres_store.partition_repo.create_partition(part_b)
        for fid in ("included-file", "excluded-file"):
            await _seed_document(postgres_store, part_a, fid)
        await _seed_document(postgres_store, part_b, "included-file")  # same file_id, different partition

        await milvus_store.upsert(
            [
                _chunk(part_a, "included-file", "the one that should come back"),
                _chunk(part_a, "excluded-file", "must never be returned"),
                _chunk(part_b, "included-file", "same file_id, wrong partition — must never leak"),
            ]
        )

        workspace_service = WorkspaceService(
            workspace_repo=postgres_store.workspace_repo,
            document_repo=postgres_store.document_repo,
            vector_store=milvus_store,
            collection=milvus_store._collection_name,
        )
        ws_id = f"ws-{uuid.uuid4().hex[:8]}"
        await postgres_store.workspace_repo.create_workspace(Workspace(workspace_id=ws_id, partition=part_a))
        missing = await workspace_service.add_files(ws_id, ["included-file"])
        assert missing == []

        # A caller with access to both partitions searches "all" with the workspace set.
        scope = await workspace_service.resolve_scope(ws_id, [part_a, part_b])
        assert scope is not None
        assert scope.partition == part_a  # narrowed to the workspace's own partition

        results = await searcher.search(
            query="anything",
            partition=[scope.partition],  # production code narrows to this, not [part_a, part_b]
            top_k=10,
            filter_params={"file_id": scope.file_ids},
            with_surrounding_chunks=False,
        )
        texts = {c.text for c in results}
        assert texts == {"the one that should come back"}

        # Now unassign the file — the workspace is valid but empty.
        assert await workspace_service.remove_file(ws_id, "included-file") is True
        empty_scope = await workspace_service.resolve_scope(ws_id, [part_a, part_b])
        assert empty_scope.file_ids == []

        empty_results = await searcher.search(
            query="anything",
            partition=[empty_scope.partition],
            top_k=10,
            filter_params={"file_id": empty_scope.file_ids},
            with_surrounding_chunks=False,
        )
        # Must be empty — never fall back to every document in the partition.
        assert empty_results == []

    async def test_resolve_scope_none_for_unknown_workspace(self, postgres_store: PostgresStore):
        workspace_service = WorkspaceService(
            workspace_repo=postgres_store.workspace_repo,
            document_repo=postgres_store.document_repo,
            vector_store=None,
            collection="unused",
        )
        assert await workspace_service.resolve_scope("does-not-exist", ["all"]) is None
