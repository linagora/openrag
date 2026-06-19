from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import ray
from services.workers.indexer_actor import IndexerWorker


@ray.remote
class IndexerPool:
    """Thin Ray actor wrapper around ``IndexerWorker``."""

    def __init__(self) -> None:
        import services.inference.ollama_client  # noqa: F401
        import services.inference.vllm_client  # noqa: F401
        from core.config import load_config
        from core.embeddings import embedder_registry
        from services.storage.milvus_store import MilvusVectorStore
        from services.storage.postgres_store import PostgresStore
        from services.workers.parsers.doc_serializer_bridge import DocSerializerBridgeParser
        from services.workers.pipeline_builder import build_indexing_pipeline

        cfg = load_config()

        parser = DocSerializerBridgeParser(config=cfg)
        chunker = _build_chunker(cfg)
        embedder_factory = _build_embedder_factory(cfg)
        contextualizer_factory = _build_contextualizer_factory(cfg)

        embed_cfg = cfg.embedder
        embedder = embedder_registry.create(
            "vllm",
            endpoint=embed_cfg.base_url,
            model_name=embed_cfg.model_name,
            api_key=embed_cfg.api_key,
            max_model_len=embed_cfg.max_model_len,
            timeout=embed_cfg.timeout,
            batch_size=embed_cfg.batch_size,
            embed_concurrency=embed_cfg.embed_concurrency,
        )
        self._vector_store = MilvusVectorStore(cfg.vectordb)
        task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")
        pipeline = build_indexing_pipeline(
            parser=parser,
            chunker=chunker,
            embedder=embedder,
            vector_store=self._vector_store,
            chunker_factory=_build_chunker_from_config,
            embedder_factory=embedder_factory,
            contextualizer_factory=contextualizer_factory,
        )
        rdb_cfg = cfg.rdb.model_copy(update={"database": f"partitions_for_collection_{cfg.vectordb.collection_name}"})
        self._catalog_store = PostgresStore(rdb_cfg, run_migrations=False)
        self._catalog_initialized = False
        self._catalog_init_lock = asyncio.Lock()
        self._worker = IndexerWorker(
            pipeline=pipeline,
            task_state_manager=task_state_manager,
            document_repo=self._catalog_store.document_repo,
        )

    async def _ensure_catalog(self) -> None:
        if self._catalog_initialized:
            return
        async with self._catalog_init_lock:
            if self._catalog_initialized:
                return
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
        indexation_config: dict[str, Any] | None = None,
        embedder_name: str | None = None,
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
            indexation_config=indexation_config,
            embedder_name=embedder_name,
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
    from core.config import load_config

    cfg = load_config()
    max_concurrency = max(1, cfg.ray.max_tasks_per_worker)
    return IndexerPool.options(  # type: ignore[attr-defined]
        name="IndexerPool",
        namespace=namespace,
        get_if_exists=True,
        lifetime="detached",
        max_concurrency=max_concurrency,
    ).remote()


def _build_chunker(cfg: Any) -> Any:
    from core.chunking.factory import create_chunker

    chunker = create_chunker(cfg)
    if not callable(getattr(chunker, "chunk", None)):
        raise TypeError("Configured chunker does not expose a chunk(document, partition) method")
    return chunker


def _build_chunker_from_config(chunker_config: Any) -> Any:
    return _build_chunker(SimpleNamespace(chunker=chunker_config))


def _build_embedder_factory(cfg: Any) -> Any:
    if not getattr(cfg.models, "embedder", None):
        return None

    from core.embeddings import embedder_registry

    cache: dict[str, Any] = {}
    lock = threading.Lock()

    def factory(name: str = "default") -> Any:
        if name in cache:
            return cache[name]
        with lock:
            if name in cache:
                return cache[name]
            model_cfg = cfg.models.embedder.get(name)
            if model_cfg is None:
                raise KeyError(f"Unknown embedder '{name}'. Available: {list(cfg.models.embedder)}")
            impl_kwargs = {key: value for key, value in model_cfg.extra.items() if key != "implementation"}
            impl = model_cfg.extra.get("implementation", "vllm")
            instance = embedder_registry.create(
                impl,
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                batch_size=model_cfg.batch_size,
                timeout=model_cfg.timeout,
                **impl_kwargs,
            )
            cache[name] = instance
            return instance

    return factory


def _build_contextualizer_factory(cfg: Any) -> Any:
    import services.inference.ollama_client  # noqa: F401
    import services.inference.vllm_client  # noqa: F401
    from core.indexing.contextualize import ChunkContextualizer
    from core.llm import llm_registry
    from core.prompts import load_template_by_key

    named_llms = getattr(getattr(cfg, "models", None), "llm", {}) or {}
    fallback_cfg = _global_llm_endpoint_config(cfg)
    if not named_llms and fallback_cfg is None:
        return None

    system_prompt = load_template_by_key(cfg.paths.prompts_dir, cfg.prompts, "chunk_contextualizer")
    cache: dict[str, ChunkContextualizer] = {}
    lock = threading.Lock()

    def factory(name: str = "default") -> ChunkContextualizer:
        if name in cache:
            return cache[name]
        with lock:
            if name in cache:
                return cache[name]
            model_cfg = named_llms.get(name)
            if model_cfg is None:
                if name == "default" and fallback_cfg is not None:
                    model_cfg = fallback_cfg
                else:
                    raise KeyError(f"Unknown llm '{name}'. Available: {list(named_llms)}")
            impl_kwargs = {key: value for key, value in model_cfg.extra.items() if key != "implementation"}
            impl = model_cfg.extra.get("implementation", "vllm")
            llm = llm_registry.create(
                impl,
                endpoint=model_cfg.endpoint,
                model_name=model_cfg.model_name,
                timeout=model_cfg.timeout,
                **impl_kwargs,
            )
            contextualizer = ChunkContextualizer(
                llm,
                system_prompt,
                timeout_seconds=cfg.chunker.contextualization_timeout,
                max_concurrent=cfg.chunker.max_concurrent_contextualization,
            )
            cache[name] = contextualizer
            return contextualizer

    return factory


def _global_llm_endpoint_config(cfg: Any) -> Any | None:
    from core.config.model_endpoints import ModelEndpointConfig

    llm_cfg = getattr(cfg, "llm", None)
    endpoint = getattr(llm_cfg, "base_url", "")
    model_name = getattr(llm_cfg, "model", "")
    if not endpoint or not model_name:
        return None
    extra = {
        "implementation": "vllm",
        "api_key": getattr(llm_cfg, "api_key", ""),
        "temperature": getattr(llm_cfg, "temperature", 0.1),
        "max_retries": getattr(llm_cfg, "max_retries", 2),
        "logprobs": getattr(llm_cfg, "logprobs", True),
    }
    return ModelEndpointConfig(
        endpoint=endpoint,
        model_name=model_name,
        timeout=getattr(llm_cfg, "timeout", 60),
        extra=extra,
    )


__all__ = ["IndexerPool", "build_indexer_pool"]
