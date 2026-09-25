"""Reproducible retrieval and index configuration snapshots for administrators."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import TYPE_CHECKING, Any

from core.retrieval.trace import canonical_fingerprint

MAX_SNAPSHOT_DOCUMENT_IDS = 10_000

if TYPE_CHECKING:
    from core.ports.document_repo import DocumentRepository
    from services.orchestrators.partition_service import PartitionService
    from services.orchestrators.retrieval_service import RetrievalService


def _installed_version() -> str | None:
    try:
        return package_version("openrag")
    except PackageNotFoundError:
        return None


class RetrievalSnapshotService:
    """Build an allowlisted snapshot without credentials or document content."""

    def __init__(
        self,
        *,
        partition_service: PartitionService,
        document_repo: DocumentRepository,
        retrieval_service: RetrievalService,
        version: str | None = None,
        commit: str | None = None,
    ) -> None:
        self._partitions = partition_service
        self._documents = document_repo
        self._retrieval = retrieval_service
        self._version = version if version is not None else _installed_version()
        self._commit = (commit if commit is not None else os.getenv("OPENRAG_COMMIT")) or None

    async def snapshot(self, partition: str, *, include_document_ids: bool = False) -> dict[str, object]:
        detail = await self._partitions.get_partition_config(partition)
        resolve_plan = getattr(self._retrieval, "resolve_retrieval_plan", None)
        if resolve_plan is not None:
            plan = await resolve_plan([partition], build_execution=False)
            public = plan.public_configuration
            retrieval_fingerprint = plan.configuration_fingerprint
        else:
            public = await self._retrieval.resolved_public_retrieval_configuration([partition])
            retrieval_fingerprint = canonical_fingerprint(public)
        partition_config = (public.get("partitions") or [{}])[0]

        embedder = self._keys(partition_config.get("embedder"), "name", "model")
        embedder["dimensions"] = detail.get("dimension")
        contextualizer = self._keys(
            partition_config.get("contextualizer"),
            "name",
            "model",
            "prompt_name",
        )
        contextualizer["prompt"] = self._keys(
            public.get("contextualizer_prompt"),
            "name",
            "source",
            "content_hash",
        )
        retrieval = self._keys(
            partition_config.get("retrieval"),
            "type",
            "top_k",
            "similarity_threshold",
            "rrf_k",
            "k_queries",
            "combine",
            "with_surrounding_chunks",
            "allow_filterless_fallback",
            "hyde_prompt_name",
            "multi_query_prompt_name",
        )
        retrieval_source = partition_config.get("retrieval")
        query_expansion_prompt = (
            retrieval_source.get("query_expansion_prompt") if isinstance(retrieval_source, dict) else None
        )
        retrieval["query_expansion_prompt"] = (
            self._keys(query_expansion_prompt, "type", "name", "source", "content_hash")
            if isinstance(query_expansion_prompt, dict)
            else None
        )
        configuration = {
            "openrag": {"version": self._version, "commit": self._commit},
            "embedder": embedder,
            "hybrid": self._keys(public.get("hybrid"), "enabled", "fusion"),
            "retrieval": retrieval,
            "reranker": self._keys(
                partition_config.get("reranker"),
                "name",
                "model",
                "enabled",
                "top_n",
            ),
            "contextualizer": contextualizer,
            "expansion": self._keys(
                partition_config.get("expansion"),
                "include_related",
                "include_ancestors",
                "related_limit",
                "max_ancestor_depth",
            ),
        }

        corpus_state = await self._documents.get_indexed_corpus_state(
            partition,
            document_ids_limit=MAX_SNAPSHOT_DOCUMENT_IDS + 1 if include_document_ids else 0,
        )
        index_base: dict[str, object] = {
            "partition": partition,
            "indexed_corpus_count": corpus_state.count,
            "indexed_corpus_digest": corpus_state.digest,
            "created_at": self._json_value(detail.get("created_at")),
        }
        index: dict[str, object] = {
            **index_base,
            "fingerprint": canonical_fingerprint(index_base),
        }
        if include_document_ids:
            index["document_ids"] = list(corpus_state.document_ids[:MAX_SNAPSHOT_DOCUMENT_IDS])
            index["document_ids_limit"] = MAX_SNAPSHOT_DOCUMENT_IDS
            index["document_ids_truncated"] = (
                corpus_state.document_ids_truncated or len(corpus_state.document_ids) > MAX_SNAPSHOT_DOCUMENT_IDS
            )

        fingerprint_index = {
            key: value
            for key, value in index.items()
            if key not in {"document_ids", "document_ids_limit", "document_ids_truncated"}
        }
        return {
            "configuration": configuration,
            "index": index,
            "retrieval_configuration_fingerprint": retrieval_fingerprint,
            "fingerprint": canonical_fingerprint({"configuration": configuration, "index": fingerprint_index}),
        }

    @staticmethod
    def _keys(value: object, *keys: str) -> dict[str, object]:
        source = value if isinstance(value, dict) else {}
        return {key: source.get(key) for key in keys}

    @staticmethod
    def _json_value(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value


__all__ = ["MAX_SNAPSHOT_DOCUMENT_IDS", "RetrievalSnapshotService"]
