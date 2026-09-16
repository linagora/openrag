"""Public, reproducible retrieval and index configuration snapshots."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import TYPE_CHECKING, Any

from core.retrieval.trace import canonical_fingerprint

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
        self._commit = commit if commit is not None else os.getenv("OPENRAG_COMMIT")

    async def snapshot(self, partition: str, *, include_document_ids: bool = False) -> dict[str, object]:
        detail = await self._partitions.get_partition_config(partition)
        public = self._retrieval.public_retrieval_configuration([partition])
        partition_config = (public.get("partitions") or [{}])[0]

        embedder = self._keys(partition_config.get("embedder"), "name", "model")
        embedder["dimensions"] = detail.get("dimension")
        configuration = {
            "openrag": {"version": self._version, "commit": self._commit},
            "embedder": embedder,
            "hybrid": self._keys(public.get("hybrid"), "enabled", "fusion"),
            "retrieval": self._keys(
                partition_config.get("retrieval"),
                "type",
                "top_k",
                "similarity_threshold",
            ),
            "reranker": self._keys(
                partition_config.get("reranker"),
                "name",
                "model",
                "enabled",
                "top_n",
            ),
            "contextualizer": self._keys(
                partition_config.get("contextualizer"),
                "name",
                "model",
                "prompt_name",
            ),
            "expansion": self._keys(
                partition_config.get("expansion"),
                "include_related",
                "include_ancestors",
                "related_limit",
                "max_ancestor_depth",
            ),
        }

        index_base: dict[str, object] = {
            "partition": partition,
            "indexed_corpus_count": detail.get("document_count"),
            "created_at": self._json_value(detail.get("created_at")),
        }
        index: dict[str, object] = {
            **index_base,
            "fingerprint": canonical_fingerprint(index_base),
        }
        if include_document_ids:
            index["document_ids"] = await self._document_ids(partition)

        fingerprint_index = {key: value for key, value in index.items() if key != "document_ids"}
        return {
            "configuration": configuration,
            "index": index,
            "fingerprint": canonical_fingerprint(
                {"configuration": configuration, "index": fingerprint_index}
            ),
        }

    async def _document_ids(self, partition: str) -> list[str]:
        before = datetime.now(UTC)
        after: str | None = None
        document_ids: list[str] = []
        while True:
            page = await self._documents.list_indexed_documents(
                partition,
                before=before,
                after=after,
                limit=1000,
            )
            document_ids.extend(page)
            if len(page) < 1000:
                break
            after = page[-1]
        return sorted(set(document_ids))

    @staticmethod
    def _keys(value: object, *keys: str) -> dict[str, object]:
        source = value if isinstance(value, dict) else {}
        return {key: source.get(key) for key in keys}

    @staticmethod
    def _json_value(value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value


__all__ = ["RetrievalSnapshotService"]
