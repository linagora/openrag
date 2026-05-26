from __future__ import annotations

import asyncio
from typing import Any

import ray
from services.workers.indexer_actor import IndexerWorker


@ray.remote
class IndexerPool:
    """Thin Ray actor wrapper around ``IndexerWorker``."""

    def __init__(self) -> None:
        import services.inference.vllm_client  # noqa: F401
        from config import load_config
        from core.embeddings import embedder_registry
        from services.storage.milvus_store import MilvusVectorStore
        from services.storage.postgres_store import PostgresStore
        from services.workers.parsers.doc_serializer_bridge import DocSerializerBridgeParser
        from services.workers.pipeline_builder import build_indexing_pipeline

        cfg = load_config()

        parser = DocSerializerBridgeParser(config=cfg)
        chunker = _build_chunker(cfg)

        embed_cfg = cfg.embedder
        embedder = embedder_registry.create(
            "vllm",
            endpoint=embed_cfg.base_url,
            model_name=embed_cfg.model_name,
            api_key=embed_cfg.api_key,
            max_model_len=embed_cfg.max_model_len,
        )
        self._vector_store = MilvusVectorStore(cfg.vectordb)
        task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")
        pipeline = build_indexing_pipeline(
            parser=parser,
            chunker=chunker,
            embedder=embedder,
            vector_store=self._vector_store,
        )
        rdb_cfg = cfg.rdb.model_copy(update={"database": f"partitions_for_collection_{cfg.vectordb.collection_name}"})
        self._catalog_store = PostgresStore(rdb_cfg, run_migrations=False)
        self._catalog_initialized = False
        self._worker = IndexerWorker(
            pipeline=pipeline,
            task_state_manager=task_state_manager,
            document_repo=self._catalog_store.document_repo,
        )

    async def _ensure_catalog(self) -> None:
        if not self._catalog_initialized:
            await self._catalog_store.initialize()
            self._catalog_initialized = True

    async def process_file(
        self,
        *,
        task_id: str,
        path: str,
        metadata: dict[str, Any],
        partition: str,
        user: dict[str, Any] | None = None,
        workspace_ids: list[str] | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        await self._ensure_catalog()
        result = await self._worker.process_file(
            task_id=task_id,
            path=path,
            metadata=metadata,
            partition=partition,
            user=user,
            workspace_ids=workspace_ids,
            replace=replace,
        )
        file_id = metadata.get("file_id", "")
        if workspace_ids and not replace and file_id:
            try:
                await asyncio.gather(
                    *(
                        self._catalog_store.workspace_repo.add_files_to_workspace(workspace_id, [file_id])
                        for workspace_id in workspace_ids
                    )
                )
            except Exception:
                pass
        return result


def build_indexer_pool(namespace: str = "openrag") -> Any:
    return IndexerPool.options(  # type: ignore[attr-defined]
        name="IndexerPool",
        namespace=namespace,
        get_if_exists=True,
    ).remote()


def _build_chunker(cfg: Any) -> Any:
    from components.indexer.chunker.chunker import ChunkerFactory

    legacy_chunker = ChunkerFactory.create_chunker(cfg)
    if hasattr(legacy_chunker, "chunk"):
        return legacy_chunker
    core_chunker = getattr(legacy_chunker, "_core_splitter", None)
    if core_chunker is None or not hasattr(core_chunker, "chunk"):
        raise TypeError("Configured chunker does not expose a chunk(document, partition) method")
    return core_chunker


__all__ = ["IndexerPool", "build_indexer_pool"]
