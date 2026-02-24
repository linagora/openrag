from typing import Any

from components.mcp.interfaces import BaseSearchGateway, SearchRequest


class SearchToolService:
    def __init__(
        self,
        gateway: BaseSearchGateway,
        default_top_k: int = 5,
        max_top_k: int = 50,
        similarity_threshold: float = 0.8,
    ):
        self.gateway = gateway
        self.default_top_k = default_top_k
        self.max_top_k = max_top_k
        self.similarity_threshold = similarity_threshold

    async def search_documents(
        self,
        query: str,
        partitions: list[str] | None = None,
        top_k: int | None = None,
        file_id: str | None = None,
        allowed_partitions: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("Query cannot be empty")

        normalized_partitions = partitions or ["all"]
        if not normalized_partitions:
            normalized_partitions = ["all"]

        normalized_partitions = [partition.strip() for partition in normalized_partitions if partition and partition.strip()]
        if not normalized_partitions:
            normalized_partitions = ["all"]

        normalized_partitions = self._enforce_partition_scope(
            requested_partitions=normalized_partitions,
            allowed_partitions=allowed_partitions,
        )

        effective_top_k = top_k if top_k is not None else self.default_top_k
        if effective_top_k <= 0:
            raise ValueError("top_k must be greater than 0")
        if effective_top_k > self.max_top_k:
            effective_top_k = self.max_top_k

        request = SearchRequest(
            query=normalized_query,
            partitions=normalized_partitions,
            top_k=effective_top_k,
            similarity_threshold=self.similarity_threshold,
            file_id=file_id,
        )

        results = await self.gateway.search(request)

        documents = [
            {
                "chunk_id": item.metadata.get("_id"),
                "content": item.content,
                "metadata": item.metadata,
            }
            for item in results
        ]

        return {
            "query": normalized_query,
            "partitions": normalized_partitions,
            "top_k": effective_top_k,
            "count": len(documents),
            "documents": documents,
        }

    def _enforce_partition_scope(
        self,
        requested_partitions: list[str],
        allowed_partitions: list[str] | None,
    ) -> list[str]:
        if allowed_partitions is None:
            raise PermissionError("Authentication context is missing")

        if allowed_partitions == ["all"]:
            if requested_partitions == ["all"]:
                return ["all"]
            return requested_partitions

        if requested_partitions == ["all"]:
            return allowed_partitions

        denied = [partition for partition in requested_partitions if partition not in allowed_partitions]
        if denied:
            denied_list = ", ".join(sorted(set(denied)))
            raise PermissionError(f"Access denied for partition(s): {denied_list}")

        return requested_partitions
